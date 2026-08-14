"""Small, dependency-free performance spans for synchronous operations."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock
from time import perf_counter
from uuid import UUID

PERFORMANCE_LOGGER_NAME = "hindsight.performance"
MAX_PERFORMANCE_SPANS = 64
_LABEL_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")

Clock = Callable[[], float]
PerformanceEvent = dict[str, object]


@dataclass(slots=True)
class _TraceCollector:
    correlation_id: str
    _events: list[PerformanceEvent] = field(default_factory=list)
    _lock: Lock = field(default_factory=Lock)

    def append(self, event: PerformanceEvent) -> None:
        with self._lock:
            if len(self._events) < MAX_PERFORMANCE_SPANS:
                self._events.append(dict(event))

    def snapshot(self) -> tuple[PerformanceEvent, ...]:
        with self._lock:
            return tuple(dict(event) for event in self._events)


_CURRENT_CORRELATION_ID: ContextVar[str | None] = ContextVar(
    "hindsight_correlation_id",
    default=None,
)
_CURRENT_TRACE: ContextVar[_TraceCollector | None] = ContextVar(
    "hindsight_performance_trace",
    default=None,
)


@contextmanager
def performance_trace(correlation_id: UUID | str) -> Iterator[None]:
    """Bind a correlation ID and a bounded span collector to the current execution.

    Context variables are copied by Starlette/AnyIO when synchronous work moves to
    a worker thread. The collector itself is therefore shared safely across the
    complete HTTP -> agent -> provider execution without passing observability
    concerns through every domain interface.
    """

    normalized = _correlation_id(correlation_id)
    current = _CURRENT_TRACE.get()
    if current is not None and current.correlation_id == normalized:
        yield
        return

    correlation_token = _CURRENT_CORRELATION_ID.set(normalized)
    trace_token = _CURRENT_TRACE.set(_TraceCollector(normalized))
    try:
        yield
    finally:
        _CURRENT_TRACE.reset(trace_token)
        _CURRENT_CORRELATION_ID.reset(correlation_token)


def current_correlation_id() -> UUID | None:
    value = _CURRENT_CORRELATION_ID.get()
    return UUID(value) if value is not None else None


def current_performance_trace() -> tuple[PerformanceEvent, ...]:
    collector = _CURRENT_TRACE.get()
    return collector.snapshot() if collector is not None else ()


@contextmanager
def current_performance_span(
    *,
    component: str,
    operation: str,
    clock: Clock = perf_counter,
    logger: logging.Logger | None = None,
) -> Iterator[None]:
    """Measure against the bound request trace, or become a safe no-op.

    Provider and repository objects are also used by CLI commands and focused unit
    tests. Treating an absent request context as a no-op keeps those paths reusable
    while every web-triggered execution remains correlated automatically.
    """

    correlation_id = _CURRENT_CORRELATION_ID.get()
    if correlation_id is None:
        yield
        return
    with performance_span(
        correlation_id=correlation_id,
        component=component,
        operation=operation,
        clock=clock,
        logger=logger,
    ):
        yield


@contextmanager
def performance_span(
    *,
    correlation_id: UUID | str,
    component: str,
    operation: str,
    clock: Clock = perf_counter,
    logger: logging.Logger | None = None,
) -> Iterator[None]:
    """Measure an operation and emit one bounded JSON event when it finishes.

    The event schema is deliberately closed: exception messages, operation
    inputs, database identifiers, and arbitrary caller metadata are never
    serialized. ``component`` and ``operation`` must be stable code-defined
    labels rather than request data.
    """

    normalized_correlation_id = _correlation_id(correlation_id)
    normalized_component = _label(component, "component")
    normalized_operation = _label(operation, "operation")
    resolved_logger = logger if logger is not None else logging.getLogger(PERFORMANCE_LOGGER_NAME)
    started_at = clock()

    try:
        yield
    except BaseException as error:
        _emit_span(
            resolved_logger,
            normalized_correlation_id,
            normalized_component,
            normalized_operation,
            started_at,
            clock(),
            outcome="error",
            error_type=type(error).__name__,
        )
        raise
    else:
        _emit_span(
            resolved_logger,
            normalized_correlation_id,
            normalized_component,
            normalized_operation,
            started_at,
            clock(),
            outcome="success",
            error_type=None,
        )


def _emit_span(
    logger: logging.Logger,
    correlation_id: str,
    component: str,
    operation: str,
    started_at: float,
    completed_at: float,
    *,
    outcome: str,
    error_type: str | None,
) -> None:
    duration_ms = round(max(0.0, completed_at - started_at) * 1_000, 2)
    event: PerformanceEvent = {
        "event": "performance_span",
        "correlation_id": correlation_id,
        "component": component,
        "operation": operation,
        "duration_ms": duration_ms,
        "observed_at": _utc_timestamp(),
        "outcome": outcome,
        "error_type": error_type,
    }
    collector = _CURRENT_TRACE.get()
    if collector is not None and collector.correlation_id == correlation_id:
        collector.append(event)
    logger.info(json.dumps(event, separators=(",", ":"), sort_keys=True))


def _correlation_id(value: UUID | str) -> str:
    try:
        return str(UUID(str(value)))
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("correlation_id must be a UUID") from error


def _label(value: str, field: str) -> str:
    if not isinstance(value, str) or _LABEL_PATTERN.fullmatch(value) is None:
        raise ValueError(
            f"{field} must start with a lowercase letter and contain at most "
            "64 lowercase letters, digits, dots, underscores, or hyphens"
        )
    return value


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
