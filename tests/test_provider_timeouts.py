from __future__ import annotations

from hindsight.infrastructure.bedrock import (
    CONNECT_TIMEOUT_SECONDS,
    MAX_RETRY_BACKOFF_SECONDS,
    READ_TIMEOUT_SECONDS,
    TOTAL_MAX_ATTEMPTS,
    bedrock_client_config,
)
from hindsight.infrastructure.embeddings import EMBEDDING_READ_TIMEOUT_SECONDS

CONCURRENCY_LEASE_TTL_SECONDS = 600
CONVERSE_CALLS_PER_SEED = 4
EMBEDDING_CALLS_PER_SEED = 3
MANAGED_MCP_CALLS_PER_SEED = 1
MANAGED_MCP_TIMEOUT_SECONDS = 15
OPERATIONAL_MARGIN_SECONDS = 30


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


def test_the_worst_case_seed_stays_inside_the_concurrency_lease_ttl() -> None:
    """A lease that expires mid-operation would let the concurrency cap be exceeded,
    so the provider timeouts must bound a seed below the lease TTL."""
    retry_backoff = (TOTAL_MAX_ATTEMPTS - 1) * MAX_RETRY_BACKOFF_SECONDS
    per_call = TOTAL_MAX_ATTEMPTS * (CONNECT_TIMEOUT_SECONDS + READ_TIMEOUT_SECONDS) + retry_backoff
    per_embedding = (
        TOTAL_MAX_ATTEMPTS * (CONNECT_TIMEOUT_SECONDS + EMBEDDING_READ_TIMEOUT_SECONDS)
        + retry_backoff
    )
    worst_case = (
        CONVERSE_CALLS_PER_SEED * per_call
        + EMBEDDING_CALLS_PER_SEED * per_embedding
        + MANAGED_MCP_CALLS_PER_SEED * MANAGED_MCP_TIMEOUT_SECONDS
        + OPERATIONAL_MARGIN_SECONDS
    )

    assert worst_case < CONCURRENCY_LEASE_TTL_SECONDS
