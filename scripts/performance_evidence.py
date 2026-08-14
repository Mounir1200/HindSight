"""Build a deterministic, sanitized performance report from HindSight JSONL logs.

This tool is deliberately offline. It reads two local files and writes one JSON
document to stdout; it never starts a workload or connects to a remote service.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, TextIO
from uuid import UUID

SCHEMA_VERSION = 1
HARD_MAX_INPUT_BYTES = 16 * 1024 * 1024
HARD_MAX_LINES = 100_000
MAX_LINE_BYTES = 64 * 1024
MAX_METADATA_BYTES = 16 * 1024
MAX_GROUPS = 256
MAX_DURATION_MS = 86_400_000.0

_COMMIT_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_IMAGE_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_LABEL_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_REGION_PATTERN = re.compile(r"^(?:local|[a-z]{2}(?:-[a-z0-9]+)+-[0-9])$")
_DEPLOYMENT_PROFILES = frozenset({"local", "showcase", "production"})
_DEPLOYED_PROVIDER_CONCURRENCY_VALUES = frozenset({1, 2, 4, 8, 16, 32, 64})
_DEPLOYED_RATE_LIMIT_SCALE_VALUES = frozenset({0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0})
_AUTOSCALING_METRICS = frozenset(
    {"AVERAGE_CPU", "AVERAGE_MEMORY", "REQUEST_COUNT_PER_TARGET", "disabled"}
)
_SAFE_PATH_SEGMENT_PATTERN = re.compile(r"^[a-z][a-z0-9._~-]{0,63}$")
_UUID_SEGMENT_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_OPAQUE_ID_PATTERN = re.compile(r"^(?:[0-9a-f]{16,}|[A-Za-z0-9_-]{32,})$")
_KNOWN_NORMALIZED_PATH_PATTERN = re.compile(
    r"^(?:/|/\{other\}|/health|/ready|/demo/(?:prepare|reset|seed|workspace)|"
    r"/memories/search|/decisions/(?:\{uuid\}|\{number\}|\{id\})"
    r"(?:/(?:truth|knowledge|evidence|verdict))?|/assets/\{asset\})$"
)

_CONFIGURATION_FIELDS = frozenset(
    {
        "deployment_profile",
        "region",
        "task_cpu_units",
        "task_memory_mib",
        "desired_tasks",
        "min_tasks",
        "max_tasks",
        "autoscaling_metric",
        "autoscaling_target_value",
        "provider_concurrency",
        "rate_limit_scale",
        "request_cap",
        "test_concurrency",
        "duration_cap_seconds",
        "retry_cap",
        "database_pool_max_size",
        "rate_limit_pool_max_size",
        "server_limit_concurrency",
        "server_backlog",
        "window_started_at",
        "window_ended_at",
        "application_auth_enabled",
        "enhanced_observability",
        "bedrock_enabled",
        "vector_enabled",
        "mcp_enabled",
    }
)


class EvidenceError(ValueError):
    """Raised when local evidence input violates the closed, bounded schema."""


def _integer(
    value: object,
    field: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EvidenceError(f"metadata {field} must be an integer")
    if not minimum <= value <= maximum:
        raise EvidenceError(f"metadata {field} is outside the allowed range")
    return value


def _boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise EvidenceError(f"metadata {field} must be a boolean")
    return value


def _allowed_number(value: object, field: str, *, allowed: frozenset[float]) -> int | float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise EvidenceError(f"metadata {field} must be a number")
    try:
        normalized = float(value)
    except OverflowError as error:
        raise EvidenceError(f"metadata {field} is outside the allowed values") from error
    if not math.isfinite(normalized) or normalized not in allowed:
        raise EvidenceError(f"metadata {field} is outside the allowed values")
    return int(normalized) if normalized.is_integer() else normalized


def _timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or len(value) > 35:
        raise EvidenceError(f"{field} must be a bounded ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (OverflowError, ValueError) as error:
        raise EvidenceError(f"{field} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvidenceError(f"{field} must include a timezone")
    try:
        return parsed.astimezone(UTC)
    except (OverflowError, ValueError) as error:
        raise EvidenceError(f"{field} cannot be represented in UTC") from error


def _timestamp_text(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _load_metadata(path: Path) -> dict[str, Any]:
    try:
        size = path.stat().st_size
    except OSError as error:
        raise EvidenceError("metadata file cannot be read") from error
    if size > MAX_METADATA_BYTES:
        raise EvidenceError("metadata file exceeds the size limit")

    try:
        raw = path.read_bytes()
    except OSError as error:
        raise EvidenceError("metadata file cannot be read") from error
    if len(raw) > MAX_METADATA_BYTES:
        raise EvidenceError("metadata file exceeds the size limit")
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise EvidenceError("metadata file must contain one UTF-8 JSON object") from error
    if not isinstance(value, dict) or set(value) != {
        "commit_sha",
        "image_digest",
        "configuration",
    }:
        raise EvidenceError("metadata must use the documented closed schema")

    commit_sha = value["commit_sha"]
    image_digest = value["image_digest"]
    configuration = value["configuration"]
    if not isinstance(commit_sha, str) or _COMMIT_PATTERN.fullmatch(commit_sha.lower()) is None:
        raise EvidenceError("metadata commit_sha must be a full hexadecimal Git object id")
    if (
        not isinstance(image_digest, str)
        or _IMAGE_DIGEST_PATTERN.fullmatch(image_digest.lower()) is None
    ):
        raise EvidenceError("metadata image_digest must be a sha256 image digest")
    if not isinstance(configuration, dict) or set(configuration) != _CONFIGURATION_FIELDS:
        raise EvidenceError("metadata configuration must use the documented closed schema")

    profile = configuration["deployment_profile"]
    region = configuration["region"]
    if not isinstance(profile, str) or profile not in _DEPLOYMENT_PROFILES:
        raise EvidenceError("metadata deployment_profile must be local, showcase, or production")
    if not isinstance(region, str) or _REGION_PATTERN.fullmatch(region) is None:
        raise EvidenceError("metadata region must be 'local' or an AWS region name")
    autoscaling_metric = configuration["autoscaling_metric"]
    if not isinstance(autoscaling_metric, str) or autoscaling_metric not in _AUTOSCALING_METRICS:
        raise EvidenceError("metadata autoscaling_metric is unsupported")

    window_started_at = _timestamp(
        configuration["window_started_at"],
        "metadata window_started_at",
    )
    window_ended_at = _timestamp(
        configuration["window_ended_at"],
        "metadata window_ended_at",
    )
    if window_ended_at <= window_started_at:
        raise EvidenceError("metadata measurement window must end after it starts")
    if (window_ended_at - window_started_at).total_seconds() > 86_400:
        raise EvidenceError("metadata measurement window cannot exceed 24 hours")

    normalized_configuration = {
        "deployment_profile": profile,
        "region": region,
        "task_cpu_units": _integer(
            configuration["task_cpu_units"], "task_cpu_units", minimum=1, maximum=1_048_576
        ),
        "task_memory_mib": _integer(
            configuration["task_memory_mib"], "task_memory_mib", minimum=1, maximum=1_048_576
        ),
        "desired_tasks": _integer(
            configuration["desired_tasks"], "desired_tasks", minimum=1, maximum=10_000
        ),
        "min_tasks": _integer(configuration["min_tasks"], "min_tasks", minimum=0, maximum=10_000),
        "max_tasks": _integer(configuration["max_tasks"], "max_tasks", minimum=1, maximum=10_000),
        "autoscaling_metric": autoscaling_metric,
        "autoscaling_target_value": _integer(
            configuration["autoscaling_target_value"],
            "autoscaling_target_value",
            minimum=0 if autoscaling_metric == "disabled" else 1,
            maximum=10_000,
        ),
        "provider_concurrency": _integer(
            configuration["provider_concurrency"],
            "provider_concurrency",
            minimum=1,
            maximum=10_000,
        ),
        "rate_limit_scale": _allowed_number(
            configuration["rate_limit_scale"],
            "rate_limit_scale",
            allowed=_DEPLOYED_RATE_LIMIT_SCALE_VALUES,
        ),
        "request_cap": _integer(
            configuration["request_cap"], "request_cap", minimum=1, maximum=HARD_MAX_LINES
        ),
        "test_concurrency": _integer(
            configuration["test_concurrency"],
            "test_concurrency",
            minimum=1,
            maximum=10_000,
        ),
        "duration_cap_seconds": _integer(
            configuration["duration_cap_seconds"],
            "duration_cap_seconds",
            minimum=1,
            maximum=86_400,
        ),
        "retry_cap": _integer(configuration["retry_cap"], "retry_cap", minimum=0, maximum=100),
        "database_pool_max_size": _integer(
            configuration["database_pool_max_size"],
            "database_pool_max_size",
            minimum=1,
            maximum=50,
        ),
        "rate_limit_pool_max_size": _integer(
            configuration["rate_limit_pool_max_size"],
            "rate_limit_pool_max_size",
            minimum=1,
            maximum=20,
        ),
        "server_limit_concurrency": _integer(
            configuration["server_limit_concurrency"],
            "server_limit_concurrency",
            minimum=16,
            maximum=4_096,
        ),
        "server_backlog": _integer(
            configuration["server_backlog"],
            "server_backlog",
            minimum=64,
            maximum=8_192,
        ),
        "window_started_at": _timestamp_text(window_started_at),
        "window_ended_at": _timestamp_text(window_ended_at),
        "application_auth_enabled": _boolean(
            configuration["application_auth_enabled"],
            "application_auth_enabled",
        ),
        "enhanced_observability": _boolean(
            configuration["enhanced_observability"],
            "enhanced_observability",
        ),
        "bedrock_enabled": _boolean(configuration["bedrock_enabled"], "bedrock_enabled"),
        "vector_enabled": _boolean(configuration["vector_enabled"], "vector_enabled"),
        "mcp_enabled": _boolean(configuration["mcp_enabled"], "mcp_enabled"),
    }
    if not (
        normalized_configuration["min_tasks"]
        <= normalized_configuration["desired_tasks"]
        <= normalized_configuration["max_tasks"]
    ):
        raise EvidenceError(
            "metadata task counts must satisfy min_tasks <= desired_tasks <= max_tasks"
        )
    if normalized_configuration["test_concurrency"] > normalized_configuration["request_cap"]:
        raise EvidenceError("metadata test_concurrency cannot exceed request_cap")
    if (window_ended_at - window_started_at).total_seconds() > normalized_configuration[
        "duration_cap_seconds"
    ]:
        raise EvidenceError("metadata measurement window cannot exceed duration_cap_seconds")
    if (
        autoscaling_metric == "disabled"
        and normalized_configuration["autoscaling_target_value"] != 0
    ):
        raise EvidenceError("metadata disabled autoscaling must use a zero target")
    if normalized_configuration["mcp_enabled"] and not normalized_configuration["bedrock_enabled"]:
        raise EvidenceError("metadata MCP requires Bedrock")
    if profile == "local":
        if region != "local":
            raise EvidenceError("metadata local profile requires region 'local'")
        if autoscaling_metric != "disabled":
            raise EvidenceError("metadata local profile requires disabled autoscaling")
    else:
        if region == "local":
            raise EvidenceError("metadata deployed profiles require an AWS region")
        if autoscaling_metric == "disabled":
            raise EvidenceError("metadata deployed profiles require an autoscaling metric")
        if not 10 <= normalized_configuration["autoscaling_target_value"] <= 1_000:
            raise EvidenceError(
                "metadata deployed autoscaling_target_value must match the IaC range"
            )
        if (
            normalized_configuration["task_cpu_units"],
            normalized_configuration["task_memory_mib"],
        ) != (1_024, 2_048):
            raise EvidenceError("metadata deployed task size must match the IaC task size")
        if normalized_configuration["min_tasks"] not in {1, 2, 4, 8} or normalized_configuration[
            "max_tasks"
        ] not in {1, 2, 4, 8, 16, 20}:
            raise EvidenceError("metadata deployed task capacity must match the IaC values")
        if normalized_configuration["database_pool_max_size"] not in {1, 2, 5, 10, 20}:
            raise EvidenceError("metadata database pool ceiling must match the IaC values")
        if (
            normalized_configuration["provider_concurrency"]
            not in _DEPLOYED_PROVIDER_CONCURRENCY_VALUES
        ):
            raise EvidenceError(
                "metadata provider_concurrency must match the deployed release policy"
            )
    if profile == "showcase" and (
        normalized_configuration["min_tasks"],
        normalized_configuration["desired_tasks"],
        normalized_configuration["max_tasks"],
    ) != (1, 1, 1):
        raise EvidenceError("metadata showcase task capacity must be fixed at one")
    if profile == "showcase" and (
        normalized_configuration["database_pool_max_size"],
        normalized_configuration["rate_limit_pool_max_size"],
    ) != (5, 5):
        raise EvidenceError("metadata showcase pool ceilings must both be fixed at five")
    if profile == "showcase" and normalized_configuration["provider_concurrency"] != 4:
        raise EvidenceError("metadata showcase provider_concurrency must match the release policy")
    if profile == "showcase" and normalized_configuration["rate_limit_scale"] != 1:
        raise EvidenceError("metadata showcase rate_limit_scale must match the deployed release")
    if profile == "production" and (
        normalized_configuration["min_tasks"] < 2
        or not normalized_configuration["application_auth_enabled"]
        or not normalized_configuration["enhanced_observability"]
    ):
        raise EvidenceError(
            "metadata production requires multi-task capacity, auth, and observability"
        )

    return {
        "commit_sha": commit_sha.lower(),
        "image_digest": image_digest.lower(),
        "configuration": normalized_configuration,
    }


def _duration(value: object, line_number: int) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise EvidenceError(f"line {line_number}: duration_ms must be a number")
    try:
        normalized = float(value)
    except OverflowError as error:
        raise EvidenceError(
            f"line {line_number}: duration_ms is outside the allowed range"
        ) from error
    if not math.isfinite(normalized) or not 0.0 <= normalized <= MAX_DURATION_MS:
        raise EvidenceError(f"line {line_number}: duration_ms is outside the allowed range")
    return normalized


def _status(value: object, line_number: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 100 <= value <= 599:
        raise EvidenceError(f"line {line_number}: status_code must be an HTTP status")
    return value


def _label(value: object, field: str, line_number: int) -> str:
    if not isinstance(value, str) or _LABEL_PATTERN.fullmatch(value) is None:
        raise EvidenceError(f"line {line_number}: {field} must be a bounded lowercase label")
    return value


def _safe_path(value: object, line_number: int) -> str:
    if not isinstance(value, str) or not value.startswith("/") or len(value) > 256:
        raise EvidenceError(f"line {line_number}: path must be a bounded absolute path")
    if "?" in value or "#" in value or any(ord(character) < 32 for character in value):
        raise EvidenceError(f"line {line_number}: path contains unsupported data")
    if _KNOWN_NORMALIZED_PATH_PATTERN.fullmatch(value):
        return value
    if value.startswith("/assets/"):
        return "/assets/{asset}"

    normalized_segments = []
    for segment in value.split("/")[1:]:
        if not segment:
            normalized_segments.append("")
        elif _UUID_SEGMENT_PATTERN.fullmatch(segment):
            normalized_segments.append("{uuid}")
        elif segment.isdecimal():
            normalized_segments.append("{number}")
        elif _OPAQUE_ID_PATTERN.fullmatch(segment):
            normalized_segments.append("{id}")
        elif _SAFE_PATH_SEGMENT_PATTERN.fullmatch(segment):
            normalized_segments.append(segment)
        else:
            normalized_segments.append("{redacted}")
    normalized = "/" + "/".join(normalized_segments)
    return normalized if _KNOWN_NORMALIZED_PATH_PATTERN.fullmatch(normalized) else "/{other}"


def _correlation_id(value: object, line_number: int) -> str:
    if not isinstance(value, str) or len(value) != 36:
        raise EvidenceError(f"line {line_number}: correlation_id must be a canonical UUID")
    try:
        normalized = str(UUID(value))
    except (AttributeError, TypeError, ValueError) as error:
        raise EvidenceError(
            f"line {line_number}: correlation_id must be a canonical UUID"
        ) from error
    if value != normalized:
        raise EvidenceError(f"line {line_number}: correlation_id must be a canonical UUID")
    return normalized


def _event_timestamp(
    value: object,
    line_number: int,
    window_started_at: datetime,
    window_ended_at: datetime,
) -> datetime:
    observed_at = _timestamp(value, f"line {line_number}: observed_at")
    if not window_started_at <= observed_at <= window_ended_at:
        raise EvidenceError(
            f"line {line_number}: observed_at is outside the metadata measurement window"
        )
    return observed_at


def _percentile(sorted_values: Sequence[float], percentile: float) -> float | None:
    if not sorted_values:
        return None
    index = max(0, math.ceil(percentile * len(sorted_values)) - 1)
    return sorted_values[index]


def _duration_summary(values: Sequence[float]) -> dict[str, float | None]:
    ordered = sorted(values)
    return {
        "min": ordered[0] if ordered else None,
        "p50": _percentile(ordered, 0.50),
        "p95": _percentile(ordered, 0.95),
        "p99": _percentile(ordered, 0.99),
        "max": ordered[-1] if ordered else None,
    }


def _status_counts(values: Counter[int]) -> dict[str, int]:
    return {str(status): values[status] for status in sorted(values)}


def _outcome_counts(values: Counter[str]) -> dict[str, int]:
    return {outcome: values[outcome] for outcome in ("success", "error") if values[outcome]}


def _readline(stream: BinaryIO) -> bytes:
    line = stream.readline(MAX_LINE_BYTES + 1)
    if len(line) > MAX_LINE_BYTES:
        raise EvidenceError("input contains a line that exceeds the line-size limit")
    return line


def build_report(
    input_path: Path,
    metadata_path: Path,
    *,
    max_input_bytes: int = HARD_MAX_INPUT_BYTES,
    max_lines: int = HARD_MAX_LINES,
) -> dict[str, Any]:
    """Return a closed-schema report from bounded, local JSONL input."""

    if not 1 <= max_input_bytes <= HARD_MAX_INPUT_BYTES:
        raise EvidenceError("max_input_bytes is outside the hard limit")
    if not 1 <= max_lines <= HARD_MAX_LINES:
        raise EvidenceError("max_lines is outside the hard limit")
    metadata = _load_metadata(metadata_path)
    configuration = metadata["configuration"]
    window_started_at = _timestamp(
        configuration["window_started_at"],
        "metadata window_started_at",
    )
    window_ended_at = _timestamp(
        configuration["window_ended_at"],
        "metadata window_ended_at",
    )
    request_cap = configuration["request_cap"]

    try:
        input_size = input_path.stat().st_size
    except OSError as error:
        raise EvidenceError("input file cannot be read") from error
    if input_size > max_input_bytes:
        raise EvidenceError("input file exceeds the configured byte limit")

    request_durations: list[float] = []
    request_statuses: Counter[int] = Counter()
    requests_by_path: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"durations": [], "statuses": Counter()}
    )
    span_durations: list[float] = []
    span_outcomes: Counter[str] = Counter()
    spans_by_key: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {"durations": [], "outcomes": Counter()}
    )
    request_completed_at: dict[str, datetime] = {}
    span_observations: list[tuple[str, datetime, int]] = []
    observed_timestamps: list[datetime] = []
    digest = hashlib.sha256()
    bytes_read = 0
    lines_read = 0
    blank_lines = 0
    ignored_events = 0

    try:
        with input_path.open("rb") as stream:
            while raw_line := _readline(stream):
                lines_read += 1
                if lines_read > max_lines:
                    raise EvidenceError("input file exceeds the configured line limit")
                bytes_read += len(raw_line)
                if bytes_read > max_input_bytes:
                    raise EvidenceError("input file exceeds the configured byte limit")
                digest.update(raw_line)
                if not raw_line.strip():
                    blank_lines += 1
                    continue
                try:
                    encoding = "utf-8-sig" if lines_read == 1 else "utf-8"
                    event = json.loads(raw_line.decode(encoding))
                except (UnicodeDecodeError, ValueError, RecursionError) as error:
                    raise EvidenceError(
                        f"line {lines_read}: expected one UTF-8 JSON object"
                    ) from error
                if not isinstance(event, Mapping):
                    raise EvidenceError(f"line {lines_read}: expected one JSON object")

                event_name = event.get("event")
                if event_name == "request_complete":
                    correlation_id = _correlation_id(event.get("correlation_id"), lines_read)
                    if correlation_id in request_completed_at:
                        raise EvidenceError(f"line {lines_read}: duplicate request correlation_id")
                    observed_at = _event_timestamp(
                        event.get("observed_at"),
                        lines_read,
                        window_started_at,
                        window_ended_at,
                    )
                    path = _safe_path(event.get("path"), lines_read)
                    duration = _duration(event.get("duration_ms"), lines_read)
                    status = _status(event.get("status_code"), lines_read)
                    if len(request_durations) >= request_cap:
                        raise EvidenceError("input exceeds metadata request_cap")
                    if path not in requests_by_path and len(requests_by_path) >= MAX_GROUPS:
                        raise EvidenceError(
                            "input exceeds the maximum number of request path groups"
                        )
                    request_completed_at[correlation_id] = observed_at
                    observed_timestamps.append(observed_at)
                    request_durations.append(duration)
                    request_statuses[status] += 1
                    requests_by_path[path]["durations"].append(duration)
                    requests_by_path[path]["statuses"][status] += 1
                elif event_name == "performance_span":
                    correlation_id = _correlation_id(event.get("correlation_id"), lines_read)
                    observed_at = _event_timestamp(
                        event.get("observed_at"),
                        lines_read,
                        window_started_at,
                        window_ended_at,
                    )
                    component = _label(event.get("component"), "component", lines_read)
                    operation = _label(event.get("operation"), "operation", lines_read)
                    duration = _duration(event.get("duration_ms"), lines_read)
                    outcome = event.get("outcome")
                    if not isinstance(outcome, str) or outcome not in {"success", "error"}:
                        raise EvidenceError(
                            f"line {lines_read}: outcome must be 'success' or 'error'"
                        )
                    key = (component, operation)
                    if key not in spans_by_key and len(spans_by_key) >= MAX_GROUPS:
                        raise EvidenceError("input exceeds the maximum number of span groups")
                    span_observations.append((correlation_id, observed_at, lines_read))
                    observed_timestamps.append(observed_at)
                    span_durations.append(duration)
                    span_outcomes[outcome] += 1
                    spans_by_key[key]["durations"].append(duration)
                    spans_by_key[key]["outcomes"][outcome] += 1
                else:
                    ignored_events += 1
    except OSError as error:
        raise EvidenceError("input file cannot be read") from error

    if not request_completed_at:
        raise EvidenceError("input contains no request_complete events")
    for correlation_id, observed_at, line_number in span_observations:
        request_observed_at = request_completed_at.get(correlation_id)
        if request_observed_at is None:
            raise EvidenceError(
                f"line {line_number}: performance_span has no matching request_complete"
            )
        if observed_at > request_observed_at:
            raise EvidenceError(f"line {line_number}: performance_span completes after its request")

    request_groups = []
    for path in sorted(requests_by_path):
        group = requests_by_path[path]
        statuses = group["statuses"]
        request_groups.append(
            {
                "path": path,
                "count": len(group["durations"]),
                "error_count": sum(count for status, count in statuses.items() if status >= 400),
                "status_counts": _status_counts(statuses),
                "duration_ms": _duration_summary(group["durations"]),
            }
        )

    span_groups = []
    for component, operation in sorted(spans_by_key):
        group = spans_by_key[(component, operation)]
        outcomes = group["outcomes"]
        span_groups.append(
            {
                "component": component,
                "operation": operation,
                "count": len(group["durations"]),
                "error_count": outcomes["error"],
                "outcome_counts": _outcome_counts(outcomes),
                "duration_ms": _duration_summary(group["durations"]),
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "metadata": metadata,
        "source": {
            "sha256": digest.hexdigest(),
            "bytes": bytes_read,
            "lines": lines_read,
            "blank_lines": blank_lines,
            "ignored_events": ignored_events,
            "observed_window": {
                "started_at": _timestamp_text(min(observed_timestamps)),
                "ended_at": _timestamp_text(max(observed_timestamps)),
            },
        },
        "requests": {
            "count": len(request_durations),
            "error_count": sum(
                count for status, count in request_statuses.items() if status >= 400
            ),
            "status_counts": _status_counts(request_statuses),
            "duration_ms": _duration_summary(request_durations),
            "by_path": request_groups,
        },
        "spans": {
            "count": len(span_durations),
            "error_count": span_outcomes["error"],
            "outcome_counts": _outcome_counts(span_outcomes),
            "duration_ms": _duration_summary(span_durations),
            "by_component_operation": span_groups,
        },
    }


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    parser = argparse.ArgumentParser(
        description="Build sanitized performance evidence from local HindSight JSONL logs"
    )
    parser.add_argument("--input", required=True, type=Path, help="local JSONL log file")
    parser.add_argument("--metadata", required=True, type=Path, help="closed-schema metadata JSON")
    parser.add_argument(
        "--max-input-bytes",
        type=int,
        default=HARD_MAX_INPUT_BYTES,
        help=f"lower byte limit (hard maximum: {HARD_MAX_INPUT_BYTES})",
    )
    parser.add_argument(
        "--max-lines",
        type=int,
        default=HARD_MAX_LINES,
        help=f"lower line limit (hard maximum: {HARD_MAX_LINES})",
    )
    args = parser.parse_args(argv)

    try:
        report = build_report(
            args.input,
            args.metadata,
            max_input_bytes=args.max_input_bytes,
            max_lines=args.max_lines,
        )
    except EvidenceError as error:
        print(f"error: {error}", file=stderr)
        return 2

    print(json.dumps(report, indent=2, sort_keys=True), file=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
