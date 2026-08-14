from __future__ import annotations

import asyncio
import time

import pytest

from hindsight.agents.investigation import InvestigationContextReadError
from hindsight.infrastructure.bedrock import (
    CONNECT_TIMEOUT_SECONDS,
    READ_TIMEOUT_SECONDS,
    TOTAL_MAX_ATTEMPTS,
    bedrock_client_config,
)
from hindsight.infrastructure.embeddings import EMBEDDING_READ_TIMEOUT_SECONDS
from hindsight.infrastructure.managed_mcp import (
    MANAGED_MCP_DEADLINE_SECONDS,
    MANAGED_MCP_PHASE_TIMEOUT_SECONDS,
    MAX_MANAGED_MCP_DEADLINE_SECONDS,
    CockroachCloudManagedMcpClient,
)
from hindsight.infrastructure.provider_budget import (
    DEMO_WORKSPACE_EXCLUSIVE_LEASE_TTL_SECONDS,
    DEMO_WORKSPACE_LEASE_SECONDS,
    PROVIDER_LEASE_TTL_SECONDS,
    worst_case_seed_seconds,
)
from hindsight.web.rate_limit import DefaultRateLimitPolicy

CLUSTER_ID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
SHORT_DEADLINE_SECONDS = 0.05
SLOW_PHASE_SECONDS = 5.0


class _SlowManagedMcpClient(CockroachCloudManagedMcpClient):
    """A client whose exchange never finishes, standing in for a stalled provider."""

    async def _select(self, database: str, query: str) -> object:
        await asyncio.sleep(SLOW_PHASE_SECONDS)
        return {}


def test_bedrock_clients_bound_every_call_instead_of_using_botocore_defaults() -> None:
    config = bedrock_client_config()

    assert config.connect_timeout == CONNECT_TIMEOUT_SECONDS
    assert config.read_timeout == READ_TIMEOUT_SECONDS
    assert config.retries["total_max_attempts"] == TOTAL_MAX_ATTEMPTS
    assert "max_attempts" not in config.retries


def test_embeddings_use_a_shorter_read_timeout_than_conversations() -> None:
    config = bedrock_client_config(EMBEDDING_READ_TIMEOUT_SECONDS)

    assert config.read_timeout == EMBEDDING_READ_TIMEOUT_SECONDS
    assert EMBEDDING_READ_TIMEOUT_SECONDS < READ_TIMEOUT_SECONDS


def test_managed_mcp_bounds_the_whole_exchange_not_only_one_phase() -> None:
    """One select opens a session, lists tools, then calls one. Without a deadline
    over all of them, the phase timeout bounds a stall but never the request."""
    client = _SlowManagedMcpClient(
        CLUSTER_ID,
        "test-key",
        timeout_seconds=SHORT_DEADLINE_SECONDS,
        deadline_seconds=SHORT_DEADLINE_SECONDS,
    )

    started_at = time.monotonic()
    with pytest.raises(InvestigationContextReadError) as error:
        client.select(database="defaultdb", query="SELECT 1")
    elapsed = time.monotonic() - started_at

    assert error.value.code == "managed_mcp_deadline_exceeded"
    assert error.value.retryable is True
    assert elapsed < SLOW_PHASE_SECONDS


def test_managed_mcp_deadline_cannot_be_shorter_than_one_phase() -> None:
    with pytest.raises(ValueError):
        CockroachCloudManagedMcpClient(
            CLUSTER_ID,
            "test-key",
            timeout_seconds=MANAGED_MCP_DEADLINE_SECONDS,
            deadline_seconds=MANAGED_MCP_PHASE_TIMEOUT_SECONDS / 2,
        )


def test_managed_mcp_default_deadline_covers_a_phase_and_stays_bounded() -> None:
    assert MANAGED_MCP_PHASE_TIMEOUT_SECONDS <= MANAGED_MCP_DEADLINE_SECONDS
    assert MANAGED_MCP_DEADLINE_SECONDS <= MAX_MANAGED_MCP_DEADLINE_SECONDS


def test_the_worst_case_seed_stays_inside_the_concurrency_lease_ttl() -> None:
    """A lease that expires mid-operation would let the concurrency cap be exceeded,
    so the provider timeouts must bound a seed below the lease TTL. Both sides are
    read from the runtime, so raising any timeout moves the TTL with it."""
    policy = DefaultRateLimitPolicy()
    provider_lease = policy.lease_rule("demo-seed-provider")

    assert provider_lease is not None
    assert provider_lease.ttl_seconds == PROVIDER_LEASE_TTL_SECONDS
    assert worst_case_seed_seconds() <= PROVIDER_LEASE_TTL_SECONDS


def test_workspace_leases_outlive_the_provider_lease_they_protect() -> None:
    policy = DefaultRateLimitPolicy()
    exclusive_lease = policy.lease_rule("demo-workspace-exclusive")

    assert exclusive_lease is not None
    assert PROVIDER_LEASE_TTL_SECONDS < DEMO_WORKSPACE_LEASE_SECONDS
    assert DEMO_WORKSPACE_LEASE_SECONDS < DEMO_WORKSPACE_EXCLUSIVE_LEASE_TTL_SECONDS
    assert exclusive_lease.ttl_seconds == DEMO_WORKSPACE_EXCLUSIVE_LEASE_TTL_SECONDS
