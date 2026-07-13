from dataclasses import dataclass
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
class DecisionAudit:
    lookup: TemporalLookup
    snapshot: TemporalSnapshot
    decision: DecisionCalculation
    comparison: OutcomeComparison
    verdict: VerdictResult
