from contextlib import contextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest

import hindsight.infrastructure.pooled_repositories as pooled


class CheckoutRecorder:
    def __init__(self) -> None:
        self.connection = object()
        self.entered = 0
        self.exited = 0

    @contextmanager
    def checkout(self):
        self.entered += 1
        try:
            yield self.connection
        finally:
            self.exited += 1


def test_assertion_repository_checks_out_for_each_delegated_call(monkeypatch) -> None:
    pool = CheckoutRecorder()
    calls = []

    class Repository:
        def __init__(self, connection, *, max_retries):
            assert connection is pool.connection
            assert max_retries == 2

        def current_truth(self, lookup):
            calls.append(lookup)
            return "assertion"

    monkeypatch.setattr(pooled, "CockroachAssertionRepository", Repository)
    repository = pooled.PooledAssertionRepository(pool.checkout, max_retries=2)

    assert repository.current_truth("lookup") == "assertion"
    assert calls == ["lookup"]
    assert (pool.entered, pool.exited) == (1, 1)


def test_checkout_returns_connection_when_delegate_raises(monkeypatch) -> None:
    pool = CheckoutRecorder()

    class Repository:
        def __init__(self, connection, *, max_retries):
            assert connection is pool.connection

        def get(self, decision_id):
            raise LookupError(decision_id)

    monkeypatch.setattr(pooled, "CockroachDecisionRepository", Repository)
    repository = pooled.PooledDecisionRepository(pool.checkout)

    with pytest.raises(LookupError):
        repository.get(uuid4())

    assert (pool.entered, pool.exited) == (1, 1)


@pytest.mark.parametrize(
    ("adapter", "implementation_name", "method", "arguments"),
    [
        (
            pooled.PooledTelecomRemediationRepository,
            "CockroachTelecomRemediationRepository",
            "apply_remediation",
            (object(),),
        ),
        (
            pooled.PooledAgentRunRepository,
            "CockroachAgentRunRepository",
            "start_run",
            (object(),),
        ),
        (
            pooled.PooledTelecomVectorMemoryStore,
            "CockroachTelecomVectorMemoryStore",
            "source",
            (uuid4(),),
        ),
    ],
)
def test_database_repositories_delegate_without_an_inline_recovery_checkout(
    monkeypatch,
    adapter,
    implementation_name,
    method,
    arguments,
) -> None:
    pool = CheckoutRecorder()
    result = object()
    captured = SimpleNamespace()

    class Repository:
        def __init__(self, connection, *, max_retries):
            captured.connection = connection
            captured.max_retries = max_retries

        def __getattr__(self, name):
            assert name == method
            return lambda *values: (setattr(captured, "arguments", values), result)[1]

    monkeypatch.setattr(pooled, implementation_name, Repository)
    repository = adapter(pool.checkout, max_retries=1)

    assert getattr(repository, method)(*arguments) is result
    assert captured.connection is pool.connection
    assert captured.max_retries == 1
    assert captured.arguments == arguments
    assert (pool.entered, pool.exited) == (1, 1)


@pytest.mark.parametrize(
    ("adapter", "implementation_name", "ambiguous_error", "method", "arguments"),
    [
        (
            pooled.PooledTelecomRemediationRepository,
            "CockroachTelecomRemediationRepository",
            pooled.RemediationAmbiguousCommitError,
            "apply_remediation",
            (object(),),
        ),
        (
            pooled.PooledAgentRunRepository,
            "CockroachAgentRunRepository",
            pooled.AgentRunAmbiguousCommitError,
            "start_run",
            (object(),),
        ),
        (
            pooled.PooledTelecomVectorMemoryStore,
            "CockroachTelecomVectorMemoryStore",
            pooled.VectorMemoryAmbiguousCommitError,
            "store",
            (object(), object(), "digest", object()),
        ),
    ],
)
def test_ambiguous_commit_releases_single_connection_before_fresh_replay(
    monkeypatch,
    adapter,
    implementation_name,
    ambiguous_error,
    method,
    arguments,
) -> None:
    result = object()
    events = []
    active_checkouts = 0
    connections = iter((object(), object()))

    @contextmanager
    def single_connection_checkout():
        nonlocal active_checkouts
        assert active_checkouts == 0, "recovery attempted a nested checkout"
        connection = next(connections)
        active_checkouts += 1
        events.append(("enter", connection))
        try:
            yield connection
        finally:
            events.append(("exit", connection))
            active_checkouts -= 1

    class Repository:
        calls = 0

        def __init__(self, connection, *, max_retries):
            self.connection = connection
            assert max_retries == 1

        def __getattr__(self, name):
            assert name == method

            def operation(*values):
                assert values == arguments
                Repository.calls += 1
                if Repository.calls == 1:
                    raise ambiguous_error("commit outcome unknown")
                return result

            return operation

    monkeypatch.setattr(pooled, implementation_name, Repository)

    repository = adapter(single_connection_checkout, max_retries=1)

    assert getattr(repository, method)(*arguments) is result
    assert Repository.calls == 2
    assert [event for event, _connection in events] == ["enter", "exit", "enter", "exit"]
    assert events[0][1] is not events[2][1]
    assert active_checkouts == 0


def test_investigation_context_store_delegates_and_returns_connection(monkeypatch) -> None:
    pool = CheckoutRecorder()
    result = object()
    case_id = uuid4()
    context = {"case": "context"}

    class Store:
        def __init__(self, connection):
            assert connection is pool.connection

        def persist(self, actual_case_id, actual_context):
            assert (actual_case_id, actual_context) == (case_id, context)
            return result

    monkeypatch.setattr(pooled, "CockroachInvestigationContextStore", Store)

    assert pooled.PooledInvestigationContextStore(pool.checkout).persist(case_id, context) is result
    assert (pool.entered, pool.exited) == (1, 1)
