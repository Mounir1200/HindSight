from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from hindsight.adapters.telecom.models import CallEvent
from hindsight.agents._trace import AgentTrace, TraceIdentity
from hindsight.agents.advisory import AdvisoryClient, AdvisoryResult, resolve_advisory
from hindsight.core.agents.repository import AgentRunRepository
from hindsight.core.decisions.models import DecisionAudit, DecisionJournalEntry
from hindsight.core.decisions.service import DecisionAuditService, DecisionJournalService

_TOOL_USE_ID = "deterministic-billing-1"
_TOOL_NAME = "calculate_and_record_charge"


@dataclass(frozen=True, slots=True)
class BillingAgentResult:
    run_id: UUID
    audit: DecisionAudit
    journal: DecisionJournalEntry
    advisory: AdvisoryResult


class BillingAgent:
    def __init__(
        self,
        audit_service: DecisionAuditService[CallEvent],
        journal_service: DecisionJournalService,
        run_repository: AgentRunRepository,
        *,
        advisor: AdvisoryClient | None = None,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], UUID] | None = None,
    ) -> None:
        self._audit_service = audit_service
        self._journal_service = journal_service
        self._run_repository = run_repository
        self._advisor = advisor
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or uuid4

    def run(
        self,
        event: CallEvent,
        *,
        decision_id: UUID,
        decision_time: datetime,
        investigated_at: datetime,
        context: Mapping[str, object] | None = None,
        correlation_id: UUID | None = None,
    ) -> BillingAgentResult:
        run_id = self._id_factory()
        correlation_id = correlation_id or self._id_factory()
        trace = AgentTrace(
            self._run_repository,
            self._clock,
            run_id,
            correlation_id,
        )
        provider = self._advisor.provider if self._advisor else "deterministic"
        model_id = self._advisor.model_id if self._advisor else "telecom-adapter-v1"
        trace.start(
            TraceIdentity(
                agent_id="billing_agent",
                run_type="calculate_call_charge",
                subject_type="telecom_call",
                subject_id=event.id,
                provider=provider,
                model_id=model_id,
                prompt_version="billing-advisory-v1",
                toolset_version="temporal-billing-v1",
            ),
            {
                "call_id": event.id,
                "decision_id": str(decision_id),
                "tariff_key": event.tariff_key,
                "event_time": event.started_at.isoformat(),
                "decision_time": decision_time.isoformat(),
            },
        )
        arguments = {
            "call_id": event.id,
            "tariff_key": event.tariff_key,
            "duration_seconds": event.duration_seconds,
            "event_time": event.started_at.isoformat(),
            "decision_time": decision_time.isoformat(),
        }
        requested_at = trace.requested()
        try:
            audit = self._audit_service.audit(
                event=event,
                subject_id=event.tariff_key,
                event_time=event.started_at,
                decision_time=decision_time,
                context=dict(context or {}),
            )
            journal = self._journal_service.record(
                audit,
                decision_id=decision_id,
                agent_id="billing_agent",
                action="calculate_call_charge",
                subject_type="telecom_call",
                subject_id=event.id,
                investigated_at=investigated_at,
                input={
                    "call_id": event.id,
                    "tariff_key": event.tariff_key,
                    "started_at": event.started_at,
                    "duration_seconds": event.duration_seconds,
                },
                rationale="Selected the latest tariff known at decision time.",
            )
        except Exception as error:
            trace.tool_failed(_TOOL_USE_ID, _TOOL_NAME, requested_at, arguments, error)
            trace.fail(error)
            raise

        facts = _billing_facts(event, journal)
        trace.tool_succeeded(
            _TOOL_USE_ID,
            _TOOL_NAME,
            requested_at,
            arguments,
            facts,
        )
        advisory = resolve_advisory(
            self._advisor,
            purpose="billing_explanation",
            facts=facts,
            request_metadata={
                "run_id": str(run_id),
                "correlation_id": str(correlation_id),
            },
            fallback=(
                f"Charged {facts['currency']} {facts['amount']} for "
                f"{event.duration_seconds} seconds using the tariff known at decision time."
            ),
        )
        trace.complete(
            {
                **facts,
                "advisory_explanation": advisory.text,
                "advisory_status": advisory.status,
                "advisory_request_id": advisory.request_id,
                "advisory_error_code": advisory.error_code,
                "safety": {
                    "calculation_source": "telecom_adapter",
                    "evidence_source": "temporal_assertion_service",
                    "decision_write_source": "decision_journal_service",
                    "model_output_role": "advisory_explanation",
                    "model_mutations_performed": 0,
                },
            },
            advisory.usage,
        )
        return BillingAgentResult(run_id, audit, journal, advisory)


def _billing_facts(
    event: CallEvent,
    journal: DecisionJournalEntry,
) -> dict[str, object]:
    record = journal.record
    return {
        "decision_id": str(record.id),
        "call_id": event.id,
        "selected_assertion_id": str(record.selected_assertion_id),
        "selected_rate": str(record.output["selected_value"]),
        "amount": str(record.output["amount"]),
        "currency": str(record.output["currency"]),
        "duration_seconds": int(record.output["duration_seconds"]),
        "evidence_count": len(journal.evidence),
    }
