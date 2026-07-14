import json
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any
from uuid import UUID

from hindsight.core.agents.models import (
    AgentRunRecord,
    AgentRunStatus,
    ToolCallRecord,
    ToolCallStatus,
)
from hindsight.core.agents.repository import (
    AgentRunConflictError,
    AgentRunNotFoundError,
    AgentRunStateError,
)

INSERT_RUN_SQL = """
INSERT INTO agent_runs (
  id, correlation_id, domain, agent_id, run_type, subject_type, subject_id,
  provider, model_id, prompt_version, toolset_version, status, started_at,
  updated_at, completed_at, input_summary, output, error, usage, stop_reason
)
VALUES (
  %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
  CAST(%s AS JSONB), %s, %s, CAST(%s AS JSONB), %s
)
ON CONFLICT (id) DO NOTHING
"""

INSERT_TOOL_CALL_SQL = """
INSERT INTO tool_calls (
  id, run_id, tool_use_id, sequence_number, tool_name, status,
  requested_at, completed_at, arguments, result, error
)
VALUES (
  %s, %s, %s, %s, %s, %s, %s, %s,
  CAST(%s AS JSONB), CAST(%s AS JSONB), CAST(%s AS JSONB)
)
ON CONFLICT (run_id, tool_use_id) DO NOTHING
"""

SELECT_RUN_SQL = "SELECT * FROM agent_runs WHERE id = %s"
SELECT_TOOL_CALL_SQL = """
SELECT * FROM tool_calls WHERE run_id = %s AND tool_use_id = %s
"""
SELECT_TOOL_CALLS_SQL = """
SELECT * FROM tool_calls WHERE run_id = %s ORDER BY sequence_number
"""
MAX_JSON_BYTES = 64_000


class CockroachAgentRunRepository:
    def __init__(
        self,
        connection: Any,
        max_retries: int = 3,
        connection_factory: Callable[[], Any] | None = None,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        self._connection = connection
        self._max_retries = max_retries
        self._connection_factory = connection_factory

    def start_run(self, record: AgentRunRecord) -> AgentRunRecord:
        if (
            record.status is not AgentRunStatus.RUNNING
            or record.updated_at != record.started_at
            or record.usage
        ):
            raise ValueError("a new agent run must be an unmodified running record")

        def operation() -> AgentRunRecord:
            with self._connection.transaction():
                self._connection.execute(INSERT_RUN_SQL, _run_values(record))
                persisted = _fetch_run(self._connection, record.id)
                if persisted is None:
                    raise AgentRunStateError("agent run insert did not persist")
                _ensure_same(_as_start(persisted), record)
                return persisted

        return self._write(operation, lambda repository: repository.start_run(record))

    def record_tool_call(self, record: ToolCallRecord) -> ToolCallRecord:
        def operation() -> ToolCallRecord:
            with self._connection.transaction():
                run = _required_run(self._connection, record.run_id)
                existing = _fetch_tool_call(
                    self._connection,
                    record.run_id,
                    record.tool_use_id,
                )
                if existing is not None:
                    _ensure_same(existing, record)
                    return existing
                if run.status is not AgentRunStatus.RUNNING:
                    raise AgentRunStateError("cannot add a tool call to a terminal run")
                result = _dump_json(record.result) if record.result is not None else None
                error = _dump_json(record.error) if record.error is not None else None
                self._connection.execute(
                    INSERT_TOOL_CALL_SQL,
                    (
                        record.id,
                        record.run_id,
                        record.tool_use_id,
                        record.sequence_number,
                        record.tool_name,
                        record.status.value,
                        record.requested_at,
                        record.completed_at,
                        _dump_json(record.arguments),
                        result,
                        error,
                    ),
                )
                persisted = _fetch_tool_call(
                    self._connection,
                    record.run_id,
                    record.tool_use_id,
                )
                if persisted is None:
                    raise AgentRunStateError("tool call insert did not persist")
                _ensure_same(persisted, record)
                return persisted

        return self._write(
            operation,
            lambda repository: repository.record_tool_call(record),
        )

    def complete_run(
        self,
        run_id: UUID,
        *,
        output: dict[str, Any],
        usage: dict[str, int],
        stop_reason: str,
        completed_at: datetime,
    ) -> AgentRunRecord:
        return self._finish(
            run_id,
            AgentRunStatus.COMPLETED,
            completed_at,
            output=output,
            usage=usage,
            stop_reason=stop_reason,
        )

    def fail_run(
        self,
        run_id: UUID,
        *,
        error: dict[str, Any],
        usage: dict[str, int],
        completed_at: datetime,
        stop_reason: str | None = None,
    ) -> AgentRunRecord:
        return self._finish(
            run_id,
            AgentRunStatus.FAILED,
            completed_at,
            error=error,
            usage=usage,
            stop_reason=stop_reason,
        )

    def get(self, run_id: UUID) -> AgentRunRecord:
        return _required_run(self._connection, run_id)

    def tool_calls(self, run_id: UUID) -> tuple[ToolCallRecord, ...]:
        self.get(run_id)
        rows = self._connection.execute(SELECT_TOOL_CALLS_SQL, (run_id,)).fetchall()
        return tuple(_tool_call_from_row(row) for row in rows)

    def _finish(
        self,
        run_id: UUID,
        status: AgentRunStatus,
        completed_at: datetime,
        *,
        output: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        usage: dict[str, int] | None = None,
        stop_reason: str | None = None,
    ) -> AgentRunRecord:
        def operation() -> AgentRunRecord:
            with self._connection.transaction():
                current = _required_run(self._connection, run_id)
                terminal = replace(
                    current,
                    status=status,
                    updated_at=completed_at,
                    completed_at=completed_at,
                    output=output,
                    error=error,
                    usage=current.usage if usage is None else usage,
                    stop_reason=stop_reason,
                )
                if current.status is not AgentRunStatus.RUNNING:
                    _ensure_same(current, terminal)
                    return current
                self._connection.execute(
                    """
                    UPDATE agent_runs
                    SET status = %s, updated_at = %s, completed_at = %s,
                        output = CAST(%s AS JSONB), error = CAST(%s AS JSONB),
                        usage = CAST(%s AS JSONB), stop_reason = %s
                    WHERE id = %s AND status = 'running'
                    """,
                    (
                        status.value,
                        completed_at,
                        completed_at,
                        _dump_json(output) if output is not None else None,
                        _dump_json(error) if error is not None else None,
                        _dump_json(current.usage if usage is None else usage),
                        stop_reason,
                        run_id,
                    ),
                )
                persisted = _required_run(self._connection, run_id)
                _ensure_same(persisted, terminal)
                return persisted

        def recover(repository: Any) -> AgentRunRecord:
            if status is AgentRunStatus.COMPLETED:
                return repository.complete_run(
                    run_id,
                    output=output or {},
                    usage=usage or {},
                    stop_reason=stop_reason or "end_turn",
                    completed_at=completed_at,
                )
            return repository.fail_run(
                run_id,
                error=error or {"code": "unknown", "retryable": False},
                usage=usage or {},
                completed_at=completed_at,
                stop_reason=stop_reason,
            )

        return self._write(operation, recover)

    def _write(self, operation: Any, recover: Callable[[Any], Any]) -> Any:
        try:
            return self._retry(operation)
        except Exception as error:
            if getattr(error, "sqlstate", None) != "40003":
                raise
            if self._connection_factory is None:
                raise AgentRunStateError(
                    "agent journal commit outcome is unknown; retry with a fresh connection"
                ) from error
            with self._connection_factory() as connection:
                repository = CockroachAgentRunRepository(
                    connection,
                    max_retries=self._max_retries,
                )
                return recover(repository)

    def _retry(self, operation: Any) -> Any:
        for attempt in range(self._max_retries + 1):
            try:
                return operation()
            except (AgentRunConflictError, AgentRunNotFoundError, AgentRunStateError):
                raise
            except Exception as error:
                if getattr(error, "sqlstate", None) != "40001" or attempt == self._max_retries:
                    raise
                time.sleep(0.05 * 2**attempt)
        raise RuntimeError("unreachable retry state")


def _run_values(record: AgentRunRecord) -> tuple[object, ...]:
    return (
        record.id,
        record.correlation_id,
        record.domain,
        record.agent_id,
        record.run_type,
        record.subject_type,
        record.subject_id,
        record.provider,
        record.model_id,
        record.prompt_version,
        record.toolset_version,
        record.status.value,
        record.started_at,
        record.updated_at,
        record.completed_at,
        _dump_json(record.input_summary),
        None,
        None,
        _dump_json(record.usage),
        record.stop_reason,
    )


def _fetch_run(connection: Any, run_id: UUID) -> AgentRunRecord | None:
    row = connection.execute(SELECT_RUN_SQL, (run_id,)).fetchone()
    return _run_from_row(row) if row is not None else None


def _required_run(connection: Any, run_id: UUID) -> AgentRunRecord:
    record = _fetch_run(connection, run_id)
    if record is None:
        raise AgentRunNotFoundError(f"agent run {run_id} was not found")
    return record


def _fetch_tool_call(
    connection: Any,
    run_id: UUID,
    tool_use_id: str,
) -> ToolCallRecord | None:
    row = connection.execute(SELECT_TOOL_CALL_SQL, (run_id, tool_use_id)).fetchone()
    return _tool_call_from_row(row) if row is not None else None


def _run_from_row(row: Mapping[str, Any]) -> AgentRunRecord:
    return AgentRunRecord(
        id=UUID(str(row["id"])),
        correlation_id=UUID(str(row["correlation_id"])),
        domain=str(row["domain"]),
        agent_id=str(row["agent_id"]),
        run_type=str(row["run_type"]),
        subject_type=str(row["subject_type"]),
        subject_id=str(row["subject_id"]),
        provider=str(row["provider"]),
        model_id=str(row["model_id"]),
        prompt_version=str(row["prompt_version"]),
        toolset_version=str(row["toolset_version"]),
        status=AgentRunStatus(str(row["status"])),
        started_at=row["started_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
        input_summary=_json_object(row["input_summary"]),
        output=_optional_json_object(row["output"]),
        error=_optional_json_object(row["error"]),
        usage={key: int(value) for key, value in _json_object(row["usage"]).items()},
        stop_reason=row["stop_reason"],
    )


def _tool_call_from_row(row: Mapping[str, Any]) -> ToolCallRecord:
    return ToolCallRecord(
        id=UUID(str(row["id"])),
        run_id=UUID(str(row["run_id"])),
        tool_use_id=str(row["tool_use_id"]),
        sequence_number=int(row["sequence_number"]),
        tool_name=str(row["tool_name"]),
        status=ToolCallStatus(str(row["status"])),
        requested_at=row["requested_at"],
        completed_at=row["completed_at"],
        arguments=_json_object(row["arguments"]),
        result=_optional_json_object(row["result"]),
        error=_optional_json_object(row["error"]),
    )


def _json_object(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("agent journal JSON must be an object")
    return value


def _optional_json_object(value: object) -> dict[str, Any] | None:
    return None if value is None else _json_object(value)


def _dump_json(value: object) -> str:
    encoded = json.dumps(value, allow_nan=False, separators=(",", ":"))
    if len(encoded.encode()) > MAX_JSON_BYTES:
        raise ValueError("agent journal JSON exceeds the 64 KB limit")
    return encoded


def _ensure_same(existing: object, candidate: object) -> None:
    if existing != candidate:
        raise AgentRunConflictError("identifier already refers to different agent data")


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
