"""그래프는 전용 스레드 하나에서만 돈다 - 진입점마다 확인한다.

왜 이 파일이 있나: 자동 진행을 켠 뒤로 같은 계열의 장애가 연달아 났다.
원인은 전부 하나였다 - 그래프(몇 분짜리)를 요청·폴러·스케줄러·async 라우트가
**직접** 돌리면서 _GRAPH_LOCK과 케이스 잠금 앞에 서로 줄을 선 것.

  - '자동 진행 지금 확인'을 누르면 판정이 요청 안에서 돌아 60초 뒤 504
  - 그 사이 그래프가 케이스 잠금을 쥐고 있어 마감 스캔이 그 앞에서 멈추고,
    잡 전체 잠금까지 쥔 채라 이후 스캔도 전부 건너뛰어짐(마감이 지나도 자동
    처리가 안 됨)
  - 협력사 수주 응답은 async 라우트에서 잠금을 잡아 서버 전체를 멈출 수 있었음

진입점이 늘어나면 같은 실수를 또 하기 쉬워서, 경로마다 "직접 돌리지 않고
예약만 한다"를 못 박는다.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from backend_logic2.policies.schema import CompanyPolicy
from backend_logic2.services import auto_progress_runner as runner
from backend_logic2.services import workflow_service


def _policy(**rules):
    base = CompanyPolicy()
    return base.model_copy(update={"rules": base.rules.model_copy(update=rules)})


def _case(**overrides):
    case = {
        "case_id": "CASE-1",
        "mr_name": "MAT-MR-2026-00120",
        "thread_id": "MAT-MR-2026-00120",
        "stage": "QUOTATION_COLLECTION",
        "status": "WAITING_INPUT",
        "quotation_deadline_at": datetime.now(timezone.utc) + timedelta(hours=2),
        "quotation_snapshot": {"recipient_count": 2, "responded_count": 1},
        "workflow_snapshot": {"values": {"rfq_name": "PUR-RFQ-1"}},
        "automation_hold": False,
        "auto_progress_signature": None,
        "auto_deadline_extended_at": None,
    }
    case.update(overrides)
    return case


def _drain_graph_worker() -> None:
    """전용 스레드에 쌓인 일이 다 끝날 때까지 기다린다."""
    workflow_service._GRAPH_EXECUTOR.submit(lambda: None).result(timeout=10)


@pytest.fixture(autouse=True)
def _clean_queue():
    runner._PENDING.clear()
    runner._LAST_LOGGED.clear()
    runner._LAST_ERROR.clear()
    yield
    _drain_graph_worker()
    runner._PENDING.clear()


# ---------------------------------------------------------------------------
# '자동 진행 지금 확인'
# ---------------------------------------------------------------------------


@pytest.fixture
def manual(monkeypatch):
    state = {"policy": _policy(automation_mode="on"), "enqueued": []}
    monkeypatch.setattr(runner, "_policy_for", lambda case: state["policy"])

    def fake_enqueue(case_id, **kwargs):
        if any(row[0] == case_id for row in state["enqueued"]):
            return False
        state["enqueued"].append((case_id, kwargs))
        return True

    monkeypatch.setattr(runner, "enqueue_case", fake_enqueue)
    return state


def test_manual_check_never_runs_the_graph_inside_the_request(manual, monkeypatch):
    """판정은 예약만 하고 즉시 돌아온다 - 요청 안에서 process_case를 부르지 않는다."""
    monkeypatch.setattr(
        runner, "process_case",
        lambda *a, **k: pytest.fail("요청 안에서 그래프를 돌리면 60초 제한에 걸린다"),
    )
    case = _case(quotation_deadline_at=datetime.now(timezone.utc) - timedelta(minutes=10))

    result = runner.request_manual_check(case)

    assert result["outcome"] == "queued"
    assert result["queued"] is True
    assert manual["enqueued"] == [
        ("CASE-1", {"trigger": "deadline", "force": True, "manual": True})
    ]


def test_manual_check_before_deadline_does_not_pretend_the_deadline_passed(manual):
    """⚠️ 그래프는 trigger가 all_responded가 아니면 마감이 지났다고 믿는다.

    예전엔 '지금 확인'이 무조건 deadline을 넘겨서, 마감 전에 누르면 마감이
    지난 것처럼 자동 선정될 수 있었다.
    """
    result = runner.request_manual_check(_case())

    assert result["outcome"] == "not_due"
    assert manual["enqueued"] == []


def test_manual_check_when_everyone_responded_uses_that_as_the_reason(manual):
    case = _case(quotation_snapshot={"recipient_count": 2, "responded_count": 2})

    result = runner.request_manual_check(case)

    assert result["outcome"] == "queued"
    assert manual["enqueued"][0][1]["trigger"] == "all_responded"


def test_pressing_twice_does_not_queue_twice(manual):
    case = _case(quotation_deadline_at=datetime.now(timezone.utc) - timedelta(minutes=1))

    first = runner.request_manual_check(case)
    second = runner.request_manual_check(case)

    assert first["outcome"] == "queued"
    assert second["outcome"] == "already_queued"
    assert len(manual["enqueued"]) == 1


@pytest.mark.parametrize(
    ("overrides", "policy", "expected"),
    [
        ({"automation_hold": True}, "on", "held"),
        ({}, "off", "disabled"),
        ({"stage": "SUPPLIER_SELECTION"}, "on", "no_task"),
    ],
)
def test_manual_check_answers_obvious_reasons_right_away(manual, overrides, policy, expected):
    manual["policy"] = _policy(automation_mode=policy)

    result = runner.request_manual_check(_case(**overrides))

    assert result["outcome"] == expected
    assert result["message"]
    assert manual["enqueued"] == []


# ---------------------------------------------------------------------------
# 예약 → 전용 스레드
# ---------------------------------------------------------------------------


@pytest.fixture
def recorded(monkeypatch):
    logs: list[tuple[str, str, str]] = []
    import backend_logic2.nodes.supplier.tools.case_logging as case_logging

    monkeypatch.setattr(
        case_logging, "log_ai_decision",
        lambda case_id, node, detail: logs.append((case_id, node, detail)),
    )
    monkeypatch.setattr(runner.case_repository, "get_case", lambda case_id: _case())
    return logs


def test_queued_work_runs_on_the_single_graph_thread(recorded, monkeypatch):
    seen: list[str] = []

    def fake_process(case, **kwargs):
        seen.append(threading.current_thread().name)
        return "advanced"

    monkeypatch.setattr(runner, "process_case", fake_process)

    assert runner.enqueue_case("CASE-1", trigger="deadline") is True
    _drain_graph_worker()

    assert seen and seen[0].startswith("biddingflow-graph")
    # 끝나면 예약 표시가 풀려서 다음 판정을 받을 수 있다.
    assert "CASE-1" not in runner._PENDING


def test_a_case_is_never_queued_twice_while_it_waits(recorded, monkeypatch):
    release = threading.Event()
    calls: list[str] = []

    def slow_process(case, **kwargs):
        calls.append(case["case_id"])
        release.wait(timeout=10)
        return "advanced"

    monkeypatch.setattr(runner, "process_case", slow_process)

    assert runner.enqueue_case("CASE-1", trigger="deadline") is True
    assert runner.enqueue_case("CASE-1", trigger="all_responded") is False
    release.set()
    _drain_graph_worker()

    assert calls == ["CASE-1"]


def test_manual_outcome_is_always_written_where_the_buyer_can_read_it(recorded, monkeypatch):
    """서버 로그를 못 봐도 '왜 안 넘어가지'의 답이 화면(AI 판단 기록)에 남는다."""
    monkeypatch.setattr(runner, "process_case", lambda case, **k: "unchanged")

    runner.enqueue_case("CASE-1", trigger="deadline", force=True, manual=True)
    _drain_graph_worker()

    assert recorded and recorded[-1][1] == "auto_progress_scan"
    assert recorded[-1][2].startswith("[지금 확인]")


def test_a_failure_reason_reaches_the_record(recorded, monkeypatch):
    def boom(case, **kwargs):
        raise RuntimeError("ERPNext 응답이 없습니다")

    monkeypatch.setattr(runner, "process_case", boom)

    runner.enqueue_case("CASE-1", trigger="deadline")
    _drain_graph_worker()

    assert "ERPNext 응답이 없습니다" in recorded[-1][2]
    assert "CASE-1" not in runner._PENDING


def test_repeated_automatic_outcomes_are_not_spammed(recorded, monkeypatch):
    monkeypatch.setattr(runner, "process_case", lambda case, **k: "not_mine")

    for _ in range(3):
        runner.enqueue_case("CASE-1", trigger="deadline")
        _drain_graph_worker()

    assert len(recorded) == 1


# ---------------------------------------------------------------------------
# 스캔·전원 회신은 예약만 한다
# ---------------------------------------------------------------------------


def test_the_deadline_scan_only_queues_and_never_runs_the_graph(monkeypatch):
    @contextmanager
    def free_scan_lock():
        yield True

    queued: list[tuple[str, str]] = []
    monkeypatch.setattr(runner, "_scan_lock", free_scan_lock)
    monkeypatch.setattr(
        runner, "process_case",
        lambda *a, **k: pytest.fail("스캔 스레드가 그래프를 직접 돌리면 뒤 스캔이 전부 막힌다"),
    )
    monkeypatch.setattr(
        runner, "enqueue_case",
        lambda case_id, **k: queued.append((case_id, k["trigger"])) or True,
    )
    monkeypatch.setattr(runner.case_repository, "record_automation_scan", lambda *a, **k: None)
    past = datetime.now(timezone.utc) - timedelta(minutes=10)
    monkeypatch.setattr(
        runner.case_repository,
        "list_cases_for_quotation_reconciliation",
        lambda: [
            _case(case_id="DUE", quotation_deadline_at=past),
            _case(case_id="NOT-YET"),
            _case(case_id="OTHER-STAGE", stage="ORDER_START", quotation_deadline_at=past),
        ],
    )

    counts = runner.run_due_auto_progress()

    assert queued == [("DUE", "deadline")]
    assert counts["queued"] == 1


def test_full_response_only_queues(monkeypatch):
    queued: list[tuple[str, str]] = []
    monkeypatch.setattr(
        runner.case_repository, "get_case",
        lambda case_id: _case(quotation_snapshot={"recipient_count": 2, "responded_count": 2}),
    )
    monkeypatch.setattr(
        runner, "process_case",
        lambda *a, **k: pytest.fail("웹훅·폴러 스레드에서 그래프를 돌리면 안 된다"),
    )
    monkeypatch.setattr(
        runner, "enqueue_case",
        lambda case_id, **k: queued.append((case_id, k["trigger"])) or True,
    )

    assert runner.trigger_on_full_response("CASE-1") == "queued"
    assert queued == [("CASE-1", "all_responded")]


# ---------------------------------------------------------------------------
# 케이스 잠금은 절대 기다리지 않는다
# ---------------------------------------------------------------------------


class _FakeConnection:
    def __init__(self, acquired: bool):
        self.acquired = acquired
        self.statements: list[str] = []

    def execute(self, sql, params=None):
        self.statements.append(sql)
        return SimpleNamespace(fetchone=lambda: (self.acquired,))


def test_case_lock_gives_up_immediately_when_someone_else_holds_it(monkeypatch):
    """예전엔 기다리는 잠금이라, 앞 실행이 몇 분 걸리면 스캔이 그 앞에서 멈췄다."""
    connection = _FakeConnection(acquired=False)

    @contextmanager
    def fake_get_connection(**kwargs):
        yield connection

    monkeypatch.setattr(runner, "get_connection", fake_get_connection)

    with pytest.raises(runner.CaseBusy):
        with runner.case_lock("CASE-1"):
            pytest.fail("잠금을 못 잡았는데 안으로 들어오면 안 된다")
    assert all("pg_try_advisory_lock" in sql for sql in connection.statements)


def test_case_lock_releases_what_it_took(monkeypatch):
    connection = _FakeConnection(acquired=True)

    @contextmanager
    def fake_get_connection(**kwargs):
        yield connection

    monkeypatch.setattr(runner, "get_connection", fake_get_connection)

    with runner.case_lock("CASE-1"):
        pass
    assert "pg_advisory_unlock" in connection.statements[-1]


def test_a_busy_case_is_skipped_not_waited_on(monkeypatch):
    @contextmanager
    def busy(case_id):
        raise runner.CaseBusy(case_id)
        yield  # pragma: no cover

    monkeypatch.setattr(runner, "case_lock", busy)
    monkeypatch.setattr(runner, "_policy_for", lambda case: _policy(automation_mode="on"))
    monkeypatch.setattr(runner, "has_local_checkpoint", lambda case: True)
    monkeypatch.setattr(runner, "_extend_deadline_for_silence", lambda case, policy: False)
    monkeypatch.setattr(
        runner, "_pending_quotation_task",
        lambda case_id: {"task_id": "T-1", "version": 1},
    )

    assert runner.process_case(_case(), force=True) == "busy"


def test_an_instance_without_the_checkpoint_does_not_extend_the_deadline(monkeypatch):
    """마감 연장도 이 건의 진행 상황을 가진 인스턴스만 한다."""
    monkeypatch.setattr(runner, "_policy_for", lambda case: _policy(automation_mode="on"))
    monkeypatch.setattr(runner, "has_local_checkpoint", lambda case: False)
    monkeypatch.setattr(
        runner, "_extend_deadline_for_silence",
        lambda *a: pytest.fail("진행 상황이 없는 인스턴스가 마감을 건드리면 안 된다"),
    )

    assert runner.process_case(_case()) == "not_mine"


# ---------------------------------------------------------------------------
# ERPNext 대체품 결정 라우트
# ---------------------------------------------------------------------------


@pytest.fixture
def substitute_route(monkeypatch):
    from backend_logic2.api import mr_substitute_routes as routes

    state = {
        "values": {
            "status": "awaiting_substitute_selection",
            "substitute_results": {},
        },
        "submitted": [],
    }
    fake_app = SimpleNamespace(
        get_state=lambda config: SimpleNamespace(values=state["values"]),
        invoke=lambda *a, **k: pytest.fail("라우트 안에서 그래프를 돌리면 안 된다"),
    )
    monkeypatch.setattr(routes, "get_process_app", lambda: fake_app)
    monkeypatch.setattr(routes, "_resolve_thread_id", lambda mr_name: mr_name)
    monkeypatch.setattr(
        routes, "flatten_substitute_candidates",
        lambda results: [{"item_code": "ITEM-OK", "is_original_item": False}],
    )
    monkeypatch.setattr(
        workflow_service, "submit_graph_work",
        lambda work, *a, **k: state["submitted"].append((work.__name__, a, k)),
    )
    return routes, state


def test_substitute_decision_is_queued_not_run_in_the_request(substitute_route):
    routes, state = substitute_route
    body = routes.SubstituteDecisionRequest(item_code="ITEM-OK")

    result = routes.submit_substitute_decision("MAT-MR-1", body)

    assert result["success"] is True
    assert result["status"] == "processing"
    assert [row[0] for row in state["submitted"]] == ["_apply_substitute_decision"]


def test_an_invalid_substitute_is_rejected_before_anything_runs(substitute_route):
    routes, state = substitute_route
    body = routes.SubstituteDecisionRequest(item_code="NOT-A-CANDIDATE")

    result = routes.submit_substitute_decision("MAT-MR-1", body)

    assert result["success"] is False
    assert state["submitted"] == []


def test_new_purchase_is_accepted(substitute_route):
    routes, state = substitute_route
    body = routes.SubstituteDecisionRequest(decision="new_purchase")

    result = routes.submit_substitute_decision("MAT-MR-1", body)

    assert result["success"] is True
    assert state["submitted"][0][2]["new_purchase"] is True


def test_a_decision_after_the_fact_is_refused(substitute_route):
    routes, state = substitute_route
    state["values"] = {"status": "checking_bidding"}

    result = routes.submit_substitute_decision(
        "MAT-MR-1", routes.SubstituteDecisionRequest(decision="new_purchase")
    )

    assert result["success"] is False
    assert state["submitted"] == []


# ---------------------------------------------------------------------------
# 협력사 수주 응답(async 라우트)
# ---------------------------------------------------------------------------


def test_supplier_response_never_touches_the_graph_on_the_event_loop(monkeypatch):
    """async 라우트에서 그래프 잠금을 잡으면 서버 전체가 멈춘다."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend_logic2.pr import routes as pr_routes

    submitted: list[str] = []
    monkeypatch.setattr(
        pr_routes.service, "respond",
        lambda token, decision, reason: {"case_id": "CASE-1", "pr_id": "PR-1", "status": "ACCEPTED"},
    )
    monkeypatch.setattr(
        workflow_service, "check_supplier_pr_response_ready", lambda **kwargs: None
    )
    monkeypatch.setattr(
        workflow_service, "resume_supplier_pr_response",
        lambda **kwargs: pytest.fail("요청 처리 중에 그래프를 돌리면 안 된다"),
    )
    monkeypatch.setattr(
        workflow_service, "submit_graph_work",
        lambda work, *a, **k: submitted.append(work.__name__),
    )
    app = FastAPI()
    app.include_router(pr_routes.public_router)

    response = TestClient(app).post(
        "/api/public/pr/respond/TOKEN",
        content="decision=accept&reason=",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 200
    assert "수주 접수 완료" in response.text
    assert submitted == ["_apply_supplier_response"]


def test_supplier_response_that_no_longer_fits_is_explained(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend_logic2.pr import routes as pr_routes

    errors: list[str] = []
    monkeypatch.setattr(
        pr_routes.service, "respond",
        lambda token, decision, reason: {"case_id": "CASE-1", "pr_id": "PR-1", "status": "ACCEPTED"},
    )

    def not_ready(**kwargs):
        raise ValueError("현재 구매 건은 공급사 PR 응답 대기 상태가 아닙니다.")

    monkeypatch.setattr(workflow_service, "check_supplier_pr_response_ready", not_ready)
    monkeypatch.setattr(
        pr_routes.repository, "record_processing_error",
        lambda pr_id, *, stage, error: errors.append(stage),
    )
    monkeypatch.setattr(
        workflow_service, "submit_graph_work",
        lambda *a, **k: pytest.fail("상태가 안 맞으면 예약하지 않는다"),
    )
    app = FastAPI()
    app.include_router(pr_routes.public_router)

    response = TestClient(app).post(
        "/api/public/pr/respond/TOKEN",
        content="decision=accept&reason=",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 409
    assert errors == ["validation"]


# ---------------------------------------------------------------------------
# 재시작 복구
# ---------------------------------------------------------------------------


@pytest.fixture
def recovery(monkeypatch):
    state = {
        "processing": [],
        "running": [],
        "queued": [],
        "local": {"CASE-LOCAL"},
        "released": [],
        "projected": [],
        "transitions": [],
        "snapshot": SimpleNamespace(next=("select_rfq_targets",), values={"status": "x"}),
        "interrupts": [{"type": "select_rfq_targets"}],
    }
    monkeypatch.setattr(
        workflow_service.task_repository, "list_tasks",
        lambda status="PENDING", **k: state["processing"] if status == "PROCESSING" else [],
    )
    monkeypatch.setattr(
        workflow_service.task_repository, "release_claimed_task",
        lambda task_id, claimed_version: state["released"].append(task_id) or True,
    )

    def list_cases(status=None, **kwargs):
        return {"RUNNING": state["running"], "QUEUED": state["queued"]}.get(status, [])

    monkeypatch.setattr(workflow_service.case_repository, "list_cases", list_cases)
    monkeypatch.setattr(
        workflow_service.case_repository, "get_case",
        lambda case_id: _case(case_id=case_id, thread_id=case_id),
    )
    monkeypatch.setattr(
        workflow_service.case_repository, "transition_case",
        lambda case_id, **kwargs: state["transitions"].append((case_id, kwargs)) or {},
    )
    monkeypatch.setattr(
        runner, "has_local_checkpoint", lambda case: case["case_id"] in state["local"]
    )
    monkeypatch.setattr(
        workflow_service, "project_case_from_checkpoint",
        lambda case_id: state["projected"].append(case_id)
        or {"status": "WAITING_INPUT", "stage": "RFQ_TARGET_SELECTION"},
    )
    monkeypatch.setattr(
        workflow_service, "get_process_app",
        lambda: SimpleNamespace(get_state=lambda config: state["snapshot"]),
    )
    monkeypatch.setattr(
        workflow_service, "_interrupt_payloads", lambda snapshot: state["interrupts"]
    )
    return state


def test_a_claim_cut_off_by_a_restart_can_be_answered_again(recovery):
    recovery["processing"] = [{"task_id": "T-1", "case_id": "CASE-LOCAL", "version": 4}]

    counts = workflow_service.recover_interrupted_work()

    assert recovery["released"] == ["T-1"]
    assert recovery["projected"] == ["CASE-LOCAL"]
    assert counts["tasks_released"] == 1
    case_id, change = recovery["transitions"][-1]
    assert change["status"] == "WAITING_INPUT"
    assert "재시작" in change["last_error"]


def test_a_case_cut_off_mid_node_becomes_retryable(recovery):
    """사람을 기다리는 지점이 아니면 FAILED로 둔다 - '다시 시도'가 체크포인트부터 잇는다."""
    recovery["running"] = [_case(case_id="CASE-LOCAL", status="RUNNING")]
    recovery["interrupts"] = []

    workflow_service.recover_interrupted_work()

    case_id, change = recovery["transitions"][-1]
    assert change["status"] == "FAILED"
    assert change["stage"] == "HUMAN_REVIEW"


def test_another_instances_work_is_left_alone(recovery):
    recovery["processing"] = [{"task_id": "T-9", "case_id": "CASE-ELSEWHERE", "version": 1}]
    recovery["running"] = [_case(case_id="CASE-ELSEWHERE", status="RUNNING")]

    workflow_service.recover_interrupted_work()

    assert recovery["released"] == []
    assert recovery["transitions"] == []


def test_a_start_that_never_began_goes_back_to_the_start(recovery):
    recovery["queued"] = [
        _case(case_id="CASE-NEW", status="QUEUED", workflow_snapshot={}),
    ]

    counts = workflow_service.recover_interrupted_work()

    case_id, change = recovery["transitions"][-1]
    assert case_id == "CASE-NEW"
    assert change["status"] == "AWAITING_MR_REVIEW"
    assert counts["queued_reverted"] == 1


def test_a_retry_owned_by_another_instance_is_left_alone(recovery):
    recovery["queued"] = [
        _case(
            case_id="CASE-ELSEWHERE",
            status="QUEUED",
            workflow_snapshot={"retry_from_checkpoint": True},
        ),
    ]

    workflow_service.recover_interrupted_work()

    assert recovery["transitions"] == []


def test_run_on_graph_worker_does_not_deadlock_on_itself():
    """전용 스레드 위에서 다시 부르면 그대로 실행한다(자기 자신을 기다리면 영원히 멈춘다)."""

    def inner():
        return workflow_service.run_on_graph_worker(lambda: threading.current_thread().name)

    name = workflow_service.submit_graph_work(inner).result(timeout=10)

    assert name.startswith("biddingflow-graph")
