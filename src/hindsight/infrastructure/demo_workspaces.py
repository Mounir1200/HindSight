from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from threading import Lock
from typing import Any
from uuid import UUID

from hindsight.infrastructure.database import DatabaseCapacityError

MAX_WORKSPACE_ID_LENGTH = 128
# The migration caps the stored payload at 64000 bytes, measured on the JSONB value
# rendered back to text. CockroachDB renders JSONB with a space after every ':' and
# ',', so that rendering is larger than the compact encoding checked here. Half the
# stored cap absorbs that expansion for any object shape.
MAX_PAYLOAD_BYTES = 32_000
MAX_LEASE_SECONDS = 3_600

_WORKSPACE_COLUMNS = """
workspace_id, state, payload, version, lease_token, lease_expires_at,
created_at, updated_at
"""

READ_WORKSPACE_SQL = f"""
SELECT {_WORKSPACE_COLUMNS}
FROM demo_workspaces
WHERE workspace_id = %s
"""

INSERT_PREPARED_WORKSPACE_SQL = f"""
INSERT INTO demo_workspaces (workspace_id, state, payload)
VALUES (%s, 'prepared', CAST(%s AS JSONB))
ON CONFLICT (workspace_id) DO NOTHING
RETURNING {_WORKSPACE_COLUMNS}
"""

PREPARE_WORKSPACE_SQL = f"""
UPDATE demo_workspaces
SET state = 'prepared',
    payload = CAST(%s AS JSONB),
    version = version + 1,
    lease_token = NULL,
    lease_expires_at = NULL,
    updated_at = now()
WHERE workspace_id = %s
  AND (state <> 'running' OR lease_expires_at <= now())
  AND NOT (state = 'prepared' AND payload = CAST(%s AS JSONB))
RETURNING {_WORKSPACE_COLUMNS}
"""

CLAIM_WORKSPACE_SQL = f"""
UPDATE demo_workspaces
SET state = 'running',
    version = version + 1,
    lease_token = %s,
    lease_expires_at = now() + CAST(%s AS INT8) * INTERVAL '1 second',
    updated_at = now()
WHERE workspace_id = %s
  AND lease_token IS DISTINCT FROM %s
  AND (
    state = 'prepared'
    OR (state = 'running' AND lease_expires_at <= now())
  )
RETURNING {_WORKSPACE_COLUMNS}
"""

COMPLETE_WORKSPACE_SQL = f"""
UPDATE demo_workspaces
SET state = 'completed',
    payload = CAST(%s AS JSONB),
    version = version + 1,
    lease_expires_at = NULL,
    updated_at = now()
WHERE workspace_id = %s
  AND state = 'running'
  AND lease_token = %s
RETURNING {_WORKSPACE_COLUMNS}
"""

RESTORE_WORKSPACE_SQL = f"""
UPDATE demo_workspaces
SET state = 'prepared',
    version = version + 1,
    lease_token = NULL,
    lease_expires_at = NULL,
    updated_at = now()
WHERE workspace_id = %s
  AND state = 'running'
  AND lease_token = %s
RETURNING {_WORKSPACE_COLUMNS}
"""

INSERT_EMPTY_WORKSPACE_SQL = f"""
INSERT INTO demo_workspaces (workspace_id, state, payload)
VALUES (%s, 'empty', '{{}}'::JSONB)
ON CONFLICT (workspace_id) DO NOTHING
RETURNING {_WORKSPACE_COLUMNS}
"""

RESET_WORKSPACE_SQL = f"""
UPDATE demo_workspaces
SET state = 'empty',
    payload = '{{}}'::JSONB,
    version = version + 1,
    lease_token = NULL,
    lease_expires_at = NULL,
    updated_at = now()
WHERE workspace_id = %s
  AND state <> 'empty'
  AND (state <> 'running' OR lease_expires_at <= now())
RETURNING {_WORKSPACE_COLUMNS}
"""

_RETRYABLE_SQLSTATES = frozenset({"40001", "40003"})


class DemoWorkspaceState(StrEnum):
    EMPTY = "empty"
    PREPARED = "prepared"
    RUNNING = "running"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class DemoWorkspace:
    workspace_id: str
    state: DemoWorkspaceState
    payload: dict[str, Any]
    version: int
    lease_token: UUID | None
    lease_expires_at: datetime | None
    created_at: datetime
    updated_at: datetime


class DemoWorkspaceError(RuntimeError):
    """Base error for durable demo workspace operations."""


class DemoWorkspaceBusyError(DemoWorkspaceError):
    """Raised when an active workspace lease prevents a transition."""


class DemoWorkspaceNotFoundError(DemoWorkspaceError):
    """Raised when a transition targets an unknown workspace."""


class DemoWorkspaceConflictError(DemoWorkspaceError):
    """Raised when a caller no longer owns the workspace execution."""


class DemoWorkspaceStoreError(DemoWorkspaceError):
    """Raised when CockroachDB cannot complete a workspace operation."""


class InMemoryDemoWorkspaceRepository:
    """Thread-safe demo workspace store with the durable repository semantics."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        self._clock = clock or _utc_now
        self._workspaces: dict[str, DemoWorkspace] = {}
        self._lock = Lock()

    def read(self, workspace_id: str) -> DemoWorkspace | None:
        workspace_id = _validate_workspace_id(workspace_id)
        with self._lock:
            current = self._workspaces.get(workspace_id)
            return None if current is None else _clone_workspace(current)

    def prepare(
        self,
        workspace_id: str,
        payload: Mapping[str, Any],
    ) -> DemoWorkspace:
        workspace_id = _validate_workspace_id(workspace_id)
        _encoded, normalized = _encode_payload(payload)
        with self._lock:
            now = self._now()
            current = self._workspaces.get(workspace_id)
            if current is None:
                prepared = DemoWorkspace(
                    workspace_id=workspace_id,
                    state=DemoWorkspaceState.PREPARED,
                    payload=normalized,
                    version=1,
                    lease_token=None,
                    lease_expires_at=None,
                    created_at=now,
                    updated_at=now,
                )
            else:
                if current.state is DemoWorkspaceState.RUNNING and _lease_is_active(current, now):
                    raise DemoWorkspaceBusyError(f"workspace {workspace_id!r} is running")
                if current.state is DemoWorkspaceState.PREPARED and current.payload == normalized:
                    return _clone_workspace(current)
                prepared = replace(
                    current,
                    state=DemoWorkspaceState.PREPARED,
                    payload=normalized,
                    version=current.version + 1,
                    lease_token=None,
                    lease_expires_at=None,
                    updated_at=_transition_time(current, now),
                )
            self._workspaces[workspace_id] = prepared
            return _clone_workspace(prepared)

    def claim(
        self,
        workspace_id: str,
        lease_token: UUID,
        *,
        lease_seconds: int = 300,
    ) -> DemoWorkspace | None:
        workspace_id = _validate_workspace_id(workspace_id)
        lease_token = _validate_lease_token(lease_token)
        if not 1 <= lease_seconds <= MAX_LEASE_SECONDS:
            raise ValueError(f"lease_seconds must be between 1 and {MAX_LEASE_SECONDS}")
        with self._lock:
            now = self._now()
            current = self._workspaces.get(workspace_id)
            if current is None:
                return None
            if current.state is DemoWorkspaceState.RUNNING and current.lease_token == lease_token:
                return _clone_workspace(current)
            can_claim = current.state is DemoWorkspaceState.PREPARED or (
                current.state is DemoWorkspaceState.RUNNING and not _lease_is_active(current, now)
            )
            if not can_claim:
                return None
            updated_at = _transition_time(current, now)
            claimed = replace(
                current,
                state=DemoWorkspaceState.RUNNING,
                version=current.version + 1,
                lease_token=lease_token,
                lease_expires_at=updated_at + timedelta(seconds=lease_seconds),
                updated_at=updated_at,
            )
            self._workspaces[workspace_id] = claimed
            return _clone_workspace(claimed)

    def complete(
        self,
        workspace_id: str,
        lease_token: UUID,
        payload: Mapping[str, Any],
    ) -> DemoWorkspace:
        workspace_id = _validate_workspace_id(workspace_id)
        lease_token = _validate_lease_token(lease_token)
        _encoded, normalized = _encode_payload(payload)
        with self._lock:
            current = self._workspaces.get(workspace_id)
            if current is None:
                raise DemoWorkspaceNotFoundError(f"workspace {workspace_id!r} was not found")
            if current.state is DemoWorkspaceState.COMPLETED and current.lease_token == lease_token:
                if current.payload != normalized:
                    raise DemoWorkspaceConflictError(
                        "completion token already refers to a different payload"
                    )
                return _clone_workspace(current)
            if (
                current.state is not DemoWorkspaceState.RUNNING
                or current.lease_token != lease_token
            ):
                raise DemoWorkspaceConflictError("workspace execution lease is no longer owned")
            completed = replace(
                current,
                state=DemoWorkspaceState.COMPLETED,
                payload=normalized,
                version=current.version + 1,
                lease_expires_at=None,
                updated_at=_transition_time(current, self._now()),
            )
            self._workspaces[workspace_id] = completed
            return _clone_workspace(completed)

    def restore_after_failure(
        self,
        workspace_id: str,
        lease_token: UUID,
    ) -> DemoWorkspace:
        workspace_id = _validate_workspace_id(workspace_id)
        lease_token = _validate_lease_token(lease_token)
        with self._lock:
            current = self._workspaces.get(workspace_id)
            if current is None:
                raise DemoWorkspaceNotFoundError(f"workspace {workspace_id!r} was not found")
            if current.state is DemoWorkspaceState.PREPARED:
                return _clone_workspace(current)
            if (
                current.state is not DemoWorkspaceState.RUNNING
                or current.lease_token != lease_token
            ):
                raise DemoWorkspaceConflictError("workspace execution lease is no longer owned")
            prepared = replace(
                current,
                state=DemoWorkspaceState.PREPARED,
                version=current.version + 1,
                lease_token=None,
                lease_expires_at=None,
                updated_at=_transition_time(current, self._now()),
            )
            self._workspaces[workspace_id] = prepared
            return _clone_workspace(prepared)

    def restore(self, workspace_id: str, lease_token: UUID) -> DemoWorkspace:
        return self.restore_after_failure(workspace_id, lease_token)

    def reset(self, workspace_id: str) -> DemoWorkspace:
        workspace_id = _validate_workspace_id(workspace_id)
        with self._lock:
            now = self._now()
            current = self._workspaces.get(workspace_id)
            if current is None:
                empty = DemoWorkspace(
                    workspace_id=workspace_id,
                    state=DemoWorkspaceState.EMPTY,
                    payload={},
                    version=1,
                    lease_token=None,
                    lease_expires_at=None,
                    created_at=now,
                    updated_at=now,
                )
            else:
                if current.state is DemoWorkspaceState.EMPTY:
                    return _clone_workspace(current)
                if current.state is DemoWorkspaceState.RUNNING and _lease_is_active(current, now):
                    raise DemoWorkspaceBusyError(f"workspace {workspace_id!r} is running")
                empty = replace(
                    current,
                    state=DemoWorkspaceState.EMPTY,
                    payload={},
                    version=current.version + 1,
                    lease_token=None,
                    lease_expires_at=None,
                    updated_at=_transition_time(current, now),
                )
            self._workspaces[workspace_id] = empty
            return _clone_workspace(empty)

    def _now(self) -> datetime:
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return now


class CockroachDemoWorkspaceRepository:
    """Short-lived, serializable operations for a multi-replica demo workspace.

    ``connection_factory`` must return a context-managed connection configured with
    mapping rows (for example ``psycopg.rows.dict_row``). A fresh connection is
    requested for every retry, so it can be backed by either ``connect_database``
    or ``ConnectionPool.connection`` without retaining a connection while an agent
    or provider is running.
    """

    def __init__(
        self,
        connection_factory: Callable[[], Any],
        *,
        max_retries: int = 3,
    ) -> None:
        if not callable(connection_factory):
            raise TypeError("connection_factory must be callable")
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        self._connection_factory = connection_factory
        self._max_retries = max_retries

    def read(self, workspace_id: str) -> DemoWorkspace | None:
        workspace_id = _validate_workspace_id(workspace_id)
        try:
            with self._connection_factory() as connection:
                return _fetch_workspace(connection, workspace_id)
        except (DemoWorkspaceError, DatabaseCapacityError):
            raise
        except Exception as error:
            raise DemoWorkspaceStoreError("demo workspace could not be read") from error

    def prepare(
        self,
        workspace_id: str,
        payload: Mapping[str, Any],
    ) -> DemoWorkspace:
        workspace_id = _validate_workspace_id(workspace_id)
        encoded, normalized = _encode_payload(payload)

        def operation(connection: Any) -> DemoWorkspace:
            inserted = connection.execute(
                INSERT_PREPARED_WORKSPACE_SQL,
                (workspace_id, encoded),
            ).fetchone()
            if inserted is not None:
                return _workspace_from_row(inserted)

            updated = connection.execute(
                PREPARE_WORKSPACE_SQL,
                (encoded, workspace_id, encoded),
            ).fetchone()
            if updated is not None:
                return _workspace_from_row(updated)

            current = _fetch_workspace(connection, workspace_id)
            if current is None:
                raise _RetryableWorkspaceRace
            if current.state is DemoWorkspaceState.PREPARED and current.payload == normalized:
                return current
            if current.state is DemoWorkspaceState.RUNNING:
                raise DemoWorkspaceBusyError(f"workspace {workspace_id!r} is running")
            raise _RetryableWorkspaceRace

        return self._write("prepare", operation)

    def claim(
        self,
        workspace_id: str,
        lease_token: UUID,
        *,
        lease_seconds: int = 300,
    ) -> DemoWorkspace | None:
        workspace_id = _validate_workspace_id(workspace_id)
        lease_token = _validate_lease_token(lease_token)
        if not 1 <= lease_seconds <= MAX_LEASE_SECONDS:
            raise ValueError(f"lease_seconds must be between 1 and {MAX_LEASE_SECONDS}")

        def operation(connection: Any) -> DemoWorkspace | None:
            updated = connection.execute(
                CLAIM_WORKSPACE_SQL,
                (lease_token, lease_seconds, workspace_id, lease_token),
            ).fetchone()
            if updated is not None:
                return _workspace_from_row(updated)

            current = _fetch_workspace(connection, workspace_id)
            if (
                current is not None
                and current.state is DemoWorkspaceState.RUNNING
                and current.lease_token == lease_token
            ):
                return current
            return None

        return self._write("claim", operation)

    def complete(
        self,
        workspace_id: str,
        lease_token: UUID,
        payload: Mapping[str, Any],
    ) -> DemoWorkspace:
        workspace_id = _validate_workspace_id(workspace_id)
        lease_token = _validate_lease_token(lease_token)
        encoded, normalized = _encode_payload(payload)

        def operation(connection: Any) -> DemoWorkspace:
            updated = connection.execute(
                COMPLETE_WORKSPACE_SQL,
                (encoded, workspace_id, lease_token),
            ).fetchone()
            if updated is not None:
                return _workspace_from_row(updated)

            current = _fetch_workspace(connection, workspace_id)
            if current is None:
                raise DemoWorkspaceNotFoundError(f"workspace {workspace_id!r} was not found")
            if current.state is DemoWorkspaceState.COMPLETED and current.lease_token == lease_token:
                if current.payload != normalized:
                    raise DemoWorkspaceConflictError(
                        "completion token already refers to a different payload"
                    )
                return current
            raise DemoWorkspaceConflictError("workspace execution lease is no longer owned")

        return self._write("complete", operation)

    def restore_after_failure(
        self,
        workspace_id: str,
        lease_token: UUID,
    ) -> DemoWorkspace:
        workspace_id = _validate_workspace_id(workspace_id)
        lease_token = _validate_lease_token(lease_token)

        def operation(connection: Any) -> DemoWorkspace:
            updated = connection.execute(
                RESTORE_WORKSPACE_SQL,
                (workspace_id, lease_token),
            ).fetchone()
            if updated is not None:
                return _workspace_from_row(updated)

            current = _fetch_workspace(connection, workspace_id)
            if current is None:
                raise DemoWorkspaceNotFoundError(f"workspace {workspace_id!r} was not found")
            if current.state is DemoWorkspaceState.PREPARED:
                return current
            raise DemoWorkspaceConflictError("workspace execution lease is no longer owned")

        return self._write("restore", operation)

    def restore(self, workspace_id: str, lease_token: UUID) -> DemoWorkspace:
        return self.restore_after_failure(workspace_id, lease_token)

    def reset(self, workspace_id: str) -> DemoWorkspace:
        workspace_id = _validate_workspace_id(workspace_id)

        def operation(connection: Any) -> DemoWorkspace:
            inserted = connection.execute(
                INSERT_EMPTY_WORKSPACE_SQL,
                (workspace_id,),
            ).fetchone()
            if inserted is not None:
                return _workspace_from_row(inserted)

            updated = connection.execute(
                RESET_WORKSPACE_SQL,
                (workspace_id,),
            ).fetchone()
            if updated is not None:
                return _workspace_from_row(updated)

            current = _fetch_workspace(connection, workspace_id)
            if current is None:
                raise _RetryableWorkspaceRace
            if current.state is DemoWorkspaceState.EMPTY:
                return current
            if current.state is DemoWorkspaceState.RUNNING:
                raise DemoWorkspaceBusyError(f"workspace {workspace_id!r} is running")
            raise _RetryableWorkspaceRace

        return self._write("reset", operation)

    def _write(self, label: str, operation: Callable[[Any], Any]) -> Any:
        for attempt in range(self._max_retries + 1):
            try:
                with self._connection_factory() as connection, connection.transaction():
                    return operation(connection)
            except (DemoWorkspaceError, DatabaseCapacityError):
                raise
            except Exception as error:
                retryable = isinstance(error, _RetryableWorkspaceRace) or (
                    getattr(error, "sqlstate", None) in _RETRYABLE_SQLSTATES
                )
                if retryable and attempt < self._max_retries:
                    _retry_delay(attempt)
                    continue
                raise DemoWorkspaceStoreError(f"demo workspace {label} operation failed") from error
        raise RuntimeError("unreachable demo workspace retry state")


class _RetryableWorkspaceRace(Exception):
    pass


def _fetch_workspace(connection: Any, workspace_id: str) -> DemoWorkspace | None:
    row = connection.execute(READ_WORKSPACE_SQL, (workspace_id,)).fetchone()
    return None if row is None else _workspace_from_row(row)


def _workspace_from_row(row: Mapping[str, Any]) -> DemoWorkspace:
    workspace = DemoWorkspace(
        workspace_id=str(row["workspace_id"]),
        state=DemoWorkspaceState(str(row["state"])),
        payload=_json_object(row["payload"]),
        version=int(row["version"]),
        lease_token=(None if row["lease_token"] is None else UUID(str(row["lease_token"]))),
        lease_expires_at=row["lease_expires_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
    _validate_persisted_workspace(workspace)
    return workspace


def _validate_persisted_workspace(workspace: DemoWorkspace) -> None:
    if workspace.version < 1 or workspace.updated_at < workspace.created_at:
        raise DemoWorkspaceStoreError("demo workspace row violates its version or time invariant")
    if workspace.state is DemoWorkspaceState.EMPTY and workspace.payload:
        raise DemoWorkspaceStoreError("empty demo workspace row contains a payload")
    if workspace.state is DemoWorkspaceState.RUNNING:
        valid_lease = (
            workspace.lease_token is not None
            and workspace.lease_expires_at is not None
            and workspace.lease_expires_at > workspace.updated_at
        )
    elif workspace.state is DemoWorkspaceState.COMPLETED:
        valid_lease = workspace.lease_token is not None and workspace.lease_expires_at is None
    else:
        valid_lease = workspace.lease_token is None and workspace.lease_expires_at is None
    if not valid_lease:
        raise DemoWorkspaceStoreError("demo workspace row violates its lease invariant")


def _validate_workspace_id(workspace_id: str) -> str:
    if not isinstance(workspace_id, str):
        raise TypeError("workspace_id must be a string")
    if not workspace_id or len(workspace_id) > MAX_WORKSPACE_ID_LENGTH:
        raise ValueError(f"workspace_id must contain 1 to {MAX_WORKSPACE_ID_LENGTH} characters")
    if any(character.isspace() or ord(character) < 32 for character in workspace_id):
        raise ValueError("workspace_id cannot contain whitespace or control characters")
    return workspace_id


def _validate_lease_token(lease_token: UUID) -> UUID:
    if not isinstance(lease_token, UUID):
        raise TypeError("lease_token must be a UUID")
    return lease_token


def _encode_payload(payload: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    if not isinstance(payload, Mapping):
        raise TypeError("demo workspace payload must be an object")
    try:
        encoded = json.dumps(payload, allow_nan=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as error:
        raise ValueError("demo workspace payload must be finite JSON") from error
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"demo workspace payload exceeds the {MAX_PAYLOAD_BYTES}-byte limit")
    return encoded, json.loads(encoded)


def _json_object(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise DemoWorkspaceStoreError("demo workspace payload is not a JSON object")
    return value


def _retry_delay(attempt: int) -> None:
    time.sleep(min(0.2, 0.025 * 2**attempt))


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _lease_is_active(workspace: DemoWorkspace, now: datetime) -> bool:
    return workspace.lease_expires_at is not None and workspace.lease_expires_at > now


def _transition_time(workspace: DemoWorkspace, now: datetime) -> datetime:
    return max(workspace.updated_at, now)


def _clone_workspace(workspace: DemoWorkspace) -> DemoWorkspace:
    return replace(workspace, payload=json.loads(json.dumps(workspace.payload)))
