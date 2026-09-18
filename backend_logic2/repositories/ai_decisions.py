"""Read-only access to AI decisions and quotation specification assessments."""

from __future__ import annotations

from typing import Any

from procurement_db import get_connection


_COMBINED_DECISIONS_SQL = """
    SELECT
        'decision:' || log.id::text AS id,
        'ai_decision_log'::text AS source,
        log.case_id,
        pc.mr_name,
        pc.workflow_snapshot #>> '{values,rfq_name}' AS rfq_name,
        NULL::text AS quotation_id,
        log.node,
        NULL::double precision AS score,
        NULL::text AS evaluation_source,
        log.reason,
        log.created_at
    FROM procurement.ai_decision_log log
    LEFT JOIN procurement.procurement_case pc ON pc.case_id = log.case_id
    WHERE log.node IS DISTINCT FROM 'item_spec_completeness_check'

    UNION ALL

    SELECT
        'quotation:' || md5(
            cache.rfq_name || ':' || cache.quotation_id || ':' ||
            cache.input_hash || ':' || cache.evaluation_source
        ) AS id,
        'quotation_specification_cache'::text AS source,
        cache.case_id,
        pc.mr_name,
        cache.rfq_name,
        cache.quotation_id,
        'quotation_specification_evaluation'::text AS node,
        CASE
            WHEN jsonb_typeof(cache.assessment_json->'score') = 'number'
            THEN (cache.assessment_json->>'score')::double precision
            ELSE NULL
        END AS score,
        cache.evaluation_source,
        cache.assessment_json->>'reason' AS reason,
        cache.updated_at AS created_at
    FROM procurement.quotation_specification_cache cache
    LEFT JOIN procurement.procurement_case pc ON pc.case_id = cache.case_id
"""


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
        conditions.append("combined.node = %(node)s")
        params["node"] = node
    if case_id:
        conditions.append("combined.case_id = %(case_id)s::uuid")
        params["case_id"] = case_id
    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    with get_connection() as connection:
        total = connection.execute(
            f"""
            WITH combined AS ({_COMBINED_DECISIONS_SQL})
            SELECT count(1) AS count FROM combined {where}
            """,
            params,
        ).fetchone()["count"]
        rows = connection.execute(
            f"""
            WITH combined AS ({_COMBINED_DECISIONS_SQL})
            SELECT id, source, case_id, mr_name, rfq_name, quotation_id,
                   node, score, evaluation_source, reason, created_at
            FROM combined
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
            f"""
            WITH combined AS ({_COMBINED_DECISIONS_SQL})
            SELECT DISTINCT node
            FROM combined
            WHERE node IS NOT NULL AND node <> ''
            ORDER BY node
            """
        ).fetchall()
    return [str(row["node"]) for row in rows]
