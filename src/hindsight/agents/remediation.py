from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from hindsight.adapters.telecom.remediation import (
    RemediationReceipt,
    TelecomCaseSeed,
    TelecomRemediationPlan,
    TelecomRemediationRepository,
    build_remediation_plan,
    serialize_remediation_result,
)
from hindsight.agents._trace import AgentTrace, TraceIdentity
from hindsight.agents.advisory import AdvisoryClient, AdvisoryResult, resolve_advisory
from hindsight.core.agents.repository import AgentRunRepository
from hindsight.core.decisions.models import DecisionAudit, DecisionJournalEntry

_TOOL_USE_ID = "deterministic-remediation-1"
_TOOL_NAME = "apply_idempotent_remediation"


@dataclass(frozen=True, slots=True)
class RemediationAgentResult:
    run_id: UUID
    plan: TelecomRemediationPlan
    receipt: RemediationReceipt
    advisory: AdvisoryResult


class RemediationAgent:
    def __init__(
        self,
        repository: TelecomRemediationRepository,
        run_repository: AgentRunRepository,
        *,
        advisor: AdvisoryClient | None = None,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], UUID] | None = None,
    ) -> None:
        self._repository = repository
        self._run_repository = run_repository
        self._advisor = advisor
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or uuid4

    def run(
        self,
        audit: DecisionAudit,
        journal: DecisionJournalEntry,
        case: TelecomCaseSeed,
        *,
        remediation_run_id: UUID,
        refund_id: UUID,
        incident_id: UUID,
        memory_id: UUID,
        started_at: datetime,
        completed_at: datetime,
        correlation_id: UUID | None = None,
    ) -> RemediationAgentResult:
        run_id = self._id_factory()
        correlation_id = correlation_id or self._id_factory()
        trace = AgentTrace(
            self._run_repository,
            self._clock,
            run_id,
            correlation_id,
        )
        provider = self._advisor.provider if self._advisor else "deterministic"
        model_id = self._advisor.model_id if self._advisor else "telecom-remediation-v1"
        trace.start(
            TraceIdentity(
                agent_id="remediation_agent",
                run_type="remediate_billing_dispute",
                subject_type="telecom_dispute",
                subject_id=str(case.dispute_id),
                provider=provider,
                model_id=model_id,
                prompt_version="remediation-advisory-v1",
                toolset_version="telecom-remediation-v1",
            ),
            {
                "dispute_id": str(case.dispute_id),
                "decision_id": str(journal.record.id),
                "remediation_run_id": str(remediation_run_id),
            },
        )
        arguments = {
            "dispute_id": str(case.dispute_id),
            "decision_id": str(journal.record.id),
            "remediation_run_id": str(remediation_run_id),
            "refund_id": str(refund_id),
            "incident_id": str(incident_id),
            "memory_id": str(memory_id),
        }
        requested_at = trace.requested()
        try:
            plan = build_remediation_plan(
                audit,
                journal,
                case,
                run_id=remediation_run_id,
                dispute_id=case.dispute_id,
                refund_id=refund_id,
                incident_id=incident_id,
                memory_id=memory_id,
                started_at=started_at,
                completed_at=completed_at,
                executed_by="remediation_agent",
            )
            receipt = self._repository.apply_remediation(plan)
        except Exception as error:
            trace.tool_failed(_TOOL_USE_ID, _TOOL_NAME, requested_at, arguments, error)
            trace.fail(error)
            raise

        facts = {
            **serialize_remediation_result(receipt),
            "outcome": receipt.outcome.value,
            "safe_noop": receipt.safe_noop,
            "root_cause": plan.root_cause,
        }
        trace.tool_succeeded(
            _TOOL_USE_ID,
            _TOOL_NAME,
            requested_at,
            arguments,
            facts,
        )
        advisory = resolve_advisory(
            None if receipt.safe_noop else self._advisor,
            purpose="remediation_summary",
            facts=facts,
            request_metadata={
                "run_id": str(run_id),
                "correlation_id": str(correlation_id),
            },
            fallback=(
                f"Remediation {receipt.outcome.value}: invoice corrected to "
                f"{receipt.currency} {receipt.corrected_amount} with a "
                f"{receipt.currency} {receipt.refund_amount} refund."
            ),
        )
        trace.complete(
            {
                **facts,
                "advisory_summary": advisory.text,
                "advisory_status": advisory.status,
                "advisory_request_id": advisory.request_id,
                "advisory_error_code": advisory.error_code,
                "safety": {
                    "plan_source": "deterministic_remediation_builder",
                    "mutation_source": "telecom_remediation_repository",
                    "transaction_policy": "serializable_idempotent",
                    "model_output_role": "advisory_summary",
                    "model_mutations_performed": 0,
                },
            },
            advisory.usage,
        )
        return RemediationAgentResult(run_id, plan, receipt, advisory)
