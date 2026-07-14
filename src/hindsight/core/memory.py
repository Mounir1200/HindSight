from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


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
        if not all(
            (self.domain, self.namespace, self.route, self.service_type, self.symptom)
        ):
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
