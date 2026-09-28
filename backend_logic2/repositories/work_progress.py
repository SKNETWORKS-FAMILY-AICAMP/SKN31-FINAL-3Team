"""Small, read-only dashboard projection. Never call ERP, models or the graph."""
from __future__ import annotations

import logging
from procurement_db import get_connection

LOGGER = logging.getLogger(__name__)


def notify_progress(connection, case_id: str) -> None:
    """Reuse the caller's connection; commit delivers the signal, rollback drops it.

    No notification-inbox write or advisory lock is involved. A savepoint keeps
    optional UI telemetry failures from rolling back the business transaction.
    Only an invalidation signal is sent, never AI reasoning or document content.
    """
    try:
        with connection.transaction():
            connection.execute("""
                SELECT pg_notify('biddingflow_notifications', json_build_object(
                    'event_type', 'progress', 'recipient_id', assigned_user_id
                )::text)
                FROM procurement.procurement_case WHERE case_id=%s
            """, (str(case_id),))
    except Exception:
        LOGGER.warning('Progress signal skipped', exc_info=True)


def list_progress(actor: str, *, admin: bool = False) -> dict:
    """One connection/query, bounded output and statement time; no graph lock.

    Deadline reasons belong only to the current RFQ/selection stage. Old jobs
    must not make a case that has reached PO look blocked again.
    """
    with get_connection() as conn:
        conn.execute("SET LOCAL statement_timeout = '2000ms'")
        rows = conn.execute("""
            SELECT c.case_id, c.mr_name, c.status, c.stage, c.version, c.updated_at,
                   j.status AS deadline_status, j.reason AS waiting_reason,
                   j.updated_at AS checked_at, j.metrics,
                   h.to_status AS last_step, h.occurred_at AS last_step_at
            FROM procurement.procurement_case c
            LEFT JOIN procurement.quotation_deadline_job j ON j.case_id=c.case_id
                AND j.rfq_name=c.workflow_snapshot #>> '{values,rfq_name}'
                AND c.stage IN ('QUOTATION_COLLECTION','SUPPLIER_SELECTION')
            LEFT JOIN LATERAL (
                SELECT to_status, occurred_at FROM procurement.case_status_history
                WHERE case_id=c.case_id ORDER BY occurred_at DESC, id DESC LIMIT 1
            ) h ON true
            WHERE (%(admin)s OR lower(trim(c.assigned_user_id))=%(actor)s)
              AND c.mr_name NOT LIKE '%%#archived-%%'
              AND c.status NOT IN ('COMPLETED','CANCELLED','REJECTED')
            ORDER BY c.updated_at DESC, c.case_id LIMIT 201
        """, {'admin': admin, 'actor': actor.strip().lower()}).fetchall()
    return {'items': [dict(row) for row in rows[:200]], 'truncated': len(rows) > 200}
