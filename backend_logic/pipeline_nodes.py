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
import requests
from typing import List, Literal, Optional

from dotenv import load_dotenv
from langgraph.types import Command, interrupt

from erp_client import (
    get_stock_level,
    get_item_name,
    get_saved_substitute,
    save_substitute_to_erp,
    get_all_available_candidates,
    erp_get_one,
    erp_post,
    ERPNextAPIError,
    create_rfq_from_material_request,
    send_rfq_with_portal_access,
)
from bidding_decision import decide_bidding

load_dotenv()
# Tavily 완전히 안 씀 — 벤더검색·연락처확보 다 네이버 웹문서검색으로 대체함

_EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_PHONE_PATTERN = re.compile(r"(0\d{1,2}[-.\s]?\d{3,4}[-.\s]?\d{4})")

TARGET_SUPPLIER_COUNT = 10  # 신규탐색 시 이메일 확보된 후보를 몇 개 채울지


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

    if found:
        print(f"  기존 승인 공급사 목록:")
        for s in suppliers:
            supplier_name = s.get("supplier") or s.get("name")
            print(f"    - {supplier_name}")

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


def _clean_search_term(item_name: str) -> str:
    """
    검색 질의 만들 때만 쓰는 정리 함수 — item_name 원본은 안 건드림 (RFQ 등엔
    원래 이름 그대로 나가야 함). "#132" 같은 내부 관리용 코드/번호가 붙어있으면
    검색엔진·임베딩엔 노이즈만 되므로 떼어냄.
    """
    cleaned = re.sub(r"#\s*\d+", "", item_name)  # "#132" 같은 패턴 제거
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or item_name  # 다 지워져서 빈 문자열이면 원본으로 되돌림


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

    query_embedding = _get_query_embedding(_clean_search_term(item_name))

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


def search_new_vendor_candidates(item_name: str, max_results: int = 15) -> List[dict]:
    """
    새 벤더 후보 자체를 웹에서 찾음. 네이버 웹문서검색 사용 (Tavily 안 씀).
    이메일 없는 후보가 많이 걸러질 걸 감안해서 넉넉하게 뽑음 (기본 15개).
    """
    items = _search_naver_web(f"{_clean_search_term(item_name)} 공급업체 제조사 견적", display=max_results)
    return [{"name": r.get("title", ""), "source_url": r.get("link", ""), "raw_snippet": r.get("description", "")} for r in items]


_EXCLUDED_CONTACT_DOMAINS = [
    "jobkorea.co.kr", "saramin.co.kr", "wanted.co.kr", "catch.co.kr",
    "incruit.com", "albamon.com", "job.co.kr", "linkedin.com",
]


def _search_naver_web(query: str, display: int = 5) -> list:
    """네이버 웹문서검색 공통 헬퍼. 응답의 title/description엔 <b> 태그가 섞여있어서 제거함."""
    try:
        res = requests.get(
            "https://naverapihub.apigw.ntruss.com/search/v1/webkr",
            headers={
                "X-NCP-APIGW-API-KEY-ID": os.environ["NAVER_CLIENT_ID"],
                "X-NCP-APIGW-API-KEY": os.environ["NAVER_CLIENT_SECRET"],
            },
            params={"query": query, "display": display},
            timeout=10,
        )
        res.raise_for_status()
        items = res.json().get("items", [])
    except Exception as e:
        print(f"[_search_naver_web] 검색 실패 ('{query}'): {e}")
        return []

    for item in items:
        item["title"] = re.sub(r"<.*?>", "", item.get("title", ""))
        item["description"] = re.sub(r"<.*?>", "", item.get("description", ""))
    return items


def _find_official_site(company_name: str) -> Optional[dict]:
    """
    회사의 공식 홈페이지로 추정되는 검색결과 1건을 찾음.
    네이버 웹문서검색 사용 (무료 할당량 안에서 처리, Tavily 안 씀).
    채용사이트 등은 결과에서 직접 걸러냄 (Tavily의 exclude_domains 같은
    파라미터가 없어서, 반환된 링크 도메인을 파이썬에서 직접 확인).
    """
    items = _search_naver_web(f"{company_name} 공식 홈페이지")

    for item in items:
        link = item.get("link", "")
        if any(domain in link for domain in _EXCLUDED_CONTACT_DOMAINS):
            continue  # 채용사이트 등은 건너뜀
        return {"url": link, "content": item.get("description", "")}

    return None  # 걸러내고 남은 게 하나도 없으면 못 찾은 것


def _fetch_page_text(url: str, max_chars: int = 6000) -> str:
    """
    URL을 직접 요청해서 HTML 태그 벗겨낸 텍스트만 반환 (Tavily 안 씀).
    네이버 검색 스니펫보다 훨씬 풍부한 본문을 무료로 확보하기 위함.
    """
    if not url:
        return ""
    try:
        res = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        res.raise_for_status()
        text = re.sub(r"<script.*?</script>", " ", res.text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<.*?>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars]
    except Exception as e:
        print(f"[_fetch_page_text] 페이지 가져오기 실패 ({url}): {e}")
        return ""


def _extract_contact_from_page(company_name: str, source_url: str, page_text: str) -> dict:
    """
    공식 홈페이지로 추정되는 페이지 '하나'의 내용에서 이메일·전화번호·회사명(clean_name) 추출.
    여러 문서를 모아놓고 AI가 어디서 뽑을지 고민하게 하는 대신, 신뢰할 만한
    출처 하나로 미리 좁혀놓고 그 안에서만 뽑게 함 — 훨씬 더 정확함.

    ⚠️ clean_name을 추가한 이유: 웹검색 결과의 title(페이지 제목)을 그대로
    회사명으로 쓰면, "\"플라스틱 용접\" - 중국 제조 업체, 공급 업체 및 제품"
    같은 깨진 텍스트가 그대로 ERPNext Supplier 이름으로 들어가는 사고가
    실제로 있었음(포털 500 에러의 원인으로 의심됨). AI가 페이지 내용을 보고
    진짜 회사명이 뭔지 판단해서 따로 뽑아줌.
    """
    import json
    from langchain_openai import ChatOpenAI
    from langchain_core.prompts import PromptTemplate

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    prompt = PromptTemplate.from_template(
        "당신은 정보 추출 전문가입니다. 아래는 '{company_name}'의 공식 홈페이지로 "
        "추정되는 페이지({url})의 내용입니다.\n"
        "이 회사의 (1)정확한 회사명, (2)대표 이메일, (3)대표 전화번호를 찾아주세요.\n\n"
        "[페이지 내용]\n{text}\n\n"
        "규칙:\n"
        "- 이 페이지가 진짜 이 회사의 것이 맞는지 스스로 확인하고, 아니라고 "
        "판단되면 clean_name, email, phone 전부 null로 답하세요.\n"
        "- clean_name은 검색결과 제목이나 페이지 <title> 태그 원문을 그대로 "
        "베끼지 말고, 실제 상호명만 간결하게 답하세요 (예: '플라스틱 용접 전문 "
        "업체 - 중국 제조사 목록' 같은 문구가 아니라 실제 회사 이름만).\n"
        "- 페이지 안에 잡코리아·사람인·원티드 같은 채용중개사이트나 다른 "
        "제3자 서비스의 연락처(채용문의, 고객센터 등)가 같이 언급되어 있을 수 "
        "있습니다. 그런 제3자 연락처는 절대 답하지 말고, 반드시 이 회사 "
        "자체의 연락처만 답하세요.\n"
        "- 전화번호와 팩스번호를 명확히 구분하세요. 팩스번호를 전화번호로 답하면 안 됩니다.\n"
        "- 확신이 없으면 해당 값을 null로 답하세요. 틀린 값을 넣는 것보다 모른다고 하는 게 낫습니다.\n"
        "- 반드시 아래 JSON 형식으로만 답하세요, 다른 설명 붙이지 마세요:\n"
        '{{"clean_name": "값 또는 null", "email": "값 또는 null", "phone": "값 또는 null"}}'
    )
    chain = prompt | llm
    result_text = chain.invoke({"company_name": company_name, "url": source_url, "text": page_text[:6000]}).content

    try:
        cleaned = result_text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        parsed = json.loads(cleaned)
        clean_name = parsed.get("clean_name") if parsed.get("clean_name") not in (None, "null", "") else None
        email = parsed.get("email") if parsed.get("email") not in (None, "null", "") else None
        phone = parsed.get("phone") if parsed.get("phone") not in (None, "null", "") else None
    except (json.JSONDecodeError, AttributeError) as e:
        print(f"[_extract_contact_from_page] '{company_name}' AI 응답 파싱 실패: {e}")
        return {"clean_name": None, "email": None, "phone": None}

    # 코드로 한 번 더 검증 — AI가 프롬프트 지시를 놓쳐도, 이메일 도메인이
    # 제3자 서비스면 여기서 무조건 걸러냄 (실제로 잡코리아 이메일이 샌 적 있어서 추가함)
    if email and any(f"@{domain}" in email or email.endswith(f"@{domain}") for domain in _EXCLUDED_CONTACT_DOMAINS):
        print(f"[_extract_contact_from_page] '{company_name}' 추출된 이메일({email})이 제3자 도메인이라 폐기")
        email = None

    return {"clean_name": clean_name, "email": email, "phone": phone}


# [보류] 카카오 로컬 API — 전화번호 전용 구조화 조회, 지금은 안 씀
# (연락처가 그렇게까지 중요하지 않다고 판단, 웹문서검색+AI로 통합함.
#  나중에 히트율이 너무 낮으면 이거 다시 살려서 쓰면 됨)
# def _search_kakao_local_phone(company_name: str) -> Optional[str]:
#     try:
#         res = requests.get(
#             "https://dapi.kakao.com/v2/local/search/keyword.json",
#             headers={"Authorization": f"KakaoAK {os.environ['KAKAO_REST_API_KEY']}"},
#             params={"query": company_name, "size": 1},
#             timeout=10,
#         )
#         res.raise_for_status()
#         documents = res.json().get("documents", [])
#     except Exception as e:
#         print(f"[_search_kakao_local_phone] 카카오 검색 실패 ({company_name}): {e}")
#         return None
#     if not documents:
#         return None
#     phone = documents[0].get("phone", "").strip()
#     return phone or None


def _enrich_contact_info(vendor: dict) -> dict:
    """
    후보 업체의 이메일·전화번호 확보. 네이버 웹문서검색으로 공식사이트를
    찾고, 그 페이지 내용을 AI로 추출해서 이메일·전화 둘 다 채움.
    """
    name = vendor.get("name", "")

    if vendor.get("email") and vendor.get("phone"):
        return vendor  # 둘 다 채워졌으면 더 안 봐도 됨

    name = vendor.get("name", "")
    site = _find_official_site(name)

    if not site:
        print(f"[_enrich_contact_info] '{name}' 공식 홈페이지로 추정되는 곳을 못 찾음")
        return vendor

    site_url = site.get("url", "")
    page_text = site.get("content", "")  # 네이버 검색 스니펫 (기본값)

    # 직접 페이지를 가져와서 스니펫보다 풍부한 본문 확보 시도 (Tavily 안 씀, 실패하면 스니펫으로 진행)
    fetched = _fetch_page_text(site_url)
    if fetched:
        page_text = fetched

    extracted = _extract_contact_from_page(name, site_url, page_text)

    # 검색결과 title을 그대로 회사명으로 썼던 게 문제였어서, AI가 페이지에서
    # 직접 확인한 정식 회사명이 있으면 그걸로 교체함 (Supplier 이름 깨짐 방지)
    if extracted.get("clean_name"):
        print(f"[_enrich_contact_info] 회사명 정리: '{name}' → '{extracted['clean_name']}'")
        vendor["name"] = extracted["clean_name"]

    if not vendor.get("email") and extracted.get("email"):
        vendor["email"] = extracted["email"]
    if not vendor.get("phone") and extracted.get("phone"):
        vendor["phone"] = extracted["phone"]

    if not vendor.get("email"):
        print(f"[_enrich_contact_info] '{name}' 신뢰할 만한 이메일 못 찾음")
    if not vendor.get("phone"):
        print(f"[_enrich_contact_info] '{name}' 신뢰할 만한 전화번호 못 찾음")

    return vendor


def find_qualified_suppliers(item_name, target_count=10):
    """
    1) RAG로 1차 후보 target_count개 추림
    2) 각각 네이버 웹문서검색+AI로 이메일·전화 보강
    3) 이메일 없으면 가차없이 버림 (RFQ 못 보내는 후보는 의미 없음)
    4) 버려서 부족해진 만큼 네이버 웹문서검색으로 새 후보 찾아서 target_count개 맞춤

    ⚠️ 중복 방지를 "이름"만으로 하면 안 됨 — RAG랑 네이버 검색에서 같은 회사가
    서로 다른 이름 문자열로 나올 수 있어서(예: "허닝스톤즈" vs "Honing Stones"),
    실제로 같은 회사한테 RFQ가 두 번 나간 사고가 있었음. 그래서 이메일이
    확인된 후에는 이메일 기준으로도 한 번 더 중복 체크함.
    """
    qualified = []
    seen_names = set()
    seen_emails = set()

    def add_if_qualified(vendor):
        key = vendor.get("name", "").strip().lower()
        if not key or key in seen_names:
            return False
        vendor = _enrich_contact_info(vendor)

        email = vendor.get("email")
        if not email:
            print(f"  '{vendor.get('name')}' 이메일 없음 → 버림")
            return False

        email_key = email.strip().lower()
        if email_key in seen_emails:
            print(f"  '{vendor.get('name')}' 이메일({email})이 이미 확보된 다른 업체와 동일 → 중복으로 버림")
            return False

        qualified.append(vendor)
        seen_names.add(key)
        seen_emails.add(email_key)
        return True

    # 1) RAG 1차 후보
    rag_candidates = rag_search_past_vendors(item_name, limit_vendors=target_count)
    print(f"[find_qualified_suppliers] RAG 1차 후보 {len(rag_candidates)}건 → 이메일 확인 중...")
    for c in rag_candidates:
        add_if_qualified(c)

    print(f"[find_qualified_suppliers] RAG 중 이메일 확보 {len(qualified)}/{target_count}건")

    # 2) 부족한 만큼 네이버 웹문서검색으로 새 후보 찾아서 채움
    if len(qualified) < target_count:
        needed = target_count - len(qualified)
        print(f"[find_qualified_suppliers] {needed}건 부족 → 네이버 웹문서검색으로 보충")

        web_candidates = search_new_vendor_candidates(item_name)
        for c in web_candidates:
            if len(qualified) >= target_count:
                break
            add_if_qualified(c)

    print(f"[find_qualified_suppliers] 최종 확보: {len(qualified)}/{target_count}건")
    return qualified


def search_and_enrich_vendors_node(state) -> Command[Literal["create_rfq_node"]]:
    """
    기존 공급사가 없을 때, 이메일까지 확보된 신규 후보 10곳을 채워서 넘김.
    """
    item_name = state["item_name"]

    qualified = find_qualified_suppliers(item_name, target_count=TARGET_SUPPLIER_COUNT)

    if not qualified:
        decision = interrupt({
            "reason": "no_vendor_candidates_found",
            "item_name": item_name,
            "question": "이메일이 확보된 후보 업체를 하나도 찾지 못했습니다. 수동으로 입력하시겠습니까?",
        })
        qualified = decision.get("manual_vendors", []) if isinstance(decision, dict) else []

    return Command(
        update={"existing_suppliers": state["existing_suppliers"] + qualified},
        goto="create_rfq_node",
    )


def _ensure_supplier_exists(name, email=None):
    """
    이 이름의 Supplier가 ERPNext에 이미 있는지 확인하고, 없으면 새로 생성.
    RFQ의 suppliers 필드는 Link라서, 존재하지 않는 이름을 그냥 넣으면
    LinkValidationError로 RFQ 생성 자체가 실패함 (실제로 겪은 에러).

    ⚠️ 이름 정제(sanitize)를 마지막 안전장치로 한 번 더 함 — AI가 clean_name을
    잘 뽑아주는 게 기본이지만, 혹시 놓쳐도 여기서 HTML 인코딩(&quot; 등)이나
    비정상적으로 긴 텍스트가 그대로 Supplier 이름으로 들어가는 걸 막음
    (실제로 이것 때문에 포털 페이지가 500 에러로 깨진 적 있음).

    반환: (등록된 이름, 이메일) — 등록 실패하면 (None, None)
    """
    import html as _html

    name = _html.unescape(name).strip()
    if len(name) > 140:  # ERPNext Supplier 이름 필드 길이 여유있게 제한
        name = name[:140].strip()

    try:
        existing = erp_get_one("Supplier", name)
        return name, (existing.get("email_id") or email)
    except ERPNextAPIError:
        pass  # 없다는 뜻, 아래에서 새로 만듦

    payload = {
        "supplier_name": name,
        "supplier_group": "All Supplier Groups",
        "country": "Korea, Republic of",
        "supplier_type": "Company",
    }
    if email:
        payload["email_id"] = email

    try:
        created = erp_post("Supplier", payload)
        print(f"[_ensure_supplier_exists] 신규 Supplier 등록: {name}")
        return created["name"], email
    except ERPNextAPIError as e:
        print(f"[_ensure_supplier_exists] '{name}' 등록 실패: {e}")
        return None, None


def create_rfq_node(state) -> dict:
    """
    existing_suppliers에 담긴 대상(기존 승인공급사 or 신규발굴+연락처보강된 벤더)
    전체를 대상으로 RFQ 생성 + 발송.

    ⚠️ RFQ 만들기 전에, 아직 ERPNext Supplier로 없는 신규 벤더는 먼저
    Supplier로 등록함 (RAG/Tavily로 찾은 후보는 이 시점까지 ERPNext에
    한 번도 안 올라가있는 상태라서, 등록 없이 RFQ 만들면 에러남).
    """
    supplier_names, supplier_emails = [], {}

    for s in state["existing_suppliers"]:
        raw_name = s.get("supplier") or s.get("name")
        if not raw_name:
            continue

        registered_name, email = _ensure_supplier_exists(raw_name, s.get("email"))
        if not registered_name:
            print(f"[create_rfq_node] '{raw_name}' 등록 실패, RFQ 대상에서 제외")
            continue

        supplier_names.append(registered_name)
        if email:
            supplier_emails[registered_name] = email

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