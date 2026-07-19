import time
from typing import Any
from uuid import UUID

from hindsight.infrastructure.sources import CockroachSourceRepository
from hindsight.ingestion.cdrs import (
    CdrConflictError,
    CdrIngestionResult,
    CdrIngestionService,
    CdrRecord,
    CdrRow,
    cdr_id,
)

INSERT_CDR_SQL = """
INSERT INTO telecom_cdrs (
  id, external_id, msisdn_hash, route, service_type, started_at,
  duration_sec, data_mb, source_id
)
VALUES (%s, %s, %s, %s, %s, %s, %s, NULL, %s)
ON CONFLICT (external_id) DO NOTHING
RETURNING id
"""

SELECT_CDR_SQL = """
SELECT id, external_id, msisdn_hash, route, service_type, started_at,
       duration_sec, source_id
FROM telecom_cdrs
WHERE external_id = %s
"""


class CockroachCdrRepository:
    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def append(self, row: CdrRow, source_id: UUID) -> CdrRecord:
        record = CdrRecord(cdr_id(row.external_id), row, source_id)
        inserted = self._connection.execute(
            INSERT_CDR_SQL,
            (
                record.id,
                row.external_id,
                row.msisdn_hash,
                row.route,
                row.service_type,
                row.started_at,
                row.duration_sec,
                source_id,
            ),
        ).fetchone()
        if inserted is not None:
            return record
        existing = self._connection.execute(SELECT_CDR_SQL, (row.external_id,)).fetchone()
        if existing is None:
            raise RuntimeError("CDR insertion did not return a durable identity")
        if existing["source_id"] is None:
            raise CdrConflictError(
                f"external_id {row.external_id!r} already identifies another CDR"
            )
        persisted = _from_row(existing)
        if persisted.row != row or persisted.source_id != source_id:
            raise CdrConflictError(
                f"external_id {row.external_id!r} already identifies another CDR"
            )
        return persisted


class CockroachCdrIngestion:
    def __init__(
        self,
        connection: Any,
        *,
        max_rows: int = 10_000,
        max_retries: int = 3,
        trust_level: str = "untrusted",
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        self._connection = connection
        self._max_rows = max_rows
        self._max_retries = max_retries
        self._trust_level = trust_level

    def ingest(
        self,
        payload: bytes,
        *,
        source_uri: str,
        metadata: dict[str, object] | None = None,
    ) -> CdrIngestionResult:
        for attempt in range(self._max_retries + 1):
            try:
                with self._connection.transaction():
                    service = CdrIngestionService(
                        CockroachSourceRepository(self._connection),
                        CockroachCdrRepository(self._connection),
                        max_rows=self._max_rows,
                        trust_level=self._trust_level,
                    )
                    return service.ingest(payload, source_uri=source_uri, metadata=metadata)
            except Exception as error:
                if getattr(error, "sqlstate", None) != "40001" or attempt == self._max_retries:
                    raise
                time.sleep(0.05 * 2**attempt)
        raise RuntimeError("unreachable retry state")


def _from_row(row: dict[str, object]) -> CdrRecord:
    return CdrRecord(
        id=UUID(str(row["id"])),
        row=CdrRow(
            external_id=str(row["external_id"]),
            msisdn_hash=str(row["msisdn_hash"]),
            route=str(row["route"]),
            service_type=str(row["service_type"]),
            started_at=row["started_at"],
            duration_sec=int(row["duration_sec"]),
        ),
        source_id=UUID(str(row["source_id"])),
    )
