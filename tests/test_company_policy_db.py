"""Opt-in PostgreSQL transactions in a UUID-named disposable schema only."""
import os
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from procurement_db import get_connection
from backend_logic2.policies import repository as repo
from backend_logic2.policies.schema import CompanyPolicy


@pytest.fixture
def isolated_policy(monkeypatch):
    if os.getenv('POLICY_REPOSITORY_DB_TEST') != '1':
        pytest.skip('explicit disposable-schema PostgreSQL test')
    schema = 'test_policy_' + uuid4().hex
    legacy_case = str(uuid4())
    class Proxy:
        def __init__(self, conn): self.conn = conn
        def execute(self, sql, params=None):
            return self.conn.execute(sql.replace('procurement.', f'{schema}.'), params)
    @contextmanager
    def connection():
        with get_connection() as conn:
            yield Proxy(conn)
    with get_connection() as conn:
        conn.execute(f'CREATE SCHEMA {schema}')
    try:
        with connection() as conn:
            conn.execute('CREATE TABLE procurement.procurement_case(case_id UUID PRIMARY KEY, mr_name TEXT)')
            conn.execute('INSERT INTO procurement.procurement_case VALUES (%s, %s)', (legacy_case, 'LEGACY'))
            path = Path(__file__).resolve().parents[1] / 'migrations/014_create_company_policy.sql'
            conn.execute(path.read_text(encoding='utf-8'))
        monkeypatch.setattr(repo, 'get_connection', connection)
        yield connection, legacy_case
    finally:
        with get_connection() as conn:
            assert schema.startswith('test_policy_') and len(schema) == 44
            conn.execute(f'DROP SCHEMA {schema} CASCADE')


def test_publish_pin_resume_restore(isolated_policy):
    connection, legacy_case = isolated_policy
    assert repo.get_active()['policy'] == CompanyPolicy().model_dump()
    changed = CompanyPolicy(rules={'urgent_lead_days': 2})
    repo.publish(changed, expected_version=1, actor='tester', reason='테스트 게시')
    assert repo.for_case(legacy_case)['version'] == 1
    new_case = str(uuid4())
    with connection() as conn:
        conn.execute('INSERT INTO procurement.procurement_case VALUES (%s, %s)', (new_case, 'NEW'))
    assert repo.for_case(new_case)['version'] == 2
    assert repo.for_mr('NEW')['version'] == 2
    restored = repo.publish(CompanyPolicy(), expected_version=2, actor='tester', reason='기존 기준 복원')
    assert restored['version'] == 3
    assert repo.for_case(new_case)['version'] == 2  # survives latest-version change
    assert [v['version'] for v in repo.list_versions()] == [3, 2, 1]


def test_concurrent_publish_has_one_winner(isolated_policy):
    def publish(_):
        try:
            return repo.publish(CompanyPolicy(), expected_version=1, actor='tester', reason='동시 게시')['version']
        except repo.PolicyConflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(map(str, pool.map(publish, [1, 2]))) == ['2', 'conflict']
    assert len(repo.list_versions()) == 2
