"""Notification inbox persistence shared by webhook and future SSE delivery."""

from __future__ import annotations

import json
from typing import Any

from psycopg.types.json import Jsonb

from procurement_db import get_connection


def create_notification(
    *,
    case_id: str | None,
    recipient_id: str | None,
    notification_type: str,
    title: str,
    message: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with get_connection() as connection:
        row = connection.execute(
            """
            INSERT INTO procurement.notification (
                case_id, recipient_id, notification_type, title, message, payload
            ) VALUES (
                %(case_id)s, %(recipient_id)s, %(notification_type)s,
                %(title)s, %(message)s, %(payload)s
            )
            RETURNING *
            """,
            {
                "case_id": case_id,
                "recipient_id": recipient_id,
                "notification_type": notification_type,
                "title": title,
                "message": message,
                "payload": Jsonb(payload or {}),
            },
        ).fetchone()
        connection.execute(
            "SELECT pg_notify('biddingflow_notifications', %(payload)s)",
            {
                "payload": json.dumps(
                    {
                        "notification_id": str(row["notification_id"]),
                        "case_id": str(row["case_id"]) if row.get("case_id") else None,
                        "recipient_id": row.get("recipient_id"),
                        "notification_type": row["notification_type"],
                        "title": row["title"],
                        "message": row["message"],
                    },
                    ensure_ascii=False,
                )
            },
        )
    return dict(row)


def list_notifications(recipient_id: str, *, unread_only: bool = False, limit: int = 50):
    conditions = ["(recipient_id = %(recipient_id)s OR recipient_id IS NULL)"]
    if unread_only:
        conditions.append("is_read = false")
    with get_connection() as connection:
        rows = connection.execute(
            f"""
            SELECT * FROM procurement.notification
            WHERE {' AND '.join(conditions)}
            ORDER BY created_at DESC
            LIMIT %(limit)s
            """,
            {"recipient_id": recipient_id, "limit": min(max(limit, 1), 100)},
        ).fetchall()
    return [dict(row) for row in rows]


def mark_notification_read(notification_id: str, recipient_id: str) -> bool:
    with get_connection() as connection:
        row = connection.execute(
            """
            UPDATE procurement.notification
            SET is_read = true, read_at = COALESCE(read_at, now())
            WHERE notification_id = %(notification_id)s
              AND (recipient_id = %(recipient_id)s OR recipient_id IS NULL)
            RETURNING notification_id
            """,
            {"notification_id": notification_id, "recipient_id": recipient_id},
        ).fetchone()
    return row is not None


def delete_notification(notification_id: str, recipient_id: str) -> bool:
    """Remove one notification that is visible to the current user.

    ``recipient_id IS NULL`` is the existing broadcast-notification convention,
    so those rows follow the same visibility rule as list/mark-read operations.
    """

    with get_connection() as connection:
        row = connection.execute(
            """
            DELETE FROM procurement.notification
            WHERE notification_id = %(notification_id)s
              AND (recipient_id = %(recipient_id)s OR recipient_id IS NULL)
            RETURNING notification_id
            """,
            {"notification_id": notification_id, "recipient_id": recipient_id},
        ).fetchone()
    return row is not None


def delete_all_notifications(recipient_id: str) -> int:
    """Remove every notification currently visible to the current user."""

    with get_connection() as connection:
        cursor = connection.execute(
            """
            DELETE FROM procurement.notification
            WHERE recipient_id = %(recipient_id)s OR recipient_id IS NULL
            """,
            {"recipient_id": recipient_id},
        )
    return cursor.rowcount


def delete_case_notifications(case_id: str) -> int:
    """Remove stale inbox rows belonging to one procurement case.

    A successful workflow action supersedes every earlier notice for the same
    MR. Filtering by ``case_id`` also covers PO/receipt notifications whose
    visible reference is a PO number rather than the original MR number.
    """

    with get_connection() as connection:
        cursor = connection.execute(
            """
            DELETE FROM procurement.notification
            WHERE case_id = %(case_id)s
            """,
            {"case_id": case_id},
        )
    return cursor.rowcount
