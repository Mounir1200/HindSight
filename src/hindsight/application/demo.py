from typing import cast
from uuid import UUID, uuid4

from hindsight.adapters.telecom.remediation import InMemoryTelecomRemediationRepository
from hindsight.agents.advisory import BedrockAdvisoryClient
from hindsight.agents.investigation import InvestigationAgent
from hindsight.core.agents.repository import InMemoryAgentRunRepository
from hindsight.core.assertions.repository import (
    CockroachAssertionRepository,
    InMemoryAssertionRepository,
)
from hindsight.core.decisions.repository import (
    CockroachDecisionRepository,
    InMemoryDecisionRepository,
)
from hindsight.core.memory import SemanticProceduralMemory
from hindsight.demo import run_demo_workflow
from hindsight.infrastructure.agent_runs import CockroachAgentRunRepository
from hindsight.infrastructure.bedrock import BedrockConverseClient
from hindsight.infrastructure.database import connect_database
from hindsight.infrastructure.embeddings import (
    DEFAULT_EMBEDDING_MODEL_ID,
    BedrockTitanTextEmbedder,
)
from hindsight.infrastructure.managed_mcp import (
    CockroachCloudManagedMcpClient,
    CockroachInvestigationContextStore,
    ManagedMcpInvestigationContextReader,
    database_name_from_url,
)
from hindsight.infrastructure.telecom_remediation import (
    CockroachTelecomRemediationRepository,
)
from hindsight.infrastructure.vector_memory import CockroachTelecomVectorMemoryStore


def execute_demo(
    database_url: str | None,
    *,
    bedrock_model_id: str | None = None,
    vector_enabled: bool = False,
    embedding_model_id: str = DEFAULT_EMBEDDING_MODEL_ID,
    aws_region: str | None = None,
    mcp_cluster_id: str | None = None,
    mcp_api_key: str | None = None,
) -> dict[str, object]:
    connection = connect_database(database_url) if database_url else None
    correlation_id = uuid4()
    try:
        if connection is None:
            assertion_repository = InMemoryAssertionRepository()
            decision_repository = InMemoryDecisionRepository()
            remediation_repository = InMemoryTelecomRemediationRepository()
            agent_run_repository = InMemoryAgentRunRepository()
            backend = "in_memory"
        else:
            assertion_repository = CockroachAssertionRepository(connection)
            decision_repository = CockroachDecisionRepository(connection)
            remediation_repository = CockroachTelecomRemediationRepository(
                connection,
                connection_factory=lambda: connect_database(database_url),
            )
            agent_run_repository = CockroachAgentRunRepository(
                connection,
                connection_factory=lambda: connect_database(database_url),
            )
            backend = "cockroachdb"

        bedrock_client = None
        advisory_client = None
        if bedrock_model_id:
            if connection is None:
                raise ValueError("Bedrock agents require CockroachDB for durable traces")
            bedrock_client = BedrockConverseClient(bedrock_model_id, aws_region)
            advisory_client = BedrockAdvisoryClient(bedrock_client)

        vector_memory = None
        if vector_enabled:
            if connection is None or database_url is None:
                raise ValueError("vector memory requires CockroachDB")
            vector_memory = SemanticProceduralMemory(
                CockroachTelecomVectorMemoryStore(
                    connection,
                    connection_factory=lambda: connect_database(database_url),
                ),
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
            if connection is None or database_url is None or bedrock_client is None:
                raise ValueError("Bedrock investigation requires CockroachDB")
            context_store = None
            context_reader = None
            if mcp_cluster_id is not None:
                if mcp_api_key is None:
                    raise ValueError("Managed MCP requires an API key")
                context_store = CockroachInvestigationContextStore(connection)
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
        if connection is not None:
            connection.close()


def _add_bedrock_investigation(
    payload: dict[str, object],
    repository: CockroachAgentRunRepository,
    client: BedrockConverseClient,
    correlation_id: UUID,
    *,
    context_store: CockroachInvestigationContextStore | None = None,
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
