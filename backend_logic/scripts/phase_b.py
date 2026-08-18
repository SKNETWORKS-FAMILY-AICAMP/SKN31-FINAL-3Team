import os
import requests
from typing import TypedDict, List, Literal, Optional
from langgraph.types import Command, interrupt
import re
from typing import Optional
from dotenv import load_dotenv
from tavily import TavilyClient

load_dotenv()

class ProcurementState(TypedDict):
    request_id: str
    item_name: str
    item_category: str
    existing_suppliers: List[dict]
    candidate_vendors: List[dict]   # 신규 발굴 후보 (정제 전)
    cleansed_vendors: List[dict]    # 정제 완료 (RFQ 발송 가능 상태)
    supplier_found: bool


ERP_BASE_URL = os.environ["ERPNEXT_BASE_URL"]
ERP_API_KEY = os.environ["ERPNEXT_API_KEY"]
ERP_API_SECRET = os.environ["ERPNEXT_API_SECRET"]

def _erp_headers():
    return {
        "Authorization": f"token {ERP_API_KEY}:{ERP_API_SECRET}",
        "Content-Type": "application/json",
    }


# ============================================================
# 1) check_existing_suppliers_node
#    있음 -> create_rfq_node / 없음 -> search_new_vendors_node
# ============================================================
def check_existing_suppliers_node(state: ProcurementState) -> Command[
    Literal["create_rfq_node", "search_new_vendors_node"]
]:
    category = state["item_category"]

    try:
        erp_suppliers = query_erpnext_suppliers(category)
    except requests.RequestException as e:
        decision = interrupt({
            "reason": "erpnext_api_error",
            "error": str(e),
            "item_category": category,
            "question": "ERPNext 조회 실패. 로컬 DB만으로 진행할까요, 재시도할까요?",
        })
        erp_suppliers = query_erpnext_suppliers(category) if decision == "retry" else []

    local_suppliers = query_local_supplement_db(category)
    suppliers = erp_suppliers + local_suppliers
    found = len(suppliers) > 0

    return Command(
        update={
            "existing_suppliers": suppliers,
            "supplier_found": found,
        },
        goto="create_rfq_node" if found else "search_new_vendors_node",
    )


def query_erpnext_suppliers(category: str) -> List[dict]:
    params = {
        "filters": f'[["item_group","=","{category}"],["disabled","=",0]]',
        "fields": '["supplier","supplier_name","supplier_group"]',
        "limit_page_length": 0,
    }
    resp = requests.get(
        f"{ERP_BASE_URL}/api/resource/Supplier Item",
        headers=_erp_headers(), params=params, timeout=10,
    )
    resp.raise_for_status()
    return resp.json().get("data", [])

######################
def query_local_supplement_db(category: str) -> List[dict]:
    # TODO: Postgres 등 보조 DB 연동
    # return db.execute(
    #     "SELECT * FROM supplemental_suppliers WHERE category=%s AND status='approved'",
    #     (category,)
    # )
    return []
######################

# ============================================================
# 2) search_new_vendors_node
#    Tavily 웹검색 + 내부 RAG(과거 벤더 이력) 로 후보 업체 탐색
#    항상 다음 -> cleanse_vendor_data_node
# ============================================================
def search_new_vendors_node(state: ProcurementState) -> Command[Literal["cleanse_vendor_data_node"]]:
    category = state["item_category"]
    item_name = state["item_name"]

    web_candidates = tavily_search_vendors(item_name, category)
    rag_candidates = rag_search_past_vendors(item_name, category)

    merged = _dedupe_vendors(web_candidates + rag_candidates)

    if not merged:
        # 후보를 하나도 못 찾은 경우 -> 사람 개입
        decision = interrupt({
            "reason": "no_vendor_candidates_found",
            "item_category": category,
            "question": "웹/RAG 검색에서 후보 업체를 찾지 못했습니다. 수동으로 업체를 입력하시겠습니까?",
        })
        merged = decision.get("manual_vendors", []) if isinstance(decision, dict) else []

    return Command(
        update={"candidate_vendors": merged},
        goto="cleanse_vendor_data_node",
    )


def tavily_search_vendors(item_name: str, category: str) -> List[dict]:
    """Tavily API로 '{item_name} 공급업체 / 제조사' 웹 검색"""
    
    client = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])
    results = client.search(f"{item_name} 공급업체 제조사 견적", max_results=5)
    return [{"name": r["title"], "source_url": r["url"], "raw_snippet": r["content"]} for r in results["results"]]
    

#############################
def rag_search_past_vendors(item_name: str, category: str) -> List[dict]:
    """과거 RFQ/발주 이력 벡터DB에서 유사 품목 취급 업체 검색"""
    # TODO: 벡터스토어 연동 (예: Qdrant, Chroma 등)
    # query_vector = embed(item_name)
    # hits = vector_store.search(query_vector, filter={"category": category}, top_k=5)
    # return [hit.metadata for hit in hits]
    return []
#############################

def _dedupe_vendors(vendors: List[dict]) -> List[dict]:
    seen, result = set(), []
    for v in vendors:
        key = v.get("name", "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            result.append(v)
    return result


# ============================================================
# 3) cleanse_vendor_data_node
#    빈칸(연락처·사업자번호) 채우기. 완료되면 -> create_rfq_node   
# ============================================================
def cleanse_vendor_data_node(state: ProcurementState) -> Command[Literal["create_rfq_node"]]:
    candidates = state["candidate_vendors"]
    cleansed = []
    incomplete = []

    for vendor in candidates:
        enriched = _enrich_vendor_contact_info(vendor)
        if _is_complete(enriched):
            cleansed.append(enriched)
        else:
            incomplete.append(enriched)

    if incomplete:
        decision = interrupt({
            "reason": "vendor_data_incomplete",
            "incomplete_vendors": incomplete,
            "complete_count": len(cleansed),
            "question": "일부 업체의 연락처/사업자번호를 채우지 못했습니다. 제외하고 진행할까요, 불완전한 채로 포함할까요?",
        })
        if decision == "include_incomplete":
            cleansed.extend(incomplete)
        # "exclude"(기본) -> incomplete는 그냥 버림

    return Command(
        update={
            "cleansed_vendors": cleansed,
            "existing_suppliers": state["existing_suppliers"] + cleansed,
        },
        goto="create_rfq_node",
    )


def _enrich_vendor_contact_info(vendor: dict) -> dict:
    """빈 연락처/사업자등록번호를 웹검색이나 공공 API로 보강"""
    if not vendor.get("contact"):
        vendor["contact"] = _search_contact(vendor.get("name", ""))
    if not vendor.get("business_reg_no"):
        vendor["business_reg_no"] = _search_business_reg_no(vendor.get("name", ""))
    return vendor







def _search_contact(vendor_name: str) -> Optional[str]:
    """Tavily로 업체 대표 연락처(전화번호) 검색"""
    tavily_client = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])

    # 한국 전화번호 패턴 (지역번호/휴대폰/대표번호)
    _PHONE_PATTERN = re.compile(r"(0\d{1,2}[-.\s]?\d{3,4}[-.\s]?\d{4})")

    try:
        results = tavily_client.search(
            query=f"{vendor_name} 대표번호 전화번호 연락처",
            max_results=5,
            search_depth="basic",
        )
    except Exception as e:
        print(f"[Tavily] contact search failed for {vendor_name}: {e}")
        return None

    for r in results.get("results", []):
        text = f"{r.get('title', '')} {r.get('content', '')}"
        match = _PHONE_PATTERN.search(text)
        if match:
            return match.group(1)

    return None



def _search_business_reg_no(vendor_name: str) -> Optional[str]:
    # TODO: 국세청 사업자등록정보 진위확인 API 등
    return None


def _is_complete(vendor: dict) -> bool:
    return bool(vendor.get("contact")) and bool(vendor.get("business_reg_no"))