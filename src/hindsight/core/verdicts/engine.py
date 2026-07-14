from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from uuid import UUID


class Verdict(StrEnum):
    CORRECT = "correct"
    WRONG_NOT_KNOWABLE = "wrong_not_knowable"
    WRONG_KNOWABLE_NOT_RETRIEVED = "wrong_knowable_not_retrieved"
    WRONG_RETRIEVED_NOT_PRESENTED = "wrong_retrieved_not_presented"
    WRONG_PRESENTED_IGNORED = "wrong_presented_ignored"
    WRONG_DUE_TO_UNTRUSTED_SOURCE = "wrong_due_to_untrusted_source"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


@dataclass(frozen=True, slots=True)
class VerdictContext:
    selected_value: Decimal
    current_truth_value: Decimal
    correct_evidence_existed_at_decision: bool | None
    correct_evidence_was_accessible_to_agent: bool | None
    correct_evidence_was_retrieved: bool | None
    correct_evidence_was_presented: bool | None
    correct_evidence_was_used: bool | None
    lower_trust_source_overrode_higher_trust_source: bool | None = False


@dataclass(frozen=True, slots=True)
class VerdictResult:
    verdict: Verdict
    agent_fault: bool | None
    knowledge_gap_seconds: int
    root_cause: str | None
    current_truth_assertion_id: UUID
    known_assertion_id: UUID
    selected_assertion_id: UUID


def classify_decision(context: VerdictContext) -> Verdict:
    if context.selected_value == context.current_truth_value:
        return Verdict.CORRECT
    if context.correct_evidence_existed_at_decision is False:
        return Verdict.WRONG_NOT_KNOWABLE
    if context.correct_evidence_existed_at_decision is None:
        return Verdict.INSUFFICIENT_EVIDENCE
    if context.correct_evidence_was_accessible_to_agent is False:
        return Verdict.WRONG_NOT_KNOWABLE
    if context.correct_evidence_was_accessible_to_agent is None:
        return Verdict.INSUFFICIENT_EVIDENCE
    if context.correct_evidence_was_retrieved is False:
        return Verdict.WRONG_KNOWABLE_NOT_RETRIEVED
    if context.correct_evidence_was_retrieved is None:
        return Verdict.INSUFFICIENT_EVIDENCE
    if context.correct_evidence_was_presented is False:
        return Verdict.WRONG_RETRIEVED_NOT_PRESENTED
    if context.correct_evidence_was_presented is None:
        return Verdict.INSUFFICIENT_EVIDENCE
    if context.correct_evidence_was_used is False:
        return Verdict.WRONG_PRESENTED_IGNORED
    if context.correct_evidence_was_used is None:
        return Verdict.INSUFFICIENT_EVIDENCE
    if context.lower_trust_source_overrode_higher_trust_source is True:
        return Verdict.WRONG_DUE_TO_UNTRUSTED_SOURCE
    return Verdict.INSUFFICIENT_EVIDENCE
