"""RFQ 자동 발송 - 자동화 v2의 4단계.

원칙: **판단이 애매하면 사람에게 넘긴다.** RFQ는 외부로 메일이 나가는,
되돌리기 어려운 행동이다. 조건을 "자동으로 갈 이유"가 아니라 "사람을 부를
이유"로 적고, 하나라도 걸리면 멈춘다.
"""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import patch

import pytest

from backend_logic2.policies.runtime import policy_scope
from backend_logic2.policies.schema import CompanyPolicy
from backend_logic2.services.auto_progress import (
    KST,
    auto_rfq_deadline,
    evaluate_rfq_dispatch,
)


def _policy(**rules):
    base = CompanyPolicy()
    return base.model_copy(update={"rules": base.rules.model_copy(update=rules)})


def _candidates(count: int = 3, *, email: bool = True):
    return [
        {"name": f"협력사{index}", "email": f"s{index}@example.com" if email else ""}
        for index in range(count)
    ]


def _pool(needs_search: bool = False):
    return {
        "needs_search": needs_search,
        "reasons": ["[ITEM-1] 과거 확정 구매이력 없음"] if needs_search else [],
    }


def _evaluate(policy=None, **overrides):
    kwargs = {
        "pool_decision": _pool(),
        "is_rebid": False,
        "deadline": "2026-10-02T18:00:00+09:00",
    }
    kwargs.update(overrides)
    candidates = kwargs.pop("candidates", _candidates())
    with policy_scope(policy or _policy(automation_mode="on")):
        return evaluate_rfq_dispatch(candidates, **kwargs)


# ---------------------------------------------------------------------------
# 통과하는 경우
# ---------------------------------------------------------------------------


def test_an_existing_pool_with_contacts_goes_out_without_a_person() -> None:
    decision = _evaluate()

    assert decision.allowed is True
    assert decision.should_proceed is True


def test_recording_mode_evaluates_but_never_proceeds() -> None:
    """임계값을 실제 데이터로 확인하는 단계. 판정만 남기고 멈춘다."""
    decision = _evaluate(_policy(automation_mode="shadow"))

    assert decision.allowed is True
    assert decision.should_proceed is False
    assert decision.shadow_only is True


def test_automation_off_never_proceeds() -> None:
    decision = _evaluate(_policy())

    assert decision.should_proceed is False


def test_the_stage_switch_alone_can_stop_it() -> None:
    decision = _evaluate(_policy(automation_mode="on", auto_rfq_dispatch=False))

    assert decision.allowed is True
    assert decision.should_proceed is False


# ---------------------------------------------------------------------------
# 사람을 부르는 경우
# ---------------------------------------------------------------------------


def test_a_pool_needing_new_suppliers_always_asks_a_person() -> None:
    """거래한 적 없는 곳을 자동으로 입찰에 넣지 않는다."""
    decision = _evaluate(pool_decision=_pool(needs_search=True))

    assert decision.should_proceed is False
    assert "SUPPLIER_POOL" in {row["code"] for row in decision.blockers}
    # 사람이 읽을 수 있는 이유가 그대로 실려야 한다.
    assert "구매이력" in decision.summary()


def test_a_missing_pool_decision_is_not_treated_as_permission() -> None:
    """값을 알 수 없을 때도 통과가 아니라 정지다."""
    decision = _evaluate(pool_decision=None)

    assert decision.should_proceed is False
    assert "SUPPLIER_POOL" in {row["code"] for row in decision.blockers}


def test_a_rebid_round_is_always_decided_by_a_person() -> None:
    """사람이 재비딩을 고른 건 1차에서 뭔가 잘못됐기 때문이다."""
    decision = _evaluate(is_rebid=True)

    assert decision.should_proceed is False
    assert "REBID_ROUND" in {row["code"] for row in decision.blockers}


def test_too_few_suppliers_asks_a_person() -> None:
    decision = _evaluate(candidates=_candidates(2))

    assert decision.should_proceed is False
    assert "ENOUGH_SUPPLIERS" in {row["code"] for row in decision.blockers}


def test_the_threshold_comes_from_policy() -> None:
    decision = _evaluate(
        _policy(automation_mode="on", auto_rfq_min_existing_suppliers=5),
        candidates=_candidates(4),
    )

    assert decision.should_proceed is False
    assert "5곳" in decision.summary()


def test_a_supplier_without_an_email_asks_a_person() -> None:
    decision = _evaluate(candidates=_candidates(3, email=False))

    assert decision.should_proceed is False
    assert "SUPPLIER_EMAIL" in {row["code"] for row in decision.blockers}


def test_no_usable_deadline_asks_a_person() -> None:
    decision = _evaluate(deadline=None)

    assert decision.should_proceed is False
    assert "DEADLINE" in {row["code"] for row in decision.blockers}


def test_the_verdict_says_which_step_it_came_from() -> None:
    """지난 단계 판정을 지금 상태로 착각하면 안 된다."""
    assert _evaluate().as_payload()["node"] == "auto_rfq_dispatch"


# ---------------------------------------------------------------------------
# 마감일 계산
# ---------------------------------------------------------------------------


_NOW = datetime(2026, 9, 27, 10, 0, tzinfo=KST)


def test_the_deadline_is_n_days_out_at_six_pm() -> None:
    with policy_scope(_policy(auto_rfq_deadline_days=5)):
        assert auto_rfq_deadline(None, now=_NOW) == "2026-10-02T18:00:00+09:00"


def test_a_closer_due_date_pulls_the_deadline_in() -> None:
    """min(N, 납기까지 남은 일수)."""
    with policy_scope(_policy(auto_rfq_deadline_days=5)):
        assert auto_rfq_deadline(date(2026, 9, 30), now=_NOW) == "2026-09-30T18:00:00+09:00"


def test_a_far_due_date_does_not_push_the_deadline_out() -> None:
    with policy_scope(_policy(auto_rfq_deadline_days=5)):
        assert auto_rfq_deadline(date(2026, 12, 31), now=_NOW) == "2026-10-02T18:00:00+09:00"


def test_no_due_date_means_no_constraint() -> None:
    """납기일이 없는 MR만 긴급 판정을 건너뛰어 비딩에 올 수 있다."""
    with policy_scope(_policy(auto_rfq_deadline_days=7)):
        assert auto_rfq_deadline(None, now=_NOW) == "2026-10-04T18:00:00+09:00"


@pytest.mark.parametrize("schedule", [date(2026, 9, 27), date(2026, 9, 26)])
def test_a_due_date_that_leaves_no_room_yields_no_deadline(schedule: date) -> None:
    """마감을 줄 수 없으면 자동으로 보내지 않는다(사람에게 넘어간다)."""
    with policy_scope(_policy(auto_rfq_deadline_days=5)):
        assert auto_rfq_deadline(schedule, now=_NOW) is None


def test_the_deadline_respects_the_policy_value() -> None:
    with policy_scope(_policy(auto_rfq_deadline_days=1)):
        assert auto_rfq_deadline(None, now=_NOW) == "2026-09-28T18:00:00+09:00"


# ---------------------------------------------------------------------------
# 그래프 노드에 실제로 붙었는가
# ---------------------------------------------------------------------------


def _rfq_state(**overrides):
    state = {
        "mr_name": "MAT-MR-2026-00200",
        "case_id": "CASE-1",
        "supplier_candidates": _candidates(),
        "existing_supplier_candidates": _candidates(),
        "supplier_pool_decision": _pool(),
    }
    state.update(overrides)
    return state


def _run_node(policy, state):
    from backend_logic2.workflow import process_commands

    with policy_scope(policy), \
            patch.object(process_commands, "_mr_schedule_date", return_value=None), \
            patch.object(process_commands, "interrupt") as interrupt, \
            patch(
                "backend_logic2.nodes.supplier.register_candidate_suppliers"
                ".register_candidate_suppliers",
                side_effect=lambda rows, case_id=None: [
                    {"name": row["name"], "status": "ok"} for row in rows
                ],
            ):
        interrupt.side_effect = AssertionError("사람에게 물었다")
        try:
            command = process_commands.select_rfq_targets_command(state)
        except AssertionError:
            return None, interrupt
    return command, interrupt


def test_the_node_sends_without_asking_when_conditions_pass() -> None:
    command, interrupt = _run_node(_policy(automation_mode="on"), _rfq_state())

    assert command is not None, "조건을 통과했는데 사람에게 물었다"
    interrupt.assert_not_called()
    assert command.goto == "create_rfq"
    assert command.update["quotation_deadline"].endswith("+09:00")
    assert len(command.update["selected_suppliers"]) == 3


def test_the_node_asks_when_a_condition_fails() -> None:
    command, interrupt = _run_node(
        _policy(automation_mode="on"),
        _rfq_state(supplier_pool_decision=_pool(needs_search=True)),
    )

    assert command is None, "조건에 걸렸는데 자동으로 보냈다"
    interrupt.assert_called_once()
    payload = interrupt.call_args[0][0]
    assert payload["auto_progress"]["allowed"] is False


def test_the_node_asks_in_recording_mode() -> None:
    command, interrupt = _run_node(_policy(automation_mode="shadow"), _rfq_state())

    assert command is None, "기록 모드인데 실제로 보냈다"
    payload = interrupt.call_args[0][0]
    assert payload["auto_progress"]["allowed"] is True
    assert payload["auto_progress"]["mode"] == "shadow"


def test_the_node_asks_when_automation_is_off() -> None:
    command, interrupt = _run_node(_policy(), _rfq_state())

    assert command is None
    interrupt.assert_called_once()


def test_a_rebid_round_reaches_the_person_through_the_node() -> None:
    command, interrupt = _run_node(
        _policy(automation_mode="on"),
        _rfq_state(rfq_rounds=[{"rfq_name": "PUR-RFQ-2026-00010"}]),
    )

    assert command is None
    codes = {
        row["code"]
        for row in interrupt.call_args[0][0]["auto_progress"]["checks"]
        if row["status"] == "blocked"
    }
    assert "REBID_ROUND" in codes
