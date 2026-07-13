from datetime import datetime

from hindsight.adapters.base import DomainAdapter
from hindsight.core.assertions.service import TemporalAssertionService
from hindsight.core.decisions.models import DecisionAudit
from hindsight.core.verdicts.engine import Verdict, VerdictContext, VerdictResult, classify_decision


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

        correct_evidence_existed = snapshot.current_truth.recorded_at <= decision_time
        selected_current_truth = decision.selected_assertion_id == snapshot.current_truth.id
        verdict = classify_decision(
            VerdictContext(
                selected_value=decision.selected_value,
                current_truth_value=comparison.current_truth_value,
                correct_evidence_existed_at_decision=correct_evidence_existed,
                correct_evidence_was_accessible_to_agent=correct_evidence_existed,
                correct_evidence_was_retrieved=selected_current_truth,
                correct_evidence_was_presented=selected_current_truth,
                correct_evidence_was_used=selected_current_truth,
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
        return DecisionAudit(lookup, snapshot, decision, comparison, result)


def _agent_fault(verdict: Verdict) -> bool | None:
    if verdict is Verdict.INSUFFICIENT_EVIDENCE:
        return None
    return verdict not in {Verdict.CORRECT, Verdict.WRONG_NOT_KNOWABLE}
