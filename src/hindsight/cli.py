import argparse
import json
import os
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from hindsight.adapters.telecom.remediation import InMemoryTelecomRemediationRepository
from hindsight.core.assertions.repository import (
    CockroachAssertionRepository,
    InMemoryAssertionRepository,
)
from hindsight.core.decisions.repository import (
    CockroachDecisionRepository,
    InMemoryDecisionRepository,
)
from hindsight.demo import run_demo_workflow
from hindsight.infrastructure.database import connect_database
from hindsight.infrastructure.migrations import apply_migrations
from hindsight.infrastructure.telecom_remediation import (
    CockroachTelecomRemediationRepository,
)


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
    _run_demo(database_url)
    return 0


def _run_demo(database_url: str | None) -> None:
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

        payload = run_demo_workflow(
            assertion_repository,
            decision_repository,
            remediation_repository,
            backend,
        )
        print(json.dumps(payload, indent=2, default=_json_default))
    finally:
        if connection is not None:
            connection.close()


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
