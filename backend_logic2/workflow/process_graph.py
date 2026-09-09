"""Compiled LangGraph for the backend_logic2 purchasing process.

전체 9단계(check_mr_item+substitute_selection, decide_bidding_choice,
resolve_suppliers_choice, search_new_suppliers, select_rfq_targets,
create_rfq, check_quotations, final_selection, create_po) 등록 완료
(2026-08-31, PO 생성+발송 추가).

케이스 상태이력 로깅(2026-08-31 추가): 노드 함수들(process_commands.py)이
전부 Command(update={"status": ..., ...}, goto=...) 형태로 통일돼 있는
걸 이용해서, 각 노드 함수 내부를 일일이 고치는 대신 여기 노드 등록
시점에 _with_status_log()로 감싸서 "이전상태 -> 새상태" 전이를
case_status_history에 자동으로 남김. 새 노드(PO 생성 등)가 추가돼도
add_node를 이 wrapper로 감싸기만 하면 자동으로 로깅 대상에 포함됨.

interrupt()로 사람 입력을 기다리며 멈추는 노드(substitute_selection,
select_rfq_targets, check_quotations, final_selection)는 멈추는 시점엔
LangGraph가 예외를 던져서 함수가 끝까지 실행이 안 되고 멈추므로(정상
동작), 그때는 로깅도 자동으로 건너뛰어짐 - 사람이 답을 줘서 resume되고
함수가 끝까지 실행돼 실제 Command를 반환하는 시점에만 로깅됨.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import START, StateGraph

from .process_commands import (
    PurchaseProcessState,
    await_supplier_pr_response_command,
    await_order_start_command,
    check_mr_item_command,
    check_quotations_command,
    create_po_command,
    create_pr_command,
    create_rfq_command,
    decide_bidding_choice_command,
    final_selection_command,
    handle_pr_rejection_command,
    inspect_selected_supplier_documents_command,
    po_approval_command,
    request_pr_command,
    resolve_suppliers_choice_command,
    route_entrypoint_command,
    search_new_suppliers_command,
    select_rfq_targets_command,
    substitute_selection_command,
    review_supplier_documents_command,
)


def _with_status_log(node_name: str, fn):
    """
    노드 함수 하나를 감싸서, 실행 전/후 state["status"]를 비교해
    case_status_history에 자동 기록. state["case_id"]가 없으면(케이스
    없이 도는 상황) 조용히 스킵 - case_logging.log_status_change 자체가
    case_id=None을 안전하게 허용함.
    """
    from functools import wraps

    @wraps(fn)
    def wrapper(state: Any) -> Any:
        from backend_logic2.nodes.supplier.tools.case_logging import log_status_change

        cmd = fn(state)

        update = getattr(cmd, "update", None) or {}
        case_id = update.get("case_id") or state.get("case_id")
        to_status = update.get("status")
        if case_id and to_status:
            # ⚠️ from_status를 여기서 state.get("status")로 미리 캡처해서 넘기면
            # 안 됨(예전엔 그랬었고, 실제로 버그였음) - 이 노드 함수(fn) 안에서
            # supplier_search() 같은 서브파이프라인이 log_status_change()를
            # 여러 번 직접 호출해서 DB status를 이미 몇 단계 더 바꿔놓은 뒤일
            # 수 있음(예: search_new_suppliers_command 안에서 searching ->
            # collected -> search_completed까지 갔는데, 노드 시작 시점 state는
            # 여전히 resolving_supplier_pool). 그 상태에서 fn 실행 전 값을
            # from_status로 넘기면 case_status_history 체인이 끊겨 보임(중간
            # 단계가 통째로 사라진 것처럼). from_status를 아예 안 넘기고
            # log_status_change() 자체의 자동조회(현재 DB status)에 맡기면
            # 항상 진짜 마지막 상태부터 이어짐.
            reason = update.get("error") or f"[{node_name}] 처리 완료"
            log_status_change(case_id, to_status, reason=reason)

        return cmd

    return wrapper


def build_process_graph(*, checkpointer: Any = None):
    graph = StateGraph(PurchaseProcessState)
    graph.add_node("route_entrypoint", _with_status_log("route_entrypoint", route_entrypoint_command))
    graph.add_node("check_mr_item", _with_status_log("check_mr_item", check_mr_item_command))
    graph.add_node("substitute_selection", _with_status_log("substitute_selection", substitute_selection_command))
    graph.add_node("decide_bidding_choice", _with_status_log("decide_bidding_choice", decide_bidding_choice_command))
    graph.add_node(
        "resolve_suppliers_choice",
        _with_status_log("resolve_suppliers_choice", resolve_suppliers_choice_command),
    )
    graph.add_node("search_new_suppliers", _with_status_log("search_new_suppliers", search_new_suppliers_command))
    graph.add_node("select_rfq_targets", _with_status_log("select_rfq_targets", select_rfq_targets_command))
    graph.add_node("create_rfq", _with_status_log("create_rfq", create_rfq_command))
    graph.add_node("check_quotations", _with_status_log("check_quotations", check_quotations_command))
    graph.add_node("final_selection", _with_status_log("final_selection", final_selection_command))
    graph.add_node(
        "inspect_selected_supplier_documents",
        _with_status_log(
            "inspect_selected_supplier_documents",
            inspect_selected_supplier_documents_command,
        ),
    )
    graph.add_node(
        "review_supplier_documents",
        _with_status_log(
            "review_supplier_documents",
            review_supplier_documents_command,
        ),
    )
    graph.add_node("await_order_start", _with_status_log("await_order_start", await_order_start_command))
    graph.add_node("po_approval", _with_status_log("po_approval", po_approval_command))
    graph.add_node("request_pr", _with_status_log("request_pr", request_pr_command))
    graph.add_node("create_pr", _with_status_log("create_pr", create_pr_command))
    graph.add_node("await_supplier_pr_response", _with_status_log("await_supplier_pr_response", await_supplier_pr_response_command))
    graph.add_node("handle_pr_rejection", _with_status_log("handle_pr_rejection", handle_pr_rejection_command))
    graph.add_node("create_po", _with_status_log("create_po", create_po_command))
    graph.add_edge(START, "route_entrypoint")
    return graph.compile(checkpointer=checkpointer)


_APP = None
_CONNECTION = None


def get_process_app():
    global _APP, _CONNECTION
    if _APP is None:
        checkpoint_path = Path(__file__).resolve().parents[1] / "process_checkpoints.sqlite"
        _CONNECTION = sqlite3.connect(checkpoint_path, check_same_thread=False)
        _APP = build_process_graph(checkpointer=SqliteSaver(_CONNECTION))
    return _APP


def delete_thread_checkpoints(thread_id: str) -> None:
    """이 thread_id의 체크포인트를 process_checkpoints.sqlite에서 완전히 지운다.

    ERPNext에서 Material Request가 삭제돼도 이 SQLite는 별도 저장소라
    자동으로 같이 지워지지 않는다 - 방치하면 파일이 계속 커지고, 나중에
    같은 MR 번호가 재사용될 때 옛 실행의 흔적과 혼동될 여지도 남는다
    (실제로는 material_request_thread_id()가 recreated 케이스에 새
    thread_id를 발급해 그 혼동 자체는 막아주지만, 옛 thread_id의
    체크포인트는 여전히 안 지워진 채로 남는다). 호출하는 쪽
    (workflow_service._close_case_missing_in_erp)에서 "ERPNext에 더 이상
    이 MR이 없다"고 확인한 시점에만 불러야 한다.
    """
    app = get_process_app()
    app.checkpointer.delete_thread(str(thread_id))
