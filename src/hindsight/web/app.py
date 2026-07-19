import json
import logging
import os
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from enum import Enum
from hmac import compare_digest
from pathlib import Path
from threading import Lock
from time import perf_counter
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from hindsight.adapters.telecom.seed import (
    DEMO_DISPUTE_CLAIM,
    DEMO_INCIDENT_ID,
    DEMO_MEMORY_ID,
    DEMO_REFUND_ID,
    DEMO_REMEDIATION_RUN_ID,
    DEMO_ROUTE,
    DEMO_SERVICE_TYPE,
    FOLLOW_UP_DEMO_CASE,
    PRIMARY_DEMO_CASE,
)
from hindsight.infrastructure.database import connect_database
from hindsight.web.memory_search import (
    build_memory_search_reader,
    create_memory_search_router,
)
from hindsight.web.runtime import DemoRuntimeConfig

DemoRunner = Callable[[], dict[str, object]]
DemoResetter = Callable[[], None]
HealthProbe = Callable[[], None]
WorkspaceReader = Callable[[], dict[str, object]]
DecisionReader = Callable[[UUID], dict[str, object] | None]
STATIC_DIRECTORY = Path(__file__).with_name("static")
logger = logging.getLogger("hindsight.web")
MAX_EVIDENCE_ITEMS = 64
MAX_TEXT_LENGTH = 2_048
MAX_JSON_ITEMS = 64
MAX_JSON_DEPTH = 4
SENSITIVE_FIELD_MARKERS = (
    "api_key",
    "authorization",
    "database_url",
    "password",
    "secret",
    "token",
)

AUDITS_SQL = """
SELECT
  dispute.id AS case_id,
  decision.id AS decision_id,
  decision.subject_id,
  cdr.route,
  cdr.service_type,
  decision.investigated_at AS audited_at,
  decision.verdict,
  decision.agent_fault,
  decision.knowledge_gap_seconds,
  decision.root_cause,
  refund.amount AS customer_impact,
  invoice.currency
FROM decisions AS decision
JOIN telecom_invoices AS invoice ON invoice.decision_id = decision.id
JOIN telecom_disputes AS dispute ON dispute.invoice_id = invoice.id
JOIN telecom_cdrs AS cdr ON cdr.id = invoice.cdr_id
LEFT JOIN telecom_refunds AS refund ON refund.dispute_id = dispute.id
WHERE decision.domain = 'telecom'
  AND decision.subject_type = 'telecom_call'
ORDER BY decision.investigated_at DESC, decision.id DESC
LIMIT 8
"""

DECISION_API_SQL = """
SELECT
  id, domain, agent_id, action, subject_type, subject_id, event_time,
  decided_at, investigated_at, selected_assertion_id,
  current_truth_assertion_id, known_assertion_id, output, rationale,
  verdict, agent_fault, knowledge_gap_seconds, root_cause
FROM decisions
WHERE id = %s
LIMIT 1
"""

DECISION_ASSERTIONS_SQL = """
SELECT
  assertion.id, assertion.assertion_key, assertion.lineage_id,
  assertion.version_number, assertion.domain, assertion.subject_type,
  assertion.subject_id, assertion.predicate, assertion.value_json,
  assertion.value_number, assertion.value_text, assertion.unit,
  assertion.currency, assertion.valid_from, assertion.valid_until,
  assertion.recorded_at, assertion.superseded_at, assertion.superseded_by,
  assertion.written_by, assertion.source_id, assertion.confidence,
  source.kind AS source_kind, source.trust_level AS source_trust_level
FROM assertions AS assertion
LEFT JOIN sources AS source ON source.id = assertion.source_id
WHERE assertion.id IN (%s, %s)
ORDER BY assertion.id
LIMIT 2
"""

DECISION_EVIDENCE_SQL = """
SELECT
  evidence_type, assertion_id, available_to_agent,
  retrieval_started_at, retrieved_at, retrieval_method,
  retrieval_rank, retrieval_score, was_presented_to_model,
  presentation_position, was_cited_in_rationale, was_used_for_decision,
  exclusion_reason
FROM decision_evidence
WHERE decision_id = %s
ORDER BY retrieval_rank NULLS LAST, evidence_type, assertion_id
LIMIT %s
"""

DELETE_DEMO_TOOL_CALLS_SQL = """
DELETE FROM tool_calls
WHERE run_id IN (
  SELECT id
  FROM agent_runs
  WHERE domain = 'telecom'
    AND subject_id IN (%s, %s, %s, %s)
)
"""

DELETE_DEMO_AGENT_RUNS_SQL = """
DELETE FROM agent_runs
WHERE domain = 'telecom'
  AND subject_id IN (%s, %s, %s, %s)
"""

DELETE_DEMO_CONTEXT_SNAPSHOTS_SQL = """
DELETE FROM investigation_context_snapshots
WHERE case_id IN (%s, %s)
"""

DELETE_DEMO_CONTEXTS_SQL = """
DELETE FROM investigation_contexts
WHERE case_id IN (%s, %s)
"""

DELETE_DEMO_EMBEDDINGS_SQL = """
DELETE FROM memory_embeddings
WHERE memory_id = %s
"""

DELETE_DEMO_REFUNDS_SQL = """
DELETE FROM telecom_refunds
WHERE id = %s
"""

DELETE_DEMO_INCIDENTS_SQL = """
DELETE FROM telecom_incidents
WHERE id = %s
"""

DELETE_DEMO_MEMORIES_SQL = """
DELETE FROM memories
WHERE id = %s
"""

DELETE_DEMO_REMEDIATION_RUNS_SQL = """
DELETE FROM remediation_runs
WHERE id = %s
"""

DELETE_DEMO_DISPUTES_SQL = """
DELETE FROM telecom_disputes
WHERE id IN (%s, %s)
"""

DELETE_DEMO_INVOICES_SQL = """
DELETE FROM telecom_invoices
WHERE id IN (%s, %s)
"""

DELETE_DEMO_CDRS_SQL = """
DELETE FROM telecom_cdrs
WHERE id IN (%s, %s)
"""

DELETE_DEMO_EVIDENCE_SQL = """
DELETE FROM decision_evidence
WHERE decision_id IN (%s, %s)
"""

DELETE_DEMO_DECISIONS_SQL = """
DELETE FROM decisions
WHERE id IN (%s, %s)
"""


def create_app(
    *,
    database_url: str | None = None,
    demo_runner: DemoRunner | None = None,
    demo_resetter: DemoResetter | None = None,
    health_probe: HealthProbe | None = None,
    workspace_reader: WorkspaceReader | None = None,
    decision_reader: DecisionReader | None = None,
) -> FastAPI:
    logger.setLevel(_log_level())
    resolved_database_url = database_url if database_url is not None else os.getenv("DATABASE_URL")
    backend = "cockroachdb" if resolved_database_url else "in_memory"
    runner = demo_runner or DemoRuntimeConfig.from_environment(resolved_database_url).runner()
    resetter = demo_resetter or _demo_resetter(resolved_database_url)
    reset_token = _configured_reset_token()
    probe = health_probe or _health_probe(resolved_database_url)
    workspace_state = _empty_workspace()
    demo_state = "empty"
    state_lock = Lock()

    def read_workspace() -> dict[str, object]:
        persisted = _empty_workspace()
        if workspace_reader is not None:
            persisted = workspace_reader()
        elif resolved_database_url:
            persisted = _database_workspace(resolved_database_url)
        with state_lock:
            local = workspace_state
            state = demo_state
        return _workspace_view(local, persisted, state)

    def read_decision(decision_id: UUID) -> dict[str, object] | None:
        if decision_reader is not None:
            return decision_reader(decision_id)
        if resolved_database_url:
            return _database_decision(resolved_database_url, decision_id)
        return None

    app = FastAPI(
        title="HindSight",
        description="Temporal Decision Accountability for AI Agents",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
    )
    app.include_router(
        create_memory_search_router(build_memory_search_reader(resolved_database_url))
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next: Callable[..., object]):
        correlation_id = str(uuid4())
        started_at = perf_counter()
        request.state.correlation_id = correlation_id
        try:
            response = await call_next(request)
        except Exception as error:
            logger.error(
                json.dumps(
                    {
                        "event": "request_failed",
                        "correlation_id": correlation_id,
                        "error_type": type(error).__name__,
                    },
                    separators=(",", ":"),
                )
            )
            response = JSONResponse(
                status_code=500,
                content={"detail": "internal_error", "correlation_id": correlation_id},
            )
        response.headers["X-Correlation-ID"] = correlation_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
            "style-src 'self'; script-src 'self'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'"
        )
        if request.url.path.startswith("/decisions/") or request.url.path in {
            "/health",
            "/demo/prepare",
            "/demo/reset",
            "/demo/seed",
            "/demo/workspace",
        }:
            response.headers["Cache-Control"] = "no-store"
        logger.info(
            json.dumps(
                {
                    "event": "request_complete",
                    "correlation_id": correlation_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": round((perf_counter() - started_at) * 1000, 2),
                },
                separators=(",", ":"),
            )
        )
        return response

    @app.get("/health", tags=["operations"])
    async def health() -> JSONResponse:
        try:
            await run_in_threadpool(probe)
        except Exception as error:
            logger.warning(
                json.dumps(
                    {
                        "event": "health_check_failed",
                        "error_type": type(error).__name__,
                    },
                    separators=(",", ":"),
                )
            )
            return JSONResponse(
                status_code=503,
                content={"status": "unhealthy", "backend": backend},
            )
        database = "reachable" if resolved_database_url else "not_configured"
        return JSONResponse(content={"status": "ok", "backend": backend, "database": database})

    if reset_token is not None:

        @app.post("/demo/reset", tags=["demo"])
        async def reset_demo(request: Request) -> JSONResponse:
            nonlocal demo_state, workspace_state
            supplied_token = request.headers.get("x-demo-reset-token", "")
            if not _valid_reset_token(reset_token, supplied_token):
                logger.warning(
                    json.dumps(
                        {
                            "event": "demo_reset_denied",
                            "correlation_id": request.state.correlation_id,
                        },
                        separators=(",", ":"),
                    )
                )
                return JSONResponse(
                    status_code=403,
                    content={"detail": "demo_reset_forbidden"},
                )

            with state_lock:
                if demo_state == "running":
                    return JSONResponse(
                        status_code=409,
                        content={"detail": "audit_in_progress"},
                    )
                previous_state = demo_state
                previous_workspace = workspace_state
                demo_state = "running"
            try:
                await run_in_threadpool(resetter)
            except Exception:
                with state_lock:
                    demo_state = previous_state
                    workspace_state = previous_workspace
                raise
            with state_lock:
                workspace_state = _empty_workspace()
                demo_state = "empty"
            logger.info(
                json.dumps(
                    {
                        "event": "demo_reset_completed",
                        "correlation_id": request.state.correlation_id,
                        "backend": backend,
                    },
                    separators=(",", ":"),
                )
            )
            return JSONResponse(content={"status": "reset", "backend": backend})

    @app.post("/demo/seed", tags=["demo"])
    async def seed_demo(request: Request) -> JSONResponse:
        nonlocal demo_state, workspace_state
        with state_lock:
            if demo_state == "running":
                return JSONResponse(
                    status_code=409,
                    content={"detail": "audit_in_progress"},
                )
            if demo_state != "prepared":
                return JSONResponse(
                    status_code=409,
                    content={"detail": "no_reported_incident"},
                )
            incident = workspace_state["reported_incidents"][0]
            demo_state = "running"
        try:
            payload = await run_in_threadpool(runner)
        except Exception as error:
            with state_lock:
                demo_state = "prepared"
            _log_demo_agent_failure(request.state.correlation_id, error)
            raise
        if not _valid_demo_result(payload, incident):
            with state_lock:
                demo_state = "prepared"
            return JSONResponse(
                status_code=500,
                content={"detail": "invalid_demo_result"},
            )
        with state_lock:
            try:
                completed_workspace = _workspace_from_demo(payload, workspace_state)
            except Exception as error:
                demo_state = "prepared"
                _log_demo_agent_failure(request.state.correlation_id, error)
                raise
            workspace_state = completed_workspace
            demo_state = "completed"
        logger.info(
            json.dumps(
                {
                    "event": "demo_agent_runs_completed",
                    "correlation_id": request.state.correlation_id,
                    **_agent_execution_log(payload),
                },
                separators=(",", ":"),
            )
        )
        payload["workspace"] = await run_in_threadpool(read_workspace)
        return JSONResponse(content=_json_payload(payload))

    @app.post("/demo/prepare", tags=["demo"])
    async def prepare_demo() -> JSONResponse:
        nonlocal demo_state, workspace_state
        current = await run_in_threadpool(read_workspace)
        replay = bool(current["sample_already_audited"])
        history_snapshot = {"past_audits": current["past_audits"]}
        with state_lock:
            if demo_state == "running":
                return JSONResponse(
                    status_code=409,
                    content={"detail": "audit_in_progress"},
                )
            created = demo_state != "prepared"
            workspace_state = _prepared_demo_workspace(
                workspace_state,
                replay=replay,
            )
            demo_state = "prepared"
            workspace = _workspace_view(workspace_state, history_snapshot, demo_state)
        return JSONResponse(
            status_code=201 if created else 200,
            content=_json_payload(workspace),
        )

    @app.get("/demo/workspace", tags=["demo"])
    async def demo_workspace() -> JSONResponse:
        workspace = await run_in_threadpool(read_workspace)
        return JSONResponse(content=_json_payload(workspace))

    async def decision_section(
        decision_id: UUID,
        section: str,
    ) -> JSONResponse:
        view = await run_in_threadpool(read_decision, decision_id)
        if view is None:
            return JSONResponse(
                status_code=404,
                content={"detail": "decision_not_found"},
            )
        payload = _decision_section(view, section)
        if payload is None:
            logger.error(
                "invalid decision reader result",
                extra={"decision_id": str(decision_id), "section": section},
            )
            return JSONResponse(
                status_code=500,
                content={"detail": "invalid_decision_view"},
            )
        return JSONResponse(content=_json_payload(_bounded_mapping(payload)))

    @app.get("/decisions/{decision_id}", tags=["decisions"])
    async def get_decision(decision_id: UUID) -> JSONResponse:
        return await decision_section(decision_id, "decision")

    @app.get("/decisions/{decision_id}/truth", tags=["decisions"])
    async def get_decision_truth(decision_id: UUID) -> JSONResponse:
        return await decision_section(decision_id, "truth")

    @app.get("/decisions/{decision_id}/knowledge", tags=["decisions"])
    async def get_decision_knowledge(decision_id: UUID) -> JSONResponse:
        return await decision_section(decision_id, "knowledge")

    @app.get("/decisions/{decision_id}/evidence", tags=["decisions"])
    async def get_decision_evidence(decision_id: UUID) -> JSONResponse:
        return await decision_section(decision_id, "evidence")

    @app.get("/decisions/{decision_id}/verdict", tags=["decisions"])
    async def get_decision_verdict(decision_id: UUID) -> JSONResponse:
        return await decision_section(decision_id, "verdict")

    @app.get("/", include_in_schema=False)
    async def dashboard() -> FileResponse:
        return FileResponse(STATIC_DIRECTORY / "index.html")

    app.mount(
        "/assets",
        StaticFiles(directory=STATIC_DIRECTORY),
        name="assets",
    )
    return app


def _log_level() -> int:
    value = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    return value if isinstance(value, int) else logging.INFO


def _configured_reset_token() -> str | None:
    token = os.getenv("HINDSIGHT_DEMO_RESET_TOKEN")
    return token if token and token.strip() else None


def _valid_reset_token(expected: str, supplied: str) -> bool:
    return compare_digest(expected.encode("utf-8"), supplied.encode("utf-8"))


def _demo_resetter(database_url: str | None) -> DemoResetter:
    if not database_url:
        return lambda: None
    return lambda: _database_demo_reset(database_url)


def _database_demo_reset(database_url: str) -> None:
    operations = _demo_reset_operations()
    for attempt in range(4):
        try:
            with (
                connect_database(database_url) as connection,
                connection.transaction(),
            ):
                for sql, params in operations:
                    connection.execute(sql, params)
            return
        except Exception as error:
            if getattr(error, "sqlstate", None) not in {"40001", "40003"} or attempt == 3:
                raise


def _demo_reset_operations() -> tuple[tuple[str, tuple[object, ...]], ...]:
    case_ids = (PRIMARY_DEMO_CASE.dispute_id, FOLLOW_UP_DEMO_CASE.dispute_id)
    decision_ids = (PRIMARY_DEMO_CASE.decision_id, FOLLOW_UP_DEMO_CASE.decision_id)
    agent_subject_ids = (
        PRIMARY_DEMO_CASE.call_id,
        FOLLOW_UP_DEMO_CASE.call_id,
        str(PRIMARY_DEMO_CASE.dispute_id),
        str(FOLLOW_UP_DEMO_CASE.dispute_id),
    )
    return (
        (DELETE_DEMO_TOOL_CALLS_SQL, agent_subject_ids),
        (DELETE_DEMO_AGENT_RUNS_SQL, agent_subject_ids),
        (DELETE_DEMO_CONTEXT_SNAPSHOTS_SQL, case_ids),
        (DELETE_DEMO_CONTEXTS_SQL, case_ids),
        (DELETE_DEMO_EMBEDDINGS_SQL, (DEMO_MEMORY_ID,)),
        (DELETE_DEMO_REFUNDS_SQL, (DEMO_REFUND_ID,)),
        (DELETE_DEMO_INCIDENTS_SQL, (DEMO_INCIDENT_ID,)),
        (DELETE_DEMO_MEMORIES_SQL, (DEMO_MEMORY_ID,)),
        (DELETE_DEMO_REMEDIATION_RUNS_SQL, (DEMO_REMEDIATION_RUN_ID,)),
        (DELETE_DEMO_DISPUTES_SQL, case_ids),
        (
            DELETE_DEMO_INVOICES_SQL,
            (PRIMARY_DEMO_CASE.invoice_id, FOLLOW_UP_DEMO_CASE.invoice_id),
        ),
        (
            DELETE_DEMO_CDRS_SQL,
            (PRIMARY_DEMO_CASE.cdr_id, FOLLOW_UP_DEMO_CASE.cdr_id),
        ),
        (DELETE_DEMO_EVIDENCE_SQL, decision_ids),
        (DELETE_DEMO_DECISIONS_SQL, decision_ids),
    )


def _health_probe(database_url: str | None) -> HealthProbe:
    if not database_url:
        return lambda: None

    def probe() -> None:
        with connect_database(database_url) as connection:
            row = connection.execute("SELECT 1 AS ok").fetchone()
            if row is None or row["ok"] != 1:
                raise RuntimeError("database health probe returned no result")

    return probe


def _json_payload(payload: dict[str, object]) -> dict[str, object]:
    return jsonable_encoder(
        payload,
        custom_encoder={
            datetime: datetime.isoformat,
            Decimal: lambda value: format(value, "f"),
            UUID: str,
            Enum: lambda value: value.value,
        },
    )


def _database_decision(
    database_url: str,
    decision_id: UUID,
) -> dict[str, object] | None:
    with connect_database(database_url) as connection:
        row = connection.execute(DECISION_API_SQL, (decision_id,)).fetchone()
        if row is None:
            return None
        current_truth_id = row["current_truth_assertion_id"]
        known_assertion_id = row["known_assertion_id"]
        assertion_rows = connection.execute(
            DECISION_ASSERTIONS_SQL,
            (current_truth_id, known_assertion_id),
        ).fetchall()
        evidence_rows = connection.execute(
            DECISION_EVIDENCE_SQL,
            (decision_id, MAX_EVIDENCE_ITEMS),
        ).fetchall()

    assertions = {str(item["id"]): _assertion_view(item) for item in assertion_rows}
    try:
        truth = assertions[str(current_truth_id)]
        knowledge = assertions[str(known_assertion_id)]
    except KeyError as error:
        raise RuntimeError("decision temporal snapshot is incomplete") from error

    return {
        "decision": {
            "id": row["id"],
            "domain": row["domain"],
            "agent_id": row["agent_id"],
            "action": row["action"],
            "subject_type": row["subject_type"],
            "subject_id": row["subject_id"],
            "event_time": row["event_time"],
            "decided_at": row["decided_at"],
            "investigated_at": row["investigated_at"],
            "selected_assertion_id": row["selected_assertion_id"],
            "output": _database_json_object(row["output"]),
            "rationale": row["rationale"],
        },
        "truth": truth,
        "knowledge": knowledge,
        "evidence": [_evidence_view(item) for item in evidence_rows],
        "verdict": {
            "category": row["verdict"],
            "agent_fault": row["agent_fault"],
            "knowledge_gap_seconds": row["knowledge_gap_seconds"],
            "root_cause": row["root_cause"],
            "current_truth_assertion_id": current_truth_id,
            "known_assertion_id": known_assertion_id,
            "selected_assertion_id": row["selected_assertion_id"],
        },
    }


def _assertion_view(row: object) -> dict[str, object]:
    item = dict(row)
    source = None
    if item["source_id"] is not None:
        source = {
            "id": item["source_id"],
            "kind": item["source_kind"],
            "trust_level": item["source_trust_level"],
        }
    return {
        "id": item["id"],
        "assertion_key": item["assertion_key"],
        "lineage_id": item["lineage_id"],
        "version_number": item["version_number"],
        "domain": item["domain"],
        "subject_type": item["subject_type"],
        "subject_id": item["subject_id"],
        "predicate": item["predicate"],
        "value": {
            "json": _database_json_object(item["value_json"]),
            "number": item["value_number"],
            "text": item["value_text"],
            "unit": item["unit"],
            "currency": item["currency"],
        },
        "valid_from": item["valid_from"],
        "valid_until": item["valid_until"],
        "recorded_at": item["recorded_at"],
        "superseded_at": item["superseded_at"],
        "superseded_by": item["superseded_by"],
        "written_by": item["written_by"],
        "confidence": item["confidence"],
        "source": source,
    }


def _evidence_view(row: object) -> dict[str, object]:
    item = dict(row)
    return {
        "evidence_type": item["evidence_type"],
        "assertion_id": item["assertion_id"],
        "available_to_agent": item["available_to_agent"],
        "retrieval_started_at": item["retrieval_started_at"],
        "retrieved_at": item["retrieved_at"],
        "retrieval_method": item["retrieval_method"],
        "retrieval_rank": item["retrieval_rank"],
        "retrieval_score": item["retrieval_score"],
        "was_presented_to_model": item["was_presented_to_model"],
        "presentation_position": item["presentation_position"],
        "was_cited_in_rationale": item["was_cited_in_rationale"],
        "was_used_for_decision": item["was_used_for_decision"],
        "exclusion_reason": item["exclusion_reason"],
    }


def _decision_section(
    view: dict[str, object],
    section: str,
) -> dict[str, object] | None:
    decision = _mapping(view.get("decision"))
    decision_id = decision.get("id")
    if not decision or decision_id is None:
        return None
    if section == "decision":
        return decision
    if section == "evidence":
        evidence = view.get("evidence")
        if not isinstance(evidence, list):
            return None
        items = evidence[:MAX_EVIDENCE_ITEMS]
        return {"decision_id": decision_id, "count": len(items), "items": items}
    value = _mapping(view.get(section))
    if section not in {"truth", "knowledge", "verdict"} or not value:
        return None
    return {"decision_id": decision_id, section: value}


def _database_json_object(value: object) -> dict[str, object]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("database JSON must be an object")
    return value


def _bounded_mapping(value: dict[str, object]) -> dict[str, object]:
    bounded = _bounded_value(value, MAX_JSON_DEPTH)
    if not isinstance(bounded, dict):
        raise TypeError("bounded API payload must be an object")
    return bounded


def _bounded_value(value: object, depth: int) -> object:
    if isinstance(value, str):
        return value[:MAX_TEXT_LENGTH]
    if isinstance(value, dict):
        if depth == 0:
            return {"truncated": True}
        result: dict[str, object] = {}
        for raw_key, item in list(value.items())[:MAX_JSON_ITEMS]:
            key = str(raw_key)
            if any(marker in key.lower() for marker in SENSITIVE_FIELD_MARKERS):
                result[key] = "[redacted]"
            else:
                result[key] = _bounded_value(item, depth - 1)
        return result
    if isinstance(value, list | tuple):
        if depth == 0:
            return []
        return [_bounded_value(item, depth - 1) for item in value[:MAX_JSON_ITEMS]]
    return value


def _prepared_demo_workspace(
    current: dict[str, object],
    *,
    replay: bool,
) -> dict[str, object]:
    return {
        "reported_incidents": [
            {
                "case_id": PRIMARY_DEMO_CASE.dispute_id,
                "decision_id": PRIMARY_DEMO_CASE.decision_id,
                "subject_id": PRIMARY_DEMO_CASE.call_id,
                "route": DEMO_ROUTE,
                "service_type": DEMO_SERVICE_TYPE,
                "opened_at": PRIMARY_DEMO_CASE.dispute_time,
                "claim": DEMO_DISPUTE_CLAIM,
                "status": "replay" if replay else "reported",
                "amount_at_issue": None,
                "currency": "EUR",
                "source": "synthetic_replay" if replay else "synthetic_fixture",
            }
        ],
        "past_audits": current.get("past_audits", []),
    }


def _empty_workspace() -> dict[str, object]:
    return {"reported_incidents": [], "past_audits": []}


def _database_workspace(database_url: str) -> dict[str, object]:
    with connect_database(database_url) as connection:
        audits = connection.execute(AUDITS_SQL).fetchall()
    return {
        "reported_incidents": [],
        "past_audits": [
            {
                **dict(row),
                "audit_role": (
                    "memory_evaluation"
                    if str(row["decision_id"]) == str(FOLLOW_UP_DEMO_CASE.decision_id)
                    else "reported_incident"
                ),
                "status": "completed",
            }
            for row in audits
        ],
    }


def _workspace_from_demo(
    payload: dict[str, object],
    fallback: dict[str, object],
) -> dict[str, object]:
    decision = _mapping(payload.get("decision"))
    verdict = _mapping(payload.get("verdict"))
    comparison = _mapping(payload.get("comparison"))
    remediation = _mapping(payload.get("remediation"))
    learning = _mapping(payload.get("learning_proof"))
    second_case = _mapping(learning.get("second_case"))
    if not decision or not verdict or not remediation:
        return fallback

    history = {
        "case_id": remediation.get("case_id"),
        "decision_id": decision.get("id"),
        "subject_id": decision.get("subject_id"),
        "route": DEMO_ROUTE,
        "service_type": DEMO_SERVICE_TYPE,
        "audited_at": decision.get("investigated_at") or decision.get("decided_at"),
        "verdict": verdict.get("category"),
        "agent_fault": verdict.get("agent_fault"),
        "knowledge_gap_seconds": verdict.get("knowledge_gap_seconds"),
        "root_cause": verdict.get("root_cause"),
        "customer_impact": comparison.get("overcharge"),
        "currency": comparison.get("currency") or decision.get("currency") or "EUR",
        "audit_role": "reported_incident",
        "status": "completed",
    }
    history_records = [history]
    if second_case:
        history_records.insert(
            0,
            {
                "case_id": second_case.get("dispute_id"),
                "decision_id": second_case.get("decision_id"),
                "subject_id": second_case.get("call_id"),
                "route": DEMO_ROUTE,
                "service_type": DEMO_SERVICE_TYPE,
                "audited_at": second_case.get("audited_at"),
                "verdict": second_case.get("verdict"),
                "agent_fault": second_case.get("agent_fault"),
                "knowledge_gap_seconds": second_case.get("knowledge_gap_seconds"),
                "root_cause": second_case.get("root_cause"),
                "customer_impact": second_case.get("overcharge"),
                "currency": second_case.get("currency") or "EUR",
                "audit_role": "memory_evaluation",
                "status": "completed",
            },
        )
    previous = fallback.get("past_audits", [])
    return {
        "reported_incidents": [],
        "past_audits": _merge_audits(history_records, previous),
    }


def _valid_demo_result(payload: object, incident: object) -> bool:
    if not isinstance(payload, dict) or not isinstance(incident, dict):
        return False
    decision = _mapping(payload.get("decision"))
    verdict = _mapping(payload.get("verdict"))
    comparison = _mapping(payload.get("comparison"))
    remediation = _mapping(payload.get("remediation"))
    return bool(
        decision
        and verdict
        and comparison
        and remediation
        and decision.get("subject_id") == incident.get("subject_id")
        and str(remediation.get("case_id")) == str(incident.get("case_id"))
    )


def _workspace_view(
    local: dict[str, object],
    persisted: dict[str, object],
    demo_state: str,
) -> dict[str, object]:
    local_incidents = local.get("reported_incidents", [])
    local_audits = local.get("past_audits", [])
    persisted_audits = persisted.get("past_audits", [])
    audits = _merge_audits(local_audits, persisted_audits)
    return {
        "demo_state": demo_state,
        "can_run_demo": demo_state == "prepared",
        "sample_already_audited": any(
            str(audit.get("decision_id")) == str(PRIMARY_DEMO_CASE.decision_id) for audit in audits
        ),
        "reported_incidents": list(local_incidents) if isinstance(local_incidents, list) else [],
        "past_audits": audits,
    }


def _merge_audits(*collections: object) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    seen: set[str] = set()
    for collection in collections:
        if not isinstance(collection, list):
            continue
        for value in collection:
            if not isinstance(value, dict):
                continue
            key = str(value.get("decision_id") or value.get("case_id"))
            if key in seen:
                continue
            seen.add(key)
            records.append(value)
            if len(records) == 8:
                return records
    return records


def _agent_execution_log(payload: dict[str, object]) -> dict[str, object]:
    execution = _mapping(payload.get("agent_execution"))
    run_ids: list[str] = []
    for key in ("billing_run_id", "follow_up_billing_run_id"):
        value = _uuid_text(execution.get(key))
        if value is not None:
            run_ids.append(value)
    remediation_ids = execution.get("remediation_run_ids")
    if isinstance(remediation_ids, list | tuple):
        for candidate in remediation_ids[:4]:
            value = _uuid_text(candidate)
            if value is not None:
                run_ids.append(value)
    investigation = _mapping(payload.get("bedrock_investigation"))
    investigation_id = _uuid_text(investigation.get("agent_run_id"))
    if investigation_id is not None:
        run_ids.append(investigation_id)
    result: dict[str, object] = {"agent_run_ids": run_ids}
    agent_correlation_id = _uuid_text(execution.get("correlation_id"))
    if agent_correlation_id is not None:
        result["agent_correlation_id"] = agent_correlation_id
    return result


def _log_demo_agent_failure(correlation_id: str, error: Exception) -> None:
    failure = {
        "event": "demo_agent_runs_failed",
        "correlation_id": correlation_id,
        "error_type": type(error).__name__,
    }
    run_id = _uuid_text(getattr(error, "run_id", None))
    if run_id is not None:
        failure["run_id"] = run_id
    logger.error(json.dumps(failure, separators=(",", ":")))


def _uuid_text(value: object) -> str | None:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return None


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}
