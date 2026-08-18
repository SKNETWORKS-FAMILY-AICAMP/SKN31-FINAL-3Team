"""
pipeline_graph.py — 그래프 배선 (통합본)

흐름:
START → check_stock_node
   ├─ 충분 → stock_sufficient_node → END
   └─ 부족 → check_substitute_node
                ├─ (저장된 대체품+재고있음) → save_and_end_node → END
                └─ (없음/AI후보만 있음) → human_interaction_node(⚠️블로킹)
                                              → save_and_end_node
                                                   ├─ 대체품 선택함 → END
                                                   └─ "없음" 선택 → classify_route_node
                                                                        → catalog_node / bidding_node → END
"""

from typing import TypedDict, Literal, Optional
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver
import sqlite3

from pipeline_nodes import (
    check_stock_node,
    stock_sufficient_node,
    check_substitute_node,
    human_interaction_node,
    save_and_end_node,
    use_substitute_node,
    classify_route_node,
    catalog_node,
    bidding_node,
)


class PipelineState(TypedDict):
    item_code: str
    item_name: str
    qty: int
    warehouse: str
    mr_name: str
    stock_check: str            # "sufficient" | "insufficient"
    candidates: list
    final_substitute: Optional[str]
    substitute_check: str       # "found" | "none_found"
    substitute_item: Optional[str]
    route: str                   # "catalog" | "bidding"
    reasons: list
    bidding_decision: dict
    result_message: str


# ---------------- 분기 함수들 ----------------

def stock_decision(state) -> Literal["stock_sufficient_node", "check_substitute_node"]:
    return "stock_sufficient_node" if state["stock_check"] == "sufficient" else "check_substitute_node"


def substitute_found_decision(state) -> Literal["save_and_end_node", "human_interaction_node"]:
    # check_substitute_node에서 저장된 대체품+재고 확인까지 이미 끝났으면 바로 저장단계로
    return "save_and_end_node" if state.get("final_substitute") else "human_interaction_node"


def after_save_decision(state) -> Literal["classify_route_node", "__end__"]:
    # 대체품이 확정됐으면 종료, 없으면 신규구매 판별로
    return END if state.get("substitute_item") else "classify_route_node"


def route_decision(state) -> Literal["catalog_node", "bidding_node"]:
    return {"catalog": "catalog_node", "bidding": "bidding_node"}[state["route"]]


# ---------------- 그래프 조립 ----------------

graph = StateGraph(PipelineState)

graph.add_node("check_stock_node", check_stock_node)
graph.add_node("stock_sufficient_node", stock_sufficient_node)
graph.add_node("check_substitute_node", check_substitute_node)
graph.add_node("human_interaction_node", human_interaction_node)
graph.add_node("save_and_end_node", save_and_end_node)
graph.add_node("classify_route_node", classify_route_node)
graph.add_node("catalog_node", catalog_node)
graph.add_node("bidding_node", bidding_node)

graph.add_edge(START, "check_stock_node")

graph.add_conditional_edges(
    "check_stock_node",
    stock_decision,
    {"stock_sufficient_node": "stock_sufficient_node", "check_substitute_node": "check_substitute_node"},
)

graph.add_conditional_edges(
    "check_substitute_node",
    substitute_found_decision,
    {"save_and_end_node": "save_and_end_node", "human_interaction_node": "human_interaction_node"},
)

graph.add_edge("human_interaction_node", "save_and_end_node")

graph.add_conditional_edges(
    "save_and_end_node",
    after_save_decision,
    {"classify_route_node": "classify_route_node", END: END},
)

graph.add_conditional_edges(
    "classify_route_node",
    route_decision,
    {"catalog_node": "catalog_node", "bidding_node": "bidding_node"},
)

graph.add_edge("stock_sufficient_node", END)
graph.add_edge("catalog_node", END)
graph.add_edge("bidding_node", END)

# 파일 기반(sqlite) 체크포인터 — 그래프가 interrupt()로 멈췄을 때 그 상태를
# 파일에 저장해둠. watcher.py랑 resume_pending.py가 서로 다른 실행이어도
# 같은 파일을 보니까 "이어서 재개"가 가능해짐.
_conn = sqlite3.connect("checkpoints.sqlite", check_same_thread=False)
checkpointer = SqliteSaver(_conn)

app = graph.compile(checkpointer=checkpointer)