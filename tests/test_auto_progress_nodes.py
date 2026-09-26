"""워크플로 노드가 조건을 보고 멈추거나 그냥 지나가는지."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from backend_logic2.policies.runtime import policy_scope
from backend_logic2.policies.schema import CompanyPolicy
from backend_logic2.workflow.process_commands import (
    check_quotations_command,
    select_rfq_targets_command,
)


def _policy(**rules):
    base = CompanyPolicy()
    return base.model_copy(update={"rules": base.rules.model_copy(update=rules)})


def _candidates():
    return [
        {"name": "동관컴퍼니", "email": "a@x.com", "registered": True},
        {"name": "세희세희", "email": "b@x.com", "registered": True},
        {"name": "진짜효민", "email": "c@x.com", "registered": True},
    ]


def _rfq_state(**overrides):
    state = {
        "mr_name": "MAT-MR-2026-00120",
        "case_id": "CASE-1",
        "supplier_candidates": _candidates(),
        "existing_supplier_candidates": _candidates(),
        # resolve_supplier_pool이 "기존 풀만으로 충분"이라고 판정한 상태.
        "supplier_pool_decision": {
            "needs_search": False,
            "reasons": ["[ITEM-1] 기존 공급사 3곳 확보 -> 기존 공급사 풀 사용"],
        },
    }
    state.update(overrides)
    return state


def _evaluation():
    return {
        "ranking": [
            {
                "supplier": "동관컴퍼니",
                "quotation_id": "SQ-1",
                "overall_score": 93.6,
                "total_amount": "8400000",
                "penalties": [],
            },
            {"supplier": "세희세희", "quotation_id": "SQ-2", "overall_score": 81.2},
            {"supplier": "진짜효민", "quotation_id": "SQ-3", "overall_score": 73.4},
        ],
        "excluded": [],
        "parse_failed": [],
        "competition_count": 3,
        "single_bid": False,
        "specification_evaluation": {"status": "completed", "model": "qwen3.5-9b-4bit"},
    }


@pytest.fixture
def registrations():
    with patch(
        "backend_logic2.nodes.supplier.register_candidate_suppliers.register_candidate_suppliers",
        return_value=[{"name": row["name"], "status": "ok"} for row in _candidates()],
    ):
        yield


def test_rfq_targets_go_out_without_a_person_when_all_are_known(registrations) -> None:
    with policy_scope(_policy(automation_mode="on")), \
            patch("backend_logic2.workflow.process_commands.interrupt") as mock_interrupt, \
            patch(
                "backend_logic2.workflow.process_commands._default_quotation_deadline",
                return_value="2026-10-05T18:00:00+09:00",
            ):
        command = select_rfq_targets_command(_rfq_state())

    mock_interrupt.assert_not_called()
    assert command.goto == "create_rfq"
    assert command.update["quotation_deadline"] == "2026-10-05T18:00:00+09:00"
    assert sorted(command.update["selected_suppliers"]) == ["동관컴퍼니", "세희세희", "진짜효민"]
    assert command.update["auto_progress"]["allowed"] is True


def test_a_pool_needing_new_suppliers_still_asks_the_person(registrations) -> None:
    state = _rfq_state(
        supplier_candidates=[*_candidates(), {"name": "처음보는곳", "email": "d@x.com"}],
        supplier_pool_decision={
            "needs_search": True,
            "reasons": ["[ITEM-1] 기존 공급사 1곳 < 최소 경쟁기준 3곳 -> 신규 공급사 탐색 필요"],
        },
    )
    with policy_scope(_policy(automation_mode="on")), \
            patch(
                "backend_logic2.workflow.process_commands.interrupt",
                return_value={"suppliers": ["동관컴퍼니"], "quotation_deadline": "2026-10-05T18:00:00+09:00"},
            ) as mock_interrupt:
        select_rfq_targets_command(state)

    mock_interrupt.assert_called_once()
    payload = mock_interrupt.call_args[0][0]
    assert payload["auto_progress"]["allowed"] is False
    assert "SUPPLIER_POOL" in {
        row["code"] for row in payload["auto_progress"]["checks"] if row["status"] == "blocked"
    }


def test_deadline_that_cannot_be_derived_falls_back_to_the_person(registrations) -> None:
    with policy_scope(_policy(automation_mode="on")), \
            patch(
                "backend_logic2.workflow.process_commands.interrupt",
                return_value={"suppliers": ["동관컴퍼니"], "quotation_deadline": "2026-10-05T18:00:00+09:00"},
            ) as mock_interrupt, \
            patch("backend_logic2.workflow.process_commands._default_quotation_deadline", return_value=None):
        select_rfq_targets_command(_rfq_state())

    mock_interrupt.assert_called_once()
    checks = mock_interrupt.call_args[0][0]["auto_progress"]["checks"]
    assert "DEADLINE_UNAVAILABLE" in {row["code"] for row in checks if row["status"] == "blocked"}


def _run_auto_check(policy, evaluation, trigger="deadline"):
    state = {
        "mr_name": "MAT-MR-2026-00120",
        "case_id": "CASE-1",
        "rfq_name": "PUR-RFQ-2026-00010",
        "existing_supplier_candidates": _candidates(),
    }
    with policy_scope(policy), \
            patch(
                "backend_logic2.workflow.process_commands.interrupt",
                return_value={"decision": "auto", "trigger": trigger},
            ), \
            patch(
                "backend_logic2.nodes.quotation.quotation_filter.quotation_ranker.evaluate_quotations_for_rfqs",
                return_value=evaluation,
            ), \
            patch(
                "backend_logic2.nodes.quotation.quotation_filter.quotation_registrar.submit_finalized_quotations",
            ) as submit, \
            patch("backend_logic2.services.quotation_service.save_live_ranking_from_result"):
        return check_quotations_command(state), submit


def test_deadline_trigger_selects_the_top_quotation_automatically() -> None:
    command, submit = _run_auto_check(_policy(automation_mode="on"), _evaluation())

    assert command.goto == "final_selection"
    assert command.update["requested_supplier"] == "동관컴퍼니"
    assert command.update["requested_quotation"] == "SQ-1"
    assert command.update["selection_mode"] == "auto"
    assert command.update["auto_pr_dispatch"] is True
    submit.assert_called_once()


def test_a_blocked_condition_leaves_the_case_waiting_with_its_reason() -> None:
    evaluation = _evaluation()
    evaluation["single_bid"] = True
    evaluation["competition_count"] = 1
    command, submit = _run_auto_check(_policy(automation_mode="on"), evaluation)

    assert command.goto == "check_quotations"
    assert command.update["status"] == "awaiting_quotation_check"
    assert "단독" in command.update["error"] or "비교" in command.update["error"]
    assert command.update["auto_progress"]["allowed"] is False
    # 조건에 걸린 건은 견적을 확정(Submit)하지 않는다.
    submit.assert_not_called()


def test_shadow_mode_records_the_verdict_without_selecting() -> None:
    command, submit = _run_auto_check(_policy(automation_mode="shadow"), _evaluation())

    assert command.goto == "check_quotations"
    assert command.update["auto_progress"]["allowed"] is True
    assert command.update["auto_progress"]["mode"] == "shadow"
    assert command.update["error"] == ""
    submit.assert_not_called()


def test_automation_off_never_selects() -> None:
    command, submit = _run_auto_check(_policy(), _evaluation())

    assert command.goto == "check_quotations"
    submit.assert_not_called()


def _run_rebid(previous_verdict):
    """예외 결정 화면에서 '재비딩'을 고른 상황."""
    state = {
        "mr_name": "MAT-MR-2026-00120",
        "case_id": "CASE-1",
        "rfq_name": "PUR-RFQ-2026-00010",
        "existing_supplier_candidates": _candidates(),
        "supplier_candidates": _candidates(),
        "auto_progress": previous_verdict,
    }
    with policy_scope(_policy(automation_mode="on")), \
            patch(
                "backend_logic2.workflow.process_commands.interrupt",
                return_value={"decision": "rebid"},
            ), \
            patch("backend_logic2.workflow.process_commands._archive_current_rfq_round",
                  return_value=[{"rfq_name": "PUR-RFQ-2026-00010"}]):
        return check_quotations_command(state)


def test_rebid_clears_the_previous_rounds_verdict() -> None:
    """지난 판정이 남으면 화면이 예외 결정 화면에 갇힌다.

    실제로 겪은 증상: 재비딩을 눌러도 계속 '결정 필요'로 보이고, 자동 진행을
    보류하면(그 표시가 꺼져서) 정상으로 보였다.
    """
    blocked = {
        "allowed": False,
        "mode": "on",
        "enabled": True,
        "node": "auto_final_selection",
        "checks": [{"code": "SCORE_GAP", "label": "점수차", "detail": "붙었다", "status": "blocked"}],
    }

    command = _run_rebid(blocked)

    assert command.goto == "select_rfq_targets"
    assert command.update["auto_progress"] == {}
    # 협력사 풀은 그대로 남아 같은 후보로 다시 고를 수 있어야 한다.
    assert command.update["rfq_name"] == ""
    assert command.update["quotation_deadline"] == ""


def test_a_rebid_round_is_never_auto_dispatched(registrations) -> None:
    """2차는 협력사와 마감일을 사람이 정한다."""
    state = _rfq_state(rfq_rounds=[{"rfq_name": "PUR-RFQ-2026-00010"}])

    with policy_scope(_policy(automation_mode="on")), \
            patch(
                "backend_logic2.workflow.process_commands.interrupt",
                return_value={"suppliers": ["동관컴퍼니"], "quotation_deadline": "2026-10-05T18:00:00+09:00"},
            ) as mock_interrupt:
        select_rfq_targets_command(state)

    # 자동으로 나가지 않고 사람에게 물었다.
    mock_interrupt.assert_called_once()
    checks = mock_interrupt.call_args[0][0]["auto_progress"]["checks"]
    assert "REBID_ROUND" in {row["code"] for row in checks if row["status"] == "blocked"}
