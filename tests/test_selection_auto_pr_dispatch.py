"""선정 팝업에서 수주 접수 요청 메일 발송까지 확인받은 경우의 흐름."""

from __future__ import annotations

from unittest.mock import patch

from backend_logic2.workflow.process_commands import (
    await_order_start_command,
    final_selection_command,
    request_pr_command,
)


def _selection_state(**overrides):
    state = {
        "mr_name": "MAT-MR-0001",
        "rfq_name": "PUR-RFQ-0002",
        "quotation_ranking": [
            {"supplier": "공급사 A", "quotation_id": "SQ-1", "rfq_name": "PUR-RFQ-0001"},
        ],
    }
    state.update(overrides)
    return state


def test_confirmed_selection_carries_the_dispatch_flag() -> None:
    with patch(
        "backend_logic2.workflow.process_commands.interrupt",
        return_value={"supplier": "공급사 A", "quotation_id": "SQ-1", "start_order": True},
    ):
        command = final_selection_command(_selection_state())

    assert command.goto == "await_order_start"
    assert command.update["auto_pr_dispatch"] is True
    # 지난 차수 견적을 골라도 그 차수 이름이 그대로 따라간다.
    assert command.update["selected_rfq_name"] == "PUR-RFQ-0001"


def test_selection_without_confirmation_keeps_the_manual_buttons() -> None:
    with patch(
        "backend_logic2.workflow.process_commands.interrupt",
        return_value={"supplier": "공급사 A", "quotation_id": "SQ-1"},
    ):
        command = final_selection_command(_selection_state(auto_pr_dispatch=True))

    # 이 화면에서 다시 고른 답변에 확인이 없으면 예전 확인은 무효다.
    assert command.update["auto_pr_dispatch"] is False


def test_order_start_and_pr_request_skip_their_confirmations() -> None:
    with patch("backend_logic2.workflow.process_commands.interrupt") as mock_interrupt:
        order = await_order_start_command({
            "mr_name": "MAT-MR-0001",
            "selected_supplier": "공급사 A",
            "auto_pr_dispatch": True,
        })
    assert order.goto == "request_pr"
    assert order.update["order_started"] is True
    mock_interrupt.assert_not_called()

    with patch("backend_logic2.workflow.process_commands.interrupt") as mock_interrupt:
        pr = request_pr_command({
            "case_id": "CASE-1",
            "mr_name": "MAT-MR-0001",
            "order_started": True,
            "auto_pr_dispatch": True,
        })
    assert pr.goto == "create_pr"
    # 발송이 실패해 다시 돌아오면 그때는 사람이 직접 눌러야 한다.
    assert pr.update["auto_pr_dispatch"] is False
    mock_interrupt.assert_not_called()


def test_expired_quotation_clears_the_confirmation() -> None:
    state = _selection_state(
        requested_supplier="공급사 A",
        requested_quotation="SQ-1",
        auto_pr_dispatch=True,
        quotation_ranking=[
            {"supplier": "공급사 A", "quotation_id": "SQ-1", "valid_till": "2020-01-01"},
        ],
    )
    command = final_selection_command(state)

    assert command.goto == "final_selection"
    assert command.update["auto_pr_dispatch"] is False
    assert "유효기간" in command.update["error"]


def test_a_new_supplier_no_longer_stops_before_the_order() -> None:
    """신규 협력사 서류 확인은 프로젝트에서 뺐다. 여기서 멈추면 안 된다.

    화면에 처리 UI가 없는 인터럽트였고, 서류가 다 붙어 있어도
    review_required가 항상 True라서 신규 업체 건은 선정 직후 영구히
    멈췄다. 경로를 지웠으니 곧바로 발주 시작 확인으로 간다.
    """
    with patch(
        "backend_logic2.workflow.process_commands.interrupt",
        return_value={"decision": "start_order"},
    ):
        new_supplier = {
            "mr_name": "MAT-MR-0001",
            "selected_supplier": "공급사 A",
            "supplier_registration_results": [
                {"name": "공급사 A", "is_new_supplier": True, "onboarding_status": "PROVISIONAL"},
            ],
        }
        # 선정 팝업에서 이미 확인을 받은 경로.
        assert await_order_start_command(
            {**new_supplier, "auto_pr_dispatch": True}
        ).goto == "request_pr"
        # 화면에서 '발주 시작'을 누르는 경로.
        assert await_order_start_command(new_supplier).goto == "request_pr"
