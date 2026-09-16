from unittest.mock import MagicMock, patch
import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient
from auth_service.dependencies import require_authenticated_user
from backend_logic2.api.policy_routes import router
from backend_logic2.policies.access import read_policy_access, PolicyAccessUnavailable
from backend_logic2.policies.schema import CompanyPolicy


@pytest.mark.parametrize('identity,roles,enabled,allowed', [
    ('Administrator', [], 1, True),
    ('boss@example.com', ['System Manager'], 1, True),
    ('buyer@example.com', ['Purchase Master Manager'], 1, True),
    ('buyer@example.com', ['Purchase Manager'], 1, False),
    ('buyer@example.com', ['Purchase User'], 1, False),
    ('admin@example.com', [], 1, False),  # no longer inferred from an email alias
    ('Administrator', [], 0, False),
    ('buyer@example.com', ['Purchase Master Manager'], '0', False),
])
def test_current_erp_roles_control_access(identity, roles, enabled, allowed):
    response = MagicMock(status_code=200)
    response.json.return_value = {'data': {'name': identity, 'enabled': enabled,
                                          'roles': [{'role': role} for role in roles]}}
    with patch('backend_logic2.policies.access.requests.get', return_value=response) as get:
        result = read_policy_access(identity)
    assert result['can_manage'] is allowed
    assert result['roles'] == sorted(roles)
    assert result['source'] == 'erpnext'
    assert get.call_args.kwargs['timeout'] == (5, 10)


def test_outage_identity_mismatch_and_deleted_user_fail_closed():
    with patch('backend_logic2.policies.access.requests.get', side_effect=requests.Timeout()):
        with pytest.raises(PolicyAccessUnavailable):
            read_policy_access('Administrator')
    response = MagicMock(status_code=200)
    response.json.return_value = {'data': {'name': 'someone-else', 'enabled': 1, 'roles': []}}
    with patch('backend_logic2.policies.access.requests.get', return_value=response):
        with pytest.raises(PolicyAccessUnavailable):
            read_policy_access('Administrator')
        response.status_code = 404
        assert not read_policy_access('Administrator')['can_manage']


def test_open_session_cannot_publish_after_role_revocation():
    app = FastAPI(); app.include_router(router)
    app.dependency_overrides[require_authenticated_user] = lambda: {
        'erp_user_id': 'buyer@example.com', 'roles': ['System Manager'],  # ignored stale claim
    }
    body = {'expected_version': 1, 'policy': CompanyPolicy().model_dump(), 'reason': '정책 조정'}
    with TestClient(app) as client, \
         patch('backend_logic2.api.policy_routes.read_policy_access', side_effect=[
             {'can_manage': True, 'roles': ['Purchase Master Manager']},
             {'can_manage': False, 'roles': []},
             PolicyAccessUnavailable('ERP unavailable'),
         ]) as access, patch('backend_logic2.api.policy_routes.repository.publish') as publish:
        assert client.get('/api/company-policy/capabilities').json()['can_manage']
        assert client.post('/api/company-policy/publish', json=body).status_code == 403
        assert client.post('/api/company-policy/publish', json=body).status_code == 503
        publish.assert_not_called()
        assert access.call_count == 3
