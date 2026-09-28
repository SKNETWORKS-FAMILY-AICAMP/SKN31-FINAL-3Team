"""Telemetry must never invoke purchasing or reveal another buyer's reasons."""
from contextlib import contextmanager, nullcontext
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from fastapi import HTTPException
from backend_logic2.repositories import work_progress as repo
from backend_logic2.api import procurement_routes as routes

CASE = '12345678-1234-1234-1234-123456789012'


def test_progress_is_bounded_filtered_and_read_only(monkeypatch):
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = [{'case_id': str(i)} for i in range(201)]
    @contextmanager
    def connection():
        yield conn
    monkeypatch.setattr(repo, 'get_connection', connection)
    result = repo.list_progress(' Buyer@Example.com ')
    assert len(result['items']) == 200 and result['truncated']
    sql, params = conn.execute.call_args.args
    assert params == {'admin': False, 'actor': 'buyer@example.com'}
    assert 'LIMIT 201' in sql and 'j.rfq_name=c.workflow_snapshot' in sql
    assert "c.stage IN ('QUOTATION_COLLECTION','SUPPLIER_SELECTION')" in sql
    assert 'FOR UPDATE' not in sql and 'pg_advisory' not in sql
    assert conn.execute.call_count == 2  # timeout + one query, not N+1 connections


def test_progress_signal_uses_existing_transaction_without_inbox_or_lock():
    conn = MagicMock()
    conn.transaction.return_value = nullcontext()
    repo.notify_progress(conn, CASE)
    sql, args = conn.execute.call_args.args
    assert 'pg_notify' in sql and 'assigned_user_id' in sql
    assert args == (CASE,)
    assert 'INSERT' not in sql and 'pg_advisory' not in sql


def test_signal_failure_cannot_break_workflow():
    conn = MagicMock()
    conn.transaction.side_effect = RuntimeError('telemetry unavailable')
    repo.notify_progress(conn, CASE)  # fail-open for optional telemetry only


def test_other_buyers_cannot_read_case_reasoning(monkeypatch):
    # Role lookup was added by item-group assignment; keep this unit test offline.
    monkeypatch.setattr(routes, 'read_policy_access', lambda _: {'can_manage': False})
    monkeypatch.setattr(routes.case_repository, 'get_case', lambda _: {'assigned_user_id': 'other@example.com'})
    with pytest.raises(HTTPException) as error:
        routes.get_case_decisions(UUID(CASE), {'email': 'buyer@example.com'}, 30, 0)
    assert error.value.status_code == 403


def test_case_reasoning_checks_access_and_scopes_query(monkeypatch):
    monkeypatch.setattr(routes, 'read_policy_access', lambda _: {'can_manage': False})
    from backend_logic2.repositories import ai_decisions
    monkeypatch.setattr(routes.case_repository, 'get_case', lambda _: {'assigned_user_id': 'buyer@example.com'})
    query = MagicMock(return_value=([{'reason': '규격 확인'}], 1))
    monkeypatch.setattr(ai_decisions, 'list_decisions', query)
    assert routes.get_case_decisions(UUID(CASE), {'email': 'buyer@example.com'}, 30, 0)['count'] == 1
    query.assert_called_once_with(case_id=CASE, limit=30, offset=0)


def test_progress_route_does_not_infer_admin_from_client_roles(monkeypatch):
    read = MagicMock(return_value={'items': [], 'truncated': False})
    monkeypatch.setattr(repo, 'list_progress', read)
    routes.get_work_progress({'email': 'buyer@example.com', 'roles': ['System Manager']})
    read.assert_called_once_with('buyer@example.com', admin=False)


@pytest.mark.parametrize('actor,expected', [('buyer@example.com', 1), ('other@example.com', 0), ('Administrator', 2)])
def test_sse_progress_routes_to_owner_or_admin_without_private_payload(monkeypatch, actor, expected):
    import asyncio
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    async def notices():
        for recipient in ['buyer@example.com', None]:
            yield SimpleNamespace(payload=json.dumps({'event_type': 'progress', 'recipient_id': recipient, 'reason': 'PRIVATE'}))
    connection = MagicMock()
    connection.execute = AsyncMock()
    connection.close = AsyncMock()
    connection.notifies = notices
    monkeypatch.setattr(routes, 'require_database_url', lambda: 'mock')
    monkeypatch.setattr(routes.psycopg.AsyncConnection, 'connect', AsyncMock(return_value=connection))
    async def consume():
        response = await routes.stream_procurement_events({'email': actor})
        return [frame async for frame in response.body_iterator]
    frames = asyncio.run(consume())
    assert sum('event: progress' in frame for frame in frames) == expected
    assert 'PRIVATE' not in ''.join(frames)
    connection.close.assert_awaited_once()
