from datetime import UTC, datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from hindsight.adapters.telecom.models import CallEvent
from hindsight.adapters.telecom.remediation import (
    TelecomCaseSeed,
    TelecomRemediationPlan,
    build_remediation_plan,
)
from hindsight.core.assertions.models import AssertionDraft
from hindsight.core.assertions.service import TemporalAssertionService
from hindsight.core.decisions.models import DecisionAudit, DecisionJournalEntry

DEMO_TARIFF_KEY = "FR-SN-VOICE"
DEMO_CALL_ID = "CALL-2026-07-02-001"
DEMO_DECISION_ID = uuid5(NAMESPACE_URL, "hindsight:demo:retroactive-telecom-rate")
DEMO_CDR_ID = uuid5(NAMESPACE_URL, "hindsight:demo:cdr:retroactive-telecom-rate")
DEMO_INVOICE_ID = uuid5(NAMESPACE_URL, "hindsight:demo:invoice:retroactive-telecom-rate")
DEMO_DISPUTE_ID = uuid5(NAMESPACE_URL, "hindsight:demo:dispute:retroactive-telecom-rate")
DEMO_REMEDIATION_RUN_ID = uuid5(
    NAMESPACE_URL,
    "hindsight:demo:remediation:retroactive-telecom-rate",
)
DEMO_REFUND_ID = uuid5(NAMESPACE_URL, "hindsight:demo:refund:retroactive-telecom-rate")
DEMO_INCIDENT_ID = uuid5(NAMESPACE_URL, "hindsight:demo:incident:retroactive-telecom-rate")
DEMO_MEMORY_ID = uuid5(NAMESPACE_URL, "hindsight:demo:memory:retroactive-telecom-rate")
DEMO_EVENT_TIME = datetime(2026, 7, 2, 12, tzinfo=UTC)
DEMO_DECISION_TIME = datetime(2026, 7, 2, 12, 1, tzinfo=UTC)
DEMO_INVESTIGATION_TIME = datetime(2026, 7, 3, 0, 1, tzinfo=UTC)
DEMO_DISPUTE_TIME = datetime(2026, 7, 3, 0, 0, 30, tzinfo=UTC)
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


def demo_case_seed(
    audit: DecisionAudit,
    journal: DecisionJournalEntry,
) -> TelecomCaseSeed:
    return TelecomCaseSeed(
        cdr_id=DEMO_CDR_ID,
        external_call_id=DEMO_CALL_ID,
        msisdn_hash="da3d12d669f4657a318fbe5d77d3aba526b8f9e67756a6d4f734734689080a31",
        route="FR->SN",
        service_type="voice",
        started_at=DEMO_EVENT_TIME,
        duration_seconds=600,
        invoice_id=DEMO_INVOICE_ID,
        decision_id=journal.record.id,
        selected_assertion_id=audit.decision.selected_assertion_id,
        billed_amount=Decimal(str(audit.decision.output["amount"])),
        currency=str(audit.decision.output["currency"]),
        invoice_created_at=journal.record.decided_at,
        dispute_id=DEMO_DISPUTE_ID,
        claim="The call was billed using a tariff superseded by a retroactive correction.",
        opened_at=DEMO_DISPUTE_TIME,
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
