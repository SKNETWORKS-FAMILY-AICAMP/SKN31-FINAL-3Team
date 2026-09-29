"""DB scheduling -> isolated read-only preparation -> ONE idle graph execution.

The old sweep is deliberately not reused. No ERP lookup or graph-lock wait runs
in the scheduler tick or on the HTTP/AnyIO pool. Unchanged WAITING/DONE jobs sleep
until business input changes. Defaults are off to avoid auto-processing old MRs
merely because a developer pulled main or a server deployed this migration.
"""
from __future__ import annotations

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from time import monotonic

from procurement_db.operation_metrics import measure, OperationMetrics, current as current_metrics
from backend_logic2.repositories import deadline_jobs as jobs
from backend_logic2.repositories import cases, tasks
from backend_logic2.services import deadline_readiness, graph_worker

LOGGER = logging.getLogger(__name__)
_PREPARER = ThreadPoolExecutor(max_workers=1, thread_name_prefix='quotation-preparer')
_preparing = None
_executing = None
_last_tick = {}
ACTOR = 'system:deadline-scheduler'


def process_enabled() -> bool:
    """Instance opt-in: a local checkout sharing production DB must not dispatch."""
    return os.getenv('DEADLINE_SCHEDULER_ENABLED', 'false').lower().strip() in {'true', '1', 'on'}


def due_at(row, now=None):
    now = now or datetime.now(timezone.utc)
    values = (row.get('workflow_snapshot') or {}).get('values') or {}
    deadline = deadline_readiness.parse_deadline(values.get('quotation_deadline'))
    if deadline is None:
        return None
    quote = row.get('quotation_snapshot') or {}
    recipients, responded = int(quote.get('recipient_count') or 0), int(quote.get('responded_count') or 0)
    return now if recipients > 0 and responded >= recipients else deadline


def _same_input(job):
    row = jobs.input_for(str(job['case_id']))
    return row if row and row['input_hash'] == job['input_hash'] else None


def _prepare(job):
    """ERP may be slow, but this thread neither holds _GRAPH_LOCK nor owns the graph lane."""
    from backend_logic2.policies.repository import for_case
    from backend_logic2.services import quotation_service
    with measure(seconds=30, max_erp_calls=100) as metrics:
        try:
            row = _same_input(job)
            if row is None:
                jobs.finish(job, 'DONE', '입력이 변경되거나 사람이 먼저 진행했습니다.')
                return
            if due_at(row) is None:
                jobs.finish(job, 'WAITING', '유효한 마감 시간이 설정되기를 기다립니다.')
                return
            if not jobs.control()['enabled'] or not process_enabled():
                jobs.finish(job, 'PENDING', '관리자가 자동 처리를 중지했습니다.', retry_seconds=30)
                return
            if row['unfinished_extractions']:
                # Includes SUBMISSION_UNKNOWN: never omit an unread attachment.
                jobs.finish(job, 'WAITING', '첨부 견적 처리 완료 이벤트를 기다립니다.', metrics=metrics.snapshot())
                return
            policy = for_case(str(job['case_id']))
            if (policy.get('policy') or {}).get('rules', {}).get('automation_mode', 'off') == 'off':
                jobs.finish(job, 'DONE', '이 구매 건의 고정 정책에서 자동 진행이 꺼져 있습니다.', metrics=metrics.snapshot())
                return
            case = cases.get_case(str(job['case_id']))
            if case is None:
                jobs.finish(job, 'DONE', '구매 건이 없습니다.')
                return
            # Manual screens historically tolerate unavailable caches. Background
            # progression must fail closed instead of treating missing evidence as OK.
            validation = quotation_service.validate_case_quotations(case, strict=True)
            readiness = deadline_readiness.judge(case, validation=validation)
            if _same_input(job) is None:
                jobs.finish(job, 'DONE', '준비 도중 견적/차수/평가 입력이 변경되어 결과를 버렸습니다.', metrics=metrics.snapshot())
                return
            jobs.finish(job, 'READY' if readiness.ready else 'WAITING', readiness.reason,
                        prepared=readiness.as_payload(), metrics=metrics.snapshot())
        except Exception:
            LOGGER.exception('deadline_prepare_failed case_id=%s attempt=%s', job['case_id'], job['attempts'])
            retry = int(job['attempts']) < 3
            jobs.finish(job, 'PENDING' if retry else 'BLOCKED',
                        '준비 검사 실패: 재시도 대기' if retry else '준비 검사 3회 실패: 담당자 확인 필요',
                        metrics=metrics.snapshot(), retry_seconds=min(900, 60 * 2 ** int(job['attempts'])))
        finally:
            LOGGER.info('deadline_prepare case_id=%s metrics=%s', job['case_id'], metrics.snapshot())


def _execute(job):
    """One prepared case, never a sweep. Revalidate DB/version and policy at execution."""
    from backend_logic2.services import workflow_service
    from backend_logic2.services.case_recovery import has_local_checkpoint
    started = monotonic()
    # Do not wait behind a human or another API worker holding the graph lock.
    if not workflow_service._GRAPH_LOCK.acquire(blocking=False):
        return
    # Count graph calls without imposing the preparation budget on existing
    # business operations (which already have their own timeout contracts).
    graph_metrics = OperationMetrics(seconds=float('inf'), max_erp_calls=10**9)
    metrics_token = current_metrics.set(graph_metrics)
    invoked = False
    try:
        if not process_enabled() or not jobs.control()['enabled']:
            return
        row = _same_input(job)
        if row is None:
            jobs.finish(job, 'DONE', '입력이 바뀌어 준비 결과를 폐기했습니다.')
            return
        case = cases.get_case(str(job['case_id']))
        if case is None or case['status'] != 'WAITING_INPUT' or case['stage'] != 'QUOTATION_COLLECTION':
            jobs.finish(job, 'DONE', '사람 또는 다른 작업이 먼저 진행했습니다.')
            return
        if not has_local_checkpoint(case):
            jobs.finish(job, 'BLOCKED', '이 서버에 체크포인트가 없습니다. 원래 실행 서버에서 처리하세요.')
            return
        pending = [t for t in tasks.list_tasks(case_id=str(job['case_id']), audience='BUYER', status='PENDING')
                   if t['task_type'] in {'quotation_check', 'check_quotations'}]
        if not pending:
            jobs.finish(job, 'DONE', '진행할 견적 대기 작업이 없습니다.')
            return
        prepared = job.get('prepared') or {}
        if not prepared.get('ready') or row['unfinished_extractions']:
            jobs.finish(job, 'WAITING', '준비 완료 조건이 충족되지 않았습니다.')
            return
        with graph_worker.case_lock(str(job['case_id'])), graph_worker.case_in_flight(str(job['case_id'])):
            latest = cases.get_case(str(job['case_id']))
            if latest and latest.get('automation_paused'):
                jobs.finish(job, 'DONE', '담당자가 이 구매 건의 자동 진행을 껐습니다.')
                return
            if not jobs.start(job):
                return  # CAS: another process or an administrator won the race.
            invoked = True
            task = pending[0]
            workflow_service.resume_task(str(task['task_id']), answer={
                'decision': 'auto', 'ready': True, 'ready_reason': prepared['reason'],
                'late': prepared.get('late', []),
            }, answered_by=ACTOR, expected_version=int(task['version']))
            # Keep the original fingerprint. If graph caches or a new quote changed
            # during execution, the next DB synchronization sees that new version.
            jobs.finish(job, 'DONE', '기존 자동 진행 정책으로 판정했습니다.',
                        metrics={**(job.get('metrics') or {}), 'graph_ms': round((monotonic()-started)*1000),
                                 'graph_erp_calls': graph_metrics.erp_calls,
                                 'graph_db_connections': graph_metrics.db_connections,
                                 'queue_wait_ms': max(0, round((datetime.now(timezone.utc) -
                                     job.get('updated_at', datetime.now(timezone.utc))).total_seconds()*1000) -
                                     round((monotonic()-started)*1000))})
    except graph_worker.CaseBusy:
        return
    except Exception:
        LOGGER.exception('deadline_execute_failed case_id=%s', job['case_id'])
        # A graph error may happen AFTER an ERP write/email. No blind auto retry.
        jobs.finish(job, 'UNCERTAIN' if invoked else 'BLOCKED',
                    '실행 결과 확인 필요: 기존 수동 화면에서 상태를 확인하세요.')
    finally:
        current_metrics.reset(metrics_token)
        workflow_service._GRAPH_LOCK.release()


def tick():
    """Bounded DB-only coordinator. Never await the preparer/graph futures here."""
    global _preparing, _executing, _last_tick
    started = monotonic()
    if not process_enabled():
        return
    if not jobs.control()['enabled']:
        _last_tick = {'at': datetime.now(timezone.utc).isoformat(), 'enabled': False}
        return
    jobs.recover_expired()
    scheduled = []
    for row in jobs.changed_inputs(limit=50):
        due = due_at(row)
        # Persist malformed/missing deadlines too: otherwise the first 50 bad
        # records could permanently starve valid records behind them.
        scheduled.append((row, due or datetime.now(timezone.utc)))
    jobs.schedule_many(scheduled)
    if _preparing is None or _preparing.done():
        if _preparing is not None:
            completed, _preparing = _preparing, None
            completed.result()  # Surface unexpected persistence errors once.
        job = jobs.claim()
        if job:
            _preparing = _PREPARER.submit(_prepare, job)
    if _executing is None or _executing.done():
        if _executing is not None:
            completed, _executing = _executing, None
            completed.result()
        job = jobs.ready_job()
        if job:
            _executing = graph_worker.try_submit_idle(_execute, job)
    _last_tick = {'at': datetime.now(timezone.utc).isoformat(), 'enabled': True,
                  'scheduled': len(scheduled), 'elapsed_ms': round((monotonic()-started)*1000),
                  'graph_outstanding': graph_worker.queued_count()}
    LOGGER.info('deadline_tick %s', _last_tick)


def runtime_status():
    return {'instance_enabled': process_enabled(), 'last_tick': dict(_last_tick),
            'preparing': bool(_preparing and not _preparing.done()),
            'graph_outstanding': graph_worker.queued_count()}


async def run():
    while True:
        try:
            await asyncio.to_thread(tick)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception('deadline_tick_failed; no graph work admitted')
        await asyncio.sleep(30)
