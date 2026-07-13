import argparse
import json
import os
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from hindsight.adapters.telecom.billing import TelecomAdapter
from hindsight.adapters.telecom.seed import (
    DEMO_DECISION_TIME,
    DEMO_EVENT_TIME,
    DEMO_TARIFF_KEY,
    demo_call,
    seed_demo,
)
from hindsight.core.assertions.repository import (
    CockroachAssertionRepository,
    InMemoryAssertionRepository,
)
from hindsight.core.assertions.service import TemporalAssertionService
from hindsight.core.decisions.service import DecisionAuditService
from hindsight.infrastructure.database import connect_database
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
            repository = CockroachAssertionRepository(connection)
            backend = "cockroachdb"
        else:
            repository = InMemoryAssertionRepository()
            backend = "in_memory"

        assertions = TemporalAssertionService(repository)
        seed_demo(assertions)
        audit = DecisionAuditService(assertions, TelecomAdapter()).audit(
            event=demo_call(),
            subject_id=DEMO_TARIFF_KEY,
            event_time=DEMO_EVENT_TIME,
            decision_time=DEMO_DECISION_TIME,
        )
        payload = {
            "scenario": "retroactive_telecom_rate",
            "backend": backend,
            "current_truth": {
                "rate": audit.snapshot.current_truth.value_number,
                "valid_from": audit.snapshot.current_truth.valid_from,
                "recorded_at": audit.snapshot.current_truth.recorded_at,
            },
            "known_at_decision": {
                "rate": audit.snapshot.known_at_decision.value_number,
                "decision_time": audit.lookup.decision_time,
            },
            "decision": {
                "selected_rate": audit.decision.selected_value,
                **audit.decision.output,
            },
            "comparison": audit.comparison.details,
            "verdict": {
                "category": audit.verdict.verdict,
                "agent_fault": audit.verdict.agent_fault,
                "knowledge_gap_seconds": audit.verdict.knowledge_gap_seconds,
                "root_cause": audit.verdict.root_cause,
            },
        }
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
