from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from hindsight.adapters.telecom.billing import TelecomAdapter
from hindsight.adapters.telecom.seed import (
    DEMO_CALL_ID,
    DEMO_DECISION_ID,
    DEMO_DECISION_TIME,
    DEMO_EVENT_TIME,
    DEMO_INVESTIGATION_TIME,
    DEMO_TARIFF_KEY,
    demo_call,
    seed_demo,
)
from hindsight.core.assertions.models import TemporalLookup
from hindsight.core.assertions.repository import InMemoryAssertionRepository
from hindsight.core.assertions.service import TemporalAssertionService
from hindsight.core.decisions.repository import InMemoryDecisionRepository
from hindsight.core.decisions.service import DecisionAuditService, DecisionJournalService
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

    evidence = {item.evidence_type: item for item in audit.evidence}
    assert evidence["decision_input"].assertion_id == audit.snapshot.known_at_decision.id
    assert evidence["decision_input"].was_used_for_decision is True
    assert evidence["decision_input"].was_presented_to_model is False
    assert evidence["counterfactual_current_truth"].assertion_id == audit.snapshot.current_truth.id
    assert evidence["counterfactual_current_truth"].available_to_agent is False
    assert evidence["counterfactual_current_truth"].exclusion_reason == "not_recorded_at_decision"

    decision_repository = InMemoryDecisionRepository()
    journal = DecisionJournalService(decision_repository)
    record = journal.record(
        audit,
        decision_id=DEMO_DECISION_ID,
        agent_id="billing_agent",
        action="calculate_call_charge",
        subject_type="telecom_call",
        subject_id=DEMO_CALL_ID,
        investigated_at=DEMO_INVESTIGATION_TIME,
        input={"call_id": DEMO_CALL_ID},
    )
    replay = journal.record(
        replace(audit, evidence=tuple(reversed(audit.evidence))),
        decision_id=DEMO_DECISION_ID,
        agent_id="billing_agent",
        action="calculate_call_charge",
        subject_type="telecom_call",
        subject_id=DEMO_CALL_ID,
        investigated_at=DEMO_INVESTIGATION_TIME,
        input={"call_id": DEMO_CALL_ID},
    )
    assert replay == record == decision_repository.get(DEMO_DECISION_ID)

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
