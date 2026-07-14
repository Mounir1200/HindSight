from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from hindsight.core.memory import ProceduralMemoryLookup, ProceduralMemoryReader

MEMORY_NAMESPACE = "revenue_assurance"
MANUAL_RECOMMENDATION = "Perform a full temporal review before designing remediation."


@dataclass(frozen=True, slots=True)
class InvestigationGuidance:
    case_id: UUID
    memory_reused: bool
    recommendation: str
    applicable_at: datetime
    known_at: datetime
    root_cause: str | None = None
    checklist: tuple[str, ...] = ()
    memory_id: UUID | None = None
    source_dispute_id: UUID | None = None
    remediation_run_id: UUID | None = None
    retrieval_method: str | None = None
    retrieval_rank: int | None = None
    memory_recorded_at: datetime | None = None
    retrieval_score: float | None = None

    @property
    def procedure_steps_reused(self) -> int:
        return len(self.checklist) if self.memory_reused else 0


def build_investigation_guidance(
    reader: ProceduralMemoryReader,
    *,
    dispute_id: UUID,
    route: str,
    service_type: str,
    symptom: str,
    as_of: datetime,
    exclude_current_case: bool = True,
) -> InvestigationGuidance:
    retrieval = reader.retrieve(
        ProceduralMemoryLookup(
            domain="telecom",
            namespace=MEMORY_NAMESPACE,
            route=route,
            service_type=service_type,
            symptom=symptom,
            applicable_at=as_of,
            known_at=as_of,
            exclude_source_dispute_id=(dispute_id if exclude_current_case else None),
            limit=1,
        )
    )
    if not retrieval.hits:
        return InvestigationGuidance(
            dispute_id,
            False,
            MANUAL_RECOMMENDATION,
            retrieval.lookup.applicable_at,
            retrieval.lookup.known_at,
        )

    memory = retrieval.hits[0]
    return InvestigationGuidance(
        case_id=dispute_id,
        memory_reused=True,
        recommendation=memory.content,
        applicable_at=retrieval.lookup.applicable_at,
        known_at=retrieval.lookup.known_at,
        root_cause=memory.root_cause,
        checklist=memory.checklist,
        memory_id=memory.memory_id,
        source_dispute_id=memory.source_dispute_id,
        remediation_run_id=memory.remediation_run_id,
        retrieval_method=retrieval.method,
        retrieval_rank=memory.rank,
        retrieval_score=memory.score,
        memory_recorded_at=memory.recorded_at,
    )
