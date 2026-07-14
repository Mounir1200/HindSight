from datetime import datetime
from decimal import Decimal
from uuid import UUID

from hindsight.core.decisions.models import DecisionEvidence
from hindsight.core.verdicts.engine import VerdictContext


def build_verdict_context(
    selected_value: Decimal,
    current_truth_value: Decimal,
    current_truth_assertion_id: UUID,
    current_truth_recorded_at: datetime,
    decision_time: datetime,
    evidence: tuple[DecisionEvidence, ...],
) -> VerdictContext:
    truth_traces = tuple(
        item for item in evidence if item.assertion_id == current_truth_assertion_id
    )
    return VerdictContext(
        selected_value=selected_value,
        current_truth_value=current_truth_value,
        correct_evidence_existed_at_decision=current_truth_recorded_at <= decision_time,
        correct_evidence_was_accessible_to_agent=(
            any(item.available_to_agent for item in truth_traces) if truth_traces else None
        ),
        correct_evidence_was_retrieved=(
            any(item.retrieved_at is not None for item in truth_traces) if truth_traces else None
        ),
        correct_evidence_was_presented=(
            any(item.was_presented_to_model for item in truth_traces) if truth_traces else None
        ),
        correct_evidence_was_used=(
            any(item.was_used_for_decision for item in truth_traces) if truth_traces else None
        ),
    )
