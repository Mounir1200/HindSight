from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest

from hindsight.adapters.telecom.billing import TelecomAdapter
from hindsight.adapters.telecom.remediation import InMemoryTelecomRemediationRepository
from hindsight.adapters.telecom.seed import (
    DEMO_DECISION_ID,
    DEMO_DECISION_TIME,
    DEMO_INCIDENT_ID,
    DEMO_INVESTIGATION_TIME,
    DEMO_MEMORY_ID,
    DEMO_REFUND_ID,
    DEMO_REMEDIATION_END,
    DEMO_REMEDIATION_RUN_ID,
    DEMO_REMEDIATION_START,
    PRIMARY_DEMO_CASE,
    demo_call,
    demo_case_seed,
    seed_demo,
)
from hindsight.agents.advisory import AdvisoryResponse
from hindsight.agents.billing import BillingAgent
from hindsight.agents.remediation import RemediationAgent
from hindsight.core.agents.models import AgentRunStatus, ToolCallStatus
from hindsight.core.agents.repository import InMemoryAgentRunRepository
from hindsight.core.assertions.repository import InMemoryAssertionRepository
from hindsight.core.assertions.service import TemporalAssertionService
from hindsight.core.decisions.repository import InMemoryDecisionRepository
from hindsight.core.decisions.service import DecisionAuditService, DecisionJournalService
from hindsight.demo import run_demo_workflow


class SequenceClock:
    def __init__(self) -> None:
        self._value = datetime(2026, 7, 3, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        value = self._value
        self._value += timedelta(milliseconds=1)
        return value


class RecordingAdvisor:
    provider = "amazon_bedrock"
    model_id = "test-model"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.requests: list[dict[str, Any]] = []

    def generate(self, **request: Any) -> AdvisoryResponse:
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("provider unavailable: raw-secret-token")
        return AdvisoryResponse(
            "Advisory: the persisted deterministic result is ready for review.",
            {"inputTokens": 20, "outputTokens": 10, "totalTokens": 30},
            "request-1",
        )


def _billing_agent(
    runs: InMemoryAgentRunRepository,
    advisor: RecordingAdvisor | None = None,
) -> BillingAgent:
    assertions = TemporalAssertionService(InMemoryAssertionRepository())
    seed_demo(assertions)
    return BillingAgent(
        DecisionAuditService(assertions, TelecomAdapter()),
        DecisionJournalService(InMemoryDecisionRepository()),
        runs,
        advisor=advisor,
        clock=SequenceClock(),
    )


def test_billing_agent_persists_deterministic_decision_and_bounded_trace() -> None:
    runs = InMemoryAgentRunRepository()
    advisor = RecordingAdvisor()
    result = _billing_agent(runs, advisor).run(
        demo_call(),
        decision_id=DEMO_DECISION_ID,
        decision_time=DEMO_DECISION_TIME,
        investigated_at=DEMO_INVESTIGATION_TIME,
    )

    assert result.journal.record.output["amount"] == "2.50"
    assert result.audit.decision.output["amount"] == Decimal("2.50")
    assert set(advisor.requests[0]["facts"]) == {
        "decision_id",
        "call_id",
        "selected_assertion_id",
        "selected_rate",
        "amount",
        "currency",
        "duration_seconds",
        "evidence_count",
    }
    run = runs.get(result.run_id)
    call = runs.tool_calls(result.run_id)[0]
    assert run.status is AgentRunStatus.COMPLETED
    assert run.output["safety"]["calculation_source"] == "telecom_adapter"
    assert run.output["safety"]["model_mutations_performed"] == 0
    assert call.status is ToolCallStatus.SUCCEEDED
    assert call.result["amount"] == "2.50"


def test_remediation_agent_applies_once_and_traces_safe_replay() -> None:
    runs = InMemoryAgentRunRepository()
    billing = _billing_agent(runs).run(
        demo_call(),
        decision_id=DEMO_DECISION_ID,
        decision_time=DEMO_DECISION_TIME,
        investigated_at=DEMO_INVESTIGATION_TIME,
    )
    repository = InMemoryTelecomRemediationRepository()
    case = demo_case_seed(billing.audit, billing.journal, PRIMARY_DEMO_CASE)
    repository.seed_case(case)
    agent = RemediationAgent(repository, runs, clock=SequenceClock())
    arguments = {
        "remediation_run_id": DEMO_REMEDIATION_RUN_ID,
        "refund_id": DEMO_REFUND_ID,
        "incident_id": DEMO_INCIDENT_ID,
        "memory_id": DEMO_MEMORY_ID,
        "started_at": DEMO_REMEDIATION_START,
        "completed_at": DEMO_REMEDIATION_END,
    }

    first = agent.run(billing.audit, billing.journal, case, **arguments)
    replay = agent.run(billing.audit, billing.journal, case, **arguments)
    state = repository.snapshot(case.dispute_id, first.plan.memory_key)

    assert first.receipt.outcome.value == "applied"
    assert replay.receipt.safe_noop is True
    assert (state.refund_count, state.incident_count, state.procedural_memory_count) == (1, 1, 1)
    assert runs.get(first.run_id).status is AgentRunStatus.COMPLETED
    assert runs.get(replay.run_id).output["safe_noop"] is True
    assert runs.tool_calls(replay.run_id)[0].result["outcome"] == "already_remediated"


def test_advisory_failure_cannot_rollback_remediation() -> None:
    runs = InMemoryAgentRunRepository()
    billing = _billing_agent(runs).run(
        demo_call(),
        decision_id=DEMO_DECISION_ID,
        decision_time=DEMO_DECISION_TIME,
        investigated_at=DEMO_INVESTIGATION_TIME,
    )
    repository = InMemoryTelecomRemediationRepository()
    case = demo_case_seed(billing.audit, billing.journal, PRIMARY_DEMO_CASE)
    repository.seed_case(case)
    result = RemediationAgent(
        repository,
        runs,
        advisor=RecordingAdvisor(fail=True),
        clock=SequenceClock(),
    ).run(
        billing.audit,
        billing.journal,
        case,
        remediation_run_id=DEMO_REMEDIATION_RUN_ID,
        refund_id=DEMO_REFUND_ID,
        incident_id=DEMO_INCIDENT_ID,
        memory_id=DEMO_MEMORY_ID,
        started_at=DEMO_REMEDIATION_START,
        completed_at=DEMO_REMEDIATION_END,
    )

    assert result.receipt.outcome.value == "applied"
    assert result.advisory.status == "unavailable"
    run = runs.get(result.run_id)
    assert run.status is AgentRunStatus.COMPLETED
    assert run.output["advisory_error_code"] == "advisory_provider_unavailable"
    assert "raw-secret-token" not in str(run.output)
    assert repository.snapshot(case.dispute_id, result.plan.memory_key).refund_count == 1


def test_demo_wires_billing_and_remediation_into_agent_run_journal() -> None:
    runs = InMemoryAgentRunRepository()
    correlation_id = uuid5(NAMESPACE_URL, "hindsight:test:workflow-correlation")
    payload = run_demo_workflow(
        InMemoryAssertionRepository(),
        InMemoryDecisionRepository(),
        InMemoryTelecomRemediationRepository(),
        "in_memory",
        agent_run_repository=runs,
        correlation_id=correlation_id,
    )

    execution = payload["agent_execution"]
    run_ids = [
        execution["billing_run_id"],
        *execution["remediation_run_ids"],
        execution["follow_up_billing_run_id"],
    ]
    assert len(run_ids) == 4
    assert all(runs.get(run_id).status is AgentRunStatus.COMPLETED for run_id in run_ids)
    assert all(runs.get(run_id).correlation_id == correlation_id for run_id in run_ids)
    assert execution["correlation_id"] == correlation_id
    assert execution["billing_advisory_status"] == "not_requested"
    assert execution["replay_advisory_status"] == "not_requested"


def test_deterministic_failure_trace_never_persists_raw_database_error() -> None:
    runs = InMemoryAgentRunRepository()
    billing = _billing_agent(runs).run(
        demo_call(),
        decision_id=DEMO_DECISION_ID,
        decision_time=DEMO_DECISION_TIME,
        investigated_at=DEMO_INVESTIGATION_TIME,
    )
    case = demo_case_seed(billing.audit, billing.journal, PRIMARY_DEMO_CASE)
    failed_run_id = uuid5(NAMESPACE_URL, "hindsight:test:failed-remediation-agent-run")

    class FailingRepository:
        def apply_remediation(self, plan: Any) -> Any:
            raise RuntimeError("database failed: postgresql://user:password@example")

    agent = RemediationAgent(
        FailingRepository(),
        runs,
        clock=SequenceClock(),
        id_factory=iter([failed_run_id]).__next__,
    )
    with pytest.raises(RuntimeError, match="database failed"):
        agent.run(
            billing.audit,
            billing.journal,
            case,
            remediation_run_id=DEMO_REMEDIATION_RUN_ID,
            refund_id=DEMO_REFUND_ID,
            incident_id=DEMO_INCIDENT_ID,
            memory_id=DEMO_MEMORY_ID,
            started_at=DEMO_REMEDIATION_START,
            completed_at=DEMO_REMEDIATION_END,
            correlation_id=uuid5(NAMESPACE_URL, "hindsight:test:failed-correlation"),
        )

    run = runs.get(failed_run_id)
    call = runs.tool_calls(failed_run_id)[0]
    assert run.status is AgentRunStatus.FAILED
    assert run.error == {
        "code": "operation_failed",
        "category": "dependency_or_persistence",
    }
    assert call.error == run.error
    assert "password" not in str(run.error)
