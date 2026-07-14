from datetime import UTC, datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from hindsight.adapters.telecom.models import CallEvent
from hindsight.core.assertions.models import AssertionDraft
from hindsight.core.assertions.service import TemporalAssertionService

DEMO_TARIFF_KEY = "FR-SN-VOICE"
DEMO_CALL_ID = "CALL-2026-07-02-001"
DEMO_DECISION_ID = uuid5(NAMESPACE_URL, "hindsight:demo:retroactive-telecom-rate")
DEMO_EVENT_TIME = datetime(2026, 7, 2, 12, tzinfo=UTC)
DEMO_DECISION_TIME = datetime(2026, 7, 2, 12, 1, tzinfo=UTC)
DEMO_INVESTIGATION_TIME = datetime(2026, 7, 3, 0, 1, tzinfo=UTC)


def demo_tariff_versions() -> tuple[AssertionDraft, AssertionDraft]:
    common = {
        "assertion_key": DEMO_TARIFF_KEY,
        "domain": "telecom",
        "subject_type": "roaming_route",
        "subject_id": DEMO_TARIFF_KEY,
        "predicate": "rate_per_minute",
        "unit": "minute",
        "currency": "EUR",
        "written_by": "demo_seed",
    }
    old_rate = AssertionDraft(
        **common,
        value_json={"rate": "0.25", "route": "FR->SN", "service_type": "voice"},
        value_number=Decimal("0.25"),
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        recorded_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    retroactive_rate = AssertionDraft(
        **common,
        value_json={"rate": "0.15", "route": "FR->SN", "service_type": "voice"},
        value_number=Decimal("0.15"),
        valid_from=datetime(2026, 7, 1, tzinfo=UTC),
        recorded_at=datetime(2026, 7, 3, tzinfo=UTC),
    )
    return old_rate, retroactive_rate


def seed_demo(assertions: TemporalAssertionService) -> None:
    assertions.ingest_versions(demo_tariff_versions())


def demo_call() -> CallEvent:
    return CallEvent(
        id=DEMO_CALL_ID,
        tariff_key=DEMO_TARIFF_KEY,
        started_at=DEMO_EVENT_TIME,
        duration_seconds=600,
    )
