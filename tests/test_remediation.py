from decimal import Decimal

from hindsight.adapters.telecom.remediation import (
    InMemoryTelecomRemediationRepository,
    RemediationOutcome,
)
from hindsight.adapters.telecom.seed import DEMO_REMEDIATION_END
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

    learning = payload["learning_proof"]
    before = learning["before_memory"]
    after = learning["after_memory"]
    change = learning["measured_change"]
    assert before["memory_reused"] is False
    assert after["memory_reused"] is True
    assert before["case_id"] == remediation["case_id"]
    assert after["case_id"] == learning["second_case"]["dispute_id"]
    assert after["memory_id"] == remediation["effects"]["procedural_memory_id"]
    assert after["source_dispute_id"] == remediation["case_id"]
    assert after["retrieval_method"] == "structured_exact"
    assert after["retrieval_rank"] == 1
    assert before["known_at"] < after["memory_recorded_at"] <= after["known_at"]
    assert after["memory_recorded_at"] == DEMO_REMEDIATION_END
    assert change == {
        "checklist_items_loaded": 4,
        "recommendation_changed": True,
        "procedural_memory_reuse": {
            "before": {"reused_cases": 0, "eligible_cases": 1},
            "after": {"reused_cases": 1, "eligible_cases": 1},
        },
        "suggested_root_cause_confirmed": True,
        "second_case_opened_after_memory": True,
    }
    assert learning["advisory_boundary"] == {
        "memory_used_for_verdict": False,
        "memory_used_for_financial_calculation": False,
        "verdict_source": "deterministic_temporal_engine",
        "financial_source": "telecom_adapter",
    }
