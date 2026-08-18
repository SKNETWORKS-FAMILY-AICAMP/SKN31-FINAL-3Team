"""
pipeline_graph.py — 그래프 배선

흐름: START → check_substitute_node → (있음: 종료 / 없음: classify_route_node)
              → classify_route_node → (catalog / bidding — 2갈래, needs_review 없음)

check_stock_node는 이번 스코프에서 뺌.
classify_route_node는 팀원의 bidding_decision.py 기반이라 3갈래가 아니라
2갈래로 갈라짐 (기존 3갈래 버전에서 변경됨).
"""

from typing import TypedDict, Literal
from langgraph.graph import StateGraph, START, END

from pipeline_nodes import (
    check_substitute_node,
    use_substitute_node,
    classify_route_node,
    catalog_node,
    bidding_node,
)


class PipelineState(TypedDict):
    item_code: str
    qty: int
    warehouse: str
    mr_name: str
    substitute_check: str   # "found" | "none_found"
    substitute_item: dict | None
    route: str               # "catalog" | "bidding"
    reasons: list
    bidding_decision: dict
    result_message: str


# ---------------- 분기 함수 ----------------

def substitute_decision(state) -> Literal["use_substitute_node", "classify_route_node"]:
    return "use_substitute_node" if state["substitute_check"] == "found" else "classify_route_node"


def route_decision(state) -> Literal["catalog_node", "bidding_node"]:
    return {
        "catalog": "catalog_node",
        "bidding": "bidding_node",
    }[state["route"]]


# ---------------- 그래프 조립 ----------------

graph = StateGraph(PipelineState)

graph.add_node("check_substitute_node", check_substitute_node)
graph.add_node("use_substitute_node", use_substitute_node)
graph.add_node("classify_route_node", classify_route_node)
graph.add_node("catalog_node", catalog_node)
graph.add_node("bidding_node", bidding_node)

graph.add_edge(START, "check_substitute_node")

graph.add_conditional_edges(
    "check_substitute_node",
    substitute_decision,
    {"use_substitute_node": "use_substitute_node", "classify_route_node": "classify_route_node"},
)

graph.add_conditional_edges(
    "classify_route_node",
    route_decision,
    {"catalog_node": "catalog_node", "bidding_node": "bidding_node"},
)

graph.add_edge("use_substitute_node", END)
graph.add_edge("catalog_node", END)
graph.add_edge("bidding_node", END)

app = graph.compile()