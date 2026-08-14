from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from hindsight.adapters.telecom.remediation import InMemoryTelecomRemediationRepository
from hindsight.agents.advisory import BedrockAdvisoryClient
from hindsight.agents.investigation import InvestigationAgent
from hindsight.core.agents.repository import AgentRunRepository, InMemoryAgentRunRepository
from hindsight.core.assertions.repository import InMemoryAssertionRepository
from hindsight.core.decisions.repository import InMemoryDecisionRepository
from hindsight.core.memory import SemanticProceduralMemory
from hindsight.demo import run_demo_workflow
from hindsight.infrastructure.bedrock import BedrockConverseClient
from hindsight.infrastructure.database import CockroachDatabasePool, DatabasePoolConfig
from hindsight.infrastructure.embeddings import (
    DEFAULT_EMBEDDING_MODEL_ID,
    BedrockTitanTextEmbedder,
)
from hindsight.infrastructure.managed_mcp import (
    CockroachCloudManagedMcpClient,
    InvestigationContextSnapshot,
    ManagedMcpInvestigationContextReader,
    database_name_from_url,
)
from hindsight.infrastructure.pooled_repositories import (
    PooledAgentRunRepository,
    PooledAssertionRepository,
    PooledDecisionRepository,
    PooledInvestigationContextStore,
    PooledTelecomRemediationRepository,
    PooledTelecomVectorMemoryStore,
)
from hindsight.telemetry import current_correlation_id

ConnectionCheckout = Callable[[], AbstractContextManager[Any]]


class InvestigationContextStore(Protocol):
    def persist(
        self,
        case_id: UUID,
        context: dict[str, object],
    ) -> InvestigationContextSnapshot: ...


def execute_demo(
    database_url: str | None,
    *,
    bedrock_model_id: str | None = None,
    vector_enabled: bool = False,
    embedding_model_id: str = DEFAULT_EMBEDDING_MODEL_ID,
    aws_region: str | None = None,
    mcp_cluster_id: str | None = None,
    mcp_api_key: str | None = None,
    connection_context_factory: ConnectionCheckout | None = None,
) -> dict[str, object]:
    owned_pool = None
    checkout = connection_context_factory
    if database_url is not None and checkout is None:
        owned_pool = CockroachDatabasePool(
            database_url,
            config=DatabasePoolConfig.from_environment(),
        )
        owned_pool.open()
        checkout = owned_pool.checkout
    correlation_id = current_correlation_id() or uuid4()
    try:
        if database_url is None:
            assertion_repository = InMemoryAssertionRepository()
            decision_repository = InMemoryDecisionRepository()
            remediation_repository = InMemoryTelecomRemediationRepository()
            agent_run_repository = InMemoryAgentRunRepository()
            backend = "in_memory"
        else:
            if checkout is None:
                raise RuntimeError("CockroachDB checkout was not configured")
            assertion_repository = PooledAssertionRepository(checkout)
            decision_repository = PooledDecisionRepository(checkout)
            remediation_repository = PooledTelecomRemediationRepository(checkout)
            agent_run_repository = PooledAgentRunRepository(checkout)
            backend = "cockroachdb"

        bedrock_client = None
        advisory_client = None
        if bedrock_model_id:
            if database_url is None:
                raise ValueError("Bedrock agents require CockroachDB for durable traces")
            bedrock_client = BedrockConverseClient(bedrock_model_id, aws_region)
            advisory_client = BedrockAdvisoryClient(bedrock_client)

        vector_memory = None
        if vector_enabled:
            if database_url is None or checkout is None:
                raise ValueError("vector memory requires CockroachDB")
            vector_memory = SemanticProceduralMemory(
                PooledTelecomVectorMemoryStore(checkout),
                BedrockTitanTextEmbedder(embedding_model_id, aws_region),
                remediation_repository,
            )

        payload = run_demo_workflow(
            assertion_repository,
            decision_repository,
            remediation_repository,
            backend,
            vector_memory=vector_memory,
            include_investigation_context=bedrock_model_id is not None,
            agent_run_repository=agent_run_repository,
            advisory_client=advisory_client,
            correlation_id=correlation_id,
        )
        if bedrock_model_id:
            if database_url is None or checkout is None or bedrock_client is None:
                raise ValueError("Bedrock investigation requires CockroachDB")
            context_store = None
            context_reader = None
            if mcp_cluster_id is not None:
                if mcp_api_key is None:
                    raise ValueError("Managed MCP requires an API key")
                context_store = PooledInvestigationContextStore(checkout)
                context_reader = ManagedMcpInvestigationContextReader(
                    CockroachCloudManagedMcpClient(mcp_cluster_id, mcp_api_key),
                    database_name_from_url(database_url),
                )
            _add_bedrock_investigation(
                payload,
                agent_run_repository,
                bedrock_client,
                correlation_id,
                context_store=context_store,
                context_reader=context_reader,
            )
        return payload
    finally:
        if owned_pool is not None:
            owned_pool.close()


def _add_bedrock_investigation(
    payload: dict[str, object],
    repository: AgentRunRepository,
    client: BedrockConverseClient,
    correlation_id: UUID,
    *,
    context_store: InvestigationContextStore | None = None,
    context_reader: ManagedMcpInvestigationContextReader | None = None,
) -> None:
    learning = cast(dict[str, object], payload["learning_proof"])
    context = cast(dict[str, object], learning["investigation_context"])
    case_id = UUID(str(context["case_id"]))
    context_snapshot_id = None
    if context_reader is None:
        result = InvestigationAgent(client, repository).run(
            case_id=case_id,
            context=context,
            correlation_id=correlation_id,
        )
    else:
        if context_store is None:
            raise ValueError("Managed MCP requires a durable context store")
        snapshot = context_store.persist(case_id, context)
        context_snapshot_id = snapshot.id
        result = InvestigationAgent(
            client,
            repository,
            context_reader=context_reader.for_snapshot(snapshot.id),
        ).run(case_id=case_id, correlation_id=correlation_id)
    persisted = repository.get(result.run_id)
    calls = repository.tool_calls(result.run_id)
    learning.pop("investigation_context")
    payload["bedrock_investigation"] = {
        "agent_run_id": persisted.id,
        "status": persisted.status,
        **({"context_snapshot_id": context_snapshot_id} if context_snapshot_id is not None else {}),
        **(persisted.output or {}),
        "tool_calls": [
            {
                "tool_use_id": call.tool_use_id,
                "tool_name": call.tool_name,
                "status": call.status,
            }
            for call in calls
        ],
    }
