from __future__ import annotations

from decimal import Decimal

import pytest

from hindsight.core.rate_limits import (
    RateLimitBucket,
    RateLimitLease,
    RateLimitResult,
    RateLimitStoreError,
)
from hindsight.infrastructure.rate_limits import InMemoryTokenBucketStore
from hindsight.web.provider_admission import (
    admit_provider_operation,
    provider_admission_response,
    release_provider_lease,
)
from hindsight.web.rate_limit import (
    RateLimitConfig,
    RateLimiter,
    RateLimitLeaseRule,
    RateLimitRule,
)

OPERATION = "memory-search-provider"


class OrderedStore:
    """Records the order in which the limiter touches budgets and leases."""

    def __init__(self, *, failing: bool = False) -> None:
        self._inner = InMemoryTokenBucketStore()
        self._failing = failing
        self.events: list[str] = []

    def open(self) -> None:
        self._inner.open()

    def close(self) -> None:
        self._inner.close()

    def consume(
        self,
        buckets: tuple[RateLimitBucket, ...],
        now: float,
    ) -> tuple[RateLimitResult, ...]:
        self.events.append("budget")
        if self._failing:
            raise RateLimitStoreError("simulated budget backend failure")
        return self._inner.consume(buckets, now)

    def acquire_lease(
        self,
        lease_key: str,
        holder_hash: str,
        slots: int,
        ttl_seconds: int,
        now: float,
    ) -> RateLimitLease | None:
        self.events.append("lease")
        return self._inner.acquire_lease(lease_key, holder_hash, slots, ttl_seconds, now)

    def release_lease(self, lease: RateLimitLease) -> None:
        self.events.append("release")
        self._inner.release_lease(lease)


class FailingReleaseStore(OrderedStore):
    def release_lease(self, lease: RateLimitLease) -> None:
        raise RateLimitStoreError("simulated release failure")


class FailingLeaseStore(OrderedStore):
    def acquire_lease(
        self,
        lease_key: str,
        holder_hash: str,
        slots: int,
        ttl_seconds: int,
        now: float,
    ) -> RateLimitLease | None:
        self.events.append("lease")
        raise RateLimitStoreError("simulated lease backend failure")


class SingleSlotPolicy:
    def __init__(self, *, budget: int = 1, slots: int = 1) -> None:
        self._budget = budget
        self._slots = slots

    def rules(self, method: str, path: str) -> tuple[RateLimitRule, ...]:
        return ()

    def operation_rules(self, operation: str) -> tuple[RateLimitRule, ...]:
        return (
            RateLimitRule(
                policy_id="test-budget",
                limit=self._budget,
                window_seconds=3_600,
                burst=self._budget,
            ),
        )

    def lease_rule(self, operation: str) -> RateLimitLeaseRule | None:
        return RateLimitLeaseRule(
            lease_key="provider-concurrency",
            slots=self._slots,
            ttl_seconds=60,
        )


def _limiter(store: OrderedStore, policy: SingleSlotPolicy) -> RateLimiter:
    config = RateLimitConfig(
        enabled=True,
        backend="memory",
        hmac_key=b"k" * 32,
        trusted_proxy_hops=0,
        trusted_proxy_networks=(),
        scale=Decimal(1),
        max_local_buckets=1_000,
    )
    return RateLimiter(config, store, policy=policy, clock=lambda: 0)


def test_concurrency_is_reserved_before_budget_is_charged() -> None:
    store = OrderedStore()
    limiter = _limiter(store, SingleSlotPolicy())

    admission = admit_provider_operation(limiter, OPERATION, "203.0.113.5")

    assert admission.granted is True
    assert admission.lease is not None
    assert store.events == ["lease", "budget"]


def test_a_caller_over_budget_releases_its_concurrency_slot() -> None:
    store = OrderedStore()
    limiter = _limiter(store, SingleSlotPolicy())
    first = admit_provider_operation(limiter, OPERATION, "203.0.113.5")
    release_provider_lease(limiter, first.lease, "correlation")
    store.events.clear()

    denied = admit_provider_operation(limiter, OPERATION, "203.0.113.5")

    assert denied.granted is False
    assert denied.unavailable is False
    assert denied.policy_id == "test-budget"
    assert store.events == ["lease", "budget", "release"]


def test_exhausted_concurrency_returns_429_with_retry_headers() -> None:
    store = OrderedStore()
    limiter = _limiter(store, SingleSlotPolicy(budget=10, slots=1))
    admit_provider_operation(limiter, OPERATION, "203.0.113.5")

    denied = admit_provider_operation(limiter, OPERATION, "198.51.100.6")
    response = provider_admission_response(denied, "correlation")

    assert denied.granted is False
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "5"
    assert response.headers["Cache-Control"] == "no-store"


def test_exhausted_concurrency_does_not_consume_provider_budget() -> None:
    store = OrderedStore()
    limiter = _limiter(store, SingleSlotPolicy(budget=1, slots=1))
    first = admit_provider_operation(limiter, OPERATION, "203.0.113.5")

    denied = admit_provider_operation(limiter, OPERATION, "198.51.100.6")
    release_provider_lease(limiter, first.lease, "correlation")
    retry = admit_provider_operation(limiter, OPERATION, "198.51.100.6")

    assert denied.granted is False
    assert retry.granted is True


def test_an_unavailable_lease_backend_does_not_touch_provider_budget() -> None:
    store = FailingLeaseStore()
    limiter = _limiter(store, SingleSlotPolicy())

    denied = admit_provider_operation(limiter, OPERATION, "203.0.113.5")

    assert denied.unavailable is True
    assert store.events == ["lease"]


def test_an_unavailable_backend_fails_closed_with_503() -> None:
    store = OrderedStore(failing=True)
    limiter = _limiter(store, SingleSlotPolicy())

    denied = admit_provider_operation(limiter, OPERATION, "203.0.113.5")
    response = provider_admission_response(denied, "correlation")

    assert denied.unavailable is True
    assert store.events == ["lease", "budget", "release"]
    assert response.status_code == 503
    assert response.headers["Cache-Control"] == "no-store"


def test_a_failed_release_is_logged_without_failing_the_request() -> None:
    store = FailingReleaseStore()
    limiter = _limiter(store, SingleSlotPolicy())
    admission = admit_provider_operation(limiter, OPERATION, "203.0.113.5")

    release_provider_lease(limiter, admission.lease, "correlation")


def test_a_granted_admission_has_no_denial_response() -> None:
    store = OrderedStore()
    limiter = _limiter(store, SingleSlotPolicy())
    admission = admit_provider_operation(limiter, OPERATION, "203.0.113.5")

    with pytest.raises(ValueError):
        provider_admission_response(admission, "correlation")
