from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from math import ceil
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RateLimitBucket:
    policy_id: str
    principal_hash: str
    limit: int
    window_seconds: int
    burst: int
    cost: int = 1
    shared: bool = True

    def __post_init__(self) -> None:
        if not self.policy_id or len(self.policy_id) > 64:
            raise ValueError("rate-limit policy_id must contain 1 to 64 characters")
        if len(self.principal_hash) != 64:
            raise ValueError("rate-limit principal_hash must contain 64 characters")
        if any(value <= 0 for value in (self.limit, self.window_seconds, self.burst, self.cost)):
            raise ValueError("rate-limit values must be positive")
        if self.cost > self.burst:
            raise ValueError("rate-limit cost cannot exceed burst capacity")

    @property
    def store_key(self) -> str:
        return f"{self.policy_id}:{self.principal_hash}"

    @property
    def refill_per_second(self) -> Decimal:
        return Decimal(self.limit) / Decimal(self.window_seconds)


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    policy_id: str
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int
    reset_after_seconds: int

    def __post_init__(self) -> None:
        if self.limit <= 0:
            raise ValueError("rate-limit result limit must be positive")
        if self.remaining < 0:
            raise ValueError("rate-limit remaining cannot be negative")
        if self.retry_after_seconds < 0 or self.reset_after_seconds < 0:
            raise ValueError("rate-limit delays cannot be negative")


@dataclass(frozen=True, slots=True)
class TokenBucketState:
    tokens: Decimal
    updated_at: Decimal


@dataclass(frozen=True, slots=True)
class RateLimitLease:
    lease_key: str
    slot: int
    holder_hash: str

    def __post_init__(self) -> None:
        if not self.lease_key or len(self.lease_key) > 64:
            raise ValueError("rate-limit lease_key must contain 1 to 64 characters")
        if self.slot < 0:
            raise ValueError("rate-limit lease slot cannot be negative")
        if len(self.holder_hash) != 64:
            raise ValueError("rate-limit lease holder_hash must contain 64 characters")


class RateLimitStore(Protocol):
    def open(self) -> None: ...

    def close(self) -> None: ...

    def consume(
        self,
        buckets: tuple[RateLimitBucket, ...],
        now: float,
    ) -> tuple[RateLimitResult, ...]: ...

    def acquire_lease(
        self,
        lease_key: str,
        holder_hash: str,
        slots: int,
        ttl_seconds: int,
        now: float,
    ) -> RateLimitLease | None: ...

    def release_lease(self, lease: RateLimitLease) -> None: ...


class RateLimitStoreError(RuntimeError):
    pass


def evaluate_buckets(
    buckets: tuple[RateLimitBucket, ...],
    states: tuple[TokenBucketState, ...],
    now: float,
) -> tuple[tuple[RateLimitResult, ...], tuple[TokenBucketState, ...]]:
    if len(buckets) != len(states):
        raise ValueError("rate-limit buckets and states must have the same length")
    if len({bucket.store_key for bucket in buckets}) != len(buckets):
        raise ValueError("rate-limit bucket keys must be unique")

    evaluated: list[tuple[RateLimitBucket, Decimal, Decimal]] = []
    now_decimal = Decimal(str(now))
    all_allowed = True
    for bucket, state in zip(buckets, states, strict=True):
        effective_now = max(now_decimal, state.updated_at)
        elapsed = effective_now - state.updated_at
        available = min(
            Decimal(bucket.burst),
            state.tokens + elapsed * bucket.refill_per_second,
        )
        if available < bucket.cost:
            all_allowed = False
        evaluated.append((bucket, available, effective_now))

    results: list[RateLimitResult] = []
    next_states: list[TokenBucketState] = []
    for bucket, available, effective_now in evaluated:
        tokens = available - bucket.cost if all_allowed else available
        missing_for_request = max(Decimal(0), Decimal(bucket.cost) - available)
        retry_after = (
            ceil(missing_for_request / bucket.refill_per_second) if missing_for_request > 0 else 0
        )
        missing_for_reset = max(Decimal(0), Decimal(bucket.burst) - tokens)
        reset_after = (
            ceil(missing_for_reset / bucket.refill_per_second) if missing_for_reset > 0 else 0
        )
        results.append(
            RateLimitResult(
                policy_id=bucket.policy_id,
                allowed=available >= bucket.cost,
                limit=max(1, bucket.limit // bucket.cost),
                remaining=max(0, int(tokens // bucket.cost)),
                retry_after_seconds=retry_after,
                reset_after_seconds=reset_after,
            )
        )
        next_states.append(TokenBucketState(tokens=tokens, updated_at=effective_now))
    return tuple(results), tuple(next_states)
