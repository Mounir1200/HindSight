import argparse
import json
import os
import sys
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from hindsight.agents.investigation import InvestigationAgentError
from hindsight.application import execute_demo
from hindsight.infrastructure.database import connect_database
from hindsight.infrastructure.embeddings import (
    DEFAULT_EMBEDDING_MODEL_ID,
    EmbeddingProviderError,
)
from hindsight.infrastructure.migrations import apply_migrations


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
        "--mcp",
        action="store_true",
        help="retrieve the Bedrock investigation context through CockroachDB Managed MCP",
    )
    demo.add_argument(
        "--mcp-cluster-id",
        default=os.getenv("COCKROACH_MCP_CLUSTER_ID"),
        help="CockroachDB Cloud cluster UUID; defaults to COCKROACH_MCP_CLUSTER_ID",
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
    serve = commands.add_parser("serve", help="serve the public API and dashboard")
    serve.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    serve.add_argument("--port", type=_port, default=os.getenv("PORT", "8000"))
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
    if args.command == "serve":
        import uvicorn

        log_level = os.getenv("LOG_LEVEL", "INFO").upper()
        if log_level not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            log_level = "INFO"
        log_config = deepcopy(uvicorn.config.LOGGING_CONFIG)
        log_config["formatters"]["hindsight"] = {"format": "%(message)s"}
        log_config["handlers"]["hindsight"] = {
            "class": "logging.StreamHandler",
            "formatter": "hindsight",
            "stream": "ext://sys.stdout",
        }
        log_config["loggers"]["hindsight.web"] = {
            "handlers": ["hindsight"],
            "level": log_level,
            "propagate": False,
        }
        uvicorn.run(
            "hindsight.web.app:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            access_log=False,
            log_config=log_config,
        )
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
    mcp_api_key = os.getenv("COCKROACH_MCP_API_KEY") if args.mcp else None
    if args.mcp and not args.bedrock:
        parser.error("--mcp requires --bedrock")
    if args.mcp and not args.mcp_cluster_id:
        parser.error("--mcp requires COCKROACH_MCP_CLUSTER_ID or --mcp-cluster-id")
    if args.mcp and not mcp_api_key:
        parser.error("--mcp requires COCKROACH_MCP_API_KEY")
    try:
        payload = execute_demo(
            database_url,
            bedrock_model_id=args.bedrock_model_id if args.bedrock else None,
            vector_enabled=args.vector,
            embedding_model_id=args.embedding_model_id,
            aws_region=args.aws_region,
            mcp_cluster_id=args.mcp_cluster_id if args.mcp else None,
            mcp_api_key=mcp_api_key,
        )
        print(json.dumps(payload, indent=2, default=_json_default))
    except (EmbeddingProviderError, InvestigationAgentError) as error:
        failure = {"error": str(error)}
        if isinstance(error, InvestigationAgentError) and error.run_id is not None:
            failure["agent_run_id"] = str(error.run_id)
        print(json.dumps(failure), file=sys.stderr)
        return 1
    return 0


def _port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65_535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


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
