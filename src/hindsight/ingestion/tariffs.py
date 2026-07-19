import csv
import hashlib
import io
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

from hindsight.core.assertions.models import Assertion, AssertionDraft
from hindsight.core.assertions.repository import AssertionRepository
from hindsight.infrastructure.sources import SourceRepository

TARIFF_COLUMNS = frozenset(
    {
        "assertion_key",
        "route",
        "service_type",
        "value",
        "currency",
        "unit",
        "valid_from",
        "recorded_at",
        "source",
    }
)


@dataclass(frozen=True, slots=True)
class TariffRow:
    assertion_key: str
    route: str
    service_type: str
    value: Decimal
    currency: str
    unit: str
    valid_from: datetime
    recorded_at: datetime
    source: str


@dataclass(frozen=True, slots=True)
class TariffIngestionResult:
    source_id: str
    source_uri: str
    checksum: str
    assertions_processed: int
    replayed: bool


class TariffIngestionService:
    def __init__(
        self,
        sources: SourceRepository,
        assertions: AssertionRepository,
        *,
        max_rows: int = 10_000,
        trust_level: str = "untrusted",
    ) -> None:
        if max_rows < 1:
            raise ValueError("max_rows must be positive")
        if not trust_level:
            raise ValueError("trust_level cannot be empty")
        self._sources = sources
        self._assertions = assertions
        self._max_rows = max_rows
        self._trust_level = trust_level

    def ingest(
        self,
        payload: bytes,
        *,
        source_uri: str,
        metadata: dict[str, object] | None = None,
    ) -> TariffIngestionResult:
        rows = parse_tariff_csv(payload, max_rows=self._max_rows)
        checksum = f"sha256:{hashlib.sha256(payload).hexdigest()}"
        source = self._sources.ensure(
            domain="telecom",
            kind="tariff_csv",
            uri=source_uri,
            checksum=checksum,
            trust_level=self._trust_level,
            metadata=metadata or {},
        )
        drafts = [_to_draft(row, source.id) for row in rows]
        assertions: list[Assertion] = [self._assertions.append(draft) for draft in drafts]
        return TariffIngestionResult(
            source_id=str(source.id),
            source_uri=source_uri,
            checksum=checksum,
            assertions_processed=len(assertions),
            replayed=not source.created,
        )


def parse_tariff_csv(payload: bytes, *, max_rows: int = 10_000) -> list[TariffRow]:
    if not payload:
        raise ValueError("tariff CSV cannot be empty")
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("tariff CSV must be UTF-8 encoded") from error

    reader = csv.DictReader(io.StringIO(text, newline=""))
    fieldnames = reader.fieldnames
    if fieldnames is None or len(fieldnames) != len(set(fieldnames)):
        raise ValueError("tariff CSV must have a unique header row")
    if set(fieldnames) != TARIFF_COLUMNS:
        missing = sorted(TARIFF_COLUMNS.difference(fieldnames))
        extra = sorted(set(fieldnames).difference(TARIFF_COLUMNS))
        raise ValueError(f"invalid tariff CSV columns; missing={missing}, extra={extra}")

    rows: list[TariffRow] = []
    identities: set[tuple[str, datetime]] = set()
    latest_recordings: dict[str, datetime] = {}
    for line_number, raw in enumerate(reader, start=2):
        if len(rows) == max_rows:
            raise ValueError(f"tariff CSV exceeds the {max_rows}-row limit")
        if None in raw:
            raise ValueError(f"line {line_number}: too many values")
        row = _parse_row(raw, line_number)
        identity = (row.assertion_key, row.recorded_at)
        if identity in identities:
            raise ValueError(f"line {line_number}: duplicate assertion_key and recorded_at")
        identities.add(identity)
        previous_recording = latest_recordings.get(row.assertion_key)
        if previous_recording is not None and row.recorded_at < previous_recording:
            raise ValueError(f"line {line_number}: versions must be ordered by recorded_at")
        latest_recordings[row.assertion_key] = row.recorded_at
        rows.append(row)
    if not rows:
        raise ValueError("tariff CSV must contain at least one data row")
    return rows


def _parse_row(raw: dict[str, str | None], line_number: int) -> TariffRow:
    values = {key: (value or "").strip() for key, value in raw.items()}
    empty = sorted(key for key in TARIFF_COLUMNS if not values[key])
    if empty:
        raise ValueError(f"line {line_number}: empty fields {empty}")
    oversized = sorted(key for key, value in values.items() if len(value) > 256)
    if oversized:
        raise ValueError(f"line {line_number}: fields exceed 256 characters {oversized}")
    if values["service_type"] != "voice" or values["unit"] != "minute":
        raise ValueError(f"line {line_number}: only voice tariffs per minute are supported")
    try:
        value = Decimal(values["value"])
    except InvalidOperation as error:
        raise ValueError(f"line {line_number}: value must be a decimal") from error
    if (
        not value.is_finite()
        or value < 0
        or value >= Decimal("1000000000000")
        or value.as_tuple().exponent < -8
    ):
        raise ValueError(f"line {line_number}: value must be a non-negative finite decimal")
    valid_from = _timestamp(values["valid_from"], line_number, "valid_from")
    recorded_at = _timestamp(values["recorded_at"], line_number, "recorded_at")
    return TariffRow(
        assertion_key=values["assertion_key"],
        route=values["route"],
        service_type=values["service_type"],
        value=value,
        currency=values["currency"],
        unit=values["unit"],
        valid_from=valid_from,
        recorded_at=recorded_at,
        source=values["source"],
    )


def _timestamp(value: str, line_number: int, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"line {line_number}: {field_name} must be ISO 8601") from error
    if parsed.utcoffset() is None:
        raise ValueError(f"line {line_number}: {field_name} must include a timezone")
    return parsed


def _to_draft(row: TariffRow, source_id: UUID) -> AssertionDraft:
    return AssertionDraft(
        assertion_key=row.assertion_key,
        domain="telecom",
        subject_type="roaming_route",
        subject_id=row.assertion_key,
        predicate="rate_per_minute",
        value_json={
            "rate": format(row.value, "f"),
            "route": row.route,
            "service_type": row.service_type,
            "source": row.source,
        },
        value_number=row.value,
        currency=row.currency,
        unit=row.unit,
        valid_from=row.valid_from,
        recorded_at=row.recorded_at,
        written_by="s3_tariff_ingestion",
        source_id=source_id,
    )
