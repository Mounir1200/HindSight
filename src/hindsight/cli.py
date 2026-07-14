import argparse
import json
import os
import sys
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, cast
from uuid import UUID

from hindsight.adapters.telecom.remediation import InMemoryTelecomRemediationRepository
from hindsight.agents.investigation import InvestigationAgent, InvestigationAgentError
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
    EmbeddingProviderError,
)
from hindsight.infrastructure.migrations import apply_migrations
from hindsight.infrastructure.telecom_remediation import (
    CockroachTelecomRemediationRepository,
)
from hindsight.infrastructure.vector_memory import CockroachTelecomVectorMemoryStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hindsight")
    commands = parser.add_subparsers(dest="command", required=True)

    demo = commands.add_parser("demo", help="run the retroactive telecom-rate scenario")
    demo.add_argument(
        "--database-url",
        help="explicit CockroachDB URL; prefer --cockroach with DATABASE_URL",
    )
    demo.add_argument(
        "--cockroach",
        action="store_true",
        help="use CockroachDB through DATABASE_URL instead of local memory",
    )
    demo.add_argument(
        "--bedrock",
        action="store_true",
        help="run the durable read-only Bedrock investigation after the demo",
    )
    demo.add_argument(
        "--vector",
        action="store_true",
        help="index and retrieve procedural memory with CockroachDB DVI",
    )
    demo.add_argument(
        "--bedrock-model-id",
        default=os.getenv("BEDROCK_MODEL_ID"),
        help="tool-capable model or inference-profile ID; defaults to BEDROCK_MODEL_ID",
    )
    demo.add_argument(
        "--aws-region",
        default=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION"),
        help="Bedrock Runtime region; defaults to AWS_REGION or AWS_DEFAULT_REGION",
    )
    demo.add_argument(
        "--embedding-model-id",
        default=os.getenv("BEDROCK_EMBEDDING_MODEL_ID", DEFAULT_EMBEDDING_MODEL_ID),
        help="Titan embedding model; defaults to BEDROCK_EMBEDDING_MODEL_ID",
    )

    migrate = commands.add_parser("migrate", help="apply CockroachDB migrations")
    migrate.add_argument(
        "--database-url",
        default=os.getenv("MIGRATION_DATABASE_URL"),
        help="schema-owner URL; defaults to MIGRATION_DATABASE_URL",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "migrate":
        if not args.database_url:
            parser.error("migrate requires --database-url or MIGRATION_DATABASE_URL")
        with connect_database(args.database_url) as connection:
            applied = apply_migrations(connection)
        print(json.dumps({"applied": applied}, indent=2))
        return 0

    database_url = args.database_url or (os.getenv("DATABASE_URL") if args.cockroach else None)
    if args.cockroach and not database_url:
        parser.error("--cockroach requires DATABASE_URL or --database-url")
    if args.bedrock and not database_url:
        parser.error("--bedrock requires CockroachDB for a durable agent trace")
    if args.bedrock and not args.bedrock_model_id:
        parser.error("--bedrock requires BEDROCK_MODEL_ID or --bedrock-model-id")
    if args.bedrock and not args.aws_region:
        parser.error("--bedrock requires AWS_REGION or --aws-region")
    if args.vector and not database_url:
        parser.error("--vector requires CockroachDB")
    if args.vector and not args.aws_region:
        parser.error("--vector requires AWS_REGION or --aws-region")
    try:
        _run_demo(
            database_url,
            bedrock_model_id=args.bedrock_model_id if args.bedrock else None,
            vector_enabled=args.vector,
            embedding_model_id=args.embedding_model_id,
            aws_region=args.aws_region,
        )
    except (EmbeddingProviderError, InvestigationAgentError) as error:
        failure = {"error": str(error)}
        if isinstance(error, InvestigationAgentError) and error.run_id is not None:
            failure["agent_run_id"] = str(error.run_id)
        print(json.dumps(failure), file=sys.stderr)
        return 1
    return 0


def _run_demo(
    database_url: str | None,
    *,
    bedrock_model_id: str | None = None,
    vector_enabled: bool = False,
    embedding_model_id: str = DEFAULT_EMBEDDING_MODEL_ID,
    aws_region: str | None = None,
) -> None:
    connection = connect_database(database_url) if database_url else None
    try:
        if connection is not None:
            assertion_repository = CockroachAssertionRepository(connection)
            decision_repository = CockroachDecisionRepository(connection)
            remediation_repository = CockroachTelecomRemediationRepository(
                connection,
                connection_factory=lambda: connect_database(database_url),
            )
            backend = "cockroachdb"
        else:
            assertion_repository = InMemoryAssertionRepository()
            decision_repository = InMemoryDecisionRepository()
            remediation_repository = InMemoryTelecomRemediationRepository()
            backend = "in_memory"

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
        )
        if bedrock_model_id:
            if connection is None:
                raise ValueError("Bedrock investigation requires CockroachDB")
            _add_bedrock_investigation(
                payload,
                CockroachAgentRunRepository(
                    connection,
                    connection_factory=lambda: connect_database(database_url),
                ),
                BedrockConverseClient(bedrock_model_id, aws_region),
            )
        print(json.dumps(payload, indent=2, default=_json_default))
    finally:
        if connection is not None:
            connection.close()


def _add_bedrock_investigation(
    payload: dict[str, object],
    repository: CockroachAgentRunRepository,
    client: BedrockConverseClient,
) -> None:
    learning = cast(dict[str, object], payload["learning_proof"])
    context = cast(dict[str, object], learning["investigation_context"])
    result = InvestigationAgent(client, repository).run(
        case_id=UUID(str(context["case_id"])),
        context=context,
    )
    persisted = repository.get(result.run_id)
    calls = repository.tool_calls(result.run_id)
    learning.pop("investigation_context")
    payload["bedrock_investigation"] = {
        "agent_run_id": persisted.id,
        "status": persisted.status,
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


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return str(value.value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


if __name__ == "__main__":
    raise SystemExit(main())
