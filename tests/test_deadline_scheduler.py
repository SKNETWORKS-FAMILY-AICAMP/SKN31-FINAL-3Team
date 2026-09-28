"""Regression contract for the replacement scheduler: no production services."""
from concurrent.futures import Future
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend_logic2.services import deadline_scheduler as s, graph_worker, workflow_service
from backend_logic2.repositories import deadline_jobs
from procurement_db.operation_metrics import measure, OperationBudgetExceeded


@pytest.fixture
def scenario(monkeypatch):
    monkeypatch.setenv('DEADLINE_SCHEDULER_ENABLED', 'true')
    row = {'case_id': 'case-1', 'input_hash': 'v1', 'rfq_name': 'RFQ-1',
           'unfinished_extractions': 0, 'workflow_snapshot': {'values': {
               'rfq_name': 'RFQ-1', 'quotation_deadline': '2026-09-01T18:00:00+09:00'}},
           'quotation_snapshot': {'recipient_count': 3, 'responded_count': 2}}
    job = {**row, 'attempts': 1, 'claim_token': 'token', 'prepared': {'ready': True, 'reason': 'ready', 'late': []}}
    case = {**row, 'status': 'WAITING_INPUT', 'stage': 'QUOTATION_COLLECTION', 'mr_name': 'MR-1'}
    monkeypatch.setattr(s.jobs, 'input_for', Mock(return_value=row))
    monkeypatch.setattr(s.jobs, 'control', Mock(return_value={'enabled': True}))
    monkeypatch.setattr(s.jobs, 'finish', Mock(return_value=True))
    monkeypatch.setattr(s.jobs, 'start', Mock(return_value=True))
    monkeypatch.setattr(s.cases, 'get_case', Mock(return_value=case))
    from backend_logic2.policies import repository
    monkeypatch.setattr(repository, 'for_case', Mock(return_value={'policy': {'rules': {'automation_mode': 'on'}}}))
    from backend_logic2.services import quotation_service, case_recovery
    monkeypatch.setattr(quotation_service, 'validate_case_quotations', Mock(return_value={'items': []}))
    monkeypatch.setattr(s.deadline_readiness, 'judge', Mock(return_value=s.deadline_readiness.Readiness(
        ready=True, reason='ready', deadline=datetime.now(timezone.utc))))
    monkeypatch.setattr(case_recovery, 'has_local_checkpoint', Mock(return_value=True))
    monkeypatch.setattr(s.tasks, 'list_tasks', Mock(return_value=[{'task_id': 't1', 'task_type': 'quotation_check', 'version': 5}]))
    monkeypatch.setattr(graph_worker, 'case_lock', lambda _: nullcontext())
    monkeypatch.setattr(workflow_service, 'resume_task', Mock(return_value={}))
    return SimpleNamespace(row=row, job=job, case=case, validation=quotation_service.validate_case_quotations,
                           finish=s.jobs.finish, invoke=workflow_service.resume_task, policy=repository.for_case)


def test_due_time_and_all_replied(scenario):
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert s.due_at(scenario.row, now) > now
    scenario.row['quotation_snapshot']['responded_count'] = 3
    assert s.due_at(scenario.row, now) == now


def test_date_only_deadline_is_18_kst(scenario):
    scenario.row['workflow_snapshot']['values']['quotation_deadline'] = '2026-09-30'
    assert s.due_at(scenario.row).hour == 18


@pytest.mark.parametrize('pending', [1, 4])
def test_inflight_attachment_never_reads_erp(scenario, pending):
    scenario.row['unfinished_extractions'] = pending
    s._prepare(scenario.job)
    scenario.validation.assert_not_called()
    assert scenario.finish.call_args.args[1] == 'WAITING'


def test_disabled_policy_does_not_read_erp(scenario):
    scenario.policy.return_value = {'policy': {'rules': {'automation_mode': 'off'}}}
    s._prepare(scenario.job)
    scenario.validation.assert_not_called()
    assert scenario.finish.call_args.args[1] == 'DONE'


def test_prepare_reuses_single_validation_outside_graph_lane(scenario):
    s._prepare(scenario.job)
    scenario.validation.assert_called_once()
    s.deadline_readiness.judge.assert_called_once_with(scenario.case, validation={'items': []})
    scenario.invoke.assert_not_called()
    assert scenario.finish.call_args.args[1] == 'READY'


def test_modified_input_during_preparation_is_discarded(scenario):
    s.jobs.input_for.side_effect = [scenario.row, {**scenario.row, 'input_hash': 'v2'}]
    s._prepare(scenario.job)
    assert scenario.finish.call_args.args[1] == 'DONE'
    assert 'prepared' not in scenario.finish.call_args.kwargs


def test_ai_pending_waits_for_input_change_not_every_minute(scenario):
    s.deadline_readiness.judge.return_value.ready = False
    s._prepare(scenario.job)
    assert scenario.finish.call_args.args[1] == 'WAITING'


@pytest.mark.parametrize(('attempt','expected'), [(1,'PENDING'), (2,'PENDING'), (3,'BLOCKED')])
def test_erp_failure_has_bounded_backoff(scenario, attempt, expected):
    scenario.job['attempts'] = attempt
    scenario.validation.side_effect = RuntimeError('offline')
    s._prepare(scenario.job)
    assert scenario.finish.call_args.args[1] == expected
    assert scenario.finish.call_args.kwargs['retry_seconds'] >= 120


def test_no_lock_is_held_while_erp_is_slow(scenario):
    observed = []
    def check(_case, **_kwargs):
        def other_thread():
            got = workflow_service._GRAPH_LOCK.acquire(blocking=False)
            observed.append(got)
            if got:
                workflow_service._GRAPH_LOCK.release()
        thread = Thread(target=other_thread)
        thread.start(); thread.join(timeout=2)
        return {'items': []}
    scenario.validation.side_effect = check
    s._prepare(scenario.job)
    assert observed == [True]


def test_execute_uses_prepared_result_and_current_task_version(scenario):
    s._execute(scenario.job)
    scenario.validation.assert_not_called()
    s.deadline_readiness.judge.assert_not_called()
    assert scenario.invoke.call_args.kwargs['expected_version'] == 5
    assert scenario.finish.call_args.args[1] == 'DONE'


@pytest.mark.parametrize('reason', ['input','control','instance','task','checkpoint','cas'])
def test_execute_rechecks_guards(scenario, monkeypatch, reason):
    if reason == 'input':
        s.jobs.input_for.return_value = {**scenario.row, 'input_hash': 'v2'}
    elif reason == 'control':
        s.jobs.control.return_value = {'enabled': False}
    elif reason == 'instance':
        monkeypatch.setenv('DEADLINE_SCHEDULER_ENABLED', 'false')
    elif reason == 'task':
        s.tasks.list_tasks.return_value = []
    elif reason == 'checkpoint':
        from backend_logic2.services import case_recovery
        case_recovery.has_local_checkpoint.return_value = False
    else:
        s.jobs.start.return_value = False
    s._execute(scenario.job)
    scenario.invoke.assert_not_called()


def test_graph_error_is_uncertain_never_automatically_replayed(scenario):
    scenario.invoke.side_effect = RuntimeError('write may already have happened')
    s._execute(scenario.job)
    assert scenario.finish.call_args.args[1] == 'UNCERTAIN'


def test_busy_graph_never_waits(scenario):
    entered, release = Event(), Event()
    def hold():
        with workflow_service._GRAPH_LOCK:
            entered.set(); release.wait(3)
    thread = Thread(target=hold)
    thread.start()
    assert entered.wait(1)
    try:
        s._execute(scenario.job)
        scenario.invoke.assert_not_called()
        with pytest.raises(RuntimeError, match='요청은 실행되지 않았습니다'):
            with workflow_service._graph_lock_without_wait():
                pytest.fail('must not acquire another thread lock')
    finally:
        release.set(); thread.join(timeout=2)


def test_graph_idle_admission_cannot_queue_a_second_auto_job():
    entered, release = Event(), Event()
    def hold():
        entered.set(); release.wait(3)
    first = graph_worker.try_submit_idle(hold)
    assert first is not None and entered.wait(1)
    try:
        assert graph_worker.try_submit_idle(lambda: None) is None
    finally:
        release.set(); first.result(timeout=2)


def test_disabled_instance_does_not_touch_database(monkeypatch):
    monkeypatch.delenv('DEADLINE_SCHEDULER_ENABLED', raising=False)
    read = Mock(side_effect=AssertionError('no DB'))
    monkeypatch.setattr(s.jobs, 'control', read)
    s.tick()
    read.assert_not_called()


def test_tick_does_not_wait_for_slow_preparation(scenario, monkeypatch):
    pending = Future()
    monkeypatch.setattr(s, '_preparing', pending)
    monkeypatch.setattr(s, '_executing', pending)
    monkeypatch.setattr(s.jobs, 'recover_expired', Mock())
    monkeypatch.setattr(s.jobs, 'changed_inputs', Mock(return_value=[]))
    claim = Mock(side_effect=AssertionError('must not queue more'))
    monkeypatch.setattr(s.jobs, 'claim', claim)
    s.tick()
    claim.assert_not_called()
    assert not pending.done()


def test_failed_future_is_consumed_once(scenario, monkeypatch):
    failed = Future(); failed.set_exception(RuntimeError('storage failed'))
    monkeypatch.setattr(s, '_preparing', failed)
    monkeypatch.setattr(s, '_executing', None)
    monkeypatch.setattr(s.jobs, 'recover_expired', Mock())
    monkeypatch.setattr(s.jobs, 'changed_inputs', Mock(return_value=[]))
    monkeypatch.setattr(s.jobs, 'claim', Mock(return_value=None))
    monkeypatch.setattr(s.jobs, 'ready_job', Mock(return_value=None))
    with pytest.raises(RuntimeError, match='storage failed'):
        s.tick()
    s.tick()


def test_operation_counters_are_scoped_and_enforce_call_budget():
    with measure(max_erp_calls=1) as stats:
        assert stats.before_erp() > 0
        with pytest.raises(OperationBudgetExceeded):
            stats.before_erp()
    from procurement_db.operation_metrics import current
    assert current.get() is None


def test_admin_only_controls(scenario, monkeypatch):
    from backend_logic2.api import policy_routes as routes
    from fastapi import HTTPException
    monkeypatch.setattr(routes, '_require_admin', Mock(side_effect=HTTPException(403)))
    write = Mock()
    monkeypatch.setattr(deadline_jobs, 'set_enabled', write)
    with pytest.raises(HTTPException) as exc:
        routes.change_deadline_scheduler(routes.DeadlineSchedulerCommand(enabled=True, reason='test'), {})
    assert exc.value.status_code == 403
    write.assert_not_called()


def test_manual_analysis_dispatch_does_not_run_on_request_pool(monkeypatch):
    from threading import BoundedSemaphore
    executor = Mock()
    future = Future()
    executor.submit.return_value = future
    monkeypatch.setattr(workflow_service, '_QUOTATION_EXECUTOR', executor)
    slots = BoundedSemaphore(1)
    assert slots.acquire(False)
    monkeypatch.setattr(workflow_service, '_QUOTATION_SLOTS', slots)
    workflow_service._submit_queued_quotation_analysis('t1', claimed_version=2)
    executor.submit.assert_called_once_with(workflow_service._run_queued_quotation_analysis,
                                           't1', claimed_version=2)
    assert not slots.acquire(False)
    future.set_result(None)
    assert slots.acquire(False)


def test_full_manual_queue_rejects_before_claiming_task(monkeypatch):
    from threading import BoundedSemaphore
    from fastapi import BackgroundTasks
    slots = BoundedSemaphore(1)
    slots.acquire()
    monkeypatch.setattr(workflow_service, '_QUOTATION_SLOTS', slots)
    reserve = Mock()
    monkeypatch.setattr(workflow_service, '_reserve_quotation_analysis', reserve)
    with pytest.raises(RuntimeError, match='대기열이 가득'):
        workflow_service.queue_quotation_analysis('t1', answer={'decision':'auto'}, answered_by='buyer',
            expected_version=1, background_tasks=BackgroundTasks())
    reserve.assert_not_called()


def test_failed_manual_reservation_returns_slot(monkeypatch):
    from threading import BoundedSemaphore
    from fastapi import BackgroundTasks
    slots = BoundedSemaphore(1)
    monkeypatch.setattr(workflow_service, '_QUOTATION_SLOTS', slots)
    monkeypatch.setattr(workflow_service, '_reserve_quotation_analysis', Mock(side_effect=RuntimeError('stale task')))
    with pytest.raises(RuntimeError, match='stale task'):
        workflow_service.queue_quotation_analysis('t1', answer={}, answered_by='buyer', expected_version=1,
                                                 background_tasks=BackgroundTasks())
    assert slots.acquire(False)
