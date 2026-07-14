from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


class AgentRunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ToolCallStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AgentRunRecord:
    id: UUID
    correlation_id: UUID
    domain: str
    agent_id: str
    run_type: str
    subject_type: str
    subject_id: str
    provider: str
    model_id: str
    prompt_version: str
    toolset_version: str
    status: AgentRunStatus
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    input_summary: dict[str, Any]
    output: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str | None = None

    def __post_init__(self) -> None:
        _validate_text(
            self.domain,
            self.agent_id,
            self.run_type,
            self.subject_type,
            self.subject_id,
            self.provider,
            self.model_id,
            self.prompt_version,
            self.toolset_version,
        )
        limits = (
            (self.domain, 64),
            (self.agent_id, 64),
            (self.run_type, 128),
            (self.subject_type, 64),
            (self.subject_id, 256),
            (self.provider, 64),
            (self.model_id, 512),
            (self.prompt_version, 64),
            (self.toolset_version, 64),
        )
        if any(len(value) > limit for value, limit in limits):
            raise ValueError("agent run text field exceeds its storage limit")
        _validate_timestamp(self.started_at, "started_at")
        _validate_timestamp(self.updated_at, "updated_at")
        if self.updated_at < self.started_at:
            raise ValueError("updated_at cannot be earlier than started_at")
        if self.completed_at is not None:
            _validate_timestamp(self.completed_at, "completed_at")
            if self.completed_at < self.started_at:
                raise ValueError("completed_at cannot be earlier than started_at")
        if self.usage is not None and any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in self.usage.values()
        ):
            raise ValueError("usage values must be non-negative integers")

        if self.status is AgentRunStatus.RUNNING:
            valid = (
                self.completed_at is None
                and self.output is None
                and self.error is None
                and self.stop_reason is None
            )
        elif self.status is AgentRunStatus.COMPLETED:
            valid = (
                self.completed_at is not None
                and self.output is not None
                and self.error is None
                and bool(self.stop_reason)
            )
        else:
            valid = self.completed_at is not None and self.output is None and self.error is not None
        if not valid:
            raise ValueError("agent run fields do not match its status")


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    id: UUID
    run_id: UUID
    tool_use_id: str
    sequence_number: int
    tool_name: str
    status: ToolCallStatus
    requested_at: datetime
    completed_at: datetime
    arguments: dict[str, Any]
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        _validate_text(self.tool_use_id, self.tool_name)
        if len(self.tool_use_id) > 64 or len(self.tool_name) > 64:
            raise ValueError("tool identifiers cannot exceed 64 characters")
        if self.sequence_number <= 0:
            raise ValueError("sequence_number must be positive")
        _validate_timestamp(self.requested_at, "requested_at")
        _validate_timestamp(self.completed_at, "completed_at")
        if self.completed_at < self.requested_at:
            raise ValueError("completed_at cannot be earlier than requested_at")
        if self.status is ToolCallStatus.SUCCEEDED:
            valid = self.result is not None and self.error is None
        else:
            valid = self.result is None and self.error is not None
        if not valid:
            raise ValueError("tool call fields do not match its status")


def _validate_text(*values: str) -> None:
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError("text fields cannot be empty")


def _validate_timestamp(value: datetime, name: str) -> None:
    if value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
