"""
pipeline_nodes.py — 노드 함수 모음

⚠️ check_substitute_node는 아직 팀원 담당 · 스텁만 있음.
classify_route_node는 팀원이 만든 bidding_decision.py의 decide_bidding()을
연결해서 실제로 동작함 (2갈래: catalog / bidding — needs_review 없음).
"""

from bidding_decision import decide_bidding


def check_substitute_node(state):
    """
    [팀원 담당 · 스텁] 대체품이 있는지 확인.

    구현할 때 지켜야 할 반환 형식:
      state["substitute_check"] = "found" | "none_found"
      찾았으면 state["substitute_item"]에 대체품 정보도 채워줄 것
    """
    print(f"[check_substitute_node] (스텁) '{state['item_code']}' 대체품 확인 중...")
    return {**state, "substitute_check": "none_found", "substitute_item": None}


def classify_route_node(state):
    """
    카탈로그/비딩 판별. bidding_decision.py의 decide_bidding()이 Material
    Request 전체(모든 품목)를 보고 판단함 — 단일 품목이 아니라 mr_name 기준.

    ⚠️ needs_review 경로 없음 — bidding_required가 True/False 둘 중 하나로만 나옴.
    """
    decision = decide_bidding(state["mr_name"])
    route = "bidding" if decision.bidding_required else "catalog"
    print(f"[classify_route_node] route={route}, reasons={decision.reasons}")
    return {
        **state,
        "route": route,
        "reasons": list(decision.reasons),
        "bidding_decision": decision.to_dict(),  # 상세 근거 전체 로그용으로 같이 보관
    }


# ---------------- 종료 노드들 (결과 확인용, 최소 구현) ----------------

def use_substitute_node(state):
    msg = f"[use_substitute_node] 대체품으로 처리: {state.get('substitute_item')}"
    print(msg)
    return {**state, "result_message": msg}


def catalog_node(state):
    msg = f"[catalog_node] '{state['item_code']}' 표준구매 채널로 처리"
    print(msg)
    return {**state, "result_message": msg}


def bidding_node(state):
    msg = f"[bidding_node] '{state['item_code']}' RFQ 비딩 파이프라인 진입"
    print(msg)
    return {**state, "result_message": msg}


def needs_review_node(state):
    msg = f"[needs_review_node] '{state['item_code']}' 담당자 확인 필요"
    print(msg)
    return {**state, "result_message": msg}