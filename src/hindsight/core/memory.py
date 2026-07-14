from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from math import isfinite
from typing import Protocol
from uuid import UUID

MEMORY_EMBEDDING_DIMENSIONS = 1_024
DEFAULT_MIN_MEMORY_SIMILARITY = 0.8


@dataclass(frozen=True, slots=True)
class ProceduralMemoryLookup:
    domain: str
    namespace: str
    route: str
    service_type: str
    symptom: str
    applicable_at: datetime
    known_at: datetime
    exclude_source_dispute_id: UUID | None = None
    limit: int = 3

    def __post_init__(self) -> None:
        if not all((self.domain, self.namespace, self.route, self.service_type, self.symptom)):
            raise ValueError("memory lookup fields cannot be empty")
        if self.applicable_at.utcoffset() is None or self.known_at.utcoffset() is None:
            raise ValueError("memory lookup timestamps must be timezone-aware")
        if not 1 <= self.limit <= 20:
            raise ValueError("memory lookup limit must be between 1 and 20")


@dataclass(frozen=True, slots=True)
class ProceduralMemoryHit:
    memory_id: UUID
    memory_key: str
    source_dispute_id: UUID
    corrected_assertion_id: UUID
    remediation_run_id: UUID
    root_cause: str
    content: str
    checklist: tuple[str, ...]
    recorded_at: datetime
    rank: int
    score: float | None = None

    def __post_init__(self) -> None:
        if (
            not self.memory_key
            or not self.root_cause
            or not self.content
            or not self.checklist
            or any(not item for item in self.checklist)
        ):
            raise ValueError("procedural memory content cannot be empty")
        if self.recorded_at.utcoffset() is None:
            raise ValueError("recorded_at must be timezone-aware")
        if self.rank <= 0:
            raise ValueError("memory rank must be positive")
        if self.score is not None and (not isfinite(self.score) or not -1 <= self.score <= 1):
            raise ValueError("memory score must be between -1 and 1")


@dataclass(frozen=True, slots=True)
class ProceduralMemoryRetrieval:
    lookup: ProceduralMemoryLookup
    method: str
    hits: tuple[ProceduralMemoryHit, ...]


class ProceduralMemoryReader(Protocol):
    def retrieve(
        self,
        lookup: ProceduralMemoryLookup,
    ) -> ProceduralMemoryRetrieval: ...


@dataclass(frozen=True, slots=True)
class TextEmbedding:
    values: tuple[float, ...]
    model_id: str
    input_tokens: int

    def __post_init__(self) -> None:
        if not self.model_id:
            raise ValueError("embedding model_id cannot be empty")
        if len(self.values) != MEMORY_EMBEDDING_DIMENSIONS:
            raise ValueError("embedding has an unexpected number of dimensions")
        if any(
            isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value)
            for value in self.values
        ):
            raise ValueError("embedding values must be finite numbers")
        if self.input_tokens < 0:
            raise ValueError("embedding input_tokens cannot be negative")

    @property
    def dimensions(self) -> int:
        return len(self.values)


class TextEmbedder(Protocol):
    @property
    def model_id(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    def embed(self, text: str) -> TextEmbedding: ...


@dataclass(frozen=True, slots=True)
class MemoryEmbeddingSource:
    memory_id: UUID
    domain: str
    namespace: str
    kind: str
    route: str
    service_type: str
    symptom: str
    root_cause: str
    content: str
    checklist: tuple[str, ...]

    def __post_init__(self) -> None:
        values = (
            self.domain,
            self.namespace,
            self.kind,
            self.route,
            self.service_type,
            self.symptom,
            self.root_cause,
            self.content,
        )
        if not all(values) or not self.checklist or any(not item for item in self.checklist):
            raise ValueError("memory embedding source cannot contain empty text")


@dataclass(frozen=True, slots=True)
class MemoryEmbeddingReceipt:
    memory_id: UUID
    model_id: str
    content_sha256: str
    input_tokens: int
    embedded_at: datetime
    status: str = "indexed"
    dimensions: int = MEMORY_EMBEDDING_DIMENSIONS

    def __post_init__(self) -> None:
        if not self.model_id or len(self.content_sha256) != 64:
            raise ValueError("memory embedding identity is invalid")
        if self.status not in {"indexed", "already_indexed"}:
            raise ValueError("memory embedding status is invalid")
        if self.input_tokens < 0 or self.dimensions != MEMORY_EMBEDDING_DIMENSIONS:
            raise ValueError("memory embedding metadata is invalid")
        if self.embedded_at.utcoffset() is None:
            raise ValueError("embedded_at must be timezone-aware")


class ProceduralMemoryVectorStore(Protocol):
    def source(self, memory_id: UUID) -> MemoryEmbeddingSource: ...

    def existing(
        self,
        memory_id: UUID,
        model_id: str,
    ) -> MemoryEmbeddingReceipt | None: ...

    def store(
        self,
        source: MemoryEmbeddingSource,
        embedding: TextEmbedding,
        content_sha256: str,
        embedded_at: datetime,
    ) -> MemoryEmbeddingReceipt: ...

    def retrieve(
        self,
        lookup: ProceduralMemoryLookup,
        embedding: TextEmbedding,
    ) -> ProceduralMemoryRetrieval: ...


class ProceduralMemoryIndexer(Protocol):
    def index(self, memory_id: UUID) -> MemoryEmbeddingReceipt: ...


class SemanticProceduralMemory(ProceduralMemoryReader, ProceduralMemoryIndexer):
    def __init__(
        self,
        store: ProceduralMemoryVectorStore,
        embedder: TextEmbedder,
        fallback: ProceduralMemoryReader,
        *,
        min_similarity: float = DEFAULT_MIN_MEMORY_SIMILARITY,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if embedder.dimensions != MEMORY_EMBEDDING_DIMENSIONS:
            raise ValueError("embedder dimensions do not match the vector schema")
        if not isfinite(min_similarity) or not 0 <= min_similarity <= 1:
            raise ValueError("min_similarity must be between 0 and 1")
        self._store = store
        self._embedder = embedder
        self._fallback = fallback
        self._min_similarity = min_similarity
        self._clock = clock or (lambda: datetime.now(UTC))

    def index(self, memory_id: UUID) -> MemoryEmbeddingReceipt:
        source = self._store.source(memory_id)
        document = _memory_document(source)
        digest = sha256(document.encode()).hexdigest()
        existing = self._store.existing(memory_id, self._embedder.model_id)
        if existing is not None and existing.content_sha256 == digest:
            return replace(existing, status="already_indexed")

        embedding = self._embed(document)
        return self._store.store(source, embedding, digest, self._clock())

    def retrieve(
        self,
        lookup: ProceduralMemoryLookup,
    ) -> ProceduralMemoryRetrieval:
        embedding = self._embed(_lookup_document(lookup))
        retrieval = self._store.retrieve(lookup, embedding)
        hits = tuple(
            hit
            for hit in retrieval.hits
            if hit.score is not None and hit.score >= self._min_similarity
        )
        return replace(retrieval, hits=hits) if hits else self._fallback.retrieve(lookup)

    def _embed(self, text: str) -> TextEmbedding:
        embedding = self._embedder.embed(text)
        if embedding.model_id != self._embedder.model_id:
            raise ValueError("embedder returned a different model_id")
        return embedding


def _memory_document(source: MemoryEmbeddingSource) -> str:
    checklist = "; ".join(source.checklist)
    return "\n".join(
        (
            f"Domain: {source.domain}",
            f"Route: {source.route}",
            f"Service: {source.service_type}",
            f"Symptom: {source.symptom}",
            f"Root cause: {source.root_cause}",
            f"Procedure: {source.content}",
            f"Checklist: {checklist}",
        )
    )


def _lookup_document(lookup: ProceduralMemoryLookup) -> str:
    return "\n".join(
        (
            f"Domain: {lookup.domain}",
            f"Route: {lookup.route}",
            f"Service: {lookup.service_type}",
            f"Symptom: {lookup.symptom}",
        )
    )
