from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import hindsight.web.memory_search as memory_search_module
from hindsight.core.memory import (
    MEMORY_EMBEDDING_DIMENSIONS,
    ProceduralMemoryHit,
    ProceduralMemoryLookup,
    ProceduralMemoryRetrieval,
    TextEmbedding,
)
from hindsight.web.memory_search import (
    CockroachMemorySearchReader,
    MemoryAccessScope,
    MemorySearchRuntimeConfig,
    StaticAgentMemoryPolicy,
    build_memory_search_reader,
    create_memory_search_router,
)

NOW = datetime(2026, 7, 3, 1, tzinfo=UTC)
MEMORY_ID = UUID("10000000-0000-0000-0000-000000000001")
DISPUTE_ID = UUID("10000000-0000-0000-0000-000000000002")
ASSERTION_ID = UUID("10000000-0000-0000-0000-000000000003")
RUN_ID = UUID("10000000-0000-0000-0000-000000000004")


class RecordingReader:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.lookups = []
        self.failure = failure

    def retrieve(self, lookup):
        self.lookups.append(lookup)
        if self.failure is not None:
            raise self.failure
        hits = tuple(_hit(rank) for rank in range(1, 25))
        return ProceduralMemoryRetrieval(lookup, "distributed_vector_index", hits)


def _client(reader=None, policy=None) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_memory_search_router(
            reader or RecordingReader(),
            **({"policy": policy} if policy is not None else {}),
        )
    )
    return TestClient(app)


def _params(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "agent_id": "investigation_agent",
        "route": "FR->SN",
        "service_type": "voice",
        "symptom": "A corrected tariff arrived after billing.",
        "applicable_at": NOW.isoformat(),
        "known_at": NOW.isoformat(),
        "limit": 2,
    }
    values.update(overrides)
    return values


def test_search_derives_scope_and_bounds_results() -> None:
    reader = RecordingReader()
    response = _client(reader).get("/memories/search", params=_params())

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["scope"] == {
        "domain": "telecom",
        "namespace": "revenue_assurance",
    }
    assert payload["retrieval_method"] == "distributed_vector_index"
    assert payload["count"] == 2
    assert len(payload["hits"]) == 2
    assert reader.lookups[0].namespace == "revenue_assurance"
    assert reader.lookups[0].limit == 2


def test_namespace_is_not_client_controlled_and_unknown_agents_are_denied() -> None:
    client = _client()

    assert (
        client.get(
            "/memories/search",
            params=_params(namespace="attacker_scope"),
        ).status_code
        == 422
    )
    denied = client.get(
        "/memories/search",
        params=_params(agent_id="billing_agent"),
    )
    assert denied.status_code == 403
    assert denied.json() == {"detail": "memory_access_denied"}


@pytest.mark.parametrize(
    "overrides",
    [
        {"limit": 21},
        {"agent_id": "InvestigationAgent"},
        {"route": " "},
        {"route": "r" * 129},
        {"symptom": "s" * 513},
        {"known_at": "2026-07-03T01:00:00"},
    ],
)
def test_search_rejects_invalid_or_unbounded_queries(overrides: dict[str, object]) -> None:
    assert _client().get("/memories/search", params=_params(**overrides)).status_code == 422


def test_policy_is_injectable_without_exposing_namespace_as_input() -> None:
    reader = RecordingReader()
    policy = StaticAgentMemoryPolicy({"review_agent": MemoryAccessScope("telecom", "review_only")})
    response = _client(reader, policy).get(
        "/memories/search",
        params=_params(agent_id="review_agent"),
    )

    assert response.status_code == 200
    assert reader.lookups[0].namespace == "review_only"


def test_backend_failures_return_a_stable_safe_error() -> None:
    response = _client(RecordingReader(failure=RuntimeError("password=do-not-leak"))).get(
        "/memories/search",
        params=_params(),
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "memory_search_unavailable"}
    assert "do-not-leak" not in response.text


def test_runtime_keeps_titan_off_until_explicitly_enabled() -> None:
    default = MemorySearchRuntimeConfig.from_environment({})
    enabled = MemorySearchRuntimeConfig.from_environment(
        {
            "HINDSIGHT_DEMO_VECTOR": "true",
            "BEDROCK_EMBEDDING_MODEL_ID": "test-titan",
            "AWS_REGION": "eu-central-1",
        }
    )

    assert default.vector_enabled is False
    assert enabled.vector_enabled is True
    assert enabled.embedding_model_id == "test-titan"
    assert enabled.aws_region == "eu-central-1"


def test_runtime_rejects_ambiguous_vector_flag() -> None:
    with pytest.raises(ValueError, match="must be true or false"):
        MemorySearchRuntimeConfig.from_environment({"HINDSIGHT_DEMO_VECTOR": "sometimes"})


def test_reader_builder_keeps_route_available_without_a_database() -> None:
    response = _client(build_memory_search_reader(None, {})).get(
        "/memories/search",
        params=_params(),
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "memory_search_unavailable"}


class QueryResult:
    def __init__(self, rows=()) -> None:
        self._rows = rows

    def fetchall(self):
        return self._rows


class RecordingConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []
        self.closed = False

    def execute(self, sql, parameters):
        self.calls.append((sql, parameters))
        return QueryResult()

    def close(self) -> None:
        self.closed = True


class RecordingEmbedder:
    model_id = "test-titan"
    dimensions = MEMORY_EMBEDDING_DIMENSIONS

    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed(self, text: str) -> TextEmbedding:
        self.calls.append(text)
        return TextEmbedding(
            (1.0,) + (0.0,) * (MEMORY_EMBEDDING_DIMENSIONS - 1),
            self.model_id,
            4,
        )


def test_durable_reader_uses_parameterized_exact_search_without_titan_by_default(
    monkeypatch,
) -> None:
    connection = RecordingConnection()

    def forbidden_embedder(*args, **kwargs):
        raise AssertionError("Titan must remain disabled")

    monkeypatch.setattr(
        memory_search_module,
        "BedrockTitanTextEmbedder",
        forbidden_embedder,
    )

    reader = CockroachMemorySearchReader(
        "postgresql://not-exposed",
        connection_factory=lambda: connection,
    )
    lookup = _lookup_from_request(reader)

    assert lookup.method == "structured_exact"
    assert connection.closed is True
    assert len(connection.calls) == 1
    sql, parameters = connection.calls[0]
    assert "memory.namespace = %s" in sql
    assert "revenue_assurance" not in sql
    assert parameters[1] == "revenue_assurance"


def test_durable_reader_uses_vector_store_and_embedder_only_when_enabled() -> None:
    connection = RecordingConnection()
    embedder = RecordingEmbedder()
    reader = CockroachMemorySearchReader(
        "postgresql://not-exposed",
        vector_enabled=True,
        embedding_model_id=embedder.model_id,
        connection_factory=lambda: connection,
        embedder_factory=lambda: embedder,
    )

    result = _lookup_from_request(reader)

    assert result.method == "structured_exact"
    assert len(embedder.calls) == 1
    assert any("FROM memory_embeddings" in sql for sql, _ in connection.calls)
    assert connection.closed is True


def _lookup_from_request(reader: CockroachMemorySearchReader):
    return reader.retrieve(
        ProceduralMemoryLookup(
            domain="telecom",
            namespace="revenue_assurance",
            route="FR->SN",
            service_type="voice",
            symptom="A corrected tariff arrived after billing.",
            applicable_at=NOW,
            known_at=NOW,
            limit=2,
        )
    )


def _hit(rank: int) -> ProceduralMemoryHit:
    return ProceduralMemoryHit(
        memory_id=MEMORY_ID,
        memory_key="telecom:procedure",
        source_dispute_id=DISPUTE_ID,
        corrected_assertion_id=ASSERTION_ID,
        remediation_run_id=RUN_ID,
        root_cause="delayed_tariff_ingestion",
        content="Compare valid and recorded time before remediation.",
        checklist=("Reconstruct temporal knowledge",),
        recorded_at=NOW,
        rank=rank,
        score=0.92,
    )
