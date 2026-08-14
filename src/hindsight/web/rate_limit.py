from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Protocol

from hindsight.core.rate_limits import (
    RateLimitBucket,
    RateLimitLease,
    RateLimitResult,
    RateLimitStore,
    RateLimitStoreError,
)
from hindsight.infrastructure.rate_limits import (
    CockroachTokenBucketStore,
    InMemoryTokenBucketStore,
)

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off", ""})
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_MAX_FORWARDED_HOPS = 8
_RATE_LIMIT_POOL_MAX_SIZE_ENV = "HINDSIGHT_RATE_LIMIT_POOL_MAX_SIZE"
_DEFAULT_RATE_LIMIT_POOL_MAX_SIZE = 5
_MAX_RATE_LIMIT_POOL_MAX_SIZE = 20
_PROVIDER_CONCURRENCY_ENV = "HINDSIGHT_PROVIDER_CONCURRENCY"
_DEFAULT_PROVIDER_CONCURRENCY = 4
_MAX_PROVIDER_CONCURRENCY = 100


@dataclass(frozen=True, slots=True)
class RateLimitConfig:
    enabled: bool
    backend: str
    hmac_key: bytes
    trusted_proxy_hops: int
    trusted_proxy_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
    scale: Decimal
    max_local_buckets: int = 50_000
    trust_app_runner_xff: bool = False
    provider_concurrency: int = _DEFAULT_PROVIDER_CONCURRENCY

    @classmethod
    def from_environment(
        cls,
        database_url: str | None,
        environment: Mapping[str, str] | None = None,
    ) -> RateLimitConfig:
        values = environment if environment is not None else os.environ
        enabled = _flag(values, "HINDSIGHT_RATE_LIMIT_ENABLED", default=True)
        requested_backend = (
            values.get(
                "HINDSIGHT_RATE_LIMIT_BACKEND",
                "auto",
            )
            .strip()
            .lower()
        )
        if requested_backend not in {"auto", "memory", "cockroach"}:
            raise ValueError("HINDSIGHT_RATE_LIMIT_BACKEND must be auto, memory, or cockroach")
        if requested_backend == "auto":
            backend = "cockroach" if database_url else "memory"
        else:
            backend = requested_backend
        if enabled and backend == "cockroach" and not database_url:
            raise ValueError("Cockroach rate limiting requires DATABASE_URL")

        raw_key = values.get("HINDSIGHT_RATE_LIMIT_HMAC_KEY", "")
        if enabled and backend == "cockroach" and len(raw_key.encode("utf-8")) < 32:
            raise ValueError(
                "HINDSIGHT_RATE_LIMIT_HMAC_KEY must contain at least 32 bytes "
                "for Cockroach rate limiting"
            )
        hmac_key = raw_key.encode("utf-8") if raw_key else secrets.token_bytes(32)
        trust_app_runner_xff = _flag(
            values,
            "HINDSIGHT_RATE_LIMIT_TRUST_APP_RUNNER_XFF",
            default=False,
        )

        raw_hops = values.get("HINDSIGHT_RATE_LIMIT_TRUSTED_PROXY_HOPS", "0").strip()
        try:
            trusted_proxy_hops = int(raw_hops)
        except ValueError as error:
            raise ValueError(
                "HINDSIGHT_RATE_LIMIT_TRUSTED_PROXY_HOPS must be an integer"
            ) from error
        if not 0 <= trusted_proxy_hops <= _MAX_FORWARDED_HOPS:
            raise ValueError(
                f"HINDSIGHT_RATE_LIMIT_TRUSTED_PROXY_HOPS must be between 0 "
                f"and {_MAX_FORWARDED_HOPS}"
            )

        raw_networks = values.get("HINDSIGHT_RATE_LIMIT_TRUSTED_PROXY_CIDRS", "")
        networks = _proxy_networks(raw_networks)
        if trusted_proxy_hops and not networks:
            raise ValueError("trusted proxy hops require HINDSIGHT_RATE_LIMIT_TRUSTED_PROXY_CIDRS")
        if trusted_proxy_hops and any(network.prefixlen == 0 for network in networks):
            raise ValueError("trusted proxy CIDRs cannot cover an entire address family")
        if trust_app_runner_xff and (trusted_proxy_hops or networks):
            raise ValueError("App Runner X-Forwarded-For trust cannot be combined with proxy CIDRs")

        raw_scale = values.get("HINDSIGHT_RATE_LIMIT_SCALE", "1").strip()
        try:
            scale = Decimal(raw_scale)
        except InvalidOperation as error:
            raise ValueError("HINDSIGHT_RATE_LIMIT_SCALE must be a decimal") from error
        if not Decimal("0.1") <= scale <= Decimal("10"):
            raise ValueError("HINDSIGHT_RATE_LIMIT_SCALE must be between 0.1 and 10")

        raw_max_buckets = values.get("HINDSIGHT_RATE_LIMIT_MAX_LOCAL_BUCKETS", "50000")
        try:
            max_local_buckets = int(raw_max_buckets)
        except ValueError as error:
            raise ValueError("HINDSIGHT_RATE_LIMIT_MAX_LOCAL_BUCKETS must be an integer") from error
        if not 1_000 <= max_local_buckets <= 1_000_000:
            raise ValueError(
                "HINDSIGHT_RATE_LIMIT_MAX_LOCAL_BUCKETS must be between 1000 and 1000000"
            )

        raw_provider_concurrency = values.get(
            _PROVIDER_CONCURRENCY_ENV,
            str(_DEFAULT_PROVIDER_CONCURRENCY),
        )
        try:
            provider_concurrency = int(raw_provider_concurrency.strip())
        except (AttributeError, ValueError) as error:
            raise ValueError(f"{_PROVIDER_CONCURRENCY_ENV} must be an integer") from error
        if not 1 <= provider_concurrency <= _MAX_PROVIDER_CONCURRENCY:
            raise ValueError(
                f"{_PROVIDER_CONCURRENCY_ENV} must be between 1 and {_MAX_PROVIDER_CONCURRENCY}"
            )

        return cls(
            enabled=enabled,
            backend=backend,
            hmac_key=hmac_key,
            trusted_proxy_hops=trusted_proxy_hops,
            trusted_proxy_networks=networks,
            scale=scale,
            max_local_buckets=max_local_buckets,
            trust_app_runner_xff=trust_app_runner_xff,
            provider_concurrency=provider_concurrency,
        )


@dataclass(frozen=True, slots=True)
class RateLimitRule:
    policy_id: str
    limit: int
    window_seconds: int
    burst: int
    cost: int = 1
    scope: str = "client"
    shared: bool = True


@dataclass(frozen=True, slots=True)
class RateLimitLeaseRule:
    lease_key: str
    slots: int
    ttl_seconds: int


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    allowed: bool
    policy_id: str
    limit: int
    remaining: int
    retry_after_seconds: int
    reset_after_seconds: int

    @property
    def headers(self) -> dict[str, str]:
        headers = {
            "RateLimit-Limit": str(self.limit),
            "RateLimit-Remaining": str(self.remaining),
            "RateLimit-Reset": str(self.reset_after_seconds),
        }
        if not self.allowed:
            headers["Retry-After"] = str(max(1, self.retry_after_seconds))
        return headers


@dataclass(frozen=True, slots=True)
class OperationLeaseDecision:
    acquired: bool
    policy_id: str
    limit: int
    lease: RateLimitLease | None

    @property
    def headers(self) -> dict[str, str]:
        headers = {
            "RateLimit-Limit": str(self.limit),
            "RateLimit-Remaining": str(max(0, self.limit - 1) if self.acquired else 0),
            "RateLimit-Reset": "5",
        }
        if not self.acquired:
            headers["Retry-After"] = "5"
        return headers


class RateLimitUnavailableError(RuntimeError):
    pass


class RateLimitPolicy(Protocol):
    def rules(self, method: str, path: str) -> tuple[RateLimitRule, ...]: ...

    def operation_rules(self, operation: str) -> tuple[RateLimitRule, ...]: ...

    def lease_rule(self, operation: str) -> RateLimitLeaseRule | None: ...


class DefaultRateLimitPolicy:
    def __init__(
        self,
        scale: Decimal = Decimal(1),
        provider_concurrency: int = _DEFAULT_PROVIDER_CONCURRENCY,
    ) -> None:
        if not 1 <= provider_concurrency <= _MAX_PROVIDER_CONCURRENCY:
            raise ValueError(
                f"provider_concurrency must be between 1 and {_MAX_PROVIDER_CONCURRENCY}"
            )
        self._scale = scale
        self._provider_concurrency = provider_concurrency

    def rules(self, method: str, path: str) -> tuple[RateLimitRule, ...]:
        normalized_method = method.upper()
        normalized_path = _normalized_path(path)
        if normalized_path == "/health":
            # The load-balancer probe must never be throttled. It carries no
            # forwarded address, so it shares a principal with any request whose
            # X-Forwarded-For cannot be resolved; a shared bucket would let that
            # traffic starve the probe and deregister the task. The edge rate
            # rules remain the ceiling for this static response.
            return ()
        if normalized_path == "/ready":
            # Client-scoped rather than global: readiness drives a database probe,
            # so it stays bounded per caller, but no caller can exhaust the budget
            # that an operator or platform prober depends on.
            return (
                self._rule(
                    "readiness-client",
                    60,
                    60,
                    30,
                    shared=False,
                ),
            )

        rules = [
            self._rule("edge-fallback", 180, 60, 30, shared=False),
        ]
        is_api = normalized_path.startswith(("/demo", "/decisions", "/memories"))
        if is_api:
            rules.append(
                self._rule(
                    "api-global",
                    600,
                    60,
                    100,
                    scope="global",
                )
            )

        if normalized_path == "/memories/search" and normalized_method == "GET":
            rules.extend(
                (
                    self._rule("memory-search-client", 12, 60, 3),
                    self._rule(
                        "memory-search-global",
                        120,
                        60,
                        20,
                        scope="global",
                    ),
                )
            )
        elif normalized_path == "/demo/seed" and normalized_method == "POST":
            rules.append(self._rule("demo-seed-attempt-client", 6, 60, 2))
        elif normalized_path == "/demo/reset" and normalized_method == "POST":
            rules.append(self._rule("demo-reset-client", 3, 3_600, 1))
        elif normalized_method not in _SAFE_METHODS:
            rules.extend(
                (
                    self._rule("api-mutation-client", 10, 60, 3),
                    self._rule(
                        "api-mutation-client-hour",
                        60,
                        3_600,
                        10,
                    ),
                    self._rule(
                        "api-mutation-global",
                        120,
                        60,
                        20,
                        scope="global",
                    ),
                )
            )
        elif normalized_path.startswith(("/demo/", "/decisions/")):
            rules.append(self._rule("api-read-client", 60, 60, 15))

        return tuple(rules)

    def operation_rules(self, operation: str) -> tuple[RateLimitRule, ...]:
        if operation == "memory-search-provider":
            return (
                self._rule("provider-budget-client", 32, 3_600, 8, cost=1),
                self._rule(
                    "provider-budget-global",
                    160,
                    3_600,
                    24,
                    cost=1,
                    scope="global",
                ),
            )
        if operation == "demo-seed-execution":
            return (
                self._rule("demo-seed-client", 2, 600, 1),
                self._rule(
                    "demo-seed-global",
                    12,
                    3_600,
                    1,
                    scope="global",
                ),
            )
        if operation == "demo-seed-provider":
            return (
                self._rule("demo-seed-client", 2, 600, 1),
                self._rule(
                    "demo-seed-global",
                    12,
                    3_600,
                    1,
                    scope="global",
                ),
                self._rule("provider-budget-client", 32, 3_600, 8, cost=8),
                self._rule(
                    "provider-budget-global",
                    160,
                    3_600,
                    24,
                    cost=8,
                    scope="global",
                ),
            )
        if operation == "demo-reset-authorized":
            return (
                self._rule(
                    "demo-reset-authorized-global",
                    10,
                    3_600,
                    1,
                    scope="global",
                ),
            )
        return ()

    def lease_rule(self, operation: str) -> RateLimitLeaseRule | None:
        if operation in {"demo-workspace-exclusive", "demo-reset-authorized"}:
            return RateLimitLeaseRule(
                lease_key="demo-workspace-execution",
                slots=1,
                ttl_seconds=720,
            )
        if operation in {"demo-seed-provider", "memory-search-provider"}:
            return RateLimitLeaseRule(
                lease_key="provider-concurrency",
                slots=self._provider_concurrency,
                ttl_seconds=600,
            )
        return None

    def _rule(
        self,
        policy_id: str,
        limit: int,
        window_seconds: int,
        burst: int,
        *,
        cost: int = 1,
        scope: str = "client",
        shared: bool = True,
    ) -> RateLimitRule:
        scaled_limit = max(cost, int(Decimal(limit) * self._scale))
        scaled_burst = max(cost, int(Decimal(burst) * self._scale))
        return RateLimitRule(
            policy_id=policy_id,
            limit=scaled_limit,
            window_seconds=window_seconds,
            burst=scaled_burst,
            cost=cost,
            scope=scope,
            shared=shared,
        )


class RateLimiter:
    def __init__(
        self,
        config: RateLimitConfig,
        local_store: RateLimitStore,
        *,
        shared_store: RateLimitStore | None = None,
        policy: RateLimitPolicy | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._local_store = local_store
        self._shared_store = shared_store
        self._policy = policy or DefaultRateLimitPolicy(
            config.scale,
            config.provider_concurrency,
        )
        self._clock = clock

    def open(self) -> None:
        self._local_store.open()
        if self._shared_store is not None:
            self._shared_store.open()

    def close(self) -> None:
        try:
            if self._shared_store is not None:
                self._shared_store.close()
        finally:
            self._local_store.close()

    def check(
        self,
        *,
        method: str,
        path: str,
        peer_host: str | None,
        forwarded_for: tuple[str, ...] = (),
        principal: str | None = None,
    ) -> RateLimitDecision | None:
        if not self._config.enabled:
            return None
        rules = self._policy.rules(method, path)
        if not rules:
            return None

        resolved_principal = principal or self.resolve_client_principal(
            peer_host,
            forwarded_for,
        )
        return self._check_rules(rules, resolved_principal)

    def resolve_client_principal(
        self,
        peer_host: str | None,
        forwarded_for: tuple[str, ...] = (),
    ) -> str:
        return client_principal(
            peer_host,
            forwarded_for,
            trusted_proxy_hops=self._config.trusted_proxy_hops,
            trusted_proxy_networks=self._config.trusted_proxy_networks,
            trust_app_runner_xff=self._config.trust_app_runner_xff,
        )

    def check_operation(
        self,
        operation: str,
        principal: str = "global",
    ) -> RateLimitDecision | None:
        if not self._config.enabled:
            return None
        rule_factory = getattr(self._policy, "operation_rules", None)
        rules = rule_factory(operation) if rule_factory is not None else ()
        if not rules:
            return None
        return self._check_rules(rules, principal)

    def acquire_operation_lease(
        self,
        operation: str,
    ) -> OperationLeaseDecision | None:
        if not self._config.enabled:
            return None
        rule_factory = getattr(self._policy, "lease_rule", None)
        rule = rule_factory(operation) if rule_factory is not None else None
        if rule is None:
            return None
        store = self._shared_store or self._local_store
        holder_hash = _principal_hash(self._config.hmac_key, secrets.token_hex(32))
        try:
            lease = store.acquire_lease(
                rule.lease_key,
                holder_hash,
                rule.slots,
                rule.ttl_seconds,
                float(self._clock()),
            )
        except RateLimitStoreError as error:
            raise RateLimitUnavailableError("concurrency limiter unavailable") from error
        except Exception as error:
            raise RateLimitUnavailableError("concurrency limiter failed") from error
        return OperationLeaseDecision(
            acquired=lease is not None,
            policy_id=rule.lease_key,
            limit=rule.slots,
            lease=lease,
        )

    def release_operation_lease(self, lease: RateLimitLease | None) -> None:
        if lease is None:
            return
        store = self._shared_store or self._local_store
        try:
            store.release_lease(lease)
        except RateLimitStoreError as error:
            raise RateLimitUnavailableError("concurrency lease release unavailable") from error
        except Exception as error:
            raise RateLimitUnavailableError("concurrency lease release failed") from error

    def probe(self) -> None:
        if not self._config.enabled or self._shared_store is None:
            return
        bucket = RateLimitBucket(
            policy_id="limiter-readiness",
            principal_hash=_principal_hash(self._config.hmac_key, "global"),
            limit=60,
            window_seconds=60,
            burst=60,
        )
        try:
            self._shared_store.consume((bucket,), float(self._clock()))
            holder_hash = _principal_hash(
                self._config.hmac_key,
                secrets.token_hex(32),
            )
            lease = self._shared_store.acquire_lease(
                "limiter-readiness",
                holder_hash,
                1,
                30,
                float(self._clock()),
            )
            if lease is not None:
                self._shared_store.release_lease(lease)
        except RateLimitStoreError as error:
            raise RateLimitUnavailableError("rate-limit backend unavailable") from error
        except Exception as error:
            raise RateLimitUnavailableError("rate-limit readiness failed") from error

    def _check_rules(
        self,
        rules: tuple[RateLimitRule, ...],
        principal: str,
    ) -> RateLimitDecision:
        client_hash = _principal_hash(self._config.hmac_key, principal)
        global_hash = _principal_hash(self._config.hmac_key, "global")
        now = float(self._clock())
        buckets = tuple(
            RateLimitBucket(
                policy_id=rule.policy_id,
                principal_hash=global_hash if rule.scope == "global" else client_hash,
                limit=rule.limit,
                window_seconds=rule.window_seconds,
                burst=rule.burst,
                cost=rule.cost,
                shared=rule.shared,
            )
            for rule in rules
        )

        try:
            local_buckets = (
                buckets
                if self._shared_store is None
                else tuple(bucket for bucket in buckets if not bucket.shared)
            )
            local_results = self._local_store.consume(local_buckets, now)
            if local_results:
                local_decision = _decision(local_results)
                if not local_decision.allowed:
                    return local_decision

            shared_buckets = (
                ()
                if self._shared_store is None
                else tuple(bucket for bucket in buckets if bucket.shared)
            )
            shared_results = (
                () if not shared_buckets else self._shared_store.consume(shared_buckets, now)
            )
            return _decision(local_results + shared_results)
        except RateLimitStoreError as error:
            raise RateLimitUnavailableError("rate-limit backend unavailable") from error
        except Exception as error:
            raise RateLimitUnavailableError("rate-limit evaluation failed") from error


def build_rate_limiter(
    database_url: str | None,
    *,
    environment: Mapping[str, str] | None = None,
) -> RateLimiter:
    values = environment if environment is not None else os.environ
    config = RateLimitConfig.from_environment(database_url, values)
    local = InMemoryTokenBucketStore(max_buckets=config.max_local_buckets)
    shared: RateLimitStore | None = None
    if config.enabled and config.backend == "cockroach":
        if database_url is None:
            raise ValueError("Cockroach rate limiting requires DATABASE_URL")
        shared = CockroachTokenBucketStore(
            database_url,
            max_pool_size=_rate_limit_pool_max_size(values),
        )
    return RateLimiter(config, local, shared_store=shared)


def _rate_limit_pool_max_size(environment: Mapping[str, str]) -> int:
    raw_value = environment.get(
        _RATE_LIMIT_POOL_MAX_SIZE_ENV,
        str(_DEFAULT_RATE_LIMIT_POOL_MAX_SIZE),
    )
    try:
        value = int(raw_value.strip())
    except (AttributeError, ValueError) as error:
        raise ValueError(f"{_RATE_LIMIT_POOL_MAX_SIZE_ENV} must be an integer") from error
    if not 1 <= value <= _MAX_RATE_LIMIT_POOL_MAX_SIZE:
        raise ValueError(
            f"{_RATE_LIMIT_POOL_MAX_SIZE_ENV} must be between 1 and {_MAX_RATE_LIMIT_POOL_MAX_SIZE}"
        )
    return value


def client_principal(
    peer_host: str | None,
    forwarded_for: tuple[str, ...],
    *,
    trusted_proxy_hops: int,
    trusted_proxy_networks: tuple[
        ipaddress.IPv4Network | ipaddress.IPv6Network,
        ...,
    ],
    trust_app_runner_xff: bool = False,
) -> str:
    peer = _parsed_ip(peer_host)
    selected = peer
    if trust_app_runner_xff and len(forwarded_for) == 1:
        parts = [part.strip() for part in forwarded_for[0].split(",")]
        parsed = [_parsed_ip(part) for part in parts]
        if len(parts) == 1 and parsed[0] is not None:
            selected = parsed[0]
    elif (
        peer is not None
        and trusted_proxy_hops > 0
        and _is_trusted(peer, trusted_proxy_networks)
        and len(forwarded_for) == 1
    ):
        parts = [part.strip() for part in forwarded_for[0].split(",")]
        if len(parts) <= 16 and len(parts) >= trusted_proxy_hops:
            parsed = [_parsed_ip(part) for part in parts]
            candidate_index = len(parsed) - trusted_proxy_hops
            trusted_chain = parsed[candidate_index + 1 :]
            if parsed[candidate_index] is not None and all(
                item is not None and _is_trusted(item, trusted_proxy_networks)
                for item in trusted_chain
            ):
                selected = parsed[candidate_index]
    return _canonical_ip(selected) if selected is not None else "anonymous"


def _decision(results: tuple[RateLimitResult, ...]) -> RateLimitDecision:
    if not results:
        raise ValueError("rate-limit decision requires at least one result")
    denied = tuple(result for result in results if not result.allowed)
    if denied:
        selected = max(denied, key=lambda result: result.retry_after_seconds)
        return RateLimitDecision(
            allowed=False,
            policy_id=selected.policy_id,
            limit=selected.limit,
            remaining=0,
            retry_after_seconds=max(1, selected.retry_after_seconds),
            reset_after_seconds=max(
                selected.reset_after_seconds,
                selected.retry_after_seconds,
            ),
        )
    selected = min(
        results,
        key=lambda result: (
            result.remaining / result.limit,
            result.remaining,
        ),
    )
    return RateLimitDecision(
        allowed=True,
        policy_id=selected.policy_id,
        limit=selected.limit,
        remaining=selected.remaining,
        retry_after_seconds=0,
        reset_after_seconds=selected.reset_after_seconds,
    )


def _flag(
    environment: Mapping[str, str],
    name: str,
    *,
    default: bool,
) -> bool:
    raw = environment.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(f"{name} must be true or false")


def _proxy_networks(
    raw: str,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    if not raw.strip():
        return ()
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for value in raw.split(","):
        try:
            networks.append(ipaddress.ip_network(value.strip(), strict=False))
        except ValueError as error:
            raise ValueError(
                "HINDSIGHT_RATE_LIMIT_TRUSTED_PROXY_CIDRS contains an invalid network"
            ) from error
    return tuple(networks)


def _parsed_ip(value: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if candidate.startswith("[") and "]" in candidate:
        candidate = candidate[1 : candidate.index("]")]
    candidate = candidate.split("%", 1)[0]
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        if candidate.count(":") == 1 and "." in candidate:
            try:
                return ipaddress.ip_address(candidate.rsplit(":", 1)[0])
            except ValueError:
                return None
        return None


def _canonical_ip(
    value: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> str:
    if isinstance(value, ipaddress.IPv6Address):
        if value.ipv4_mapped is not None:
            return value.ipv4_mapped.compressed
        network = ipaddress.ip_network(f"{value.compressed}/64", strict=False)
        return f"{network.network_address.compressed}/64"
    return value.compressed


def _is_trusted(
    value: ipaddress.IPv4Address | ipaddress.IPv6Address,
    networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...],
) -> bool:
    if isinstance(value, ipaddress.IPv6Address) and value.ipv4_mapped is not None:
        value = value.ipv4_mapped
    return any(value.version == network.version and value in network for network in networks)


def _principal_hash(key: bytes, principal: str) -> str:
    return hmac.new(key, principal.encode("utf-8"), hashlib.sha256).hexdigest()


def _normalized_path(path: str) -> str:
    if not path:
        return "/"
    normalized = path if path.startswith("/") else f"/{path}"
    if normalized != "/":
        normalized = normalized.rstrip("/")
    return normalized
