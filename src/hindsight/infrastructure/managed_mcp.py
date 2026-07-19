import asyncio
import json
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from typing import Any, Protocol
from urllib.parse import unquote, urlparse
from uuid import NAMESPACE_URL, UUID, uuid5

from hindsight.agents.investigation import (
    MAX_TOOL_RESULT_BYTES,
    InvestigationContextReadError,
    prepare_investigation_context,
)

DEFAULT_MANAGED_MCP_ENDPOINT = "https://cockroachlabs.cloud/mcp"
INVESTIGATION_CONTEXT_VERSION = "telecom-investigation-v1"
SELECT_TOOL_NAME = "select_query"
MAX_MCP_RESPONSE_BYTES = MAX_TOOL_RESULT_BYTES * 4
MAX_MCP_RESULT_NODES = 4_096

INSERT_CONTEXT_SQL = """
INSERT INTO investigation_context_snapshots (
  id, case_id, context_version, content_hash, context
)
VALUES (%s, %s, %s, %s, CAST(%s AS JSONB))
ON CONFLICT (case_id, content_hash) DO NOTHING
RETURNING id
"""

SELECT_CONTEXT_SQL = """
SELECT id, context_version, content_hash, context
FROM investigation_context_snapshots
WHERE case_id = %s AND content_hash = %s
"""


class ManagedMcpSelectClient(Protocol):
    def select(self, *, database: str, query: str) -> object: ...


@dataclass(frozen=True, slots=True)
class InvestigationContextSnapshot:
    id: UUID
    case_id: UUID
    content_hash: str


class CockroachInvestigationContextStore:
    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def persist(
        self,
        case_id: UUID,
        context: Mapping[str, object],
    ) -> InvestigationContextSnapshot:
        normalized = prepare_investigation_context(case_id, context)
        encoded = json.dumps(normalized, separators=(",", ":"), sort_keys=True)
        content_hash = sha256(encoded.encode()).hexdigest()
        snapshot_id = uuid5(NAMESPACE_URL, f"hindsight:{case_id}:{content_hash}")
        inserted = self._connection.execute(
            INSERT_CONTEXT_SQL,
            (
                snapshot_id,
                case_id,
                INVESTIGATION_CONTEXT_VERSION,
                content_hash,
                encoded,
            ),
        ).fetchone()
        if inserted is None:
            existing = self._connection.execute(
                SELECT_CONTEXT_SQL,
                (case_id, content_hash),
            ).fetchone()
            if (
                existing is None
                or UUID(str(existing["id"])) != snapshot_id
                or existing["context_version"] != INVESTIGATION_CONTEXT_VERSION
            ):
                raise ValueError("the persisted investigation context version conflicts")
            stored = existing["context"]
            if isinstance(stored, str):
                stored = json.loads(stored)
            if stored != normalized:
                raise ValueError("the persisted investigation context is immutable")
        return InvestigationContextSnapshot(snapshot_id, case_id, content_hash)


class ManagedMcpInvestigationContextReader:
    source = "cockroachdb_managed_mcp"

    def __init__(
        self,
        client: ManagedMcpSelectClient,
        database: str,
        snapshot_id: UUID | None = None,
    ) -> None:
        if not isinstance(database, str) or not database.strip():
            raise ValueError("MCP database cannot be empty")
        self._client = client
        self._database = database.strip()
        self._snapshot_id = snapshot_id

    def for_snapshot(self, snapshot_id: UUID) -> "ManagedMcpInvestigationContextReader":
        return ManagedMcpInvestigationContextReader(
            self._client,
            self._database,
            snapshot_id,
        )

    @property
    def reference(self) -> str | None:
        return str(self._snapshot_id) if self._snapshot_id is not None else None

    def read(self, case_id: UUID) -> Mapping[str, object]:
        if self._snapshot_id is None:
            raise InvestigationContextReadError(
                "managed_mcp_snapshot_unbound",
                "Managed MCP context reader has no assigned snapshot",
                retryable=False,
            )
        query = (
            "SELECT context::STRING AS context "
            "FROM public.investigation_context_snapshots "
            f"WHERE id = '{self._snapshot_id}' AND case_id = '{case_id}' LIMIT 1"
        )
        response = self._client.select(database=self._database, query=query)
        return _extract_context(response)


class CockroachCloudManagedMcpClient:
    def __init__(
        self,
        cluster_id: str,
        api_key: str,
        *,
        timeout_seconds: float = 15,
    ) -> None:
        try:
            self._cluster_id = str(UUID(cluster_id))
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError("MCP cluster ID must be a UUID") from error
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("MCP API key cannot be empty")
        if timeout_seconds <= 0 or timeout_seconds > 60:
            raise ValueError("MCP timeout must be between 0 and 60 seconds")
        self._api_key = api_key.strip()
        self._endpoint = DEFAULT_MANAGED_MCP_ENDPOINT
        self._timeout_seconds = timeout_seconds

    def select(self, *, database: str, query: str) -> object:
        if not isinstance(query, str) or not query.lstrip().upper().startswith("SELECT "):
            raise InvestigationContextReadError(
                "managed_mcp_query_rejected",
                "Managed MCP accepts only the fixed SELECT investigation query",
                retryable=False,
            )
        if len(query) > 4_096:
            raise InvestigationContextReadError(
                "managed_mcp_query_too_large",
                "Managed MCP query exceeds its budget",
                retryable=False,
            )
        try:
            return asyncio.run(self._select(database, query))
        except InvestigationContextReadError:
            raise
        except Exception as error:
            raise InvestigationContextReadError(
                "managed_mcp_request_failed",
                "CockroachDB Managed MCP request failed",
                retryable=True,
            ) from error

    async def _select(self, database: str, query: str) -> object:
        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        headers = {
            "mcp-cluster-id": self._cluster_id,
            "Authorization": f"Bearer {self._api_key}",
        }
        timeout = httpx.Timeout(self._timeout_seconds)
        async with (
            httpx.AsyncClient(
                headers=headers,
                timeout=timeout,
                follow_redirects=False,
            ) as http_client,
            streamable_http_client(
                self._endpoint,
                http_client=http_client,
            ) as (read_stream, write_stream, _),
            ClientSession(
                read_stream,
                write_stream,
                read_timeout_seconds=timedelta(seconds=self._timeout_seconds),
            ) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            select_tool = next(
                (tool for tool in tools.tools if tool.name == SELECT_TOOL_NAME),
                None,
            )
            if select_tool is None:
                raise InvestigationContextReadError(
                    "managed_mcp_tool_missing",
                    "CockroachDB Managed MCP did not expose select_query",
                    retryable=False,
                )
            arguments = _select_arguments(
                select_tool.inputSchema,
                database,
                query,
            )
            result = await session.call_tool(
                SELECT_TOOL_NAME,
                arguments,
                read_timeout_seconds=timedelta(seconds=self._timeout_seconds),
            )
        payload = result.model_dump(by_alias=True, mode="json")
        if payload.get("isError"):
            raise InvestigationContextReadError(
                "managed_mcp_tool_failed",
                "CockroachDB Managed MCP rejected the investigation query",
                retryable=False,
            )
        _enforce_response_budget(payload)
        return payload


def database_name_from_url(database_url: str) -> str:
    name = unquote(urlparse(database_url).path.lstrip("/")).split("/", 1)[0]
    if not name:
        raise ValueError("DATABASE_URL must include a database name for MCP")
    return name


def _select_arguments(
    schema: Mapping[str, object],
    database: str,
    query: str,
) -> dict[str, str]:
    properties = schema.get("properties")
    required = schema.get("required", [])
    if not isinstance(properties, Mapping) or not isinstance(required, list):
        raise InvestigationContextReadError(
            "managed_mcp_schema_invalid",
            "select_query exposed an invalid input schema",
            retryable=False,
        )
    names = {str(name) for name in properties}
    query_name = _first_name(
        names,
        ("query", "sql", "statement", "sql_query", "sql_statement"),
    )
    database_name = _first_name(
        names,
        ("database", "database_name", "databaseName"),
    )
    if query_name is None:
        raise InvestigationContextReadError(
            "managed_mcp_schema_unsupported",
            "select_query has no recognized query argument",
            retryable=False,
        )
    arguments = {query_name: query}
    if database_name is not None:
        arguments[database_name] = database
    unsupported = {str(name) for name in required} - arguments.keys()
    if unsupported:
        raise InvestigationContextReadError(
            "managed_mcp_schema_unsupported",
            "select_query requires unsupported arguments",
            retryable=False,
        )
    return arguments


def _first_name(names: set[str], candidates: tuple[str, ...]) -> str | None:
    return next((candidate for candidate in candidates if candidate in names), None)


def _enforce_response_budget(value: object) -> None:
    try:
        size = len(json.dumps(value, separators=(",", ":")).encode())
    except (TypeError, ValueError) as error:
        raise InvestigationContextReadError(
            "managed_mcp_response_invalid",
            "Managed MCP returned a non-JSON response",
            retryable=False,
        ) from error
    if size > MAX_MCP_RESPONSE_BYTES:
        raise InvestigationContextReadError(
            "managed_mcp_response_too_large",
            "Managed MCP response exceeds its budget",
            retryable=False,
        )


def _extract_context(response: object) -> Mapping[str, object]:
    _enforce_response_budget(response)
    stack = [response]
    visited = 0
    while stack and visited < MAX_MCP_RESULT_NODES:
        value = stack.pop()
        visited += 1
        if isinstance(value, Mapping):
            if "case_id" in value and "decision" in value:
                return {str(key): item for key, item in value.items()}
            values = list(value.values())
            if "context" in value:
                values.append(value["context"])
            stack.extend(values)
        elif isinstance(value, list | tuple):
            stack.extend(value)
        elif isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith(("{", "[")):
                with suppress(json.JSONDecodeError):
                    stack.append(json.loads(stripped))
    raise InvestigationContextReadError(
        "managed_mcp_context_missing",
        "Managed MCP returned no investigation context",
        retryable=False,
    )
