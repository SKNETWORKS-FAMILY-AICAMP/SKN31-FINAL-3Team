"""그래프 실행 경로 - 자동화 v2의 2단계.

이 파일이 지키는 것은 전부 v1에서 **실제로 터진 것**이다.

  - 그래프를 요청/배경작업/이벤트루프에서 기다리면 각각 504, 로그인 멈춤,
    서버 전체 먹통이 났다. 그래서 전용 스레드 하나에서만 돈다.
  - 기다리는 잠금은 스캔 전체를 세우고 그 뒤 스캔까지 연쇄로 막았다.
    그래서 케이스 잠금은 절대 기다리지 않는다.
  - RUNNING 표시만 보고 막았더니 표시가 남은 케이스가 영구히 잠겼다.
    그래서 실제로 도는지를 따로 추적한다.
  - dict_row 연결에서 fetchone()[0]은 KeyError(0)이다. 그것 때문에 마감
    스캔이 한 번도 돌지 못했는데, 가짜 연결이 튜플을 돌려줘서 테스트는
    통과했다. 그래서 가짜도 진짜처럼 딕셔너리를 돌려준다.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from backend_logic2.services import case_recovery, graph_worker


def _drain() -> None:
    graph_worker._EXECUTOR.submit(lambda: None).result(timeout=10)


@pytest.fixture(autouse=True)
def _clean():
    graph_worker._IN_FLIGHT.clear()
    yield
    _drain()
    graph_worker._IN_FLIGHT.clear()


# ---------------------------------------------------------------------------
# 전용 스레드
# ---------------------------------------------------------------------------


def test_graph_work_runs_on_one_dedicated_thread() -> None:
    seen: list[str] = []

    for _ in range(3):
        graph_worker.submit(lambda: seen.append(threading.current_thread().name))
    _drain()

    assert len(seen) == 3
    assert len(set(seen)) == 1, "그래프가 여러 스레드에서 동시에 돌면 안 된다"
    assert seen[0].startswith("biddingflow-graph")


def test_submitting_returns_before_the_work_finishes() -> None:
    """예약한 쪽은 기다리지 않는다 - 기다리면 60초 제한에 걸린다."""
    started = threading.Event()
    release = threading.Event()

    graph_worker.submit(lambda: (started.set(), release.wait(timeout=10)))
    started.wait(timeout=10)

    assert started.is_set()
    release.set()
    _drain()


def test_one_failure_does_not_stop_the_queue() -> None:
    done: list[bool] = []

    graph_worker.submit(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    graph_worker.submit(lambda: done.append(True))
    _drain()

    assert done == [True]


def test_run_and_wait_does_not_deadlock_on_itself() -> None:
    """전용 스레드 위에서 다시 부르면 그대로 실행한다(스레드가 하나뿐이다)."""

    def inner():
        return graph_worker.run_and_wait(lambda: threading.current_thread().name)

    name = graph_worker.submit(inner).result(timeout=10)

    assert name.startswith("biddingflow-graph")


# ---------------------------------------------------------------------------
# 실제로 도는 중인지 추적
# ---------------------------------------------------------------------------


def test_a_case_is_only_in_flight_while_it_runs() -> None:
    assert graph_worker.is_case_in_flight("CASE-1") is False
    with graph_worker.case_in_flight("CASE-1"):
        assert graph_worker.is_case_in_flight("CASE-1") is True
    assert graph_worker.is_case_in_flight("CASE-1") is False


def test_a_crash_still_clears_the_in_flight_mark() -> None:
    """이게 안 지워지면 그 케이스는 영구히 '처리 중'으로 막힌다."""
    with pytest.raises(RuntimeError):
        with graph_worker.case_in_flight("CASE-1"):
            raise RuntimeError("boom")

    assert graph_worker.is_case_in_flight("CASE-1") is False


# ---------------------------------------------------------------------------
# 케이스 잠금 - 절대 기다리지 않는다
# ---------------------------------------------------------------------------


class _FakeConnection:
    """진짜 연결처럼 **딕셔너리** 행을 돌려준다.

    ⚠️ get_connection은 row_factory=dict_row다. v1의 가짜 연결은 튜플을 돌려줘서,
    실제로는 KeyError(0)으로 죽는 fetchone()[0] 코드가 테스트를 통과했다.
    그 탓에 마감 스캔이 한 번도 돌지 못한 걸 아무도 못 잡았다.
    """

    def __init__(self, acquired: bool):
        self.acquired = acquired
        self.statements: list[str] = []

    def execute(self, sql, params=None):
        self.statements.append(sql)
        columns = [part.strip() for part in sql.upper().split(" AS ")[1:]]
        name = columns[0].lower() if columns else "?column?"
        return SimpleNamespace(fetchone=lambda: {name: self.acquired})


@contextmanager
def _fake_connection(connection):
    yield connection


def test_case_lock_reads_its_result_the_way_the_real_driver_returns_it(monkeypatch):
    connection = _FakeConnection(acquired=True)
    monkeypatch.setattr(
        graph_worker, "get_connection", lambda **kw: _fake_connection(connection)
    )

    with graph_worker.case_lock("CASE-1"):
        pass

    assert "pg_advisory_unlock" in connection.statements[-1]


def test_case_lock_gives_up_immediately_when_someone_else_holds_it(monkeypatch):
    """기다리는 잠금이면 스캔 전체가 그 앞에서 멈춘다."""
    connection = _FakeConnection(acquired=False)
    monkeypatch.setattr(
        graph_worker, "get_connection", lambda **kw: _fake_connection(connection)
    )

    with pytest.raises(graph_worker.CaseBusy):
        with graph_worker.case_lock("CASE-1"):
            pytest.fail("잠금을 못 잡았는데 들어오면 안 된다")

    assert all("try_advisory_lock" in sql for sql in connection.statements)
    assert not any("unlock" in sql for sql in connection.statements)


def test_scalar_refuses_an_empty_result():
    with pytest.raises(RuntimeError):
        graph_worker._scalar(SimpleNamespace(fetchone=lambda: None), "acquired")


# ---------------------------------------------------------------------------
# 재시작 복구 · 대기 단계 재동기화
# ---------------------------------------------------------------------------


def _case(**overrides):
    case = {
        "case_id": "CASE-1",
        "mr_name": "MAT-MR-2026-00150",
        "thread_id": "MAT-MR-2026-00150",
        "status": "WAITING_INPUT",
        "stage": "SUPPLIER_RECOMMENDATION",
    }
    case.update(overrides)
    return case


@pytest.fixture
def recovery(monkeypatch):
    from backend_logic2.services import workflow_service
    from backend_logic2.workflow import process_graph

    state = {
        "cases": [_case()],
        "processing": [],
        "local": {"CASE-1"},
        "interrupts": [{"type": "select_rfq_targets"}],
        "next": ("select_rfq_targets",),
        "released": [],
        "transitions": [],
        "projected": [],
    }
    monkeypatch.setattr(
        case_recovery.case_repository, "list_open_case_references",
        lambda: state["cases"],
    )
    monkeypatch.setattr(
        case_recovery.case_repository, "get_case",
        lambda case_id: next((c for c in state["cases"] if c["case_id"] == case_id), None),
    )
    monkeypatch.setattr(
        case_recovery.case_repository, "transition_case",
        lambda case_id, **kw: state["transitions"].append((case_id, kw)) or {},
    )
    monkeypatch.setattr(
        case_recovery.task_repository, "list_tasks",
        lambda status="PENDING", **kw: state["processing"] if status == "PROCESSING" else [],
    )
    monkeypatch.setattr(
        case_recovery.task_repository, "release_claimed_task",
        lambda task_id, claimed_version: state["released"].append(task_id) or True,
    )
    monkeypatch.setattr(
        case_recovery, "has_local_checkpoint",
        lambda case: case["case_id"] in state["local"],
    )
    monkeypatch.setattr(
        process_graph, "get_process_app",
        lambda: SimpleNamespace(
            get_state=lambda config: SimpleNamespace(
                next=state["next"], values={"status": "resolving_supplier_pool"}
            )
        ),
    )
    monkeypatch.setattr(
        workflow_service, "project_case_from_checkpoint",
        lambda case_id: state["projected"].append(case_id)
        or {"status": "WAITING_INPUT", "stage": "RFQ_TARGET_SELECTION"},
    )
    monkeypatch.setattr(
        workflow_service, "_interrupt_payloads", lambda snapshot: state["interrupts"]
    )
    return state


def test_a_waiting_case_with_a_stale_stage_is_corrected(recovery):
    """⚠️ 실제로 겪은 것 - 기존 협력사 풀로 충분한 건이 'RFQ 대상 선택'에서
    사람을 기다리는데 화면은 '협력사 탐색'으로 보고 버튼을 막았다."""
    counts = case_recovery.resync_waiting_cases()

    assert counts["resynced"] == 1
    case_id, change = recovery["transitions"][-1]
    assert case_id == "CASE-1"
    assert change["status"] == "WAITING_INPUT"
    assert change["stage"] == "RFQ_TARGET_SELECTION"


def test_a_case_already_in_step_is_left_alone(recovery):
    """같은 값을 다시 쓰면 이력만 의미 없이 불어난다."""
    recovery["cases"] = [_case(stage="RFQ_TARGET_SELECTION")]

    counts = case_recovery.resync_waiting_cases()

    assert counts["resynced"] == 0
    assert recovery["transitions"] == []


def test_a_case_that_is_not_waiting_is_left_alone(recovery):
    """돌고 있는 중에는 건드리지 않는다."""
    recovery["interrupts"] = []

    counts = case_recovery.resync_waiting_cases()

    assert counts["resynced"] == 0
    assert recovery["transitions"] == []


def test_another_instances_case_is_never_touched(recovery):
    """체크포인트는 인스턴스마다 따로다. 없는 쪽이 건드리면 처음부터 다시 돈다."""
    recovery["local"] = set()

    counts = case_recovery.resync_waiting_cases()

    assert counts["checked"] == 0
    assert recovery["transitions"] == []


def test_a_claim_cut_off_by_a_restart_can_be_answered_again(recovery):
    recovery["processing"] = [{"task_id": "T-1", "case_id": "CASE-1", "version": 4}]

    counts = case_recovery.recover_interrupted_work()

    assert recovery["released"] == ["T-1"]
    assert counts["tasks_released"] == 1
    _, change = recovery["transitions"][-1]
    assert "재시작" in change["last_error"]


def test_a_case_cut_off_mid_node_becomes_retryable(recovery):
    """사람을 기다리는 지점이 아니면 FAILED로 - '다시 시도'가 그 지점부터 잇는다."""
    recovery["cases"] = [_case(status="RUNNING")]
    recovery["interrupts"] = []

    case_recovery.recover_interrupted_work()

    _, change = recovery["transitions"][-1]
    assert change["status"] == "FAILED"
    assert change["stage"] == "HUMAN_REVIEW"


def test_a_restart_leaves_a_human_waiting_point_where_it_is(recovery):
    """사람 답을 기다리던 건을 FAILED로 떨어뜨리면 멀쩡한 건을 망친다."""
    recovery["cases"] = [_case(status="RUNNING")]

    case_recovery.recover_interrupted_work()

    _, change = recovery["transitions"][-1]
    assert change["status"] == "WAITING_INPUT"
    assert change["stage"] == "RFQ_TARGET_SELECTION"


def test_another_instances_unfinished_case_is_not_settled(recovery):
    recovery["cases"] = [_case(status="RUNNING")]
    recovery["local"] = set()

    counts = case_recovery.recover_interrupted_work()

    assert counts["cases_settled"] == 0
    assert recovery["transitions"] == []
