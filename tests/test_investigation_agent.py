from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import pytest

from hindsight.adapters.telecom.remediation import InMemoryTelecomRemediationRepository
from hindsight.agents.investigation import AgentProtocolError, InvestigationAgent
from hindsight.core.agents.models import AgentRunStatus, ToolCallStatus
from hindsight.core.agents.repository import InMemoryAgentRunRepository
from hindsight.core.assertions.repository import InMemoryAssertionRepository
from hindsight.core.decisions.repository import InMemoryDecisionRepository
from hindsight.demo import run_demo_workflow
from hindsight.infrastructure.bedrock import ConverseResponse


class ScriptedConverseClient:
    model_id = "test.tool-capable-model"

    def __init__(self, responses: list[ConverseResponse]) -> None:
        self._responses = responses
        self.requests: list[dict[str, Any]] = []

    def converse(self, **request: Any) -> ConverseResponse:
        self.requests.append(deepcopy(request))
        return self._responses.pop(0)


class SequenceClock:
    def __init__(self) -> None:
        self._value = datetime(2026, 7, 3, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        value = self._value
        self._value += timedelta(milliseconds=1)
        return value


def test_investigation_agent_reads_context_then_persists_advisory_output() -> None:
    case_id, context = _demo_context()
    tool_use_id = "tool-use-1"
    client = ScriptedConverseClient(
        [
            ConverseResponse(
                message={
                    "role": "assistant",
                    "content": [
                        {
                            "toolUse": {
                                "toolUseId": tool_use_id,
                                "name": "get_investigation_context",
                                "input": {"case_id": str(case_id)},
                            }
                        }
                    ],
                },
                stop_reason="tool_use",
                usage={"inputTokens": 20, "outputTokens": 5, "totalTokens": 25},
                request_id="request-1",
            ),
            ConverseResponse(
                message={
                    "role": "assistant",
                    "content": [
                        {
                            "text": (
                                "Advisory: the corrected tariff was recorded after the "
                                "decision, so the deterministic verdict finds no agent fault."
                            )
                        }
                    ],
                },
                stop_reason="end_turn",
                usage={"inputTokens": 40, "outputTokens": 15, "totalTokens": 55},
                request_id="request-2",
            ),
        ]
    )
    repository = InMemoryAgentRunRepository()

    result = InvestigationAgent(
        client,
        repository,
        clock=SequenceClock(),
    ).run(case_id=case_id, context=context)

    run = repository.get(result.run_id)
    calls = repository.tool_calls(result.run_id)
    assert run.status is AgentRunStatus.COMPLETED
    assert run.output == result.output
    assert run.usage == {"inputTokens": 60, "outputTokens": 20, "totalTokens": 80}
    assert result.output["safety"]["mutations_performed"] == 0
    assert len(calls) == 1
    assert calls[0].status is ToolCallStatus.SUCCEEDED
    assert calls[0].result["case_id"] == str(case_id)

    second_messages = client.requests[1]["messages"]
    assert second_messages[1]["content"][0]["toolUse"]["toolUseId"] == tool_use_id
    tool_result = second_messages[2]["content"][0]["toolResult"]
    assert tool_result["toolUseId"] == tool_use_id
    assert tool_result["content"][0]["json"] == calls[0].result


def test_investigation_agent_fails_closed_when_model_skips_the_tool() -> None:
    case_id, context = _demo_context()
    client = ScriptedConverseClient(
        [
            ConverseResponse(
                message={
                    "role": "assistant",
                    "content": [{"text": "An unsupported answer without evidence."}],
                },
                stop_reason="end_turn",
                usage={"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
                request_id="request-1",
            )
        ]
    )
    repository = InMemoryAgentRunRepository()
    run_id = UUID("00000000-0000-0000-0000-000000000001")
    ids = iter((run_id, UUID("00000000-0000-0000-0000-000000000002")))

    with pytest.raises(AgentProtocolError):
        InvestigationAgent(
            client,
            repository,
            clock=SequenceClock(),
            id_factory=lambda: next(ids),
        ).run(
            case_id=case_id,
            context=context,
        )

    run = repository.get(run_id)
    assert run.status is AgentRunStatus.FAILED
    assert run.error == {
        "code": "context_not_retrieved",
        "category": "protocol_or_policy",
        "retryable": False,
        "request_ids": ["request-1"],
        "model_turns": 1,
    }
    assert run.usage == {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15}
    assert repository.tool_calls(run.id) == ()


def _demo_context() -> tuple[UUID, dict[str, object]]:
    payload = run_demo_workflow(
        InMemoryAssertionRepository(),
        InMemoryDecisionRepository(),
        InMemoryTelecomRemediationRepository(),
        "in_memory",
        include_investigation_context=True,
    )
    learning = cast(dict[str, object], payload["learning_proof"])
    context = cast(dict[str, object], learning["investigation_context"])
    return UUID(str(context["case_id"])), context
