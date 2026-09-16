"""Short PostgreSQL transactions; no process-local policy cache to go stale."""

from psycopg.types.json import Jsonb
from procurement_db.connection import get_connection
from .schema import CompanyPolicy


class PolicyConflict(Exception):
    pass


def get_active():
    with get_connection() as conn:
        return conn.execute("""
            SELECT v.* FROM procurement.company_policy_head h
            JOIN procurement.company_policy_version v USING (version)
            WHERE h.singleton = true
        """).fetchone()


def list_versions():
    with get_connection() as conn:
        return conn.execute("""
            SELECT * FROM procurement.company_policy_version
            ORDER BY version DESC LIMIT 100
        """).fetchall()


def publish(policy: CompanyPolicy, *, expected_version: int, actor: str, reason: str):
    """Serialize publishers; a stale editor gets 409 instead of losing edits."""
    with get_connection() as conn:
        head = conn.execute("""
            SELECT version FROM procurement.company_policy_head
            WHERE singleton = true FOR UPDATE
        """).fetchone()
        if head["version"] != expected_version:
            raise PolicyConflict("다른 관리자가 먼저 게시했습니다. 최신 정책을 다시 불러오세요.")
        row = conn.execute("""
            INSERT INTO procurement.company_policy_version(version, policy, published_by, reason)
            VALUES (%s, %s, %s, %s) RETURNING *
        """, (expected_version + 1, Jsonb(policy.model_dump()), actor, reason.strip())).fetchone()
        conn.execute("UPDATE procurement.company_policy_head SET version = %s WHERE singleton = true",
                     (row["version"],))
        return row


def for_case(case_id):
    """First use pins the active version atomically; later resumes reuse it."""
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO procurement.case_policy(case_id, version)
            SELECT %s, version FROM procurement.company_policy_head WHERE singleton = true
            ON CONFLICT (case_id) DO NOTHING
        """, (str(case_id),))
        return conn.execute("""
            SELECT v.* FROM procurement.case_policy p
            JOIN procurement.company_policy_version v USING (version)
            WHERE p.case_id = %s
        """, (str(case_id),)).fetchone()


def for_mr(mr_name):
    with get_connection() as conn:
        row = conn.execute("SELECT case_id FROM procurement.procurement_case WHERE mr_name = %s",
                           (mr_name,)).fetchone()
    return for_case(row["case_id"]) if row else get_active()
