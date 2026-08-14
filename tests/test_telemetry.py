import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID

import pytest

from hindsight.telemetry import (
    MAX_PERFORMANCE_SPANS,
    current_correlation_id,
    current_performance_span,
    current_performance_trace,
    performance_span,
    performance_trace,
)

CORRELATION_ID = UUID("0eaf4168-fd2a-41ca-a655-677a24473399")


def _clock(*values: float):
    readings: Iterator[float] = iter(values)
    return lambda: next(readings)


def test_performance_span_emits_a_bounded_success_event(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("test.performance.success")

    with (
        caplog.at_level(logging.INFO, logger=logger.name),
        performance_span(
            correlation_id=CORRELATION_ID,
            component="cockroach",
            operation="memory.retrieve",
            clock=_clock(10.0, 10.123456),
            logger=logger,
        ),
    ):
        pass

    assert len(caplog.records) == 1
    event = json.loads(caplog.records[0].message)
    observed_at = datetime.fromisoformat(event.pop("observed_at").replace("Z", "+00:00"))
    assert observed_at.tzinfo == UTC
    assert event == {
        "event": "performance_span",
        "correlation_id": str(CORRELATION_ID),
        "component": "cockroach",
        "operation": "memory.retrieve",
        "duration_ms": 123.46,
        "outcome": "success",
        "error_type": None,
    }


def test_performance_span_reraises_and_never_logs_exception_details(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("test.performance.error")
    secret = "postgresql://user:password@private.example/hindsight"

    with (
        caplog.at_level(logging.INFO, logger=logger.name),
        pytest.raises(RuntimeError, match="postgresql"),
        performance_span(
            correlation_id=str(CORRELATION_ID),
            component="bedrock",
            operation="converse",
            clock=_clock(20.0, 20.5),
            logger=logger,
        ),
    ):
        raise RuntimeError(secret)

    event = json.loads(caplog.records[0].message)
    assert event["outcome"] == "error"
    assert event["error_type"] == "RuntimeError"
    assert event["duration_ms"] == 500.0
    assert datetime.fromisoformat(event["observed_at"].replace("Z", "+00:00")).tzinfo == UTC
    assert secret not in caplog.records[0].message
    assert "password" not in caplog.records[0].message


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("correlation_id", "customer@example.com"),
        ("component", "database/customer-42"),
        ("operation", "SELECT private_data"),
    ),
)
def test_performance_span_rejects_unbounded_labels(field: str, value: str) -> None:
    arguments: dict[str, object] = {
        "correlation_id": CORRELATION_ID,
        "component": "database",
        "operation": "query",
    }
    arguments[field] = value

    with pytest.raises(ValueError), performance_span(**arguments):  # type: ignore[arg-type]
        pass


def test_performance_span_clamps_a_backwards_clock(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("test.performance.clock")
    with (
        caplog.at_level(logging.INFO, logger=logger.name),
        performance_span(
            correlation_id=CORRELATION_ID,
            component="database",
            operation="query",
            clock=_clock(2.0, 1.0),
            logger=logger,
        ),
    ):
        pass

    assert json.loads(caplog.records[0].message)["duration_ms"] == 0.0


def test_performance_trace_collects_nested_spans_and_restores_context() -> None:
    assert current_correlation_id() is None

    with performance_trace(CORRELATION_ID):
        assert current_correlation_id() == CORRELATION_ID
        with current_performance_span(
            component="bedrock",
            operation="converse",
            clock=_clock(3.0, 3.25),
            logger=logging.getLogger("test.performance.trace"),
        ):
            pass
        events = current_performance_trace()

    assert current_correlation_id() is None
    assert len(events) == 1
    assert events[0]["correlation_id"] == str(CORRELATION_ID)
    assert events[0]["duration_ms"] == 250.0
    assert isinstance(events[0]["observed_at"], str)


def test_current_span_is_a_noop_without_a_bound_trace(caplog: pytest.LogCaptureFixture) -> None:
    with (
        caplog.at_level(logging.INFO, logger="test.performance.noop"),
        current_performance_span(
            component="database",
            operation="query",
            clock=_clock(1.0, 2.0),
            logger=logging.getLogger("test.performance.noop"),
        ),
    ):
        pass

    assert caplog.records == []


def test_performance_trace_is_bounded() -> None:
    with performance_trace(CORRELATION_ID):
        for _ in range(MAX_PERFORMANCE_SPANS + 5):
            with current_performance_span(
                component="database",
                operation="query",
                clock=_clock(1.0, 1.001),
                logger=logging.getLogger("test.performance.bounded"),
            ):
                pass
        events = current_performance_trace()

    assert len(events) == MAX_PERFORMANCE_SPANS
