"""Database-backed Item Group to BiddingFlow user assignments."""

from __future__ import annotations

from typing import Any

from procurement_db import get_connection


class AssignmentConflict(RuntimeError):
    """Raised when an administrator edits a stale assignment revision."""


def get_assignments() -> tuple[int, list[dict[str, Any]]]:
    with get_connection() as connection:
        revision = connection.execute(
            "SELECT revision FROM procurement.item_group_assignment_head WHERE singleton = true"
        ).fetchone()["revision"]
        rows = connection.execute(
            """SELECT item_group, manager_user_id, updated_by, updated_at
               FROM procurement.item_group_assignment
               ORDER BY item_group"""
        ).fetchall()
    return revision, [dict(row) for row in rows]


def get_manager(item_group: str | None) -> str | None:
    group = str(item_group or "").strip()
    if not group:
        return None
    with get_connection() as connection:
        row = connection.execute(
            """SELECT manager_user_id
               FROM procurement.item_group_assignment
               WHERE item_group = %s""",
            (group,),
        ).fetchone()
    return str(row["manager_user_id"]) if row else None


def replace_assignments(
    assignments: list[dict[str, str]], *, expected_revision: int, actor: str
) -> tuple[int, list[dict[str, Any]]]:
    """Replace mappings and immediately reassign existing cases and notices."""
    with get_connection() as connection:
        head = connection.execute(
            """SELECT revision FROM procurement.item_group_assignment_head
               WHERE singleton = true FOR UPDATE"""
        ).fetchone()
        if head["revision"] != expected_revision:
            raise AssignmentConflict("담당자 설정이 먼저 변경되었습니다. 최신 설정을 다시 불러오세요.")

        connection.execute("DELETE FROM procurement.item_group_assignment")
        with connection.cursor() as cursor:
            cursor.executemany(
                """INSERT INTO procurement.item_group_assignment
                   (item_group, manager_user_id, updated_by)
                   VALUES (%s, %s, %s)""",
                [(row["item_group"], row["manager_user_id"], actor) for row in assignments],
            )

        connection.execute(
            """UPDATE procurement.procurement_case AS pc
               SET assigned_user_id = (
                   SELECT iga.manager_user_id
                   FROM procurement.item_group_assignment AS iga
                   WHERE iga.item_group = pc.summary->>'item_group'
               )
               WHERE pc.assigned_user_id IS DISTINCT FROM (
                   SELECT iga.manager_user_id
                   FROM procurement.item_group_assignment AS iga
                   WHERE iga.item_group = pc.summary->>'item_group'
               )"""
        )
        connection.execute(
            """UPDATE procurement.notification AS n
               SET recipient_id = pc.assigned_user_id
               FROM procurement.procurement_case AS pc
               WHERE n.case_id = pc.case_id
                 AND n.recipient_id IS NOT NULL
                 AND pc.assigned_user_id IS NOT NULL
                 AND n.recipient_id IS DISTINCT FROM pc.assigned_user_id"""
        )
        connection.execute(
            """DELETE FROM procurement.notification AS n
               USING procurement.procurement_case AS pc
               WHERE n.case_id = pc.case_id
                 AND n.recipient_id IS NOT NULL
                 AND pc.assigned_user_id IS NULL"""
        )
        revision = expected_revision + 1
        connection.execute(
            """UPDATE procurement.item_group_assignment_head
               SET revision = %s WHERE singleton = true""",
            (revision,),
        )
        rows = connection.execute(
            """SELECT item_group, manager_user_id, updated_by, updated_at
               FROM procurement.item_group_assignment ORDER BY item_group"""
        ).fetchall()
    return revision, [dict(row) for row in rows]