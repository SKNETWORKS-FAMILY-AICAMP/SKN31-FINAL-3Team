"""
pipeline_nodes.py — 노드 함수 모음 (통합본)

- check_stock_node: 재고 확인
- check_substitute_node, human_interaction_node, save_and_end_node: 대체품 확인
- classify_route_node: 팀원#2의 bidding_decision.py 연결
- check_existing_suppliers_node, search_and_enrich_vendors_node, create_rfq_node:
  벤더 발굴 + RFQ 생성 (팀원#3 코드 기반, 최소화해서 통합)

⚠️ human_interaction_node는 아직 터미널 input()으로 사람 입력을 "기다리는"
블로킹 코드임. watcher.py처럼 계속 도는 백그라운드 루프에 이대로 물리면
그 순간 전체가 멈춤. 지금은 손으로 테스트하는 단계라 이대로 두지만,
나중에 대시보드 UI 만들 때 LangGraph의 interrupt()로 반드시 교체할 것.
"""

import os
import re
from typing import List, Literal

from dotenv import load_dotenv
from tavily import TavilyClient
from langgraph.types import Command, interrupt

from erp_client import (
    get_stock_level,
    get_item_name,
    get_saved_substitute,
    save_substitute_to_erp,
    get_all_available_candidates,
    erp_get_one,
    ERPNextAPIError,
    create_rfq_from_material_request,
    send_rfq_with_portal_access,
)
from bidding_decision import decide_bidding

load_dotenv()
_tavily_client = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])

_EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_PHONE_PATTERN = re.compile(r"(0\d{1,2}[-.\s]?\d{3,4}[-.\s]?\d{4})")

MIN_RAG_CANDIDATES = 3  # RAG 결과가 이 개수 미만이면 Tavily로 보완


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
# 대체품 확인
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
            "warehouses": in_stock_dict[code]["warehouses"],
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


# ============================================================
# 벤더 발굴 + RFQ 생성 (팀원#3 코드 기반, 최소화)
#
# Tavily 쓰임새 2가지:
#   ① RAG 후보 부족할 때 — 새 벤더 후보 자체를 찾기
#   ② 찾은 후보의 이메일·전화번호 검색 — RFQ는 이메일 필수라서 꼭 필요
# 사업자번호 검증(국세청 API)은 필요없다고 판단, 뺌.
# ============================================================

def check_existing_suppliers_node(state) -> Command[
    Literal["create_rfq_node", "search_and_enrich_vendors_node"]
]:
    """Item.supplier_items(기존 승인 공급사)가 있으면 바로 RFQ로, 없으면 벤더 발굴로."""
    item_code = state["item_code"]

    try:
        item = erp_get_one("Item", item_code)
    except ERPNextAPIError as e:
        print(f"[check_existing_suppliers_node] Item 조회 실패: {e}")
        item = None

    if not item:
        return Command(update={"supplier_found": False, "existing_suppliers": []}, goto="search_and_enrich_vendors_node")

    suppliers = item.get("supplier_items", [])
    item_name = item.get("item_name", state.get("item_name", item_code))
    found = len(suppliers) > 0

    print(f"[check_existing_suppliers_node] '{item_name}({item_code})' 기존 공급사 {len(suppliers)}건 → "
          f"{'있음' if found else '없음'}")

    return Command(
        update={"item_name": item_name, "existing_suppliers": suppliers, "supplier_found": found},
        goto="create_rfq_node" if found else "search_and_enrich_vendors_node",
    )


_embedding_model = None  # 모듈 로드 시 한 번만 채워지는 캐시 (매번 새로 안 부름)


def _get_query_embedding(text: str) -> list:
    """
    질의 텍스트를 BGE 모델로 임베딩. procurement_item_category_bge 테이블의
    임베딩과 같은 모델이어야 벡터 비교가 의미 있음.

    ⚠️ 정확히 어떤 BGE 체크포인트인지(bge-large-ko, bge-m3 등) 확인 필요.
    확인되면 아래 model_name만 맞는 값으로 교체.

    모델은 첫 호출 때 한 번만 로드해서 전역에 캐싱함 — 이전 버전은 호출할
    때마다 매번 새로 불러와서(검색어 개수만큼 반복 다운로드/로딩) 느렸음.
    """
    global _embedding_model

    if _embedding_model is None:
        from sentence_transformers import SentenceTransformer
        print("[_get_query_embedding] 임베딩 모델 최초 로딩 중... (한 번만 실행됨)")
        # TODO: 실제 사용된 BGE 모델명으로 교체
        _embedding_model = SentenceTransformer("BAAI/bge-m3")

    return _embedding_model.encode(text).tolist()


def rag_search_past_vendors(item_name: str, limit_categories: int = 5, limit_vendors: int = 10) -> List[dict]:
    """
    Postgres(pgvector) 벤더풀에서 2단계로 검색:
      1) 품목명으로 procurement_item_category_bge에서 의미상 가까운 분류 찾기
      2) 그 분류에 연결된(vendor_item_category) 실제 업체들(vendor_catalog) 조회

    연결정보는 config.py의 PG_* 값을 사용 (SITE_URL/API_KEY처럼 여기서 하드코딩 안 함).
    """
    import psycopg
    PG_HOST = os.environ["PG_HOST"]
    PG_PORT = os.environ["PG_PORT"]
    PG_USER = os.environ["PG_USER"]
    PG_PASSWORD = os.environ["PG_PASSWORD"]
    PG_DBNAME = os.environ["PG_DBNAME"]

    query_embedding = _get_query_embedding(item_name)

    conn = psycopg.connect(
        host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname=PG_DBNAME,
    )
    try:
        with conn.cursor() as cur:
            # 1) 품목명과 의미상 가까운 분류 찾기
            cur.execute("""
                SELECT c.id, c.name, 1 - (b.embedding <=> %s::vector) AS similarity
                FROM procurement_item_category_bge b
                JOIN procurement_item_category c ON c.id = b.category_id
                ORDER BY b.embedding <=> %s::vector
                LIMIT %s
            """, (query_embedding, query_embedding, limit_categories))
            categories = cur.fetchall()  # [(id, name, similarity), ...]

            if not categories:
                return []

            category_ids = [c[0] for c in categories]

            # 2) 그 분류들에 연결된 실제 업체 조회
            cur.execute("""
                SELECT v.id, v.business_no, v.company_name, v.address, v.company_size,
                       v.email, v.phone, v.description, c.name AS category
                FROM vendor_item_category vic
                JOIN vendor_catalog v ON v.id = vic.vendor_id
                JOIN procurement_item_category c ON c.id = vic.category_id
                WHERE vic.category_id = ANY(%s)
                LIMIT %s
            """, (category_ids, limit_vendors))
            rows = cur.fetchall()
    finally:
        conn.close()

    return [
        {
            "name": r[2],
            "business_no": r[1],
            "address": r[3],
            "company_size": r[4],
            "email": r[5],
            "phone": r[6],
            "description": r[7],
            "category": r[8],
        }
        for r in rows
    ]


def tavily_search_vendors(item_name: str) -> List[dict]:
    """Tavily 쓰임새 ① — 새 벤더 후보 자체를 웹에서 찾음"""
    results = _tavily_client.search(f"{item_name} 공급업체 제조사 견적", max_results=5)
    return [{"name": r["title"], "source_url": r["url"], "raw_snippet": r["content"]} for r in results["results"]]


def _dedupe_vendors(vendors: List[dict]) -> List[dict]:
    seen, result = set(), []
    for v in vendors:
        key = v.get("name", "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            result.append(v)
    return result


def _enrich_contact_info(vendor: dict) -> dict:
    """Tavily 쓰임새 ② — 후보 업체의 이메일·전화번호 검색"""
    if vendor.get("email") and vendor.get("phone"):
        return vendor

    name = vendor.get("name", "")
    try:
        results = _tavily_client.search(query=f"{name} 이메일 연락처 전화번호", max_results=5, search_depth="basic")
    except Exception as e:
        print(f"[_enrich_contact_info] Tavily 검색 실패 ({name}): {e}")
        return vendor

    combined_text = " ".join(f"{r.get('title', '')} {r.get('content', '')}" for r in results.get("results", []))

    if not vendor.get("email"):
        m = _EMAIL_PATTERN.search(combined_text)
        if m:
            vendor["email"] = m.group(0)

    if not vendor.get("phone"):
        m = _PHONE_PATTERN.search(combined_text)
        if m:
            vendor["phone"] = m.group(1)

    return vendor


def search_and_enrich_vendors_node(state) -> Command[Literal["create_rfq_node"]]:
    """
    RAG 먼저 → 부족하면 Tavily로 후보 보완 → 각 후보 이메일/전화번호 Tavily로 채움.
    이메일 없는 후보는 RFQ 자체를 못 보내므로 걸러내거나 사람에게 확인받음.
    """
    item_name = state["item_name"]

    rag_candidates = rag_search_past_vendors(item_name)
    print(f"[search_and_enrich_vendors_node] RAG 후보 {len(rag_candidates)}건")

    if len(rag_candidates) >= MIN_RAG_CANDIDATES:
        merged = _dedupe_vendors(rag_candidates)
        print("[search_and_enrich_vendors_node] RAG만으로 충분 → Tavily 후보탐색 생략")
    else:
        web_candidates = tavily_search_vendors(item_name)
        merged = _dedupe_vendors(rag_candidates + web_candidates)
        print(f"[search_and_enrich_vendors_node] RAG 부족 → Tavily 보완, 총 {len(merged)}건")

    if not merged:
        decision = interrupt({
            "reason": "no_vendor_candidates_found",
            "item_name": item_name,
            "question": "후보 업체를 찾지 못했습니다. 수동으로 입력하시겠습니까?",
        })
        merged = decision.get("manual_vendors", []) if isinstance(decision, dict) else []

    enriched = [_enrich_contact_info(v) for v in merged]
    with_email = [v for v in enriched if v.get("email")]
    without_email = [v for v in enriched if not v.get("email")]

    print(f"[search_and_enrich_vendors_node] 이메일 확보 {len(with_email)}건 / 못찾음 {len(without_email)}건")

    if without_email:
        decision = interrupt({
            "reason": "vendor_email_missing",
            "vendors_without_email": without_email,
            "question": "일부 업체 이메일을 못 찾았습니다. 제외하고 진행할까요, 수동 입력할까요?",
        })
        if isinstance(decision, dict) and decision.get("manual_emails"):
            for v in without_email:
                email = decision["manual_emails"].get(v["name"])
                if email:
                    v["email"] = email
                    with_email.append(v)

    return Command(
        update={"existing_suppliers": state["existing_suppliers"] + with_email},
        goto="create_rfq_node",
    )


def create_rfq_node(state) -> dict:
    """
    existing_suppliers에 담긴 대상(기존 승인공급사 or 신규발굴+연락처보강된 벤더)
    전체를 대상으로 RFQ 생성 + 발송.

    ⚠️ 기존 ERPNext 공급사는 email이 바로 안 들어있을 수 있어서, 없으면
    Supplier 문서에서 email_id를 따로 조회함.
    """
    supplier_names, supplier_emails = [], {}

    for s in state["existing_suppliers"]:
        name = s.get("supplier") or s.get("name")
        if not name:
            continue
        supplier_names.append(name)

        email = s.get("email")
        if not email:
            try:
                supplier_doc = erp_get_one("Supplier", name)
                email = supplier_doc.get("email_id") if supplier_doc else None
            except ERPNextAPIError:
                email = None
        if email:
            supplier_emails[name] = email

    if not supplier_names:
        msg = f"[create_rfq_node] '{state['item_code']}' 공급사 후보 없음, RFQ 생성 불가"
        print(msg)
        return {**state, "result_message": msg}

    rfq = create_rfq_from_material_request(state["mr_name"], supplier_names)
    if not rfq:
        msg = f"[create_rfq_node] '{state['item_code']}' RFQ 생성 실패"
        print(msg)
        return {**state, "result_message": msg}

    sent_count = 0
    for name in supplier_names:
        email = supplier_emails.get(name)
        if not email:
            print(f"[create_rfq_node] '{name}' 이메일 없음, 발송 생략")
            continue
        try:
            send_rfq_with_portal_access(rfq["name"], name, email)
            sent_count += 1
        except Exception as e:
            print(f"[create_rfq_node] '{name}' 발송 실패: {e}")

    msg = f"[create_rfq_node] RFQ {rfq['name']} 생성 완료, {sent_count}/{len(supplier_names)}개 공급사에 발송"
    print(msg)
    return {**state, "rfq_name": rfq["name"], "result_message": msg}