"""Real LangGraph checkpoints + in-memory repository: never ERP/SMTP/production DB.

Regression: MR-00178 consumed a quotation answer and failed at create_pr due
to the recipient allowlist. The old PENDING quotation task must not survive.
"""
from types import SimpleNamespace
from typing import TypedDict
from unittest.mock import Mock

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, StateGraph
from langgraph.types import Command, interrupt

from backend_logic2.services import case_recovery, workflow_service as service
from backend_logic2.workflow import process_graph

BLOCKED = "PR 수신 이메일이 EMAIL_RECIPIENT_ALLOWLIST에 없어 발송을 차단했습니다."


class State(TypedDict, total=False):
    case_id: str
    mr_name: str
    status: str


@pytest.fixture
def rig(monkeypatch):
    # Fail immediately if a new path accidentally tries to reach a real DB.
    monkeypatch.setattr("psycopg.connect", Mock(side_effect=AssertionError("Real DB forbidden")))
    state = SimpleNamespace(
        case={"case_id": "case-178", "mr_name": "MR-178", "thread_id": "MR-178",
              "status": "WAITING_INPUT", "stage": "QUOTATION_COLLECTION"},
        tasks=[{"task_id": "old-task", "case_id": "case-178", "task_type": "check_quotations",
                "status": "PENDING", "version": 1}],
        transitions=[], executions=0, two_interrupts=False,
    )

    def quotations(values):
        interrupt({"type": "check_quotations"})
        return Command(update={"status": "awaiting_final_selection" if state.two_interrupts else "creating_pr"},
                       goto="final_selection" if state.two_interrupts else "create_pr")

    def select(values):
        interrupt({"type": "final_selection"})
        return Command(update={"status": "creating_pr"}, goto="create_pr")

    def create_pr(values):
        state.executions += 1
        raise RuntimeError(BLOCKED)

    graph = StateGraph(State)
    graph.add_node("check_quotations", quotations)
    graph.add_node("final_selection", select)
    graph.add_node("create_pr", create_pr)
    graph.add_edge(START, "check_quotations")
    state.app = graph.compile(checkpointer=InMemorySaver())
    state.config = service._config("MR-178")
    state.app.invoke({"case_id": "case-178", "mr_name": "MR-178", "status": "awaiting_quotation_check"}, state.config)
    monkeypatch.setattr(service, "get_process_app", lambda: state.app)
    monkeypatch.setattr(process_graph, "get_process_app", lambda: state.app)
    monkeypatch.setattr(service.case_repository, "get_case", lambda _id: dict(state.case))
    monkeypatch.setattr(service.case_repository, "list_open_case_references", lambda: [dict(state.case)])

    def transition(_id, **kwargs):
        state.transitions.append(kwargs)
        state.case.update({k: v for k, v in kwargs.items() if k in {"status", "stage", "workflow_snapshot", "last_error"}})
        return dict(state.case)

    monkeypatch.setattr(service.case_repository, "transition_case", transition)
    repo = service.task_repository
    monkeypatch.setattr(repo, "get_task", lambda tid: dict(next(t for t in state.tasks if t["task_id"] == tid)))
    monkeypatch.setattr(repo, "list_tasks", lambda **kwargs: [dict(t) for t in state.tasks if t["status"] == kwargs.get("status", "PENDING")])

    def claim(tid, *, expected_version, **kw):
        task = next(t for t in state.tasks if t["task_id"] == tid)
        assert task["status"] == "PENDING" and task["version"] == expected_version
        task.update(status="PROCESSING", version=expected_version + 1)
        return dict(task)

    def release(tid, *, claimed_version):
        task = next(t for t in state.tasks if t["task_id"] == tid)
        assert task["version"] == claimed_version
        if task["status"] == "PROCESSING":
            task.update(status="PENDING", version=claimed_version + 1)
            return True
        return False

    def complete(tid, *, claimed_version):
        task = next(t for t in state.tasks if t["task_id"] == tid)
        assert task["version"] == claimed_version
        task.update(status="COMPLETED", version=claimed_version + 1)

    def supersede(_id, active_types):
        for task in state.tasks:
            if task["status"] == "PENDING" and task["task_type"] not in active_types:
                task["status"] = "SUPERSEDED"

    def replace(**kwargs):
        for task in state.tasks:
            if task["status"] == "PENDING" and task["task_type"] == kwargs["task_type"]:
                task["status"] = "SUPERSEDED"
        task = {**kwargs, "task_id": f"new-{len(state.tasks)}", "status": "PENDING", "version": 1}
        state.tasks.append(task)
        return dict(task)

    monkeypatch.setattr(repo, "claim_task", claim)
    monkeypatch.setattr(repo, "release_claimed_task", release)
    monkeypatch.setattr(repo, "complete_claimed_task", complete)
    monkeypatch.setattr(repo, "supersede_inactive_tasks", supersede)
    monkeypatch.setattr(repo, "replace_pending_task", replace)
    monkeypatch.setattr(service, "_create_notification_safely", Mock())
    monkeypatch.setattr(service, "_delete_case_notifications_safely", Mock())
    monkeypatch.setattr(service, "_with_server_side_readiness", lambda answer, case, **kw: answer)
    state.claim = claim
    return state


def answer():
    return service.resume_task("old-task", answer={"decision": "finalize", "supplier": "Test supplier"},
                               answered_by="tester", expected_version=1)


def assert_failed_at_pr(rig):
    assert rig.case["status"] == "FAILED"
    assert rig.case["stage"] == "HUMAN_REVIEW"
    assert BLOCKED in rig.case["last_error"]
    assert rig.case["workflow_snapshot"]["next"] == ["create_pr"]
    assert rig.case["workflow_snapshot"]["values"]["status"] == "creating_pr"
    assert not any(t["status"] in {"PENDING", "PROCESSING"} for t in rig.tasks)
    assert rig.executions == 1  # No replay of mail sending during repair.


def test_sync_answer_failure_projects_actual_checkpoint(rig):
    with pytest.raises(RuntimeError, match="ALLOWLIST"):
        answer()
    assert_failed_at_pr(rig)
    assert rig.tasks[0]["status"] == "SUPERSEDED"


def test_adjacent_final_selection_failure_also_repairs(rig):
    rig.two_interrupts = True
    with pytest.raises(RuntimeError, match="ALLOWLIST"):
        answer()
    assert_failed_at_pr(rig)
    assert rig.tasks[0]["status"] == "COMPLETED"
    assert rig.tasks[1]["status"] == "SUPERSEDED"


def test_async_auto_failure_does_not_restore_old_stage(rig):
    rig.claim("old-task", expected_version=1)
    service._run_queued_quotation_analysis("old-task", answer={"decision": "auto"},
        answered_by="tester", claimed_version=2, case_id="case-178", stage="QUOTATION_COLLECTION")
    assert_failed_at_pr(rig)


def fail_checkpoint_without_projection(rig):
    with pytest.raises(RuntimeError, match="ALLOWLIST"):
        rig.app.invoke(Command(resume={"decision": "finalize"}), rig.config)


def test_stale_button_reconciles_without_consuming_another_answer(rig, monkeypatch):
    fail_checkpoint_without_projection(rig)
    invoke = Mock(side_effect=AssertionError("Must not execute graph"))
    monkeypatch.setattr(rig.app, "invoke", invoke)
    with pytest.raises(ValueError, match="동기화했습니다"):
        answer()
    invoke.assert_not_called()
    assert_failed_at_pr(rig)


def test_periodic_resync_recovers_legacy_stale_waiting_row(rig):
    fail_checkpoint_without_projection(rig)
    assert case_recovery.resync_waiting_cases() == {"checked": 1, "resynced": 1}
    assert_failed_at_pr(rig)
    count = len(rig.transitions)
    assert case_recovery.resync_waiting_cases()["resynced"] == 0
    assert len(rig.transitions) == count


def test_failure_before_advance_keeps_real_human_wait(rig, monkeypatch):
    monkeypatch.setattr(rig.app, "invoke", Mock(side_effect=RuntimeError("before advance")))
    with pytest.raises(RuntimeError, match="before advance"):
        answer()
    assert rig.case["status"] == "WAITING_INPUT"
    assert rig.case["stage"] == "QUOTATION_COLLECTION"
    assert [t["task_type"] for t in rig.tasks if t["status"] == "PENDING"] == ["check_quotations"]
    assert rig.executions == 0


@pytest.mark.parametrize("values", [{}, {"case_id": "other-case"}, {"mr_name": "other-mr"}])
def test_absent_or_foreign_checkpoint_is_not_projected(rig, monkeypatch, values):
    monkeypatch.setattr(rig.app, "get_state", lambda config: SimpleNamespace(values=values, next=(), tasks=()))
    assert not case_recovery.reconcile_failed_action("case-178", error="mismatch", actor="tester")
    assert rig.transitions == []
    assert rig.tasks[0]["status"] == "PENDING"


def test_terminal_case_is_never_resurrected(rig):
    rig.case["status"] = "CANCELLED"
    assert not case_recovery.reconcile_failed_action("case-178", error="mismatch", actor="tester")
    assert rig.transitions == []


def test_pr_legacy_recovery_cannot_delete_a_newer_checkpoint(rig, monkeypatch):
    fail_checkpoint_without_projection(rig)
    rig.tasks[0]["task_type"] = "pr_request"
    rig.case.update(stage="PR_REQUEST", workflow_snapshot={"interrupts": [{"type": "pr_request"}]})
    delete = Mock(side_effect=AssertionError("Cannot delete progressed checkpoint"))
    monkeypatch.setattr(service, "delete_thread_checkpoints", delete)
    with pytest.raises(ValueError, match="동기화했습니다"):
        answer()
    delete.assert_not_called()
    assert_failed_at_pr(rig)


def test_secondary_projection_error_does_not_hide_original_failure(rig, monkeypatch):
    monkeypatch.setattr(case_recovery, "reconcile_failed_action", Mock(side_effect=RuntimeError("DB unavailable")))
    with pytest.raises(RuntimeError, match="ALLOWLIST"):
        answer()
    assert rig.tasks[0]["status"] == "PENDING"


def test_resync_skips_busy_graph_lane(rig, monkeypatch):
    lock = Mock()
    lock.acquire.return_value = False
    monkeypatch.setattr(service, "_GRAPH_LOCK", lock)
    assert case_recovery.resync_waiting_cases() == {"checked": 0, "resynced": 0}
    lock.release.assert_not_called()
    assert rig.transitions == []
