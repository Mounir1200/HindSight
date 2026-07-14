import json
import random
import time
from collections.abc import Callable, Mapping
from decimal import Decimal
from threading import RLock
from typing import Any
from uuid import UUID

from hindsight.adapters.telecom.billing import calculate_call_amount
from hindsight.adapters.telecom.remediation import (
    RemediationConflictError,
    RemediationOutcome,
    RemediationReceipt,
    RemediationStateError,
    TelecomCaseNotFoundError,
    TelecomCaseSeed,
    TelecomCaseSnapshot,
    TelecomRemediationPlan,
    remediation_receipt_from_result,
    serialize_remediation_request,
    serialize_remediation_result,
)

INSERT_CDR_SQL = """
INSERT INTO telecom_cdrs (
  id, external_id, msisdn_hash, route, service_type, started_at,
  duration_sec, created_at
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (id) DO NOTHING
"""

INSERT_INVOICE_SQL = """
INSERT INTO telecom_invoices (
  id, cdr_id, amount, currency, status, decision_id,
  selected_assertion_id, created_at, updated_at
)
VALUES (%s, %s, %s, %s, 'issued', %s, %s, %s, %s)
ON CONFLICT (id) DO NOTHING
"""

INSERT_DISPUTE_SQL = """
INSERT INTO telecom_disputes (
  id, invoice_id, claim, status, opened_at
)
VALUES (%s, %s, %s, 'open', %s)
ON CONFLICT (id) DO NOTHING
"""

SELECT_CASE_SEED_SQL = """
SELECT
  cdr.id AS cdr_id,
  cdr.external_id,
  cdr.msisdn_hash,
  cdr.route,
  cdr.service_type,
  cdr.started_at,
  cdr.duration_sec,
  invoice.id AS invoice_id,
  invoice.cdr_id AS invoice_cdr_id,
  invoice.amount AS invoice_amount,
  invoice.currency AS invoice_currency,
  invoice.status AS invoice_status,
  invoice.decision_id AS invoice_decision_id,
  invoice.selected_assertion_id AS invoice_selected_assertion_id,
  invoice.created_at AS invoice_created_at,
  decision.subject_id AS decision_subject_id,
  decision.event_time AS decision_event_time,
  decision.selected_assertion_id AS decision_selected_assertion_id,
  decision.output AS decision_output,
  dispute.id AS dispute_id,
  dispute.invoice_id AS dispute_invoice_id,
  dispute.claim,
  dispute.opened_at
FROM telecom_disputes AS dispute
JOIN telecom_invoices AS invoice ON invoice.id = dispute.invoice_id
JOIN telecom_cdrs AS cdr ON cdr.id = invoice.cdr_id
JOIN decisions AS decision ON decision.id = invoice.decision_id
WHERE dispute.id = %s
"""

CLAIM_REMEDIATION_SQL = """
INSERT INTO remediation_runs (
  id, domain, case_type, case_id, status, started_at, executed_by, request
)
VALUES (%s, 'telecom', 'dispute', %s, 'started', %s, %s, CAST(%s AS JSONB))
ON CONFLICT (domain, case_type, case_id) DO NOTHING
RETURNING id
"""

SELECT_REMEDIATION_SQL = """
SELECT id, status, request, result
FROM remediation_runs
WHERE domain = 'telecom'
  AND case_type = 'dispute'
  AND case_id = %s
"""

SELECT_DISPUTE_FOR_UPDATE_SQL = """
SELECT id, invoice_id, status
FROM telecom_disputes
WHERE id = %s
FOR UPDATE
"""

SELECT_INVOICE_FOR_UPDATE_SQL = """
SELECT id, cdr_id, amount, currency, status, decision_id, selected_assertion_id
FROM telecom_invoices
WHERE id = %s
FOR UPDATE
"""

SELECT_FINANCIAL_CONTEXT_SQL = """
SELECT
  decision.selected_assertion_id AS decision_selected_assertion_id,
  decision.current_truth_assertion_id,
  decision.output AS decision_output,
  truth.value_number AS truth_rate,
  truth.currency AS truth_currency,
  truth.unit AS truth_unit,
  cdr.duration_sec
FROM decisions AS decision
JOIN assertions AS truth ON truth.id = decision.current_truth_assertion_id
JOIN telecom_cdrs AS cdr ON cdr.id = %s
WHERE decision.id = %s
"""

UPDATE_INVOICE_SQL = """
UPDATE telecom_invoices
SET amount = %s,
    status = 'corrected',
    selected_assertion_id = %s,
    updated_at = %s
WHERE id = %s
  AND status = 'issued'
"""

INSERT_REFUND_SQL = """
INSERT INTO telecom_refunds (
  id, dispute_id, remediation_run_id, amount, currency, status, created_at
)
VALUES (%s, %s, %s, %s, %s, 'created', %s)
"""

CLOSE_DISPUTE_SQL = """
UPDATE telecom_disputes
SET status = 'closed',
    closed_at = %s,
    resolution = CAST(%s AS JSONB)
WHERE id = %s
  AND status = 'open'
"""

INSERT_INCIDENT_SQL = """
INSERT INTO telecom_incidents (
  id, dispute_id, remediation_run_id, category, status, description, created_at
)
VALUES (%s, %s, %s, %s, 'open', %s, %s)
"""

INSERT_MEMORY_SQL = """
INSERT INTO memories (
  id, memory_key, lineage_id, version_number, domain, namespace, kind,
  content, content_struct, valid_from, recorded_at, written_by, confidence,
  remediation_run_id
)
VALUES (
  %s, %s, %s, 1, 'telecom', 'revenue_assurance', 'procedure',
  %s, CAST(%s AS JSONB), %s, %s, %s, 1.0, %s
)
"""

COMPLETE_REMEDIATION_SQL = """
UPDATE remediation_runs
SET status = 'applied',
    completed_at = %s,
    result = CAST(%s AS JSONB)
WHERE id = %s
  AND status = 'started'
"""

CASE_SNAPSHOT_SQL = """
SELECT
  invoice.amount AS invoice_amount,
  invoice.status AS invoice_status,
  invoice.selected_assertion_id,
  dispute.status AS dispute_status,
  (
    SELECT refund.amount
    FROM telecom_refunds AS refund
    WHERE refund.dispute_id = dispute.id
    LIMIT 1
  ) AS refund_amount,
  (
    SELECT count(*)
    FROM telecom_refunds AS refund
    WHERE refund.dispute_id = dispute.id
  ) AS refund_count,
  (
    SELECT count(*)
    FROM telecom_incidents AS incident
    WHERE incident.dispute_id = dispute.id
  ) AS incident_count,
  (
    SELECT count(*)
    FROM memories AS memory
    WHERE memory.memory_key = %s
  ) AS procedural_memory_count,
  (
    SELECT count(*)
    FROM remediation_runs AS run
    WHERE run.domain = 'telecom'
      AND run.case_type = 'dispute'
      AND run.case_id = %s
  ) AS remediation_run_count
FROM telecom_disputes AS dispute
JOIN telecom_invoices AS invoice ON invoice.id = dispute.invoice_id
WHERE dispute.id = %s
"""


class CockroachTelecomRemediationRepository:
    def __init__(
        self,
        connection: Any,
        max_retries: int = 3,
        connection_factory: Callable[[], Any] | None = None,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        self._connection = connection
        self._max_retries = max_retries
        self._connection_factory = connection_factory
        self._lock = RLock()

    def seed_case(self, seed: TelecomCaseSeed) -> None:
        with self._lock:
            self._retry_serializable(lambda: self._seed_case_once(seed))

    def _seed_case_once(self, seed: TelecomCaseSeed) -> None:
        with self._connection.transaction():
            self._connection.execute(
                INSERT_CDR_SQL,
                (
                    seed.cdr_id,
                    seed.external_call_id,
                    seed.msisdn_hash,
                    seed.route,
                    seed.service_type,
                    seed.started_at,
                    seed.duration_seconds,
                    seed.started_at,
                ),
            )
            self._connection.execute(
                INSERT_INVOICE_SQL,
                (
                    seed.invoice_id,
                    seed.cdr_id,
                    seed.billed_amount,
                    seed.currency,
                    seed.decision_id,
                    seed.selected_assertion_id,
                    seed.invoice_created_at,
                    seed.invoice_created_at,
                ),
            )
            self._connection.execute(
                INSERT_DISPUTE_SQL,
                (
                    seed.dispute_id,
                    seed.invoice_id,
                    seed.claim,
                    seed.opened_at,
                ),
            )
            row = self._connection.execute(
                SELECT_CASE_SEED_SQL,
                (seed.dispute_id,),
            ).fetchone()
            if row is None:
                raise RemediationStateError("telecom case seed was not persisted")
            self._validate_seed(row, seed)

    def apply_remediation(self, plan: TelecomRemediationPlan) -> RemediationReceipt:
        with self._lock:
            try:
                return self._retry_serializable(lambda: self._apply_once(plan))
            except Exception as error:
                if getattr(error, "sqlstate", None) != "40003":
                    raise
                return self._recover_ambiguous_commit(plan)

    def _apply_once(self, plan: TelecomRemediationPlan) -> RemediationReceipt:
        request = serialize_remediation_request(plan)
        with self._connection.transaction():
            claimed = self._connection.execute(
                CLAIM_REMEDIATION_SQL,
                (
                    plan.run_id,
                    plan.case_id,
                    plan.started_at,
                    plan.executed_by,
                    json.dumps(request),
                ),
            ).fetchone()
            if claimed is None:
                return self._existing_receipt(plan, request)

            dispute = self._connection.execute(
                SELECT_DISPUTE_FOR_UPDATE_SQL,
                (plan.dispute_id,),
            ).fetchone()
            if dispute is None:
                raise TelecomCaseNotFoundError(f"dispute {plan.dispute_id} was not found")
            if str(dispute["status"]) != "open":
                raise RemediationStateError("telecom dispute is not open")

            invoice = self._connection.execute(
                SELECT_INVOICE_FOR_UPDATE_SQL,
                (dispute["invoice_id"],),
            ).fetchone()
            if invoice is None:
                raise TelecomCaseNotFoundError("dispute invoice was not found")
            _validate_invoice(invoice, plan)
            context = self._connection.execute(
                SELECT_FINANCIAL_CONTEXT_SQL,
                (invoice["cdr_id"], plan.decision_id),
            ).fetchone()
            if context is None:
                raise TelecomCaseNotFoundError("decision financial context was not found")
            _validate_financial_context(context, invoice, plan)

            invoice_id = UUID(str(invoice["id"]))
            receipt = RemediationReceipt(
                outcome=RemediationOutcome.APPLIED,
                run_id=plan.run_id,
                dispute_id=plan.dispute_id,
                invoice_id=invoice_id,
                refund_id=plan.refund_id,
                incident_id=plan.incident_id,
                memory_id=plan.memory_id,
                previous_amount=_decimal(invoice["amount"]),
                corrected_amount=plan.corrected_amount,
                refund_amount=plan.refund_amount,
                currency=plan.currency,
            )
            self._apply_effects(plan, receipt)
            return receipt

    def _existing_receipt(
        self,
        plan: TelecomRemediationPlan,
        request: dict[str, object],
    ) -> RemediationReceipt:
        row = self._connection.execute(
            SELECT_REMEDIATION_SQL,
            (plan.case_id,),
        ).fetchone()
        if row is None:
            raise RemediationStateError("remediation claim was not visible")
        if _json_object(row["request"]) != request:
            raise RemediationConflictError(
                "remediation key already identifies a different request"
            )
        if str(row["status"]) != "applied" or row["result"] is None:
            raise RemediationStateError("existing remediation is incomplete")
        return remediation_receipt_from_result(
            _json_object(row["result"]),
            RemediationOutcome.ALREADY_REMEDIATED,
        )

    def _apply_effects(
        self,
        plan: TelecomRemediationPlan,
        receipt: RemediationReceipt,
    ) -> None:
        invoice_update = self._connection.execute(
            UPDATE_INVOICE_SQL,
            (
                plan.corrected_amount,
                plan.corrected_assertion_id,
                plan.completed_at,
                receipt.invoice_id,
            ),
        )
        if invoice_update.rowcount != 1:
            raise RemediationStateError("invoice correction was not applied")

        self._connection.execute(
            INSERT_REFUND_SQL,
            (
                plan.refund_id,
                plan.dispute_id,
                plan.run_id,
                plan.refund_amount,
                plan.currency,
                plan.completed_at,
            ),
        )
        resolution = {
            "type": "retroactive_tariff_correction",
            "root_cause": plan.root_cause,
            "previous_amount": format(receipt.previous_amount, "f"),
            "corrected_amount": format(plan.corrected_amount, "f"),
            "refund_id": str(plan.refund_id),
        }
        dispute_update = self._connection.execute(
            CLOSE_DISPUTE_SQL,
            (plan.completed_at, json.dumps(resolution), plan.dispute_id),
        )
        if dispute_update.rowcount != 1:
            raise RemediationStateError("dispute closure was not applied")

        self._connection.execute(
            INSERT_INCIDENT_SQL,
            (
                plan.incident_id,
                plan.dispute_id,
                plan.run_id,
                plan.root_cause,
                plan.incident_description,
                plan.completed_at,
            ),
        )
        memory_struct = {
            "root_cause": plan.root_cause,
            "source_dispute_id": str(plan.dispute_id),
            "corrected_assertion_id": str(plan.corrected_assertion_id),
            "checklist": list(plan.memory_checklist),
        }
        self._connection.execute(
            INSERT_MEMORY_SQL,
            (
                plan.memory_id,
                plan.memory_key,
                plan.memory_id,
                plan.memory_content,
                json.dumps(memory_struct),
                plan.completed_at,
                plan.completed_at,
                plan.executed_by,
                plan.run_id,
            ),
        )

        result = serialize_remediation_result(receipt)
        run_update = self._connection.execute(
            COMPLETE_REMEDIATION_SQL,
            (plan.completed_at, json.dumps(result), plan.run_id),
        )
        if run_update.rowcount != 1:
            raise RemediationStateError("remediation run was not finalized")

    def snapshot(self, dispute_id: UUID, memory_key: str) -> TelecomCaseSnapshot:
        with self._lock:
            row = self._connection.execute(
                CASE_SNAPSHOT_SQL,
                (memory_key, str(dispute_id), dispute_id),
            ).fetchone()
            if row is None:
                raise TelecomCaseNotFoundError(f"dispute {dispute_id} was not found")
            return TelecomCaseSnapshot(
                invoice_amount=_decimal(row["invoice_amount"]),
                invoice_status=str(row["invoice_status"]),
                selected_assertion_id=UUID(str(row["selected_assertion_id"])),
                dispute_status=str(row["dispute_status"]),
                refund_amount=(
                    _decimal(row["refund_amount"])
                    if row["refund_amount"] is not None
                    else None
                ),
                refund_count=int(row["refund_count"]),
                incident_count=int(row["incident_count"]),
                procedural_memory_count=int(row["procedural_memory_count"]),
                remediation_run_count=int(row["remediation_run_count"]),
            )

    def _validate_seed(
        self,
        row: Mapping[str, Any],
        seed: TelecomCaseSeed,
    ) -> None:
        _validate_seed_identity(row, seed)
        status = str(row["invoice_status"])
        if status == "issued":
            if (
                _decimal(row["invoice_amount"]) != seed.billed_amount
                or UUID(str(row["invoice_selected_assertion_id"]))
                != seed.selected_assertion_id
            ):
                raise RemediationConflictError("issued invoice does not match the case seed")
            return
        if status != "corrected":
            raise RemediationStateError("seeded invoice has an unsupported state")

        run = self._connection.execute(
            SELECT_REMEDIATION_SQL,
            (str(seed.dispute_id),),
        ).fetchone()
        if run is None or str(run["status"]) != "applied" or run["result"] is None:
            raise RemediationStateError("corrected invoice has no applied remediation")
        result = _json_object(run["result"])
        request = _json_object(run["request"])
        if (
            UUID(str(result["invoice_id"])) != seed.invoice_id
            or _decimal(result["previous_amount"]) != seed.billed_amount
            or _decimal(result["corrected_amount"]) != _decimal(row["invoice_amount"])
            or UUID(str(request["corrected_assertion_id"]))
            != UUID(str(row["invoice_selected_assertion_id"]))
        ):
            raise RemediationConflictError("corrected invoice does not match its remediation")

    def _retry_serializable[T](self, operation: Callable[[], T]) -> T:
        for attempt in range(self._max_retries + 1):
            try:
                return operation()
            except Exception as error:
                if (
                    getattr(error, "sqlstate", None) != "40001"
                    or attempt == self._max_retries
                ):
                    raise
                delay = min(0.5, 0.05 * 2**attempt) + random.uniform(0, 0.01)
                time.sleep(delay)
        raise RuntimeError("unreachable retry state")

    def _recover_ambiguous_commit(
        self,
        plan: TelecomRemediationPlan,
    ) -> RemediationReceipt:
        if self._connection_factory is None:
            raise RemediationStateError(
                "commit outcome is unknown; replay the remediation with a fresh connection"
            )
        with self._connection_factory() as connection:
            repository = CockroachTelecomRemediationRepository(
                connection,
                max_retries=self._max_retries,
            )
            return repository.apply_remediation(plan)


def _validate_invoice(invoice: Mapping[str, Any], plan: TelecomRemediationPlan) -> None:
    if UUID(str(invoice["decision_id"])) != plan.decision_id:
        raise RemediationConflictError("invoice does not belong to the audited decision")
    if str(invoice["currency"]) != plan.currency:
        raise RemediationConflictError("invoice and remediation currencies differ")
    if _decimal(invoice["amount"]) != plan.expected_billed_amount:
        raise RemediationConflictError("invoice amount changed before remediation")
    if str(invoice["status"]) != "issued":
        raise RemediationStateError("invoice is not open for correction")


def _validate_financial_context(
    context: Mapping[str, Any],
    invoice: Mapping[str, Any],
    plan: TelecomRemediationPlan,
) -> None:
    if (
        UUID(str(context["decision_selected_assertion_id"]))
        != UUID(str(invoice["selected_assertion_id"]))
    ):
        raise RemediationConflictError("invoice assertion does not match the decision")
    if UUID(str(context["current_truth_assertion_id"])) != plan.corrected_assertion_id:
        raise RemediationConflictError("correction does not use the decision's current truth")
    if context["truth_rate"] is None:
        raise RemediationConflictError("current truth has no numeric tariff rate")
    if str(context["truth_unit"]) != "minute" or str(context["truth_currency"]) != plan.currency:
        raise RemediationConflictError("current truth is not a matching per-minute tariff")

    duration_seconds = int(context["duration_sec"])
    decision_output = _json_object(context["decision_output"])
    if (
        int(decision_output["duration_seconds"]) != duration_seconds
        or _decimal(decision_output["amount"]) != _decimal(invoice["amount"])
        or str(decision_output["currency"]) != plan.currency
    ):
        raise RemediationConflictError("invoice does not match its persisted decision output")
    corrected_amount = calculate_call_amount(
        duration_seconds,
        _decimal(context["truth_rate"]),
    )
    if corrected_amount != plan.corrected_amount:
        raise RemediationConflictError("corrected amount does not match persisted truth")


def _validate_seed_identity(row: Mapping[str, Any], seed: TelecomCaseSeed) -> None:
    decision_output = _json_object(row["decision_output"])
    actual = (
        UUID(str(row["cdr_id"])),
        str(row["external_id"]),
        str(row["msisdn_hash"]),
        str(row["route"]),
        str(row["service_type"]),
        row["started_at"],
        int(row["duration_sec"]),
        UUID(str(row["invoice_id"])),
        UUID(str(row["invoice_cdr_id"])),
        str(row["invoice_currency"]),
        UUID(str(row["invoice_decision_id"])),
        row["invoice_created_at"],
        str(row["decision_subject_id"]),
        row["decision_event_time"],
        UUID(str(row["decision_selected_assertion_id"])),
        _decimal(decision_output["amount"]),
        str(decision_output["currency"]),
        int(decision_output["duration_seconds"]),
        UUID(str(row["dispute_id"])),
        UUID(str(row["dispute_invoice_id"])),
        str(row["claim"]),
        row["opened_at"],
    )
    expected = (
        seed.cdr_id,
        seed.external_call_id,
        seed.msisdn_hash,
        seed.route,
        seed.service_type,
        seed.started_at,
        seed.duration_seconds,
        seed.invoice_id,
        seed.cdr_id,
        seed.currency,
        seed.decision_id,
        seed.invoice_created_at,
        seed.external_call_id,
        seed.started_at,
        seed.selected_assertion_id,
        seed.billed_amount,
        seed.currency,
        seed.duration_seconds,
        seed.dispute_id,
        seed.invoice_id,
        seed.claim,
        seed.opened_at,
    )
    if actual != expected:
        raise RemediationConflictError("persisted telecom case differs from the seed")


def _json_object(value: object) -> dict[str, object]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("remediation JSON must be an object")
    return value


def _decimal(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))
