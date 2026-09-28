"""Small DB transactions; never hold a connection during ERP/model/graph work."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4
from contextlib import contextmanager

from psycopg.types.json import Jsonb
from procurement_db import get_connection as _get_connection


@contextmanager
def get_connection():
    # A coordinator query must not turn into another long background scan.
    with _get_connection() as conn:
        conn.execute("SET LOCAL statement_timeout = '3000ms'")
        conn.execute("SET LOCAL lock_timeout = '500ms'")
        yield conn


def bump_rfqs(rfq_names: list[str]) -> None:
    """An old-round SQ edit must invalidate even when current-round counters do not change."""
    if not rfq_names:
        return
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO procurement.quotation_deadline_revision(rfq_name)
            SELECT DISTINCT unnest(%s::text[])
            ON CONFLICT(rfq_name) DO UPDATE SET revision=quotation_deadline_revision.revision+1
        """, (rfq_names,))


def control() -> dict[str, Any]:
    with get_connection() as conn:
        return dict(conn.execute("SELECT * FROM procurement.quotation_deadline_control WHERE singleton").fetchone())


def set_enabled(enabled: bool, *, actor: str, reason: str) -> dict[str, Any]:
    with get_connection() as conn:
        return dict(conn.execute("""
            UPDATE procurement.quotation_deadline_control SET enabled=%s,
                changed_by=%s, reason=%s, updated_at=now() WHERE singleton RETURNING *
        """, (enabled, actor, reason)).fetchone())


def changed_inputs(limit: int = 50) -> list[dict[str, Any]]:
    """One DB query; unchanged DONE/WAITING jobs are filtered BEFORE the limit."""
    with get_connection() as conn:
        return list(conn.execute("""
            SELECT i.* FROM procurement.quotation_deadline_input i
            LEFT JOIN procurement.quotation_deadline_job j USING(case_id)
            WHERE (j.case_id IS NULL OR i.input_hash <> j.input_hash)
              AND (j.status IS NULL OR j.status NOT IN ('CHECKING','READY','RUNNING','UNCERTAIN'))
            ORDER BY i.case_id LIMIT %s
        """, (limit,)).fetchall())


def input_for(case_id: str) -> dict[str, Any] | None:
    with get_connection() as conn:
        return conn.execute("SELECT * FROM procurement.quotation_deadline_input WHERE case_id=%s", (case_id,)).fetchone()


def schedule(row: dict[str, Any], due_at: datetime) -> None:
    schedule_many([(row, due_at)])


def schedule_many(entries: list[tuple[dict[str, Any], datetime]]) -> None:
    """One connection/statement even when a startup discovers 50 changed cases."""
    if not entries:
        return
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO procurement.quotation_deadline_job(case_id,input_hash,rfq_name,due_at,next_attempt_at)
            SELECT case_id,input_hash,rfq_name,due_at,due_at FROM UNNEST(
                %s::uuid[],%s::text[],%s::text[],%s::timestamptz[]
            ) AS batch(case_id,input_hash,rfq_name,due_at)
            ON CONFLICT(case_id) DO UPDATE SET input_hash=EXCLUDED.input_hash,
                rfq_name=EXCLUDED.rfq_name,due_at=EXCLUDED.due_at,next_attempt_at=EXCLUDED.next_attempt_at,
                status='PENDING',attempts=0,claim_token=NULL,lease_until=NULL,
                prepared=NULL,reason=NULL,metrics='{}'::jsonb,updated_at=now()
            WHERE quotation_deadline_job.status NOT IN ('CHECKING','READY','RUNNING','UNCERTAIN')
              AND quotation_deadline_job.input_hash <> EXCLUDED.input_hash
        """, ([str(row['case_id']) for row, _ in entries],
              [row['input_hash'] for row, _ in entries],
              [row['rfq_name'] for row, _ in entries], [due for _, due in entries]))


def recover_expired() -> None:
    """Preparation is read-only/retryable. A crashed graph may have sent mail: never replay it automatically."""
    with get_connection() as conn:
        conn.execute("""
            UPDATE procurement.quotation_deadline_job SET
                status=CASE WHEN status='RUNNING' THEN 'UNCERTAIN'
                    WHEN attempts < 3 THEN 'PENDING' ELSE 'BLOCKED' END,
                reason=CASE WHEN status='RUNNING' THEN '실행 중 연결이 끊겼습니다. 실제 진행 상태를 확인 후 수동 처리하세요.'
                    ELSE '준비 작업 임대 만료' END,
                claim_token=NULL,lease_until=NULL,next_attempt_at=now(),updated_at=now()
            WHERE status IN ('CHECKING','RUNNING') AND lease_until < now()
        """)


def claim() -> dict[str, Any] | None:
    token = str(uuid4())
    with get_connection() as conn:
        return conn.execute("""
            WITH candidate AS (
                SELECT case_id FROM procurement.quotation_deadline_job
                WHERE status='PENDING' AND next_attempt_at<=now()
                  AND (SELECT enabled FROM procurement.quotation_deadline_control WHERE singleton)
                ORDER BY next_attempt_at,case_id FOR UPDATE SKIP LOCKED LIMIT 1
            ) UPDATE procurement.quotation_deadline_job j
            SET status='CHECKING',claim_token=%s,lease_until=now()+interval '5 minutes',
                attempts=attempts+1,updated_at=now()
            FROM candidate c WHERE j.case_id=c.case_id RETURNING j.*
        """, (token,)).fetchone()


def finish(job: dict[str, Any], status: str, reason: str, *, prepared=None, metrics=None,
           retry_seconds: int = 0) -> bool:
    # Token CAS prevents an expired worker from overwriting a newer claim.
    with get_connection() as conn:
        result = conn.execute("""
            UPDATE procurement.quotation_deadline_job SET status=%s,reason=%s,
                prepared=%s,metrics=COALESCE(%s::jsonb,metrics),lease_until=NULL,
                next_attempt_at=now()+(%s * interval '1 second'),updated_at=now()
            WHERE case_id=%s AND claim_token=%s AND status IN ('CHECKING','READY','RUNNING')
        """, (status, reason, Jsonb(prepared) if prepared is not None else None,
              Jsonb(metrics) if metrics is not None else None,
              retry_seconds, str(job['case_id']), job['claim_token']))
        return result.rowcount == 1


def ready_job() -> dict[str, Any] | None:
    with get_connection() as conn:
        return conn.execute("""
            SELECT * FROM procurement.quotation_deadline_job
            WHERE status='READY' ORDER BY updated_at,case_id LIMIT 1
        """).fetchone()


def start(job: dict[str, Any]) -> bool:
    with get_connection() as conn:
        return conn.execute("""
            UPDATE procurement.quotation_deadline_job SET status='RUNNING',
                lease_until=now()+interval '30 minutes',updated_at=now()
            WHERE case_id=%s AND claim_token=%s AND status='READY'
              AND (SELECT enabled FROM procurement.quotation_deadline_control WHERE singleton)
              AND EXISTS (SELECT 1 FROM procurement.quotation_deadline_input i
                  WHERE i.case_id=quotation_deadline_job.case_id
                    AND i.input_hash=quotation_deadline_job.input_hash
                    AND i.unfinished_extractions=0)
        """, (str(job['case_id']), job['claim_token'])).rowcount == 1


def status(limit: int = 50) -> dict[str, Any]:
    with get_connection() as conn:
        counts = conn.execute("SELECT status,count(*) AS count FROM procurement.quotation_deadline_job GROUP BY status").fetchall()
        jobs = conn.execute("""
            SELECT case_id,rfq_name,status,due_at,next_attempt_at,attempts,reason,metrics,updated_at
            FROM procurement.quotation_deadline_job ORDER BY updated_at DESC LIMIT %s
        """, (limit,)).fetchall()
    return {'counts': list(counts), 'jobs': list(jobs)}
