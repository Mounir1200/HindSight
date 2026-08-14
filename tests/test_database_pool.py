from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from hindsight.infrastructure.database import (
    DATABASE_POOL_MAX_LIFETIME_ENV,
    DATABASE_POOL_MAX_SIZE_ENV,
    DATABASE_POOL_MIN_SIZE_ENV,
    DATABASE_POOL_TIMEOUT_ENV,
    DEFAULT_DATABASE_POOL_MAX_LIFETIME_SECONDS,
    DEFAULT_DATABASE_POOL_MAX_SIZE,
    DEFAULT_DATABASE_POOL_MIN_SIZE,
    DEFAULT_DATABASE_POOL_TIMEOUT_SECONDS,
    VALIDATE_DATABASE_CONNECTION_SQL,
    CockroachDatabasePool,
    DatabasePoolConfig,
)


class FakeResult:
    def __init__(self, row: object) -> None:
        self._row = row

    def fetchone(self) -> object:
        return self._row


class FakeConnection:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.validation_row: object = {"healthy": 1}

    def execute(self, statement: str) -> FakeResult:
        self.executed.append(statement)
        return FakeResult(self.validation_row)


class FakeCheckout:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.entered = False
        self.exited = False

    def __enter__(self) -> FakeConnection:
        self.entered = True
        return self.connection

    def __exit__(self, *_args: object) -> None:
        self.exited = True


class FakeConnectionPool:
    def __init__(self, **arguments: object) -> None:
        self.arguments = arguments
        self.connection_value = FakeConnection()
        self.checkouts: list[FakeCheckout] = []
        self.connection_timeouts: list[float] = []
        self.open_calls: list[tuple[bool, float]] = []
        self.close_calls: list[float] = []

    def open(self, *, wait: bool, timeout: float) -> None:
        self.open_calls.append((wait, timeout))

    def close(self, *, timeout: float) -> None:
        self.close_calls.append(timeout)

    def connection(self, *, timeout: float) -> FakeCheckout:
        self.connection_timeouts.append(timeout)
        checkout = FakeCheckout(self.connection_value)
        self.checkouts.append(checkout)
        return checkout


def _recording_factory() -> tuple[Callable[..., FakeConnectionPool], list[FakeConnectionPool]]:
    pools: list[FakeConnectionPool] = []

    def factory(**arguments: object) -> FakeConnectionPool:
        pool = FakeConnectionPool(**arguments)
        pools.append(pool)
        return pool

    return factory, pools


def test_pool_config_defaults_are_lazy_and_conservative() -> None:
    config = DatabasePoolConfig.from_environment({})

    assert config == DatabasePoolConfig(
        min_size=DEFAULT_DATABASE_POOL_MIN_SIZE,
        max_size=DEFAULT_DATABASE_POOL_MAX_SIZE,
        timeout_seconds=DEFAULT_DATABASE_POOL_TIMEOUT_SECONDS,
        max_lifetime_seconds=DEFAULT_DATABASE_POOL_MAX_LIFETIME_SECONDS,
    )
    assert config.min_size == 0


def test_pool_config_reads_all_bounded_environment_values() -> None:
    config = DatabasePoolConfig.from_environment(
        {
            DATABASE_POOL_MIN_SIZE_ENV: "2",
            DATABASE_POOL_MAX_SIZE_ENV: "12",
            DATABASE_POOL_TIMEOUT_ENV: "4.5",
            DATABASE_POOL_MAX_LIFETIME_ENV: "1800",
        }
    )

    assert config == DatabasePoolConfig(
        min_size=2,
        max_size=12,
        timeout_seconds=4.5,
        max_lifetime_seconds=1800.0,
    )


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        ({DATABASE_POOL_MIN_SIZE_ENV: "not-an-int"}, "must be an integer"),
        ({DATABASE_POOL_MIN_SIZE_ENV: "-1"}, "must be between 0 and 20"),
        ({DATABASE_POOL_MAX_SIZE_ENV: "0"}, "must be between 1 and 50"),
        (
            {DATABASE_POOL_MIN_SIZE_ENV: "6", DATABASE_POOL_MAX_SIZE_ENV: "5"},
            "cannot exceed",
        ),
        ({DATABASE_POOL_TIMEOUT_ENV: "0"}, "must be between 0.1 and 30.0"),
        ({DATABASE_POOL_TIMEOUT_ENV: "nan"}, "must be between 0.1 and 30.0"),
        (
            {DATABASE_POOL_MAX_LIFETIME_ENV: "59"},
            "must be between 60.0 and 86400.0",
        ),
    ],
)
def test_pool_config_rejects_invalid_or_unbounded_environment_values(
    environment: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        DatabasePoolConfig.from_environment(environment)


@pytest.mark.parametrize(
    "database_url",
    ["", "   ", " postgresql://configured", "postgresql://configured "],
)
def test_pool_rejects_empty_or_ambiguous_database_urls(database_url: str) -> None:
    factory, _pools = _recording_factory()

    with pytest.raises(ValueError, match="database_url"):
        CockroachDatabasePool(database_url, pool_factory=factory)


def test_construction_configures_a_closed_validating_psycopg_pool() -> None:
    factory, pools = _recording_factory()
    config = DatabasePoolConfig(
        min_size=1,
        max_size=7,
        timeout_seconds=3.5,
        max_lifetime_seconds=600,
    )

    database_pool = CockroachDatabasePool(
        "postgresql://configured",
        config=config,
        pool_factory=factory,
    )

    assert database_pool.is_open is False
    assert len(pools) == 1
    arguments = pools[0].arguments
    assert arguments["conninfo"] == "postgresql://configured"
    assert arguments["open"] is False
    assert arguments["min_size"] == 1
    assert arguments["max_size"] == 7
    assert arguments["timeout"] == 3.5
    assert arguments["max_lifetime"] == 600
    assert arguments["check"] is ConnectionPool.check_connection
    assert arguments["kwargs"] == {
        "autocommit": True,
        "row_factory": dict_row,
        "connect_timeout": 5,
        "application_name": "hindsight",
    }
    assert pools[0].open_calls == []
    assert pools[0].checkouts == []


def test_open_and_close_are_explicit_and_idempotent() -> None:
    factory, pools = _recording_factory()
    database_pool = CockroachDatabasePool(
        "postgresql://configured",
        config=DatabasePoolConfig(timeout_seconds=4),
        pool_factory=factory,
    )

    database_pool.open(wait=True)
    database_pool.open(wait=True)

    assert database_pool.is_open is True
    assert pools[0].open_calls == [(True, 4)]

    database_pool.close()
    database_pool.close()

    assert database_pool.is_open is False
    assert pools[0].close_calls == [4]
    with pytest.raises(RuntimeError, match="cannot be reopened"):
        database_pool.open()


def test_checkout_requires_open_pool_and_always_returns_the_connection() -> None:
    factory, pools = _recording_factory()
    database_pool = CockroachDatabasePool(
        "postgresql://configured",
        config=DatabasePoolConfig(timeout_seconds=2.5),
        pool_factory=factory,
    )

    with pytest.raises(RuntimeError, match="must be opened"), database_pool.checkout():
        pass

    database_pool.open()
    with database_pool.checkout() as connection:
        assert connection is pools[0].connection_value
        assert pools[0].checkouts[0].entered is True
        assert pools[0].checkouts[0].exited is False

    assert pools[0].connection_timeouts == [2.5]
    assert pools[0].checkouts[0].exited is True

    database_pool.close()
    with pytest.raises(RuntimeError, match="is closed"), database_pool.checkout():
        pass


def test_validate_executes_one_bounded_health_query() -> None:
    factory, pools = _recording_factory()
    database_pool = CockroachDatabasePool(
        "postgresql://configured",
        pool_factory=factory,
    )
    database_pool.open()

    database_pool.validate()

    assert pools[0].connection_value.executed == [VALIDATE_DATABASE_CONNECTION_SQL]
    assert pools[0].checkouts[0].exited is True


@pytest.mark.parametrize("row", [None, {}, {"healthy": 0}, (1,)])
def test_validate_rejects_an_unexpected_health_result(row: Any) -> None:
    factory, pools = _recording_factory()
    database_pool = CockroachDatabasePool(
        "postgresql://configured",
        pool_factory=factory,
    )
    pools[0].connection_value.validation_row = row
    database_pool.open()

    with pytest.raises(RuntimeError, match="unexpected result"):
        database_pool.validate()
