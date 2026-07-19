from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid5

from hindsight.core.agents.models import (
    AgentRunRecord,
    AgentRunStatus,
    ToolCallRecord,
    ToolCallStatus,
)
from hindsight.core.agents.repository import AgentRunRepository


@dataclass(frozen=True, slots=True)
class TraceIdentity:
    agent_id: str
    run_type: str
    subject_type: str
    subject_id: str
    provider: str
    model_id: str
    prompt_version: str
    toolset_version: str


class AgentTrace:
    def __init__(
        self,
        repository: AgentRunRepository,
        clock: Callable[[], datetime],
        run_id: UUID,
        correlation_id: UUID,
    ) -> None:
        self._repository = repository
        self._clock = clock
        self.run_id = run_id
        self.correlation_id = correlation_id

    def start(self, identity: TraceIdentity, input_summary: Mapping[str, Any]) -> None:
        now = self._clock()
        self._repository.start_run(
            AgentRunRecord(
                id=self.run_id,
                correlation_id=self.correlation_id,
                domain="telecom",
                agent_id=identity.agent_id,
                run_type=identity.run_type,
                subject_type=identity.subject_type,
                subject_id=identity.subject_id,
                provider=identity.provider,
                model_id=identity.model_id,
                prompt_version=identity.prompt_version,
                toolset_version=identity.toolset_version,
                status=AgentRunStatus.RUNNING,
                started_at=now,
                updated_at=now,
                completed_at=None,
                input_summary=dict(input_summary),
            )
        )

    def requested(self) -> datetime:
        return self._clock()

    def tool_succeeded(
        self,
        tool_use_id: str,
        tool_name: str,
        requested_at: datetime,
        arguments: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> None:
        self._record_tool(
            tool_use_id,
            tool_name,
            requested_at,
            ToolCallStatus.SUCCEEDED,
            arguments,
            result=dict(result),
        )

    def tool_failed(
        self,
        tool_use_id: str,
        tool_name: str,
        requested_at: datetime,
        arguments: Mapping[str, Any],
        error: Exception,
    ) -> None:
        self._record_tool(
            tool_use_id,
            tool_name,
            requested_at,
            ToolCallStatus.FAILED,
            arguments,
            error=_error(error),
        )

    def complete(
        self,
        output: Mapping[str, Any],
        usage: Mapping[str, int],
    ) -> AgentRunRecord:
        return self._repository.complete_run(
            self.run_id,
            output=dict(output),
            usage=dict(usage),
            stop_reason="completed",
            completed_at=self._clock(),
        )

    def fail(self, error: Exception) -> AgentRunRecord:
        return self._repository.fail_run(
            self.run_id,
            error=_error(error),
            usage={},
            completed_at=self._clock(),
        )

    def _record_tool(
        self,
        tool_use_id: str,
        tool_name: str,
        requested_at: datetime,
        status: ToolCallStatus,
        arguments: Mapping[str, Any],
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        self._repository.record_tool_call(
            ToolCallRecord(
                id=uuid5(self.run_id, tool_use_id),
                run_id=self.run_id,
                tool_use_id=tool_use_id,
                sequence_number=1,
                tool_name=tool_name,
                status=status,
                requested_at=requested_at,
                completed_at=self._clock(),
                arguments=dict(arguments),
                result=result,
                error=error,
            )
        )


def _error(error: Exception) -> dict[str, str]:
    if isinstance(error, LookupError):
        return {"code": "subject_not_found", "category": "domain_state"}
    if isinstance(error, ValueError):
        return {"code": "operation_rejected", "category": "domain_state"}
    return {"code": "operation_failed", "category": "dependency_or_persistence"}
