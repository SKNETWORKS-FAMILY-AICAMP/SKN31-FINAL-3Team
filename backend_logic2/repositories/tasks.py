"""Persistence for resumable human-in-the-loop tasks."""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from procurement_db import get_connection


def replace_pending_task(
    *,
    case_id: str,
    task_type: str,
    audience: str,
    channel: str,
    title: str,
    description: str | None,
    payload: dict[str, Any],
    input_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Keep at most one pending task for the same case/type/audience."""

    with get_connection() as connection:
        connection.execute(
            """
            UPDATE procurement.human_task
            SET status = 'SUPERSEDED', updated_at = now()
            WHERE case_id = %(case_id)s
              AND task_type = %(task_type)s
              AND audience = %(audience)s
              AND status = 'PENDING'
            """,
            {"case_id": case_id, "task_type": task_type, "audience": audience},
        )
        row = connection.execute(
            """
            INSERT INTO procurement.human_task (
                case_id, task_type, channel, audience, title, description,
                input_schema, payload
            ) VALUES (
                %(case_id)s, %(task_type)s, %(channel)s, %(audience)s,
                %(title)s, %(description)s, %(input_schema)s, %(payload)s
            )
            RETURNING *
            """,
            {
                "case_id": case_id,
                "task_type": task_type,
                "channel": channel,
                "audience": audience,
                "title": title,
                "description": description,
                "input_schema": Jsonb(input_schema or {}),
                "payload": Jsonb(payload),
            },
        ).fetchone()
    return dict(row)


def get_task(task_id: str) -> dict[str, Any] | None:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM procurement.human_task WHERE task_id = %(task_id)s",
            {"task_id": task_id},
        ).fetchone()
    return dict(row) if row else None


def list_tasks(
    *,
    case_id: str | None = None,
    audience: str | None = None,
    status: str = "PENDING",
) -> list[dict[str, Any]]:
    conditions = ["status = %(status)s"]
    params: dict[str, Any] = {"status": status}
    if case_id:
        conditions.append("case_id = %(case_id)s")
        params["case_id"] = case_id
    if audience:
        conditions.append("audience = %(audience)s")
        params["audience"] = audience
    with get_connection() as connection:
        rows = connection.execute(
            f"""
            SELECT * FROM procurement.human_task
            WHERE {' AND '.join(conditions)}
            ORDER BY created_at DESC
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows]


def supersede_inactive_tasks(case_id: str, active_task_types: set[str]) -> int:
    """Close pending tasks that are no longer present in the graph snapshot."""
    with get_connection() as connection:
        if active_task_types:
            cursor = connection.execute(
                """
                UPDATE procurement.human_task
                SET status = 'SUPERSEDED', updated_at = now()
                WHERE case_id = %(case_id)s
                  AND status = 'PENDING'
                  AND NOT (task_type = ANY(%(active_task_types)s::varchar[]))
                """,
                {"case_id": case_id, "active_task_types": list(active_task_types)},
            )
        else:
            cursor = connection.execute(
                """
                UPDATE procurement.human_task
                SET status = 'SUPERSEDED', updated_at = now()
                WHERE case_id = %(case_id)s AND status = 'PENDING'
                """,
                {"case_id": case_id},
            )
    return cursor.rowcount


def cancel_pending_tasks(case_id: str, *, reason: str) -> int:
    with get_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE procurement.human_task
            SET status = 'CANCELLED', answer = %(answer)s,
                answered_at = now(), updated_at = now(), version = version + 1
            WHERE case_id = %(case_id)s AND status IN ('PENDING', 'PROCESSING')
            """,
            {"case_id": case_id, "answer": Jsonb({"reason": reason})},
        )
    return cursor.rowcount


def cancel_pending_tasks_by_type(
    case_id: str, *, task_type: str, reason: str
) -> int:
    """Cancel only one invalidated interrupt while preserving unrelated work."""

    with get_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE procurement.human_task
            SET status = 'CANCELLED', answer = %(answer)s,
                answered_at = now(), updated_at = now(), version = version + 1
            WHERE case_id = %(case_id)s
              AND task_type = %(task_type)s
              AND status IN ('PENDING', 'PROCESSING')
            """,
            {
                "case_id": case_id,
                "task_type": task_type,
                "answer": Jsonb({"reason": reason}),
            },
        )
    return cursor.rowcount


def complete_task(
    task_id: str,
    *,
    answer: dict[str, Any],
    answered_by: str,
    expected_version: int | None = None,
) -> dict[str, Any]:
    params = {
        "task_id": task_id,
        "answer": Jsonb(answer),
        "answered_by": answered_by,
        "expected_version": expected_version,
    }
    with get_connection() as connection:
        row = connection.execute(
            """
            UPDATE procurement.human_task
            SET status = 'COMPLETED', answer = %(answer)s,
                answered_by = %(answered_by)s, answered_at = now(),
                updated_at = now(), version = version + 1
            WHERE task_id = %(task_id)s
              AND status = 'PENDING'
              -- psycopg가 None만 받은 placeholder의 PostgreSQL 타입을
              -- 추론하지 못하면 AmbiguousParameter가 발생하므로 명시한다.
              AND (
                  %(expected_version)s::integer IS NULL
                  OR version = %(expected_version)s::integer
              )
            RETURNING *
            """,
            params,
        ).fetchone()
    if row is None:
        raise RuntimeError("이미 처리되었거나 최신 버전이 아닌 작업입니다.")
    return dict(row)


def claim_task(
    task_id: str,
    *,
    answer: dict[str, Any],
    answered_by: str,
    expected_version: int,
) -> dict[str, Any]:
    """Atomically reserve one pending task before advancing LangGraph.

    The old flow advanced LangGraph first and only then changed the task row.
    Two simultaneous requests could therefore resume two adjacent interrupts
    with the same answer.  A PENDING -> PROCESSING compare-and-swap makes the
    database the cross-process concurrency gate.
    """

    with get_connection() as connection:
        row = connection.execute(
            """
            UPDATE procurement.human_task
            SET status = 'PROCESSING', answer = %(answer)s,
                answered_by = %(answered_by)s, updated_at = now(),
                version = version + 1
            WHERE task_id = %(task_id)s
              AND status = 'PENDING'
              AND version = %(expected_version)s
            RETURNING *
            """,
            {
                "task_id": task_id,
                "answer": Jsonb(answer),
                "answered_by": answered_by,
                "expected_version": expected_version,
            },
        ).fetchone()
    if row is None:
        raise RuntimeError(
            "이미 처리 중이거나 최신 버전이 아닌 작업입니다. 목록을 새로고침해 주세요."
        )
    return dict(row)


def complete_claimed_task(task_id: str, *, claimed_version: int) -> dict[str, Any]:
    """Finish the exact task claim that advanced the workflow."""

    with get_connection() as connection:
        row = connection.execute(
            """
            UPDATE procurement.human_task
            SET status = 'COMPLETED', answered_at = now(), updated_at = now(),
                version = version + 1
            WHERE task_id = %(task_id)s
              AND status = 'PROCESSING'
              AND version = %(claimed_version)s
            RETURNING *
            """,
            {"task_id": task_id, "claimed_version": claimed_version},
        ).fetchone()
    if row is None:
        raise RuntimeError("처리 중인 작업의 완료 상태를 확정하지 못했습니다.")
    return dict(row)


def release_claimed_task(task_id: str, *, claimed_version: int) -> bool:
    """Return an unconsumed claim to PENDING after a graph/domain failure.

    Its version is increased again, so an already-open browser must reload
    before retrying and cannot replay the stale answer.
    """

    with get_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE procurement.human_task
            SET status = 'PENDING', answer = NULL, answered_by = NULL,
                updated_at = now(), version = version + 1
            WHERE task_id = %(task_id)s
              AND status = 'PROCESSING'
              AND version = %(claimed_version)s
            """,
            {"task_id": task_id, "claimed_version": claimed_version},
        )
    return cursor.rowcount == 1
