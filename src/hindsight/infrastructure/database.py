from __future__ import annotations

import math
import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout, TooManyRequests

from hindsight.telemetry import current_performance_span

DATABASE_POOL_MIN_SIZE_ENV = "HINDSIGHT_DATABASE_POOL_MIN_SIZE"
DATABASE_POOL_MAX_SIZE_ENV = "HINDSIGHT_DATABASE_POOL_MAX_SIZE"
DATABASE_POOL_TIMEOUT_ENV = "HINDSIGHT_DATABASE_POOL_TIMEOUT_SECONDS"
DATABASE_POOL_MAX_LIFETIME_ENV = "HINDSIGHT_DATABASE_POOL_MAX_LIFETIME_SECONDS"

DEFAULT_DATABASE_POOL_MIN_SIZE = 0
DEFAULT_DATABASE_POOL_MAX_SIZE = 5
DEFAULT_DATABASE_POOL_TIMEOUT_SECONDS = 2.0
DEFAULT_DATABASE_POOL_MAX_LIFETIME_SECONDS = 900.0

MIN_DATABASE_POOL_MIN_SIZE = 0
MAX_DATABASE_POOL_MIN_SIZE = 20
MIN_DATABASE_POOL_MAX_SIZE = 1
MAX_DATABASE_POOL_MAX_SIZE = 50
MIN_DATABASE_POOL_TIMEOUT_SECONDS = 0.1
MAX_DATABASE_POOL_TIMEOUT_SECONDS = 30.0
MIN_DATABASE_POOL_MAX_LIFETIME_SECONDS = 60.0
MAX_DATABASE_POOL_MAX_LIFETIME_SECONDS = 86_400.0

DATABASE_CONNECT_TIMEOUT_SECONDS = 5
DATABASE_POOL_MAX_WAITING = 100
VALIDATE_DATABASE_CONNECTION_SQL = "SELECT 1 AS healthy"

PoolFactory = Callable[..., Any]


class DatabaseCapacityError(RuntimeError):
    """Raised when no pooled connection became available within the checkout timeout.

    This is saturation, not failure: the caller should shed the request with a
    retryable status rather than report it as an internal error.
    """


@dataclass(frozen=True, slots=True)
class DatabasePoolConfig:
    """Bounded settings for the process-wide CockroachDB connection pool."""

    min_size: int = DEFAULT_DATABASE_POOL_MIN_SIZE
    max_size: int = DEFAULT_DATABASE_POOL_MAX_SIZE
    timeout_seconds: float = DEFAULT_DATABASE_POOL_TIMEOUT_SECONDS
    max_lifetime_seconds: float = DEFAULT_DATABASE_POOL_MAX_LIFETIME_SECONDS

    def __post_init__(self) -> None:
        _bounded_int(
            self.min_size,
            "min_size",
            MIN_DATABASE_POOL_MIN_SIZE,
            MAX_DATABASE_POOL_MIN_SIZE,
        )
        _bounded_int(
            self.max_size,
            "max_size",
            MIN_DATABASE_POOL_MAX_SIZE,
            MAX_DATABASE_POOL_MAX_SIZE,
        )
        _bounded_float(
            self.timeout_seconds,
            "timeout_seconds",
            MIN_DATABASE_POOL_TIMEOUT_SECONDS,
            MAX_DATABASE_POOL_TIMEOUT_SECONDS,
        )
        _bounded_float(
            self.max_lifetime_seconds,
            "max_lifetime_seconds",
            MIN_DATABASE_POOL_MAX_LIFETIME_SECONDS,
            MAX_DATABASE_POOL_MAX_LIFETIME_SECONDS,
        )
        if self.min_size > self.max_size:
            raise ValueError("min_size cannot exceed max_size")

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> DatabasePoolConfig:
        values = environment if environment is not None else os.environ
        return cls(
            min_size=_environment_int(
                values,
                DATABASE_POOL_MIN_SIZE_ENV,
                DEFAULT_DATABASE_POOL_MIN_SIZE,
                MIN_DATABASE_POOL_MIN_SIZE,
                MAX_DATABASE_POOL_MIN_SIZE,
            ),
            max_size=_environment_int(
                values,
                DATABASE_POOL_MAX_SIZE_ENV,
                DEFAULT_DATABASE_POOL_MAX_SIZE,
                MIN_DATABASE_POOL_MAX_SIZE,
                MAX_DATABASE_POOL_MAX_SIZE,
            ),
            timeout_seconds=_environment_float(
                values,
                DATABASE_POOL_TIMEOUT_ENV,
                DEFAULT_DATABASE_POOL_TIMEOUT_SECONDS,
                MIN_DATABASE_POOL_TIMEOUT_SECONDS,
                MAX_DATABASE_POOL_TIMEOUT_SECONDS,
            ),
            max_lifetime_seconds=_environment_float(
                values,
                DATABASE_POOL_MAX_LIFETIME_ENV,
                DEFAULT_DATABASE_POOL_MAX_LIFETIME_SECONDS,
                MIN_DATABASE_POOL_MAX_LIFETIME_SECONDS,
                MAX_DATABASE_POOL_MAX_LIFETIME_SECONDS,
            ),
        )


class CockroachDatabasePool:
    """Explicit-lifecycle pool shared by database-backed application services.

    Constructing this object never opens a connection. ``open()`` starts the
    psycopg pool, while the default ``min_size=0`` keeps actual connections lazy
    until the first checkout.
    """

    def __init__(
        self,
        database_url: str,
        *,
        config: DatabasePoolConfig | None = None,
        pool_factory: PoolFactory = ConnectionPool,
    ) -> None:
        self._database_url = _validated_database_url(database_url)
        self._config = config if config is not None else DatabasePoolConfig()
        if not isinstance(self._config, DatabasePoolConfig):
            raise TypeError("config must be a DatabasePoolConfig")
        if not callable(pool_factory):
            raise TypeError("pool_factory must be callable")

        self._pool = pool_factory(
            conninfo=self._database_url,
            min_size=self._config.min_size,
            max_size=self._config.max_size,
            timeout=self._config.timeout_seconds,
            max_lifetime=self._config.max_lifetime_seconds,
            max_waiting=DATABASE_POOL_MAX_WAITING,
            open=False,
            check=ConnectionPool.check_connection,
            name="hindsight-database",
            kwargs={
                "autocommit": True,
                "row_factory": dict_row,
                "connect_timeout": DATABASE_CONNECT_TIMEOUT_SECONDS,
                "application_name": "hindsight",
            },
        )
        self._state_lock = Lock()
        self._opened = False
        self._closed = False

    @property
    def config(self) -> DatabasePoolConfig:
        return self._config

    @property
    def is_open(self) -> bool:
        with self._state_lock:
            return self._opened and not self._closed

    def open(self, *, wait: bool = False) -> None:
        """Open the pool once, optionally waiting for its minimum capacity."""

        if not isinstance(wait, bool):
            raise TypeError("wait must be a boolean")
        with self._state_lock:
            if self._closed:
                raise RuntimeError("database pool cannot be reopened after close")
            if self._opened:
                return
            self._pool.open(wait=wait, timeout=self._config.timeout_seconds)
            self._opened = True

    def close(self) -> None:
        """Close the pool once; closing before ``open`` is safe."""

        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._pool.close(timeout=self._config.timeout_seconds)
            finally:
                self._opened = False

    @contextmanager
    def checkout(self) -> Iterator[Any]:
        """Borrow one validated connection and always return it to the pool."""

        self._require_open()
        try:
            with (
                current_performance_span(
                    component="cockroach",
                    operation="connection.checkout",
                ),
                self._pool.connection(timeout=self._config.timeout_seconds) as connection,
            ):
                yield connection
        except (PoolTimeout, TooManyRequests) as error:
            # Two distinct saturation signals: the checkout waited out its timeout, or
            # DATABASE_POOL_MAX_WAITING callers were already queued ahead of it.
            # TooManyRequests does not derive from PoolTimeout, so it needs naming.
            raise DatabaseCapacityError("the database pool could not admit the request") from error

    def validate(self) -> None:
        """Prove that the pool can execute a minimal query on a live connection."""

        with self.checkout() as connection:
            row = connection.execute(VALIDATE_DATABASE_CONNECTION_SQL).fetchone()
        if not isinstance(row, Mapping) or row.get("healthy") != 1:
            raise RuntimeError("database pool validation returned an unexpected result")

    def _require_open(self) -> None:
        with self._state_lock:
            if self._closed:
                raise RuntimeError("database pool is closed")
            if not self._opened:
                raise RuntimeError("database pool must be opened before checkout")


def connect_database(database_url: str) -> Any:
    return psycopg.connect(
        _validated_database_url(database_url),
        autocommit=True,
        row_factory=dict_row,
        connect_timeout=DATABASE_CONNECT_TIMEOUT_SECONDS,
        application_name="hindsight",
    )


def _validated_database_url(database_url: str) -> str:
    if not isinstance(database_url, str) or not database_url.strip():
        raise ValueError("database_url cannot be empty")
    if database_url != database_url.strip():
        raise ValueError("database_url cannot contain surrounding whitespace")
    return database_url


def _environment_int(
    environment: Mapping[str, str],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw_value = environment.get(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value.strip())
    except (AttributeError, ValueError) as error:
        raise ValueError(f"{name} must be an integer") from error
    return _bounded_int(value, name, minimum, maximum)


def _environment_float(
    environment: Mapping[str, str],
    name: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    raw_value = environment.get(name)
    if raw_value is None:
        return default
    try:
        value = float(raw_value.strip())
    except (AttributeError, ValueError) as error:
        raise ValueError(f"{name} must be a number") from error
    return _bounded_float(value, name, minimum, maximum)


def _bounded_int(value: object, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _bounded_float(value: object, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    parsed = float(value)
    if not math.isfinite(parsed) or not minimum <= parsed <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return parsed
