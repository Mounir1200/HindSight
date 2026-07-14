import json
import time
from collections.abc import Mapping
from typing import Any, Protocol
from uuid import UUID, uuid5

from hindsight.core.decisions.models import (
    DecisionConflictError,
    DecisionEvidence,
    DecisionJournalEntry,
    DecisionNotFoundError,
    DecisionRecord,
)
from hindsight.core.verdicts.engine import Verdict, VerdictResult

SELECT_DECISION_SQL = "SELECT * FROM decisions WHERE id = %s"

SELECT_EVIDENCE_SQL = """
SELECT *
FROM decision_evidence
WHERE decision_id = %s
ORDER BY retrieval_rank NULLS LAST, evidence_type, assertion_id
"""

INSERT_DECISION_SQL = """
INSERT INTO decisions (
  id, domain, agent_id, action, subject_type, subject_id, event_time,
  decided_at, selected_assertion_id, current_truth_assertion_id,
  known_assertion_id, input, output, rationale, verdict, agent_fault,
  knowledge_gap_seconds, root_cause, investigated_at
)
VALUES (
  %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
  CAST(%s AS JSONB), CAST(%s AS JSONB), %s, %s, %s, %s, %s, %s
)
"""

INSERT_EVIDENCE_SQL = """
INSERT INTO decision_evidence (
  id, decision_id, evidence_type, assertion_id, available_to_agent,
  retrieval_started_at, retrieved_at, retrieval_method, retrieval_query,
  retrieval_rank, retrieval_score, was_presented_to_model,
  presentation_position, was_cited_in_rationale, was_used_for_decision,
  exclusion_reason
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""


class DecisionRepository(Protocol):
    def append(self, entry: DecisionJournalEntry) -> DecisionJournalEntry: ...

    def get(self, decision_id: UUID) -> DecisionJournalEntry: ...


class InMemoryDecisionRepository:
    def __init__(self) -> None:
        self._entries: dict[UUID, DecisionJournalEntry] = {}

    def append(self, entry: DecisionJournalEntry) -> DecisionJournalEntry:
        existing = self._entries.get(entry.record.id)
        if existing is not None:
            _ensure_same(existing, entry)
            return existing
        self._entries[entry.record.id] = entry
        return entry

    def get(self, decision_id: UUID) -> DecisionJournalEntry:
        try:
            return self._entries[decision_id]
        except KeyError as error:
            raise DecisionNotFoundError(f"decision {decision_id} was not found") from error


class CockroachDecisionRepository:
    def __init__(self, connection: Any, max_retries: int = 3) -> None:
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        self._connection = connection
        self._max_retries = max_retries

    def append(self, entry: DecisionJournalEntry) -> DecisionJournalEntry:
        for attempt in range(self._max_retries + 1):
            try:
                return self._append_once(entry)
            except DecisionConflictError:
                raise
            except Exception as error:
                if (
                    getattr(error, "sqlstate", None) not in {"40001", "23505"}
                    or attempt == self._max_retries
                ):
                    raise
                time.sleep(0.05 * 2**attempt)
        raise RuntimeError("unreachable retry state")

    def _append_once(self, entry: DecisionJournalEntry) -> DecisionJournalEntry:
        with self._connection.transaction():
            existing = _fetch_entry(self._connection, entry.record.id)
            if existing is not None:
                _ensure_same(existing, entry)
                return existing

            record = entry.record
            verdict = record.verdict
            self._connection.execute(
                INSERT_DECISION_SQL,
                (
                    record.id,
                    record.domain,
                    record.agent_id,
                    record.action,
                    record.subject_type,
                    record.subject_id,
                    record.event_time,
                    record.decided_at,
                    record.selected_assertion_id,
                    verdict.current_truth_assertion_id,
                    verdict.known_assertion_id,
                    json.dumps(record.input),
                    json.dumps(record.output),
                    record.rationale,
                    verdict.verdict.value,
                    verdict.agent_fault,
                    verdict.knowledge_gap_seconds,
                    verdict.root_cause,
                    record.investigated_at,
                ),
            )
            for evidence in entry.evidence:
                self._connection.execute(
                    INSERT_EVIDENCE_SQL,
                    _evidence_values(record.id, evidence),
                )
            return entry

    def get(self, decision_id: UUID) -> DecisionJournalEntry:
        entry = _fetch_entry(self._connection, decision_id)
        if entry is None:
            raise DecisionNotFoundError(f"decision {decision_id} was not found")
        return entry


def _fetch_entry(connection: Any, decision_id: UUID) -> DecisionJournalEntry | None:
    row = connection.execute(SELECT_DECISION_SQL, (decision_id,)).fetchone()
    if row is None:
        return None
    evidence_rows = connection.execute(SELECT_EVIDENCE_SQL, (decision_id,)).fetchall()
    return DecisionJournalEntry(
        record=_record_from_row(row),
        evidence=tuple(_evidence_from_row(item) for item in evidence_rows),
    )


def _record_from_row(row: Mapping[str, Any]) -> DecisionRecord:
    verdict = VerdictResult(
        verdict=Verdict(str(row["verdict"])),
        agent_fault=row["agent_fault"],
        knowledge_gap_seconds=int(row["knowledge_gap_seconds"]),
        root_cause=row["root_cause"],
        current_truth_assertion_id=UUID(str(row["current_truth_assertion_id"])),
        known_assertion_id=UUID(str(row["known_assertion_id"])),
        selected_assertion_id=UUID(str(row["selected_assertion_id"])),
    )
    return DecisionRecord(
        id=UUID(str(row["id"])),
        domain=str(row["domain"]),
        agent_id=str(row["agent_id"]),
        action=str(row["action"]),
        subject_type=str(row["subject_type"]),
        subject_id=str(row["subject_id"]),
        event_time=row["event_time"],
        decided_at=row["decided_at"],
        investigated_at=row["investigated_at"],
        selected_assertion_id=UUID(str(row["selected_assertion_id"])),
        input=_json_object(row["input"]),
        output=_json_object(row["output"]),
        rationale=row["rationale"],
        verdict=verdict,
    )


def _evidence_from_row(row: Mapping[str, Any]) -> DecisionEvidence:
    return DecisionEvidence(
        evidence_type=str(row["evidence_type"]),
        assertion_id=UUID(str(row["assertion_id"])),
        available_to_agent=bool(row["available_to_agent"]),
        retrieval_started_at=row["retrieval_started_at"],
        retrieved_at=row["retrieved_at"],
        retrieval_method=row["retrieval_method"],
        retrieval_query=row["retrieval_query"],
        retrieval_rank=row["retrieval_rank"],
        retrieval_score=row["retrieval_score"],
        was_presented_to_model=bool(row["was_presented_to_model"]),
        presentation_position=row["presentation_position"],
        was_cited_in_rationale=bool(row["was_cited_in_rationale"]),
        was_used_for_decision=bool(row["was_used_for_decision"]),
        exclusion_reason=row["exclusion_reason"],
    )


def _evidence_values(decision_id: UUID, evidence: DecisionEvidence) -> tuple[object, ...]:
    evidence_id = uuid5(
        decision_id,
        f"{evidence.evidence_type}:{evidence.assertion_id}",
    )
    return (
        evidence_id,
        decision_id,
        evidence.evidence_type,
        evidence.assertion_id,
        evidence.available_to_agent,
        evidence.retrieval_started_at,
        evidence.retrieved_at,
        evidence.retrieval_method,
        evidence.retrieval_query,
        evidence.retrieval_rank,
        evidence.retrieval_score,
        evidence.was_presented_to_model,
        evidence.presentation_position,
        evidence.was_cited_in_rationale,
        evidence.was_used_for_decision,
        evidence.exclusion_reason,
    )


def _json_object(value: object) -> dict[str, object]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("decision JSON must be an object")
    return value


def _ensure_same(existing: DecisionJournalEntry, candidate: DecisionJournalEntry) -> None:
    if existing != candidate:
        raise DecisionConflictError(
            f"decision {candidate.record.id} already identifies a different journal entry"
        )
