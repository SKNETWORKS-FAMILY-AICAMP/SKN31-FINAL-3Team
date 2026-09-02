"""Persistence for one-item-per-MR procurement cases.

ERPNext owns the original Material Request.  The summary stored here is only a
read model for fast list rendering and offline recovery; API actions always use
``mr_name`` to re-read the authoritative ERP document before a write.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb

from procurement_db import get_connection


class CaseConflictError(RuntimeError):
    """Raised when an optimistic version check detects a concurrent update."""


_TERMINAL_CASE_STATUSES = {"COMPLETED", "CANCELLED", "REJECTED"}


def _erp_datetime(value: Any) -> datetime | None:
    """Normalize Frappe's usually-naive site timestamp for safe comparisons."""

    if value in (None, ""):
        return None
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        site_timezone = os.getenv("ERPNEXT_TIMEZONE", "Asia/Seoul")
        parsed = parsed.replace(tzinfo=ZoneInfo(site_timezone))
    return parsed.astimezone(timezone.utc)


def is_recreated_material_request(
    existing_case: dict[str, Any] | None,
    material_request: dict[str, Any],
) -> bool:
    """Return true when ERP reused a deleted MR name for a newer document.

    A rejected case may intentionally remain Draft in ERPNext, so terminal
    status plus docstatus=0 alone is not sufficient.  The ERP document must
    have been created after the former case reached its terminal state.
    """

    if not existing_case or existing_case.get("status") not in _TERMINAL_CASE_STATUSES:
        return False
    incoming_created_at = _erp_datetime(material_request.get("creation"))
    terminal_at = (
        existing_case.get("cancelled_at")
        or existing_case.get("completed_at")
        or existing_case.get("updated_at")
    )
    terminal_at = _erp_datetime(terminal_at)
    return bool(incoming_created_at and terminal_at and incoming_created_at > terminal_at)


def material_request_thread_id(
    mr_name: str,
    material_request: dict[str, Any],
    *,
    recreated: bool,
) -> str:
    """Return a checkpoint id that never reuses an archived MR execution.

    Frappe may reuse a deleted naming-series value.  PostgreSQL rows are
    archived correctly in that situation, but LangGraph checkpoints live in a
    separate SQLite store and are keyed only by ``thread_id``.  Reusing the MR
    name there can therefore resume a former document halfway through its old
    workflow.  Only recreated documents need a generation suffix; ordinary
    retries keep the stable MR-name thread id.
    """

    if not recreated:
        return mr_name
    created_at = _erp_datetime(material_request.get("creation"))
    generation = (
        created_at.strftime("%Y%m%dT%H%M%S%fZ")
        if created_at
        else datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    )
    return f"{mr_name}:recreated:{generation}"[:180]


def _json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return _json_value(value.value)
    if isinstance(value, UUID):
        return str(value)
    return value


def material_request_summary(material_request: dict[str, Any]) -> dict[str, Any]:
    """Build the stable UI projection for the confirmed one-item MR rule."""

    items = material_request.get("items") or []
    item = items[0] if items else {}
    qty = item.get("qty") or item.get("stock_qty") or 0
    rate = item.get("rate") or item.get("valuation_rate") or 0
    amount = item.get("amount")
    if amount is None:
        try:
            amount = Decimal(str(qty)) * Decimal(str(rate))
        except (ValueError, TypeError):
            amount = 0

    return _json_value(
        {
            "mr_name": material_request.get("name"),
            "creation": material_request.get("creation"),
            "transaction_date": material_request.get("transaction_date"),
            "schedule_date": item.get("schedule_date") or material_request.get("schedule_date"),
            "material_request_type": material_request.get("material_request_type"),
            "company": material_request.get("company"),
            "requester": (
                material_request.get("requested_by")
                or material_request.get("owner")
                or material_request.get("modified_by")
            ),
            "department": material_request.get("department"),
            "item_code": item.get("item_code"),
            "item_name": item.get("item_name") or item.get("item_code"),
            "item_group": item.get("item_group"),
            "description": item.get("description"),
            "qty": qty,
            "uom": item.get("uom") or item.get("stock_uom"),
            "rate": rate,
            "amount": amount,
            "warehouse": item.get("warehouse"),
            "attachments": material_request.get("_attachments") or [],
            "erp_status": material_request.get("status"),
            "docstatus": material_request.get("docstatus"),
            "modified": material_request.get("modified"),
        }
    )


def upsert_case_from_material_request(material_request: dict[str, Any]) -> dict[str, Any]:
    mr_name = str(material_request.get("name") or "").strip()
    if not mr_name:
        raise ValueError("Material Request name is required.")

    items = material_request.get("items") or []
    if len(items) != 1:
        raise ValueError(
            f"MR당 품목은 정확히 1개여야 합니다: {mr_name} (현재 {len(items)}개)"
        )

    summary = material_request_summary(material_request)
    item = items[0]
    requester_id = summary.get("requester")

    with get_connection() as connection:
        existing_case = connection.execute(
            """
            SELECT case_id, mr_name, thread_id, status, updated_at,
                   completed_at, cancelled_at
            FROM procurement.procurement_case
            WHERE mr_name = %(mr_name)s
            FOR UPDATE
            """,
            {"mr_name": mr_name},
        ).fetchone()

        recreated = is_recreated_material_request(
            dict(existing_case) if existing_case else None,
            material_request,
        )
        if recreated:
            # Frappe can reuse the last naming-series number after a test MR is
            # deleted. Keep the previous workflow/audit rows addressable, but
            # release its unique MR/thread identifiers for a genuinely new case.
            suffix = f"#archived-{existing_case['case_id']}"
            archived_mr_name = f"{mr_name[: max(0, 140 - len(suffix))]}{suffix}"
            old_thread_id = str(existing_case.get("thread_id") or mr_name)
            archived_thread_id = (
                f"{old_thread_id[: max(0, 180 - len(suffix))]}{suffix}"
            )
            connection.execute(
                """
                UPDATE procurement.procurement_case
                SET mr_name = %(archived_mr_name)s,
                    thread_id = %(archived_thread_id)s,
                    updated_at = now()
                WHERE case_id = %(case_id)s
                """,
                {
                    "case_id": existing_case["case_id"],
                    "archived_mr_name": archived_mr_name,
                    "archived_thread_id": archived_thread_id,
                },
            )

        row = connection.execute(
            """
            INSERT INTO procurement.procurement_case (
                case_id, mr_name, thread_id, status, stage, item_code,
                item_name, requester_id, summary, erp_modified_at
            )
            VALUES (
                gen_random_uuid(), %(mr_name)s, %(thread_id)s,
                'AWAITING_MR_REVIEW', 'MR_REVIEW', %(item_code)s,
                %(item_name)s, %(requester_id)s, %(summary)s,
                %(erp_modified_at)s
            )
            ON CONFLICT (mr_name) WHERE mr_name IS NOT NULL DO UPDATE SET
                item_code = EXCLUDED.item_code,
                item_name = EXCLUDED.item_name,
                requester_id = EXCLUDED.requester_id,
                summary = EXCLUDED.summary,
                erp_modified_at = EXCLUDED.erp_modified_at,
                updated_at = now(),
                version = procurement.procurement_case.version + 1
            RETURNING *
            """,
            {
                "mr_name": mr_name,
                "thread_id": material_request_thread_id(
                    mr_name,
                    material_request,
                    recreated=recreated,
                ),
                "item_code": item.get("item_code"),
                "item_name": item.get("item_name") or item.get("item_code"),
                "requester_id": requester_id,
                "summary": Jsonb(summary),
                "erp_modified_at": _erp_datetime(material_request.get("modified")),
            },
        ).fetchone()
    return dict(row)


def get_case(case_id: str) -> dict[str, Any] | None:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM procurement.procurement_case WHERE case_id = %(case_id)s",
            {"case_id": case_id},
        ).fetchone()
    return dict(row) if row else None


def get_case_by_mr(mr_name: str) -> dict[str, Any] | None:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM procurement.procurement_case WHERE mr_name = %(mr_name)s",
            {"mr_name": mr_name},
        ).fetchone()
    return dict(row) if row else None


def list_cases_missing_item_projection(*, limit: int = 200) -> list[dict[str, Any]]:
    """Return legacy cases whose ERP item summary was never populated."""
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT case_id, mr_name
            FROM procurement.procurement_case
            WHERE mr_name IS NOT NULL
              AND (item_code IS NULL OR btrim(item_code) = '')
            ORDER BY updated_at DESC
            LIMIT %(limit)s
            """,
            {"limit": min(max(limit, 1), 500)},
        ).fetchall()
    return [dict(row) for row in rows]


def list_open_case_references() -> list[dict[str, Any]]:
    """Return the lightweight set needed to reconcile DB cases with ERPNext."""

    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT case_id, mr_name, status, stage
            FROM procurement.procurement_case
            WHERE mr_name IS NOT NULL
              AND status NOT IN ('COMPLETED', 'CANCELLED', 'REJECTED')
            ORDER BY updated_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def get_case_by_po(po_name: str) -> dict[str, Any] | None:
    """Resolve a case from the PO name persisted in its graph snapshot."""

    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT *
            FROM procurement.procurement_case
            WHERE workflow_snapshot #>> '{values,po_name}' = %(po_name)s
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            {"po_name": po_name},
        ).fetchone()
    return dict(row) if row else None


def list_cases(
    *,
    status: str | None = None,
    stage: str | None = None,
    include_closed: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    conditions: list[str] = []
    params: dict[str, Any] = {"limit": min(max(limit, 1), 200), "offset": max(offset, 0)}
    if status:
        conditions.append("pc.status = %(status)s")
        params["status"] = status
    if stage:
        conditions.append("pc.stage = %(stage)s")
        params["stage"] = stage
    if not include_closed:
        conditions.append("pc.status NOT IN ('COMPLETED', 'CANCELLED', 'REJECTED')")

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    with get_connection() as connection:
        rows = connection.execute(
            f"""
            SELECT pc.*,
                   COALESCE(task_counts.pending_task_count, 0) AS pending_task_count,
                   pending_task.task AS pending_task,
                   delivery.projection AS delivery
            FROM procurement.procurement_case pc
            LEFT JOIN (
                SELECT case_id, count(*) AS pending_task_count
                FROM procurement.human_task
                WHERE status = 'PENDING'
                GROUP BY case_id
            ) task_counts ON task_counts.case_id = pc.case_id
            LEFT JOIN LATERAL (
                SELECT to_jsonb(ht.*) AS task
                FROM procurement.human_task ht
                WHERE ht.case_id = pc.case_id AND ht.status = 'PENDING'
                ORDER BY ht.created_at DESC
                LIMIT 1
            ) pending_task ON true
            LEFT JOIN LATERAL (
                SELECT to_jsonb(pod.*) AS projection
                FROM procurement.purchase_order_delivery pod
                WHERE pod.case_id = pc.case_id
                ORDER BY pod.updated_at DESC
                LIMIT 1
            ) delivery ON true
            {where}
            ORDER BY
                NULLIF(pc.summary->>'schedule_date', '')::date NULLS LAST,
                pc.updated_at DESC
            LIMIT %(limit)s OFFSET %(offset)s
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def transition_case(
    case_id: str,
    *,
    status: str,
    stage: str | None = None,
    reason: str | None = None,
    triggered_by: str | None = None,
    workflow_snapshot: dict[str, Any] | None = None,
    last_error: str | None = None,
    expected_version: int | None = None,
) -> dict[str, Any]:
    """Atomically update current state and append a UUID audit row."""

    with get_connection() as connection:
        current = connection.execute(
            """
            SELECT status, stage, version
            FROM procurement.procurement_case
            WHERE case_id = %(case_id)s
            FOR UPDATE
            """,
            {"case_id": case_id},
        ).fetchone()
        if current is None:
            raise KeyError(case_id)
        if expected_version is not None and current["version"] != expected_version:
            raise CaseConflictError(
                f"case version mismatch: expected {expected_version}, actual {current['version']}"
            )

        target_stage = stage or current["stage"]
        row = connection.execute(
            """
            UPDATE procurement.procurement_case
            SET status = %(status)s,
                stage = %(stage)s,
                workflow_snapshot = COALESCE(%(snapshot)s, workflow_snapshot),
                last_error = %(last_error)s,
                completed_at = CASE
                    WHEN %(status)s = 'COMPLETED' THEN COALESCE(completed_at, now())
                    ELSE NULL
                END,
                cancelled_at = CASE
                    WHEN %(status)s IN ('CANCELLED', 'REJECTED') THEN COALESCE(cancelled_at, now())
                    ELSE NULL
                END,
                updated_at = now(),
                version = version + 1
            WHERE case_id = %(case_id)s
            RETURNING *
            """,
            {
                "case_id": case_id,
                "status": status,
                "stage": target_stage,
                "snapshot": Jsonb(_json_value(workflow_snapshot)) if workflow_snapshot is not None else None,
                "last_error": last_error,
            },
        ).fetchone()
        connection.execute(
            """
            INSERT INTO procurement.workflow_status_history (
                case_id, from_status, to_status, stage, reason, triggered_by
            ) VALUES (
                %(case_id)s, %(from_status)s, %(to_status)s,
                %(stage)s, %(reason)s, %(triggered_by)s
            )
            """,
            {
                "case_id": case_id,
                "from_status": current["status"],
                "to_status": status,
                "stage": target_stage,
                "reason": reason,
                "triggered_by": triggered_by,
            },
        )
    return dict(row)


def update_quotation_deadline(case_id: str, deadline_at: datetime | str) -> dict[str, Any]:
    with get_connection() as connection:
        row = connection.execute(
            """
            UPDATE procurement.procurement_case
            SET quotation_deadline_at = %(deadline_at)s,
                updated_at = now(), version = version + 1
            WHERE case_id = %(case_id)s
            RETURNING *
            """,
            {"case_id": case_id, "deadline_at": deadline_at},
        ).fetchone()
    if row is None:
        raise KeyError(case_id)
    return dict(row)
