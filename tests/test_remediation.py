from decimal import Decimal

from hindsight.adapters.telecom.remediation import (
    InMemoryTelecomRemediationRepository,
    RemediationOutcome,
)
from hindsight.core.assertions.repository import InMemoryAssertionRepository
from hindsight.core.decisions.repository import InMemoryDecisionRepository
from hindsight.demo import run_demo_workflow


def test_remediation_applies_once_and_replay_is_a_safe_noop() -> None:
    payload = run_demo_workflow(
        InMemoryAssertionRepository(),
        InMemoryDecisionRepository(),
        InMemoryTelecomRemediationRepository(),
        "in_memory",
    )

    remediation = payload["remediation"]
    first, replay = remediation["attempts"]
    state = remediation["final_state"]

    assert first["status"] is RemediationOutcome.APPLIED
    assert first["safe_noop"] is False
    assert replay["status"] is RemediationOutcome.ALREADY_REMEDIATED
    assert replay["safe_noop"] is True
    assert replay["run_id"] == first["run_id"]
    assert state["invoice_amount"] == Decimal("1.50")
    assert state["refund_amount"] == Decimal("1.00")
    assert state["invoice_status"] == "corrected"
    assert state["dispute_status"] == "closed"
    assert state["selected_assertion_id"] == payload["verdict"][
        "current_truth_assertion_id"
    ]
    assert (
        state["refund_count"],
        state["incident_count"],
        state["procedural_memory_count"],
        state["remediation_run_count"],
    ) == (1, 1, 1, 1)
