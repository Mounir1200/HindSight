from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class CallEvent:
    id: str
    tariff_key: str
    started_at: datetime
    duration_seconds: int

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("id cannot be empty")
        if self.started_at.utcoffset() is None:
            raise ValueError("started_at must be timezone-aware")
        if self.duration_seconds <= 0:
            raise ValueError("duration_seconds must be positive")
