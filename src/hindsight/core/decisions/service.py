from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import Decimal
from enum import Enum
from uuid import UUID

from hindsight.adapters.base import DomainAdapter
from hindsight.core.assertions.models import Assertion, TemporalLookup
from hindsight.core.assertions.service import TemporalAssertionService
from hindsight.core.decisions.evidence import build_verdict_context
from hindsight.core.decisions.models import (
    DecisionAudit,
    DecisionEvidence,
    DecisionJournalEntry,
    DecisionRecord,
)
from hindsight.core.decisions.repository import DecisionRepository
from hindsight.core.verdicts.engine import Verdict, VerdictResult, classify_decision


class DecisionAuditService[EventT]:
    def __init__(
        self,
        assertions: TemporalAssertionService,
        adapter: DomainAdapter[EventT],
    ) -> None:
        self._assertions = assertions
        self._adapter = adapter

    def audit(
        self,
        event: EventT,
        subject_id: str,
        event_time: datetime,
        decision_time: datetime,
        context: dict[str, object] | None = None,
        evidence: Iterable[DecisionEvidence] | None = None,
    ) -> DecisionAudit:
        lookup = self._adapter.build_assertion_lookup(
            subject_id=subject_id,
            event_time=event_time,
            decision_time=decision_time,
            context=context or {},
        )
        snapshot = self._assertions.reconstruct(lookup)
        decision = self._adapter.calculate_decision(event, snapshot.known_at_decision)
        comparison = self._adapter.compare_outcome(decision, snapshot.current_truth)
        traces = (
            tuple(evidence)
            if evidence is not None
            else _default_evidence(snapshot.current_truth, snapshot.known_at_decision, lookup)
        )
        verdict = classify_decision(
            build_verdict_context(
                selected_value=decision.selected_value,
                current_truth_value=comparison.current_truth_value,
                current_truth_assertion_id=snapshot.current_truth.id,
                current_truth_recorded_at=snapshot.current_truth.recorded_at,
                decision_time=decision_time,
                evidence=traces,
            )
        )
        knowledge_gap = max(
            0,
            int(
                (
                    snapshot.current_truth.recorded_at - snapshot.current_truth.valid_from
                ).total_seconds()
            ),
        )
        result = VerdictResult(
            verdict=verdict,
            agent_fault=_agent_fault(verdict),
            knowledge_gap_seconds=knowledge_gap,
            root_cause=(
                self._adapter.late_information_root_cause
                if verdict is Verdict.WRONG_NOT_KNOWABLE
                and snapshot.current_truth.recorded_at > decision_time
                else None
            ),
            current_truth_assertion_id=snapshot.current_truth.id,
            known_assertion_id=snapshot.known_at_decision.id,
            selected_assertion_id=decision.selected_assertion_id,
        )
        return DecisionAudit(lookup, snapshot, decision, comparison, result, traces)


class DecisionJournalService:
    def __init__(self, repository: DecisionRepository) -> None:
        self._repository = repository

    def record(
        self,
        audit: DecisionAudit,
        *,
        decision_id: UUID,
        agent_id: str,
        action: str,
        subject_type: str,
        subject_id: str,
        investigated_at: datetime,
        input: Mapping[str, object],
        rationale: str | None = None,
    ) -> DecisionJournalEntry:
        record = DecisionRecord(
            id=decision_id,
            domain=audit.lookup.domain,
            agent_id=agent_id,
            action=action,
            subject_type=subject_type,
            subject_id=subject_id,
            event_time=audit.lookup.event_time,
            decided_at=audit.lookup.decision_time,
            investigated_at=investigated_at,
            selected_assertion_id=audit.decision.selected_assertion_id,
            input=_json_mapping(input),
            output=_json_mapping(
                {
                    "selected_value": audit.decision.selected_value,
                    **audit.decision.output,
                }
            ),
            rationale=rationale,
            verdict=audit.verdict,
        )
        return self._repository.append(DecisionJournalEntry(record, audit.evidence))


def _default_evidence(
    current_truth: Assertion,
    known_at_decision: Assertion,
    lookup: TemporalLookup,
) -> tuple[DecisionEvidence, ...]:
    selected = DecisionEvidence(
        evidence_type="decision_input",
        assertion_id=known_at_decision.id,
        available_to_agent=True,
        retrieval_started_at=lookup.decision_time,
        retrieved_at=lookup.decision_time,
        retrieval_method="temporal_sql",
        retrieval_query=lookup.assertion_key,
        retrieval_rank=1,
        was_used_for_decision=True,
    )
    if current_truth.id == known_at_decision.id:
        return (selected,)

    existed = current_truth.recorded_at <= lookup.decision_time
    counterfactual = DecisionEvidence(
        evidence_type="counterfactual_current_truth",
        assertion_id=current_truth.id,
        available_to_agent=existed,
        exclusion_reason="not_retrieved" if existed else "not_recorded_at_decision",
    )
    return selected, counterfactual


def _json_mapping(value: Mapping[str, object]) -> dict[str, object]:
    return {str(key): _json_value(item) for key, item in value.items()}


def _json_value(value: object) -> object:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return _json_mapping(value)
    if isinstance(value, tuple | list):
        return [_json_value(item) for item in value]
    raise TypeError(f"cannot store {type(value).__name__} as decision JSON")


def _agent_fault(verdict: Verdict) -> bool | None:
    if verdict is Verdict.INSUFFICIENT_EVIDENCE:
        return None
    return verdict not in {Verdict.CORRECT, Verdict.WRONG_NOT_KNOWABLE}
