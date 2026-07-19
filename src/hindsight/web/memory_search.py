import os
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, Protocol
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from hindsight.core.memory import (
    ProceduralMemoryHit,
    ProceduralMemoryLookup,
    ProceduralMemoryReader,
    ProceduralMemoryRetrieval,
    SemanticProceduralMemory,
    TextEmbedder,
)
from hindsight.infrastructure.database import connect_database
from hindsight.infrastructure.embeddings import (
    DEFAULT_EMBEDDING_MODEL_ID,
    BedrockTitanTextEmbedder,
)
from hindsight.infrastructure.telecom_remediation import (
    CockroachTelecomRemediationRepository,
)
from hindsight.infrastructure.vector_memory import CockroachTelecomVectorMemoryStore

MAX_AGENT_ID_LENGTH = 64
MAX_ROUTE_LENGTH = 128
MAX_SERVICE_TYPE_LENGTH = 64
MAX_SYMPTOM_LENGTH = 512
MAX_MEMORY_KEY_LENGTH = 256
MAX_ROOT_CAUSE_LENGTH = 256
MAX_CONTENT_LENGTH = 4_096
MAX_CHECKLIST_ITEMS = 20
MAX_CHECKLIST_ITEM_LENGTH = 512
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off", ""})
_QUERY_FIELDS = frozenset(
    {
        "agent_id",
        "route",
        "service_type",
        "symptom",
        "applicable_at",
        "known_at",
        "exclude_source_dispute_id",
        "limit",
    }
)


def _validate_server_text(value: str, field: str, max_length: int) -> None:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise ValueError(f"{field} is invalid")


def _validate_query_text(value: str, field: str, max_length: int) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise ValueError(f"{field} contains surrounding whitespace")
    _validate_server_text(value, field, max_length)
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{field} contains control characters")
    return value


def _validate_agent_id(value: str) -> str:
    value = _validate_query_text(value, "agent_id", MAX_AGENT_ID_LENGTH)
    if (
        not value.isascii()
        or not value[0].islower()
        or any(
            not (character.islower() or character.isdigit() or character == "_")
            for character in value
        )
    ):
        raise ValueError("agent_id is invalid")
    return value


@dataclass(frozen=True, slots=True)
class MemoryAccessScope:
    domain: str
    namespace: str

    def __post_init__(self) -> None:
        _validate_query_text(self.domain, "domain", 64)
        _validate_query_text(self.namespace, "namespace", 128)


class AgentMemoryPolicy(Protocol):
    def scope_for(self, agent_id: str) -> MemoryAccessScope: ...


class StaticAgentMemoryPolicy:
    def __init__(self, scopes: Mapping[str, MemoryAccessScope]) -> None:
        if not scopes:
            raise ValueError("memory policy must contain at least one agent")
        self._scopes = {_validate_agent_id(agent_id): scope for agent_id, scope in scopes.items()}

    def scope_for(self, agent_id: str) -> MemoryAccessScope:
        try:
            return self._scopes[agent_id]
        except KeyError as error:
            raise MemoryAccessDeniedError from error


DEFAULT_MEMORY_POLICY = StaticAgentMemoryPolicy(
    {
        "investigation_agent": MemoryAccessScope(
            domain="telecom",
            namespace="revenue_assurance",
        )
    }
)


@dataclass(frozen=True, slots=True)
class MemorySearchRuntimeConfig:
    vector_enabled: bool = False
    embedding_model_id: str = DEFAULT_EMBEDDING_MODEL_ID
    aws_region: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.vector_enabled, bool):
            raise ValueError("vector_enabled must be a boolean")
        _validate_query_text(self.embedding_model_id, "embedding_model_id", 256)
        if self.aws_region is not None:
            _validate_query_text(self.aws_region, "aws_region", 64)

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> "MemorySearchRuntimeConfig":
        values = environment if environment is not None else os.environ
        return cls(
            vector_enabled=_flag(values, "HINDSIGHT_DEMO_VECTOR"),
            embedding_model_id=(
                _optional(values, "BEDROCK_EMBEDDING_MODEL_ID") or DEFAULT_EMBEDDING_MODEL_ID
            ),
            aws_region=_optional(values, "AWS_REGION"),
        )

    def reader(self, database_url: str) -> "CockroachMemorySearchReader":
        return CockroachMemorySearchReader(
            database_url,
            vector_enabled=self.vector_enabled,
            embedding_model_id=self.embedding_model_id,
            aws_region=self.aws_region,
        )


class CockroachMemorySearchReader:
    def __init__(
        self,
        database_url: str,
        *,
        vector_enabled: bool = False,
        embedding_model_id: str = DEFAULT_EMBEDDING_MODEL_ID,
        aws_region: str | None = None,
        connection_factory: Callable[[], Any] | None = None,
        embedder_factory: Callable[[], TextEmbedder] | None = None,
    ) -> None:
        if not isinstance(database_url, str) or not database_url.strip():
            raise ValueError("database_url cannot be empty")
        if not isinstance(vector_enabled, bool):
            raise ValueError("vector_enabled must be a boolean")
        if not isinstance(embedding_model_id, str) or not embedding_model_id.strip():
            raise ValueError("embedding_model_id cannot be empty")
        if aws_region is not None and not aws_region.strip():
            raise ValueError("aws_region cannot be empty")
        if embedder_factory is not None and not vector_enabled:
            raise ValueError("embedder_factory requires vector_enabled")

        self._database_url = database_url
        self._vector_enabled = vector_enabled
        self._connection_factory = connection_factory or (
            lambda: connect_database(self._database_url)
        )
        self._embedder = None
        if vector_enabled:
            self._embedder = (
                embedder_factory()
                if embedder_factory is not None
                else BedrockTitanTextEmbedder(embedding_model_id, aws_region)
            )

    @property
    def vector_enabled(self) -> bool:
        return self._vector_enabled

    def retrieve(self, lookup: ProceduralMemoryLookup) -> ProceduralMemoryRetrieval:
        connection = self._connection_factory()
        try:
            fallback = CockroachTelecomRemediationRepository(connection)
            if not self._vector_enabled:
                return fallback.retrieve(lookup)

            if self._embedder is None:
                raise RuntimeError("vector search is not initialized")
            return SemanticProceduralMemory(
                CockroachTelecomVectorMemoryStore(connection),
                self._embedder,
                fallback,
            ).retrieve(lookup)
        finally:
            connection.close()


class UnavailableMemorySearchReader:
    def retrieve(self, lookup: ProceduralMemoryLookup) -> ProceduralMemoryRetrieval:
        raise MemorySearchUnavailableError


def build_memory_search_reader(
    database_url: str | None,
    environment: Mapping[str, str] | None = None,
) -> ProceduralMemoryReader:
    config = MemorySearchRuntimeConfig.from_environment(environment)
    if database_url is None or not database_url.strip():
        return UnavailableMemorySearchReader()
    return config.reader(database_url)


def create_memory_search_router(
    reader: ProceduralMemoryReader,
    *,
    policy: AgentMemoryPolicy = DEFAULT_MEMORY_POLICY,
) -> APIRouter:
    router = APIRouter(tags=["memory"])

    @router.get("/memories/search")
    async def search_memories(
        request: Request,
        agent_id: Annotated[
            str,
            Query(min_length=1, max_length=MAX_AGENT_ID_LENGTH, pattern=r"^[a-z][a-z0-9_]*$"),
        ],
        route: Annotated[str, Query(min_length=1, max_length=MAX_ROUTE_LENGTH)],
        service_type: Annotated[
            str,
            Query(min_length=1, max_length=MAX_SERVICE_TYPE_LENGTH),
        ],
        symptom: Annotated[str, Query(min_length=1, max_length=MAX_SYMPTOM_LENGTH)],
        applicable_at: datetime,
        known_at: datetime,
        exclude_source_dispute_id: UUID | None = None,
        limit: Annotated[int, Query(ge=1, le=20)] = 3,
    ) -> JSONResponse:
        _validate_query_shape(request)
        try:
            validated_agent_id = _validate_agent_id(agent_id)
            scope = policy.scope_for(validated_agent_id)
            lookup = ProceduralMemoryLookup(
                domain=scope.domain,
                namespace=scope.namespace,
                route=_validate_query_text(route, "route", MAX_ROUTE_LENGTH),
                service_type=_validate_query_text(
                    service_type,
                    "service_type",
                    MAX_SERVICE_TYPE_LENGTH,
                ),
                symptom=_validate_query_text(symptom, "symptom", MAX_SYMPTOM_LENGTH),
                applicable_at=applicable_at,
                known_at=known_at,
                exclude_source_dispute_id=exclude_source_dispute_id,
                limit=limit,
            )
        except MemoryAccessDeniedError as error:
            raise HTTPException(status_code=403, detail="memory_access_denied") from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail="invalid_memory_search") from error

        try:
            retrieval = await run_in_threadpool(reader.retrieve, lookup)
        except Exception as error:
            raise HTTPException(status_code=503, detail="memory_search_unavailable") from error

        payload = _response_payload(validated_agent_id, scope, retrieval, limit)
        return JSONResponse(
            content=jsonable_encoder(payload),
            headers={"Cache-Control": "no-store"},
        )

    return router


def _response_payload(
    agent_id: str,
    scope: MemoryAccessScope,
    retrieval: ProceduralMemoryRetrieval,
    limit: int,
) -> dict[str, object]:
    hits = retrieval.hits[:limit]
    return {
        "agent_id": agent_id,
        "scope": {"domain": scope.domain, "namespace": scope.namespace},
        "retrieval_method": _bounded_output(retrieval.method, 64),
        "count": len(hits),
        "hits": [_hit_payload(hit) for hit in hits],
    }


def _hit_payload(hit: ProceduralMemoryHit) -> dict[str, object]:
    truncated = (
        len(hit.memory_key) > MAX_MEMORY_KEY_LENGTH
        or len(hit.root_cause) > MAX_ROOT_CAUSE_LENGTH
        or len(hit.content) > MAX_CONTENT_LENGTH
        or len(hit.checklist) > MAX_CHECKLIST_ITEMS
        or any(len(item) > MAX_CHECKLIST_ITEM_LENGTH for item in hit.checklist)
    )
    return {
        "memory_id": hit.memory_id,
        "memory_key": _bounded_output(hit.memory_key, MAX_MEMORY_KEY_LENGTH),
        "source_dispute_id": hit.source_dispute_id,
        "corrected_assertion_id": hit.corrected_assertion_id,
        "remediation_run_id": hit.remediation_run_id,
        "root_cause": _bounded_output(hit.root_cause, MAX_ROOT_CAUSE_LENGTH),
        "content": _bounded_output(hit.content, MAX_CONTENT_LENGTH),
        "checklist": [
            _bounded_output(item, MAX_CHECKLIST_ITEM_LENGTH)
            for item in hit.checklist[:MAX_CHECKLIST_ITEMS]
        ],
        "recorded_at": hit.recorded_at,
        "rank": hit.rank,
        "score": hit.score,
        "truncated": truncated,
    }


def _validate_query_shape(request: Request) -> None:
    names = [name for name, _ in request.query_params.multi_items()]
    if any(name not in _QUERY_FIELDS for name in names):
        raise HTTPException(status_code=422, detail="unexpected_query_parameter")
    if any(count > 1 for count in Counter(names).values()):
        raise HTTPException(status_code=422, detail="duplicate_query_parameter")


def _bounded_output(value: str, max_length: int) -> str:
    return str(value)[:max_length]


def _flag(environment: Mapping[str, str], name: str) -> bool:
    value = environment.get(name, "").strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(f"{name} must be true or false")


def _optional(environment: Mapping[str, str], name: str) -> str | None:
    value = environment.get(name, "").strip()
    return value or None


class MemoryAccessDeniedError(PermissionError):
    pass


class MemorySearchUnavailableError(RuntimeError):
    pass
