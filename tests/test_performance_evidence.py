from __future__ import annotations

import importlib.util
import json
from io import StringIO
from pathlib import Path
from uuid import UUID

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "performance_evidence.py"
SPEC = importlib.util.spec_from_file_location("performance_evidence", SCRIPT)
assert SPEC and SPEC.loader
performance_evidence = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(performance_evidence)

COMMIT_SHA = "1" * 40
IMAGE_DIGEST = "sha256:" + "2" * 64
REQUEST_ONE = "123e4567-e89b-42d3-a456-426614174000"
REQUEST_TWO = "223e4567-e89b-42d3-a456-426614174000"
REQUEST_THREE = "323e4567-e89b-42d3-a456-426614174000"
REQUEST_OBSERVED_AT = "2026-08-14T12:00:30Z"
SPAN_OBSERVED_AT = "2026-08-14T12:00:29Z"


def _request_event(
    *,
    path: str = "/ready",
    status_code: int = 200,
    duration_ms: int | float = 1,
    correlation_id: str = REQUEST_ONE,
    observed_at: str = REQUEST_OBSERVED_AT,
    **extra: object,
) -> dict[str, object]:
    return {
        "event": "request_complete",
        "correlation_id": correlation_id,
        "path": path,
        "status_code": status_code,
        "duration_ms": duration_ms,
        "observed_at": observed_at,
        **extra,
    }


def _span_event(
    *,
    component: str = "bedrock",
    operation: str = "converse",
    duration_ms: int | float = 1,
    outcome: object = "success",
    correlation_id: str = REQUEST_ONE,
    observed_at: str = SPAN_OBSERVED_AT,
    **extra: object,
) -> dict[str, object]:
    return {
        "event": "performance_span",
        "correlation_id": correlation_id,
        "component": component,
        "operation": operation,
        "duration_ms": duration_ms,
        "observed_at": observed_at,
        "outcome": outcome,
        **extra,
    }


def _metadata() -> dict[str, object]:
    return {
        "commit_sha": COMMIT_SHA,
        "image_digest": IMAGE_DIGEST,
        "configuration": {
            "deployment_profile": "showcase",
            "region": "eu-west-1",
            "task_cpu_units": 1024,
            "task_memory_mib": 2048,
            "desired_tasks": 1,
            "min_tasks": 1,
            "max_tasks": 1,
            "autoscaling_metric": "REQUEST_COUNT_PER_TARGET",
            "autoscaling_target_value": 300,
            "provider_concurrency": 4,
            "rate_limit_scale": 1,
            "database_pool_max_size": 5,
            "rate_limit_pool_max_size": 5,
            "server_limit_concurrency": 256,
            "server_backlog": 512,
            "window_started_at": "2026-08-14T12:00:00Z",
            "window_ended_at": "2026-08-14T12:01:00Z",
            "application_auth_enabled": False,
            "enhanced_observability": False,
            "request_cap": 20,
            "test_concurrency": 2,
            "duration_cap_seconds": 60,
            "retry_cap": 1,
            "bedrock_enabled": True,
            "vector_enabled": True,
            "mcp_enabled": True,
        },
    }


def _write_metadata(tmp_path: Path, value: dict[str, object] | None = None) -> Path:
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps(value or _metadata()), encoding="utf-8")
    return path


def _write_jsonl(tmp_path: Path, events: list[dict[str, object]]) -> Path:
    path = tmp_path / "events.jsonl"
    path.write_text("".join(f"{json.dumps(event)}\n" for event in events), encoding="utf-8")
    return path


def test_report_aggregates_requests_and_spans_without_copying_sensitive_data(
    tmp_path: Path,
) -> None:
    secret = "postgresql://user:password@private.example/hindsight"
    events = [
        _request_event(
            path=f"/decisions/{REQUEST_ONE}",
            duration_ms=10,
            ignored_secret=secret,
        ),
        _request_event(
            path=f"/decisions/{REQUEST_TWO}",
            status_code=500,
            duration_ms=30,
            correlation_id=REQUEST_TWO,
        ),
        _request_event(
            path=f"/decisions/{REQUEST_THREE}",
            status_code=429,
            duration_ms=20,
            correlation_id=REQUEST_THREE,
        ),
        _span_event(duration_ms=5),
        _span_event(duration_ms=15, outcome="error", error_type=secret),
        {"event": "unrelated", "payload": secret},
    ]

    report = performance_evidence.build_report(
        _write_jsonl(tmp_path, events),
        _write_metadata(tmp_path),
    )

    assert report["requests"]["status_counts"] == {"200": 1, "429": 1, "500": 1}
    assert report["requests"]["error_count"] == 2
    assert report["requests"]["by_path"] == [
        {
            "path": "/decisions/{uuid}",
            "count": 3,
            "error_count": 2,
            "status_counts": {"200": 1, "429": 1, "500": 1},
            "duration_ms": {"min": 10.0, "p50": 20.0, "p95": 30.0, "p99": 30.0, "max": 30.0},
        }
    ]
    assert report["spans"]["by_component_operation"] == [
        {
            "component": "bedrock",
            "operation": "converse",
            "count": 2,
            "error_count": 1,
            "outcome_counts": {"success": 1, "error": 1},
            "duration_ms": {"min": 5.0, "p50": 5.0, "p95": 15.0, "p99": 15.0, "max": 15.0},
        }
    ]
    assert report["source"]["ignored_events"] == 1
    assert report["source"]["observed_window"] == {
        "started_at": "2026-08-14T12:00:29.000Z",
        "ended_at": "2026-08-14T12:00:30.000Z",
    }
    serialized = json.dumps(report)
    assert secret not in serialized
    assert REQUEST_ONE not in serialized


def test_percentiles_use_the_documented_nearest_rank_method(tmp_path: Path) -> None:
    events = [
        _request_event(
            duration_ms=duration,
            correlation_id=str(UUID(int=duration)),
        )
        for duration in range(1, 101)
    ]
    metadata = _metadata()
    configuration = metadata["configuration"]
    assert isinstance(configuration, dict)
    configuration["request_cap"] = 100

    report = performance_evidence.build_report(
        _write_jsonl(tmp_path, events),
        _write_metadata(tmp_path, metadata),
    )

    assert report["requests"]["by_path"][0]["duration_ms"] == {
        "min": 1.0,
        "p50": 50.0,
        "p95": 95.0,
        "p99": 99.0,
        "max": 100.0,
    }


@pytest.mark.parametrize(
    ("path", "expected"),
    (
        ("/customers/alice-private", "/{other}"),
        ("/assets/alice-private", "/assets/{asset}"),
    ),
)
def test_paths_are_collapsed_instead_of_preserving_user_supplied_segments(
    tmp_path: Path,
    path: str,
    expected: str,
) -> None:
    input_path = _write_jsonl(
        tmp_path,
        [_request_event(path=path, status_code=404)],
    )

    report = performance_evidence.build_report(input_path, _write_metadata(tmp_path))

    assert report["requests"]["by_path"][0]["path"] == expected
    assert "alice-private" not in json.dumps(report)


@pytest.mark.parametrize(("limit", "message"), ((1, "line limit"), (20, "byte limit")))
def test_input_limits_fail_closed(tmp_path: Path, limit: int, message: str) -> None:
    input_path = _write_jsonl(
        tmp_path,
        [
            _request_event(),
            _request_event(
                path="/health",
                duration_ms=2,
                correlation_id=REQUEST_TWO,
            ),
        ],
    )
    kwargs = {"max_lines": limit} if message == "line limit" else {"max_input_bytes": limit}

    with pytest.raises(performance_evidence.EvidenceError, match=message):
        performance_evidence.build_report(input_path, _write_metadata(tmp_path), **kwargs)


def test_metadata_schema_rejects_arbitrary_configuration(tmp_path: Path) -> None:
    metadata = _metadata()
    configuration = metadata["configuration"]
    assert isinstance(configuration, dict)
    configuration["database_url"] = "postgresql://user:secret@example.test/db"

    with pytest.raises(
        performance_evidence.EvidenceError,
        match="documented closed schema",
    ):
        performance_evidence.build_report(
            _write_jsonl(tmp_path, []),
            _write_metadata(tmp_path, metadata),
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("region", "local", "AWS region"),
        ("provider_concurrency", 3, "release policy"),
        ("rate_limit_scale", 2, "deployed release"),
        ("duration_cap_seconds", 30, "duration_cap_seconds"),
    ),
)
def test_metadata_must_match_the_deployed_release_constraints(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    metadata = _metadata()
    configuration = metadata["configuration"]
    assert isinstance(configuration, dict)
    configuration[field] = value

    with pytest.raises(performance_evidence.EvidenceError, match=message):
        performance_evidence.build_report(
            _write_jsonl(tmp_path, [_request_event()]),
            _write_metadata(tmp_path, metadata),
        )


def test_production_evidence_accepts_reviewed_scaled_capacity(tmp_path: Path) -> None:
    metadata = _metadata()
    configuration = metadata["configuration"]
    assert isinstance(configuration, dict)
    configuration.update(
        {
            "deployment_profile": "production",
            "desired_tasks": 2,
            "min_tasks": 2,
            "max_tasks": 4,
            "provider_concurrency": 8,
            "rate_limit_scale": 2,
            "application_auth_enabled": True,
            "enhanced_observability": True,
        }
    )

    report = performance_evidence.build_report(
        _write_jsonl(tmp_path, [_request_event()]),
        _write_metadata(tmp_path, metadata),
    )

    normalized = report["metadata"]["configuration"]
    assert normalized["provider_concurrency"] == 8
    assert normalized["rate_limit_scale"] == 2


def test_metadata_rejects_mcp_without_bedrock(tmp_path: Path) -> None:
    metadata = _metadata()
    configuration = metadata["configuration"]
    assert isinstance(configuration, dict)
    configuration["bedrock_enabled"] = False

    with pytest.raises(performance_evidence.EvidenceError, match="MCP requires Bedrock"):
        performance_evidence.build_report(
            _write_jsonl(tmp_path, [_request_event()]),
            _write_metadata(tmp_path, metadata),
        )


def test_report_rejects_empty_or_unrelated_input(tmp_path: Path) -> None:
    with pytest.raises(performance_evidence.EvidenceError, match="no request_complete"):
        performance_evidence.build_report(
            _write_jsonl(tmp_path, [{"event": "unrelated"}]),
            _write_metadata(tmp_path),
        )


def test_report_rejects_orphan_spans(tmp_path: Path) -> None:
    with pytest.raises(performance_evidence.EvidenceError, match="no matching request_complete"):
        performance_evidence.build_report(
            _write_jsonl(
                tmp_path,
                [
                    _span_event(correlation_id=REQUEST_TWO),
                    _request_event(),
                ],
            ),
            _write_metadata(tmp_path),
        )


def test_report_enforces_the_observed_request_cap(tmp_path: Path) -> None:
    metadata = _metadata()
    configuration = metadata["configuration"]
    assert isinstance(configuration, dict)
    configuration["request_cap"] = 1
    configuration["test_concurrency"] = 1

    with pytest.raises(performance_evidence.EvidenceError, match="request_cap"):
        performance_evidence.build_report(
            _write_jsonl(
                tmp_path,
                [
                    _request_event(),
                    _request_event(correlation_id=REQUEST_TWO),
                ],
            ),
            _write_metadata(tmp_path, metadata),
        )


def test_report_rejects_events_outside_the_declared_window(tmp_path: Path) -> None:
    with pytest.raises(performance_evidence.EvidenceError, match="measurement window"):
        performance_evidence.build_report(
            _write_jsonl(
                tmp_path,
                [_request_event(observed_at="2026-08-14T12:01:01Z")],
            ),
            _write_metadata(tmp_path),
        )


@pytest.mark.parametrize(
    "event",
    (
        _request_event(observed_at="0001-01-01T00:00:00+14:00"),
        _request_event(duration_ms=10**400),
    ),
)
def test_extreme_numeric_and_timestamp_values_fail_cleanly(
    tmp_path: Path,
    event: dict[str, object],
) -> None:
    stdout = StringIO()
    stderr = StringIO()

    exit_code = performance_evidence.main(
        [
            "--input",
            str(_write_jsonl(tmp_path, [event])),
            "--metadata",
            str(_write_metadata(tmp_path)),
        ],
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 2
    assert stdout.getvalue() == ""
    assert stderr.getvalue().startswith("error:")


def test_non_scalar_untrusted_fields_fail_without_being_echoed(tmp_path: Path) -> None:
    secret = "must-not-be-echoed"
    input_path = _write_jsonl(
        tmp_path,
        [_span_event(outcome=[secret])],
    )
    stdout = StringIO()
    stderr = StringIO()

    exit_code = performance_evidence.main(
        ["--input", str(input_path), "--metadata", str(_write_metadata(tmp_path))],
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 2
    assert stdout.getvalue() == ""
    assert secret not in stderr.getvalue()
    assert "outcome" in stderr.getvalue()


def test_cli_output_is_byte_for_byte_deterministic(tmp_path: Path) -> None:
    input_path = _write_jsonl(
        tmp_path,
        [
            _span_event(
                component="cockroach",
                operation="memory.retrieve",
                duration_ms=12.5,
            ),
            _request_event(),
        ],
    )
    metadata_path = _write_metadata(tmp_path)
    first = StringIO()
    second = StringIO()
    arguments = ["--input", str(input_path), "--metadata", str(metadata_path)]

    assert performance_evidence.main(arguments, stdout=first) == 0
    assert performance_evidence.main(arguments, stdout=second) == 0

    assert first.getvalue() == second.getvalue()
    assert first.getvalue().endswith("\n")
    assert json.loads(first.getvalue())["schema_version"] == 1
