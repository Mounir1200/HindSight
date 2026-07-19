import csv
import hashlib
import io
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from hindsight.infrastructure.sources import SourceRepository

CDR_COLUMNS = (
    "external_id",
    "msisdn_hash",
    "route",
    "service_type",
    "started_at",
    "duration_sec",
)
_EXTERNAL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_MSISDN_HASH = re.compile(r"[0-9a-f]{64}\Z")
_ROUTE = re.compile(r"[A-Z]{2}->[A-Z]{2}\Z")
_MAX_DURATION_SECONDS = 86_400


@dataclass(frozen=True, slots=True)
class CdrRow:
    external_id: str
    msisdn_hash: str
    route: str
    service_type: str
    started_at: datetime
    duration_sec: int


@dataclass(frozen=True, slots=True)
class CdrRecord:
    id: UUID
    row: CdrRow
    source_id: UUID


@dataclass(frozen=True, slots=True)
class CdrIngestionResult:
    source_id: str
    source_uri: str
    checksum: str
    cdrs_processed: int
    replayed: bool


class CdrRepository(Protocol):
    def append(self, row: CdrRow, source_id: UUID) -> CdrRecord: ...


class CdrConflictError(ValueError):
    pass


class InMemoryCdrRepository:
    def __init__(self) -> None:
        self._records: dict[str, CdrRecord] = {}

    def append(self, row: CdrRow, source_id: UUID) -> CdrRecord:
        existing = self._records.get(row.external_id)
        if existing is not None:
            _ensure_same_cdr(existing, row, source_id)
            return existing
        record = CdrRecord(cdr_id(row.external_id), row, source_id)
        self._records[row.external_id] = record
        return record

    def records(self) -> list[CdrRecord]:
        return list(self._records.values())


class CdrIngestionService:
    def __init__(
        self,
        sources: SourceRepository,
        cdrs: CdrRepository,
        *,
        max_rows: int = 10_000,
        trust_level: str = "untrusted",
    ) -> None:
        if max_rows < 1:
            raise ValueError("max_rows must be positive")
        if not trust_level:
            raise ValueError("trust_level cannot be empty")
        self._sources = sources
        self._cdrs = cdrs
        self._max_rows = max_rows
        self._trust_level = trust_level

    def ingest(
        self,
        payload: bytes,
        *,
        source_uri: str,
        metadata: dict[str, object] | None = None,
    ) -> CdrIngestionResult:
        rows = parse_cdr_csv(payload, max_rows=self._max_rows)
        checksum = f"sha256:{hashlib.sha256(payload).hexdigest()}"
        source = self._sources.ensure(
            domain="telecom",
            kind="cdr_csv",
            uri=source_uri,
            checksum=checksum,
            trust_level=self._trust_level,
            metadata=metadata or {},
        )
        records = [self._cdrs.append(row, source.id) for row in rows]
        return CdrIngestionResult(
            source_id=str(source.id),
            source_uri=source_uri,
            checksum=checksum,
            cdrs_processed=len(records),
            replayed=not source.created,
        )


def parse_cdr_csv(payload: bytes, *, max_rows: int = 10_000) -> list[CdrRow]:
    if not payload:
        raise ValueError("CDR CSV cannot be empty")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("CDR CSV must be UTF-8 encoded") from error

    reader = csv.DictReader(io.StringIO(text, newline=""))
    fieldnames = reader.fieldnames
    if fieldnames is None or tuple(fieldnames) != CDR_COLUMNS:
        raise ValueError(f"CDR CSV header must be exactly {','.join(CDR_COLUMNS)}")

    rows: list[CdrRow] = []
    external_ids: set[str] = set()
    for line_number, raw in enumerate(reader, start=2):
        if len(rows) == max_rows:
            raise ValueError(f"CDR CSV exceeds the {max_rows}-row limit")
        if None in raw:
            raise ValueError(f"line {line_number}: too many values")
        row = _parse_row(raw, line_number)
        if row.external_id in external_ids:
            raise ValueError(f"line {line_number}: duplicate external_id")
        external_ids.add(row.external_id)
        rows.append(row)
    if not rows:
        raise ValueError("CDR CSV must contain at least one data row")
    return rows


def _parse_row(raw: dict[str, str | None], line_number: int) -> CdrRow:
    values = {key: (value or "").strip() for key, value in raw.items()}
    empty = [key for key in CDR_COLUMNS if not values[key]]
    if empty:
        raise ValueError(f"line {line_number}: empty fields {empty}")
    if any(len(value) > 256 for value in values.values()):
        raise ValueError(f"line {line_number}: fields cannot exceed 256 characters")
    if not _EXTERNAL_ID.fullmatch(values["external_id"]):
        raise ValueError(f"line {line_number}: invalid external_id")
    if not _MSISDN_HASH.fullmatch(values["msisdn_hash"]):
        raise ValueError(f"line {line_number}: msisdn_hash must be 64 lowercase hex characters")
    if not _ROUTE.fullmatch(values["route"]):
        raise ValueError(f"line {line_number}: route must use the AA->BB format")
    if values["service_type"] != "voice":
        raise ValueError(f"line {line_number}: only voice CDRs are supported")
    try:
        duration_sec = int(values["duration_sec"])
    except ValueError as error:
        raise ValueError(f"line {line_number}: duration_sec must be an integer") from error
    if not 1 <= duration_sec <= _MAX_DURATION_SECONDS:
        raise ValueError(
            f"line {line_number}: duration_sec must be between 1 and {_MAX_DURATION_SECONDS}"
        )
    return CdrRow(
        external_id=values["external_id"],
        msisdn_hash=values["msisdn_hash"],
        route=values["route"],
        service_type=values["service_type"],
        started_at=_timestamp(values["started_at"], line_number),
        duration_sec=duration_sec,
    )


def _timestamp(value: str, line_number: int) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"line {line_number}: started_at must be ISO 8601") from error
    if parsed.utcoffset() is None:
        raise ValueError(f"line {line_number}: started_at must include a timezone")
    return parsed


def cdr_id(external_id: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"hindsight:telecom:cdr:{external_id}")


def _ensure_same_cdr(existing: CdrRecord, row: CdrRow, source_id: UUID) -> None:
    if existing.row != row or existing.source_id != source_id:
        raise CdrConflictError(f"external_id {row.external_id!r} already identifies another CDR")
