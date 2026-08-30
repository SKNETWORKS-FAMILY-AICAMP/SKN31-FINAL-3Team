"""Compiled LangGraph for the backend_logic2 purchasing process.

주의: 8단계(check_mr_item+substitute_selection, decide_bidding_choice,
resolve_suppliers_choice, search_new_suppliers, select_rfq_targets,
create_rfq, check_quotations, final_selection)까지 등록됨. 다음 단계
(PO 생성+발송) 합의되면 add_node로 이어서 추가할 예정.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import START, StateGraph

from .process_commands import (
    PurchaseProcessState,
    check_mr_item_command,
    check_quotations_command,
    create_rfq_command,
    decide_bidding_choice_command,
    final_selection_command,
    resolve_suppliers_choice_command,
    route_entrypoint_command,
    search_new_suppliers_command,
    select_rfq_targets_command,
    substitute_selection_command,
)


def build_process_graph(*, checkpointer: Any = None):
    graph = StateGraph(PurchaseProcessState)
    graph.add_node("route_entrypoint", route_entrypoint_command)
    graph.add_node("check_mr_item", check_mr_item_command)
    graph.add_node("substitute_selection", substitute_selection_command)
    graph.add_node("decide_bidding_choice", decide_bidding_choice_command)
    graph.add_node("resolve_suppliers_choice", resolve_suppliers_choice_command)
    graph.add_node("search_new_suppliers", search_new_suppliers_command)
    graph.add_node("select_rfq_targets", select_rfq_targets_command)
    graph.add_node("create_rfq", create_rfq_command)
    graph.add_node("check_quotations", check_quotations_command)
    graph.add_node("final_selection", final_selection_command)
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