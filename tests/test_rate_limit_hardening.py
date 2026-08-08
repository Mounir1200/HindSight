from __future__ import annotations

import ipaddress
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from threading import Barrier, Lock

import pytest
from fastapi.testclient import TestClient

import hindsight.web.app as web_app_module
from hindsight.adapters.telecom.seed import PRIMARY_DEMO_CASE
from hindsight.core.rate_limits import (
    RateLimitBucket,
    RateLimitLease,
    RateLimitResult,
    RateLimitStoreError,
)
from hindsight.infrastructure.rate_limits import InMemoryTokenBucketStore
from hindsight.web.app import create_app
from hindsight.web.rate_limit import RateLimitConfig, RateLimiter, client_principal


class RecordingStore:
    def __init__(self) -> None:
        self._inner = InMemoryTokenBucketStore()
        self._lock = Lock()
        self.consumptions: list[tuple[str, ...]] = []
        self.lease_acquisitions: list[str] = []

    def open(self) -> None:
        self._inner.open()

    def close(self) -> None:
        self._inner.close()

    def consume(
        self,
        buckets: tuple[RateLimitBucket, ...],
        now: float,
    ) -> tuple[RateLimitResult, ...]:
        with self._lock:
            self.consumptions.append(tuple(bucket.policy_id for bucket in buckets))
        return self._inner.consume(buckets, now)

    def acquire_lease(
        self,
        lease_key: str,
        holder_hash: str,
        slots: int,
        ttl_seconds: int,
        now: float,
    ) -> RateLimitLease | None:
        with self._lock:
            self.lease_acquisitions.append(lease_key)
        return self._inner.acquire_lease(
            lease_key,
            holder_hash,
            slots,
            ttl_seconds,
            now,
        )

    def release_lease(self, lease: RateLimitLease) -> None:
        self._inner.release_lease(lease)

    def policy_ids(self) -> set[str]:
        with self._lock:
            return {policy_id for consumption in self.consumptions for policy_id in consumption}


class FailingVectorReader:
    vector_enabled = True

    def __init__(self) -> None:
        self.calls = 0

    def retrieve(self, _lookup: object) -> None:
        self.calls += 1
        raise RuntimeError("simulated provider failure")


class ProbeBackend:
    def __init__(self, failure: str) -> None:
        self._inner = InMemoryTokenBucketStore()
        self._failure = failure

    def open(self) -> None:
        self._inner.open()

    def close(self) -> None:
        self._inner.close()

    def consume(
        self,
        buckets: tuple[RateLimitBucket, ...],
        now: float,
    ) -> tuple[RateLimitResult, ...]:
        if self._failure == "bucket":
            raise RateLimitStoreError("simulated bucket backend failure")
        return self._inner.consume(buckets, now)

    def acquire_lease(
        self,
        lease_key: str,
        holder_hash: str,
        slots: int,
        ttl_seconds: int,
        now: float,
    ) -> RateLimitLease | None:
        if self._failure == "lease":
            raise RateLimitStoreError("simulated lease backend failure")
        return self._inner.acquire_lease(
            lease_key,
            holder_hash,
            slots,
            ttl_seconds,
            now,
        )

    def release_lease(self, lease: RateLimitLease) -> None:
        self._inner.release_lease(lease)


def _config(*, trust_app_runner_xff: bool = False) -> RateLimitConfig:
    return RateLimitConfig(
        enabled=True,
        backend="memory",
        hmac_key=b"k" * 32,
        trusted_proxy_hops=0,
        trusted_proxy_networks=(),
        scale=Decimal(1),
        max_local_buckets=1_000,
        trust_app_runner_xff=trust_app_runner_xff,
    )


def _search_params() -> dict[str, object]:
    now = datetime(2026, 7, 3, 1, tzinfo=UTC)
    return {
        "agent_id": "investigation_agent",
        "route": "FR->SN",
        "service_type": "voice",
        "symptom": "A corrected tariff arrived after billing.",
        "applicable_at": now.isoformat(),
        "known_at": now.isoformat(),
        "limit": 2,
    }


def _demo_payload() -> dict[str, object]:
    return {
        "backend": "in_memory",
        "decision": {
            "id": PRIMARY_DEMO_CASE.decision_id,
            "subject_id": PRIMARY_DEMO_CASE.call_id,
            "amount": Decimal("2.50"),
            "decided_at": datetime(2026, 7, 2, 12, 1, tzinfo=UTC),
            "investigated_at": datetime(2026, 7, 3, 0, 1, tzinfo=UTC),
        },
        "verdict": {
            "category": "wrong_not_knowable",
            "agent_fault": False,
            "knowledge_gap_seconds": 172_800,
            "root_cause": "delayed_tariff_ingestion",
        },
        "comparison": {"overcharge": Decimal("1.00"), "currency": "EUR"},
        "remediation": {"case_id": PRIMARY_DEMO_CASE.dispute_id},
        "learning_proof": {},
    }


def _holder(index: int) -> str:
    return f"{index:064x}"


def test_wrong_verbs_and_validation_errors_do_not_debit_provider_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RecordingStore()
    limiter = RateLimiter(_config(), store, clock=lambda: 0)
    reader = FailingVectorReader()
    monkeypatch.setattr(web_app_module, "build_memory_search_reader", lambda _url: reader)

    with TestClient(create_app(database_url="", rate_limiter=limiter)) as client:
        wrong_search_verb = client.post("/memories/search")
        wrong_seed_verb = client.get("/demo/seed")
        invalid_search = client.get("/memories/search")

        assert wrong_search_verb.status_code == 405
        assert wrong_seed_verb.status_code == 405
        assert invalid_search.status_code == 422
        assert not {
            "provider-budget-client",
            "provider-budget-global",
        }.intersection(store.policy_ids())
        assert store.lease_acquisitions == []
        assert reader.calls == 0

        valid_search = client.get("/memories/search", params=_search_params())

    assert valid_search.status_code == 503
    assert {
        "provider-budget-client",
        "provider-budget-global",
    }.issubset(store.policy_ids())
    assert store.lease_acquisitions == ["provider-concurrency"]
    assert reader.calls == 1
    leases_after_failure = [
        store.acquire_lease("provider-concurrency", _holder(index), 4, 60, 1)
        for index in range(1, 5)
    ]
    assert all(lease is not None for lease in leases_after_failure)


def test_structured_memory_search_does_not_use_provider_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RecordingStore()
    limiter = RateLimiter(_config(), store, clock=lambda: 0)
    reader = FailingVectorReader()
    reader.vector_enabled = False
    monkeypatch.setattr(web_app_module, "build_memory_search_reader", lambda _url: reader)

    with TestClient(create_app(database_url="", rate_limiter=limiter)) as client:
        response = client.get("/memories/search", params=_search_params())

    assert response.status_code == 503
    assert "provider-budget-client" not in store.policy_ids()
    assert "provider-budget-global" not in store.policy_ids()
    assert store.lease_acquisitions == []


def test_unprepared_seed_attempt_does_not_block_prepared_execution() -> None:
    calls = 0

    def runner() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return _demo_payload()

    limiter = RateLimiter(
        _config(),
        InMemoryTokenBucketStore(),
        clock=lambda: 0,
    )
    with TestClient(
        create_app(database_url="", demo_runner=runner, rate_limiter=limiter)
    ) as client:
        unprepared = client.post("/demo/seed")
        prepared = client.post("/demo/prepare")
        executed = client.post("/demo/seed")

    assert unprepared.status_code == 409
    assert prepared.status_code == 201
    assert executed.status_code == 200
    assert calls == 1


def test_in_memory_leases_enforce_slots_release_and_expiration() -> None:
    store = InMemoryTokenBucketStore()
    accepted = [
        store.acquire_lease("provider-concurrency", _holder(index), 4, 10, 0)
        for index in range(1, 5)
    ]

    assert all(lease is not None for lease in accepted)
    assert {lease.slot for lease in accepted if lease is not None} == {0, 1, 2, 3}
    assert store.acquire_lease("provider-concurrency", _holder(5), 4, 10, 0) is None

    released = accepted[2]
    assert released is not None
    store.release_lease(
        RateLimitLease(
            released.lease_key,
            released.slot,
            _holder(99),
        )
    )
    assert store.acquire_lease("provider-concurrency", _holder(6), 4, 10, 1) is None
    store.release_lease(released)
    replacement = store.acquire_lease("provider-concurrency", _holder(7), 4, 10, 1)

    assert replacement is not None
    assert replacement.slot == released.slot
    assert store.acquire_lease("provider-concurrency", _holder(8), 4, 10, 1) is None
    assert store.acquire_lease("provider-concurrency", _holder(9), 4, 10, 10) is not None


def test_in_memory_lease_acquisition_is_atomic_under_concurrency() -> None:
    store = InMemoryTokenBucketStore()
    workers = 12
    barrier = Barrier(workers)

    def acquire(index: int) -> RateLimitLease | None:
        barrier.wait(timeout=5)
        return store.acquire_lease(
            "provider-concurrency",
            _holder(index),
            4,
            60,
            0,
        )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        leases = list(executor.map(acquire, range(1, workers + 1)))

    acquired = [lease for lease in leases if lease is not None]
    assert len(acquired) == 4
    assert len({lease.slot for lease in acquired}) == 4


def test_app_runner_accepts_one_platform_ip_and_rejects_injected_chains() -> None:
    principal = client_principal(
        "10.0.0.5",
        ("203.0.113.7",),
        trusted_proxy_hops=0,
        trusted_proxy_networks=(),
        trust_app_runner_xff=True,
    )
    injected_chain = client_principal(
        "10.0.0.5",
        ("192.0.2.99, 203.0.113.7",),
        trusted_proxy_hops=0,
        trusted_proxy_networks=(),
        trust_app_runner_xff=True,
    )

    assert principal == "203.0.113.7"
    assert injected_chain == "10.0.0.5"


def test_ipv4_mapped_peer_is_recognized_as_a_trusted_proxy() -> None:
    principal = client_principal(
        "::ffff:10.1.2.3",
        ("203.0.113.7",),
        trusted_proxy_hops=1,
        trusted_proxy_networks=(ipaddress.ip_network("10.0.0.0/8"),),
    )

    assert principal == "203.0.113.7"


def test_regressive_clock_does_not_refill_or_rewind_a_bucket() -> None:
    store = InMemoryTokenBucketStore()
    bucket = RateLimitBucket(
        policy_id="clock-rollback",
        principal_hash=_holder(1),
        limit=1,
        window_seconds=60,
        burst=1,
    )

    assert store.consume((bucket,), 100)[0].allowed is True
    assert store.consume((bucket,), 90)[0].allowed is False
    assert store.consume((bucket,), 100)[0].allowed is False
    assert store.consume((bucket,), 160)[0].allowed is True


def test_health_rate_limit_is_isolated_per_client() -> None:
    limiter = RateLimiter(
        _config(),
        InMemoryTokenBucketStore(),
        clock=lambda: 0,
    )
    app = create_app(database_url="", rate_limiter=limiter)

    with (
        TestClient(app, client=("203.0.113.10", 50_000)) as noisy_client,
        TestClient(app, client=("198.51.100.20", 50_000)) as probe_client,
    ):
        accepted = [noisy_client.get("/health") for _ in range(20)]
        rejected = noisy_client.get("/health")
        isolated = probe_client.get("/health")

    assert all(response.status_code == 200 for response in accepted)
    assert rejected.status_code == 429
    assert isolated.status_code == 200


@pytest.mark.parametrize("failure", ["bucket", "lease"])
def test_readiness_fails_if_any_distributed_limiter_primitive_breaks(
    failure: str,
) -> None:
    limiter = RateLimiter(
        _config(),
        InMemoryTokenBucketStore(),
        shared_store=ProbeBackend(failure),
        clock=lambda: 0,
    )

    with TestClient(
        create_app(
            database_url="",
            health_probe=lambda: None,
            rate_limiter=limiter,
        )
    ) as client:
        response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"


def test_waf_normalizes_expensive_paths_and_returns_standard_429() -> None:
    template = (
        Path(__file__).resolve().parents[1] / "deploy" / "ecs-express-service.yaml"
    ).read_text(encoding="utf-8")

    for route in ("/demo/seed", "/memories/search", "/demo/reset"):
        marker = f"SearchString: {route}"
        route_start = template.index(marker)
        route_rule = template[route_start : route_start + 400]
        assert route_rule.index("Type: URL_DECODE") < route_rule.index("Type: NORMALIZE_PATH")

    assert template.count("ResponseCode: 429") >= 3
    assert "CustomResponseBodyKey: RateLimitExceeded" in template
