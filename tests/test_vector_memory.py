from datetime import UTC, datetime
from uuid import UUID

from hindsight.core.memory import (
    MEMORY_EMBEDDING_DIMENSIONS,
    MemoryEmbeddingReceipt,
    MemoryEmbeddingSource,
    ProceduralMemoryHit,
    ProceduralMemoryLookup,
    ProceduralMemoryRetrieval,
    SemanticProceduralMemory,
    TextEmbedding,
)

MEMORY_ID = UUID("10000000-0000-0000-0000-000000000001")
DISPUTE_ID = UUID("10000000-0000-0000-0000-000000000002")
ASSERTION_ID = UUID("10000000-0000-0000-0000-000000000003")
RUN_ID = UUID("10000000-0000-0000-0000-000000000004")
NOW = datetime(2026, 7, 3, 1, tzinfo=UTC)


class FakeEmbedder:
    model_id = "test-embedding-model"
    dimensions = MEMORY_EMBEDDING_DIMENSIONS

    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed(self, text: str) -> TextEmbedding:
        self.calls.append(text)
        return TextEmbedding(
            (1.0,) + (0.0,) * (MEMORY_EMBEDDING_DIMENSIONS - 1),
            self.model_id,
            5,
        )


class FakeVectorStore:
    def __init__(self) -> None:
        self.receipt: MemoryEmbeddingReceipt | None = None
        self.return_hit = True
        self.score = 0.92

    def source(self, memory_id: UUID) -> MemoryEmbeddingSource:
        assert memory_id == MEMORY_ID
        return MemoryEmbeddingSource(
            memory_id=memory_id,
            domain="telecom",
            namespace="revenue_assurance",
            kind="procedure",
            route="FR->SN",
            service_type="voice",
            symptom="A corrected tariff arrived after billing.",
            root_cause="delayed_tariff_ingestion",
            content="Compare valid and recorded time before remediation.",
            checklist=("Reconstruct temporal knowledge", "Correct only the overcharge"),
        )

    def existing(
        self,
        memory_id: UUID,
        model_id: str,
    ) -> MemoryEmbeddingReceipt | None:
        return self.receipt

    def store(
        self,
        source: MemoryEmbeddingSource,
        embedding: TextEmbedding,
        content_sha256: str,
        embedded_at: datetime,
    ) -> MemoryEmbeddingReceipt:
        self.receipt = MemoryEmbeddingReceipt(
            source.memory_id,
            embedding.model_id,
            content_sha256,
            embedding.input_tokens,
            embedded_at,
        )
        return self.receipt

    def retrieve(
        self,
        lookup: ProceduralMemoryLookup,
        embedding: TextEmbedding,
    ) -> ProceduralMemoryRetrieval:
        hits = (_hit(self.score),) if self.return_hit else ()
        return ProceduralMemoryRetrieval(lookup, "distributed_vector_index", hits)


class ExactFallback:
    def retrieve(self, lookup: ProceduralMemoryLookup) -> ProceduralMemoryRetrieval:
        return ProceduralMemoryRetrieval(lookup, "structured_exact", (_hit(None),))


def test_semantic_memory_indexes_once_then_uses_vector_or_exact_fallback() -> None:
    embedder = FakeEmbedder()
    store = FakeVectorStore()
    service = SemanticProceduralMemory(
        store,
        embedder,
        ExactFallback(),
        clock=lambda: NOW,
    )
    lookup = ProceduralMemoryLookup(
        domain="telecom",
        namespace="revenue_assurance",
        route="FR->SN",
        service_type="voice",
        symptom="A tariff correction was ingested too late.",
        applicable_at=NOW,
        known_at=NOW,
    )

    assert service.index(MEMORY_ID).status == "indexed"
    assert service.index(MEMORY_ID).status == "already_indexed"
    assert len(embedder.calls) == 1

    vector = service.retrieve(lookup)
    assert vector.method == "distributed_vector_index"
    assert vector.hits[0].score == 0.92

    store.score = 0.5
    fallback = service.retrieve(lookup)
    assert fallback.method == "structured_exact"
    assert fallback.hits[0].score is None


def _hit(score: float | None) -> ProceduralMemoryHit:
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
        rank=1,
        score=score,
    )
