"""Compiled LangGraph for the backend_logic2 purchasing process."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import START, StateGraph

from .process_commands import (
    PurchaseProcessState,
    create_po_command,
    create_rfq_command,
    decide_bidding_command,
    final_selection_command,
    find_substitutes_command,
    inspect_mr_command,
    mr_approval_command,
    quotation_deadline_command,
    resolve_suppliers_command,
    route_entrypoint_command,
    supplier_approval_command,
)


def build_process_graph(*, checkpointer: Any = None):
    graph = StateGraph(PurchaseProcessState)
    graph.add_node("route_entrypoint", route_entrypoint_command)
    graph.add_node("inspect_mr", inspect_mr_command)
    graph.add_node("find_substitutes", find_substitutes_command)
    graph.add_node("mr_approval", mr_approval_command)
    graph.add_node("decide_bidding", decide_bidding_command)
    graph.add_node("resolve_suppliers", resolve_suppliers_command)
    graph.add_node("supplier_approval", supplier_approval_command)
    graph.add_node("create_rfq", create_rfq_command)
    graph.add_node("quotation_deadline", quotation_deadline_command)
    graph.add_node("final_selection", final_selection_command)
    graph.add_node("create_po", create_po_command)
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
