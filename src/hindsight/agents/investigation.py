import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Protocol
from uuid import UUID, uuid4, uuid5

from hindsight.core.agents.models import (
    AgentRunRecord,
    AgentRunStatus,
    ToolCallRecord,
    ToolCallStatus,
)
from hindsight.core.agents.repository import AgentRunRepository

TOOL_NAME = "get_investigation_context"
PROMPT_VERSION = "investigation-v3"
TOOLSET_VERSION = "telecom-readonly-v3-mcp"
MAX_MODEL_TURNS = 3
MAX_TOOL_CALLS = 1
MAX_TOOL_RESULT_BYTES = 64_000
MAX_EXPLANATION_CHARS = 10_000

SYSTEM_PROMPT = """You are the HindSight investigation explanation agent.
You must call get_investigation_context before answering and use only its returned facts.
The deterministic temporal engine owns the verdict and the telecom adapter owns all amounts.
Do not recalculate, override, or mutate them. Timestamp semantics are strict:
decision.event_occurred_at is the domain event time, decision.decision_made_at is the decision
time, current_truth.valid_from is business validity, and current_truth.recorded_at is when the
system learned the truth. knowledge_gap_seconds is current_truth.recorded_at minus
current_truth.valid_from, not elapsed time after the decision. Never invent or rename events or
timestamps, and never derive durations. Do not invent a dispute resolution time. Explain the
event timeline, knowledge boundary, agent fault, confirmed root cause, and advisory procedure
concisely. Return at most 220 words in exactly five labeled bullets: Timeline, Knowledge
boundary, Accountability, Root cause, and Advisory procedure. Do not repeat identifiers unless
needed to distinguish evidence. Clearly label the explanation and procedural guidance as
advisory."""

TOOL_CONFIG: dict[str, Any] = {
    "tools": [
        {
            "toolSpec": {
                "name": TOOL_NAME,
                "description": (
                    "Read the deterministic evidence, verdict, financial comparison, and "
                    "procedural guidance for the single dispute assigned to this run."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "case_id": {
                                "type": "string",
                                "description": "The assigned dispute UUID.",
                            }
                        },
                        "required": ["case_id"],
                        "additionalProperties": False,
                    }
                },
            }
        }
    ]
}

_CONTEXT_FIELDS = {
    "case_id",
    "decision",
    "current_truth",
    "known_at_decision",
    "evidence",
    "comparison",
    "verdict",
    "procedural_guidance",
    "authority",
}


class ConverseResponse(Protocol):
    message: dict[str, Any]
    stop_reason: str
    usage: dict[str, int]
    request_id: str | None


class ConverseClient(Protocol):
    @property
    def model_id(self) -> str: ...

    def converse(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, Any]],
        tool_config: dict[str, Any],
        request_metadata: dict[str, str],
    ) -> ConverseResponse: ...


class InvestigationContextReader(Protocol):
    @property
    def source(self) -> str: ...

    def read(self, case_id: UUID) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class _StaticContextReader:
    context: Mapping[str, object]
    source: str = "local_deterministic_context"

    def read(self, case_id: UUID) -> Mapping[str, object]:
        return self.context


@dataclass(frozen=True, slots=True)
class InvestigationResult:
    run_id: UUID
    output: dict[str, Any]


@dataclass(slots=True)
class _ConversationAccounting:
    usage: dict[str, int] = field(default_factory=dict)
    request_ids: list[str] = field(default_factory=list)
    model_turns: int = 0
    stop_reason: str | None = None


class InvestigationAgent:
    def __init__(
        self,
        client: ConverseClient,
        repository: AgentRunRepository,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], UUID] | None = None,
        context_reader: InvestigationContextReader | None = None,
    ) -> None:
        self._client = client
        self._repository = repository
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or uuid4
        self._context_reader = context_reader

    def run(
        self,
        *,
        case_id: UUID,
        context: Mapping[str, object] | None = None,
        correlation_id: UUID | None = None,
    ) -> InvestigationResult:
        if self._context_reader is None:
            if context is None:
                raise ValueError("an investigation context or context reader is required")
            context_reader: InvestigationContextReader = _StaticContextReader(context)
        else:
            if context is not None:
                raise ValueError("context cannot be supplied with a context reader")
            context_reader = self._context_reader
        run_id = self._id_factory()
        correlation_id = correlation_id or self._id_factory()
        started_at = self._clock()
        input_summary = {
            "case_id": str(case_id),
            "allowed_tool": TOOL_NAME,
            "context_source": context_reader.source,
        }
        context_reference = getattr(context_reader, "reference", None)
        if isinstance(context_reference, str) and context_reference:
            input_summary["context_reference"] = context_reference
        self._repository.start_run(
            AgentRunRecord(
                id=run_id,
                correlation_id=correlation_id,
                domain="telecom",
                agent_id="investigation_agent",
                run_type="explain_decision_accountability",
                subject_type="telecom_dispute",
                subject_id=str(case_id),
                provider="amazon_bedrock",
                model_id=self._client.model_id,
                prompt_version=PROMPT_VERSION,
                toolset_version=TOOLSET_VERSION,
                status=AgentRunStatus.RUNNING,
                started_at=started_at,
                updated_at=started_at,
                completed_at=None,
                input_summary=input_summary,
            )
        )

        accounting = _ConversationAccounting()
        try:
            output, usage = self._run_conversation(
                run_id,
                correlation_id,
                case_id,
                context_reader,
                accounting,
            )
            completed_at = self._clock()
            self._repository.complete_run(
                run_id,
                output=output,
                usage=usage,
                stop_reason="end_turn",
                completed_at=completed_at,
            )
            return InvestigationResult(run_id, output)
        except Exception as error:
            failure = _failure_payload(error, accounting)
            try:
                self._repository.fail_run(
                    run_id,
                    error=failure,
                    usage=accounting.usage,
                    completed_at=self._clock(),
                    stop_reason=accounting.stop_reason,
                )
            except Exception as journal_error:
                raise InvestigationAgentError(
                    "agent execution and durable failure journaling both failed",
                    run_id=run_id,
                ) from journal_error
            if isinstance(error, InvestigationAgentError):
                error.run_id = run_id
                raise
            raise InvestigationAgentError(
                "Bedrock investigation failed",
                run_id=run_id,
            ) from error

    def _run_conversation(
        self,
        run_id: UUID,
        correlation_id: UUID,
        case_id: UUID,
        context_reader: InvestigationContextReader,
        accounting: _ConversationAccounting,
    ) -> tuple[dict[str, Any], dict[str, int]]:
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {
                        "text": (
                            "Investigate dispute "
                            f"{case_id} and explain the deterministic accountability result."
                        )
                    }
                ],
            }
        ]
        tool_call_count = 0

        for model_turn in range(1, MAX_MODEL_TURNS + 1):
            response = self._client.converse(
                system_prompt=SYSTEM_PROMPT,
                messages=messages,
                tool_config=TOOL_CONFIG,
                request_metadata={
                    "run_id": str(run_id),
                    "correlation_id": str(correlation_id),
                },
            )
            accounting.model_turns = model_turn
            accounting.stop_reason = response.stop_reason
            _merge_usage(accounting.usage, response.usage)
            if response.request_id:
                accounting.request_ids.append(response.request_id)
            message = _assistant_message(response.message)
            messages.append(message)

            if response.stop_reason == "tool_use":
                requests = _tool_requests(message)
                if not requests:
                    raise AgentProtocolError(
                        "missing_tool_request",
                        "Bedrock stopped for tool use without a tool request",
                    )
                if tool_call_count + len(requests) > MAX_TOOL_CALLS:
                    raise AgentProtocolError(
                        "tool_budget_exceeded",
                        "Bedrock exceeded the read-only tool budget",
                    )
                results: list[dict[str, Any]] = []
                for request in requests:
                    tool_call_count += 1
                    results.append(
                        self._execute_tool(
                            run_id,
                            tool_call_count,
                            case_id,
                            context_reader,
                            request,
                        )
                    )
                messages.append({"role": "user", "content": results})
                continue

            if response.stop_reason != "end_turn":
                raise AgentProtocolError(
                    "unsupported_stop_reason",
                    f"Bedrock stopped with {response.stop_reason}; the explanation is incomplete",
                )
            if tool_call_count == 0:
                raise AgentProtocolError(
                    "context_not_retrieved",
                    "Bedrock answered without reading the investigation context",
                )
            explanation = _final_text(message)
            output = {
                "provider": "amazon_bedrock",
                "model_id": self._client.model_id,
                "advisory_explanation": explanation,
                "request_ids": accounting.request_ids,
                "model_turns": model_turn,
                "successful_tool_calls": tool_call_count,
                "safety": {
                    "tool_access": "read_only",
                    "mutations_performed": 0,
                    "verdict_source": "deterministic_temporal_engine",
                    "financial_source": "telecom_adapter",
                    "model_output_role": "advisory_explanation",
                    "context_transport": context_reader.source,
                },
            }
            return output, accounting.usage

        raise AgentProtocolError(
            "model_turn_budget_exceeded",
            "Bedrock exceeded the model turn budget",
        )

    def _execute_tool(
        self,
        run_id: UUID,
        sequence_number: int,
        case_id: UUID,
        context_reader: InvestigationContextReader,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        tool_use_id = request.get("toolUseId")
        name = request.get("name")
        arguments = request.get("input")
        if not isinstance(tool_use_id, str) or not tool_use_id or len(tool_use_id) > 64:
            raise AgentProtocolError("invalid_tool_use_id", "toolUseId is invalid")
        if not isinstance(name, str) or not name or len(name) > 64:
            raise AgentProtocolError("invalid_tool_name", "tool name is invalid")
        if not isinstance(arguments, dict):
            raise AgentProtocolError("invalid_tool_arguments", "tool input must be an object")

        requested_at = self._clock()
        try:
            _validate_context_request(name, arguments, case_id)
            try:
                result = prepare_investigation_context(
                    case_id,
                    context_reader.read(case_id),
                )
            except InvestigationContextReadError:
                raise
            except (TypeError, ValueError) as error:
                raise InvestigationContextReadError(
                    "invalid_context_result",
                    "the investigation context source returned invalid data",
                    retryable=False,
                ) from error
        except Exception as error:
            failure = _tool_failure(error)
            self._repository.record_tool_call(
                ToolCallRecord(
                    id=uuid5(run_id, tool_use_id),
                    run_id=run_id,
                    tool_use_id=tool_use_id,
                    sequence_number=sequence_number,
                    tool_name=name,
                    status=ToolCallStatus.FAILED,
                    requested_at=requested_at,
                    completed_at=self._clock(),
                    arguments=_safe_arguments(arguments),
                    error=failure,
                )
            )
            if isinstance(error, InvestigationAgentError):
                raise
            raise InvestigationContextReadError(
                "context_read_failed",
                "the investigation context source failed",
                retryable=True,
            ) from error

        self._repository.record_tool_call(
            ToolCallRecord(
                id=uuid5(run_id, tool_use_id),
                run_id=run_id,
                tool_use_id=tool_use_id,
                sequence_number=sequence_number,
                tool_name=name,
                status=ToolCallStatus.SUCCEEDED,
                requested_at=requested_at,
                completed_at=self._clock(),
                arguments=_safe_arguments(arguments),
                result=result,
            )
        )
        return {
            "toolResult": {
                "toolUseId": tool_use_id,
                "content": [{"json": result}],
            }
        }


def prepare_investigation_context(
    case_id: UUID,
    context: Mapping[str, object],
) -> dict[str, Any]:
    if set(context) != _CONTEXT_FIELDS:
        raise ValueError("investigation context fields are not allowlisted")
    normalized = _json_value(dict(context))
    if not isinstance(normalized, dict) or normalized.get("case_id") != str(case_id):
        raise ValueError("investigation context does not match the assigned case")
    encoded = json.dumps(normalized, separators=(",", ":")).encode()
    if len(encoded) > MAX_TOOL_RESULT_BYTES:
        raise ValueError("investigation context exceeds the tool result budget")
    return normalized


def _validate_context_request(
    name: str,
    arguments: dict[str, Any],
    case_id: UUID,
) -> None:
    if name != TOOL_NAME:
        raise AgentProtocolError("unknown_tool", "the requested tool is not allowlisted")
    if set(arguments) != {"case_id"}:
        raise AgentProtocolError(
            "invalid_tool_arguments",
            "the tool accepts only case_id",
        )
    try:
        requested_case_id = UUID(str(arguments["case_id"]))
    except (TypeError, ValueError) as error:
        raise AgentProtocolError(
            "invalid_case_id",
            "case_id must be a UUID",
        ) from error
    if requested_case_id != case_id:
        raise AgentProtocolError(
            "cross_case_access_denied",
            "the tool cannot access a different dispute",
        )


def _assistant_message(message: object) -> dict[str, Any]:
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise AgentProtocolError("invalid_model_message", "Bedrock returned an invalid role")
    content = message.get("content")
    if not isinstance(content, list) or not all(isinstance(item, dict) for item in content):
        raise AgentProtocolError(
            "invalid_model_message",
            "Bedrock returned invalid message content",
        )
    return message


def _tool_requests(message: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        block["toolUse"] for block in message["content"] if isinstance(block.get("toolUse"), dict)
    ]


def _final_text(message: dict[str, Any]) -> str:
    parts = [
        block["text"].strip()
        for block in message["content"]
        if isinstance(block.get("text"), str) and block["text"].strip()
    ]
    explanation = "\n".join(parts)
    if not explanation:
        raise AgentProtocolError("empty_model_output", "Bedrock returned no explanation")
    if len(explanation) > MAX_EXPLANATION_CHARS:
        raise AgentProtocolError("model_output_too_large", "Bedrock output exceeded its budget")
    return explanation


def _safe_arguments(arguments: Mapping[str, object]) -> dict[str, Any]:
    return {key: _json_value(value) for key, value in arguments.items() if key == "case_id"}


def _json_value(value: object) -> Any:
    if isinstance(value, Enum):
        return _json_value(value.value)
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            raise ValueError("investigation timestamps must be timezone-aware")
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("investigation JSON keys must be strings")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    raise TypeError(f"unsupported investigation JSON type: {type(value).__name__}")


def _merge_usage(total: dict[str, int], current: Mapping[str, int]) -> None:
    for key, value in current.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise AgentProtocolError("invalid_usage", "Bedrock returned invalid token usage")
        total[key] = total.get(key, 0) + value


def _failure_payload(
    error: Exception,
    accounting: _ConversationAccounting,
) -> dict[str, Any]:
    if isinstance(error, AgentProtocolError):
        failure = {
            "code": error.code,
            "category": "protocol_or_policy",
            "retryable": False,
        }
    elif isinstance(error, InvestigationContextReadError):
        failure = {
            "code": error.code,
            "category": "context_transport",
            "retryable": error.retryable,
        }
    else:
        failure = {
            "code": "agent_execution_failed",
            "category": "provider_or_persistence",
            "retryable": True,
        }
    return {
        **failure,
        "request_ids": accounting.request_ids,
        "model_turns": accounting.model_turns,
        "provider_stop_reason": accounting.stop_reason,
    }


def _tool_failure(error: Exception) -> dict[str, Any]:
    if isinstance(error, AgentProtocolError):
        return {"code": error.code, "retryable": False}
    if isinstance(error, InvestigationContextReadError):
        return {"code": error.code, "retryable": error.retryable}
    return {"code": "context_read_failed", "retryable": True}


class InvestigationAgentError(RuntimeError):
    def __init__(self, message: str, *, run_id: UUID | None = None) -> None:
        super().__init__(message)
        self.run_id = run_id


class AgentProtocolError(InvestigationAgentError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class InvestigationContextReadError(InvestigationAgentError):
    def __init__(self, code: str, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
