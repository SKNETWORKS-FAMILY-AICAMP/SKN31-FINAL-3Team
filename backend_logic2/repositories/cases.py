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
from backend_logic2.integrations.assignment_config import get_category_manager


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

    item_group = summary.get("item_group")
    assigned_user_id = get_category_manager(item_group)

    with get_connection() as connection:
        existing_case = connection.execute(
            """
            SELECT *
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
        if existing_case and not recreated:
            existing = dict(existing_case)
            # 짧은 주기의 폴링은 같은 Draft MR을 계속 발견합니다. ERP의
            # 실제 필드/첨부파일 투영이 같으면 updated_at과 version을 불필요하게
            # 올리지 않아 SSE 재조회·행 점멸·DB 쓰기 증폭을 막습니다.
            if (
                existing.get("summary") == summary
                and existing.get("item_code") == item.get("item_code")
                and existing.get("item_name")
                == (item.get("item_name") or item.get("item_code"))
                and existing.get("requester_id") == requester_id
                and existing.get("assigned_user_id") == assigned_user_id
            ):
                return existing
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
                item_name, requester_id, assigned_user_id,
                summary, erp_modified_at
            )
            VALUES (
                gen_random_uuid(), %(mr_name)s, %(thread_id)s,
                'AWAITING_MR_REVIEW', 'MR_REVIEW', %(item_code)s,
                %(item_name)s, %(requester_id)s, %(assigned_user_id)s,
                %(summary)s, %(erp_modified_at)s
            )
            ON CONFLICT (mr_name) WHERE mr_name IS NOT NULL DO UPDATE SET
                item_code = EXCLUDED.item_code,
                item_name = EXCLUDED.item_name,
                requester_id = EXCLUDED.requester_id,
                assigned_user_id = EXCLUDED.assigned_user_id,
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
                "assigned_user_id": assigned_user_id,
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
            SELECT case_id, mr_name, thread_id, status, stage, item_code, item_name,
                   requester_id, assigned_user_id, summary, erp_modified_at
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


def get_case_by_rfq(rfq_name: str) -> dict[str, Any] | None:
    """Resolve the procurement case that created one ERPNext RFQ.

    ⚠️ 재비딩 시 지난 RFQ를 더 이상 취소·폐기하지 않고 그대로 두기
    때문에(rfq_rounds 이력 - "차수" 조회 기능), 지금 진행 중인
    라운드(values.rfq_name)뿐 아니라 지난 라운드들의 RFQ 이름도 함께
    찾아야 한다. 그렇지 않으면 지난 라운드의 RFQ 포털로 뒤늦게 들어온
    Supplier Quotation 웹훅이 케이스를 못 찾고 조용히 버려진다."""

    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT *
            FROM procurement.procurement_case
            WHERE workflow_snapshot #>> '{values,rfq_name}' = %(rfq_name)s
               OR EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(
                        COALESCE(workflow_snapshot #> '{values,rfq_rounds}', '[]'::jsonb)
                    ) AS round_entry
                    WHERE round_entry->>'rfq_name' = %(rfq_name)s
               )
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            {"rfq_name": rfq_name},
        ).fetchone()
    return dict(row) if row else None


def get_case_by_supplier_quotation(quotation_name: str) -> dict[str, Any] | None:
    """Resolve a case from a previously projected SQ, including delete events."""

    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT *
            FROM procurement.procurement_case pc
            WHERE EXISTS (
                SELECT 1
                FROM jsonb_array_elements(
                    COALESCE(pc.quotation_snapshot->'quotations', '[]'::jsonb)
                ) AS quotation
                WHERE quotation->>'name' = %(quotation_name)s
            )
               OR EXISTS (
                SELECT 1
                FROM jsonb_array_elements(
                    COALESCE(
                        pc.workflow_snapshot->'values'->'quotation_ranking',
                        '[]'::jsonb
                    )
                ) AS quotation
                WHERE quotation->>'name' = %(quotation_name)s
            )
            ORDER BY pc.updated_at DESC
            LIMIT 1
            """,
            {"quotation_name": quotation_name},
        ).fetchone()
    return dict(row) if row else None


def list_cases_for_quotation_reconciliation() -> list[dict[str, Any]]:
    """Return cases whose live Supplier Quotation responses can still change."""

    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM procurement.procurement_case
            WHERE status NOT IN ('COMPLETED', 'CANCELLED', 'REJECTED')
              AND stage IN ('QUOTATION_COLLECTION', 'SUPPLIER_SELECTION', 'ORDER_START')
              AND workflow_snapshot #>> '{values,rfq_name}' IS NOT NULL
            ORDER BY updated_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def update_quotation_snapshot(
    case_id: str, quotation_snapshot: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """Update the live quotation read model only when its business data changed."""

    serialized = Jsonb(_json_value(quotation_snapshot))
    with get_connection() as connection:
        row = connection.execute(
            """
            UPDATE procurement.procurement_case
            SET quotation_snapshot = %(quotation_snapshot)s,
                updated_at = now(),
                version = version + 1
            WHERE case_id = %(case_id)s
              AND quotation_snapshot IS DISTINCT FROM %(quotation_snapshot)s
            RETURNING *
            """,
            {"case_id": case_id, "quotation_snapshot": serialized},
        ).fetchone()
        if row:
            return dict(row), True
        current = connection.execute(
            "SELECT * FROM procurement.procurement_case WHERE case_id = %(case_id)s",
            {"case_id": case_id},
        ).fetchone()
    if current is None:
        raise LookupError(case_id)
    return dict(current), False


def record_automation_scan(
    result: dict[str, Any],
    *,
    interval_seconds: int | None = None,
    ran_by: str | None = None,
) -> None:
    """스캔이 돌았다는 사실을 한 줄로 남긴다(서버 로그를 못 보는 사람용)."""

    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO procurement.automation_heartbeat
                (singleton, last_run_at, interval_seconds, ran_by, result)
            VALUES (true, now(), %(interval)s, %(ran_by)s, %(result)s)
            ON CONFLICT (singleton) DO UPDATE
            SET last_run_at = now(),
                interval_seconds = EXCLUDED.interval_seconds,
                ran_by = EXCLUDED.ran_by,
                result = EXCLUDED.result
            """,
            {
                "interval": interval_seconds,
                "ran_by": ran_by,
                "result": Jsonb(_json_value(result)),
            },
        )


def get_automation_heartbeat() -> dict[str, Any] | None:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM procurement.automation_heartbeat WHERE singleton = true"
        ).fetchone()
    return dict(row) if row else None


def set_automation_hold(
    case_id: str,
    *,
    hold: bool,
    reason: str | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """자동 진행 보류를 걸거나 푼다. version은 올리지 않는다."""

    with get_connection() as connection:
        row = connection.execute(
            """
            UPDATE procurement.procurement_case
            SET automation_hold = %(hold)s,
                automation_hold_reason = CASE WHEN %(hold)s THEN %(reason)s ELSE NULL END,
                automation_hold_by = CASE WHEN %(hold)s THEN %(actor)s ELSE NULL END,
                automation_hold_at = CASE WHEN %(hold)s THEN now() ELSE NULL END
            WHERE case_id = %(case_id)s
            RETURNING *
            """,
            {"case_id": case_id, "hold": hold, "reason": reason, "actor": actor},
        ).fetchone()
    if row is None:
        raise LookupError(case_id)
    return dict(row)


def mark_auto_progress_attempt(case_id: str, signature: str | None) -> None:
    """자동 진행을 시도한 상황의 지문을 남긴다(같은 상황 반복 평가 방지)."""

    with get_connection() as connection:
        connection.execute(
            """
            UPDATE procurement.procurement_case
            SET auto_progress_signature = %(signature)s,
                auto_progress_at = now()
            WHERE case_id = %(case_id)s
            """,
            {"case_id": case_id, "signature": signature},
        )


def mark_auto_deadline_extended(case_id: str) -> None:
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE procurement.procurement_case
            SET auto_deadline_extended_at = now()
            WHERE case_id = %(case_id)s
            """,
            {"case_id": case_id},
        )


def save_live_quotation_ranking(case_id: str, payload: dict[str, Any]) -> bool:
    """실시간 견적 순위를 저장한다. version은 올리지 않는다(그래프 쓰기와 충돌 방지)."""

    with get_connection() as connection:
        row = connection.execute(
            """
            UPDATE procurement.procurement_case
            SET live_quotation_ranking = %(payload)s,
                live_quotation_ranking_at = now()
            WHERE case_id = %(case_id)s
            RETURNING case_id
            """,
            {"case_id": case_id, "payload": Jsonb(_json_value(payload))},
        ).fetchone()
    return row is not None


def list_cases(
    *,
    status: str | None = None,
    stage: str | None = None,
    assigned_user_id: str | None = None,
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
    if assigned_user_id:
        conditions.append(
            "LOWER(pc.assigned_user_id) = LOWER(%(assigned_user_id)s)"
        )
        params["assigned_user_id"] = assigned_user_id
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


# 견적 마감일 연장 이력용 표식. procurement_case.quotation_deadline_at은
# 연장할 때마다 덮어써서 "언제 언제로 바꿨는지"가 남지 않으므로, 이미
# 있는 workflow_status_history 테이블에 이 to_status로 한 줄씩 남긴다
# (reason에 "<이전 마감일> -> <새 마감일>" ISO 문자열). 별도 테이블/
# 마이그레이션 없이 이력을 보관하려는 의도이며, 워크플로 상태 전이와
# 섞이지 않도록 to_status 값으로 구분한다.
QUOTATION_DEADLINE_EXTENDED_STATUS = "QUOTATION_DEADLINE_EXTENDED"
_DEADLINE_HISTORY_SEPARATOR = " -> "


def log_quotation_deadline_change(
    case_id: str,
    *,
    previous_deadline: datetime | str | None,
    new_deadline: datetime | str,
    stage: str | None,
    changed_by: str | None,
) -> None:
    """마감일 연장 한 건을 이력으로 남긴다."""

    def _iso(value: datetime | str | None) -> str:
        if value is None:
            return ""
        if isinstance(value, datetime):
            return value.isoformat()
        return str(value)

    reason = f"{_iso(previous_deadline)}{_DEADLINE_HISTORY_SEPARATOR}{_iso(new_deadline)}"
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO procurement.workflow_status_history (
                case_id, from_status, to_status, stage, reason, triggered_by
            ) VALUES (
                %(case_id)s, NULL, %(to_status)s, %(stage)s, %(reason)s, %(triggered_by)s
            )
            """,
            {
                "case_id": case_id,
                "to_status": QUOTATION_DEADLINE_EXTENDED_STATUS,
                "stage": stage,
                "reason": reason,
                "triggered_by": changed_by,
            },
        )


def list_quotation_deadline_changes(case_id: str) -> list[dict[str, Any]]:
    """마감일 연장 이력을 오래된 순으로 돌려준다."""
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT reason, triggered_by, changed_at
            FROM procurement.workflow_status_history
            WHERE case_id = %(case_id)s AND to_status = %(to_status)s
            ORDER BY changed_at ASC
            """,
            {"case_id": case_id, "to_status": QUOTATION_DEADLINE_EXTENDED_STATUS},
        ).fetchall()

    items: list[dict[str, Any]] = []
    for row in rows:
        reason = str(dict(row).get("reason") or "")
        previous_at, _, new_at = reason.partition(_DEADLINE_HISTORY_SEPARATOR)
        items.append(
            {
                "previous_deadline_at": previous_at.strip() or None,
                "deadline_at": new_at.strip() or None,
                "changed_by": dict(row).get("triggered_by"),
                "changed_at": dict(row).get("changed_at"),
            }
        )
    return items


def update_quotation_deadline(case_id: str, deadline_at: datetime | str | None) -> dict[str, Any]:
    """deadline_at=None이면 마감일을 지운다(NULL) - 재비딩처럼 기존 RFQ/견적을
    버리고 새로 마감일을 정할 때까지 옛 마감일이 남아있지 않도록 하기 위함."""
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
