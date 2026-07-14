import json
import random
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from threading import RLock
from typing import Any
from uuid import UUID

from hindsight.core.memory import (
    MEMORY_EMBEDDING_DIMENSIONS,
    MemoryEmbeddingReceipt,
    MemoryEmbeddingSource,
    ProceduralMemoryHit,
    ProceduralMemoryLookup,
    ProceduralMemoryRetrieval,
    TextEmbedding,
)

MEMORY_VECTOR_INDEX_NAME = "memory_embeddings_cosine_idx"
MIN_VECTOR_CANDIDATES = 20
MAX_VECTOR_CANDIDATES = 200

SELECT_MEMORY_SOURCE_SQL = """
SELECT
  memory.id,
  memory.domain,
  memory.namespace,
  memory.kind,
  memory.content,
  memory.content_struct,
  cdr.route,
  cdr.service_type,
  dispute.claim
FROM memories AS memory
JOIN telecom_refunds AS refund
  ON refund.remediation_run_id = memory.remediation_run_id
JOIN telecom_disputes AS dispute ON dispute.id = refund.dispute_id
JOIN telecom_invoices AS invoice ON invoice.id = dispute.invoice_id
JOIN telecom_cdrs AS cdr ON cdr.id = invoice.cdr_id
WHERE memory.id = %s
"""

SELECT_EMBEDDING_SQL = """
SELECT
  memory_id,
  model_id,
  content_sha256,
  input_tokens,
  embedded_at,
  vector_dims(embedding) AS dimensions
FROM memory_embeddings
WHERE memory_id = %s AND model_id = %s
"""

UPSERT_EMBEDDING_SQL = """
INSERT INTO memory_embeddings (
  memory_id,
  model_id,
  domain,
  namespace,
  kind,
  route,
  service_type,
  content_sha256,
  embedding,
  input_tokens,
  embedded_at
)
VALUES (
  %s, %s, %s, %s, %s, %s, %s, %s,
  CAST(%s AS VECTOR(1024)), %s, %s
)
ON CONFLICT (memory_id, model_id) DO UPDATE SET
  domain = excluded.domain,
  namespace = excluded.namespace,
  kind = excluded.kind,
  route = excluded.route,
  service_type = excluded.service_type,
  content_sha256 = excluded.content_sha256,
  embedding = excluded.embedding,
  input_tokens = excluded.input_tokens,
  embedded_at = excluded.embedded_at
RETURNING
  memory_id,
  model_id,
  content_sha256,
  input_tokens,
  embedded_at,
  vector_dims(embedding) AS dimensions
"""

SELECT_VECTOR_MEMORIES_SQL = """
WITH candidates AS MATERIALIZED (
  SELECT
    memory_id,
    embedding <=> CAST(%s AS VECTOR(1024)) AS distance
  FROM memory_embeddings
  WHERE domain = %s
    AND namespace = %s
    AND kind = 'procedure'
    AND model_id = %s
    AND route = %s
    AND service_type = %s
  ORDER BY embedding <=> CAST(%s AS VECTOR(1024))
  LIMIT %s
)
SELECT
  memory.id,
  memory.memory_key,
  memory.remediation_run_id,
  memory.content,
  memory.content_struct,
  memory.recorded_at,
  candidate.distance
FROM candidates AS candidate
JOIN memories AS memory ON memory.id = candidate.memory_id
JOIN telecom_refunds AS refund
  ON refund.remediation_run_id = memory.remediation_run_id
WHERE memory.valid_from <= %s
  AND (memory.valid_until IS NULL OR memory.valid_until > %s)
  AND memory.recorded_at <= %s
  AND (memory.superseded_at IS NULL OR memory.superseded_at > %s)
  AND (
    CAST(%s AS UUID) IS NULL
    OR refund.dispute_id <> %s
  )
ORDER BY candidate.distance, memory.confidence DESC, memory.recorded_at DESC, memory.id
LIMIT %s
"""


class CockroachTelecomVectorMemoryStore:
    def __init__(
        self,
        connection: Any,
        max_retries: int = 3,
        connection_factory: Callable[[], Any] | None = None,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        self._connection = connection
        self._max_retries = max_retries
        self._connection_factory = connection_factory
        self._lock = RLock()

    def source(self, memory_id: UUID) -> MemoryEmbeddingSource:
        with self._lock:
            row = self._connection.execute(
                SELECT_MEMORY_SOURCE_SQL,
                (memory_id,),
            ).fetchone()
        if row is None:
            raise MemoryEmbeddingNotFoundError(f"memory {memory_id} was not found")
        structure = _json_object(row["content_struct"])
        checklist = structure.get("checklist")
        if not isinstance(checklist, list) or not all(
            isinstance(item, str) and item for item in checklist
        ):
            raise ValueError("procedural memory checklist is invalid")
        return MemoryEmbeddingSource(
            memory_id=UUID(str(row["id"])),
            domain=str(row["domain"]),
            namespace=str(row["namespace"]),
            kind=str(row["kind"]),
            route=str(row["route"]),
            service_type=str(row["service_type"]),
            symptom=str(row["claim"]),
            root_cause=str(structure["root_cause"]),
            content=str(row["content"]),
            checklist=tuple(checklist),
        )

    def existing(
        self,
        memory_id: UUID,
        model_id: str,
    ) -> MemoryEmbeddingReceipt | None:
        with self._lock:
            row = self._connection.execute(
                SELECT_EMBEDDING_SQL,
                (memory_id, model_id),
            ).fetchone()
        return _receipt(row, "already_indexed") if row is not None else None

    def store(
        self,
        source: MemoryEmbeddingSource,
        embedding: TextEmbedding,
        content_sha256: str,
        embedded_at: datetime,
    ) -> MemoryEmbeddingReceipt:
        values = (
            source.memory_id,
            embedding.model_id,
            source.domain,
            source.namespace,
            source.kind,
            source.route,
            source.service_type,
            content_sha256,
            _vector_literal(embedding.values),
            embedding.input_tokens,
            embedded_at,
        )
        with self._lock:
            try:
                return self._retry(lambda: self._store_once(values))
            except Exception as error:
                if getattr(error, "sqlstate", None) != "40003":
                    raise
                return self._recover_ambiguous_store(
                    source.memory_id,
                    embedding,
                    content_sha256,
                    embedded_at,
                    values,
                )

    def _store_once(self, values: tuple[object, ...]) -> MemoryEmbeddingReceipt:
        with self._connection.transaction():
            row = self._connection.execute(UPSERT_EMBEDDING_SQL, values).fetchone()
        if row is None:
            raise VectorMemoryStateError("memory embedding was not persisted")
        return _receipt(row, "indexed")

    def retrieve(
        self,
        lookup: ProceduralMemoryLookup,
        embedding: TextEmbedding,
    ) -> ProceduralMemoryRetrieval:
        vector = _vector_literal(embedding.values)
        candidate_limit = min(
            MAX_VECTOR_CANDIDATES,
            max(MIN_VECTOR_CANDIDATES, lookup.limit * 10),
        )
        with self._lock:
            rows = self._retrieve_rows(lookup, embedding, vector, candidate_limit)
            if len(rows) < lookup.limit and candidate_limit < MAX_VECTOR_CANDIDATES:
                rows = self._retrieve_rows(
                    lookup,
                    embedding,
                    vector,
                    MAX_VECTOR_CANDIDATES,
                )
        hits = tuple(_memory_hit_from_row(row, rank) for rank, row in enumerate(rows, start=1))
        return ProceduralMemoryRetrieval(lookup, "distributed_vector_index", hits)

    def _retrieve_rows(
        self,
        lookup: ProceduralMemoryLookup,
        embedding: TextEmbedding,
        vector: str,
        candidate_limit: int,
    ) -> list[Mapping[str, Any]]:
        excluded = lookup.exclude_source_dispute_id
        return list(
            self._connection.execute(
                SELECT_VECTOR_MEMORIES_SQL,
                (
                    vector,
                    lookup.domain,
                    lookup.namespace,
                    embedding.model_id,
                    lookup.route,
                    lookup.service_type,
                    vector,
                    candidate_limit,
                    lookup.applicable_at,
                    lookup.applicable_at,
                    lookup.known_at,
                    lookup.known_at,
                    excluded,
                    excluded,
                    lookup.limit,
                ),
            ).fetchall()
        )

    def _retry[T](self, operation: Callable[[], T]) -> T:
        for attempt in range(self._max_retries + 1):
            try:
                return operation()
            except Exception as error:
                if getattr(error, "sqlstate", None) != "40001" or attempt == self._max_retries:
                    raise
                time.sleep(min(0.5, 0.05 * 2**attempt) + random.uniform(0, 0.01))
        raise RuntimeError("unreachable retry state")

    def _recover_ambiguous_store(
        self,
        memory_id: UUID,
        embedding: TextEmbedding,
        content_sha256: str,
        embedded_at: datetime,
        values: tuple[object, ...],
    ) -> MemoryEmbeddingReceipt:
        if self._connection_factory is None:
            raise VectorMemoryStateError(
                "embedding commit outcome is unknown; retry with a fresh connection"
            )
        with self._connection_factory() as connection:
            repository = CockroachTelecomVectorMemoryStore(
                connection,
                max_retries=self._max_retries,
            )
            existing = repository.existing(memory_id, embedding.model_id)
            if existing is not None and (
                existing.content_sha256 == content_sha256
                and existing.input_tokens == embedding.input_tokens
                and existing.embedded_at == embedded_at
            ):
                return existing
            return repository._retry(lambda: repository._store_once(values))


def _receipt(row: Mapping[str, Any], status: str) -> MemoryEmbeddingReceipt:
    return MemoryEmbeddingReceipt(
        memory_id=UUID(str(row["memory_id"])),
        model_id=str(row["model_id"]),
        content_sha256=str(row["content_sha256"]),
        input_tokens=int(row["input_tokens"]),
        embedded_at=row["embedded_at"],
        status=status,
        dimensions=int(row["dimensions"]),
    )


def _memory_hit_from_row(
    row: Mapping[str, Any],
    rank: int,
) -> ProceduralMemoryHit:
    structure = _json_object(row["content_struct"])
    checklist = structure.get("checklist")
    if not isinstance(checklist, list) or not all(
        isinstance(item, str) and item for item in checklist
    ):
        raise ValueError("procedural memory checklist is invalid")
    score = max(-1.0, min(1.0, 1.0 - float(row["distance"])))
    return ProceduralMemoryHit(
        memory_id=UUID(str(row["id"])),
        memory_key=str(row["memory_key"]),
        source_dispute_id=UUID(str(structure["source_dispute_id"])),
        corrected_assertion_id=UUID(str(structure["corrected_assertion_id"])),
        remediation_run_id=UUID(str(row["remediation_run_id"])),
        root_cause=str(structure["root_cause"]),
        content=str(row["content"]),
        checklist=tuple(checklist),
        recorded_at=row["recorded_at"],
        rank=rank,
        score=score,
    )


def _vector_literal(values: tuple[float, ...]) -> str:
    if len(values) != MEMORY_EMBEDDING_DIMENSIONS:
        raise ValueError("vector dimensions do not match the memory schema")
    return "[" + ",".join(format(value, ".12g") for value in values) + "]"


def _json_object(value: object) -> dict[str, object]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("memory JSON must be an object")
    return value


class MemoryEmbeddingNotFoundError(LookupError):
    pass


class VectorMemoryStateError(RuntimeError):
    pass
