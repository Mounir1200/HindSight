from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from hindsight.adapters.telecom.models import CallEvent
from hindsight.core.assertions.models import Assertion, TemporalLookup
from hindsight.core.decisions.models import DecisionCalculation, OutcomeComparison

CENT = Decimal("0.01")
SECONDS_PER_MINUTE = Decimal(60)


class TelecomAdapter:
    domain = "telecom"
    late_information_root_cause = "delayed_tariff_ingestion"

    def build_assertion_lookup(
        self,
        subject_id: str,
        event_time: datetime,
        decision_time: datetime,
        context: dict[str, object],
    ) -> TemporalLookup:
        return TemporalLookup(
            assertion_key=subject_id,
            domain=self.domain,
            subject_type="roaming_route",
            subject_id=subject_id,
            predicate="rate_per_minute",
            event_time=event_time,
            decision_time=decision_time,
        )

    def calculate_decision(
        self,
        event: CallEvent,
        assertion: Assertion,
    ) -> DecisionCalculation:
        rate = _rate(assertion)
        amount = _call_amount(event.duration_seconds, rate)
        return DecisionCalculation(
            selected_assertion_id=assertion.id,
            selected_value=rate,
            output={
                "amount": amount,
                "currency": assertion.currency,
                "duration_seconds": event.duration_seconds,
            },
        )

    def compare_outcome(
        self,
        decision: DecisionCalculation,
        current_truth: Assertion,
    ) -> OutcomeComparison:
        truth_rate = _rate(current_truth)
        duration_seconds = int(decision.output["duration_seconds"])
        billed_amount = Decimal(decision.output["amount"])
        expected_amount = _call_amount(duration_seconds, truth_rate)
        return OutcomeComparison(
            is_correct=decision.selected_value == truth_rate,
            current_truth_value=truth_rate,
            details={
                "billed_amount": billed_amount,
                "expected_amount": expected_amount,
                "overcharge": max(Decimal(0), billed_amount - expected_amount),
                "currency": current_truth.currency,
            },
        )


def _rate(assertion: Assertion) -> Decimal:
    if assertion.value_number is None:
        raise ValueError("a telecom rate assertion requires value_number")
    if assertion.unit != "minute" or assertion.currency is None:
        raise ValueError("a telecom rate must define minute units and a currency")
    return assertion.value_number


def _call_amount(duration_seconds: int, rate: Decimal) -> Decimal:
    minutes = Decimal(duration_seconds) / SECONDS_PER_MINUTE
    return (minutes * rate).quantize(CENT, rounding=ROUND_HALF_UP)
