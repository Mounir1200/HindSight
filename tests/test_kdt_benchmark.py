import json
from pathlib import Path

from hindsight.benchmarks.kdt import run_kdt_benchmark

ROOT = Path(__file__).resolve().parents[1]


def test_kdt_benchmark_meets_the_published_targets() -> None:
    result = run_kdt_benchmark()
    metrics = result["metrics"]

    assert result["benchmark"] == "kdt-synthetic-v1"
    assert result["scenario_count"] == 35
    assert result["passed"] is True
    assert metrics["verdict_accuracy_pct"] >= 90
    assert metrics["unjustified_blame_rate_pct"] <= 5
    assert metrics["provenance_completeness_pct"] == 100
    assert metrics["duplicate_remediation_rate_pct"] == 0
    assert metrics["idempotent_remediation_pct"] == 100
    assert result == json.loads(
        (ROOT / "benchmarks" / "kdt" / "results.json").read_text(encoding="utf-8")
    )
