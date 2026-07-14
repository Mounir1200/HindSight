from dataclasses import replace
from decimal import Decimal

import pytest

from hindsight.core.verdicts.engine import Verdict, VerdictContext, classify_decision

BASE_CONTEXT = VerdictContext(
    selected_value=Decimal("0.25"),
    current_truth_value=Decimal("0.15"),
    correct_evidence_existed_at_decision=True,
    correct_evidence_was_accessible_to_agent=True,
    correct_evidence_was_retrieved=True,
    correct_evidence_was_presented=True,
    correct_evidence_was_used=True,
)


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("selected_value", Decimal("0.15"), Verdict.CORRECT),
        ("correct_evidence_existed_at_decision", False, Verdict.WRONG_NOT_KNOWABLE),
        ("correct_evidence_was_accessible_to_agent", False, Verdict.WRONG_NOT_KNOWABLE),
        ("correct_evidence_was_retrieved", False, Verdict.WRONG_KNOWABLE_NOT_RETRIEVED),
        ("correct_evidence_was_presented", False, Verdict.WRONG_RETRIEVED_NOT_PRESENTED),
        ("correct_evidence_was_used", False, Verdict.WRONG_PRESENTED_IGNORED),
        (
            "lower_trust_source_overrode_higher_trust_source",
            True,
            Verdict.WRONG_DUE_TO_UNTRUSTED_SOURCE,
        ),
        ("correct_evidence_was_accessible_to_agent", None, Verdict.INSUFFICIENT_EVIDENCE),
    ],
)
def test_verdict_follows_the_explicit_evidence_trace(
    field: str,
    value: object,
    expected: Verdict,
) -> None:
    assert classify_decision(replace(BASE_CONTEXT, **{field: value})) is expected
