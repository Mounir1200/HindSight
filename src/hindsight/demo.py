from typing import Protocol, cast
from uuid import UUID

from hindsight.adapters.telecom.billing import TelecomAdapter
from hindsight.adapters.telecom.investigation import (
    InvestigationGuidance,
    build_investigation_guidance,
)
from hindsight.adapters.telecom.models import CallEvent
from hindsight.adapters.telecom.remediation import (
    RemediationReceipt,
    TelecomRemediationRepository,
)
from hindsight.adapters.telecom.seed import (
    DEMO_DISPUTE_CLAIM,
    DEMO_ROUTE,
    DEMO_SERVICE_TYPE,
    DEMO_TARIFF_KEY,
    FOLLOW_UP_DEMO_CASE,
    PRIMARY_DEMO_CASE,
    DemoCase,
    demo_call,
    demo_case_seed,
    demo_remediation_plan,
    seed_demo,
)
from hindsight.core.assertions.repository import AssertionRepository
from hindsight.core.assertions.service import TemporalAssertionService
from hindsight.core.decisions.models import DecisionAudit, DecisionJournalEntry
from hindsight.core.decisions.repository import DecisionRepository
from hindsight.core.decisions.service import DecisionAuditService, DecisionJournalService
from hindsight.core.memory import ProceduralMemoryReader


class DemoRepository(TelecomRemediationRepository, ProceduralMemoryReader, Protocol):
    pass


def run_demo_workflow(
    assertion_repository: AssertionRepository,
    decision_repository: DecisionRepository,
    remediation_repository: DemoRepository,
    backend: str,
    *,
    include_investigation_context: bool = False,
) -> dict[str, object]:
    assertions = TemporalAssertionService(assertion_repository)
    seed_demo(assertions)
    audit_service = DecisionAuditService(assertions, TelecomAdapter())
    journal_service = DecisionJournalService(decision_repository)
    audit = _audit_case(audit_service, PRIMARY_DEMO_CASE)
    journal = _record_case(journal_service, audit, PRIMARY_DEMO_CASE)
    case = demo_case_seed(audit, journal, PRIMARY_DEMO_CASE)
    remediation_repository.seed_case(case)

    plan = demo_remediation_plan(audit, journal, case)
    first_attempt = remediation_repository.apply_remediation(plan)
    second_attempt = remediation_repository.apply_remediation(plan)
    final_state = remediation_repository.snapshot(plan.dispute_id, plan.memory_key)

    before_memory = build_investigation_guidance(
        remediation_repository,
        dispute_id=case.dispute_id,
        route=case.route,
        service_type=case.service_type,
        symptom=case.claim,
        as_of=PRIMARY_DEMO_CASE.investigation_time,
        exclude_current_case=False,
    )
    after_memory = build_investigation_guidance(
        remediation_repository,
        dispute_id=FOLLOW_UP_DEMO_CASE.dispute_id,
        route=DEMO_ROUTE,
        service_type=DEMO_SERVICE_TYPE,
        symptom=DEMO_DISPUTE_CLAIM,
        as_of=FOLLOW_UP_DEMO_CASE.investigation_time,
    )
    follow_up_audit = _audit_case(audit_service, FOLLOW_UP_DEMO_CASE)
    follow_up_journal = _record_case(
        journal_service,
        follow_up_audit,
        FOLLOW_UP_DEMO_CASE,
    )
    follow_up_case = demo_case_seed(
        follow_up_audit,
        follow_up_journal,
        FOLLOW_UP_DEMO_CASE,
    )
    remediation_repository.seed_case(follow_up_case)

    payload = {
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
        "learning_proof": {
            "second_case": {
                "call_id": FOLLOW_UP_DEMO_CASE.call_id,
                "dispute_id": follow_up_case.dispute_id,
                "opened_at": follow_up_case.opened_at,
                "decision_id": follow_up_journal.record.id,
                "verdict": follow_up_audit.verdict.verdict,
                **follow_up_audit.comparison.details,
            },
            "before_memory": _guidance_payload(before_memory),
            "after_memory": _guidance_payload(after_memory),
            "measured_change": {
                "checklist_items_loaded": (
                    after_memory.procedure_steps_reused
                    - before_memory.procedure_steps_reused
                ),
                "recommendation_changed": (
                    before_memory.recommendation != after_memory.recommendation
                ),
                "procedural_memory_reuse": {
                    "before": {
                        "reused_cases": int(before_memory.memory_reused),
                        "eligible_cases": 1,
                    },
                    "after": {
                        "reused_cases": int(after_memory.memory_reused),
                        "eligible_cases": 1,
                    },
                },
                "suggested_root_cause_confirmed": (
                    after_memory.root_cause == follow_up_audit.verdict.root_cause
                ),
                "second_case_opened_after_memory": (
                    after_memory.memory_recorded_at is not None
                    and follow_up_case.opened_at > after_memory.memory_recorded_at
                ),
            },
            "advisory_boundary": {
                "memory_used_for_verdict": False,
                "memory_used_for_financial_calculation": False,
                "verdict_source": "deterministic_temporal_engine",
                "financial_source": "telecom_adapter",
            },
        },
    }
    if include_investigation_context:
        learning_proof = cast(dict[str, object], payload["learning_proof"])
        learning_proof["investigation_context"] = _investigation_context(
            follow_up_audit,
            follow_up_journal,
            follow_up_case.dispute_id,
            after_memory,
        )
    return payload


def _audit_case(
    service: DecisionAuditService[CallEvent],
    case: DemoCase,
) -> DecisionAudit:
    return service.audit(
        event=demo_call(case),
        subject_id=DEMO_TARIFF_KEY,
        event_time=case.event_time,
        decision_time=case.decision_time,
    )


def _record_case(
    service: DecisionJournalService,
    audit: DecisionAudit,
    case: DemoCase,
) -> DecisionJournalEntry:
    call = demo_call(case)
    return service.record(
        audit,
        decision_id=case.decision_id,
        agent_id="billing_agent",
        action="calculate_call_charge",
        subject_type="telecom_call",
        subject_id=case.call_id,
        investigated_at=case.investigation_time,
        input={
            "call_id": call.id,
            "tariff_key": call.tariff_key,
            "started_at": call.started_at,
            "duration_seconds": call.duration_seconds,
        },
        rationale="Selected the latest tariff known at decision time.",
    )


def _guidance_payload(guidance: InvestigationGuidance) -> dict[str, object]:
    return {
        "case_id": guidance.case_id,
        "memory_reused": guidance.memory_reused,
        "root_cause": guidance.root_cause,
        "recommendation": guidance.recommendation,
        "checklist": guidance.checklist,
        "procedure_steps_reused": guidance.procedure_steps_reused,
        "memory_id": guidance.memory_id,
        "source_dispute_id": guidance.source_dispute_id,
        "remediation_run_id": guidance.remediation_run_id,
        "retrieval_method": guidance.retrieval_method,
        "retrieval_rank": guidance.retrieval_rank,
        "applicable_at": guidance.applicable_at,
        "known_at": guidance.known_at,
        "memory_recorded_at": guidance.memory_recorded_at,
    }


def _investigation_context(
    audit: DecisionAudit,
    journal: DecisionJournalEntry,
    dispute_id: UUID,
    guidance: InvestigationGuidance,
) -> dict[str, object]:
    return {
        "case_id": dispute_id,
        "decision": {
            "id": journal.record.id,
            "event_time": audit.lookup.event_time,
            "decided_at": audit.lookup.decision_time,
            "selected_assertion_id": audit.decision.selected_assertion_id,
        },
        "current_truth": {
            "assertion_id": audit.snapshot.current_truth.id,
            "rate": audit.snapshot.current_truth.value_number,
            "valid_from": audit.snapshot.current_truth.valid_from,
            "recorded_at": audit.snapshot.current_truth.recorded_at,
        },
        "known_at_decision": {
            "assertion_id": audit.snapshot.known_at_decision.id,
            "rate": audit.snapshot.known_at_decision.value_number,
        },
        "evidence": [
            {
                "evidence_type": item.evidence_type,
                "assertion_id": item.assertion_id,
                "available_to_agent": item.available_to_agent,
                "retrieved": item.retrieved_at is not None,
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
        },
        "procedural_guidance": _guidance_payload(guidance),
        "authority": {
            "verdict": "deterministic_temporal_engine",
            "financials": "telecom_adapter",
            "procedural_guidance": "advisory_memory",
        },
    }


def _attempt_payload(receipt: RemediationReceipt) -> dict[str, object]:
    return {
        "status": receipt.outcome,
        "run_id": receipt.run_id,
        "safe_noop": receipt.safe_noop,
    }
