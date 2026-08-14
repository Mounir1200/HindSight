from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from fastapi.responses import JSONResponse

from hindsight.core.rate_limits import RateLimitLease
from hindsight.web.rate_limit import RateLimiter, RateLimitUnavailableError

logger = logging.getLogger("hindsight.web")

CONCURRENCY_POLICY_ID = "provider-concurrency"
_NO_STORE = {"Cache-Control": "no-store"}


@dataclass(frozen=True, slots=True)
class ProviderAdmission:
    operation: str
    granted: bool
    lease: RateLimitLease | None = None
    unavailable: bool = False
    policy_id: str = ""
    headers: dict[str, str] = field(default_factory=dict)


def admit_provider_operation(
    limiter: RateLimiter,
    operation: str,
    principal: str,
) -> ProviderAdmission:
    """Reserve provider capacity before spending the request budget.

    Token-bucket consumption has no rollback operation. Taking the concurrency lease
    first therefore guarantees that a request rejected for lack of capacity does not
    permanently consume provider budget. A lease held by an over-budget caller exists
    only for the duration of the budget check and is released before returning.
    """
    try:
        reservation = limiter.acquire_operation_lease(operation)
    except RateLimitUnavailableError:
        return ProviderAdmission(
            operation,
            granted=False,
            unavailable=True,
            policy_id=CONCURRENCY_POLICY_ID,
        )
    if reservation is not None and not reservation.acquired:
        return ProviderAdmission(
            operation,
            granted=False,
            policy_id=reservation.policy_id,
            headers=reservation.headers,
        )

    lease = reservation.lease if reservation is not None else None
    try:
        budget = limiter.check_operation(operation, principal)
    except RateLimitUnavailableError:
        release_provider_lease(limiter, lease, "")
        return ProviderAdmission(
            operation,
            granted=False,
            unavailable=True,
            policy_id=operation,
        )
    if budget is not None and not budget.allowed:
        release_provider_lease(limiter, lease, "")
        return ProviderAdmission(
            operation,
            granted=False,
            policy_id=budget.policy_id,
            headers=budget.headers,
        )
    return ProviderAdmission(
        operation,
        granted=True,
        lease=lease,
    )


def provider_admission_response(
    admission: ProviderAdmission,
    correlation_id: str,
) -> JSONResponse:
    if admission.granted:
        raise ValueError("a granted admission has no denial response")
    if admission.unavailable:
        _log_admission("rate_limit_unavailable", logging.ERROR, admission, correlation_id)
        return JSONResponse(
            status_code=503,
            content={
                "detail": "rate_limit_unavailable",
                "correlation_id": correlation_id,
            },
            headers=dict(_NO_STORE),
        )
    _log_admission("rate_limit_rejected", logging.WARNING, admission, correlation_id)
    return JSONResponse(
        status_code=429,
        content={"detail": "rate_limit_exceeded"},
        headers={**admission.headers, **_NO_STORE},
    )


def release_provider_lease(
    limiter: RateLimiter,
    lease: RateLimitLease | None,
    correlation_id: str,
) -> None:
    """Return a concurrency slot; a failed release must not also fail the request."""
    if lease is None:
        return
    try:
        limiter.release_operation_lease(lease)
    except RateLimitUnavailableError:
        logger.error(
            json.dumps(
                {
                    "event": "rate_limit_lease_release_failed",
                    "correlation_id": correlation_id,
                    "operation": CONCURRENCY_POLICY_ID,
                },
                separators=(",", ":"),
            )
        )


def _log_admission(
    event: str,
    level: int,
    admission: ProviderAdmission,
    correlation_id: str,
) -> None:
    logger.log(
        level,
        json.dumps(
            {
                "event": event,
                "correlation_id": correlation_id,
                "operation": admission.operation,
                "policy": admission.policy_id,
            },
            separators=(",", ":"),
        ),
    )
