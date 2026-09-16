"""Durable RunPod job state and cross-process completion serialization."""

from contextlib import contextmanager
from uuid import uuid4

from psycopg.types.json import Jsonb

from procurement_db import get_connection


def create_job(request_id, endpoint_id, context, prompt_sha256, prompt_version):
    with get_connection() as conn:
        row = conn.execute(
            """INSERT INTO procurement.quotation_extraction_job
               (job_id, request_id, endpoint_id, context, prompt_sha256, prompt_version)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (request_id) DO NOTHING RETURNING *""",
            (uuid4(), request_id, endpoint_id, Jsonb(context), prompt_sha256, prompt_version),
        ).fetchone()
        if row:
            return dict(row), True
        row = conn.execute(
            "SELECT * FROM procurement.quotation_extraction_job WHERE request_id = %s",
            (request_id,),
        ).fetchone()
        return dict(row), False


def get_job(job_id):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM procurement.quotation_extraction_job WHERE job_id = %s", (job_id,),
        ).fetchone()
    return dict(row) if row else None


def mark_submitted(job_id, runpod_job_id):
    with get_connection() as conn:
        conn.execute(
            """UPDATE procurement.quotation_extraction_job
               SET runpod_job_id = %s, status = 'SUBMITTED', updated_at = now(),
                   next_check_at = now() + interval '120 seconds'
               WHERE job_id = %s AND status = 'SUBMITTING'""", (runpod_job_id, job_id),
        )


def record_callback(runpod_job_id):
    """Persist a wakeup before ACK. Never trust callback status/output.

    Only known jobs can be woken. Repeated signals cannot force status reads
    more frequently than once every ten seconds per job.
    """
    with get_connection() as conn:
        row = conn.execute(
            """UPDATE procurement.quotation_extraction_job
               SET callback_at = now(), next_check_at = LEAST(next_check_at,
                   GREATEST(now(), COALESCE(last_checked_at, '-infinity'::timestamptz)
                                      + interval '10 seconds'))
               WHERE runpod_job_id = %s RETURNING *""", (runpod_job_id,),
        ).fetchone()
    return dict(row) if row else None


def due_jobs(limit=20):
    with get_connection() as conn:
        # A crash during POST leaves an ambiguous submission. Never spend GPU
        # credits by blindly resubmitting: retain it for operator inspection.
        conn.execute(
            """UPDATE procurement.quotation_extraction_job
               SET status = 'SUBMISSION_UNKNOWN', updated_at = now(),
                   last_error = 'Submission interrupted; inspect RunPod before retry'
               WHERE status = 'SUBMITTING' AND created_at < now() - interval '5 minutes'""",
        )
        rows = conn.execute(
            """SELECT job_id FROM procurement.quotation_extraction_job
               WHERE status IN ('SUBMITTED', 'READY', 'REGISTERED') AND next_check_at <= now()
               ORDER BY next_check_at LIMIT %s""", (limit,),
        ).fetchall()
    return [str(row['job_id']) for row in rows]


@contextmanager
def processing_lock(job_id, business_key):
    """Session locks survive commits but release on process/connection death.

    Hold no transaction during external requests. The second lock also
    serializes different attachments for the same RFQ/supplier.
    """
    with get_connection(autocommit=True) as conn:
        acquired = []
        try:
            for key in (f'runpod-job:{job_id}', f'runpod-supplier:{business_key}'):
                ok = conn.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s, 0)) AS locked", (key,),
                ).fetchone()['locked']
                if not ok:
                    yield False
                    return
                acquired.append(key)
            yield True
        finally:
            for key in reversed(acquired):
                conn.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (key,))


def mark_checked(job_id):
    with get_connection() as conn:
        conn.execute(
            """UPDATE procurement.quotation_extraction_job
               SET last_checked_at = now(), next_check_at = now() + interval '120 seconds'
               WHERE job_id = %s""", (job_id,),
        )


def save_result(job_id, result):
    with get_connection() as conn:
        conn.execute(
            """UPDATE procurement.quotation_extraction_job SET status = 'READY',
               result_json = %s, last_error = NULL, updated_at = now(), next_check_at = now()
               WHERE job_id = %s""", (Jsonb(result), job_id),
        )


def save_registration(job_id, registration):
    with get_connection() as conn:
        conn.execute(
            """UPDATE procurement.quotation_extraction_job SET status = 'REGISTERED',
               registration_json = %s, updated_at = now(), next_check_at = now()
               WHERE job_id = %s""", (Jsonb(registration), job_id),
        )


def complete_job(job_id):
    with get_connection() as conn:
        conn.execute(
            """UPDATE procurement.quotation_extraction_job SET status = 'COMPLETED',
               processed_at = now(), updated_at = now(), last_error = NULL WHERE job_id = %s""",
            (job_id,),
        )


def fail_job(job_id, error, *, terminal=False, submission_unknown=False):
    # Callers pass controlled messages, not provider exception strings that
    # might contain request headers or document contents.
    with get_connection() as conn:
        conn.execute(
            """UPDATE procurement.quotation_extraction_job SET last_error = %s,
               retry_count = retry_count + 1, updated_at = now(),
               next_check_at = now() + interval '120 seconds',
               status = CASE WHEN %s THEN 'SUBMISSION_UNKNOWN'
                             WHEN %s OR retry_count >= 9 THEN 'FAILED' ELSE status END
               WHERE job_id = %s""", (error[:500], submission_unknown, terminal, job_id),
        )
