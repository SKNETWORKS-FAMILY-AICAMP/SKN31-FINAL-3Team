"""Opt-in PostgreSQL checks using a disposable schema, never production rows.

RUNPOD_REPOSITORY_DB_TEST=1 enables these with the usual procurement DB config.
"""

import os
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest

from procurement_db import get_connection
from backend_logic2.repositories import quotation_jobs as jobs


@pytest.fixture
def isolated_repository(monkeypatch):
    if os.getenv('RUNPOD_REPOSITORY_DB_TEST') != '1':
        pytest.skip('explicit disposable-schema PostgreSQL test')
    schema = 'test_runpod_' + uuid4().hex

    class Proxy:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, params=None):
            return self.conn.execute(sql.replace('procurement.', f'{schema}.'), params)

    @contextmanager
    def isolated_connection(**kwargs):
        with get_connection(**kwargs) as conn:
            yield Proxy(conn)

    with get_connection() as conn:
        conn.execute(f'CREATE SCHEMA {schema}')
        migration = Path(__file__).resolve().parents[1] / 'migrations/013_create_runpod_quotation_jobs.sql'
        conn.execute(migration.read_text(encoding='utf-8').replace('procurement.', f'{schema}.'))
    monkeypatch.setattr(jobs, 'get_connection', isolated_connection)
    try:
        yield isolated_connection
    finally:
        with get_connection() as conn:
            # Only this fixture's UUID-named schema can be dropped.
            assert schema.startswith('test_runpod_') and len(schema) == 44
            conn.execute(f'DROP SCHEMA {schema} CASCADE')


def test_database_lifecycle_locks_dedupe_and_recovery(isolated_repository):
    context = {'rfq_name': 'TEST', 'supplier_id': 'TEST'}
    row, created = jobs.create_job('req-1', 'endpoint', context, 'hash', 'v1')
    assert created
    duplicate, created = jobs.create_job('req-1', 'endpoint', context, 'hash', 'v1')
    assert not created and duplicate['job_id'] == row['job_id']
    job_id = str(row['job_id'])
    jobs.mark_submitted(job_id, 'remote-1')
    assert jobs.record_callback('unknown') is None
    jobs.record_callback('remote-1')
    assert job_id in jobs.due_jobs()
    with jobs.processing_lock(job_id, 'TEST') as first:
        assert first
        with jobs.processing_lock(job_id, 'TEST') as second:
            assert not second
        with jobs.processing_lock('another-job', 'TEST') as same_supplier:
            assert not same_supplier
    with jobs.processing_lock(job_id, 'TEST') as released:
        assert released
    jobs.mark_checked(job_id)
    jobs.record_callback('remote-1')
    assert job_id not in jobs.due_jobs()  # notification flood cannot force repeated HTTP GETs
    jobs.save_result(job_id, {'extraction': {}})
    assert job_id in jobs.due_jobs()
    jobs.save_registration(job_id, {'name': 'TEST-SQ'})
    jobs.complete_job(job_id)
    jobs.record_callback('remote-1')
    assert job_id not in jobs.due_jobs()
    assert jobs.get_job(job_id)['registration_json']['name'] == 'TEST-SQ'


def test_interrupted_submission_becomes_unknown_without_resubmit(isolated_repository):
    row, _ = jobs.create_job('req-2', 'endpoint', {}, 'hash', 'v1')
    with isolated_repository() as conn:
        conn.execute("UPDATE procurement.quotation_extraction_job SET created_at = now() - interval '10 minutes'")
    assert jobs.due_jobs() == []
    assert jobs.get_job(str(row['job_id']))['status'] == 'SUBMISSION_UNKNOWN'
