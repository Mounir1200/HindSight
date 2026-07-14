from dataclasses import replace
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from hindsight.core.agents.models import (
    AgentRunRecord,
    AgentRunStatus,
    ToolCallRecord,
)


class AgentRunRepository(Protocol):
    def start_run(self, record: AgentRunRecord) -> AgentRunRecord: ...

    def record_tool_call(self, record: ToolCallRecord) -> ToolCallRecord: ...

    def complete_run(
        self,
        run_id: UUID,
        *,
        output: dict[str, Any],
        usage: dict[str, int],
        stop_reason: str,
        completed_at: datetime,
    ) -> AgentRunRecord: ...

    def fail_run(
        self,
        run_id: UUID,
        *,
        error: dict[str, Any],
        usage: dict[str, int],
        completed_at: datetime,
    ) -> AgentRunRecord: ...

    def get(self, run_id: UUID) -> AgentRunRecord: ...

    def tool_calls(self, run_id: UUID) -> tuple[ToolCallRecord, ...]: ...


class InMemoryAgentRunRepository:
    def __init__(self) -> None:
        self._runs: dict[UUID, AgentRunRecord] = {}
        self._tool_calls: dict[UUID, dict[str, ToolCallRecord]] = {}

    def start_run(self, record: AgentRunRecord) -> AgentRunRecord:
        _validate_new_run(record)
        existing = self._runs.get(record.id)
        if existing is not None:
            _ensure_same(_as_start(existing), record)
            return existing
        self._runs[record.id] = record
        self._tool_calls[record.id] = {}
        return record

    def record_tool_call(self, record: ToolCallRecord) -> ToolCallRecord:
        run = self.get(record.run_id)
        calls = self._tool_calls[record.run_id]
        existing = calls.get(record.tool_use_id)
        if existing is not None:
            _ensure_same(existing, record)
            return existing
        if run.status is not AgentRunStatus.RUNNING:
            raise AgentRunStateError("cannot add a tool call to a terminal run")
        if any(item.sequence_number == record.sequence_number for item in calls.values()):
            raise AgentRunConflictError("tool call sequence already exists")
        calls[record.tool_use_id] = record
        return record

    def complete_run(
        self,
        run_id: UUID,
        *,
        output: dict[str, Any],
        usage: dict[str, int],
        stop_reason: str,
        completed_at: datetime,
    ) -> AgentRunRecord:
        current = self.get(run_id)
        completed = replace(
            current,
            status=AgentRunStatus.COMPLETED,
            updated_at=completed_at,
            completed_at=completed_at,
            output=output,
            usage=usage,
            stop_reason=stop_reason,
        )
        return self._finish(current, completed)

    def fail_run(
        self,
        run_id: UUID,
        *,
        error: dict[str, Any],
        usage: dict[str, int],
        completed_at: datetime,
    ) -> AgentRunRecord:
        current = self.get(run_id)
        failed = replace(
            current,
            status=AgentRunStatus.FAILED,
            updated_at=completed_at,
            completed_at=completed_at,
            error=error,
            usage=usage,
        )
        return self._finish(current, failed)

    def get(self, run_id: UUID) -> AgentRunRecord:
        try:
            return self._runs[run_id]
        except KeyError as error:
            raise AgentRunNotFoundError(f"agent run {run_id} was not found") from error

    def tool_calls(self, run_id: UUID) -> tuple[ToolCallRecord, ...]:
        self.get(run_id)
        return tuple(
            sorted(
                self._tool_calls[run_id].values(),
                key=lambda item: item.sequence_number,
            )
        )

    def _finish(
        self,
        current: AgentRunRecord,
        terminal: AgentRunRecord,
    ) -> AgentRunRecord:
        if current.status is not AgentRunStatus.RUNNING:
            _ensure_same(current, terminal)
            return current
        self._runs[current.id] = terminal
        return terminal


def _ensure_same(existing: object, candidate: object) -> None:
    if existing != candidate:
        raise AgentRunConflictError("identifier already refers to different agent data")


def _validate_new_run(record: AgentRunRecord) -> None:
    if (
        record.status is not AgentRunStatus.RUNNING
        or record.updated_at != record.started_at
        or record.usage
    ):
        raise ValueError("a new agent run must be an unmodified running record")


def _as_start(record: AgentRunRecord) -> AgentRunRecord:
    return replace(
        record,
        status=AgentRunStatus.RUNNING,
        updated_at=record.started_at,
        completed_at=None,
        output=None,
        error=None,
        usage={},
        stop_reason=None,
    )


class AgentRunConflictError(ValueError):
    pass


class AgentRunStateError(RuntimeError):
    pass


class AgentRunNotFoundError(LookupError):
    pass
