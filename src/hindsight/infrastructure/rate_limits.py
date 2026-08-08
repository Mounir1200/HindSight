from __future__ import annotations

import random
import time
from collections import OrderedDict
from datetime import UTC, timedelta
from decimal import Decimal
from threading import Lock

from psycopg.rows import tuple_row
from psycopg_pool import ConnectionPool

from hindsight.core.rate_limits import (
    RateLimitBucket,
    RateLimitLease,
    RateLimitResult,
    RateLimitStoreError,
    TokenBucketState,
    evaluate_buckets,
)

INSERT_BUCKET_SQL = """
INSERT INTO api_rate_limit_buckets (
  bucket_key, tokens, updated_at, expires_at
)
VALUES (%s, %s, %s, %s)
ON CONFLICT (bucket_key) DO NOTHING
"""

SELECT_BUCKET_SQL = """
SELECT tokens, updated_at
FROM api_rate_limit_buckets
WHERE bucket_key = %s
FOR UPDATE
"""

UPDATE_BUCKET_SQL = """
UPDATE api_rate_limit_buckets
SET tokens = %s,
    updated_at = %s,
    expires_at = %s
WHERE bucket_key = %s
"""

SELECT_DATABASE_TIME_SQL = "SELECT now()"

DELETE_EXPIRED_LEASES_SQL = """
DELETE FROM api_rate_limit_leases
WHERE lease_key = %s
  AND expires_at <= %s
"""

INSERT_LEASE_SQL = """
INSERT INTO api_rate_limit_leases (
  lease_key, slot, holder_hash, acquired_at, expires_at
)
VALUES (%s, %s, %s, %s, %s)
ON CONFLICT (lease_key, slot) DO NOTHING
RETURNING slot
"""

DELETE_LEASE_SQL = """
DELETE FROM api_rate_limit_leases
WHERE lease_key = %s
  AND slot = %s
  AND holder_hash = %s
"""


class InMemoryTokenBucketStore:
    def __init__(self, *, max_buckets: int = 50_000) -> None:
        if max_buckets < 1:
            raise ValueError("max_buckets must be positive")
        self._max_buckets = max_buckets
        self._states: OrderedDict[str, tuple[TokenBucketState, int]] = OrderedDict()
        self._leases: dict[tuple[str, int], tuple[str, float]] = {}
        self._lock = Lock()
        self._checks = 0

    def open(self) -> None:
        return None

    def close(self) -> None:
        return None

    def consume(
        self,
        buckets: tuple[RateLimitBucket, ...],
        now: float,
    ) -> tuple[RateLimitResult, ...]:
        if not buckets:
            return ()
        with self._lock:
            self._checks += 1
            if self._checks % 256 == 0:
                self._remove_expired(now)

            states: list[TokenBucketState] = []
            for bucket in buckets:
                existing = self._states.get(bucket.store_key)
                if existing is None:
                    states.append(
                        TokenBucketState(
                            tokens=Decimal(bucket.burst),
                            updated_at=Decimal(str(now)),
                        )
                    )
                else:
                    states.append(existing[0])

            results, next_states = evaluate_buckets(buckets, tuple(states), now)
            for bucket, state in zip(buckets, next_states, strict=True):
                self._states[bucket.store_key] = (state, bucket.window_seconds)
                self._states.move_to_end(bucket.store_key)
            self._trim()
            return results

    def acquire_lease(
        self,
        lease_key: str,
        holder_hash: str,
        slots: int,
        ttl_seconds: int,
        now: float,
    ) -> RateLimitLease | None:
        _validate_lease_request(lease_key, holder_hash, slots, ttl_seconds)
        with self._lock:
            expired = [
                key
                for key, (_holder, expires_at) in self._leases.items()
                if key[0] == lease_key and expires_at <= now
            ]
            for key in expired:
                self._leases.pop(key, None)
            for slot in range(slots):
                key = (lease_key, slot)
                if key not in self._leases:
                    self._leases[key] = (holder_hash, now + ttl_seconds)
                    return RateLimitLease(lease_key, slot, holder_hash)
        return None

    def release_lease(self, lease: RateLimitLease) -> None:
        with self._lock:
            key = (lease.lease_key, lease.slot)
            current = self._leases.get(key)
            if current is not None and current[0] == lease.holder_hash:
                self._leases.pop(key, None)

    @property
    def bucket_count(self) -> int:
        with self._lock:
            return len(self._states)

    def _remove_expired(self, now: float) -> None:
        now_decimal = Decimal(str(now))
        expired = [
            key
            for key, (state, window_seconds) in self._states.items()
            if now_decimal - state.updated_at > max(3_600, window_seconds * 2)
        ]
        for key in expired:
            self._states.pop(key, None)

    def _trim(self) -> None:
        while len(self._states) > self._max_buckets:
            self._states.popitem(last=False)


class CockroachTokenBucketStore:
    def __init__(
        self,
        database_url: str,
        *,
        max_pool_size: int = 10,
        pool_timeout_seconds: float = 2.0,
        max_retries: int = 3,
    ) -> None:
        if not database_url:
            raise ValueError("database_url is required")
        if max_pool_size < 1 or max_retries < 0 or pool_timeout_seconds <= 0:
            raise ValueError("invalid Cockroach rate-limit store configuration")
        self._pool_timeout_seconds = pool_timeout_seconds
        self._max_retries = max_retries
        self._pool = ConnectionPool(
            conninfo=database_url,
            min_size=0,
            max_size=max_pool_size,
            timeout=pool_timeout_seconds,
            max_waiting=100,
            open=False,
            kwargs={
                "autocommit": True,
                "connect_timeout": 5,
                "application_name": "hindsight-rate-limit",
                "row_factory": tuple_row,
            },
        )

    def open(self) -> None:
        self._pool.open(wait=False)

    def close(self) -> None:
        self._pool.close()

    def consume(
        self,
        buckets: tuple[RateLimitBucket, ...],
        now: float,
    ) -> tuple[RateLimitResult, ...]:
        if not buckets:
            return ()
        ordered = tuple(sorted(buckets, key=lambda bucket: bucket.store_key))
        for attempt in range(self._max_retries + 1):
            try:
                return self._consume_once(ordered, now)
            except Exception as error:
                if getattr(error, "sqlstate", None) == "40001" and attempt < self._max_retries:
                    _retry_delay(attempt)
                    continue
                raise RateLimitStoreError("distributed rate-limit store is unavailable") from error
        raise RateLimitStoreError("distributed rate-limit retry state is invalid")

    def acquire_lease(
        self,
        lease_key: str,
        holder_hash: str,
        slots: int,
        ttl_seconds: int,
        now: float,
    ) -> RateLimitLease | None:
        _validate_lease_request(lease_key, holder_hash, slots, ttl_seconds)
        for attempt in range(self._max_retries + 1):
            try:
                return self._acquire_lease_once(
                    lease_key,
                    holder_hash,
                    slots,
                    ttl_seconds,
                )
            except Exception as error:
                if getattr(error, "sqlstate", None) == "40001" and attempt < self._max_retries:
                    _retry_delay(attempt)
                    continue
                raise RateLimitStoreError(
                    "distributed concurrency limiter is unavailable"
                ) from error
        raise RateLimitStoreError("distributed lease retry state is invalid")

    def release_lease(self, lease: RateLimitLease) -> None:
        for attempt in range(self._max_retries + 1):
            try:
                with (
                    self._pool.connection(timeout=self._pool_timeout_seconds) as connection,
                    connection.transaction(),
                ):
                    connection.execute(
                        DELETE_LEASE_SQL,
                        (lease.lease_key, lease.slot, lease.holder_hash),
                    )
                return
            except Exception as error:
                if getattr(error, "sqlstate", None) == "40001" and attempt < self._max_retries:
                    _retry_delay(attempt)
                    continue
                raise RateLimitStoreError(
                    "distributed concurrency lease could not be released"
                ) from error
        raise RateLimitStoreError("distributed lease release retry state is invalid")

    def _consume_once(
        self,
        buckets: tuple[RateLimitBucket, ...],
        now: float,
    ) -> tuple[RateLimitResult, ...]:
        states: list[TokenBucketState] = []
        with (
            self._pool.connection(timeout=self._pool_timeout_seconds) as connection,
            connection.transaction(),
        ):
            database_time = connection.execute(SELECT_DATABASE_TIME_SQL).fetchone()
            if database_time is None:
                raise RateLimitStoreError("database time is unavailable")
            now_datetime = database_time[0]
            if now_datetime.tzinfo is None:
                now_datetime = now_datetime.replace(tzinfo=UTC)
            now = now_datetime.timestamp()
            for bucket in buckets:
                expires_at = now_datetime + timedelta(seconds=max(3_600, bucket.window_seconds * 2))
                connection.execute(
                    INSERT_BUCKET_SQL,
                    (
                        bucket.store_key,
                        Decimal(bucket.burst),
                        now_datetime,
                        expires_at,
                    ),
                )
                row = connection.execute(
                    SELECT_BUCKET_SQL,
                    (bucket.store_key,),
                ).fetchone()
                if row is None:
                    raise RateLimitStoreError("rate-limit bucket was not persisted")
                states.append(
                    TokenBucketState(
                        tokens=Decimal(str(row[0])),
                        updated_at=Decimal(str(row[1].timestamp())),
                    )
                )

            results, next_states = evaluate_buckets(buckets, tuple(states), now)
            for bucket, state in zip(buckets, next_states, strict=True):
                expires_at = now_datetime + timedelta(seconds=max(3_600, bucket.window_seconds * 2))
                connection.execute(
                    UPDATE_BUCKET_SQL,
                    (
                        state.tokens,
                        now_datetime,
                        expires_at,
                        bucket.store_key,
                    ),
                )
            return results

    def _acquire_lease_once(
        self,
        lease_key: str,
        holder_hash: str,
        slots: int,
        ttl_seconds: int,
    ) -> RateLimitLease | None:
        with (
            self._pool.connection(timeout=self._pool_timeout_seconds) as connection,
            connection.transaction(),
        ):
            database_time = connection.execute(SELECT_DATABASE_TIME_SQL).fetchone()
            if database_time is None:
                raise RateLimitStoreError("database time is unavailable")
            now_datetime = database_time[0]
            if now_datetime.tzinfo is None:
                now_datetime = now_datetime.replace(tzinfo=UTC)
            connection.execute(
                DELETE_EXPIRED_LEASES_SQL,
                (lease_key, now_datetime),
            )
            expires_at = now_datetime + timedelta(seconds=ttl_seconds)
            start_slot = int(holder_hash[:8], 16) % slots
            for offset in range(slots):
                slot = (start_slot + offset) % slots
                inserted = connection.execute(
                    INSERT_LEASE_SQL,
                    (
                        lease_key,
                        slot,
                        holder_hash,
                        now_datetime,
                        expires_at,
                    ),
                ).fetchone()
                if inserted is not None:
                    return RateLimitLease(lease_key, slot, holder_hash)
        return None


def _validate_lease_request(
    lease_key: str,
    holder_hash: str,
    slots: int,
    ttl_seconds: int,
) -> None:
    if not lease_key or len(lease_key) > 64:
        raise ValueError("lease_key must contain 1 to 64 characters")
    if len(holder_hash) != 64:
        raise ValueError("holder_hash must contain 64 characters")
    if not 1 <= slots <= 100 or not 1 <= ttl_seconds <= 3_600:
        raise ValueError("invalid concurrency lease limits")


def _retry_delay(attempt: int) -> None:
    delay = min(0.25, 0.025 * 2**attempt) + random.uniform(0, 0.005)
    time.sleep(delay)
