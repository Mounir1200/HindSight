from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from hindsight.web.app import create_app


@pytest.mark.parametrize("fails", [False, True])
def test_health_reports_probe_state_without_leaking_errors(fails: bool) -> None:
    def probe() -> None:
        if fails:
            raise RuntimeError("secret database detail")

    with TestClient(create_app(database_url="configured", health_probe=probe)) as client:
        response = client.get("/health")

    assert response.status_code == (503 if fails else 200)
    assert response.json() == (
        {"status": "unhealthy", "backend": "cockroachdb"}
        if fails
        else {"status": "ok", "backend": "cockroachdb", "database": "reachable"}
    )
    assert "secret" not in response.text
    assert response.headers["x-correlation-id"]
    assert response.headers["cache-control"] == "no-store"


def test_demo_seed_serializes_one_explicit_idempotent_run() -> None:
    calls = 0

    def run_demo() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {
            "backend": "in_memory",
            "decision": {
                "id": UUID("00000000-0000-0000-0000-000000000001"),
                "amount": Decimal("2.50"),
                "decided_at": datetime(2026, 7, 2, 12, 1, tzinfo=UTC),
            },
        }

    with TestClient(create_app(database_url="", demo_runner=run_demo)) as client:
        response = client.post("/demo/seed")

    assert response.status_code == 200
    assert calls == 1
    assert response.json()["decision"] == {
        "id": "00000000-0000-0000-0000-000000000001",
        "amount": "2.50",
        "decided_at": "2026-07-02T12:01:00+00:00",
    }


def test_dashboard_and_assets_are_served_from_the_same_origin() -> None:
    with TestClient(create_app(database_url="")) as client:
        page = client.get("/")
        css = client.get("/assets/styles.css")
        script = client.get("/assets/app.js")
        brand = client.get("/assets/brand-mark.svg")
        favicon = client.get("/assets/favicon.svg")

    assert page.status_code == css.status_code == script.status_code == 200
    assert brand.status_code == favicon.status_code == 200
    assert "HindSight" in page.text
    assert "/assets/styles.css" in page.text
    assert "/assets/app.js" in page.text
    assert "/assets/brand-mark.svg" in page.text


def test_dashboard_copy_is_english() -> None:
    with TestClient(create_app(database_url="")) as client:
        page = client.get("/")
        script = client.get("/assets/app.js")

    copy = page.text + script.text
    assert '<html lang="en">' in page.text
    assert "Run the audit" in page.text
    for french_fragment in (
        "fr-FR",
        "Lancer la démo",
        "Une décision",
        "Vérité métier",
        "Aucune mémoire",
        "Erreur HTTP",
    ):
        assert french_fragment not in copy
