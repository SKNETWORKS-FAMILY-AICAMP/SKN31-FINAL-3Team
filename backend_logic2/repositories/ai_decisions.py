"""Read-only access to AI decision audit records."""

from __future__ import annotations

from typing import Any

from procurement_db import get_connection


def list_decisions(
    *,
    node: str | None = None,
    case_id: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    conditions: list[str] = []
    params: dict[str, Any] = {
        "limit": min(max(limit, 1), 200),
        "offset": max(offset, 0),
    }
    if node:
        conditions.append("node = %(node)s")
        params["node"] = node
    if case_id:
        conditions.append("case_id = %(case_id)s::uuid")
        params["case_id"] = case_id
    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    with get_connection() as connection:
        total = connection.execute(
            f"SELECT count(1) AS count FROM procurement.ai_decision_log {where}",
            params,
        ).fetchone()["count"]
        rows = connection.execute(
            f"""
            SELECT id, case_id, node, reason, created_at
            FROM procurement.ai_decision_log
            {where}
            ORDER BY created_at DESC, id DESC
            LIMIT %(limit)s OFFSET %(offset)s
            """,
            params,
        ).fetchall()
    return [dict(row) for row in rows], int(total)


def list_nodes() -> list[str]:
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT DISTINCT node
            FROM procurement.ai_decision_log
            WHERE node IS NOT NULL AND node <> ''
            ORDER BY node
            """
        ).fetchall()
    return [str(row["node"]) for row in rows]
