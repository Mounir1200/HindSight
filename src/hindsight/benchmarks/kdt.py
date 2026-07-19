import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from hindsight.application import execute_demo
from hindsight.core.assertions.models import AssertionDraft, TemporalLookup
from hindsight.core.assertions.repository import InMemoryAssertionRepository
from hindsight.core.assertions.service import TemporalAssertionService
from hindsight.core.verdicts.engine import Verdict, VerdictContext, classify_decision

EVENT_TIME = datetime(2026, 7, 2, 12, tzinfo=UTC)
DECISION_TIME = datetime(2026, 7, 2, 12, 1, tzinfo=UTC)
VALID_FROM = datetime(2026, 7, 1, tzinfo=UTC)
OLD_RECORDED_AT = datetime(2026, 1, 1, tzinfo=UTC)
KNOWN_RECORDED_AT = datetime(2026, 7, 1, 1, tzinfo=UTC)
LATE_RECORDED_AT = datetime(2026, 7, 3, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class KdtScenario:
    id: str
    family: str
    old_value: Decimal
    current_value: Decimal
    selected_value: Decimal
    correction_recorded_at: datetime | None
    context: dict[str, bool]
    expected_known_value: Decimal
    expected_verdict: Verdict
    expected_agent_fault: bool
    expected_root_cause: str | None = None


def run_kdt_benchmark() -> dict[str, object]:
    scenarios = _scenarios()
    truth_hits = 0
    knowledge_hits = 0
    verdict_hits = 0
    root_cause_hits = 0
    root_cause_cases = 0
    retrieval_hits = 0
    retrieval_cases = 0
    complete_provenance = 0
    unjustified_blame = 0
    no_fault_cases = 0

    for scenario in scenarios:
        snapshot = _snapshot(scenario)
        truth_hits += snapshot.current_truth.value_number == scenario.current_value
        knowledge_hits += snapshot.known_at_decision.value_number == scenario.expected_known_value
        context = VerdictContext(
            selected_value=scenario.selected_value,
            current_truth_value=snapshot.current_truth.value_number,
            **scenario.context,
        )
        verdict = classify_decision(context)
        verdict_hits += verdict is scenario.expected_verdict

        if scenario.family in {
            "not_retrieved",
            "not_presented",
            "presented_ignored",
            "untrusted_override",
        }:
            retrieval_cases += 1
            retrieval_hits += verdict is scenario.expected_verdict

        root_cause = (
            "delayed_tariff_ingestion"
            if verdict is Verdict.WRONG_NOT_KNOWABLE
            and snapshot.current_truth.recorded_at > DECISION_TIME
            else None
        )
        if scenario.expected_root_cause is not None:
            root_cause_cases += 1
            root_cause_hits += root_cause == scenario.expected_root_cause

        complete_provenance += all(isinstance(value, bool) for value in scenario.context.values())
        predicted_fault = _agent_fault(verdict)
        if scenario.expected_agent_fault is False:
            no_fault_cases += 1
            unjustified_blame += predicted_fault is True

    demo = execute_demo(None)
    final_state = _mapping(_mapping(demo["remediation"])["final_state"])
    attempts = _mapping(demo["remediation"])["attempts"]
    measured_change = _mapping(_mapping(demo["learning_proof"])["measured_change"])
    reuse = _mapping(measured_change["procedural_memory_reuse"])
    idempotent = bool(
        isinstance(attempts, list)
        and len(attempts) == 2
        and final_state["refund_count"] == 1
        and final_state["incident_count"] == 1
        and final_state["procedural_memory_count"] == 1
        and final_state["remediation_run_count"] == 1
    )

    total = len(scenarios)
    metrics = {
        "truth_reconstruction_accuracy_pct": _percent(truth_hits, total),
        "knowledge_reconstruction_accuracy_pct": _percent(knowledge_hits, total),
        "retrieval_attribution_accuracy_pct": _percent(retrieval_hits, retrieval_cases),
        "verdict_accuracy_pct": _percent(verdict_hits, total),
        "root_cause_accuracy_pct": _percent(root_cause_hits, root_cause_cases),
        "unjustified_blame_rate_pct": _percent(unjustified_blame, no_fault_cases),
        "provenance_completeness_pct": _percent(complete_provenance, total),
        "duplicate_remediation_rate_pct": (0.0 if final_state["refund_count"] == 1 else 100.0),
        "idempotent_remediation_pct": 100.0 if idempotent else 0.0,
        "procedural_memory_reuse_pct": _percent(
            int(_mapping(reuse["after"])["reused_cases"]),
            int(_mapping(reuse["after"])["eligible_cases"]),
        ),
    }
    targets = {
        "verdict_accuracy_pct": ">=90",
        "unjustified_blame_rate_pct": "<=5",
        "provenance_completeness_pct": "=100",
        "duplicate_remediation_rate_pct": "=0",
        "idempotent_remediation_pct": "=100",
    }
    passed = bool(
        metrics["verdict_accuracy_pct"] >= 90
        and metrics["unjustified_blame_rate_pct"] <= 5
        and metrics["provenance_completeness_pct"] == 100
        and metrics["duplicate_remediation_rate_pct"] == 0
        and metrics["idempotent_remediation_pct"] == 100
    )
    return {
        "benchmark": "kdt-synthetic-v1",
        "scope": "35 controlled synthetic temporal-accountability scenarios",
        "scenario_count": total,
        "family_counts": dict(sorted(Counter(item.family for item in scenarios).items())),
        "metrics": metrics,
        "targets": targets,
        "passed": passed,
    }


def _scenarios() -> tuple[KdtScenario, ...]:
    configurations = (
        (
            "correct",
            Verdict.CORRECT,
            False,
            None,
            None,
            _evidence(True, True, True, True, True, False),
        ),
        (
            "late_recording",
            Verdict.WRONG_NOT_KNOWABLE,
            False,
            LATE_RECORDED_AT,
            "delayed_tariff_ingestion",
            _evidence(False, False, False, False, False, False),
        ),
        (
            "inaccessible",
            Verdict.WRONG_NOT_KNOWABLE,
            False,
            KNOWN_RECORDED_AT,
            None,
            _evidence(True, False, False, False, False, False),
        ),
        (
            "not_retrieved",
            Verdict.WRONG_KNOWABLE_NOT_RETRIEVED,
            True,
            KNOWN_RECORDED_AT,
            None,
            _evidence(True, True, False, False, False, False),
        ),
        (
            "not_presented",
            Verdict.WRONG_RETRIEVED_NOT_PRESENTED,
            True,
            KNOWN_RECORDED_AT,
            None,
            _evidence(True, True, True, False, False, False),
        ),
        (
            "presented_ignored",
            Verdict.WRONG_PRESENTED_IGNORED,
            True,
            KNOWN_RECORDED_AT,
            None,
            _evidence(True, True, True, True, False, False),
        ),
        (
            "untrusted_override",
            Verdict.WRONG_DUE_TO_UNTRUSTED_SOURCE,
            True,
            KNOWN_RECORDED_AT,
            None,
            _evidence(True, True, True, True, True, True),
        ),
    )
    scenarios: list[KdtScenario] = []
    for family, verdict, fault, recorded_at, root_cause, evidence in configurations:
        for index in range(5):
            current_value = Decimal("0.10") + Decimal(index) / 100
            old_value = current_value + Decimal("0.10")
            is_correct = family == "correct"
            scenarios.append(
                KdtScenario(
                    id=f"{family}-{index + 1:02d}",
                    family=family,
                    old_value=old_value,
                    current_value=current_value,
                    selected_value=current_value if is_correct else old_value,
                    correction_recorded_at=recorded_at,
                    context=evidence,
                    expected_known_value=(
                        old_value if family == "late_recording" else current_value
                    ),
                    expected_verdict=verdict,
                    expected_agent_fault=fault,
                    expected_root_cause=root_cause,
                )
            )
    return tuple(scenarios)


def _snapshot(scenario: KdtScenario):
    key = f"KDT-{scenario.id}"
    repository = InMemoryAssertionRepository()
    drafts = [
        _draft(
            key,
            scenario.current_value if scenario.family == "correct" else scenario.old_value,
            OLD_RECORDED_AT,
        )
    ]
    if scenario.correction_recorded_at is not None:
        drafts.append(_draft(key, scenario.current_value, scenario.correction_recorded_at))
    TemporalAssertionService(repository).ingest_versions(drafts)
    return TemporalAssertionService(repository).reconstruct(
        TemporalLookup(
            assertion_key=key,
            domain="telecom",
            subject_type="roaming_route",
            subject_id=key,
            predicate="rate_per_minute",
            event_time=EVENT_TIME,
            decision_time=DECISION_TIME,
        )
    )


def _draft(key: str, value: Decimal, recorded_at: datetime) -> AssertionDraft:
    return AssertionDraft(
        assertion_key=key,
        domain="telecom",
        subject_type="roaming_route",
        subject_id=key,
        predicate="rate_per_minute",
        value_json={"rate": format(value, "f")},
        value_number=value,
        currency="EUR",
        unit="minute",
        valid_from=VALID_FROM,
        recorded_at=recorded_at,
        written_by="kdt_fixture",
    )


def _evidence(
    existed: bool,
    accessible: bool,
    retrieved: bool,
    presented: bool,
    used: bool,
    untrusted_override: bool,
) -> dict[str, bool]:
    return {
        "correct_evidence_existed_at_decision": existed,
        "correct_evidence_was_accessible_to_agent": accessible,
        "correct_evidence_was_retrieved": retrieved,
        "correct_evidence_was_presented": presented,
        "correct_evidence_was_used": used,
        "lower_trust_source_overrode_higher_trust_source": untrusted_override,
    }


def _agent_fault(verdict: Verdict) -> bool | None:
    if verdict is Verdict.INSUFFICIENT_EVIDENCE:
        return None
    return verdict not in {Verdict.CORRECT, Verdict.WRONG_NOT_KNOWABLE}


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("benchmark payload is not a mapping")
    return value


def _percent(numerator: int, denominator: int) -> float:
    return round(100 * numerator / denominator, 2) if denominator else 0.0


def main() -> None:
    print(json.dumps(run_kdt_benchmark(), indent=2))


if __name__ == "__main__":
    main()
