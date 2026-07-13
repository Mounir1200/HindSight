from datetime import UTC, datetime
from decimal import Decimal

from hindsight.adapters.telecom.billing import TelecomAdapter
from hindsight.adapters.telecom.seed import (
    DEMO_DECISION_TIME,
    DEMO_EVENT_TIME,
    DEMO_TARIFF_KEY,
    demo_call,
    seed_demo,
)
from hindsight.core.assertions.models import TemporalLookup
from hindsight.core.assertions.repository import InMemoryAssertionRepository
from hindsight.core.assertions.service import TemporalAssertionService
from hindsight.core.decisions.service import DecisionAuditService
from hindsight.core.verdicts.engine import Verdict


def test_retroactive_rate_preserves_history_and_exonerates_agent() -> None:
    repository = InMemoryAssertionRepository()
    assertions = TemporalAssertionService(repository)
    seed_demo(assertions)
    seed_demo(assertions)

    audit = DecisionAuditService(assertions, TelecomAdapter()).audit(
        event=demo_call(),
        subject_id=DEMO_TARIFF_KEY,
        event_time=DEMO_EVENT_TIME,
        decision_time=DEMO_DECISION_TIME,
    )

    assert audit.snapshot.current_truth.value_number == Decimal("0.15")
    assert audit.snapshot.known_at_decision.value_number == Decimal("0.25")
    assert audit.decision.selected_value == Decimal("0.25")
    assert audit.decision.output["amount"] == Decimal("2.50")
    assert audit.comparison.details["expected_amount"] == Decimal("1.50")
    assert audit.comparison.details["overcharge"] == Decimal("1.00")
    assert audit.verdict.verdict is Verdict.WRONG_NOT_KNOWABLE
    assert audit.verdict.agent_fault is False
    assert audit.verdict.knowledge_gap_seconds == 172_800

    history = repository.history(DEMO_TARIFF_KEY)
    assert [item.version_number for item in history] == [1, 2]
    assert history[0].superseded_by == history[1].id

    before_change = TemporalLookup(
        assertion_key=DEMO_TARIFF_KEY,
        domain="telecom",
        subject_type="roaming_route",
        subject_id=DEMO_TARIFF_KEY,
        predicate="rate_per_minute",
        event_time=datetime(2026, 6, 1, tzinfo=UTC),
        decision_time=datetime(2026, 7, 4, tzinfo=UTC),
    )
    assert repository.current_truth(before_change).value_number == Decimal("0.25")
    assert repository.known_at_decision(before_change).value_number == Decimal("0.25")
