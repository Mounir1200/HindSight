import json
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from threading import Event
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

import hindsight.web.app as web_app_module
from hindsight.adapters.telecom.seed import FOLLOW_UP_DEMO_CASE, PRIMARY_DEMO_CASE
from hindsight.web.app import create_app

DECISION_ID = UUID("10000000-0000-0000-0000-000000000001")
TRUTH_ID = UUID("10000000-0000-0000-0000-000000000002")
KNOWLEDGE_ID = UUID("10000000-0000-0000-0000-000000000003")


def _demo_payload(*, include_second_case: bool = False) -> dict[str, object]:
    payload: dict[str, object] = {
        "backend": "in_memory",
        "decision": {
            "id": PRIMARY_DEMO_CASE.decision_id,
            "subject_id": PRIMARY_DEMO_CASE.call_id,
            "amount": Decimal("2.50"),
            "decided_at": datetime(2026, 7, 2, 12, 1, tzinfo=UTC),
            "investigated_at": datetime(2026, 7, 3, 0, 1, tzinfo=UTC),
        },
        "verdict": {
            "category": "wrong_not_knowable",
            "agent_fault": False,
            "knowledge_gap_seconds": 172_800,
            "root_cause": "delayed_tariff_ingestion",
        },
        "comparison": {"overcharge": Decimal("1.00"), "currency": "EUR"},
        "remediation": {"case_id": PRIMARY_DEMO_CASE.dispute_id},
        "learning_proof": {},
    }
    if include_second_case:
        payload["learning_proof"] = {
            "second_case": {
                "call_id": FOLLOW_UP_DEMO_CASE.call_id,
                "dispute_id": FOLLOW_UP_DEMO_CASE.dispute_id,
                "decision_id": FOLLOW_UP_DEMO_CASE.decision_id,
                "audited_at": datetime(2026, 7, 3, 0, 3, tzinfo=UTC),
                "verdict": "wrong_not_knowable",
                "agent_fault": False,
                "knowledge_gap_seconds": 172_800,
                "root_cause": "delayed_tariff_ingestion",
                "overcharge": Decimal("0.50"),
                "currency": "EUR",
            }
        }
    return payload


def _decision_view(*, evidence_count: int = 1) -> dict[str, object]:
    assertion = {
        "id": TRUTH_ID,
        "assertion_key": "tariff:FR-SN:voice",
        "value": {"number": Decimal("0.15"), "currency": "EUR"},
        "recorded_at": datetime(2026, 7, 3, tzinfo=UTC),
    }
    return {
        "decision": {
            "id": DECISION_ID,
            "agent_id": "billing-agent",
            "action": "calculate_invoice",
            "subject_id": "call-001",
            "decided_at": datetime(2026, 7, 2, tzinfo=UTC),
            "output": {
                "amount": Decimal("2.50"),
                "api_token": "must-not-leak",
            },
            "rationale": "r" * 3_000,
        },
        "truth": assertion,
        "knowledge": {
            **assertion,
            "id": KNOWLEDGE_ID,
            "value": {"number": Decimal("0.25"), "currency": "EUR"},
        },
        "evidence": [
            {
                "evidence_type": f"trace-{index:03}",
                "assertion_id": KNOWLEDGE_ID,
                "available_to_agent": True,
                "retrieval_rank": index + 1,
            }
            for index in range(evidence_count)
        ],
        "verdict": {
            "category": "wrong_not_knowable",
            "agent_fault": False,
            "knowledge_gap_seconds": 172_800,
            "root_cause": "delayed_tariff_ingestion",
        },
    }


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


def test_request_log_is_structured_and_does_not_record_the_query(caplog) -> None:
    with (
        caplog.at_level(logging.INFO, logger="hindsight.web"),
        TestClient(create_app(database_url="")) as client,
    ):
        response = client.get("/health?token=must-not-be-logged")

    event = next(
        json.loads(record.message)
        for record in caplog.records
        if '"event":"request_complete"' in record.message
    )
    assert event["correlation_id"] == response.headers["x-correlation-id"]
    assert event["method"] == "GET"
    assert event["path"] == "/health"
    assert event["status_code"] == 200
    assert event["duration_ms"] >= 0
    assert "must-not-be-logged" not in json.dumps(event)


def test_decision_api_exposes_bounded_read_only_sections() -> None:
    calls: list[UUID] = []

    def reader(decision_id: UUID) -> dict[str, object]:
        calls.append(decision_id)
        return _decision_view(evidence_count=70)

    with TestClient(create_app(database_url="", decision_reader=reader)) as client:
        decision = client.get(f"/decisions/{DECISION_ID}")
        truth = client.get(f"/decisions/{DECISION_ID}/truth")
        knowledge = client.get(f"/decisions/{DECISION_ID}/knowledge")
        evidence = client.get(f"/decisions/{DECISION_ID}/evidence")
        verdict = client.get(f"/decisions/{DECISION_ID}/verdict")

    assert calls == [DECISION_ID] * 5
    assert decision.status_code == 200
    assert decision.json()["id"] == str(DECISION_ID)
    assert decision.json()["output"]["amount"] == "2.50"
    assert decision.json()["output"]["api_token"] == "[redacted]"
    assert len(decision.json()["rationale"]) == 2_048
    assert truth.json()["truth"]["id"] == str(TRUTH_ID)
    assert knowledge.json()["knowledge"]["id"] == str(KNOWLEDGE_ID)
    assert evidence.json()["decision_id"] == str(DECISION_ID)
    assert evidence.json()["count"] == 64
    assert len(evidence.json()["items"]) == 64
    assert verdict.json()["verdict"] == {
        "category": "wrong_not_knowable",
        "agent_fault": False,
        "knowledge_gap_seconds": 172800,
        "root_cause": "delayed_tariff_ingestion",
    }
    assert all(
        response.headers["cache-control"] == "no-store"
        for response in (decision, truth, knowledge, evidence, verdict)
    )


@pytest.mark.parametrize(
    "suffix",
    ("", "/truth", "/knowledge", "/evidence", "/verdict"),
)
def test_decision_api_returns_one_stable_not_found_contract(suffix: str) -> None:
    with TestClient(
        create_app(database_url="", decision_reader=lambda _decision_id: None)
    ) as client:
        response = client.get(f"/decisions/{DECISION_ID}{suffix}")

    assert response.status_code == 404
    assert response.json() == {"detail": "decision_not_found"}
    assert response.headers["x-correlation-id"]


def test_decision_api_hides_reader_failures(caplog) -> None:
    def failing_reader(_decision_id: UUID) -> dict[str, object]:
        raise RuntimeError("SELECT password FROM secrets WHERE token='private'")

    with (
        caplog.at_level(logging.ERROR, logger="hindsight.web"),
        TestClient(create_app(database_url="", decision_reader=failing_reader)) as client,
    ):
        response = client.get(f"/decisions/{DECISION_ID}")

    assert response.status_code == 500
    assert response.json()["detail"] == "internal_error"
    assert "SELECT" not in response.text
    assert "private" not in response.text
    assert "private" not in caplog.text
    assert "SELECT password" not in caplog.text


def test_cockroach_decision_reader_uses_parameterized_bounded_queries(
    monkeypatch,
) -> None:
    now = datetime(2026, 7, 3, tzinfo=UTC)
    decision_row = {
        "id": DECISION_ID,
        "domain": "telecom",
        "agent_id": "billing-agent",
        "action": "calculate_invoice",
        "subject_type": "telecom_call",
        "subject_id": "call-001",
        "event_time": now,
        "decided_at": now,
        "investigated_at": now,
        "selected_assertion_id": KNOWLEDGE_ID,
        "current_truth_assertion_id": TRUTH_ID,
        "known_assertion_id": KNOWLEDGE_ID,
        "output": {"amount": "2.50"},
        "rationale": "Used the known tariff.",
        "verdict": "wrong_not_knowable",
        "agent_fault": False,
        "knowledge_gap_seconds": 172_800,
        "root_cause": "delayed_tariff_ingestion",
    }

    def assertion_row(assertion_id: UUID, rate: str) -> dict[str, object]:
        return {
            "id": assertion_id,
            "assertion_key": "tariff:FR-SN:voice",
            "lineage_id": TRUTH_ID,
            "version_number": 2,
            "domain": "telecom",
            "subject_type": "route_service",
            "subject_id": "FR-SN:voice",
            "predicate": "rate_per_minute",
            "value_json": {"rate": rate},
            "value_number": Decimal(rate),
            "value_text": None,
            "unit": "minute",
            "currency": "EUR",
            "valid_from": now,
            "valid_until": None,
            "recorded_at": now,
            "superseded_at": None,
            "superseded_by": None,
            "written_by": "tariff-ingestion",
            "source_id": None,
            "confidence": 1.0,
            "source_kind": None,
            "source_trust_level": None,
        }

    evidence_row = {
        "evidence_type": "decision_input",
        "assertion_id": KNOWLEDGE_ID,
        "available_to_agent": True,
        "retrieval_started_at": now,
        "retrieved_at": now,
        "retrieval_method": "temporal_sql",
        "retrieval_rank": 1,
        "retrieval_score": None,
        "was_presented_to_model": False,
        "presentation_position": None,
        "was_cited_in_rationale": False,
        "was_used_for_decision": True,
        "exclusion_reason": None,
    }

    class Result:
        def __init__(self, rows: list[dict[str, object]]) -> None:
            self.rows = rows

        def fetchone(self):
            return self.rows[0] if self.rows else None

        def fetchall(self):
            return self.rows

    class Connection:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[object, ...]]] = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, sql: str, params: tuple[object, ...]) -> Result:
            self.calls.append((sql, params))
            if sql == web_app_module.DECISION_API_SQL:
                return Result([decision_row])
            if sql == web_app_module.DECISION_ASSERTIONS_SQL:
                return Result(
                    [
                        assertion_row(TRUTH_ID, "0.15"),
                        assertion_row(KNOWLEDGE_ID, "0.25"),
                    ]
                )
            return Result([evidence_row])

    connection = Connection()
    monkeypatch.setattr(
        web_app_module,
        "connect_database",
        lambda _database_url: connection,
    )

    with TestClient(create_app(database_url="postgresql://configured")) as client:
        response = client.get(f"/decisions/{DECISION_ID}/evidence")

    assert response.status_code == 200
    assert response.json()["items"][0]["retrieval_method"] == "temporal_sql"
    assert [params for _, params in connection.calls] == [
        (DECISION_ID,),
        (TRUTH_ID, KNOWLEDGE_ID),
        (DECISION_ID, 64),
    ]
    assert all(str(DECISION_ID) not in sql for sql, _ in connection.calls)


def test_demo_requires_one_explicit_prepared_incident(caplog) -> None:
    calls = 0
    agent_correlation_id = UUID("20000000-0000-0000-0000-000000000001")
    billing_run_id = UUID("20000000-0000-0000-0000-000000000002")

    def run_demo() -> dict[str, object]:
        nonlocal calls
        calls += 1
        payload = _demo_payload()
        payload["agent_execution"] = {
            "correlation_id": agent_correlation_id,
            "billing_run_id": billing_run_id,
            "remediation_run_ids": [],
        }
        return payload

    with (
        caplog.at_level(logging.INFO, logger="hindsight.web"),
        TestClient(create_app(database_url="", demo_runner=run_demo)) as client,
    ):
        rejected = client.post("/demo/seed")
        prepared = client.post("/demo/prepare")
        response = client.post("/demo/seed")
        replay = client.post("/demo/seed")

    assert rejected.status_code == replay.status_code == 409
    assert rejected.json() == replay.json() == {"detail": "no_reported_incident"}
    assert prepared.status_code == 201
    assert prepared.json()["can_run_demo"] is True
    assert prepared.json()["sample_already_audited"] is False
    assert len(prepared.json()["reported_incidents"]) == 1
    assert prepared.json()["reported_incidents"][0]["status"] == "reported"
    assert prepared.json()["reported_incidents"][0]["source"] == "synthetic_fixture"
    assert response.status_code == 200
    assert calls == 1
    assert response.json()["decision"]["id"] == str(PRIMARY_DEMO_CASE.decision_id)
    assert response.json()["decision"]["amount"] == "2.50"
    assert response.headers["cache-control"] == "no-store"
    agent_event = next(
        json.loads(record.message)
        for record in caplog.records
        if '"event":"demo_agent_runs_completed"' in record.message
    )
    assert agent_event == {
        "event": "demo_agent_runs_completed",
        "correlation_id": response.headers["x-correlation-id"],
        "agent_run_ids": [str(billing_run_id)],
        "agent_correlation_id": str(agent_correlation_id),
    }


def test_workspace_moves_both_audited_cases_to_history() -> None:
    payload = _demo_payload(include_second_case=True)

    with TestClient(create_app(database_url="", demo_runner=lambda: payload)) as client:
        initial = client.get("/demo/workspace")
        prepared = client.post("/demo/prepare")
        replay = client.post("/demo/seed")
        current = client.get("/demo/workspace")

    assert initial.status_code == replay.status_code == current.status_code == 200
    assert prepared.status_code == 201
    assert initial.headers["cache-control"] == "no-store"
    assert initial.json() == {
        "demo_state": "empty",
        "can_run_demo": False,
        "sample_already_audited": False,
        "reported_incidents": [],
        "past_audits": [],
    }
    workspace = replay.json()["workspace"]
    assert workspace == current.json()
    assert workspace["demo_state"] == "completed"
    assert workspace["can_run_demo"] is False
    assert workspace["sample_already_audited"] is True
    assert workspace["reported_incidents"] == []
    assert {audit["subject_id"] for audit in workspace["past_audits"]} == {
        PRIMARY_DEMO_CASE.call_id,
        FOLLOW_UP_DEMO_CASE.call_id,
    }
    primary = next(
        audit
        for audit in workspace["past_audits"]
        if audit["subject_id"] == PRIMARY_DEMO_CASE.call_id
    )
    memory_evaluation = next(
        audit
        for audit in workspace["past_audits"]
        if audit["subject_id"] == FOLLOW_UP_DEMO_CASE.call_id
    )
    assert memory_evaluation["audit_role"] == "memory_evaluation"
    assert primary == {
        "case_id": str(PRIMARY_DEMO_CASE.dispute_id),
        "decision_id": str(PRIMARY_DEMO_CASE.decision_id),
        "subject_id": PRIMARY_DEMO_CASE.call_id,
        "route": "FR->SN",
        "service_type": "voice",
        "audited_at": "2026-07-03T00:01:00+00:00",
        "verdict": "wrong_not_knowable",
        "agent_fault": False,
        "knowledge_gap_seconds": 172800,
        "root_cause": "delayed_tariff_ingestion",
        "customer_impact": "1.00",
        "currency": "EUR",
        "audit_role": "reported_incident",
        "status": "completed",
    }


def test_existing_sample_audit_is_prepared_as_an_explicit_replay() -> None:
    persisted_audit = {
        "case_id": PRIMARY_DEMO_CASE.dispute_id,
        "decision_id": PRIMARY_DEMO_CASE.decision_id,
        "subject_id": PRIMARY_DEMO_CASE.call_id,
        "status": "completed",
    }

    with TestClient(
        create_app(
            database_url="",
            workspace_reader=lambda: {
                "reported_incidents": [],
                "past_audits": [persisted_audit],
            },
        )
    ) as client:
        initial = client.get("/demo/workspace")
        prepared = client.post("/demo/prepare")

    assert initial.json()["sample_already_audited"] is True
    assert initial.json()["reported_incidents"] == []
    assert prepared.status_code == 201
    assert prepared.json()["sample_already_audited"] is True
    assert prepared.json()["can_run_demo"] is True
    incident = prepared.json()["reported_incidents"][0]
    assert incident["status"] == "replay"
    assert incident["source"] == "synthetic_replay"


def test_demo_claim_allows_only_one_concurrent_run() -> None:
    calls = 0
    started = Event()
    release = Event()

    def run_demo() -> dict[str, object]:
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(timeout=2)
        return _demo_payload()

    with TestClient(create_app(database_url="", demo_runner=run_demo)) as client:
        assert client.post("/demo/prepare").status_code == 201
        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(client.post, "/demo/seed")
            assert started.wait(timeout=2)
            concurrent = client.post("/demo/seed")
            release.set()
            completed = first.result(timeout=2)

    assert completed.status_code == 200
    assert concurrent.status_code == 409
    assert concurrent.json() == {"detail": "audit_in_progress"}
    assert calls == 1


def test_invalid_demo_result_does_not_consume_the_incident() -> None:
    attempts = 0

    def run_demo() -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        return {} if attempts == 1 else _demo_payload()

    with TestClient(create_app(database_url="", demo_runner=run_demo)) as client:
        client.post("/demo/prepare")
        invalid = client.post("/demo/seed")
        restored = client.get("/demo/workspace")
        retried = client.post("/demo/seed")

    assert invalid.status_code == 500
    assert invalid.json() == {"detail": "invalid_demo_result"}
    assert restored.json()["can_run_demo"] is True
    assert retried.status_code == 200
    assert attempts == 2


def test_demo_reset_is_disabled_without_an_explicit_token(monkeypatch) -> None:
    monkeypatch.delenv("HINDSIGHT_DEMO_RESET_TOKEN", raising=False)

    with TestClient(create_app(database_url="")) as client:
        response = client.post(
            "/demo/reset",
            headers={"X-Demo-Reset-Token": "unconfigured"},
        )

    assert response.status_code == 404


def test_in_memory_demo_reset_is_constant_time_and_idempotent(
    monkeypatch,
    caplog,
) -> None:
    token = "test-only-reset-token"
    comparisons: list[tuple[bytes, bytes]] = []
    original_compare = web_app_module.compare_digest

    def tracked_compare(expected: bytes, supplied: bytes) -> bool:
        comparisons.append((expected, supplied))
        return original_compare(expected, supplied)

    monkeypatch.setenv("HINDSIGHT_DEMO_RESET_TOKEN", token)
    monkeypatch.setattr(web_app_module, "compare_digest", tracked_compare)
    with (
        caplog.at_level(logging.INFO, logger="hindsight.web"),
        TestClient(create_app(database_url="", demo_runner=lambda: _demo_payload())) as client,
    ):
        missing = client.post("/demo/reset")
        denied = client.post(
            "/demo/reset",
            headers={"X-Demo-Reset-Token": "must-not-appear-in-logs"},
        )
        assert client.post("/demo/prepare").status_code == 201
        assert client.post("/demo/seed").status_code == 200
        first = client.post(
            "/demo/reset",
            headers={"X-Demo-Reset-Token": token},
        )
        second = client.post(
            "/demo/reset",
            headers={"X-Demo-Reset-Token": token},
        )
        workspace = client.get("/demo/workspace")

    assert missing.status_code == denied.status_code == 403
    assert missing.json() == denied.json() == {"detail": "demo_reset_forbidden"}
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {"status": "reset", "backend": "in_memory"}
    assert workspace.json() == {
        "demo_state": "empty",
        "can_run_demo": False,
        "sample_already_audited": False,
        "reported_incidents": [],
        "past_audits": [],
    }
    assert comparisons == [
        (token.encode(), b""),
        (token.encode(), b"must-not-appear-in-logs"),
        (token.encode(), token.encode()),
        (token.encode(), token.encode()),
    ]
    assert "must-not-appear-in-logs" not in caplog.text


def test_demo_reset_cannot_race_an_active_audit(monkeypatch) -> None:
    token = "test-only-reset-token"
    started = Event()
    release = Event()
    reset_calls = 0

    def run_demo() -> dict[str, object]:
        started.set()
        assert release.wait(timeout=2)
        return _demo_payload()

    def reset_demo() -> None:
        nonlocal reset_calls
        reset_calls += 1

    monkeypatch.setenv("HINDSIGHT_DEMO_RESET_TOKEN", token)
    with TestClient(
        create_app(database_url="", demo_runner=run_demo, demo_resetter=reset_demo)
    ) as client:
        assert client.post("/demo/prepare").status_code == 201
        with ThreadPoolExecutor(max_workers=1) as executor:
            audit = executor.submit(client.post, "/demo/seed")
            assert started.wait(timeout=2)
            concurrent = client.post(
                "/demo/reset",
                headers={"X-Demo-Reset-Token": token},
            )
            release.set()
            completed = audit.result(timeout=2)

    assert completed.status_code == 200
    assert concurrent.status_code == 409
    assert concurrent.json() == {"detail": "audit_in_progress"}
    assert reset_calls == 0


def test_cockroach_demo_reset_uses_one_bounded_parameterized_transaction(
    monkeypatch,
) -> None:
    token = "test-only-reset-token"

    class Transaction:
        def __init__(self, connection) -> None:
            self.connection = connection

        def __enter__(self):
            self.connection.transaction_entries += 1
            return self

        def __exit__(self, *_args):
            self.connection.transaction_exits += 1
            return None

    class Connection:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[object, ...]]] = []
            self.transaction_entries = 0
            self.transaction_exits = 0

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def transaction(self) -> Transaction:
            return Transaction(self)

        def execute(self, sql: str, params: tuple[object, ...]) -> None:
            self.calls.append((sql, params))

    connection = Connection()
    monkeypatch.setenv("HINDSIGHT_DEMO_RESET_TOKEN", token)
    monkeypatch.setattr(
        web_app_module,
        "connect_database",
        lambda _database_url: connection,
    )

    with TestClient(
        create_app(
            database_url="postgresql://configured",
            demo_runner=lambda: _demo_payload(),
        )
    ) as client:
        response = client.post(
            "/demo/reset",
            headers={"X-Demo-Reset-Token": token},
        )

    expected = list(web_app_module._demo_reset_operations())
    assert response.status_code == 200
    assert response.json() == {"status": "reset", "backend": "cockroachdb"}
    assert connection.calls == expected
    assert connection.transaction_entries == connection.transaction_exits == 1
    assert all("WHERE" in sql for sql, _ in connection.calls)
    fixture_ids = {
        str(PRIMARY_DEMO_CASE.decision_id),
        str(FOLLOW_UP_DEMO_CASE.decision_id),
        str(PRIMARY_DEMO_CASE.dispute_id),
        str(FOLLOW_UP_DEMO_CASE.dispute_id),
    }
    assert all(fixture_id not in sql for sql, _ in connection.calls for fixture_id in fixture_ids)


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
    assert "Load sample incident" in page.text
    assert "Replay sample scenario" in script.text
    assert "Replay the audit" in script.text
    assert "Reported incidents" in page.text
    assert "Audit history" in page.text
    for french_fragment in (
        "fr-FR",
        "Lancer la démo",
        "Une décision",
        "Vérité métier",
        "Aucune mémoire",
        "Erreur HTTP",
    ):
        assert french_fragment not in copy
