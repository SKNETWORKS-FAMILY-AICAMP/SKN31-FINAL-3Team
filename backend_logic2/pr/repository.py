"""Database operations for supplier purchase-response requests."""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from procurement_db import get_connection


def create_request(values: dict[str, Any]) -> dict[str, Any]:
    with get_connection() as connection:
        row = connection.execute(
            """
            INSERT INTO procurement.supplier_purchase_response (
                case_id, mr_name, rfq_name, supplier_quotation,
                supplier_id, supplier_email, token_hash, expires_at,
                purchase_mode, direct_purchase_items, status
            ) VALUES (
                %(case_id)s, %(mr_name)s, %(rfq_name)s, %(supplier_quotation)s,
                %(supplier_id)s, %(supplier_email)s, %(token_hash)s,
                %(expires_at)s, %(purchase_mode)s, %(direct_purchase_items)s,
                'DRAFT'
            )
            ON CONFLICT (case_id)
                WHERE status IN ('DRAFT', 'SENT', 'ACCEPTED', 'PO_FAILED')
            DO NOTHING
            RETURNING *
            """,
            {**values, "direct_purchase_items": Jsonb(values.get("direct_purchase_items") or {})},
        ).fetchone()
        created = row is not None
        if row is None:
            row = connection.execute(
                """
                SELECT * FROM procurement.supplier_purchase_response
                WHERE case_id = %(case_id)s
                  AND status IN ('DRAFT', 'SENT', 'ACCEPTED', 'PO_FAILED')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                {"case_id": values["case_id"]},
            ).fetchone()
    if row is None:
        raise RuntimeError("활성 PR을 생성하거나 조회하지 못했습니다.")
    result = dict(row)
    result["_created"] = created
    return result


def get_active_request(case_id: str) -> dict[str, Any] | None:
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT * FROM procurement.supplier_purchase_response
            WHERE case_id = %(case_id)s
              AND status IN ('DRAFT', 'SENT', 'ACCEPTED', 'PO_FAILED')
            ORDER BY created_at DESC
            LIMIT 1
            """,
            {"case_id": case_id},
        ).fetchone()
    return dict(row) if row else None


def mark_sent(pr_id: str) -> dict[str, Any]:
    with get_connection() as connection:
        row = connection.execute(
            """
            UPDATE procurement.supplier_purchase_response
            SET status = 'SENT', sent_at = now(), updated_at = now()
            WHERE pr_id = %(pr_id)s AND status = 'DRAFT'
            RETURNING *
            """,
            {"pr_id": pr_id},
        ).fetchone()
    if not row:
        raise RuntimeError("PR 발송 상태를 갱신하지 못했습니다.")
    return dict(row)


def cancel_draft(pr_id: str, *, error: str) -> None:
    """Close an unsent draft so a later graph retry can issue a fresh PR."""
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE procurement.supplier_purchase_response
            SET status = 'CANCELLED',
                processing_error = %(error)s,
                processing_error_stage = 'email',
                processing_failed_at = now(),
                updated_at = now()
            WHERE pr_id = %(pr_id)s AND status = 'DRAFT'
            """,
            {"pr_id": pr_id, "error": error[:2000]},
        )


def get_by_token_hash(token_hash: str) -> dict[str, Any] | None:
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT * FROM procurement.supplier_purchase_response
            WHERE token_hash = %(token_hash)s
            """,
            {"token_hash": token_hash},
        ).fetchone()
    return dict(row) if row else None


def record_response(token_hash: str, decision: str, reason: str | None) -> dict[str, Any] | None:
    """Atomically consume a token; only a live SENT request can be answered."""
    with get_connection() as connection:
        row = connection.execute(
            """
            UPDATE procurement.supplier_purchase_response
            SET status = %(status)s,
                rejection_reason = %(reason)s,
                responded_at = now(), updated_at = now()
            WHERE token_hash = %(token_hash)s
              AND status = 'SENT'
              AND expires_at > now()
            RETURNING *
            """,
            {"token_hash": token_hash, "status": decision, "reason": reason},
        ).fetchone()
    return dict(row) if row else None


def record_po_result(pr_id: str, *, po_name: str | None, error: str | None = None) -> dict[str, Any]:
    with get_connection() as connection:
        row = connection.execute(
            """
            UPDATE procurement.supplier_purchase_response
            SET status = CASE WHEN %(error)s IS NULL THEN 'PO_CREATED' ELSE 'PO_FAILED' END,
                po_name = %(po_name)s,
                po_error = %(error)s,
                processing_error = CASE WHEN %(error)s IS NULL THEN NULL ELSE processing_error END,
                processing_error_stage = CASE WHEN %(error)s IS NULL THEN NULL ELSE processing_error_stage END,
                processing_failed_at = CASE WHEN %(error)s IS NULL THEN NULL ELSE processing_failed_at END,
                updated_at = now()
            WHERE pr_id = %(pr_id)s AND status IN ('ACCEPTED', 'PO_FAILED')
            RETURNING *
            """,
            {"pr_id": pr_id, "po_name": po_name, "error": error},
        ).fetchone()
    if not row:
        raise RuntimeError("수락된 PR의 PO 결과를 기록하지 못했습니다.")
    return dict(row)


def record_processing_error(pr_id: str, *, stage: str, error: str) -> dict[str, Any]:
    with get_connection() as connection:
        row = connection.execute(
            """
            UPDATE procurement.supplier_purchase_response
            SET processing_error = %(error)s,
                processing_error_stage = %(stage)s,
                processing_failed_at = now(),
                updated_at = now()
            WHERE pr_id = %(pr_id)s
            RETURNING *
            """,
            {"pr_id": pr_id, "stage": stage[:30], "error": error[:2000]},
        ).fetchone()
    if not row:
        raise LookupError(pr_id)
    return dict(row)


def list_requests(*, case_id: str | None = None) -> list[dict[str, Any]]:
    where = "WHERE case_id = %(case_id)s" if case_id else ""
    with get_connection() as connection:
        rows = connection.execute(
            f"""
            SELECT pr_id, case_id, mr_name, rfq_name, supplier_quotation,
                   supplier_id, supplier_email, status, rejection_reason,
                   sent_at, responded_at, expires_at, po_name, po_error,
                   processing_error, processing_error_stage, processing_failed_at,
                   created_at, updated_at
            FROM procurement.supplier_purchase_response
            {where}
            ORDER BY created_at DESC
            """,
            {"case_id": case_id} if case_id else {},
        ).fetchall()
    return [dict(row) for row in rows]
