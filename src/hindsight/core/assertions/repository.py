import json
import time
from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID, uuid4

from hindsight.core.assertions.models import (
    Assertion,
    AssertionConflictError,
    AssertionDraft,
    AssertionNotFoundError,
    TemporalLookup,
)
from hindsight.core.assertions.temporal_queries import (
    ASSERTION_HISTORY_SQL,
    CURRENT_TRUTH_SQL,
    EXISTING_RECORDING_SQL,
    INSERT_ASSERTION_SQL,
    KNOWN_AT_DECISION_SQL,
    LATEST_ACTIVE_SQL,
    SUPERSEDE_ASSERTION_SQL,
    TEMPORAL_SNAPSHOT_SQL,
)


class AssertionRepository(Protocol):
    def append(self, draft: AssertionDraft) -> Assertion: ...

    def current_truth(self, lookup: TemporalLookup) -> Assertion: ...

    def known_at_decision(self, lookup: TemporalLookup) -> Assertion: ...

    def temporal_snapshot(self, lookup: TemporalLookup) -> tuple[Assertion, Assertion]: ...

    def history(self, assertion_key: str) -> list[Assertion]: ...


class InMemoryAssertionRepository:
    def __init__(self) -> None:
        self._assertions: list[Assertion] = []

    def append(self, draft: AssertionDraft) -> Assertion:
        existing = next(
            (
                item
                for item in self._assertions
                if item.assertion_key == draft.assertion_key
                and item.recorded_at == draft.recorded_at
            ),
            None,
        )
        if existing is not None:
            _ensure_same_recording(existing, draft)
            return existing

        active_index = next(
            (
                index
                for index, item in enumerate(self._assertions)
                if item.assertion_key == draft.assertion_key and item.superseded_at is None
            ),
            None,
        )
        previous = self._assertions[active_index] if active_index is not None else None
        _ensure_chronological(previous, draft)

        assertion = _build_assertion(draft, previous)
        if active_index is not None and previous is not None:
            self._assertions[active_index] = replace(
                previous,
                superseded_at=draft.recorded_at,
                superseded_by=assertion.id,
            )
        self._assertions.append(assertion)
        return assertion

    def current_truth(self, lookup: TemporalLookup) -> Assertion:
        candidates = [
            item
            for item in self._assertions
            if _matches_lookup(item, lookup)
            and item.valid_from <= lookup.event_time
            and (item.valid_until is None or item.valid_until > lookup.event_time)
        ]
        return _latest(candidates, key=lambda item: (item.recorded_at, item.version_number))

    def known_at_decision(self, lookup: TemporalLookup) -> Assertion:
        candidates = [
            item
            for item in self._assertions
            if _matches_lookup(item, lookup)
            and item.valid_from <= lookup.event_time
            and (item.valid_until is None or item.valid_until > lookup.event_time)
            and item.recorded_at <= lookup.decision_time
        ]
        return _latest(candidates, key=lambda item: (item.recorded_at, item.version_number))

    def temporal_snapshot(self, lookup: TemporalLookup) -> tuple[Assertion, Assertion]:
        return self.current_truth(lookup), self.known_at_decision(lookup)

    def history(self, assertion_key: str) -> list[Assertion]:
        return sorted(
            (item for item in self._assertions if item.assertion_key == assertion_key),
            key=lambda item: item.version_number,
        )


class CockroachAssertionRepository:
    def __init__(self, connection: Any, max_retries: int = 3) -> None:
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        self._connection = connection
        self._max_retries = max_retries

    def append(self, draft: AssertionDraft) -> Assertion:
        for attempt in range(self._max_retries + 1):
            try:
                return self._append_once(draft)
            except Exception as error:
                sqlstate = getattr(error, "sqlstate", None)
                if sqlstate == "23505":
                    existing = self._existing_recording(draft)
                    if existing is not None:
                        _ensure_same_recording(existing, draft)
                        return existing
                if sqlstate not in {"40001", "23505"} or attempt == self._max_retries:
                    raise
                time.sleep(0.05 * 2**attempt)
        raise RuntimeError("unreachable retry state")

    def _append_once(self, draft: AssertionDraft) -> Assertion:
        with self._connection.transaction():
            existing_row = self._connection.execute(
                EXISTING_RECORDING_SQL,
                (draft.assertion_key, draft.recorded_at),
            ).fetchone()
            if existing_row is not None:
                existing = _from_row(existing_row)
                _ensure_same_recording(existing, draft)
                return existing

            previous_row = self._connection.execute(
                LATEST_ACTIVE_SQL,
                (draft.assertion_key,),
            ).fetchone()
            previous = _from_row(previous_row) if previous_row is not None else None
            _ensure_chronological(previous, draft)
            assertion = _build_assertion(draft, previous)

            self._connection.execute(
                INSERT_ASSERTION_SQL,
                (
                    assertion.id,
                    assertion.assertion_key,
                    assertion.lineage_id,
                    assertion.version_number,
                    assertion.domain,
                    assertion.subject_type,
                    assertion.subject_id,
                    assertion.predicate,
                    json.dumps(assertion.value_json),
                    assertion.value_number,
                    assertion.value_text,
                    assertion.unit,
                    assertion.currency,
                    assertion.valid_from,
                    assertion.valid_until,
                    assertion.recorded_at,
                    assertion.written_by,
                    assertion.source_id,
                    assertion.confidence,
                ),
            )
            if previous is not None:
                self._connection.execute(
                    SUPERSEDE_ASSERTION_SQL,
                    (assertion.recorded_at, assertion.id, previous.id),
                )
            return assertion

    def _existing_recording(self, draft: AssertionDraft) -> Assertion | None:
        row = self._connection.execute(
            EXISTING_RECORDING_SQL,
            (draft.assertion_key, draft.recorded_at),
        ).fetchone()
        return _from_row(row) if row is not None else None

    def current_truth(self, lookup: TemporalLookup) -> Assertion:
        row = self._connection.execute(
            CURRENT_TRUTH_SQL,
            (
                lookup.assertion_key,
                lookup.domain,
                lookup.subject_type,
                lookup.subject_id,
                lookup.predicate,
                lookup.event_time,
                lookup.event_time,
            ),
        ).fetchone()
        return _required_row(row, lookup)

    def known_at_decision(self, lookup: TemporalLookup) -> Assertion:
        row = self._connection.execute(
            KNOWN_AT_DECISION_SQL,
            (
                lookup.assertion_key,
                lookup.domain,
                lookup.subject_type,
                lookup.subject_id,
                lookup.predicate,
                lookup.event_time,
                lookup.event_time,
                lookup.decision_time,
            ),
        ).fetchone()
        return _required_row(row, lookup)

    def temporal_snapshot(self, lookup: TemporalLookup) -> tuple[Assertion, Assertion]:
        rows = self._connection.execute(
            TEMPORAL_SNAPSHOT_SQL,
            (
                lookup.assertion_key,
                lookup.domain,
                lookup.subject_type,
                lookup.subject_id,
                lookup.predicate,
                lookup.event_time,
                lookup.event_time,
                lookup.assertion_key,
                lookup.domain,
                lookup.subject_type,
                lookup.subject_id,
                lookup.predicate,
                lookup.event_time,
                lookup.event_time,
                lookup.decision_time,
            ),
        ).fetchall()
        snapshot = {str(row["snapshot_kind"]): _from_row(row) for row in rows}
        try:
            return snapshot["current_truth"], snapshot["known_at_decision"]
        except KeyError as error:
            raise AssertionNotFoundError(
                f"incomplete temporal snapshot for {lookup.domain}/{lookup.subject_id}"
            ) from error

    def history(self, assertion_key: str) -> list[Assertion]:
        rows = self._connection.execute(ASSERTION_HISTORY_SQL, (assertion_key,)).fetchall()
        return [_from_row(row) for row in rows]


def _build_assertion(draft: AssertionDraft, previous: Assertion | None) -> Assertion:
    return Assertion(
        id=uuid4(),
        lineage_id=previous.lineage_id if previous else uuid4(),
        version_number=previous.version_number + 1 if previous else 1,
        assertion_key=draft.assertion_key,
        domain=draft.domain,
        subject_type=draft.subject_type,
        subject_id=draft.subject_id,
        predicate=draft.predicate,
        value_json=draft.value_json,
        value_number=draft.value_number,
        value_text=draft.value_text,
        unit=draft.unit,
        currency=draft.currency,
        valid_from=draft.valid_from,
        valid_until=draft.valid_until,
        recorded_at=draft.recorded_at,
        written_by=draft.written_by,
        source_id=draft.source_id,
        confidence=draft.confidence,
    )


def _ensure_chronological(previous: Assertion | None, draft: AssertionDraft) -> None:
    if previous is None:
        return
    previous_identity = (
        previous.domain,
        previous.subject_type,
        previous.subject_id,
        previous.predicate,
        previous.unit,
        previous.currency,
    )
    draft_identity = (
        draft.domain,
        draft.subject_type,
        draft.subject_id,
        draft.predicate,
        draft.unit,
        draft.currency,
    )
    if previous_identity != draft_identity:
        raise AssertionConflictError("an assertion key cannot change fact identity")
    if draft.recorded_at <= previous.recorded_at:
        raise AssertionConflictError("new assertion versions must have a later recorded_at")


def _ensure_same_recording(existing: Assertion, draft: AssertionDraft) -> None:
    comparable = (
        existing.domain,
        existing.subject_type,
        existing.subject_id,
        existing.predicate,
        existing.value_json,
        existing.value_number,
        existing.value_text,
        existing.unit,
        existing.currency,
        existing.valid_from,
        existing.valid_until,
        existing.written_by,
        existing.source_id,
        existing.confidence,
    )
    candidate = (
        draft.domain,
        draft.subject_type,
        draft.subject_id,
        draft.predicate,
        draft.value_json,
        draft.value_number,
        draft.value_text,
        draft.unit,
        draft.currency,
        draft.valid_from,
        draft.valid_until,
        draft.written_by,
        draft.source_id,
        draft.confidence,
    )
    if comparable != candidate:
        raise AssertionConflictError(
            "the assertion key and recorded_at already identify a different fact"
        )


def _matches_lookup(assertion: Assertion, lookup: TemporalLookup) -> bool:
    return (
        assertion.assertion_key == lookup.assertion_key
        and assertion.domain == lookup.domain
        and assertion.subject_type == lookup.subject_type
        and assertion.subject_id == lookup.subject_id
        and assertion.predicate == lookup.predicate
    )


def _latest(assertions: list[Assertion], key: Any) -> Assertion:
    if not assertions:
        raise AssertionNotFoundError("no assertion matches the temporal lookup")
    return max(assertions, key=key)


def _required_row(row: Mapping[str, Any] | None, lookup: TemporalLookup) -> Assertion:
    if row is None:
        raise AssertionNotFoundError(
            f"no assertion found for {lookup.domain}/{lookup.subject_id}/{lookup.predicate}"
        )
    return _from_row(row)


def _from_row(row: Mapping[str, Any]) -> Assertion:
    value_json = row["value_json"]
    if isinstance(value_json, str):
        value_json = json.loads(value_json)
    value_number = row["value_number"]
    if value_number is not None and not isinstance(value_number, Decimal):
        value_number = Decimal(str(value_number))
    return Assertion(
        id=UUID(str(row["id"])),
        lineage_id=UUID(str(row["lineage_id"])),
        version_number=int(row["version_number"]),
        assertion_key=str(row["assertion_key"]),
        domain=str(row["domain"]),
        subject_type=str(row["subject_type"]),
        subject_id=str(row["subject_id"]),
        predicate=str(row["predicate"]),
        value_json=value_json,
        value_number=value_number,
        value_text=row["value_text"],
        unit=row["unit"],
        currency=row["currency"],
        valid_from=row["valid_from"],
        valid_until=row["valid_until"],
        recorded_at=row["recorded_at"],
        superseded_at=row["superseded_at"],
        superseded_by=(UUID(str(row["superseded_by"])) if row["superseded_by"] else None),
        written_by=str(row["written_by"]),
        source_id=UUID(str(row["source_id"])) if row["source_id"] else None,
        confidence=float(row["confidence"]),
    )
