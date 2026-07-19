import logging
import os
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from hindsight.application import execute_demo
from hindsight.infrastructure.database import connect_database

DemoRunner = Callable[[], dict[str, object]]
HealthProbe = Callable[[], None]
STATIC_DIRECTORY = Path(__file__).with_name("static")
logger = logging.getLogger("hindsight.web")


def create_app(
    *,
    database_url: str | None = None,
    demo_runner: DemoRunner | None = None,
    health_probe: HealthProbe | None = None,
) -> FastAPI:
    resolved_database_url = (
        database_url if database_url is not None else os.getenv("DATABASE_URL")
    )
    backend = "cockroachdb" if resolved_database_url else "in_memory"
    runner = demo_runner or (lambda: execute_demo(resolved_database_url))
    probe = health_probe or _health_probe(resolved_database_url)
    app = FastAPI(
        title="HindSight",
        description="Temporal Decision Accountability for AI Agents",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next: Callable[..., object]):
        correlation_id = str(uuid4())
        request.state.correlation_id = correlation_id
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("request failed", extra={"correlation_id": correlation_id})
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
        if request.url.path in {"/health", "/demo/seed"}:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/health", tags=["operations"])
    async def health() -> JSONResponse:
        try:
            await run_in_threadpool(probe)
        except Exception:
            logger.warning("database health check failed", exc_info=True)
            return JSONResponse(
                status_code=503,
                content={"status": "unhealthy", "backend": backend},
            )
        database = "reachable" if resolved_database_url else "not_configured"
        return JSONResponse(
            content={"status": "ok", "backend": backend, "database": database}
        )

    @app.post("/demo/seed", tags=["demo"])
    async def seed_demo() -> JSONResponse:
        payload = await run_in_threadpool(runner)
        return JSONResponse(content=_json_payload(payload))

    @app.get("/", include_in_schema=False)
    async def dashboard() -> FileResponse:
        return FileResponse(STATIC_DIRECTORY / "index.html")

    app.mount(
        "/assets",
        StaticFiles(directory=STATIC_DIRECTORY),
        name="assets",
    )
    return app


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
