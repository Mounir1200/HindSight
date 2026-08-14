"""Worst-case provider time budget, and the lease TTLs derived from it.

A concurrency lease that expires while its holder is still calling a provider stops
capping concurrency: a second caller takes the freed slot while the first is running.
The lease TTLs must therefore be derived from the provider timeouts rather than
maintained next to them, so that changing a timeout moves the TTL with it.
"""

from math import ceil

from hindsight.infrastructure.bedrock import (
    CONNECT_TIMEOUT_SECONDS,
    MAX_RETRY_BACKOFF_SECONDS,
    READ_TIMEOUT_SECONDS,
    TOTAL_MAX_ATTEMPTS,
)
from hindsight.infrastructure.embeddings import EMBEDDING_READ_TIMEOUT_SECONDS
from hindsight.infrastructure.managed_mcp import MANAGED_MCP_DEADLINE_SECONDS

# Provider calls issued by one seeded audit. These counts describe the workflow in
# hindsight.demo and must be raised whenever it gains a Bedrock, embedding or MCP call.
CONVERSE_CALLS_PER_SEED = 4
EMBEDDING_CALLS_PER_SEED = 3
MANAGED_MCP_CALLS_PER_SEED = 1

OPERATIONAL_MARGIN_SECONDS = 30
LEASE_GRANULARITY_SECONDS = 60
WORKSPACE_LEASE_HEADROOM_SECONDS = 60


def bedrock_call_budget_seconds(read_timeout_seconds: float) -> float:
    """Bound one Bedrock call including every retry attempt and its backoff."""

    attempt_budget = TOTAL_MAX_ATTEMPTS * (CONNECT_TIMEOUT_SECONDS + read_timeout_seconds)
    return attempt_budget + (TOTAL_MAX_ATTEMPTS - 1) * MAX_RETRY_BACKOFF_SECONDS


def worst_case_seed_seconds() -> float:
    """Bound the provider time one ``/demo/seed`` execution can occupy."""

    converse = CONVERSE_CALLS_PER_SEED * bedrock_call_budget_seconds(READ_TIMEOUT_SECONDS)
    embeddings = EMBEDDING_CALLS_PER_SEED * bedrock_call_budget_seconds(
        EMBEDDING_READ_TIMEOUT_SECONDS
    )
    managed_mcp = MANAGED_MCP_CALLS_PER_SEED * MANAGED_MCP_DEADLINE_SECONDS
    return converse + embeddings + managed_mcp + OPERATIONAL_MARGIN_SECONDS


def _rounded_up_to_granularity(seconds: float) -> int:
    return ceil(seconds / LEASE_GRANULARITY_SECONDS) * LEASE_GRANULARITY_SECONDS


# A crashed task keeps its slot until the TTL elapses, so the TTL is the shortest
# value that still covers the worst case rather than a comfortable over-estimate.
PROVIDER_LEASE_TTL_SECONDS = _rounded_up_to_granularity(worst_case_seed_seconds())

# The workspace lease outlives the provider lease: once the providers return, the
# result still has to be serialized and persisted before the workspace is completed.
DEMO_WORKSPACE_LEASE_SECONDS = PROVIDER_LEASE_TTL_SECONDS + WORKSPACE_LEASE_HEADROOM_SECONDS

# The exclusive-entry lease is held across the whole request, so it outlives the
# workspace row lease it protects.
DEMO_WORKSPACE_EXCLUSIVE_LEASE_TTL_SECONDS = (
    DEMO_WORKSPACE_LEASE_SECONDS + WORKSPACE_LEASE_HEADROOM_SECONDS
)
