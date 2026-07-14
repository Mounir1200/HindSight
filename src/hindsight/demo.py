from hindsight.adapters.telecom.billing import TelecomAdapter
from hindsight.adapters.telecom.remediation import (
    RemediationReceipt,
    TelecomRemediationRepository,
)
from hindsight.adapters.telecom.seed import (
    DEMO_CALL_ID,
    DEMO_DECISION_ID,
    DEMO_DECISION_TIME,
    DEMO_EVENT_TIME,
    DEMO_INVESTIGATION_TIME,
    DEMO_TARIFF_KEY,
    demo_call,
    demo_case_seed,
    demo_remediation_plan,
    seed_demo,
)
from hindsight.core.assertions.repository import AssertionRepository
from hindsight.core.assertions.service import TemporalAssertionService
from hindsight.core.decisions.repository import DecisionRepository
from hindsight.core.decisions.service import DecisionAuditService, DecisionJournalService


def run_demo_workflow(
    assertion_repository: AssertionRepository,
    decision_repository: DecisionRepository,
    remediation_repository: TelecomRemediationRepository,
    backend: str,
) -> dict[str, object]:
    assertions = TemporalAssertionService(assertion_repository)
    seed_demo(assertions)
    call = demo_call()
    audit = DecisionAuditService(assertions, TelecomAdapter()).audit(
        event=call,
        subject_id=DEMO_TARIFF_KEY,
        event_time=DEMO_EVENT_TIME,
        decision_time=DEMO_DECISION_TIME,
    )
    journal = DecisionJournalService(decision_repository).record(
        audit,
        decision_id=DEMO_DECISION_ID,
        agent_id="billing_agent",
        action="calculate_call_charge",
        subject_type="telecom_call",
        subject_id=DEMO_CALL_ID,
        investigated_at=DEMO_INVESTIGATION_TIME,
        input={
            "call_id": call.id,
            "tariff_key": call.tariff_key,
            "started_at": call.started_at,
            "duration_seconds": call.duration_seconds,
        },
        rationale="Selected the latest tariff known at decision time.",
    )

    case = demo_case_seed(audit, journal)
    remediation_repository.seed_case(case)
    plan = demo_remediation_plan(audit, journal, case)
    first_attempt = remediation_repository.apply_remediation(plan)
    second_attempt = remediation_repository.apply_remediation(plan)
    final_state = remediation_repository.snapshot(plan.dispute_id, plan.memory_key)

    return {
        "scenario": "retroactive_telecom_rate",
        "backend": backend,
        "current_truth": {
            "assertion_id": audit.snapshot.current_truth.id,
            "rate": audit.snapshot.current_truth.value_number,
            "valid_from": audit.snapshot.current_truth.valid_from,
            "recorded_at": audit.snapshot.current_truth.recorded_at,
        },
        "known_at_decision": {
            "assertion_id": audit.snapshot.known_at_decision.id,
            "rate": audit.snapshot.known_at_decision.value_number,
            "decision_time": audit.lookup.decision_time,
        },
        "decision": {
            "id": journal.record.id,
            "selected_assertion_id": audit.decision.selected_assertion_id,
            "event_time": journal.record.event_time,
            "decided_at": journal.record.decided_at,
            "selected_rate": audit.decision.selected_value,
            **audit.decision.output,
        },
        "evidence": [
            {
                "evidence_type": item.evidence_type,
                "assertion_id": item.assertion_id,
                "available_to_agent": item.available_to_agent,
                "retrieved": item.retrieved_at is not None,
                "retrieved_at": item.retrieved_at,
                "retrieval_method": item.retrieval_method,
                "was_presented_to_model": item.was_presented_to_model,
                "was_used_for_decision": item.was_used_for_decision,
                "exclusion_reason": item.exclusion_reason,
            }
            for item in journal.evidence
        ],
        "comparison": audit.comparison.details,
        "verdict": {
            "category": audit.verdict.verdict,
            "agent_fault": audit.verdict.agent_fault,
            "knowledge_gap_seconds": audit.verdict.knowledge_gap_seconds,
            "root_cause": audit.verdict.root_cause,
            "current_truth_assertion_id": audit.verdict.current_truth_assertion_id,
            "known_assertion_id": audit.verdict.known_assertion_id,
            "selected_assertion_id": audit.verdict.selected_assertion_id,
        },
        "remediation": {
            "case_id": plan.dispute_id,
            "attempts": [
                _attempt_payload(first_attempt),
                _attempt_payload(second_attempt),
            ],
            "effects": {
                "refund_id": first_attempt.refund_id,
                "incident_id": first_attempt.incident_id,
                "procedural_memory_id": first_attempt.memory_id,
            },
            "final_state": {
                "invoice_amount": final_state.invoice_amount,
                "invoice_status": final_state.invoice_status,
                "selected_assertion_id": final_state.selected_assertion_id,
                "refund_amount": final_state.refund_amount,
                "refund_count": final_state.refund_count,
                "dispute_status": final_state.dispute_status,
                "incident_count": final_state.incident_count,
                "procedural_memory_count": final_state.procedural_memory_count,
                "remediation_run_count": final_state.remediation_run_count,
            },
        },
    }


def _attempt_payload(receipt: RemediationReceipt) -> dict[str, object]:
    return {
        "status": receipt.outcome,
        "run_id": receipt.run_id,
        "safe_noop": receipt.safe_noop,
    }
