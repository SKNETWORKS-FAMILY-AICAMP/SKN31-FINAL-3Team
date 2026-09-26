"""마감 스캔 - 무엇을 깨우고 무엇을 건드리지 않는가."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

import pytest

from backend_logic2.policies.schema import CompanyPolicy
from backend_logic2.services import auto_progress_runner as runner


# 지문(signature)에 마감 시각이 들어가므로 호출마다 값이 달라지면 안 된다.
_FIXED_DEADLINE = datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc)


def _case(**overrides):
    case = {
        "case_id": "CASE-1",
        "mr_name": "MAT-MR-2026-00120",
        "stage": "QUOTATION_COLLECTION",
        "assigned_user_id": "buyer@example.com",
        "quotation_deadline_at": _FIXED_DEADLINE,
        "quotation_snapshot": {"recipient_count": 4, "responded_count": 3},
        "workflow_snapshot": {"values": {"rfq_name": "PUR-RFQ-1", "auto_progress": {}}},
        "automation_hold": False,
        "auto_progress_signature": None,
        "auto_deadline_extended_at": None,
    }
    case.update(overrides)
    return case


def _policy(**rules):
    base = CompanyPolicy()
    return base.model_copy(update={"rules": base.rules.model_copy(update=rules)})


@pytest.fixture
def harness(monkeypatch):
    state = {
        "case": _case(),
        "policy": _policy(automation_mode="on"),
        "resumed": [],
        "signatures": [],
        "notices": [],
        "extended": [],
        "after": None,
        # ⚠️ 납기요청일은 케이스 테이블에 없는 값이다. 예전 테스트가 케이스
        # dict에 required_by를 꽂아 넣어서, 실제로는 한 번도 읽히지 않던
        # 검사를 통과한 것처럼 보이게 만들었다. 실제 경로대로 조회를 막는다.
        "required_by": None,
    }

    @contextmanager
    def fake_case_lock(case_id):
        yield

    monkeypatch.setattr(runner, "case_lock", fake_case_lock)
    # 기본은 "이 인스턴스가 진행 상황을 갖고 있다" - 없는 경우는 전용 테스트에서.
    monkeypatch.setattr(runner, "has_local_checkpoint", lambda case: True)
    monkeypatch.setattr(runner, "_policy_for", lambda case: state["policy"])
    monkeypatch.setattr(runner, "_required_by", lambda mr_name: state["required_by"])
    monkeypatch.setattr(
        runner.task_repository,
        "list_tasks",
        lambda **kwargs: [{"task_id": "TASK-1", "task_type": "quotation_check", "version": 3}],
    )
    monkeypatch.setattr(
        runner.case_repository,
        "get_case",
        lambda case_id: state["after"] or state["case"],
    )
    monkeypatch.setattr(
        runner.case_repository,
        "mark_auto_progress_attempt",
        lambda case_id, signature: state["signatures"].append(signature),
    )
    monkeypatch.setattr(
        runner.case_repository,
        "mark_auto_deadline_extended",
        lambda case_id: state["extended"].append(case_id),
    )
    monkeypatch.setattr(
        runner.notification_repository,
        "create_notification",
        lambda **kwargs: state["notices"].append(kwargs),
    )

    import backend_logic2.services.workflow_service as workflow_service

    monkeypatch.setattr(
        workflow_service,
        "resume_task",
        lambda task_id, **kwargs: state["resumed"].append(kwargs) or {},
    )
    monkeypatch.setattr(
        workflow_service,
        "extend_quotation_deadline",
        lambda case_id, **kwargs: state["extended"].append(kwargs) or {},
    )
    return state


def test_it_answers_the_waiting_task_on_behalf_of_the_person(harness) -> None:
    harness["after"] = _case(stage="SUPPLIER_SELECTION")

    assert runner.process_case(harness["case"]) == "advanced"

    assert harness["resumed"] == [{
        "answer": {"decision": "auto", "trigger": "deadline"},
        "answered_by": "system:auto-progress",
        "expected_version": 3,
    }]
    # 단계가 넘어갔으면 지문을 비워 다음 상황을 다시 평가할 수 있게 한다.
    assert harness["signatures"] == [None]


def test_a_blocked_case_notifies_once_and_then_stays_quiet(harness) -> None:
    blocked = {"allowed": False, "checks": [
        {"code": "MIN_COMPETITION", "label": "경쟁 견적", "detail": "유효한 견적이 1건뿐입니다", "status": "blocked"},
    ]}
    harness["after"] = _case(workflow_snapshot={"values": {"rfq_name": "PUR-RFQ-1", "auto_progress": blocked}})

    assert runner.process_case(harness["case"]) == "blocked"
    assert len(harness["notices"]) == 1
    assert "1건뿐" in harness["notices"][0]["message"]

    # 상황이 그대로면 다음 턴에는 건드리지 않는다(10분마다 알림이 쌓이지 않게).
    unchanged = _case(auto_progress_signature=harness["signatures"][-1])
    assert runner.process_case(unchanged) == "unchanged"
    assert len(harness["notices"]) == 1


def test_more_quotations_change_the_signature_and_it_is_re_evaluated(harness) -> None:
    first = runner.situation_signature(_case(quotation_snapshot={"recipient_count": 4, "responded_count": 3}))
    second = runner.situation_signature(_case(quotation_snapshot={"recipient_count": 4, "responded_count": 4}))
    assert first != second


def test_held_cases_are_never_touched(harness) -> None:
    assert runner.process_case(_case(automation_hold=True)) == "held"
    assert harness["resumed"] == []


def test_automation_off_does_nothing(harness) -> None:
    harness["policy"] = _policy()
    assert runner.process_case(harness["case"]) == "disabled"
    assert harness["resumed"] == []


def test_silence_extends_the_deadline_once_when_there_is_lead_time(harness) -> None:
    harness["required_by"] = date.today() + timedelta(days=30)
    case = _case(quotation_snapshot={"recipient_count": 4, "responded_count": 0})
    assert runner.process_case(case) == "deadline_extended"
    assert harness["resumed"] == []

    # 한 번 연장한 건은 다시 연장하지 않고 자동 선정 판정으로 넘어간다.
    already = _case(
        quotation_snapshot={"recipient_count": 4, "responded_count": 0},
        auto_deadline_extended_at=datetime.now(timezone.utc),
    )
    harness["after"] = _case(stage="SUPPLIER_SELECTION")
    assert runner.process_case(already) == "advanced"


def test_a_tight_due_date_is_not_extended(harness) -> None:
    harness["required_by"] = date.today() + timedelta(days=4)
    case = _case(quotation_snapshot={"recipient_count": 4, "responded_count": 0})
    harness["after"] = _case(stage="SUPPLIER_SELECTION")
    # 납기 여유가 없으면 연장하지 않고 바로 판정으로 간다.
    assert runner.process_case(case) == "advanced"
    assert harness["extended"] == []


def test_an_unknown_due_date_is_never_extended(harness) -> None:
    """납기를 모르면 연장하지 않는다.

    연장만 반복하다 납기를 놓치는 게 가장 나쁜 결말이라, 여유를 확인할 수
    없으면 통과가 아니라 정지다.
    """
    harness["required_by"] = None
    case = _case(quotation_snapshot={"recipient_count": 4, "responded_count": 0})
    harness["after"] = _case(stage="SUPPLIER_SELECTION")

    assert runner.process_case(case) == "advanced"
    assert harness["extended"] == []


def _blocked_snapshot(*checks):
    return {"values": {"rfq_name": "PUR-RFQ-1", "auto_progress": {
        "allowed": False,
        "retryable": all(row.get("retryable") for row in checks),
        "checks": list(checks),
    }}}


_SPEC_RUNNING = {
    "code": "SPEC_EVALUATION", "label": "규격 평가 완료",
    "detail": "규격 평가가 끝나지 않았습니다 (미평가 2건)",
    "status": "blocked", "retryable": True,
}


def test_a_spec_evaluation_still_running_is_retried_not_escalated(harness) -> None:
    """마감 직전에 견적이 들어와 규격 평가가 몇 분 더 걸리는 경우.

    사람이 할 일은 없고 아직 안 끝난 것뿐이라, 부르지 않고 다시 본다.
    지문을 비워둬야 다음 스캔이 같은 건을 또 본다.
    """
    # 마감이 방금 지난 건. _FIXED_DEADLINE은 이미 한참 전이라 쓸 수 없다.
    fresh = _case(
        quotation_deadline_at=datetime.now(timezone.utc) - timedelta(minutes=2),
        workflow_snapshot=_blocked_snapshot(_SPEC_RUNNING),
    )
    harness["after"] = fresh

    assert runner.process_case(fresh) == "waiting"
    assert harness["notices"] == []
    assert harness["signatures"] == [None]


def test_a_stuck_spec_evaluation_eventually_calls_a_person(harness) -> None:
    """저절로 풀릴 이유여도 무한정 기다리진 않는다. 그건 지연이 아니라 고장이다."""
    stale = _case(
        quotation_deadline_at=datetime.now(timezone.utc) - timedelta(hours=6),
        workflow_snapshot=_blocked_snapshot(_SPEC_RUNNING),
    )
    harness["after"] = stale

    assert runner.process_case(stale) == "blocked"
    assert len(harness["notices"]) == 1


def test_a_human_blocker_mixed_in_is_never_retried(harness) -> None:
    """사람이 판단할 이유가 섞여 있으면 다시 봐도 결론이 같다."""
    human = {
        "code": "AMOUNT_LIMIT", "label": "자동 선정 금액 한도",
        "detail": "금액이 한도를 넘습니다", "status": "blocked",
    }
    fresh = _case(
        quotation_deadline_at=datetime.now(timezone.utc) - timedelta(minutes=2),
        workflow_snapshot=_blocked_snapshot(_SPEC_RUNNING, human),
    )
    harness["after"] = fresh

    assert runner.process_case(fresh) == "blocked"
    assert len(harness["notices"]) == 1


def test_a_failed_resume_does_not_stop_the_rest(harness, monkeypatch) -> None:
    import backend_logic2.services.workflow_service as workflow_service

    def boom(task_id, **kwargs):
        raise RuntimeError("checkpoint locked")

    monkeypatch.setattr(workflow_service, "resume_task", boom)
    assert runner.process_case(harness["case"]) == "failed"
    assert harness["signatures"][-1] is not None


def test_a_case_whose_checkpoint_lives_elsewhere_is_left_alone(harness, monkeypatch) -> None:
    """DB는 함께 보지만 워크플로 진행 상황은 인스턴스마다 따로 있다.

    없는 쪽이 재개하면 처음부터 다시 도는 사고가 나므로 건드리지 않는다.
    """
    monkeypatch.setattr(runner, "has_local_checkpoint", lambda case: False)

    assert runner.process_case(harness["case"]) == "not_mine"
    assert harness["resumed"] == []
    assert harness["signatures"] == []


def test_shadow_mode_never_touches_the_deadline(harness) -> None:
    """기록 모드는 판정만 남긴다. 마감을 실제로 미루면 약속이 깨진다."""
    harness["policy"] = _policy(automation_mode="shadow")
    harness["after"] = _case()
    case = _case(
        quotation_snapshot={"recipient_count": 2, "responded_count": 0},
        required_by=datetime.now(timezone.utc) + timedelta(days=30),
    )

    outcome = runner.process_case(case)

    assert outcome != "deadline_extended"
    assert harness["extended"] == []
