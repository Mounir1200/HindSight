import json
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID, uuid4


@dataclass(frozen=True, slots=True)
class SourceRegistration:
    id: UUID
    created: bool


class SourceRepository(Protocol):
    def ensure(
        self,
        *,
        domain: str,
        kind: str,
        uri: str,
        checksum: str,
        trust_level: str,
        metadata: dict[str, object],
    ) -> SourceRegistration: ...


class InMemorySourceRepository:
    def __init__(self) -> None:
        self._sources: dict[tuple[str, str], UUID] = {}

    def ensure(
        self,
        *,
        domain: str,
        kind: str,
        uri: str,
        checksum: str,
        trust_level: str,
        metadata: dict[str, object],
    ) -> SourceRegistration:
        identity = (domain, checksum)
        existing = self._sources.get(identity)
        if existing is not None:
            return SourceRegistration(id=existing, created=False)
        source_id = uuid4()
        self._sources[identity] = source_id
        return SourceRegistration(id=source_id, created=True)


class CockroachSourceRepository:
    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def ensure(
        self,
        *,
        domain: str,
        kind: str,
        uri: str,
        checksum: str,
        trust_level: str,
        metadata: dict[str, object],
    ) -> SourceRegistration:
        source_id = uuid4()
        row = self._connection.execute(
            """
            INSERT INTO sources (id, domain, kind, uri, checksum, trust_level, metadata)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (domain, checksum) DO NOTHING
            RETURNING id
            """,
            (
                source_id,
                domain,
                kind,
                uri,
                checksum,
                trust_level,
                json.dumps(metadata),
            ),
        ).fetchone()
        if row is not None:
            return SourceRegistration(id=UUID(str(row["id"])), created=True)

        existing = self._connection.execute(
            "SELECT id FROM sources WHERE domain = %s AND checksum = %s",
            (domain, checksum),
        ).fetchone()
        if existing is None:
            raise RuntimeError("source registration did not return a durable identity")
        return SourceRegistration(id=UUID(str(existing["id"])), created=False)
