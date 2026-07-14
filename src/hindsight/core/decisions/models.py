from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from hindsight.core.assertions.models import TemporalLookup
from hindsight.core.assertions.service import TemporalSnapshot
from hindsight.core.verdicts.engine import VerdictResult


@dataclass(frozen=True, slots=True)
class DecisionCalculation:
    selected_assertion_id: UUID
    selected_value: Decimal
    output: dict[str, object]


@dataclass(frozen=True, slots=True)
class OutcomeComparison:
    is_correct: bool
    current_truth_value: Decimal
    details: dict[str, object]


@dataclass(frozen=True, slots=True)
class DecisionEvidence:
    evidence_type: str
    assertion_id: UUID
    available_to_agent: bool
    retrieval_started_at: datetime | None = None
    retrieved_at: datetime | None = None
    retrieval_method: str | None = None
    retrieval_query: str | None = None
    retrieval_rank: int | None = None
    retrieval_score: float | None = None
    was_presented_to_model: bool = False
    presentation_position: int | None = None
    was_cited_in_rationale: bool = False
    was_used_for_decision: bool = False
    exclusion_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.evidence_type:
            raise ValueError("evidence_type cannot be empty")
        for field_name in ("retrieval_started_at", "retrieved_at"):
            value = getattr(self, field_name)
            if value is not None and value.utcoffset() is None:
                raise ValueError(f"{field_name} must be timezone-aware")
        if (
            self.retrieval_started_at is not None
            and self.retrieved_at is not None
            and self.retrieval_started_at > self.retrieved_at
        ):
            raise ValueError("retrieval_started_at cannot be later than retrieved_at")
        if self.retrieval_rank is not None and self.retrieval_rank <= 0:
            raise ValueError("retrieval_rank must be positive")
        if self.retrieval_rank is not None and self.retrieved_at is None:
            raise ValueError("ranked evidence must have been retrieved")
        if self.presentation_position is not None and self.presentation_position <= 0:
            raise ValueError("presentation_position must be positive")
        if self.presentation_position is not None and not self.was_presented_to_model:
            raise ValueError("positioned evidence must have been presented")
        if self.was_presented_to_model and self.retrieved_at is None:
            raise ValueError("presented evidence must have been retrieved")
        if self.was_used_for_decision and self.retrieved_at is None:
            raise ValueError("used evidence must have been retrieved")


@dataclass(frozen=True, slots=True)
class DecisionAudit:
    lookup: TemporalLookup
    snapshot: TemporalSnapshot
    decision: DecisionCalculation
    comparison: OutcomeComparison
    verdict: VerdictResult
    evidence: tuple[DecisionEvidence, ...]


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    id: UUID
    domain: str
    agent_id: str
    action: str
    subject_type: str
    subject_id: str
    event_time: datetime
    decided_at: datetime
    investigated_at: datetime
    selected_assertion_id: UUID
    input: dict[str, object]
    output: dict[str, object]
    rationale: str | None
    verdict: VerdictResult

    def __post_init__(self) -> None:
        for field_name in ("event_time", "decided_at", "investigated_at"):
            if getattr(self, field_name).utcoffset() is None:
                raise ValueError(f"{field_name} must be timezone-aware")
        if self.selected_assertion_id != self.verdict.selected_assertion_id:
            raise ValueError("record and verdict must reference the same selected assertion")


@dataclass(frozen=True, slots=True)
class DecisionJournalEntry:
    record: DecisionRecord
    evidence: tuple[DecisionEvidence, ...]

    def __post_init__(self) -> None:
        if not self.evidence:
            raise ValueError("a decision requires at least one evidence trace")
        identities = {(item.evidence_type, item.assertion_id) for item in self.evidence}
        if len(identities) != len(self.evidence):
            raise ValueError("evidence identities must be unique within a decision")
        object.__setattr__(
            self,
            "evidence",
            tuple(sorted(self.evidence, key=_evidence_sort_key)),
        )
        if not any(
            item.assertion_id == self.record.selected_assertion_id
            and item.was_used_for_decision
            for item in self.evidence
        ):
            raise ValueError("selected assertion must be recorded as used evidence")


def _evidence_sort_key(evidence: DecisionEvidence) -> tuple[bool, int, str, str]:
    return (
        evidence.retrieval_rank is None,
        evidence.retrieval_rank or 0,
        evidence.evidence_type,
        str(evidence.assertion_id),
    )


class DecisionConflictError(ValueError):
    pass


class DecisionNotFoundError(LookupError):
    pass
