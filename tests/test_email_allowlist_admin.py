import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch
import pytest
from pydantic import ValidationError
from backend_logic2.policies import allowlist
from backend_logic2.integrations.erp_client import filter_email_recipients


@pytest.fixture
def editable_list(tmp_path, monkeypatch):
    path = tmp_path / 'allowlist.json'
    path.write_text(json.dumps({'enabled': True, 'recipients': ['old@example.com']}), encoding='utf-8')
    monkeypatch.setenv('EMAIL_RECIPIENT_ALLOWLIST_PATH', str(path))
    monkeypatch.setenv('TEST_MODE', 'custom_only')
    @contextmanager
    def lock(): yield MagicMock()
    monkeypatch.setattr(allowlist, 'get_connection', lock)
    return path


def test_save_changes_actual_delivery_filter_and_archives_previous(editable_list):
    old = allowlist.get_allowlist()
    saved = allowlist.save_allowlist(allowlist.SaveAllowlist(
        expected_revision=old['revision'], recipients=['New@Example.com', 'new@example.com'], reason='테스트 주소 변경',
    ), 'Administrator')
    assert saved['recipients'] == ['new@example.com']
    assert saved['revision'] != old['revision']
    assert filter_email_recipients(['old@example.com', 'new@example.com', 'outsider@example.com']) == ['new@example.com']
    backup = list((editable_list.parent / 'allowlist-history').glob('*.json'))
    assert len(backup) == 1
    assert json.loads(backup[0].read_text())['recipients'] == ['old@example.com']
    assert json.loads(editable_list.read_text(encoding='utf-8'))['updated_by'] == 'Administrator'
    with pytest.raises(allowlist.AllowlistConflict):
        allowlist.save_allowlist(allowlist.SaveAllowlist(expected_revision=old['revision'], recipients=[], reason='이전 화면'), 'Administrator')


def test_empty_blocks_all_and_cannot_change_delivery_mode(editable_list, monkeypatch):
    old = allowlist.get_allowlist()
    allowlist.save_allowlist(allowlist.SaveAllowlist(expected_revision=old['revision'], recipients=[], reason='전체 차단'), 'admin')
    assert filter_email_recipients(['old@example.com']) == []
    monkeypatch.setenv('TEST_MODE', 'false')
    with pytest.raises(allowlist.AllowlistUnavailable):
        allowlist.save_allowlist(allowlist.SaveAllowlist(expected_revision=old['revision'], recipients=[], reason='허용 안됨'), 'admin')


@pytest.mark.parametrize('address', ['*@gmail.com', 'gmail.com', 'A <a@example.com>', 'a@example.com\r\nBcc:x@example.com', 'a..b@example.com'])
def test_no_wildcard_or_header_injection(address):
    with pytest.raises(ValidationError):
        allowlist.SaveAllowlist(expected_revision='a' * 64, recipients=[address], reason='검증 테스트')


def test_failed_replace_keeps_original(editable_list):
    before = editable_list.read_bytes()
    old = allowlist.get_allowlist()
    with patch.object(allowlist.os, 'replace', side_effect=PermissionError()):
        with pytest.raises(allowlist.AllowlistUnavailable):
            allowlist.save_allowlist(allowlist.SaveAllowlist(expected_revision=old['revision'], recipients=[], reason='쓰기 실패'), 'admin')
    assert editable_list.read_bytes() == before
    assert not list(editable_list.parent.glob('.allowlist-*'))


def test_allowlist_api_uses_same_live_role_gate(editable_list):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from backend_logic2.api.policy_routes import router
    from auth_service.dependencies import require_authenticated_user
    app = FastAPI(); app.include_router(router)
    app.dependency_overrides[require_authenticated_user] = lambda: {'erp_user_id': 'buyer'}
    with TestClient(app) as client, patch('backend_logic2.api.policy_routes.read_policy_access', return_value={'can_manage': False}):
        assert client.get('/api/company-policy/email-allowlist').status_code == 403
        assert client.post('/api/company-policy/email-allowlist', json={
            'expected_revision': 'a' * 64, 'recipients': [], 'reason': '권한 없음',
        }).status_code == 403
