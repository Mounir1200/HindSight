from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import Any
from uuid import UUID

import pytest

from hindsight.infrastructure.demo_workspaces import (
    CLAIM_WORKSPACE_SQL,
    MAX_PAYLOAD_BYTES,
    CockroachDemoWorkspaceRepository,
    DemoWorkspaceBusyError,
    DemoWorkspaceConflictError,
    DemoWorkspaceState,
    InMemoryDemoWorkspaceRepository,
)

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 14, 10, 0, tzinfo=UTC)
TOKEN = UUID("f2a695ad-9d35-4f22-bc40-4e9970d08186")
OTHER_TOKEN = UUID("f26fcdba-b3a8-4214-a2f6-0e1f81ea79b5")


def _row(
    state: str,
    *,
    payload: dict[str, Any] | str | None = None,
    version: int = 1,
    token: UUID | None = None,
) -> dict[str, Any]:
    return {
        "workspace_id": "showcase",
        "state": state,
        "payload": {} if payload is None else payload,
        "version": version,
        "lease_token": token,
        "lease_expires_at": NOW + timedelta(minutes=5) if state == "running" else None,
        "created_at": NOW,
        "updated_at": NOW + timedelta(seconds=version - 1),
    }


class _Cursor:
    def __init__(self, row: object) -> None:
        self._row = row

    def fetchone(self) -> object:
        return self._row


class _Transaction:
    def __init__(self, connection: _Connection) -> None:
        self._connection = connection

    def __enter__(self) -> _Transaction:
        self._connection.transactions += 1
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class _Connection:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.transactions = 0
        self.entries = 0

    def __enter__(self) -> _Connection:
        self.entries += 1
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def transaction(self) -> _Transaction:
        return _Transaction(self)

    def execute(self, sql: str, params: tuple[object, ...]) -> _Cursor:
        self.calls.append((sql, params))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return _Cursor(outcome)


def test_migration_bounds_payload_and_enforces_state_lease_invariants() -> None:
    migration = (ROOT / "migrations" / "012_demo_workspaces.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS demo_workspaces" in migration
    assert "jsonb_typeof(payload) = 'object'" in migration
    assert "octet_length(payload::STRING) <= 64000" in migration
    assert "state <> 'empty' OR payload = '{}'::JSONB" in migration
    states = ("'empty'", "'prepared'", "'running'", "'completed'")
    assert all(state in migration for state in states)
    assert "demo_workspaces_lease_state" in migration
    assert "lease_expires_at > updated_at" in migration
    assert "WHERE state = 'running'" in migration


def test_prepare_inserts_a_parameterized_isolated_workspace() -> None:
    connection = _Connection([_row("prepared", payload={"reported_incidents": []})])
    repository = CockroachDemoWorkspaceRepository(lambda: connection)

    workspace = repository.prepare("showcase", {"reported_incidents": []})

    assert workspace.state is DemoWorkspaceState.PREPARED
    assert connection.transactions == 1
    assert len(connection.calls) == 1
    sql, params = connection.calls[0]
    assert "%s" in sql
    assert "showcase" not in sql
    assert params[0] == "showcase"
    assert params[1] == '{"reported_incidents":[]}'


def test_atomic_claim_is_scoped_by_workspace_and_reclaims_only_expired_leases() -> None:
    claimed = _row("running", payload={"incident": "demo"}, version=2, token=TOKEN)
    connection = _Connection([claimed])
    repository = CockroachDemoWorkspaceRepository(lambda: connection)

    workspace = repository.claim("showcase", TOKEN, lease_seconds=45)

    assert workspace is not None
    assert workspace.lease_token == TOKEN
    assert connection.calls == [
        (CLAIM_WORKSPACE_SQL, (TOKEN, 45, "showcase", TOKEN)),
    ]
    assert "state = 'prepared'" in CLAIM_WORKSPACE_SQL
    assert "lease_expires_at <= now()" in CLAIM_WORKSPACE_SQL
    assert "lease_token IS DISTINCT FROM %s" in CLAIM_WORKSPACE_SQL


def test_claim_replay_returns_the_same_lease_without_mutating_it_again() -> None:
    running = _row("running", payload={"incident": "demo"}, version=2, token=TOKEN)
    connection = _Connection([None, running])
    repository = CockroachDemoWorkspaceRepository(lambda: connection)

    replay = repository.claim("showcase", TOKEN)

    assert replay == repository_record(running)
    assert len(connection.calls) == 2


def test_another_replica_cannot_claim_an_active_workspace() -> None:
    running = _row("running", payload={"incident": "demo"}, version=2, token=TOKEN)
    connection = _Connection([None, running])
    repository = CockroachDemoWorkspaceRepository(lambda: connection)

    assert repository.claim("showcase", OTHER_TOKEN) is None


def test_completion_is_idempotent_for_the_same_token_and_payload() -> None:
    payload = {"reported_incidents": [], "past_audits": [{"id": "audit-1"}]}
    completed = _row("completed", payload=payload, version=3, token=TOKEN)
    connection = _Connection([None, completed])
    repository = CockroachDemoWorkspaceRepository(lambda: connection)

    replay = repository.complete("showcase", TOKEN, payload)

    assert replay.state is DemoWorkspaceState.COMPLETED
    assert replay.payload == payload
    assert connection.calls[0][1] == (
        '{"past_audits":[{"id":"audit-1"}],"reported_incidents":[]}',
        "showcase",
        TOKEN,
    )


def test_completion_replay_rejects_a_different_payload() -> None:
    completed = _row("completed", payload={"result": "first"}, version=3, token=TOKEN)
    connection = _Connection([None, completed])
    repository = CockroachDemoWorkspaceRepository(lambda: connection)

    with pytest.raises(DemoWorkspaceConflictError, match="different payload"):
        repository.complete("showcase", TOKEN, {"result": "changed"})


def test_restore_after_failure_is_scoped_to_the_claim_token() -> None:
    running = _row("running", payload={"incident": "demo"}, version=2, token=OTHER_TOKEN)
    connection = _Connection([None, running])
    repository = CockroachDemoWorkspaceRepository(lambda: connection)

    with pytest.raises(DemoWorkspaceConflictError, match="no longer owned"):
        repository.restore_after_failure("showcase", TOKEN)


def test_reset_rejects_an_active_run_and_never_uses_an_unbounded_delete() -> None:
    running = _row("running", payload={"incident": "demo"}, version=2, token=TOKEN)
    connection = _Connection([None, None, running])
    repository = CockroachDemoWorkspaceRepository(lambda: connection)

    with pytest.raises(DemoWorkspaceBusyError, match="is running"):
        repository.reset("showcase")

    assert all("DELETE" not in sql for sql, _params in connection.calls)
    assert all(params[0] == "showcase" for _sql, params in connection.calls)


def test_payload_limit_is_checked_before_opening_a_connection() -> None:
    factory_calls = 0

    def factory() -> _Connection:
        nonlocal factory_calls
        factory_calls += 1
        return _Connection([])

    repository = CockroachDemoWorkspaceRepository(factory)

    with pytest.raises(ValueError, match="byte limit"):
        repository.prepare("showcase", {"value": "x" * MAX_PAYLOAD_BYTES})

    assert factory_calls == 0


def test_serialization_failure_retries_with_a_fresh_connection(monkeypatch) -> None:
    import hindsight.infrastructure.demo_workspaces as module

    first = _Connection([_SerializationFailure()])
    second = _Connection([_row("prepared", payload={"incident": "demo"})])
    connections = iter((first, second))
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    repository = CockroachDemoWorkspaceRepository(lambda: next(connections))

    workspace = repository.prepare("showcase", {"incident": "demo"})

    assert workspace.state is DemoWorkspaceState.PREPARED
    assert first.entries == second.entries == 1
    assert first.transactions == second.transactions == 1


def repository_record(row: dict[str, Any]):
    connection = _Connection([row])
    return CockroachDemoWorkspaceRepository(lambda: connection).read("showcase")


class _SerializationFailure(Exception):
    sqlstate = "40001"


class _Clock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, *, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def test_in_memory_workspace_supports_an_idempotent_lifecycle() -> None:
    clock = _Clock()
    repository = InMemoryDemoWorkspaceRepository(clock=clock)
    payload = {"incident": {"id": "case-1"}}

    assert repository.read("showcase") is None
    prepared = repository.prepare("showcase", payload)
    payload["incident"]["id"] = "mutated"
    prepared.payload["incident"]["id"] = "also-mutated"
    replayed_prepare = repository.prepare("showcase", {"incident": {"id": "case-1"}})

    assert replayed_prepare.version == 1
    assert replayed_prepare.payload == {"incident": {"id": "case-1"}}

    claimed = repository.claim("showcase", TOKEN, lease_seconds=30)
    replayed_claim = repository.claim("showcase", TOKEN, lease_seconds=60)

    assert claimed is not None
    assert replayed_claim == claimed
    assert claimed.version == 2
    assert claimed.lease_expires_at == NOW + timedelta(seconds=30)
    assert repository.claim("showcase", OTHER_TOKEN) is None

    result = {"past_audits": [{"id": "audit-1"}]}
    completed = repository.complete("showcase", TOKEN, result)
    replayed_completion = repository.complete("showcase", TOKEN, result)

    assert completed == replayed_completion
    assert completed.state is DemoWorkspaceState.COMPLETED
    assert completed.version == 3
    assert completed.lease_token == TOKEN
    assert completed.lease_expires_at is None


def test_in_memory_expired_lease_is_reclaimed_and_workspaces_stay_isolated() -> None:
    clock = _Clock()
    repository = InMemoryDemoWorkspaceRepository(clock=clock)
    repository.prepare("workspace-a", {"incident": "a"})
    workspace_b = repository.prepare("workspace-b", {"incident": "b"})
    first_claim = repository.claim("workspace-a", TOKEN, lease_seconds=10)
    assert first_claim is not None

    clock.advance(seconds=11)
    reclaimed = repository.claim("workspace-a", OTHER_TOKEN, lease_seconds=20)

    assert reclaimed is not None
    assert reclaimed.version == 3
    assert reclaimed.lease_token == OTHER_TOKEN
    assert repository.read("workspace-b") == workspace_b
    with pytest.raises(DemoWorkspaceConflictError, match="no longer owned"):
        repository.complete("workspace-a", TOKEN, {"result": "stale"})


def test_in_memory_restore_and_reset_match_durable_transitions() -> None:
    clock = _Clock()
    repository = InMemoryDemoWorkspaceRepository(clock=clock)
    repository.prepare("showcase", {"incident": "demo"})
    repository.claim("showcase", TOKEN, lease_seconds=10)

    with pytest.raises(DemoWorkspaceConflictError, match="no longer owned"):
        repository.restore("showcase", OTHER_TOKEN)

    restored = repository.restore_after_failure("showcase", TOKEN)
    replayed_restore = repository.restore("showcase", TOKEN)
    assert restored == replayed_restore
    assert restored.state is DemoWorkspaceState.PREPARED
    assert restored.version == 3

    repository.claim("showcase", TOKEN, lease_seconds=10)
    with pytest.raises(DemoWorkspaceBusyError, match="is running"):
        repository.reset("showcase")

    clock.advance(seconds=11)
    empty = repository.reset("showcase")
    replayed_reset = repository.reset("showcase")
    assert empty == replayed_reset
    assert empty.state is DemoWorkspaceState.EMPTY
    assert empty.payload == {}
    assert empty.version == 5


def test_in_memory_claim_allows_only_one_concurrent_owner() -> None:
    repository = InMemoryDemoWorkspaceRepository(clock=_Clock())
    repository.prepare("showcase", {"incident": "demo"})
    barrier = Barrier(3)

    def claim(token: UUID):
        barrier.wait()
        return repository.claim("showcase", token)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(claim, TOKEN)
        second = executor.submit(claim, OTHER_TOKEN)
        barrier.wait()
        results = (first.result(timeout=1), second.result(timeout=1))

    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert winners[0].lease_token in {TOKEN, OTHER_TOKEN}
    assert repository.read("showcase") == winners[0]


def test_in_memory_validates_payload_and_clock_before_mutation() -> None:
    repository = InMemoryDemoWorkspaceRepository(clock=lambda: datetime(2026, 8, 14))

    with pytest.raises(ValueError, match="byte limit"):
        repository.prepare("showcase", {"value": "x" * MAX_PAYLOAD_BYTES})
    with pytest.raises(ValueError, match="timezone-aware"):
        repository.reset("showcase")

    assert repository.read("showcase") is None
