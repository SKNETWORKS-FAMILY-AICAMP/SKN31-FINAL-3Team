"""Idempotent integration-event inbox."""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from procurement_db import get_connection


def begin_event(
    *, source: str, event_type: str, dedupe_key: str, payload: dict[str, Any], external_id: str | None = None
) -> tuple[dict[str, Any], bool]:
    """Claim a new or previously failed event and return ``(row, claimed)``.

    A projection can fail after persisting part of its read model. Treating the
    dedupe key as permanently consumed in that case prevents reconciliation
    from completing the missing task or notification. Only processed and
    currently in-flight events remain deduplicated.
    """

    with get_connection() as connection:
        row = connection.execute(
            """
            INSERT INTO procurement.integration_event (
                source, event_type, external_id, dedupe_key, payload
            ) VALUES (
                %(source)s, %(event_type)s, %(external_id)s,
                %(dedupe_key)s, %(payload)s
            )
            ON CONFLICT (dedupe_key) DO NOTHING
            RETURNING *
            """,
            {
                "source": source,
                "event_type": event_type,
                "external_id": external_id,
                "dedupe_key": dedupe_key,
                "payload": Jsonb(payload),
            },
        ).fetchone()
        if row:
            return dict(row), True
        retried = connection.execute(
            """
            UPDATE procurement.integration_event
            SET status = 'RECEIVED', payload = %(payload)s,
                last_error = NULL, processed_at = NULL, updated_at = now()
            WHERE dedupe_key = %(dedupe_key)s AND status = 'FAILED'
            RETURNING *
            """,
            {"dedupe_key": dedupe_key, "payload": Jsonb(payload)},
        ).fetchone()
        if retried:
            return dict(retried), True
        existing = connection.execute(
            "SELECT * FROM procurement.integration_event WHERE dedupe_key = %(dedupe_key)s",
            {"dedupe_key": dedupe_key},
        ).fetchone()
    return dict(existing), False


def complete_event(event_id: str) -> None:
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE procurement.integration_event
            SET status = 'PROCESSED', processed_at = now(), last_error = NULL
            WHERE event_id = %(event_id)s
            """,
            {"event_id": event_id},
        )


def fail_event(event_id: str, error: str) -> None:
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE procurement.integration_event
            SET status = 'FAILED', retry_count = retry_count + 1,
                last_error = %(error)s
            WHERE event_id = %(event_id)s
            """,
            {"event_id": event_id, "error": error[:4000]},
        )
