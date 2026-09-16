"""Time-limited warm workers, not a switch that cancels quotation processing.

Intent is committed BEFORE calling RunPod. A DB session lock serializes API
processes and the independent expiry timer, including slow remote PATCH calls.
Only leases acquired here are reconciled: unrelated console settings are never
adopted. No secret, provider response body or arbitrary endpoint reaches the UI.
"""
import asyncio
import logging
import os
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Literal

import requests
from pydantic import BaseModel, ConfigDict, Field
from procurement_db.connection import get_connection

LOGGER = logging.getLogger(__name__)
LOCK_ID = 310031017


class ControlUnavailable(Exception):
    pass


class ControlConflict(Exception):
    pass


class WorkerCommand(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    action: Literal['start', 'extend', 'stop']
    expected_revision: int = Field(ge=0)
    minutes: int = Field(default=60, ge=15, le=120)


def enabled():
    return os.getenv('RUNPOD_ADMIN_CONTROL_ENABLED', 'false').lower() == 'true'


def endpoint_id():
    value = os.getenv('RUNPOD_QUOTATION_ENDPOINT_ID', '').strip()
    if not re.fullmatch(r'[a-zA-Z0-9_-]{5,100}', value):
        raise ControlUnavailable('RunPod 견적서 엔드포인트 설정이 필요합니다.')
    return value


class RunpodControlClient:
    def call(self, method, endpoint, *, operation=None, body=None):
        if not re.fullmatch(r'[a-zA-Z0-9_-]{5,100}', endpoint):
            raise ControlUnavailable('RunPod 엔드포인트 설정을 확인하세요.')
        key = os.getenv('RUNPOD_CONTROL_API_KEY') or os.getenv('RUNPOD_API_KEY', '')
        if not key:
            raise ControlUnavailable('RunPod 관리 API 키가 설정되지 않았습니다.')
        root = (f'https://api.runpod.ai/v2/{endpoint}/{operation}' if operation else
                f'https://rest.runpod.io/v1/endpoints/{endpoint}')
        try:
            response = requests.request(method, root, json=body,
                headers={'Authorization': f'Bearer {key}'}, timeout=(5, 15),
                allow_redirects=False)
            if not 200 <= response.status_code < 300:
                raise ControlUnavailable(f'RunPod 관리 요청 실패 (HTTP {response.status_code}). 권한과 연결을 확인하세요.')
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError('invalid response')
            return data
        except (requests.RequestException, ValueError) as exc:
            raise ControlUnavailable('RunPod 연결을 확인할 수 없습니다. 자동 해제 작업은 재시도됩니다.') from exc

    def settings(self, endpoint):
        return self.call('GET', endpoint)

    def set_minimum(self, endpoint, minimum):
        # Never change workersMax, GPU tier, secrets, image or other settings.
        self.call('PATCH', endpoint, body={'workersMin': minimum})

    def health(self, endpoint):
        return self.call('GET', endpoint, operation='health')

    def warmup(self, endpoint):
        data = self.call('POST', endpoint, operation='run', body={'input': {'warmup': True}})
        job_id = data.get('id', '')
        if not isinstance(job_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,150}', job_id):
            raise ControlUnavailable('모델 준비 요청의 작업 ID를 확인할 수 없습니다.')
        return job_id

    def warmup_result(self, endpoint, job_id):
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,150}', job_id):
            raise ControlUnavailable('모델 준비 작업 ID가 올바르지 않습니다.')
        return self.call('GET', endpoint, operation=f'status/{job_id}')


class LeaseStore:
    def __init__(self, conn):
        self.conn = conn

    def get(self, endpoint):
        return self.conn.execute('SELECT * FROM procurement.runpod_worker_lease WHERE endpoint_id=%s',
                                 (endpoint,)).fetchone()

    def save_intent(self, endpoint, revision, expiry, actor, action):
        # A small explicit transaction commits intent+audit atomically. The
        # surrounding connection is autocommit; the session lock spans it.
        with self.conn.transaction():
            self.conn.execute('''INSERT INTO procurement.runpod_worker_lease
                (endpoint_id, revision, owned, expires_at, updated_by)
                VALUES (%s,%s,true,%s,%s) ON CONFLICT(endpoint_id) DO UPDATE SET
                revision=EXCLUDED.revision, owned=true, expires_at=EXCLUDED.expires_at,
                updated_by=EXCLUDED.updated_by, updated_at=now(), last_error=NULL,
                warmup_job_id=CASE WHEN %s='start' THEN NULL ELSE runpod_worker_lease.warmup_job_id END,
                model_status=CASE WHEN %s='start' THEN 'unknown' ELSE runpod_worker_lease.model_status END''',
                (endpoint, revision, expiry, actor, action, action))
            self.conn.execute('''INSERT INTO procurement.runpod_worker_lease_event
                (endpoint_id,revision,actor,action,expires_at) VALUES (%s,%s,%s,%s,%s)''',
                (endpoint, revision, actor, action, expiry))

    def error(self, endpoint, message):
        self.conn.execute('UPDATE procurement.runpod_worker_lease SET last_error=%s WHERE endpoint_id=%s',
                          (message, endpoint))

    def warmup(self, endpoint, job_id, status):
        self.conn.execute('''UPDATE procurement.runpod_worker_lease
            SET warmup_job_id=%s,model_status=%s WHERE endpoint_id=%s''', (job_id, status, endpoint))

    def release(self, endpoint):
        with self.conn.transaction():
            self.conn.execute('''UPDATE procurement.runpod_worker_lease SET owned=false,
                revision=revision+1,last_error=NULL,model_status='unknown',warmup_job_id=NULL,
                updated_at=now() WHERE endpoint_id=%s''', (endpoint,))
            self.conn.execute('''INSERT INTO procurement.runpod_worker_lease_event
                (endpoint_id,revision,actor,action,expires_at)
                SELECT endpoint_id,revision,'system','released',expires_at
                FROM procurement.runpod_worker_lease WHERE endpoint_id=%s''', (endpoint,))


@contextmanager
def locked_store():
    with get_connection(autocommit=True) as conn:
        row = conn.execute('SELECT pg_try_advisory_lock(%s) AS acquired', (LOCK_ID,)).fetchone()
        if not row['acquired']:
            raise ControlConflict('다른 워커 설정 작업을 처리 중입니다. 잠시 후 다시 시도하세요.')
        try:
            yield LeaseStore(conn)
        finally:
            conn.execute('SELECT pg_advisory_unlock(%s)', (LOCK_ID,))


def apply_command(store, client, endpoint, command, actor, now):
    row = store.get(endpoint)
    revision = row['revision'] if row else 0
    if command.expected_revision != revision:
        raise ControlConflict('다른 관리자가 설정을 변경했습니다. 새로고침 후 다시 시도하세요.')
    active = bool(row and row['owned'])
    if command.action == 'start':
        if active:
            raise ControlConflict('이미 유지 중입니다. 시간 연장 또는 해제를 선택하세요.')
        remote = client.settings(endpoint)
        if remote.get('workersMin') != 0 or remote.get('workersMax') != 1:
            raise ControlConflict('안전을 위해 RunPod 콘솔의 Active workers=0, Max workers=1 상태에서 시작하세요.')
        expiry = now + timedelta(minutes=command.minutes)
    elif command.action == 'extend':
        if not active or row['expires_at'] <= now:
            raise ControlConflict('유지 시간이 만료되었습니다. 상태를 새로고침하세요.')
        # Reset from NOW instead of adding endlessly. At most 2 hours remain.
        expiry = max(row['expires_at'], now + timedelta(minutes=command.minutes))
    else:
        if not active:
            raise ControlConflict('이 관리자 화면에서 시작한 유지 설정이 없습니다.')
        expiry = now
    store.save_intent(endpoint, revision + 1, expiry, actor, command.action)


def reconcile_one(store, client, endpoint, now):
    row = store.get(endpoint)
    if not row or not row['owned']:
        return
    # Expiry always wins over model warmup and even a disabled UI flag.
    if row['expires_at'] <= now:
        client.set_minimum(endpoint, 0)
        if client.settings(endpoint).get('workersMin') != 0:
            raise ControlUnavailable('상시 유지 해제가 아직 확인되지 않았습니다. 재시도합니다.')
        store.release(endpoint)
        return
    remote = client.settings(endpoint)
    if remote.get('workersMax') != 1 or remote.get('workersMin') not in (0, 1):
        raise ControlUnavailable('RunPod 콘솔 설정이 변경되었습니다. 유지 시간 만료 시 상시 유지를 해제합니다.')
    if remote.get('workersMin') != 1:
        client.set_minimum(endpoint, 1)
    # Enable only AFTER the warmup-capable worker release has been deployed.
    if os.getenv('RUNPOD_ADMIN_WARMUP_ENABLED', 'false').lower() != 'true':
        return
    if not row['warmup_job_id']:
        # Persist submission state first. Ambiguous timeouts must not flood the
        # endpoint with warmup jobs. Operator can release/start to retry.
        if row['model_status'] == 'submitting':
            raise ControlUnavailable('모델 준비 요청 결과가 불명확합니다. RunPod 작업 목록을 확인하세요.')
        store.warmup(endpoint, None, 'submitting')
        job_id = client.warmup(endpoint)
        store.warmup(endpoint, job_id, 'loading')
    elif row['model_status'] == 'loading':
        result = client.warmup_result(endpoint, row['warmup_job_id'])
        if result.get('status') == 'COMPLETED':
            ok = result.get('output', {}).get('model_loaded') is True
            store.warmup(endpoint, row['warmup_job_id'], 'ready' if ok else 'failed')
        elif result.get('status') in ('FAILED', 'CANCELLED', 'TIMED_OUT'):
            store.warmup(endpoint, row['warmup_job_id'], 'failed')


def _reconcile_safely(store, client, endpoint):
    try:
        reconcile_one(store, client, endpoint, datetime.now(timezone.utc))
        store.error(endpoint, None)
    except ControlUnavailable as exc:
        store.error(endpoint, str(exc))
        LOGGER.warning('RunPod timed worker operation needs retry or operator review: %s', endpoint)


def command(body, actor):
    if not enabled():
        raise ControlUnavailable('관리자 워커 제어가 비활성화되어 있습니다. 자동 해제 타이머 설치 후 활성화하세요.')
    endpoint = endpoint_id()
    with locked_store() as store:
        client = RunpodControlClient()
        apply_command(store, client, endpoint, body, actor, datetime.now(timezone.utc))
        _reconcile_safely(store, client, endpoint)
    return status()


def status():
    if not enabled():
        return {'enabled': False, 'default_minutes': 60, 'message': '서버의 자동 해제 타이머와 관리 API 권한 설정이 필요합니다.'}
    endpoint = endpoint_id()
    with get_connection() as conn:
        row = LeaseStore(conn).get(endpoint)
    result = {'enabled': True, 'default_minutes': 60, 'revision': row['revision'] if row else 0,
              'owned': bool(row and row['owned']), 'expires_at': row['expires_at'] if row else None,
              'updated_by': row['updated_by'] if row else None,
              'model_status': row['model_status'] if row else 'unknown',
              'last_error': row['last_error'] if row else None,
              'remote_known': False, 'workers_min': None, 'workers_max': None}
    client = RunpodControlClient()
    try:
        remote = client.settings(endpoint)
        result.update(remote_known=True, workers_min=remote.get('workersMin'), workers_max=remote.get('workersMax'))
        health = client.health(endpoint)
        result['workers'] = health.get('workers', {})
        # A past warmup is NOT a readiness guarantee after worker replacement.
        # UI deliberately labels this as the most recent warmup result.
    except ControlUnavailable as exc:
        result['last_error'] = str(exc)
    return result


def reconcile_all():
    # Independent of configured endpoint and UI flag: old leases must expire
    # even after an endpoint configuration change or backend restart.
    with locked_store() as store:
        rows = store.conn.execute('SELECT endpoint_id FROM procurement.runpod_worker_lease WHERE owned=true').fetchall()
        for row in rows:
            _reconcile_safely(store, RunpodControlClient(), row['endpoint_id'])


async def reconciliation_loop():
    while True:
        try:
            await asyncio.to_thread(reconcile_all)
        except ControlConflict:
            pass  # API process or the independent safety timer owns the lock.
        except Exception:
            # Never emit external response bodies, tokens or DSNs.
            LOGGER.error('RunPod worker lease reconciliation failed; check database and control configuration')
        await asyncio.sleep(30)
