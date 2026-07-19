from typing import Protocol, cast
from uuid import UUID, uuid4

from hindsight.adapters.telecom.billing import TelecomAdapter
from hindsight.adapters.telecom.investigation import (
    InvestigationGuidance,
    build_investigation_guidance,
)
from hindsight.adapters.telecom.remediation import (
    RemediationReceipt,
    TelecomRemediationRepository,
)
from hindsight.adapters.telecom.seed import (
    DEMO_DISPUTE_CLAIM,
    DEMO_INCIDENT_ID,
    DEMO_MEMORY_ID,
    DEMO_REFUND_ID,
    DEMO_REMEDIATION_END,
    DEMO_REMEDIATION_RUN_ID,
    DEMO_REMEDIATION_START,
    DEMO_ROUTE,
    DEMO_SERVICE_TYPE,
    FOLLOW_UP_DEMO_CASE,
    PRIMARY_DEMO_CASE,
    demo_call,
    demo_case_seed,
    seed_demo,
)
from hindsight.agents.advisory import AdvisoryClient
from hindsight.agents.billing import BillingAgent
from hindsight.agents.remediation import RemediationAgent
from hindsight.core.agents.repository import AgentRunRepository, InMemoryAgentRunRepository
from hindsight.core.assertions.repository import AssertionRepository
from hindsight.core.assertions.service import TemporalAssertionService
from hindsight.core.decisions.models import DecisionAudit, DecisionJournalEntry
from hindsight.core.decisions.repository import DecisionRepository
from hindsight.core.decisions.service import DecisionAuditService, DecisionJournalService
from hindsight.core.memory import ProceduralMemoryIndexer, ProceduralMemoryReader
from hindsight.infrastructure.vector_memory import MEMORY_VECTOR_INDEX_NAME


class DemoRepository(TelecomRemediationRepository, ProceduralMemoryReader, Protocol):
    pass


class DemoVectorMemory(ProceduralMemoryReader, ProceduralMemoryIndexer, Protocol):
    pass


def run_demo_workflow(
    assertion_repository: AssertionRepository,
    decision_repository: DecisionRepository,
    remediation_repository: DemoRepository,
    backend: str,
    *,
    vector_memory: DemoVectorMemory | None = None,
    include_investigation_context: bool = False,
    agent_run_repository: AgentRunRepository | None = None,
    advisory_client: AdvisoryClient | None = None,
    correlation_id: UUID | None = None,
) -> dict[str, object]:
    assertions = TemporalAssertionService(assertion_repository)
    seed_demo(assertions)
    audit_service = DecisionAuditService(assertions, TelecomAdapter())
    journal_service = DecisionJournalService(decision_repository)
    run_repository = agent_run_repository or InMemoryAgentRunRepository()
    correlation_id = correlation_id or uuid4()
    billing = BillingAgent(
        audit_service,
        journal_service,
        run_repository,
        advisor=advisory_client,
    ).run(
        demo_call(PRIMARY_DEMO_CASE),
        decision_id=PRIMARY_DEMO_CASE.decision_id,
        decision_time=PRIMARY_DEMO_CASE.decision_time,
        investigated_at=PRIMARY_DEMO_CASE.investigation_time,
        correlation_id=correlation_id,
    )
    audit = billing.audit
    journal = billing.journal
    case = demo_case_seed(audit, journal, PRIMARY_DEMO_CASE)
    remediation_repository.seed_case(case)

    remediation_agent = RemediationAgent(
        remediation_repository,
        run_repository,
        advisor=advisory_client,
    )
    remediation_arguments = {
        "remediation_run_id": DEMO_REMEDIATION_RUN_ID,
        "refund_id": DEMO_REFUND_ID,
        "incident_id": DEMO_INCIDENT_ID,
        "memory_id": DEMO_MEMORY_ID,
        "started_at": DEMO_REMEDIATION_START,
        "completed_at": DEMO_REMEDIATION_END,
    }
    first_remediation = remediation_agent.run(
        audit,
        journal,
        case,
        correlation_id=correlation_id,
        **remediation_arguments,
    )
    replayed_remediation = remediation_agent.run(
        audit,
        journal,
        case,
        correlation_id=correlation_id,
        **remediation_arguments,
    )
    plan = first_remediation.plan
    first_attempt = first_remediation.receipt
    second_attempt = replayed_remediation.receipt
    final_state = remediation_repository.snapshot(plan.dispute_id, plan.memory_key)
    embedding_receipt = (
        vector_memory.index(first_attempt.memory_id) if vector_memory is not None else None
    )
    memory_reader = vector_memory or remediation_repository

    before_memory = build_investigation_guidance(
        memory_reader,
        dispute_id=case.dispute_id,
        route=case.route,
        service_type=case.service_type,
        symptom=case.claim,
        as_of=PRIMARY_DEMO_CASE.investigation_time,
        exclude_current_case=False,
    )
    after_memory = build_investigation_guidance(
        memory_reader,
        dispute_id=FOLLOW_UP_DEMO_CASE.dispute_id,
        route=DEMO_ROUTE,
        service_type=DEMO_SERVICE_TYPE,
        symptom=DEMO_DISPUTE_CLAIM,
        as_of=FOLLOW_UP_DEMO_CASE.investigation_time,
    )
    follow_up_billing = BillingAgent(
        audit_service,
        journal_service,
        run_repository,
    ).run(
        demo_call(FOLLOW_UP_DEMO_CASE),
        decision_id=FOLLOW_UP_DEMO_CASE.decision_id,
        decision_time=FOLLOW_UP_DEMO_CASE.decision_time,
        investigated_at=FOLLOW_UP_DEMO_CASE.investigation_time,
        correlation_id=correlation_id,
    )
    follow_up_audit = follow_up_billing.audit
    follow_up_journal = follow_up_billing.journal
    follow_up_case = demo_case_seed(
        follow_up_audit,
        follow_up_journal,
        FOLLOW_UP_DEMO_CASE,
    )
    remediation_repository.seed_case(follow_up_case)

    payload = {
        "scenario": "retroactive_telecom_rate",
        "backend": backend,
        "agent_execution": {
            "correlation_id": correlation_id,
            "billing_run_id": billing.run_id,
            "billing_advisory_status": billing.advisory.status,
            "remediation_run_ids": [
                first_remediation.run_id,
                replayed_remediation.run_id,
            ],
            "remediation_advisory_status": first_remediation.advisory.status,
            "replay_advisory_status": replayed_remediation.advisory.status,
            "follow_up_billing_run_id": follow_up_billing.run_id,
        },
        "current_truth": {
            "assertion_id": audit.snapshot.current_truth.id,
            "rate": audit.snapshot.current_truth.value_number,
            "version_number": audit.snapshot.current_truth.version_number,
            "valid_from": audit.snapshot.current_truth.valid_from,
            "valid_until": audit.snapshot.current_truth.valid_until,
            "recorded_at": audit.snapshot.current_truth.recorded_at,
            "written_by": audit.snapshot.current_truth.written_by,
            "source_id": audit.snapshot.current_truth.source_id,
        },
        "known_at_decision": {
            "assertion_id": audit.snapshot.known_at_decision.id,
            "rate": audit.snapshot.known_at_decision.value_number,
            "version_number": audit.snapshot.known_at_decision.version_number,
            "valid_from": audit.snapshot.known_at_decision.valid_from,
            "valid_until": audit.snapshot.known_at_decision.valid_until,
            "recorded_at": audit.snapshot.known_at_decision.recorded_at,
            "written_by": audit.snapshot.known_at_decision.written_by,
            "source_id": audit.snapshot.known_at_decision.source_id,
            "decision_time": audit.lookup.decision_time,
        },
        "decision": {
            "id": journal.record.id,
            "agent_id": journal.record.agent_id,
            "action": journal.record.action,
            "subject_type": journal.record.subject_type,
            "subject_id": journal.record.subject_id,
            "selected_assertion_id": audit.decision.selected_assertion_id,
            "event_time": journal.record.event_time,
            "decided_at": journal.record.decided_at,
            "investigated_at": journal.record.investigated_at,
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
                "retrieval_rank": item.retrieval_rank,
                "retrieval_score": item.retrieval_score,
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
            "vector_memory": (
                {
                    "enabled": True,
                    "tool": "cockroachdb_distributed_vector_index",
                    "index_name": MEMORY_VECTOR_INDEX_NAME,
                    "memory_id": embedding_receipt.memory_id,
                    "status": embedding_receipt.status,
                    "model_id": embedding_receipt.model_id,
                    "dimensions": embedding_receipt.dimensions,
                    "input_tokens": embedding_receipt.input_tokens,
                    "embedded_at": embedding_receipt.embedded_at,
                }
                if embedding_receipt is not None
                else {"enabled": False}
            ),
            "second_case": {
                "call_id": FOLLOW_UP_DEMO_CASE.call_id,
                "dispute_id": follow_up_case.dispute_id,
                "opened_at": follow_up_case.opened_at,
                "decision_id": follow_up_journal.record.id,
                "audited_at": follow_up_journal.record.investigated_at,
                "verdict": follow_up_audit.verdict.verdict,
                "agent_fault": follow_up_audit.verdict.agent_fault,
                "knowledge_gap_seconds": (follow_up_audit.verdict.knowledge_gap_seconds),
                "root_cause": follow_up_audit.verdict.root_cause,
                **follow_up_audit.comparison.details,
            },
            "before_memory": _guidance_payload(before_memory),
            "after_memory": _guidance_payload(after_memory),
            "measured_change": {
                "checklist_items_loaded": (
                    after_memory.procedure_steps_reused - before_memory.procedure_steps_reused
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
        "retrieval_score": guidance.retrieval_score,
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
    current_truth = audit.snapshot.current_truth
    return {
        "case_id": dispute_id,
        "decision": {
            "id": journal.record.id,
            "event_occurred_at": audit.lookup.event_time,
            "decision_made_at": audit.lookup.decision_time,
            "selected_assertion_id": audit.decision.selected_assertion_id,
        },
        "current_truth": {
            "assertion_id": current_truth.id,
            "rate": current_truth.value_number,
            "valid_from": current_truth.valid_from,
            "recorded_at": current_truth.recorded_at,
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
            "knowledge_gap_definition": (
                "current_truth.recorded_at_minus_current_truth.valid_from"
            ),
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
