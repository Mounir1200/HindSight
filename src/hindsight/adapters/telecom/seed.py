from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from hindsight.adapters.telecom.models import CallEvent
from hindsight.adapters.telecom.remediation import (
    TelecomCaseSeed,
    TelecomRemediationPlan,
    build_remediation_plan,
)
from hindsight.core.assertions.models import AssertionDraft
from hindsight.core.assertions.service import TemporalAssertionService
from hindsight.core.decisions.models import DecisionAudit, DecisionJournalEntry


@dataclass(frozen=True, slots=True)
class DemoCase:
    call_id: str
    decision_id: UUID
    cdr_id: UUID
    invoice_id: UUID
    dispute_id: UUID
    event_time: datetime
    decision_time: datetime
    investigation_time: datetime
    dispute_time: datetime
    duration_seconds: int


def _demo_case(
    slug: str,
    call_id: str,
    event_time: datetime,
    decision_time: datetime,
    investigation_time: datetime,
    dispute_time: datetime,
    duration_seconds: int,
) -> DemoCase:
    return DemoCase(
        call_id=call_id,
        decision_id=uuid5(NAMESPACE_URL, f"hindsight:demo:{slug}"),
        cdr_id=uuid5(NAMESPACE_URL, f"hindsight:demo:cdr:{slug}"),
        invoice_id=uuid5(NAMESPACE_URL, f"hindsight:demo:invoice:{slug}"),
        dispute_id=uuid5(NAMESPACE_URL, f"hindsight:demo:dispute:{slug}"),
        event_time=event_time,
        decision_time=decision_time,
        investigation_time=investigation_time,
        dispute_time=dispute_time,
        duration_seconds=duration_seconds,
    )


DEMO_TARIFF_KEY = "FR-SN-VOICE"
DEMO_ROUTE = "FR->SN"
DEMO_SERVICE_TYPE = "voice"
DEMO_DISPUTE_CLAIM = (
    "The call was billed using a tariff superseded by a retroactive correction."
)
PRIMARY_DEMO_CASE = _demo_case(
    "retroactive-telecom-rate",
    "CALL-2026-07-02-001",
    datetime(2026, 7, 2, 12, tzinfo=UTC),
    datetime(2026, 7, 2, 12, 1, tzinfo=UTC),
    datetime(2026, 7, 3, 0, 1, tzinfo=UTC),
    datetime(2026, 7, 3, 0, 0, 30, tzinfo=UTC),
    600,
)
FOLLOW_UP_DEMO_CASE = _demo_case(
    "retroactive-telecom-rate-follow-up",
    "CALL-2026-07-02-002",
    datetime(2026, 7, 2, 16, tzinfo=UTC),
    datetime(2026, 7, 2, 16, 1, tzinfo=UTC),
    datetime(2026, 7, 3, 0, 3, tzinfo=UTC),
    datetime(2026, 7, 3, 0, 2, 30, tzinfo=UTC),
    300,
)
DEMO_CALL_ID = PRIMARY_DEMO_CASE.call_id
DEMO_DECISION_ID = PRIMARY_DEMO_CASE.decision_id
DEMO_CDR_ID = PRIMARY_DEMO_CASE.cdr_id
DEMO_INVOICE_ID = PRIMARY_DEMO_CASE.invoice_id
DEMO_DISPUTE_ID = PRIMARY_DEMO_CASE.dispute_id
DEMO_REMEDIATION_RUN_ID = uuid5(
    NAMESPACE_URL,
    "hindsight:demo:remediation:retroactive-telecom-rate",
)
DEMO_REFUND_ID = uuid5(NAMESPACE_URL, "hindsight:demo:refund:retroactive-telecom-rate")
DEMO_INCIDENT_ID = uuid5(NAMESPACE_URL, "hindsight:demo:incident:retroactive-telecom-rate")
DEMO_MEMORY_ID = uuid5(NAMESPACE_URL, "hindsight:demo:memory:retroactive-telecom-rate")
DEMO_EVENT_TIME = PRIMARY_DEMO_CASE.event_time
DEMO_DECISION_TIME = PRIMARY_DEMO_CASE.decision_time
DEMO_INVESTIGATION_TIME = PRIMARY_DEMO_CASE.investigation_time
DEMO_DISPUTE_TIME = PRIMARY_DEMO_CASE.dispute_time
DEMO_REMEDIATION_START = datetime(2026, 7, 3, 0, 2, tzinfo=UTC)
DEMO_REMEDIATION_END = datetime(2026, 7, 3, 0, 2, 1, tzinfo=UTC)


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
        value_json={
            "rate": "0.25",
            "route": DEMO_ROUTE,
            "service_type": DEMO_SERVICE_TYPE,
        },
        value_number=Decimal("0.25"),
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        recorded_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    retroactive_rate = AssertionDraft(
        **common,
        value_json={
            "rate": "0.15",
            "route": DEMO_ROUTE,
            "service_type": DEMO_SERVICE_TYPE,
        },
        value_number=Decimal("0.15"),
        valid_from=datetime(2026, 7, 1, tzinfo=UTC),
        recorded_at=datetime(2026, 7, 3, tzinfo=UTC),
    )
    return old_rate, retroactive_rate


def seed_demo(assertions: TemporalAssertionService) -> None:
    assertions.ingest_versions(demo_tariff_versions())


def demo_call(case: DemoCase = PRIMARY_DEMO_CASE) -> CallEvent:
    return CallEvent(
        id=case.call_id,
        tariff_key=DEMO_TARIFF_KEY,
        started_at=case.event_time,
        duration_seconds=case.duration_seconds,
    )


def demo_case_seed(
    audit: DecisionAudit,
    journal: DecisionJournalEntry,
    case: DemoCase = PRIMARY_DEMO_CASE,
) -> TelecomCaseSeed:
    return TelecomCaseSeed(
        cdr_id=case.cdr_id,
        external_call_id=case.call_id,
        msisdn_hash="da3d12d669f4657a318fbe5d77d3aba526b8f9e67756a6d4f734734689080a31",
        route=DEMO_ROUTE,
        service_type=DEMO_SERVICE_TYPE,
        started_at=case.event_time,
        duration_seconds=case.duration_seconds,
        invoice_id=case.invoice_id,
        decision_id=journal.record.id,
        selected_assertion_id=audit.decision.selected_assertion_id,
        billed_amount=Decimal(str(audit.decision.output["amount"])),
        currency=str(audit.decision.output["currency"]),
        invoice_created_at=journal.record.decided_at,
        dispute_id=case.dispute_id,
        claim=DEMO_DISPUTE_CLAIM,
        opened_at=case.dispute_time,
    )


def demo_remediation_plan(
    audit: DecisionAudit,
    journal: DecisionJournalEntry,
    case: TelecomCaseSeed,
) -> TelecomRemediationPlan:
    return build_remediation_plan(
        audit,
        journal,
        case,
        run_id=DEMO_REMEDIATION_RUN_ID,
        dispute_id=DEMO_DISPUTE_ID,
        refund_id=DEMO_REFUND_ID,
        incident_id=DEMO_INCIDENT_ID,
        memory_id=DEMO_MEMORY_ID,
        started_at=DEMO_REMEDIATION_START,
        completed_at=DEMO_REMEDIATION_END,
        executed_by="remediation_agent",
    )
