from __future__ import annotations

import ipaddress
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Lock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

import hindsight.infrastructure.rate_limits as rate_limit_store_module
import hindsight.web.rate_limit as web_rate_limit_module
from hindsight.core.rate_limits import RateLimitBucket, RateLimitStoreError
from hindsight.infrastructure.rate_limits import (
    CockroachTokenBucketStore,
    InMemoryTokenBucketStore,
)
from hindsight.web.app import create_app
from hindsight.web.rate_limit import (
    DefaultRateLimitPolicy,
    RateLimitConfig,
    RateLimiter,
    RateLimitRule,
    client_principal,
)

DECISION_ID = UUID("20000000-0000-0000-0000-000000000001")
PRINCIPAL = "a" * 64


class FixedPolicy:
    def __init__(
        self,
        *,
        limit: int = 1,
        burst: int = 1,
        shared: bool = False,
        scope: str = "client",
    ) -> None:
        self.limit = limit
        self.burst = burst
        self.shared = shared
        self.scope = scope

    def rules(self, method: str, path: str) -> tuple[RateLimitRule, ...]:
        if path == "/health":
            return ()
        return (
            RateLimitRule(
                policy_id="test-policy",
                limit=self.limit,
                window_seconds=60,
                burst=self.burst,
                shared=self.shared,
                scope=self.scope,
            ),
        )


class FailingStore:
    def open(self) -> None:
        return None

    def close(self) -> None:
        return None

    def consume(self, buckets, now):
        raise RateLimitStoreError("private limiter connection detail")


class AuthorizedResetPolicy:
    def rules(self, method: str, path: str) -> tuple[RateLimitRule, ...]:
        return ()

    def operation_rules(self, operation: str) -> tuple[RateLimitRule, ...]:
        if operation != "demo-reset-authorized":
            return ()
        return (
            RateLimitRule(
                policy_id="authorized-reset",
                limit=1,
                window_seconds=60,
                burst=1,
                scope="global",
                shared=False,
            ),
        )


def _config(
    *,
    backend: str = "memory",
    hmac_key: bytes = b"k" * 32,
) -> RateLimitConfig:
    return RateLimitConfig(
        enabled=True,
        backend=backend,
        hmac_key=hmac_key,
        trusted_proxy_hops=0,
        trusted_proxy_networks=(),
        scale=Decimal(1),
        max_local_buckets=1_000,
    )


def _bucket(
    policy_id: str = "test",
    *,
    limit: int = 3,
    window_seconds: int = 60,
    burst: int = 3,
    cost: int = 1,
) -> RateLimitBucket:
    return RateLimitBucket(
        policy_id=policy_id,
        principal_hash=PRINCIPAL,
        limit=limit,
        window_seconds=window_seconds,
        burst=burst,
        cost=cost,
    )


def test_token_bucket_refills_without_a_fixed_window_boundary_burst() -> None:
    store = InMemoryTokenBucketStore()
    bucket = _bucket()

    assert [store.consume((bucket,), 0)[0].allowed for _ in range(3)] == [True] * 3
    rejected = store.consume((bucket,), 0)[0]
    almost_refilled = store.consume((bucket,), 19.9)[0]
    refilled = store.consume((bucket,), 20)[0]

    assert rejected.allowed is False
    assert rejected.retry_after_seconds == 20
    assert almost_refilled.allowed is False
    assert almost_refilled.retry_after_seconds == 1
    assert refilled.allowed is True


def test_multi_bucket_consumption_is_atomic() -> None:
    store = InMemoryTokenBucketStore()
    first = _bucket("first", limit=1, burst=1)
    second = _bucket("second", limit=1, burst=1)
    assert store.consume((second,), 0)[0].allowed is True

    rejected = store.consume((first, second), 0)
    accepted_after_refill = store.consume((first, second), 60)

    assert [result.allowed for result in rejected] == [True, False]
    assert all(result.allowed for result in accepted_after_refill)


def test_weighted_budget_charges_an_expensive_operation_more_than_a_search() -> None:
    store = InMemoryTokenBucketStore()
    seed = _bucket(
        "provider-budget",
        limit=32,
        window_seconds=3_600,
        burst=8,
        cost=8,
    )
    search = _bucket(
        "provider-budget",
        limit=32,
        window_seconds=3_600,
        burst=8,
        cost=1,
    )

    assert store.consume((seed,), 0)[0].allowed is True
    rejected_search = store.consume((search,), 0)[0]
    later_search = store.consume((search,), 113)[0]

    assert rejected_search.allowed is False
    assert rejected_search.retry_after_seconds == 113
    assert later_search.allowed is True


def test_memory_store_is_bounded_under_principal_churn() -> None:
    store = InMemoryTokenBucketStore(max_buckets=3)
    for index in range(10):
        bucket = RateLimitBucket(
            policy_id="scan",
            principal_hash=f"{index:064x}",
            limit=1,
            window_seconds=60,
            burst=1,
        )
        store.consume((bucket,), 0)

    assert store.bucket_count == 3


def test_token_bucket_is_atomic_under_concurrency() -> None:
    store = InMemoryTokenBucketStore()
    bucket = _bucket(limit=5, burst=5)

    with ThreadPoolExecutor(max_workers=20) as executor:
        allowed = list(
            executor.map(
                lambda _index: store.consume((bucket,), 0)[0].allowed,
                range(20),
            )
        )

    assert allowed.count(True) == 5
    assert allowed.count(False) == 15


def test_cockroach_store_retries_only_bounded_serialization_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class DatabaseFailure(RuntimeError):
        def __init__(self, sqlstate: str) -> None:
            super().__init__(sqlstate)
            self.sqlstate = sqlstate

    store = object.__new__(CockroachTokenBucketStore)
    store._max_retries = 2
    attempts = 0
    expected = InMemoryTokenBucketStore().consume((_bucket(),), 0)

    def consume_once(_store, _buckets, _now):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise DatabaseFailure("40001")
        return expected

    monkeypatch.setattr(CockroachTokenBucketStore, "_consume_once", consume_once)
    monkeypatch.setattr(rate_limit_store_module, "_retry_delay", lambda _attempt: None)

    assert store.consume((_bucket(),), 0) == expected
    assert attempts == 3

    attempts = 0

    def ambiguous_commit(_store, _buckets, _now):
        nonlocal attempts
        attempts += 1
        raise DatabaseFailure("40003")

    monkeypatch.setattr(CockroachTokenBucketStore, "_consume_once", ambiguous_commit)
    with pytest.raises(RateLimitStoreError):
        store.consume((_bucket(),), 0)
    assert attempts == 1


def test_client_identity_ignores_untrusted_forwarded_headers_and_normalizes_ips() -> None:
    assert (
        client_principal(
            "203.0.113.7",
            ("198.51.100.4",),
            trusted_proxy_hops=0,
            trusted_proxy_networks=(),
        )
        == "203.0.113.7"
    )
    assert (
        client_principal(
            "::ffff:203.0.113.7",
            (),
            trusted_proxy_hops=0,
            trusted_proxy_networks=(),
        )
        == "203.0.113.7"
    )
    assert client_principal(
        "2001:db8:1:2::1",
        (),
        trusted_proxy_hops=0,
        trusted_proxy_networks=(),
    ) == client_principal(
        "2001:0db8:0001:0002:ffff::2",
        (),
        trusted_proxy_hops=0,
        trusted_proxy_networks=(),
    )


def test_trusted_proxy_chain_uses_the_bounded_rightmost_hops() -> None:
    networks = (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("fd00::/8"),
    )

    principal = client_principal(
        "10.0.0.2",
        ("192.0.2.99, 203.0.113.7, 10.0.0.3",),
        trusted_proxy_hops=2,
        trusted_proxy_networks=networks,
    )
    untrusted_chain = client_principal(
        "10.0.0.2",
        ("192.0.2.99, 203.0.113.7, 198.51.100.2",),
        trusted_proxy_hops=2,
        trusted_proxy_networks=networks,
    )

    assert principal == "203.0.113.7"
    assert untrusted_chain == "10.0.0.2"


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        (
            {
                "HINDSIGHT_RATE_LIMIT_BACKEND": "cockroach",
                "HINDSIGHT_RATE_LIMIT_HMAC_KEY": "short",
            },
            "at least 32 bytes",
        ),
        (
            {
                "HINDSIGHT_RATE_LIMIT_BACKEND": "memory",
                "HINDSIGHT_RATE_LIMIT_TRUSTED_PROXY_HOPS": "1",
            },
            "trusted proxy hops require",
        ),
        (
            {
                "HINDSIGHT_RATE_LIMIT_BACKEND": "memory",
                "HINDSIGHT_RATE_LIMIT_SCALE": "100",
            },
            "must be between 0.1 and 10",
        ),
        (
            {
                "HINDSIGHT_RATE_LIMIT_BACKEND": "memory",
                "HINDSIGHT_PROVIDER_CONCURRENCY": "0",
            },
            "must be between 1 and 100",
        ),
        (
            {
                "HINDSIGHT_RATE_LIMIT_BACKEND": "memory",
                "HINDSIGHT_RATE_LIMIT_TRUSTED_PROXY_HOPS": "1",
                "HINDSIGHT_RATE_LIMIT_TRUSTED_PROXY_CIDRS": "0.0.0.0/0",
            },
            "cannot cover an entire address family",
        ),
        (
            {
                "HINDSIGHT_RATE_LIMIT_BACKEND": "memory",
                "HINDSIGHT_RATE_LIMIT_TRUST_APP_RUNNER_XFF": "true",
                "HINDSIGHT_RATE_LIMIT_TRUSTED_PROXY_CIDRS": "10.0.0.0/8",
            },
            "cannot be combined",
        ),
    ],
)
def test_rate_limit_configuration_rejects_unsafe_values(
    environment: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        RateLimitConfig.from_environment("postgresql://runtime", environment)


def test_auto_backend_uses_cockroach_with_a_database_and_memory_without_one() -> None:
    hmac_key = "a" * 32

    durable = RateLimitConfig.from_environment(
        "postgresql://runtime",
        {
            "HINDSIGHT_RATE_LIMIT_BACKEND": "auto",
            "HINDSIGHT_RATE_LIMIT_HMAC_KEY": hmac_key,
        },
    )
    local = RateLimitConfig.from_environment(
        "",
        {"HINDSIGHT_RATE_LIMIT_BACKEND": "auto"},
    )

    assert durable.backend == "cockroach"
    assert local.backend == "memory"


def test_shared_rate_limit_pool_has_an_explicit_bounded_size(monkeypatch) -> None:
    created: dict[str, object] = {}

    class Store:
        def __init__(self, database_url: str, **options: object) -> None:
            created["database_url"] = database_url
            created.update(options)

    monkeypatch.setattr(web_rate_limit_module, "CockroachTokenBucketStore", Store)
    environment = {
        "HINDSIGHT_RATE_LIMIT_BACKEND": "cockroach",
        "HINDSIGHT_RATE_LIMIT_HMAC_KEY": "a" * 32,
        "HINDSIGHT_RATE_LIMIT_POOL_MAX_SIZE": "7",
    }

    web_rate_limit_module.build_rate_limiter(
        "postgresql://runtime",
        environment=environment,
    )

    assert created == {
        "database_url": "postgresql://runtime",
        "max_pool_size": 7,
    }
    environment["HINDSIGHT_RATE_LIMIT_POOL_MAX_SIZE"] = "21"
    with pytest.raises(ValueError, match="must be between 1 and 20"):
        web_rate_limit_module.build_rate_limiter(
            "postgresql://runtime",
            environment=environment,
        )


def test_default_policy_covers_fallback_dynamic_routes_and_provider_costs() -> None:
    policy = DefaultRateLimitPolicy(provider_concurrency=7)
    first_decision = policy.rules("GET", "/decisions/11111111-1111-1111-1111-111111111111")
    second_decision = policy.rules("GET", "/decisions/22222222-2222-2222-2222-222222222222")
    unknown_write = policy.rules("PATCH", "/future/resource/123")
    seed = policy.rules("POST", "/demo/seed/")
    seed_execution = policy.operation_rules("demo-seed-execution")
    seed_provider = policy.operation_rules("demo-seed-provider")
    workspace_lease = policy.lease_rule("demo-workspace-exclusive")
    reset_lease = policy.lease_rule("demo-reset-authorized")

    assert policy.rules("GET", "/health") == ()
    assert [rule.policy_id for rule in policy.rules("GET", "/ready")] == ["readiness-client"]
    assert all(rule.scope == "client" for rule in policy.rules("GET", "/ready"))
    assert [rule.policy_id for rule in first_decision] == [
        rule.policy_id for rule in second_decision
    ]
    assert "edge-fallback" in {rule.policy_id for rule in policy.rules("GET", "/random/a")}
    assert "api-mutation-client" in {rule.policy_id for rule in unknown_write}
    assert "demo-seed-attempt-client" in {rule.policy_id for rule in seed}
    provider = next(rule for rule in seed_provider if rule.policy_id == "provider-budget-global")
    global_seed = next(rule for rule in seed_execution if rule.policy_id == "demo-seed-global")
    assert provider.cost == 8
    assert provider.scope == "global"
    assert global_seed.burst == 1
    assert workspace_lease is not None
    assert reset_lease is not None
    assert workspace_lease.lease_key == reset_lease.lease_key == "demo-workspace-execution"
    assert workspace_lease.slots == reset_lease.slots == 1
    provider_lease = policy.lease_rule("demo-seed-provider")
    assert provider_lease is not None
    assert provider_lease.slots == 7


def test_middleware_returns_a_stable_429_without_calling_the_handler_again(caplog) -> None:
    calls = 0
    calls_lock = Lock()

    def reader(_decision_id: UUID) -> dict[str, object] | None:
        nonlocal calls
        with calls_lock:
            calls += 1
        return {
            "decision": {"id": DECISION_ID},
            "truth": {},
            "knowledge": {},
            "evidence": [],
            "verdict": {},
        }

    limiter = RateLimiter(
        _config(),
        InMemoryTokenBucketStore(),
        policy=FixedPolicy(),
        clock=lambda: 0,
    )
    with TestClient(
        create_app(database_url="", decision_reader=reader, rate_limiter=limiter)
    ) as client:
        accepted = client.get(f"/decisions/{DECISION_ID}")
        rejected = client.get(
            f"/decisions/{DECISION_ID}",
            headers={"X-Forwarded-For": "198.51.100.77"},
        )

    assert accepted.status_code == 200
    assert rejected.status_code == 429
    assert rejected.json() == {"detail": "rate_limit_exceeded"}
    assert rejected.headers["retry-after"] == "60"
    assert rejected.headers["ratelimit-limit"] == "1"
    assert rejected.headers["ratelimit-remaining"] == "0"
    assert rejected.headers["cache-control"] == "no-store"
    assert rejected.headers["x-correlation-id"]
    assert rejected.headers["x-content-type-options"] == "nosniff"
    assert calls == 1
    assert "198.51.100.77" not in caplog.text


def test_limiter_backend_failure_is_fail_closed_but_health_remains_available(caplog) -> None:
    calls = 0

    def reader(_decision_id: UUID) -> dict[str, object] | None:
        nonlocal calls
        calls += 1
        return None

    limiter = RateLimiter(
        _config(),
        FailingStore(),
        policy=FixedPolicy(),
        clock=lambda: 0,
    )
    with TestClient(
        create_app(database_url="", decision_reader=reader, rate_limiter=limiter)
    ) as client:
        health = client.get("/health")
        protected = client.get(f"/decisions/{DECISION_ID}")

    assert health.status_code == 200
    assert protected.status_code == 503
    assert protected.json()["detail"] == "rate_limit_unavailable"
    assert protected.headers["cache-control"] == "no-store"
    assert calls == 0
    assert "private limiter connection detail" not in caplog.text


def test_authentication_rejects_before_the_shared_limiter_is_touched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HINDSIGHT_API_KEY", "production-key-" + ("a" * 48))
    limiter = RateLimiter(
        _config(),
        FailingStore(),
        policy=FixedPolicy(),
        clock=lambda: 0,
    )

    with TestClient(create_app(database_url="", rate_limiter=limiter)) as client:
        response = client.get(f"/decisions/{DECISION_ID}")

    assert response.status_code == 401
    assert response.json() == {"detail": "authentication_required"}


def test_unknown_path_churn_uses_one_bounded_fallback_bucket() -> None:
    config = RateLimitConfig(
        enabled=True,
        backend="memory",
        hmac_key=b"k" * 32,
        trusted_proxy_hops=0,
        trusted_proxy_networks=(),
        scale=Decimal("0.1"),
        max_local_buckets=1_000,
    )
    limiter = RateLimiter(
        config,
        InMemoryTokenBucketStore(),
        clock=lambda: 0,
    )

    with TestClient(create_app(database_url="", rate_limiter=limiter)) as client:
        responses = [client.get(f"/unmapped/{index}?token=ignored-{index}") for index in range(4)]

    assert [response.status_code for response in responses] == [404, 404, 404, 429]


def test_shared_store_enforces_one_global_quota_across_app_instances() -> None:
    shared_store = InMemoryTokenBucketStore()
    policy = FixedPolicy(shared=True, scope="global")
    first_limiter = RateLimiter(
        _config(),
        InMemoryTokenBucketStore(),
        shared_store=shared_store,
        policy=policy,
        clock=lambda: 0,
    )
    second_limiter = RateLimiter(
        _config(),
        InMemoryTokenBucketStore(),
        shared_store=shared_store,
        policy=policy,
        clock=lambda: 0,
    )

    with (
        TestClient(create_app(database_url="", rate_limiter=first_limiter)) as first,
        TestClient(create_app(database_url="", rate_limiter=second_limiter)) as second,
    ):
        accepted = first.get("/")
        rejected = second.get("/")

    assert accepted.status_code == 200
    assert rejected.status_code == 429


def test_reset_global_quota_is_consumed_only_after_token_validation(monkeypatch) -> None:
    monkeypatch.setenv("HINDSIGHT_DEMO_RESET_TOKEN", "valid-reset-token")
    calls = 0

    def resetter() -> None:
        nonlocal calls
        calls += 1

    limiter = RateLimiter(
        _config(),
        InMemoryTokenBucketStore(),
        policy=AuthorizedResetPolicy(),
        clock=lambda: 0,
    )
    with TestClient(
        create_app(
            database_url="",
            demo_resetter=resetter,
            rate_limiter=limiter,
        )
    ) as client:
        denied = client.post(
            "/demo/reset",
            headers={"X-Demo-Reset-Token": "wrong"},
        )
        accepted = client.post(
            "/demo/reset",
            headers={"X-Demo-Reset-Token": "valid-reset-token"},
        )
        limited = client.post(
            "/demo/reset",
            headers={"X-Demo-Reset-Token": "valid-reset-token"},
        )

    assert denied.status_code == 403
    assert accepted.status_code == 200
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "60"
    assert calls == 1
