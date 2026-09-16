"""No paid requests: fake RunPod, in-memory leases and authenticated API tests."""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest
from pydantic import ValidationError
from fastapi import FastAPI
from fastapi.testclient import TestClient
from auth_service.dependencies import require_authenticated_user
from backend_logic2.api.policy_routes import router
from backend_logic2.services import runpod_worker_control as control

NOW = datetime(2026, 9, 16, tzinfo=timezone.utc)
ENDPOINT = 'testendpoint'


class MemoryStore:
    def __init__(self):
        self.row = None
        self.events = []

    def get(self, endpoint):
        return dict(self.row) if self.row else None

    def save_intent(self, endpoint, revision, expiry, actor, action):
        self.row = {**(self.row or {}), 'revision': revision, 'owned': True, 'expires_at': expiry,
                    'updated_by': actor, 'last_error': None}
        if action == 'start':
            self.row.update(warmup_job_id=None, model_status='unknown')
        self.events.append(action)

    def error(self, endpoint, message):
        self.row['last_error'] = message

    def warmup(self, endpoint, job_id, status):
        self.row.update(warmup_job_id=job_id, model_status=status)

    def release(self, endpoint):
        self.row.update(owned=False, revision=self.row['revision'] + 1)
        self.events.append('released')


class FakeRunpod:
    def __init__(self):
        self.remote = {'workersMin': 0, 'workersMax': 1}
        self.calls = []
        self.fail_patch = False

    def settings(self, endpoint):
        return dict(self.remote)

    def set_minimum(self, endpoint, minimum):
        self.calls.append(minimum)
        if self.fail_patch:
            raise control.ControlUnavailable('temporary failure')
        self.remote['workersMin'] = minimum

    def warmup(self, endpoint):
        self.calls.append('warmup')
        return 'test-job'

    def warmup_result(self, endpoint, job_id):
        return {'status': 'COMPLETED', 'output': {'model_loaded': True}}


def start(store, client):
    control.apply_command(store, client, ENDPOINT,
        control.WorkerCommand(action='start', expected_revision=0), 'Administrator', NOW)


def test_default_hour_and_expiry_without_browser(monkeypatch):
    monkeypatch.setenv('RUNPOD_ADMIN_WARMUP_ENABLED', 'false')
    store, client = MemoryStore(), FakeRunpod()
    start(store, client)
    assert store.row['expires_at'] == NOW + timedelta(hours=1)
    assert client.calls == []  # Durable intent exists before the paid operation.
    control.reconcile_one(store, client, ENDPOINT, NOW)
    assert client.calls == [1]
    monkeypatch.setenv('RUNPOD_ADMIN_CONTROL_ENABLED', 'false')
    control.reconcile_one(store, client, ENDPOINT, NOW + timedelta(hours=1))
    assert client.calls == [1, 0]
    assert not store.row['owned']


def test_uncertain_start_and_stop_retry_preserve_expiry(monkeypatch):
    monkeypatch.setenv('RUNPOD_ADMIN_WARMUP_ENABLED', 'false')
    store, client = MemoryStore(), FakeRunpod()
    start(store, client)
    client.fail_patch = True
    with pytest.raises(control.ControlUnavailable):
        control.reconcile_one(store, client, ENDPOINT, NOW)
    assert store.row['owned']
    with pytest.raises(control.ControlUnavailable):
        control.reconcile_one(store, client, ENDPOINT, NOW + timedelta(hours=2))
    assert store.row['owned']  # Never report a release on failed PATCH.
    client.fail_patch = False
    control.reconcile_one(store, client, ENDPOINT, NOW + timedelta(hours=2))
    assert not store.row['owned'] and client.remote['workersMin'] == 0


def test_unowned_console_settings_never_changed():
    store, client = MemoryStore(), FakeRunpod()
    client.remote['workersMin'] = 1
    control.reconcile_one(store, client, ENDPOINT, NOW)
    with pytest.raises(control.ControlConflict):
        start(store, client)
    assert client.calls == [] and store.row is None


def test_multiple_worker_budget_is_refused():
    store, client = MemoryStore(), FakeRunpod()
    client.remote['workersMax'] = 2
    with pytest.raises(control.ControlConflict):
        start(store, client)
    assert store.row is None


def test_extension_bounded_and_stale_revision_rejected():
    store, client = MemoryStore(), FakeRunpod()
    start(store, client)
    with pytest.raises(control.ControlConflict):
        control.apply_command(store, client, ENDPOINT,
            control.WorkerCommand(action='stop', expected_revision=0), 'A', NOW)
    control.apply_command(store, client, ENDPOINT,
        control.WorkerCommand(action='extend', expected_revision=1, minutes=120), 'A', NOW)
    control.apply_command(store, client, ENDPOINT,
        control.WorkerCommand(action='extend', expected_revision=2, minutes=120), 'A', NOW)
    assert store.row['expires_at'] == NOW + timedelta(hours=2)
    control.apply_command(store, client, ENDPOINT,
        control.WorkerCommand(action='extend', expected_revision=3, minutes=30), 'A', NOW)
    assert store.row['expires_at'] == NOW + timedelta(hours=2)


@pytest.mark.parametrize('minutes', [0, 121, -1, True, '60'])
def test_invalid_duration(minutes):
    with pytest.raises(ValidationError):
        control.WorkerCommand(action='start', expected_revision=0, minutes=minutes)


def test_warmup_once_no_document_or_email(monkeypatch):
    monkeypatch.setenv('RUNPOD_ADMIN_WARMUP_ENABLED', 'true')
    store, client = MemoryStore(), FakeRunpod()
    start(store, client)
    for _ in range(3):
        control.reconcile_one(store, client, ENDPOINT, NOW)
    assert client.calls == [1, 'warmup']
    assert store.row['model_status'] == 'ready'
    control.apply_command(store, client, ENDPOINT,
        control.WorkerCommand(action='stop', expected_revision=1), 'A', NOW)
    control.reconcile_one(store, client, ENDPOINT, NOW)
    assert client.calls == [1, 'warmup', 0]


def test_uncertain_warmup_is_not_automatically_resubmitted(monkeypatch):
    monkeypatch.setenv('RUNPOD_ADMIN_WARMUP_ENABLED', 'true')
    store, client = MemoryStore(), FakeRunpod()
    start(store, client)
    with patch.object(client, 'warmup', side_effect=control.ControlUnavailable('timeout')) as warmup:
        for _ in range(2):
            with pytest.raises(control.ControlUnavailable):
                control.reconcile_one(store, client, ENDPOINT, NOW)
        assert warmup.call_count == 1
    control.reconcile_one(store, client, ENDPOINT, NOW + timedelta(hours=1))
    assert not store.row['owned']


def test_routes_require_live_admin_permission():
    app = FastAPI(); app.include_router(router)
    with TestClient(app) as http, patch('backend_logic2.api.policy_routes.read_policy_access',
            side_effect=lambda actor: {'can_manage': actor == 'Administrator'}), \
            patch.object(control, 'command', return_value={'enabled': True}) as change:
        path = '/api/company-policy/runpod-worker'
        body = {'action': 'start', 'expected_revision': 0}
        assert http.post(path, json=body).status_code == 401
        app.dependency_overrides[require_authenticated_user] = lambda: {'erp_user_id': 'buyer'}
        assert http.get(path).status_code == 403
        assert http.post(path, json=body).status_code == 403
        change.assert_not_called()
        app.dependency_overrides[require_authenticated_user] = lambda: {'erp_user_id': 'Administrator'}
        assert http.post(path, json=body).status_code == 200
        assert change.call_args.args[1] == 'Administrator'


def test_api_patch_is_narrow_and_secrets_never_echoed(monkeypatch):
    monkeypatch.setenv('RUNPOD_CONTROL_API_KEY', 'fake-secret')
    reply = MagicMock(status_code=200)
    reply.json.return_value = {}
    with patch.object(control.requests, 'request', return_value=reply) as request:
        control.RunpodControlClient().set_minimum(ENDPOINT, 0)
        assert request.call_args.kwargs['json'] == {'workersMin': 0}
        assert request.call_args.kwargs['allow_redirects'] is False
        reply.status_code = 403
        with pytest.raises(control.ControlUnavailable) as error:
            control.RunpodControlClient().settings(ENDPOINT)
        assert 'fake-secret' not in str(error.value)
