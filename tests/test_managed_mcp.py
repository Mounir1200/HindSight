import json
from copy import deepcopy
from typing import cast
from uuid import UUID

import pytest

from hindsight.adapters.telecom.remediation import InMemoryTelecomRemediationRepository
from hindsight.agents.investigation import (
    InvestigationContextReadError,
    prepare_investigation_context,
)
from hindsight.core.assertions.repository import InMemoryAssertionRepository
from hindsight.core.decisions.repository import InMemoryDecisionRepository
from hindsight.demo import run_demo_workflow
from hindsight.infrastructure.managed_mcp import (
    MAX_MCP_RESPONSE_BYTES,
    CockroachInvestigationContextStore,
    ManagedMcpInvestigationContextReader,
    _select_arguments,
)

SNAPSHOT_ID = UUID("00000000-0000-0000-0000-000000000010")


class RecordingSelectClient:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[dict[str, str]] = []

    def select(self, *, database: str, query: str) -> object:
        self.calls.append({"database": database, "query": query})
        return self.response


class RecordingCursor:
    def __init__(self, row: dict[str, object]) -> None:
        self._row = row

    def fetchone(self) -> dict[str, object]:
        return self._row


class RecordingConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, statement: str, parameters: tuple[object, ...]) -> RecordingCursor:
        self.calls.append((statement, parameters))
        return RecordingCursor({"id": parameters[0]})


def test_select_arguments_do_not_duplicate_header_scoped_cluster_id() -> None:
    schema = {
        "properties": {
            "cluster_id": {"type": "string"},
            "database": {"type": "string"},
            "query": {"type": "string"},
        },
        "required": ["database", "query"],
    }

    assert _select_arguments(schema, "hindsight", "SELECT 1") == {
        "database": "hindsight",
        "query": "SELECT 1",
    }


def test_managed_mcp_reader_uses_one_case_scoped_select() -> None:
    case_id, context = _demo_context()
    normalized = prepare_investigation_context(case_id, context)
    client = RecordingSelectClient(
        {"structuredContent": {"rows": [{"context": json.dumps(normalized)}]}}
    )

    reader = ManagedMcpInvestigationContextReader(
        client,
        "hindsight",
        SNAPSHOT_ID,
    )
    result = reader.read(case_id)

    assert result == normalized
    assert reader.reference == str(SNAPSHOT_ID)
    assert client.calls == [
        {
            "database": "hindsight",
            "query": (
                "SELECT context::STRING AS context "
                "FROM public.investigation_context_snapshots "
                f"WHERE id = '{SNAPSHOT_ID}' AND case_id = '{case_id}' LIMIT 1"
            ),
        }
    ]


@pytest.mark.parametrize(
    ("response", "code"),
    [
        ({"content": [{"text": "no context"}]}, "managed_mcp_context_missing"),
        ({"padding": "x" * MAX_MCP_RESPONSE_BYTES}, "managed_mcp_response_too_large"),
    ],
)
def test_managed_mcp_reader_fails_closed(response: object, code: str) -> None:
    case_id, _ = _demo_context()
    client = RecordingSelectClient(response)

    with pytest.raises(InvestigationContextReadError) as captured:
        ManagedMcpInvestigationContextReader(
            client,
            "hindsight",
            SNAPSHOT_ID,
        ).read(case_id)

    assert captured.value.code == code
    assert len(client.calls) == 1


def test_context_store_versions_distinct_contexts_for_the_same_case() -> None:
    case_id, context = _demo_context()
    alternate = deepcopy(context)
    guidance = cast(dict[str, object], alternate["procedural_guidance"])
    guidance["retrieval_method"] = "distributed_vector_index"
    connection = RecordingConnection()
    store = CockroachInvestigationContextStore(connection)

    first = store.persist(case_id, context)
    second = store.persist(case_id, alternate)

    assert first.id != second.id
    assert first.content_hash != second.content_hash
    assert len(connection.calls) == 2


def _demo_context() -> tuple[UUID, dict[str, object]]:
    payload = run_demo_workflow(
        InMemoryAssertionRepository(),
        InMemoryDecisionRepository(),
        InMemoryTelecomRemediationRepository(),
        "in_memory",
        include_investigation_context=True,
    )
    learning = cast(dict[str, object], payload["learning_proof"])
    context = cast(dict[str, object], learning["investigation_context"])
    return UUID(str(context["case_id"])), context
