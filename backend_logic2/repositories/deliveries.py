"""Purchase Receipt projections and Supplier Scorecard eligibility."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from psycopg.types.json import Jsonb

from procurement_db import get_connection


def record_purchase_receipt(
    *,
    receipt_name: str,
    po_name: str,
    posting_date: date | str,
    docstatus: int,
    accepted_qty: Decimal,
    payload: dict[str, Any],
) -> None:
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO procurement.purchase_receipt_record (
                receipt_name, po_name, posting_date, docstatus,
                accepted_qty, payload
            ) VALUES (
                %(receipt_name)s, %(po_name)s, %(posting_date)s, %(docstatus)s,
                %(accepted_qty)s, %(payload)s
            )
            ON CONFLICT (receipt_name, po_name) DO UPDATE SET
                posting_date = EXCLUDED.posting_date,
                docstatus = EXCLUDED.docstatus,
                accepted_qty = EXCLUDED.accepted_qty,
                payload = EXCLUDED.payload,
                updated_at = now()
            """,
            {
                "receipt_name": receipt_name,
                "po_name": po_name,
                "posting_date": posting_date,
                "docstatus": docstatus,
                "accepted_qty": accepted_qty,
                "payload": Jsonb(payload),
            },
        )


def refresh_delivery(
    *,
    case_id: str,
    po_name: str,
    supplier: str | None,
    promised_delivery_date: date | str | None,
    ordered_qty: Decimal,
) -> dict[str, Any]:
    with get_connection() as connection:
        aggregate = connection.execute(
            """
            SELECT
                COALESCE(sum(accepted_qty) FILTER (WHERE docstatus = 1), 0) AS received_qty,
                min(posting_date) FILTER (WHERE docstatus = 1 AND accepted_qty > 0) AS first_receipt_date,
                max(posting_date) FILTER (WHERE docstatus = 1 AND accepted_qty > 0) AS latest_receipt_date
            FROM procurement.purchase_receipt_record
            WHERE po_name = %(po_name)s
            """,
            {"po_name": po_name},
        ).fetchone()
        received_qty = Decimal(str(aggregate["received_qty"] or 0))
        if received_qty <= 0:
            delivery_status = "NOT_RECEIVED"
        elif ordered_qty > 0 and received_qty >= ordered_qty:
            delivery_status = "FULL"
        else:
            delivery_status = "PARTIAL"
        full_receipt_date = aggregate["latest_receipt_date"] if delivery_status == "FULL" else None
        scorecard_status = "AVAILABLE" if delivery_status == "FULL" else "LOCKED"

        row = connection.execute(
            """
            INSERT INTO procurement.purchase_order_delivery (
                case_id, po_name, supplier, promised_delivery_date,
                ordered_qty, received_qty, delivery_status,
                first_receipt_date, full_receipt_date, scorecard_status
            ) VALUES (
                %(case_id)s, %(po_name)s, %(supplier)s, %(promised_delivery_date)s,
                %(ordered_qty)s, %(received_qty)s, %(delivery_status)s,
                %(first_receipt_date)s, %(full_receipt_date)s, %(scorecard_status)s
            )
            ON CONFLICT (po_name) DO UPDATE SET
                case_id = EXCLUDED.case_id,
                supplier = EXCLUDED.supplier,
                promised_delivery_date = EXCLUDED.promised_delivery_date,
                ordered_qty = EXCLUDED.ordered_qty,
                received_qty = EXCLUDED.received_qty,
                delivery_status = EXCLUDED.delivery_status,
                first_receipt_date = EXCLUDED.first_receipt_date,
                full_receipt_date = EXCLUDED.full_receipt_date,
                scorecard_status = CASE
                    WHEN EXCLUDED.delivery_status = 'FULL'
                         AND procurement.purchase_order_delivery.scorecard_status = 'COMPLETED'
                        THEN 'COMPLETED'
                    ELSE EXCLUDED.scorecard_status
                END,
                updated_at = now()
            RETURNING *
            """,
            {
                "case_id": case_id,
                "po_name": po_name,
                "supplier": supplier,
                "promised_delivery_date": promised_delivery_date,
                "ordered_qty": ordered_qty,
                "received_qty": received_qty,
                "delivery_status": delivery_status,
                "first_receipt_date": aggregate["first_receipt_date"],
                "full_receipt_date": full_receipt_date,
                "scorecard_status": scorecard_status,
            },
        ).fetchone()
    return dict(row)


def get_delivery_by_case(case_id: str) -> dict[str, Any] | None:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM procurement.purchase_order_delivery WHERE case_id = %(case_id)s",
            {"case_id": case_id},
        ).fetchone()
    return dict(row) if row else None


def list_deliveries_for_reconciliation() -> list[dict[str, Any]]:
    """Return PO projections whose ERP follow-up documents may have changed."""

    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT pod.*, pc.status AS case_status, pc.stage AS case_stage
            FROM procurement.purchase_order_delivery pod
            JOIN procurement.procurement_case pc ON pc.case_id = pod.case_id
            WHERE pc.status NOT IN ('CANCELLED', 'REJECTED')
            ORDER BY pod.updated_at ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def record_purchase_invoice(
    *,
    invoice_name: str,
    po_name: str,
    posting_date: date | str,
    docstatus: int,
    invoice_status: str | None,
    grand_total: Decimal,
    outstanding_amount: Decimal,
    payload: dict[str, Any],
) -> None:
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO procurement.purchase_invoice_record (
                invoice_name, po_name, posting_date, docstatus, invoice_status,
                grand_total, outstanding_amount, payload
            ) VALUES (
                %(invoice_name)s, %(po_name)s, %(posting_date)s, %(docstatus)s,
                %(invoice_status)s, %(grand_total)s, %(outstanding_amount)s,
                %(payload)s
            )
            ON CONFLICT (invoice_name, po_name) DO UPDATE SET
                posting_date = EXCLUDED.posting_date,
                docstatus = EXCLUDED.docstatus,
                invoice_status = EXCLUDED.invoice_status,
                grand_total = EXCLUDED.grand_total,
                outstanding_amount = EXCLUDED.outstanding_amount,
                payload = EXCLUDED.payload,
                updated_at = now()
            """,
            {
                "invoice_name": invoice_name,
                "po_name": po_name,
                "posting_date": posting_date,
                "docstatus": docstatus,
                "invoice_status": invoice_status,
                "grand_total": grand_total,
                "outstanding_amount": outstanding_amount,
                "payload": Jsonb(payload),
            },
        )


def record_payment_entry(
    *,
    payment_entry_name: str,
    invoice_name: str,
    posting_date: date | str,
    docstatus: int,
    allocated_amount: Decimal,
    payload: dict[str, Any],
) -> None:
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO procurement.payment_entry_record (
                payment_entry_name, invoice_name, posting_date, docstatus,
                allocated_amount, payload
            ) VALUES (
                %(payment_entry_name)s, %(invoice_name)s, %(posting_date)s,
                %(docstatus)s, %(allocated_amount)s, %(payload)s
            )
            ON CONFLICT (payment_entry_name, invoice_name) DO UPDATE SET
                posting_date = EXCLUDED.posting_date,
                docstatus = EXCLUDED.docstatus,
                allocated_amount = EXCLUDED.allocated_amount,
                payload = EXCLUDED.payload,
                updated_at = now()
            """,
            {
                "payment_entry_name": payment_entry_name,
                "invoice_name": invoice_name,
                "posting_date": posting_date,
                "docstatus": docstatus,
                "allocated_amount": allocated_amount,
                "payload": Jsonb(payload),
            },
        )


def refresh_financial_progress(po_name: str) -> dict[str, Any] | None:
    """Aggregate submitted invoices and payments into the PO read model."""

    with get_connection() as connection:
        invoice = connection.execute(
            """
            SELECT
                count(*) FILTER (WHERE docstatus = 1) AS invoice_count,
                (array_agg(invoice_name ORDER BY posting_date DESC, updated_at DESC)
                    FILTER (WHERE docstatus = 1))[1] AS latest_invoice_name,
                COALESCE(sum(grand_total) FILTER (WHERE docstatus = 1), 0) AS invoice_total,
                COALESCE(sum(outstanding_amount) FILTER (WHERE docstatus = 1), 0)
                    AS outstanding_amount
            FROM procurement.purchase_invoice_record
            WHERE po_name = %(po_name)s
            """,
            {"po_name": po_name},
        ).fetchone()
        payment = connection.execute(
            """
            SELECT
                (array_agg(per.payment_entry_name ORDER BY per.posting_date DESC,
                    per.updated_at DESC) FILTER (WHERE per.docstatus = 1))[1]
                    AS latest_payment_entry,
                max(per.posting_date) FILTER (WHERE per.docstatus = 1)
                    AS last_payment_date
            FROM procurement.payment_entry_record per
            JOIN procurement.purchase_invoice_record pir
              ON pir.invoice_name = per.invoice_name
            WHERE pir.po_name = %(po_name)s
            """,
            {"po_name": po_name},
        ).fetchone()

        invoice_count = int(invoice["invoice_count"] or 0)
        invoice_total = Decimal(str(invoice["invoice_total"] or 0))
        outstanding = max(Decimal("0"), Decimal(str(invoice["outstanding_amount"] or 0)))
        paid_amount = max(Decimal("0"), invoice_total - outstanding)
        if invoice_count == 0:
            payment_status = "NOT_INVOICED"
        elif outstanding <= 0:
            payment_status = "PAID"
        elif paid_amount > 0:
            payment_status = "PARTIALLY_PAID"
        else:
            payment_status = "UNPAID"

        row = connection.execute(
            """
            UPDATE procurement.purchase_order_delivery
            SET invoice_count = %(invoice_count)s,
                latest_invoice_name = %(latest_invoice_name)s,
                invoice_total = %(invoice_total)s,
                outstanding_amount = %(outstanding_amount)s,
                payment_status = %(payment_status)s,
                paid_amount = %(paid_amount)s,
                latest_payment_entry = %(latest_payment_entry)s,
                last_payment_date = %(last_payment_date)s,
                updated_at = now()
            WHERE po_name = %(po_name)s
            RETURNING *
            """,
            {
                "po_name": po_name,
                "invoice_count": invoice_count,
                "latest_invoice_name": invoice["latest_invoice_name"],
                "invoice_total": invoice_total,
                "outstanding_amount": outstanding,
                "payment_status": payment_status,
                "paid_amount": paid_amount,
                "latest_payment_entry": payment["latest_payment_entry"],
                "last_payment_date": payment["last_payment_date"],
            },
        ).fetchone()
    return dict(row) if row else None


def get_delivery_by_invoice(invoice_name: str) -> dict[str, Any] | None:
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT pod.*
            FROM procurement.purchase_order_delivery pod
            JOIN procurement.purchase_invoice_record pir ON pir.po_name = pod.po_name
            WHERE pir.invoice_name = %(invoice_name)s
            ORDER BY pod.updated_at DESC
            LIMIT 1
            """,
            {"invoice_name": invoice_name},
        ).fetchone()
    return dict(row) if row else None


def list_invoice_names_by_po(po_name: str) -> list[str]:
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT DISTINCT invoice_name
            FROM procurement.purchase_invoice_record
            WHERE po_name = %(po_name)s
            ORDER BY invoice_name
            """,
            {"po_name": po_name},
        ).fetchall()
    return [str(row["invoice_name"]) for row in rows]


def complete_scorecard(case_id: str, scorecard: dict[str, Any]) -> dict[str, Any]:
    with get_connection() as connection:
        row = connection.execute(
            """
            UPDATE procurement.purchase_order_delivery
            SET scorecard = %(scorecard)s, scorecard_status = 'COMPLETED', updated_at = now()
            WHERE case_id = %(case_id)s
              AND delivery_status = 'FULL'
              AND scorecard_status = 'AVAILABLE'
            RETURNING *
            """,
            {"case_id": case_id, "scorecard": Jsonb(scorecard)},
        ).fetchone()
    if row is None:
        raise ValueError("전체 입고가 확인된 평가 대기 건만 Scorecard를 제출할 수 있습니다.")
    return dict(row)
