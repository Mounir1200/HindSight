from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from threading import RLock
from typing import Protocol
from uuid import UUID

from hindsight.core.decisions.models import DecisionAudit, DecisionJournalEntry


class RemediationOutcome(StrEnum):
    APPLIED = "applied"
    ALREADY_REMEDIATED = "already_remediated"


@dataclass(frozen=True, slots=True)
class TelecomCaseSeed:
    cdr_id: UUID
    external_call_id: str
    msisdn_hash: str
    route: str
    service_type: str
    started_at: datetime
    duration_seconds: int
    invoice_id: UUID
    decision_id: UUID
    selected_assertion_id: UUID
    billed_amount: Decimal
    currency: str
    invoice_created_at: datetime
    dispute_id: UUID
    claim: str
    opened_at: datetime

    def __post_init__(self) -> None:
        timestamps = (self.started_at, self.invoice_created_at, self.opened_at)
        if any(value.utcoffset() is None for value in timestamps):
            raise ValueError("case timestamps must be timezone-aware")
        if self.duration_seconds <= 0:
            raise ValueError("duration_seconds must be positive")
        if self.billed_amount < 0:
            raise ValueError("billed_amount cannot be negative")
        if not all(
            (
                self.external_call_id,
                self.msisdn_hash,
                self.route,
                self.service_type,
                self.currency,
                self.claim,
            )
        ):
            raise ValueError("telecom case text fields cannot be empty")


@dataclass(frozen=True, slots=True)
class TelecomRemediationPlan:
    run_id: UUID
    dispute_id: UUID
    decision_id: UUID
    corrected_assertion_id: UUID
    expected_billed_amount: Decimal
    corrected_amount: Decimal
    currency: str
    refund_id: UUID
    incident_id: UUID
    memory_id: UUID
    started_at: datetime
    completed_at: datetime
    executed_by: str
    root_cause: str
    incident_description: str
    memory_key: str
    memory_content: str
    memory_checklist: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.started_at.utcoffset() is None or self.completed_at.utcoffset() is None:
            raise ValueError("remediation timestamps must be timezone-aware")
        if self.completed_at < self.started_at:
            raise ValueError("completed_at cannot be earlier than started_at")
        if self.corrected_amount < 0:
            raise ValueError("corrected_amount cannot be negative")
        if self.refund_amount <= 0:
            raise ValueError("remediation requires a positive refund")
        if not all(
            (
                self.currency,
                self.executed_by,
                self.root_cause,
                self.incident_description,
                self.memory_key,
                self.memory_content,
                self.memory_checklist,
            )
        ):
            raise ValueError("remediation text fields cannot be empty")

    @property
    def refund_amount(self) -> Decimal:
        return self.expected_billed_amount - self.corrected_amount

    @property
    def case_id(self) -> str:
        return str(self.dispute_id)


@dataclass(frozen=True, slots=True)
class RemediationReceipt:
    outcome: RemediationOutcome
    run_id: UUID
    dispute_id: UUID
    invoice_id: UUID
    refund_id: UUID
    incident_id: UUID
    memory_id: UUID
    previous_amount: Decimal
    corrected_amount: Decimal
    refund_amount: Decimal
    currency: str

    @property
    def safe_noop(self) -> bool:
        return self.outcome is RemediationOutcome.ALREADY_REMEDIATED


@dataclass(frozen=True, slots=True)
class TelecomCaseSnapshot:
    invoice_amount: Decimal
    invoice_status: str
    selected_assertion_id: UUID
    dispute_status: str
    refund_amount: Decimal | None
    refund_count: int
    incident_count: int
    procedural_memory_count: int
    remediation_run_count: int


class TelecomRemediationRepository(Protocol):
    def seed_case(self, seed: TelecomCaseSeed) -> None: ...

    def apply_remediation(self, plan: TelecomRemediationPlan) -> RemediationReceipt: ...

    def snapshot(self, dispute_id: UUID, memory_key: str) -> TelecomCaseSnapshot: ...


@dataclass(slots=True)
class _InMemoryCase:
    seed: TelecomCaseSeed
    invoice_amount: Decimal
    selected_assertion_id: UUID
    invoice_status: str = "issued"
    dispute_status: str = "open"
    request: dict[str, object] | None = None
    receipt: RemediationReceipt | None = None


class InMemoryTelecomRemediationRepository:
    def __init__(self) -> None:
        self._cases: dict[UUID, _InMemoryCase] = {}
        self._lock = RLock()

    def seed_case(self, seed: TelecomCaseSeed) -> None:
        with self._lock:
            existing = self._cases.get(seed.dispute_id)
            if existing is not None:
                if existing.seed != seed:
                    raise RemediationConflictError("case id already identifies different data")
                return
            self._cases[seed.dispute_id] = _InMemoryCase(
                seed=seed,
                invoice_amount=seed.billed_amount,
                selected_assertion_id=seed.selected_assertion_id,
            )

    def apply_remediation(self, plan: TelecomRemediationPlan) -> RemediationReceipt:
        request = serialize_remediation_request(plan)
        with self._lock:
            state = self._required_case(plan.dispute_id)
            if state.receipt is not None:
                if state.request != request:
                    raise RemediationConflictError(
                        "remediation key already identifies a different request"
                    )
                return replace(
                    state.receipt,
                    outcome=RemediationOutcome.ALREADY_REMEDIATED,
                )

            _validate_case(state, plan)
            receipt = RemediationReceipt(
                outcome=RemediationOutcome.APPLIED,
                run_id=plan.run_id,
                dispute_id=plan.dispute_id,
                invoice_id=state.seed.invoice_id,
                refund_id=plan.refund_id,
                incident_id=plan.incident_id,
                memory_id=plan.memory_id,
                previous_amount=state.invoice_amount,
                corrected_amount=plan.corrected_amount,
                refund_amount=plan.refund_amount,
                currency=plan.currency,
            )
            state.invoice_amount = plan.corrected_amount
            state.selected_assertion_id = plan.corrected_assertion_id
            state.invoice_status = "corrected"
            state.dispute_status = "closed"
            state.request = request
            state.receipt = receipt
            return receipt

    def snapshot(self, dispute_id: UUID, memory_key: str) -> TelecomCaseSnapshot:
        with self._lock:
            state = self._required_case(dispute_id)
            receipt = state.receipt
            has_memory = (
                receipt is not None
                and state.request is not None
                and state.request["memory_key"] == memory_key
            )
            return TelecomCaseSnapshot(
                invoice_amount=state.invoice_amount,
                invoice_status=state.invoice_status,
                selected_assertion_id=state.selected_assertion_id,
                dispute_status=state.dispute_status,
                refund_amount=receipt.refund_amount if receipt else None,
                refund_count=int(receipt is not None),
                incident_count=int(receipt is not None),
                procedural_memory_count=int(has_memory),
                remediation_run_count=int(receipt is not None),
            )

    def _required_case(self, dispute_id: UUID) -> _InMemoryCase:
        try:
            return self._cases[dispute_id]
        except KeyError as error:
            raise TelecomCaseNotFoundError(f"dispute {dispute_id} was not found") from error


def build_remediation_plan(
    audit: DecisionAudit,
    journal: DecisionJournalEntry,
    case: TelecomCaseSeed,
    *,
    run_id: UUID,
    dispute_id: UUID,
    refund_id: UUID,
    incident_id: UUID,
    memory_id: UUID,
    started_at: datetime,
    completed_at: datetime,
    executed_by: str,
) -> TelecomRemediationPlan:
    _validate_remediation_context(audit, journal, case, dispute_id)

    root_cause = audit.verdict.root_cause
    if root_cause is None:
        raise RemediationStateError("audit has no remediable root cause")
    currency = str(audit.comparison.details["currency"])
    billed_amount = _decimal(audit.comparison.details["billed_amount"])
    corrected_amount = _decimal(audit.comparison.details["expected_amount"])
    return TelecomRemediationPlan(
        run_id=run_id,
        dispute_id=dispute_id,
        decision_id=journal.record.id,
        corrected_assertion_id=audit.verdict.current_truth_assertion_id,
        expected_billed_amount=billed_amount,
        corrected_amount=corrected_amount,
        currency=currency,
        refund_id=refund_id,
        incident_id=incident_id,
        memory_id=memory_id,
        started_at=started_at,
        completed_at=completed_at,
        executed_by=executed_by,
        root_cause=root_cause,
        incident_description=(
            "A retroactive tariff became known after the original billing decision."
        ),
        memory_key=f"telecom:dispute:{dispute_id}:remediation",
        memory_content=(
            "For delayed retroactive tariffs, compare valid time with recorded time, "
            "correct the invoice, refund the overcharge, and open an ingestion incident."
        ),
        memory_checklist=(
            "Reconstruct current truth and knowledge at decision time",
            "Verify that delayed ingestion caused the knowledge gap",
            "Correct the invoice and refund only the overcharge",
            "Open an ingestion incident",
        ),
    )


def serialize_remediation_request(plan: TelecomRemediationPlan) -> dict[str, object]:
    return {
        "dispute_id": str(plan.dispute_id),
        "decision_id": str(plan.decision_id),
        "corrected_assertion_id": str(plan.corrected_assertion_id),
        "expected_billed_amount": format(plan.expected_billed_amount, "f"),
        "corrected_amount": format(plan.corrected_amount, "f"),
        "currency": plan.currency,
        "root_cause": plan.root_cause,
        "incident_description": plan.incident_description,
        "memory_key": plan.memory_key,
        "memory_content": plan.memory_content,
        "memory_checklist": list(plan.memory_checklist),
    }


def serialize_remediation_result(receipt: RemediationReceipt) -> dict[str, object]:
    return {
        "run_id": str(receipt.run_id),
        "dispute_id": str(receipt.dispute_id),
        "invoice_id": str(receipt.invoice_id),
        "refund_id": str(receipt.refund_id),
        "incident_id": str(receipt.incident_id),
        "memory_id": str(receipt.memory_id),
        "previous_amount": format(receipt.previous_amount, "f"),
        "corrected_amount": format(receipt.corrected_amount, "f"),
        "refund_amount": format(receipt.refund_amount, "f"),
        "currency": receipt.currency,
    }


def remediation_receipt_from_result(
    result: dict[str, object],
    outcome: RemediationOutcome,
) -> RemediationReceipt:
    return RemediationReceipt(
        outcome=outcome,
        run_id=UUID(str(result["run_id"])),
        dispute_id=UUID(str(result["dispute_id"])),
        invoice_id=UUID(str(result["invoice_id"])),
        refund_id=UUID(str(result["refund_id"])),
        incident_id=UUID(str(result["incident_id"])),
        memory_id=UUID(str(result["memory_id"])),
        previous_amount=_decimal(result["previous_amount"]),
        corrected_amount=_decimal(result["corrected_amount"]),
        refund_amount=_decimal(result["refund_amount"]),
        currency=str(result["currency"]),
    )


def _validate_case(state: _InMemoryCase, plan: TelecomRemediationPlan) -> None:
    if state.seed.decision_id != plan.decision_id:
        raise RemediationConflictError("invoice does not belong to the audited decision")
    if state.seed.currency != plan.currency:
        raise RemediationConflictError("invoice and remediation currencies differ")
    if state.invoice_amount != plan.expected_billed_amount:
        raise RemediationConflictError("invoice amount changed before remediation")
    if state.invoice_status != "issued" or state.dispute_status != "open":
        raise RemediationStateError("telecom case is not open for remediation")


def _validate_remediation_context(
    audit: DecisionAudit,
    journal: DecisionJournalEntry,
    case: TelecomCaseSeed,
    dispute_id: UUID,
) -> None:
    record = journal.record
    if record.verdict != audit.verdict:
        raise RemediationConflictError("journal does not match the audited decision")
    if case.dispute_id != dispute_id:
        raise RemediationConflictError("remediation dispute does not match the case")
    if case.decision_id != record.id:
        raise RemediationConflictError("case does not belong to the audited decision")
    if case.external_call_id != record.subject_id:
        raise RemediationConflictError("case call does not match the decision subject")
    if case.started_at != record.event_time or record.event_time != audit.lookup.event_time:
        raise RemediationConflictError("case event time does not match the decision")
    if (
        case.selected_assertion_id != record.selected_assertion_id
        or record.selected_assertion_id != audit.decision.selected_assertion_id
    ):
        raise RemediationConflictError("case selected assertion does not match the audit")

    record_amount = _decimal(record.output["amount"])
    audit_amount = _decimal(audit.decision.output["amount"])
    if case.billed_amount != record_amount or record_amount != audit_amount:
        raise RemediationConflictError("case billed amount does not match the decision")
    record_currency = str(record.output["currency"])
    audit_currency = str(audit.decision.output["currency"])
    if case.currency != record_currency or record_currency != audit_currency:
        raise RemediationConflictError("case currency does not match the decision")
    if case.duration_seconds != int(record.output["duration_seconds"]):
        raise RemediationConflictError("case duration does not match the decision")


def _decimal(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


class RemediationConflictError(ValueError):
    pass


class RemediationStateError(RuntimeError):
    pass


class TelecomCaseNotFoundError(LookupError):
    pass
