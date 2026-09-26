"""한 건의 전체 이력 - 자동이든 수동이든 무슨 일이 언제 있었는지.

자동 진행이 기본이 되면 이 타임라인이 사실상 감사 기록이 된다. 그래서
단계 전환(workflow_status_history), 사람이 답한 작업(human_task), AI 판단
(ai_decision_log)을 한 줄기로 합쳐서 돌려준다.
"""

from __future__ import annotations

from typing import Any

from procurement_db import get_connection


_TIMELINE_SQL = """
    SELECT 'status'::text AS kind,
           h.changed_at AS occurred_at,
           h.to_status AS title,
           h.stage AS stage,
           h.reason AS detail,
           h.triggered_by AS actor
    FROM procurement.workflow_status_history h
    WHERE h.case_id = %(case_id)s

    UNION ALL

    SELECT 'human_task'::text AS kind,
           t.answered_at AS occurred_at,
           t.title AS title,
           t.task_type AS stage,
           NULL::text AS detail,
           t.answered_by AS actor
    FROM procurement.human_task t
    WHERE t.case_id = %(case_id)s AND t.answered_at IS NOT NULL

    UNION ALL

    SELECT 'ai_decision'::text AS kind,
           d.created_at AS occurred_at,
           d.node AS title,
           NULL::text AS stage,
           d.reason AS detail,
           'ai'::text AS actor
    FROM procurement.ai_decision_log d
    WHERE d.case_id = %(case_id)s

    ORDER BY occurred_at ASC
    LIMIT %(limit)s
"""


def list_case_timeline(case_id: str, *, limit: int = 300) -> list[dict[str, Any]]:
    with get_connection() as connection:
        rows = connection.execute(
            _TIMELINE_SQL, {"case_id": str(case_id), "limit": limit}
        ).fetchall()
    return [dict(row) for row in rows]
