from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID


def _require_aware(value: datetime, field_name: str) -> None:
    if value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class AssertionDraft:
    assertion_key: str
    domain: str
    subject_type: str
    subject_id: str
    predicate: str
    value_json: dict[str, object]
    valid_from: datetime
    recorded_at: datetime
    written_by: str
    value_number: Decimal | None = None
    value_text: str | None = None
    unit: str | None = None
    currency: str | None = None
    valid_until: datetime | None = None
    source_id: UUID | None = None
    confidence: float = 1.0

    def __post_init__(self) -> None:
        _require_aware(self.valid_from, "valid_from")
        _require_aware(self.recorded_at, "recorded_at")
        if self.valid_until is not None:
            _require_aware(self.valid_until, "valid_until")
            if self.valid_until <= self.valid_from:
                raise ValueError("valid_until must be later than valid_from")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class Assertion:
    id: UUID
    lineage_id: UUID
    version_number: int
    assertion_key: str
    domain: str
    subject_type: str
    subject_id: str
    predicate: str
    value_json: dict[str, object]
    valid_from: datetime
    recorded_at: datetime
    written_by: str
    value_number: Decimal | None = None
    value_text: str | None = None
    unit: str | None = None
    currency: str | None = None
    valid_until: datetime | None = None
    superseded_at: datetime | None = None
    superseded_by: UUID | None = None
    source_id: UUID | None = None
    confidence: float = 1.0


@dataclass(frozen=True, slots=True)
class TemporalLookup:
    assertion_key: str
    domain: str
    subject_type: str
    subject_id: str
    predicate: str
    event_time: datetime
    decision_time: datetime

    def __post_init__(self) -> None:
        _require_aware(self.event_time, "event_time")
        _require_aware(self.decision_time, "decision_time")


class AssertionNotFoundError(LookupError):
    pass


class AssertionConflictError(ValueError):
    pass
