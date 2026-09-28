"""Real two-connection regression for the graph/inbox self-deadlock.

Run only against a DISPOSABLE database via NOTIFICATION_TEST_DATABASE_URL.
No ERP calls, email, or workflow execution are involved.
"""
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from time import monotonic
from uuid import uuid4

import pytest

DSN = os.getenv("NOTIFICATION_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="Requires disposable PostgreSQL DB")


@pytest.fixture
def inbox(monkeypatch):
    import psycopg
    from psycopg.rows import dict_row
    from backend_logic2.repositories import notifications
    from backend_logic2.services import graph_worker

    @contextmanager
    def connection(*, autocommit=False):
        # Safety net: even a regression to the old SQL cannot hang the suite.
        with psycopg.connect(DSN, autocommit=autocommit, row_factory=dict_row,
                             options="-c statement_timeout=5000") as conn:
            yield conn

    with connection() as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS procurement")
        conn.execute("""CREATE TABLE IF NOT EXISTS procurement.notification (
            notification_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            case_id uuid, recipient_id text, notification_type text,
            title text, message text, payload jsonb)""")
    monkeypatch.setattr(notifications, "get_connection", connection)
    monkeypatch.setattr(graph_worker, "get_connection", connection)
    return notifications, graph_worker, connection


def notify(repo, case_id, title="test"):
    return repo.create_notification(case_id=case_id, recipient_id=None,
        notification_type="TEST", title=title, message="lock regression only")


def test_notification_finishes_while_real_graph_case_lock_is_held(inbox):
    repo, graph, _ = inbox
    case_id = str(uuid4())
    with graph.case_lock(case_id):
        # Repository opens another physical PG connection, as production does.
        assert notify(repo, case_id)["title"] == "test"


def test_concurrent_inbox_replacements_leave_one_latest_row(inbox):
    repo, _, connection = inbox
    case_id = str(uuid4())
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda n: notify(repo, case_id, str(n)), range(8)))
    with connection() as conn:
        assert conn.execute("SELECT count(*) AS n FROM procurement.notification "
                            "WHERE case_id=%s", (case_id,)).fetchone()["n"] == 1


def test_inbox_contention_times_out_and_preserves_previous_notice(inbox):
    from psycopg.errors import LockNotAvailable
    repo, _, connection = inbox
    case_id = str(uuid4())
    original = notify(repo, case_id, "previous")
    with connection() as holder:
        holder.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                       (repo._INBOX_LOCK_NAMESPACE, case_id))
        started = monotonic()
        with pytest.raises(LockNotAvailable):
            notify(repo, case_id, "replacement")
        assert monotonic() - started < 5
    with connection() as conn:
        rows = conn.execute("SELECT * FROM procurement.notification WHERE case_id=%s",
                            (case_id,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["notification_id"] == original["notification_id"]


def test_row_contention_also_rolls_back_with_bounded_wait(inbox):
    from psycopg.errors import LockNotAvailable
    repo, _, connection = inbox
    case_id = str(uuid4())
    original = notify(repo, case_id)
    with connection() as holder:
        holder.execute("SELECT * FROM procurement.notification WHERE case_id=%s FOR UPDATE",
                       (case_id,))
        with pytest.raises(LockNotAvailable):
            notify(repo, case_id)
    with connection() as conn:
        assert conn.execute("SELECT notification_id FROM procurement.notification "
                            "WHERE case_id=%s", (case_id,)).fetchone()["notification_id"] == original["notification_id"]
