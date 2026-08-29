"""Compiled LangGraph for the backend_logic2 purchasing process."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import START, StateGraph

from .process_commands import (
    PurchaseProcessState,
    catalog_or_bidding_choice_command,
    catalog_or_bidding_interrupt_command,
    check_quotations_command,
    check_substitutes_command,
    create_po_command,
    create_rfq_command,
    final_selection_command,
    resolve_suppliers_command,
    route_entrypoint_command,
    select_rfq_targets_command,
    substitute_approval_command,
    supplier_source_choice_command,
)


def build_process_graph(*, checkpointer: Any = None):
    graph = StateGraph(PurchaseProcessState)
    graph.add_node("route_entrypoint", route_entrypoint_command)
    graph.add_node("check_substitutes", check_substitutes_command)
    graph.add_node("substitute_approval", substitute_approval_command)
    graph.add_node("catalog_or_bidding_choice", catalog_or_bidding_choice_command)
    graph.add_node("catalog_or_bidding_interrupt", catalog_or_bidding_interrupt_command)
    graph.add_node("resolve_suppliers_choice", resolve_suppliers_command)
    graph.add_node("supplier_source_choice", supplier_source_choice_command)
    graph.add_node("select_rfq_targets", select_rfq_targets_command)
    graph.add_node("create_rfq", create_rfq_command)
    graph.add_node("check_quotations", check_quotations_command)
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