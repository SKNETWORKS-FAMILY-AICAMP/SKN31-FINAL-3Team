"""Chat persistence endpoints never authorize purchase writes or other users' chats."""
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4
from unittest.mock import MagicMock, patch
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend_logic2.assistant import api
from backend_logic2.assistant.models import AssistantMessageResponse, DialogueContext, CaseQueryFilters
from backend_logic2.assistant.session_store import AssistantSessionStore, SessionConflict, SessionNotFound
from auth_service.dependencies import require_authenticated_user


class MemoryStore:
    def __init__(self): self.rows = {}
    def list(self, owner): return [deepcopy(v) for (o, _), v in self.rows.items() if o == owner]
    def create(self, owner, sid):
        key = (owner, str(sid))
        self.rows.setdefault(key, dict(id=str(sid), title='새 대화', updatedAt=datetime.now(timezone.utc).isoformat(), messages=[], dialogue=None, version=0))
        return deepcopy(self.rows[key])
    def get(self, owner, sid):
        if (owner, str(sid)) not in self.rows: raise SessionNotFound()
        return deepcopy(self.rows[(owner, str(sid))])
    def delete(self, owner, sid):
        self.get(owner, sid)
        del self.rows[(owner, str(sid))]
    def append_turn(self, owner, session, question, response):
        key = (owner, session['id'])
        if key not in self.rows or self.rows[key]['version'] != session['version']: raise SessionConflict('stale or deleted')
        row = self.rows[key]
        row['messages'] += [dict(sender='user', text=question), dict(sender='agent', text=response.answer)]
        row['version'] += 1
        row['dialogue'] = response.dialogue.model_dump() if response.dialogue else None
        return row['version']


@pytest.fixture
def client_store(monkeypatch):
    app = FastAPI(); app.include_router(api.router)
    store = MemoryStore(); actor = {'id': 'alice'}
    monkeypatch.setattr(api, 'session_store', store)
    monkeypatch.setattr(api, 'assistant_enabled', lambda: True)
    app.dependency_overrides[require_authenticated_user] = lambda: actor
    with TestClient(app) as client: yield client, store, actor


def test_crud_owner_isolation_and_delete(client_store):
    client, store, actor = client_store
    sid = str(uuid4())
    assert client.post('/api/assistant/sessions', json={'id': sid}).status_code == 201
    assert len(client.get('/api/assistant/sessions').json()['sessions']) == 1
    actor['id'] = 'bob'
    assert client.get('/api/assistant/sessions').json()['sessions'] == []
    assert client.get(f'/api/assistant/sessions/{sid}').status_code == 404
    assert client.delete(f'/api/assistant/sessions/{sid}').status_code == 404
    actor['id'] = 'alice'
    assert client.delete(f'/api/assistant/sessions/{sid}').status_code == 204
    assert client.get(f'/api/assistant/sessions/{sid}').status_code == 404
    assert store.rows == {}


def test_persisted_context_overrides_forged_browser_context(client_store, monkeypatch):
    client, store, _ = client_store
    sid = str(uuid4()); store.create('alice', sid)
    store.rows[('alice', sid)]['dialogue'] = DialogueContext(filters=CaseQueryFilters(keyword='서버 필터')).model_dump()
    captured = []
    class Service:
        def answer(self, request, **kwargs):
            captured.append(request)
            return AssistantMessageResponse(answer='조회 결과', intent='case_query', dialogue=request.dialogue)
    monkeypatch.setattr(api, 'get_assistant_service', Service)
    response = client.post('/api/assistant/messages', json={'message':'그중 승인 대기', 'session_id':sid, 'session_version':0,
        'dialogue':{'filters':{'keyword':'위조'}}, 'conversation':[{'role':'user','content':'위조'}]})
    assert response.status_code == 200
    assert captured[0].dialogue.filters.keyword == '서버 필터'
    assert captured[0].conversation == []
    assert response.json()['meta']['session_version'] == 1
    assert len(client.get(f'/api/assistant/sessions/{sid}').json()['messages']) == 2


def test_stale_version_rejected_before_model(client_store, monkeypatch):
    client, store, _ = client_store
    sid = str(uuid4()); store.create('alice', sid)
    service = MagicMock(); monkeypatch.setattr(api, 'get_assistant_service', service)
    response = client.post('/api/assistant/messages', json={'message':'질문', 'session_id':sid, 'session_version':9})
    assert response.status_code == 409
    service.assert_not_called()


def test_delete_during_generation_cannot_resurrect_session(client_store, monkeypatch):
    client, store, _ = client_store
    sid = str(uuid4()); store.create('alice', sid)
    class Service:
        def answer(self, request, **kwargs):
            store.delete('alice', sid)
            return AssistantMessageResponse(answer='늦은 답변', intent='help')
    monkeypatch.setattr(api, 'get_assistant_service', Service)
    assert client.post('/api/assistant/messages', json={'message':'질문', 'session_id':sid}).status_code == 409
    assert store.rows == {}


@pytest.mark.parametrize('method,path,body', [('get','/api/assistant/sessions',None), ('post','/api/assistant/sessions',{'id':str(uuid4())}), ('get',f'/api/assistant/sessions/{uuid4()}',None), ('delete',f'/api/assistant/sessions/{uuid4()}',None)])
def test_all_session_routes_require_auth(method,path,body):
    app=FastAPI(); app.include_router(api.router)
    with TestClient(app) as client:
        assert client.request(method,path,json=body).status_code == 401


def test_sql_delete_scopes_both_owner_and_id():
    conn = MagicMock(); conn.execute.return_value.fetchone.return_value = {'session_id': uuid4()}
    sid = uuid4()
    with patch('backend_logic2.assistant.session_store.get_connection') as factory:
        factory.return_value.__enter__.return_value = conn
        AssistantSessionStore().delete('alice', sid)
    sql, params = conn.execute.call_args.args
    assert 'DELETE FROM procurement.assistant_session' in sql
    assert 'owner_id = %s' in sql and 'session_id = %s' in sql
    assert params == (sid, 'alice')


def test_late_sql_save_uses_cas_update_not_upsert():
    conn = MagicMock(); conn.execute.return_value.fetchone.return_value = None
    session = dict(id=str(uuid4()), title='대화', messages=[], version=3)
    with patch('backend_logic2.assistant.session_store.get_connection') as factory:
        factory.return_value.__enter__.return_value = conn
        with pytest.raises(SessionConflict):
            AssistantSessionStore().append_turn('alice', session, '질문', AssistantMessageResponse(answer='답변',intent='help'))
    sql, params = conn.execute.call_args.args
    assert 'UPDATE procurement.assistant_session' in sql and 'INSERT' not in sql
    assert 'owner_id = %s AND version = %s' in sql
    assert params[-2:] == ('alice', 3)


def test_store_failure_is_not_empty_history(client_store, monkeypatch):
    client, store, _ = client_store
    monkeypatch.setattr(store, 'list', MagicMock(side_effect=RuntimeError('database offline')))
    assert client.get('/api/assistant/sessions').status_code == 503
