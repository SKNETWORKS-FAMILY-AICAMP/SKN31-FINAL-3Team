"""Opt-in tests against an EMPTY disposable PostgreSQL database, never a live DB.

Set DEADLINE_TEST_DATABASE_URL to a database named scheduler_test_*.
The fixture refuses other names and replaces procurement tables ONLY there.
"""
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
import pytest

from backend_logic2.repositories import deadline_jobs as jobs

DSN = os.getenv('DEADLINE_TEST_DATABASE_URL', '')
pytestmark = pytest.mark.skipif(not DSN, reason='isolated PostgreSQL test database required')


@pytest.fixture(scope='module', autouse=True)
def schema():
    if not DSN:
        return
    assert conninfo_to_dict(DSN)['dbname'].startswith('scheduler_test_'), 'REFUSE non-test database'
    with psycopg.connect(DSN) as c:
        c.execute('''
            CREATE SCHEMA procurement;
            CREATE TABLE procurement.procurement_case(case_id uuid PRIMARY KEY, status text, stage text,
                workflow_snapshot jsonb, quotation_snapshot jsonb);
            CREATE TABLE procurement.case_policy(case_id uuid PRIMARY KEY, version integer);
            CREATE TABLE procurement.quotation_specification_cache(rfq_name text,quotation_id text,
                input_hash text,evaluation_source text,assessment_json jsonb,updated_at timestamptz DEFAULT now());
            CREATE TABLE procurement.quotation_submission(quotation_name text,rfq_name text,
                submitted_at timestamptz,source text);
            CREATE TABLE procurement.quotation_extraction_job(job_id uuid,context jsonb,status text);
            CREATE TABLE procurement.quotation_intake_failure(failure_id integer,rfq_name text,updated_at timestamptz);
        ''')
        migration = Path(os.getenv('DEADLINE_TEST_MIGRATION', str(Path(__file__).parents[1] /
                         'migrations/025_create_quotation_deadline_jobs.sql')))
        c.execute(migration.read_text(encoding='utf-8'))


@pytest.fixture
def db(monkeypatch):
    @contextmanager
    def connect():
        with psycopg.connect(DSN, row_factory=dict_row) as c:
            yield c
    monkeypatch.setattr(jobs, '_get_connection', connect)
    with connect() as c:
        c.execute('''TRUNCATE procurement.procurement_case,procurement.case_policy,
            procurement.quotation_specification_cache,procurement.quotation_submission,
            procurement.quotation_extraction_job,procurement.quotation_intake_failure,
            procurement.quotation_deadline_revision,procurement.quotation_deadline_job CASCADE''')
        c.execute('UPDATE procurement.quotation_deadline_control SET enabled=true')
    return connect


def seed(db, *, rfq='RFQ-1'):
    case = str(uuid4())
    with db() as c:
        c.execute('INSERT INTO procurement.procurement_case VALUES (%s,%s,%s,%s,%s)',
                  (case,'WAITING_INPUT','QUOTATION_COLLECTION',Jsonb({'values': {
                      'rfq_name': rfq,'rfq_rounds': [{'rfq_name':'RFQ-OLD'}],
                      'quotation_deadline':'2026-09-01T18:00:00+09:00'}}),
                   Jsonb({'recipient_count':3,'responded_count':2,'quotations':[]})))
    return case


def scheduled(db):
    seed(db)
    row = jobs.changed_inputs()[0]
    jobs.schedule(row, datetime.now(timezone.utc)-timedelta(seconds=1))
    return jobs.claim()


def test_job_survives_connections_and_claim_is_exclusive(db):
    job = scheduled(db)
    assert job['status'] == 'CHECKING'
    assert jobs.claim() is None
    assert jobs.status()['counts'] == [{'status':'CHECKING','count':1}]


@pytest.mark.parametrize('finished_status', ['WAITING','DONE','BLOCKED'])
def test_unchanged_input_never_becomes_due_again(db, finished_status):
    job = scheduled(db)
    assert jobs.finish(job,finished_status,'unchanged')
    assert jobs.changed_inputs() == []
    assert jobs.claim() is None


def test_cache_completion_wakes_waiting_but_identical_resave_does_not(db):
    job = scheduled(db)
    jobs.finish(job,'WAITING','AI running')
    with db() as c:
        c.execute('INSERT INTO procurement.quotation_specification_cache VALUES (%s,%s,%s,%s,%s,now())',
                  ('RFQ-1','SQ-1','quote-v1','model-v1',Jsonb({'score':90})))
    changed = jobs.changed_inputs()[0]
    assert changed['input_hash'] != job['input_hash']
    jobs.schedule(changed,datetime.now(timezone.utc)-timedelta(seconds=1))
    job = jobs.claim(); jobs.finish(job,'DONE','done')
    with db() as c:
        c.execute("UPDATE procurement.quotation_specification_cache SET updated_at=now()+interval '1 second'")
    assert jobs.changed_inputs() == []


def test_same_count_different_cache_hash_wakes_job(db):
    job = scheduled(db); jobs.finish(job,'WAITING','pending')
    with db() as c:
        c.execute('INSERT INTO procurement.quotation_specification_cache VALUES (%s,%s,%s,%s,%s,now())',
                  ('RFQ-1','SQ-1','old','model-v1',Jsonb({'score':90})))
    first = jobs.changed_inputs()[0]['input_hash']
    with db() as c:
        c.execute("UPDATE procurement.quotation_specification_cache SET input_hash='new'")
    assert jobs.changed_inputs()[0]['input_hash'] != first


def test_old_round_event_changes_fingerprint(db):
    job = scheduled(db); jobs.finish(job,'DONE','done')
    jobs.bump_rfqs(['RFQ-OLD'])
    assert jobs.changed_inputs()[0]['case_id'] == job['case_id']


def test_future_deadline_is_not_claimed(db):
    seed(db); row=jobs.changed_inputs()[0]
    jobs.schedule(row,datetime.now(timezone.utc)+timedelta(days=1))
    assert jobs.claim() is None


def test_changed_deadline_replaces_reservation(db):
    job=scheduled(db); jobs.finish(job,'DONE','done')
    with db() as c:
        c.execute('UPDATE procurement.procurement_case SET workflow_snapshot=jsonb_set(workflow_snapshot,%s,%s)',
                  (['values','quotation_deadline'],Jsonb('2026-10-02T18:00:00+09:00')))
    row=jobs.changed_inputs()[0]
    jobs.schedule(row,datetime.now(timezone.utc)+timedelta(days=1))
    assert jobs.claim() is None


def test_kill_switch_prevents_claim_and_start(db):
    job=scheduled(db); jobs.finish(job,'READY','ready',prepared={'ready':True})
    jobs.set_enabled(False,actor='admin',reason='test stop')
    assert not jobs.start(job)
    assert jobs.claim() is None


def test_stale_preparation_cannot_start(db):
    job=scheduled(db); jobs.finish(job,'READY','ready',prepared={'ready':True})
    jobs.bump_rfqs(['RFQ-1'])
    assert not jobs.start(job)


def test_pending_extraction_blocks_start_even_if_quote_count_is_unchanged(db):
    job=scheduled(db); jobs.finish(job,'READY','ready',prepared={'ready':True})
    with db() as c:
        c.execute('INSERT INTO procurement.quotation_extraction_job VALUES (%s,%s,%s)',
                  (str(uuid4()),Jsonb({'rfq_name':'RFQ-1'}),'SUBMISSION_UNKNOWN'))
    assert jobs.input_for(str(job['case_id']))['unfinished_extractions'] == 1
    assert not jobs.start(job)


def test_expired_read_only_claim_retries_and_rejects_old_worker(db):
    old=scheduled(db)
    with db() as c:
        c.execute("UPDATE procurement.quotation_deadline_job SET lease_until=now()-interval '1 second'")
    jobs.recover_expired()
    fresh=jobs.claim()
    assert fresh['claim_token'] != old['claim_token']
    assert not jobs.finish(old,'READY','stale')
    assert jobs.finish(fresh,'WAITING','current')


def test_interrupted_graph_is_not_replayed_even_after_input_changes(db):
    job=scheduled(db); jobs.finish(job,'READY','ready',prepared={'ready':True})
    assert jobs.start(job)
    with db() as c:
        c.execute("UPDATE procurement.quotation_deadline_job SET lease_until=now()-interval '1 second'")
    jobs.recover_expired()
    jobs.bump_rfqs(['RFQ-1'])
    assert jobs.changed_inputs() == []
    assert jobs.claim() is None
    assert jobs.status()['jobs'][0]['status'] == 'UNCERTAIN'


def test_changed_filter_runs_before_batch_limit(db):
    for _ in range(4):
        seed(db)
    for row in jobs.changed_inputs():
        jobs.schedule(row,datetime.now(timezone.utc)-timedelta(seconds=1))
    for _ in range(4):
        job=jobs.claim(); jobs.finish(job,'DONE','done')
    new_case=seed(db)
    assert str(jobs.changed_inputs(limit=1)[0]['case_id']) == new_case


def test_competing_process_connections_only_claim_once(db):
    from concurrent.futures import ThreadPoolExecutor
    seed(db)
    jobs.schedule(jobs.changed_inputs()[0],datetime.now(timezone.utc)-timedelta(seconds=1))
    with ThreadPoolExecutor(max_workers=2) as workers:
        results=list(workers.map(lambda _: jobs.claim(), range(2)))
    assert sum(row is not None for row in results) == 1


def test_batch_reservation_uses_one_connection(db, monkeypatch):
    for _ in range(20):
        seed(db)
    entries=[(row,datetime.now(timezone.utc)-timedelta(seconds=1)) for row in jobs.changed_inputs()]
    from unittest.mock import Mock
    counted=Mock(wraps=db)
    monkeypatch.setattr(jobs,'_get_connection',counted)
    jobs.schedule_many(entries)
    assert counted.call_count == 1
    assert jobs.changed_inputs() == []


def test_preparation_retry_does_not_run_before_backoff(db):
    job=scheduled(db)
    jobs.finish(job,'PENDING','retry later',retry_seconds=120)
    assert jobs.claim() is None


def test_new_rfq_round_can_wake_blocked_but_not_uncertain(db):
    job=scheduled(db); jobs.finish(job,'BLOCKED','ERP unavailable')
    with db() as c:
        c.execute('UPDATE procurement.procurement_case SET workflow_snapshot=jsonb_set(workflow_snapshot,%s,%s)',
                  (['values','rfq_name'],Jsonb('RFQ-NEW')))
    row=jobs.changed_inputs()[0]
    jobs.schedule(row,datetime.now(timezone.utc)-timedelta(seconds=1))
    new=jobs.claim()
    assert new['rfq_name']=='RFQ-NEW' and new['attempts']==1
