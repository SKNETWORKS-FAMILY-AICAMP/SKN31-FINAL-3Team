"""마감 판정과 자동 선정 경로 - 자동화 v2의 5단계 c.

⚠️ **주기 스케쥴러는 빼놨다.** 60초마다 도는 스윕이 마감 지난 케이스마다
ERPNext를 N+1로 조회해서 RFQ 전송 버튼이 504로 죽었다. 지금은 사람이 건별로
버튼을 눌러 판정을 시작한다. 스윕은 서버에 접속해서 부하를 볼 수 있을 때
다시 넣는다 - 안 보고 다시 넣으면 같은 일이 반복된다.

이 파일이 지키는 것은 전부 v1에서 **실제로 터진 것**이다.

  - 요청/이벤트루프에서 그래프를 기다리면 504와 서버 먹통이 났다.
  - 기다리는 잠금 하나가 스캔 전체를 세우고 그 뒤 스캔까지 막았다.
  - 판정하면 대기 작업이 새로 생긴다. 막지 않으면 같은 판정을 주기마다
    영원히 반복한다(대체품목 무한 뺑뺑이와 같은 모양).
  - 조건에 걸렸는데 견적을 확정(Submit)해버리면 되돌릴 수 없다.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from backend_logic2.policies.runtime import policy_scope
from backend_logic2.policies.schema import CompanyPolicy
from backend_logic2.services import deadline_readiness, deadline_scan, graph_worker

KST = ZoneInfo("Asia/Seoul")


def _policy(**rules):
    base = CompanyPolicy()
    return base.model_copy(update={"rules": base.rules.model_copy(update=rules)})


def _case(case_id="CASE-1"):
    return {
        "case_id": case_id,
        "mr_name": f"MAT-MR-{case_id}",
        "thread_id": f"MAT-MR-{case_id}",
        "status": "WAITING_INPUT",
        "stage": "QUOTATION_COLLECTION",
        "workflow_snapshot": {"values": {"rfq_name": "PUR-RFQ-0001"}},
        "quotation_snapshot": {"recipient_count": 3, "responded_count": 3},
    }


def _ready(**overrides):
    values = {
        "ready": True,
        "reason": "마감이 지났고 처리가 끝났습니다",
        "deadline": datetime(2026, 10, 2, 18, 0, tzinfo=KST),
        "late": [],
        "pending": [],
        "unknown": [],
        "responded": 3,
        "recipients": 3,
    }
    values.update(overrides)
    return deadline_readiness.Readiness(**values)


@pytest.fixture
def scan(monkeypatch):
    state = {
        "cases": [_case()],
        "local": {"CASE-1"},
        "tasks": [{
            "task_id": "T-1", "case_id": "CASE-1",
            "task_type": "quotation_check", "version": 3,
        }],
        "readiness": _ready(),
        "resumed": [],
        "locked": [],
    }
    deadline_scan._JUDGED.clear()
    monkeypatch.setattr(
        deadline_scan.case_repository, "list_cases_awaiting_quotation_deadline",
        lambda: list(state["cases"]),
    )
    monkeypatch.setattr(
        deadline_scan, "has_local_checkpoint",
        lambda case: str(case["case_id"]) in state["local"],
    )
    monkeypatch.setattr(
        deadline_scan.task_repository, "list_tasks",
        lambda **kw: [
            task for task in state["tasks"]
            if task["case_id"] == kw.get("case_id")
        ],
    )
    monkeypatch.setattr(
        deadline_readiness, "judge",
        lambda case, **kw: state["readiness"],
    )

    from contextlib import contextmanager

    @contextmanager
    def fake_lock(case_id):
        state["locked"].append(case_id)
        yield

    monkeypatch.setattr(graph_worker, "case_lock", fake_lock)
    monkeypatch.setattr(
        "backend_logic2.services.workflow_service.resume_task",
        lambda task_id, **kw: state["resumed"].append((task_id, kw)) or {},
    )
    yield state
    deadline_scan._JUDGED.clear()


# ---------------------------------------------------------------------------
# 판정을 시작한다
# ---------------------------------------------------------------------------


def test_a_due_case_is_resumed_with_the_auto_decision(scan) -> None:
    counts = deadline_scan.scan_due_cases()

    assert counts["judged"] == 1
    task_id, kwargs = scan["resumed"][0]
    assert task_id == "T-1"
    assert kwargs["answer"]["decision"] == "auto"
    assert kwargs["answer"]["ready"] is True
    assert kwargs["expected_version"] == 3


def test_the_late_submissions_travel_to_the_graph(scan) -> None:
    """그래프 안에서 다시 계산하면 ERPNext를 또 뒤지고 그만큼 줄이 막힌다."""
    scan["readiness"] = _ready(late=[{"quotation_id": "SQ-9"}])

    deadline_scan.scan_due_cases()

    assert scan["resumed"][0][1]["answer"]["late"] == [{"quotation_id": "SQ-9"}]


def test_a_case_that_is_not_due_is_left_alone(scan) -> None:
    scan["readiness"] = _ready(ready=False, reason="마감 전입니다")

    counts = deadline_scan.scan_due_cases()

    assert counts["waiting"] == 1
    assert scan["resumed"] == []


# ---------------------------------------------------------------------------
# 같은 상태를 두 번 판정하지 않는다
# ---------------------------------------------------------------------------


def test_the_same_state_is_never_judged_twice(scan) -> None:
    """⚠️ 판정하면 대기 작업이 새로 생긴다. 막지 않으면 주기마다 영원히
    같은 판정을 반복한다."""
    deadline_scan.scan_due_cases()
    counts = deadline_scan.scan_due_cases()

    assert counts["judged"] == 0
    assert len(scan["resumed"]) == 1


def test_a_new_quotation_makes_it_judge_again(scan) -> None:
    deadline_scan.scan_due_cases()
    scan["readiness"] = _ready(unknown=[{"quotation_id": "SQ-NEW"}])

    assert deadline_scan.scan_due_cases()["judged"] == 1
    assert len(scan["resumed"]) == 2


def test_a_moved_deadline_makes_it_judge_again(scan) -> None:
    deadline_scan.scan_due_cases()
    scan["readiness"] = _ready(deadline=datetime(2026, 10, 5, 18, 0, tzinfo=KST))

    assert deadline_scan.scan_due_cases()["judged"] == 1


def test_forgetting_a_case_lets_it_be_judged_again(scan) -> None:
    """재비딩처럼 판을 새로 깔 때 쓴다."""
    deadline_scan.scan_due_cases()
    deadline_scan.forget("CASE-1")

    assert deadline_scan.scan_due_cases()["judged"] == 1


def test_a_case_that_left_the_list_is_forgotten(scan) -> None:
    """기억이 무한히 쌓이면 안 된다."""
    deadline_scan.scan_due_cases()
    scan["cases"] = []

    deadline_scan.scan_due_cases()

    assert "CASE-1" not in deadline_scan._JUDGED


# ---------------------------------------------------------------------------
# 건드리면 안 되는 것
# ---------------------------------------------------------------------------


def test_another_instances_case_is_never_touched(scan) -> None:
    """체크포인트는 인스턴스마다 따로다. 없는 쪽이 건드리면 처음부터 다시 돈다."""
    scan["local"] = set()

    counts = deadline_scan.scan_due_cases()

    assert counts["checked"] == 0
    assert scan["resumed"] == []


def test_a_case_already_running_is_skipped(scan) -> None:
    with graph_worker.case_in_flight("CASE-1"):
        counts = deadline_scan.scan_due_cases()

    assert counts["busy"] == 1
    assert scan["resumed"] == []


def test_a_case_without_a_pending_task_is_skipped(scan) -> None:
    """사람 입력을 기다리는 지점이 아니면 재개할 대상이 아니다."""
    scan["tasks"] = []

    counts = deadline_scan.scan_due_cases()

    assert counts["judged"] == 0
    assert scan["resumed"] == []


def test_a_lock_held_elsewhere_does_not_stall_the_scan(scan, monkeypatch) -> None:
    """⚠️ 기다리는 잠금 하나가 스캔 전체를 세웠다. 절대 기다리지 않는다."""
    from contextlib import contextmanager

    @contextmanager
    def busy(_case_id):
        raise graph_worker.CaseBusy("다른 곳에서 처리 중")
        yield  # pragma: no cover

    monkeypatch.setattr(graph_worker, "case_lock", busy)
    scan["cases"] = [_case(), _case("CASE-2")]
    scan["local"] = {"CASE-1", "CASE-2"}
    scan["tasks"].append({
        "task_id": "T-2", "case_id": "CASE-2",
        "task_type": "quotation_check", "version": 1,
    })

    counts = deadline_scan.scan_due_cases()

    assert counts["busy"] == 2, "잠금을 못 잡았으면 다음 건으로 넘어가야 한다"


def test_one_failing_case_does_not_stop_the_others(scan, monkeypatch) -> None:
    scan["cases"] = [_case(), _case("CASE-2")]
    scan["local"] = {"CASE-1", "CASE-2"}
    scan["tasks"].append({
        "task_id": "T-2", "case_id": "CASE-2",
        "task_type": "quotation_check", "version": 1,
    })
    calls: list[str] = []

    def judge(case, **_kw):
        calls.append(str(case["case_id"]))
        if case["case_id"] == "CASE-1":
            raise RuntimeError("boom")
        return scan["readiness"]

    monkeypatch.setattr(deadline_readiness, "judge", judge)

    counts = deadline_scan.scan_due_cases()

    assert calls == ["CASE-1", "CASE-2"]
    assert counts["judged"] == 1


def test_a_failed_lookup_returns_empty_counts_instead_of_raising(monkeypatch) -> None:
    """⚠️ 스캔 루프가 예외로 죽으면 다시 살아나지 않는다."""
    monkeypatch.setattr(
        deadline_scan.case_repository, "list_cases_awaiting_quotation_deadline",
        lambda: (_ for _ in ()).throw(RuntimeError("db down")),
    )

    assert deadline_scan.scan_due_cases()["judged"] == 0


def test_a_judgment_that_fails_is_not_remembered_as_done(scan, monkeypatch) -> None:
    """실패한 판정을 '했다'고 기억하면 그 케이스는 다시 시도되지 않는다."""
    monkeypatch.setattr(
        "backend_logic2.services.workflow_service.resume_task",
        lambda task_id, **kw: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    deadline_scan.scan_due_cases()

    assert "CASE-1" not in deadline_scan._JUDGED


# ---------------------------------------------------------------------------
# 그래프 안: auto 결정
# ---------------------------------------------------------------------------


def _result(**overrides):
    result = {
        "ranking": [
            {
                "supplier": "동관컴퍼니", "quotation_id": "SQ-1",
                "overall_score": 95.0, "total_amount": "1000000",
                "penalties": [], "valid_till": "2099-12-31",
            },
            {"supplier": "세희상사", "quotation_id": "SQ-2", "overall_score": 70.0},
        ],
        "parse_failed": [],
        "excluded": [],
        "competition_count": 2,
        "single_bid": False,
        "specification_evaluation": {"status": "completed", "model": "qwen"},
    }
    result.update(overrides)
    return result


def _run_node(answer, *, policy=None, result=None):
    from backend_logic2.workflow import process_commands

    submitted: list = []
    with policy_scope(policy or _policy(automation_mode="on")), \
            patch.object(process_commands, "interrupt", return_value=answer), \
            patch(
                "backend_logic2.nodes.quotation.quotation_filter.quotation_ranker"
                ".evaluate_quotations_for_rfqs",
                return_value=result or _result(),
            ) as evaluate, \
            patch(
                "backend_logic2.nodes.quotation.quotation_filter.quotation_ranker"
                ".print_evaluation",
            ), \
            patch(
                "backend_logic2.nodes.quotation.quotation_filter.quotation_registrar"
                ".submit_finalized_quotations",
                side_effect=lambda ranking: submitted.append(ranking),
            ), \
            patch(
                "backend_logic2.services.quotation_service.save_live_ranking_from_result",
            ), \
            patch(
                "backend_logic2.services.auto_progress.known_supplier_names",
                return_value={"동관컴퍼니"},
            ):
        command = process_commands.check_quotations_command({
            "rfq_name": "PUR-RFQ-0001", "case_id": "CASE-1", "rfq_rounds": [],
        })
    return command, submitted, evaluate


_AUTO = {"decision": "auto", "ready": True, "ready_reason": "마감이 지났습니다"}


def test_passing_every_condition_selects_and_starts_the_order() -> None:
    command, submitted, _ = _run_node(_AUTO)

    assert command.goto == "final_selection"
    assert command.update["auto_pr_dispatch"] is True
    assert submitted, "통과했으면 견적을 확정해야 한다"


def test_a_blocked_condition_never_submits_the_quotations() -> None:
    """⚠️ 확정(Submit)은 되돌리기 어렵다. 사람이 재비딩을 고를 수도 있다."""
    command, submitted, _ = _run_node(
        _AUTO, result=_result(parse_failed=[{"quotation_id": "SQ-3"}]),
    )

    assert command.goto == "check_quotations"
    assert submitted == [], "조건에 걸렸는데 견적을 확정했다"
    assert "PARSE_FAILED" in str(
        command.update["quotation_ranking_meta"]["auto_progress"]
    )


def test_recording_mode_evaluates_but_never_selects() -> None:
    command, submitted, _ = _run_node(_AUTO, policy=_policy(automation_mode="shadow"))

    assert command.goto == "check_quotations"
    assert submitted == []


def test_a_missing_readiness_is_not_treated_as_permission() -> None:
    """⚠️ 판정 시점이 됐는지는 스캔이 판단한다. 그 값이 없는 호출을 통과로
    보면 마감 전에도 자동으로 닫힌다."""
    command, submitted, _ = _run_node({"decision": "auto"})

    assert command.goto == "check_quotations"
    assert submitted == []


def test_the_auto_path_drops_quotations_submitted_after_the_deadline() -> None:
    _command, _submitted, evaluate = _run_node({
        **_AUTO,
        "late": [{"quotation_id": "SQ-9", "submitted_at": "2026-10-02T18:01:00+09:00"}],
    })

    dropped = evaluate.call_args.kwargs["excluded_quotations"]
    assert dropped["SQ-9"].startswith("마감 후 제출")


def test_the_human_path_drops_nothing() -> None:
    """늦게 온 건을 살릴지는 사람이 판단할 일이다."""
    _command, _submitted, evaluate = _run_node({
        "decision": "check",
        "late": [{"quotation_id": "SQ-9"}],
    })

    assert evaluate.call_args.kwargs["excluded_quotations"] == {}


def test_no_reply_leaves_the_verdict_on_screen() -> None:
    """⚠️ 조용히 돌아가면 "마감이 지났는데 아무 일도 안 난다"가 된다."""
    command, submitted, _ = _run_node(
        _AUTO, result={"ranking": [], "message": "제출된 견적이 아직 없습니다."},
    )

    assert command.goto == "check_quotations"
    assert submitted == []
    assert "HAS_RANKING" in command.update["error"] or "회신" in command.update["error"]


def test_an_unknown_decision_is_refused() -> None:
    command, _submitted, _ = _run_node({"decision": "자동으로해줘"})

    assert command.goto == "check_quotations"
    assert "auto" in command.update["error"]


# ---------------------------------------------------------------------------
# 순위 계산에서 실제로 빠지는가
# ---------------------------------------------------------------------------


def test_a_late_quotation_is_dropped_from_the_ranking_itself(monkeypatch) -> None:
    """⚠️ 걸러내지 않고 순위만 손보면 competition_count가 늘어난 채 남는다 -
    단독 응찰이 경쟁 2건으로 보이고 자동 선정이 통과한다."""
    from backend_logic2.nodes.quotation.quotation_filter import quotation_ranker

    rows = [
        {"name": "SQ-1", "supplier": "SUP-1", "supplier_name": "동관컴퍼니"},
        {"name": "SQ-2", "supplier": "SUP-2", "supplier_name": "세희상사"},
    ]
    seen: dict = {}

    monkeypatch.setattr(
        quotation_ranker, "load_rfq_requirements",
        lambda name: SimpleNamespace(
            rfq_name=name, model_dump=lambda mode=None: {"rfq_name": name}
        ),
    )
    monkeypatch.setattr(
        quotation_ranker, "_attach_supplier_scorecards", lambda quotations: quotations
    )
    monkeypatch.setattr(
        quotation_ranker, "get_quotations_for_rfqs", lambda names: list(rows)
    )
    monkeypatch.setattr(
        quotation_ranker, "get_reviewable_quotations_for_rfqs",
        lambda names: [
            SimpleNamespace(quotation_id=row["name"], quotation=None) for row in rows
        ],
    )
    monkeypatch.setattr(
        quotation_ranker, "review_quotation",
        lambda quotation, rfq, known_rfq_names=None: quotation,
    )

    def stop(reviews, *a, **kw):
        seen["reviews"] = [review.quotation_id for review in reviews]
        raise _Stop()

    class _Stop(Exception):
        pass

    monkeypatch.setattr(quotation_ranker, "rank_quotations_with_spec_scores", stop)

    with pytest.raises(_Stop):
        quotation_ranker.evaluate_quotations(
            "PUR-RFQ-0001",
            excluded_quotations={"SQ-2": "마감 후 제출"},
            _rfq_names=["PUR-RFQ-0001"],
        )

    assert seen["reviews"] == ["SQ-1"], "마감 후 제출 견적이 평가 입력에 남아 있다"


def test_only_late_quotations_means_no_ranking_with_an_honest_reason(monkeypatch) -> None:
    """전부 마감 후 제출이면 "회신이 없다"가 아니라 그 사실을 말해야 한다."""
    from backend_logic2.nodes.quotation.quotation_filter import quotation_ranker

    monkeypatch.setattr(
        quotation_ranker, "load_rfq_requirements",
        lambda name: SimpleNamespace(
            rfq_name=name, model_dump=lambda mode=None: {"rfq_name": name}
        ),
    )
    monkeypatch.setattr(
        quotation_ranker, "_attach_supplier_scorecards", lambda quotations: quotations
    )
    monkeypatch.setattr(
        quotation_ranker, "get_quotations_for_rfqs",
        lambda names: [{"name": "SQ-9", "supplier_name": "늦은상사"}],
    )

    result = quotation_ranker.evaluate_quotations(
        "PUR-RFQ-0001",
        excluded_quotations={"SQ-9": "마감 후 제출(2026-10-02T18:01:00+09:00)"},
        _rfq_names=["PUR-RFQ-0001"],
    )

    assert result["ranking"] == []
    assert "마감 후에 제출된 견적 1건" in result["message"]
    assert result["excluded"][0]["kind"] == "late_submission"


# ---------------------------------------------------------------------------
# 주기 스케쥴러가 다시 들어오지 않게
# ---------------------------------------------------------------------------


def test_no_periodic_sweep_runs_in_the_background() -> None:
    """⚠️ 60초마다 도는 스윕이 마감 지난 케이스마다 ERPNext를 N+1로 조회해서
    RFQ 전송 버튼이 504로 죽었다. 서버 부하를 볼 수 있게 되기 전까지는 주기
    실행을 붙이지 않는다 - 안 보고 다시 넣으면 같은 일이 반복된다."""
    import inspect

    import main

    source = inspect.getsource(main)

    assert "scan_due_cases" not in source, "마감 스윕이 다시 배경으로 돌고 있다"
    assert "deadline_scan" not in source


# ---------------------------------------------------------------------------
# 수동 버튼 - 판정 시점은 서버가 정한다
# ---------------------------------------------------------------------------


def _readiness(ready=True, reason="마감이 지났습니다", late=None):
    return deadline_readiness.Readiness(
        ready=ready, reason=reason, late=late or [],
    )


@pytest.fixture
def review(monkeypatch):
    from backend_logic2.services import workflow_service

    state = {"readiness": _readiness(), "logged": []}
    monkeypatch.setattr(deadline_readiness, "judge", lambda case, **kw: state["readiness"])
    monkeypatch.setattr(
        "backend_logic2.nodes.supplier.tools.case_logging.log_ai_decision",
        lambda case_id, node, detail: state["logged"].append((node, detail)),
    )
    state["run"] = lambda answer: workflow_service._with_server_side_readiness(
        answer, _case(), actor="parkdongkwan0814@gmail.com"
    )
    return state


def test_the_screen_cannot_claim_it_is_time_to_judge(review) -> None:
    """⚠️ 화면이 ready=true를 주장할 수 있으면 마감 전에도 자동으로 닫힌다.
    v1의 '지금 확인' 버튼이 정확히 그렇게 상태를 속였다."""
    review["readiness"] = _readiness(ready=False, reason="마감 전입니다")

    answer = review["run"]({"decision": "auto", "ready": True, "ready_reason": "내맘대로"})

    assert answer["ready"] is False
    assert answer["ready_reason"] == "마감 전입니다"


def test_the_server_fills_in_the_readiness_it_judged(review) -> None:
    review["readiness"] = _readiness(late=[{"quotation_id": "SQ-9"}])

    answer = review["run"]({"decision": "auto"})

    assert answer["ready"] is True
    assert answer["late"] == [{"quotation_id": "SQ-9"}]


def test_the_button_leaves_its_reason_on_screen(review) -> None:
    """서버 로그를 볼 수 없으므로 "무엇을 기다리는지"가 기록에 남아야 한다."""
    review["readiness"] = _readiness(ready=False, reason="규격 평가가 끝나지 않았습니다")

    review["run"]({"decision": "auto"})

    node, detail = review["logged"][0]
    assert node == "auto_final_selection"
    assert "규격 평가가 끝나지 않았습니다" in detail


def test_a_failed_readiness_check_hands_over_to_a_person(review, monkeypatch) -> None:
    """⚠️ 판정하지 못한 것을 통과로 보면 조건을 안 보고 진행해버린다."""
    monkeypatch.setattr(
        deadline_readiness, "judge",
        lambda case, **kw: (_ for _ in ()).throw(RuntimeError("ERPNext down")),
    )

    answer = review["run"]({"decision": "auto"})

    assert answer["ready"] is False
    assert "확인하지 못했습니다" in answer["ready_reason"]


def test_a_logging_failure_does_not_stop_the_judgment(review, monkeypatch) -> None:
    monkeypatch.setattr(
        "backend_logic2.nodes.supplier.tools.case_logging.log_ai_decision",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db down")),
    )

    assert review["run"]({"decision": "auto"})["ready"] is True


@pytest.mark.parametrize("answer", [
    {"decision": "check"},
    {"decision": "finalize", "supplier": "동관컴퍼니"},
    {"decision": "rebid"},
    "이상한 값",
])
def test_other_decisions_are_passed_through_untouched(review, answer) -> None:
    """⚠️ 사람이 직접 고른 결정에 손대면 안 된다."""
    assert review["run"](answer) == answer
    assert review["logged"] == []


def test_the_readiness_is_judged_before_the_graph_lock_is_taken() -> None:
    """⚠️ 이 판단은 ERPNext를 여러 번 조회한다. 잠금을 쥔 채로 하면 그동안
    다른 사람의 클릭이 전부 그 뒤에서 기다리다 504로 죽는다."""
    import inspect

    from backend_logic2.services import workflow_service

    source = inspect.getsource(workflow_service._run_queued_quotation_analysis)
    readiness_at = source.index("_with_server_side_readiness")
    lock_at = source.index("with _GRAPH_LOCK")

    assert readiness_at < lock_at, "판정을 그래프 잠금 안에서 하고 있다"


def test_the_button_never_holds_the_http_socket() -> None:
    """RunPod 규격 평가가 도는 동안 소켓을 붙잡고 있으면 nginx가 먼저 끊는다."""
    import inspect

    from backend_logic2.api import procurement_routes

    source = inspect.getsource(procurement_routes.answer_task)

    assert '{"check", "auto"}' in source, "auto가 배경 처리 경로를 타지 않는다"
