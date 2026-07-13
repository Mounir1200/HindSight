from datetime import datetime
from typing import Protocol, TypeVar

from hindsight.core.assertions.models import Assertion, TemporalLookup
from hindsight.core.decisions.models import DecisionCalculation, OutcomeComparison

EventT = TypeVar("EventT")


class DomainAdapter(Protocol[EventT]):
    domain: str
    late_information_root_cause: str

    def build_assertion_lookup(
        self,
        subject_id: str,
        event_time: datetime,
        decision_time: datetime,
        context: dict[str, object],
    ) -> TemporalLookup: ...

    def calculate_decision(
        self,
        event: EventT,
        assertion: Assertion,
    ) -> DecisionCalculation: ...

    def compare_outcome(
        self,
        decision: DecisionCalculation,
        current_truth: Assertion,
    ) -> OutcomeComparison: ...
