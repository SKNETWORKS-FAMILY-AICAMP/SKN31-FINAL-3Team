"""
pipeline_nodes.py — 노드 함수 모음 (통합본)

- check_stock_node: 재고 확인 (직접 구현)
- check_substitute_node, human_interaction_node, save_and_end_node:
  팀원#3 코드를 가져와 state 필드명만 우리 규칙(item_code 등)에 맞춤
- classify_route_node: 팀원#2의 bidding_decision.py 연결

⚠️ human_interaction_node는 아직 터미널 input()으로 사람 입력을 "기다리는"
블로킹 코드임. watcher.py처럼 계속 도는 백그라운드 루프에 이대로 물리면
그 순간 전체가 멈춤. 지금은 손으로 테스트하는 단계라 이대로 두지만,
나중에 대시보드 UI 만들 때 LangGraph의 interrupt()로 반드시 교체할 것.
"""

from erp_client import (
    get_stock_level,
    get_item_name,
    get_saved_substitute,
    save_substitute_to_erp,
    get_all_available_candidates,
)
from bidding_decision import decide_bidding


# ============================================================
# 재고 확인
# ============================================================

def check_stock_node(state):
    """지금 이 순간 재고로 요청 수량이 충당되는지 확인."""
    item_name = get_item_name(state["item_code"])
    stock = get_stock_level(state["item_code"], state["warehouse"])
    current_qty = stock["actual_qty"] if stock else 0

    result = "sufficient" if current_qty >= state["qty"] else "insufficient"
    print(f"[check_stock_node] '{item_name}({state['item_code']})' 재고={current_qty}, 요청={state['qty']} → {result}")
    return {**state, "item_name": item_name, "stock_check": result}


def stock_sufficient_node(state):
    msg = f"[stock_sufficient_node] '{state['item_code']}' 재고 충분, 구매 불필요"
    print(msg)
    return {**state, "result_message": msg}


# ============================================================
# 대체품 확인 (팀원#3 로직 — state 필드명만 우리 규칙에 맞춤)
# ============================================================

def check_substitute_node(state):
    """
    저장된 대체품 이력을 먼저 확인하고, 없거나 품절이면 AI로 재고 중
    비슷한 대체품 후보를 추려서 candidates에 담아둠 (최종선택은 사람이 함).
    """
    item_code = state["item_code"]
    item_name = state["item_name"]
    warehouse = state["warehouse"]

    print(f"[check_substitute_node] '{item_name}'의 과거 대체품 이력 조회...")
    saved_substitute = get_saved_substitute(item_code)

    if saved_substitute:
        stock_info = get_stock_level(saved_substitute, warehouse)
        if stock_info and stock_info["actual_qty"] > 0:
            saved_sub_name = get_item_name(saved_substitute)
            print(f"[check_substitute_node] 과거 이력 발견! '{saved_sub_name}' 자동 지정")
            return {**state, "final_substitute": saved_substitute, "substitute_check": "found"}
        else:
            print(f"[check_substitute_node] 과거 이력 있으나 현재 품절, AI 추천으로 진행")

    # warehouse를 넘기지 않음 → 전체 창고에서 검색 (다른 창고 대체품도 후보로 봄)
    in_stock_dict = get_all_available_candidates()
    in_stock_dict.pop(item_code, None)

    if not in_stock_dict:
        print("[check_substitute_node] 대체할 수 있는 잉여 재고가 전혀 없음")
        return {**state, "candidates": [], "substitute_check": "none_found"}

    ai_filtered_codes = filter_similar_items_with_ai(item_name, in_stock_dict)
    candidates_list = [
        {
            "code": code,
            "name": in_stock_dict[code]["name"],
            "qty": in_stock_dict[code]["qty"],
            "warehouses": in_stock_dict[code]["warehouses"],  # 창고별 상세 — 사람이 판단할 때 씀
        }
        for code in ai_filtered_codes
    ]

    if not candidates_list:
        print("[check_substitute_node] AI 분석 결과 적절한 대체품 없음")

    return {**state, "candidates": candidates_list, "substitute_check": "none_found"}


def filter_similar_items_with_ai(target_name, stock_dict):
    """LLM으로 재고 중 용도가 비슷한 후보만 추려냄 (최대 3개)."""
    from langchain_openai import ChatOpenAI
    from langchain_core.prompts import PromptTemplate

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    catalog_str = "\n".join(
        f"- {code}: {info['name']} (그룹: {info['group']})" for code, info in stock_dict.items()
    )
    prompt = PromptTemplate.from_template(
        "당신은 공장 자재관리 및 구매 전문가입니다.\n"
        "사용자가 '{target_name}'의 대체품을 찾고 있습니다.\n"
        "아래 창고 재고 목록 중에서 '{target_name}'과 가장 용도나 성격이 비슷한 대체품을 최대 3개만 골라주세요.\n\n"
        "[창고 재고 목록]\n{catalog}\n\n"
        "반드시 쉼표로 구분된 품목코드(item_code)만 출력하세요. (예: SF-006, SF-007)\n"
        "적절한 대체품이 전혀 없다면 '없음'이라고 출력하세요."
    )
    print("[filter_similar_items_with_ai] AI가 재고를 분석해 대체품을 추리는 중...")
    chain = prompt | llm
    result = chain.invoke({"target_name": target_name, "catalog": catalog_str}).content

    if "없음" in result:
        return []
    return [code.strip() for code in result.split(",") if code.strip() in stock_dict]


def human_interaction_node(state):
    """
    interrupt()로 멈춤 — input()과 달리, 이 지점에서 그래프 실행 자체가
    "일시정지" 상태로 저장되고, 프로그램(watcher.py)은 안 멈추고 계속 돎.
    사람이 나중에 resume_pending.py로 답하면 그 값이 choice로 들어와서
    여기부터 이어서 실행됨.
    """
    from langgraph.types import interrupt

    if state.get("final_substitute"):
        return {}

    candidates = state.get("candidates", [])
    if not candidates:
        return {"final_substitute": "없음"}

    choice = interrupt({
        "type": "substitute_selection",
        "item_code": state["item_code"],
        "item_name": state["item_name"],
        "candidates": candidates,
    })

    if choice == 0:
        return {"final_substitute": "없음"}
    return {"final_substitute": candidates[choice - 1]["code"]}


def save_and_end_node(state):
    """
    대체품 골랐으면 ERPNext에 저장(다음번 자동재사용용), 없으면 다음 단계
    (classify_route_node, 즉 신규구매 판별)로 넘어갈 준비만 하고 상태 반환.
    """
    final_sub = state.get("final_substitute")
    item_code = state["item_code"]

    if final_sub and final_sub != "없음":
        final_sub_name = get_item_name(final_sub)
        print(f"[save_and_end_node] '{final_sub_name}' 선택 → ERPNext에 저장")
        save_substitute_to_erp(item_code, final_sub)
        msg = f"대체품 확정: {final_sub_name}"
        return {**state, "substitute_check": "found", "substitute_item": final_sub, "result_message": msg}
    else:
        print("[save_and_end_node] 대체품 선택 안 함 → 신규구매 판별로 진행")
        return {**state, "substitute_check": "none_found"}


def use_substitute_node(state):
    msg = f"[use_substitute_node] 대체품으로 처리: {state.get('final_substitute')}"
    print(msg)
    return {**state, "result_message": msg}


# ============================================================
# 카탈로그/비딩 판별 (팀원#2)
# ============================================================

def classify_route_node(state):
    """
    카탈로그/비딩 판별. bidding_decision.py의 decide_bidding()이 Material
    Request 전체(모든 품목)를 보고 판단함 — mr_name 기준.
    ⚠️ needs_review 경로 없음 — True/False 둘 중 하나로만 나옴.
    """
    decision = decide_bidding(state["mr_name"])
    route = "bidding" if decision.bidding_required else "catalog"
    print(f"[classify_route_node] route={route}, reasons={decision.reasons}")
    return {
        **state,
        "route": route,
        "reasons": list(decision.reasons),
        "bidding_decision": decision.to_dict(),
    }


def catalog_node(state):
    msg = f"[catalog_node] '{state['item_code']}' 표준구매 채널로 처리"
    print(msg)
    return {**state, "result_message": msg}


def bidding_node(state):
    msg = f"[bidding_node] '{state['item_code']}' RFQ 비딩 파이프라인 진입"
    print(msg)
    return {**state, "result_message": msg}