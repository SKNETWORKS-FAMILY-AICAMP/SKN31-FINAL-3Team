"""Role checks run before graph resumption. ERP, DB and email are mocked."""
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from auth_service.dependencies import require_authenticated_user
from backend_logic2.api import procurement_routes as routes
from backend_logic2.policies.access import PolicyAccessUnavailable, read_policy_access


@pytest.mark.parametrize('identity,roles,enabled,allowed', [
    ('buyer', ['Purchase Manager'], 1, True),
    ('buyer', ['Purchase Master Manager'], 1, True),
    ('buyer', ['Purchase User'], 1, False),
    ('Administrator', [], 1, False),
    ('boss', ['System Manager'], 1, False),
    ('buyer', ['Purchase Manager'], 0, False),
])
def test_capability_requires_explicit_purchasing_role(identity, roles, enabled, allowed):
    response = MagicMock(status_code=200)
    response.json.return_value = {'data': {'name': identity, 'enabled': enabled,
        'roles': [{'role': role} for role in roles]}}
    with patch('backend_logic2.policies.access.requests.get', return_value=response):
        assert read_policy_access(identity)['can_approve_po'] is allowed


@pytest.fixture
def api():
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[require_authenticated_user] = lambda: {
        'erp_user_id': 'buyer', 'roles': ['Purchase Manager'],  # stale browser claim must be ignored
    }
    # No accidental ERP/DB access or mail transmission from a test failure.
    with patch.object(routes, '_require_case_access') as case_access, \
         patch.object(routes.task_repository, 'get_task', return_value={'task_type': 'po_approval', 'case_id': 'c1'}) as task, \
         patch.object(routes, 'read_policy_access', return_value={'enabled': True, 'roles': []}) as access, \
         patch.object(routes.workflow_service, 'resume_task', return_value={'ok': True}) as resume, \
         TestClient(app) as client:
        yield client, task, access, resume, case_access


@pytest.mark.parametrize('kind,decision', [('po_approval', 'approve'), ('po_creation_failed', 'retry')])
@pytest.mark.parametrize('roles,enabled,status_code', [
    (['Purchase Manager'], True, 200), (['Purchase Master Manager'], True, 200),
    (['System Manager'], True, 403), ([], True, 403), (['Purchase Manager'], False, 403),
])
def test_direct_api_cannot_bypass_role_guard(api, kind, decision, roles, enabled, status_code):
    client, task, access, resume, _ = api
    task.return_value['task_type'] = kind
    access.return_value = {'enabled': enabled, 'roles': roles}
    result = client.post('/api/procurement/tasks/t1/answer', json={'answer': {'decision': decision}, 'version': 1})
    assert result.status_code == status_code
    assert resume.called == (status_code == 200)


def test_role_revocation_and_erp_outage_fail_closed(api):
    client, _, access, resume, _ = api
    access.side_effect = [{'enabled': True, 'roles': ['Purchase Manager']},
                          {'enabled': True, 'roles': []}, PolicyAccessUnavailable('unavailable')]
    body = {'answer': {'decision': 'approve'}, 'version': 1}
    assert [client.post('/api/procurement/tasks/t1/answer', json=body).status_code for _ in range(3)] == [200, 403, 503]
    assert resume.call_count == 1


def test_existing_case_scope_and_other_workflows_are_preserved(api):
    client, task, access, resume, case_access = api
    task.return_value['task_type'] = 'mr_review'
    assert client.post('/api/procurement/tasks/t1/answer', json={'answer': {'decision': 'approve'}}).status_code == 200
    access.assert_not_called()
    resume.reset_mock()
    case_access.side_effect = HTTPException(403, 'Not assigned')
    task.return_value['task_type'] = 'po_approval'
    assert client.post('/api/procurement/tasks/t1/answer', json={'answer': {'decision': 'approve'}}).status_code == 403
    resume.assert_not_called()


def test_failed_po_checkpoint_cannot_bypass_guard_via_start(api):
    client, _, _, _, case_access = api
    case_access.return_value = {'stage': 'PO_CREATION', 'status': 'FAILED'}
    with patch.object(routes.workflow_service, 'queue_case_start') as queue:
        assert client.post('/api/procurement/cases/c1/start').status_code == 403
        queue.assert_not_called()
