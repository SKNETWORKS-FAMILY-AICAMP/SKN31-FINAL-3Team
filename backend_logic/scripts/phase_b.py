import os
import re
import requests
from typing import TypedDict, List, Literal, Optional
from dotenv import load_dotenv
from tavily import TavilyClient
from langgraph.types import Command, interrupt
import psycopg2
from pgvector.psycopg2 import register_vector
from erp_client import erp_get_one, ERPNextAPIError
from openai import OpenAI
import time

load_dotenv()

_tavily_client = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])

NTS_API_KEY = os.environ["NTS_API_KEY"]
NTS_STATUS_URL = "https://api.odcloud.kr/api/nts-businessman/v1/status"

_PHONE_PATTERN = re.compile(r"(0\d{1,2}[-.\s]?\d{3,4}[-.\s]?\d{4})")
_BIZ_REG_PATTERN = re.compile(r"\b(\d{3}-\d{2}-\d{5})\b")


class ProcurementState(TypedDict):
    mr_name: str              # Material Request 문서번호
    item_code: str
    item_name: str
    warehouse: str
    qty: float
    existing_suppliers: List[dict]   # Item.supplier_items 원본 (또는 신규 발굴분 포함)
    candidate_vendors: List[dict]    # 신규 발굴 후보 (정제 전)
    cleansed_vendors: List[dict]     # 정제 완료 (RFQ 발송 가능 상태)
    supplier_found: bool


# ============================================================
# 1) check_existing_suppliers_node
# ============================================================
def check_existing_suppliers_node(state: ProcurementState) -> Command[
    Literal["create_rfq_node", "search_new_vendors_node"]
]:
    """
    Item 문서 안의 supplier_items 자식테이블을 '기존 승인 공급사'로 사용.
    있으면 -> create_rfq_node / 없으면 -> search_new_vendors_node
    """
    item_code = state["item_code"]

    try:
        item = erp_get_one("Item", item_code)
    except ERPNextAPIError as e:
        decision = interrupt({
            "reason": "erpnext_api_error",
            "error": str(e),
            "item_code": item_code,
            "question": "ERPNext Item 조회 실패. 재시도할까요, 신규 벤더 탐색으로 바로 넘어갈까요?",
        })
        item = erp_get_one("Item", item_code) if decision == "retry" else None
        if item is None and decision != "retry":
            return Command(update={"supplier_found": False}, goto="search_new_vendors_node")

    if not item:
        return Command(update={"supplier_found": False}, goto="search_new_vendors_node")

    suppliers = item.get("supplier_items", [])
    item_name = item.get("item_name", state.get("item_name", item_code))
    found = len(suppliers) > 0

    print(f"[check_existing_suppliers_node] '{item_name}({item_code})' 기존 공급사 {len(suppliers)}건 → "
          f"{'있음' if found else '없음'}")

    return Command(
        update={
            "item_name": item_name,
            "existing_suppliers": suppliers,
            "supplier_found": found,
        },
        goto="create_rfq_node" if found else "search_new_vendors_node",
    )


# ============================================================
# 2) search_new_vendors_node
# ============================================================

DATABASE_URL = os.environ["DATABASE_URL"]
VENDOR_TABLE = "vendor_master"
RAG_TOP_K = 5

def tavily_search_vendors(item_name: str) -> List[dict]:
    """Tavily API로 '{item_name} 공급업체 / 제조사' 웹 검색"""
    results = _tavily_client.search(f"{item_name} 공급업체 제조사 견적", max_results=5)
    return [
        {"name": r["title"], "source_url": r["url"], "raw_snippet": r["content"]}
        for r in results["results"]
    ]

def _get_pgvector_connection():
    conn = psycopg2.connect(DATABASE_URL)
    register_vector(conn)
    return conn

def embed_batch(texts: List[str], max_retries: int = 3) -> List[List[float]]:
    EMBEDDING_MODEL = "text-embedding-3-small"
    openai_client = OpenAI()

    for attempt in range(1, max_retries + 1):
        try:
            resp = openai_client.embeddings.create(model=EMBEDDING_MODEL, input=texts)
            return [item.embedding for item in resp.data]
        except Exception as e:
            wait = 2 ** attempt
            print(f"[embed_batch] 실패 (시도 {attempt}/{max_retries}): {e} → {wait}초 후 재시도")
            time.sleep(wait)
    raise RuntimeError("임베딩 API 재시도 초과")


def rag_search_past_vendors(item_name: str) -> List[dict]:
    """
    pgvector(vendor_master)에서 item_name과 의미상 유사한 업체를 검색.
    나라장터 등록 업체 마스터 DB 기반이라, 찾으면 사업자등록번호까지
    바로 채워서 반환함 (뒤 단계에서 국세청 재검증 없이 활용 가능).
    """
    try:
        query_embedding = embed_batch([item_name])[0]
    except Exception as e:
        print(f"[rag_search_past_vendors] 쿼리 임베딩 실패: {e}")
        return []

    try:
        conn = _get_pgvector_connection()
    except Exception as e:
        print(f"[rag_search_past_vendors] DB 연결 실패: {e}")
        return []

    try:
        with conn.cursor() as cur:
            cur.execute(
           f"""
           SELECT
               업체명,
               사업자등록번호,
               업체소재시군구,
               업체국가,
               embedding <=> %s::vector AS distance
           FROM {VENDOR_TABLE}
           ORDER BY distance ASC
           LIMIT %s;
           """,
           (query_embedding, RAG_TOP_K),
        )
            rows = cur.fetchall()
    except Exception as e:
        print(f"[rag_search_past_vendors] 쿼리 실패: {e}")
        return []
    finally:
        conn.close()

    candidates = [
        {
            "name": name,
            "business_reg_no": biz_reg_no or None,
            "location": location,
            "country": country,
            "source": "internal_vendor_db",
            "similarity_distance": float(distance),
        }
        for name, biz_reg_no, location, country, distance in rows
    ]

    print(f"[rag_search_past_vendors] '{item_name}' 유사업체 {len(candidates)}건 발견 (top-{RAG_TOP_K})")
    return candidates

def _dedupe_vendors(vendors: List[dict]) -> List[dict]:
    seen, result = set(), []
    for v in vendors:
        key = v.get("name", "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            result.append(v)
    return result

def search_new_vendors_node(state: ProcurementState) -> Command[Literal["cleanse_vendor_data_node"]]:
    """Tavily 웹검색 + 내부 RAG(과거 벤더 이력)로 신규 후보 업체 탐색."""
    item_name = state["item_name"]

    web_candidates = tavily_search_vendors(item_name)
    rag_candidates = rag_search_past_vendors(item_name)

    merged = _dedupe_vendors(web_candidates + rag_candidates)
    print(f"[search_new_vendors_node] '{item_name}' 후보업체 {len(merged)}건 발견")

    if not merged:
        decision = interrupt({
            "reason": "no_vendor_candidates_found",
            "item_name": item_name,
            "question": "웹/RAG 검색에서 후보 업체를 찾지 못했습니다. 수동으로 업체를 입력하시겠습니까?",
        })
        merged = decision.get("manual_vendors", []) if isinstance(decision, dict) else []

    return Command(
        update={"candidate_vendors": merged},
        goto="cleanse_vendor_data_node",
    )

# ============================================================
# 3) cleanse_vendor_data_node
# ============================================================

_EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

def cleanse_vendor_data_node(state: ProcurementState) -> Command[Literal["create_rfq_node"]]:
    """찾은 후보업체의 빈칸(연락처·사업자번호)을 채움. 항상 -> create_rfq_node"""
    candidates = state["candidate_vendors"]
    cleansed, incomplete = [], []

    for vendor in candidates:
        enriched = _enrich_vendor_contact_info(vendor)
        (cleansed if _is_complete(enriched) else incomplete).append(enriched)

    print(f"[cleanse_vendor_data_node] 완료 {len(cleansed)}건 / 불완전 {len(incomplete)}건")

    if incomplete:
        decision = interrupt({
            "reason": "vendor_data_incomplete",
            "incomplete_vendors": incomplete,
            "complete_count": len(cleansed),
            "question": "일부 업체의 연락처/사업자번호를 채우지 못했습니다. 제외하고 진행할까요, 불완전한 채로 포함할까요?",
        })
        if decision == "include_incomplete":
            cleansed.extend(incomplete)

    return Command(
        update={
            "cleansed_vendors": cleansed,
            "existing_suppliers": state["existing_suppliers"] + cleansed,
        },
        goto="create_rfq_node",
    )


def _enrich_vendor_contact_info(vendor: dict) -> dict:
    if not vendor.get("contact"):
        vendor["contact"] = _search_contact(vendor.get("name", ""))
    if not vendor.get("email"):
        vendor["email"] = _search_email(vendor.get("name", ""))
    if not vendor.get("business_reg_no"):
        vendor["business_reg_no"] = _search_business_reg_no(vendor.get("name", ""))
    return vendor


def _search_email(vendor_name: str) -> Optional[str]:
    """Tavily로 업체 대표 이메일 검색"""
    try:
        results = _tavily_client.search(
            query=f"{vendor_name} 이메일 contact email",
            max_results=5,
            search_depth="basic",
        )
    except Exception as e:
        print(f"[_search_email] Tavily 검색 실패 ({vendor_name}): {e}")
        return None

    for r in results.get("results", []):
        text = f"{r.get('title', '')} {r.get('content', '')}"
        match = _EMAIL_PATTERN.search(text)
        if match:
            return match.group(0)
    return None

def _search_contact(vendor_name: str) -> Optional[str]:
    """Tavily로 업체 대표 연락처(전화번호) 검색"""
    try:
        results = _tavily_client.search(
            query=f"{vendor_name} 대표번호 전화번호 연락처",
            max_results=5,
            search_depth="basic",
        )
    except Exception as e:
        print(f"[_search_contact] Tavily 검색 실패 ({vendor_name}): {e}")
        return None

    for r in results.get("results", []):
        text = f"{r.get('title', '')} {r.get('content', '')}"
        match = _PHONE_PATTERN.search(text)
        if match:
            return match.group(1)
    return None


def _search_business_reg_no(vendor_name: str) -> Optional[str]:
    """
    1) Tavily 스니펫에서 사업자등록번호 후보를 정규식으로 추출
    2) 국세청 상태조회 API로 유효(계속사업자)한지 검증
    """
    candidate = _extract_biz_reg_no_via_tavily(vendor_name)
    if not candidate:
        return None
    return candidate if _verify_biz_reg_no(candidate) else None


def _extract_biz_reg_no_via_tavily(vendor_name: str) -> Optional[str]:
    try:
        results = _tavily_client.search(
            query=f"{vendor_name} 사업자등록번호",
            max_results=5,
            search_depth="basic",
        )
    except Exception as e:
        print(f"[_extract_biz_reg_no_via_tavily] Tavily 검색 실패 ({vendor_name}): {e}")
        return None

    for r in results.get("results", []):
        text = f"{r.get('title', '')} {r.get('content', '')}"
        match = _BIZ_REG_PATTERN.search(text)
        if match:
            return match.group(1).replace("-", "")
    return None


def _verify_biz_reg_no(b_no: str) -> bool:
    try:
        resp = requests.post(
            NTS_STATUS_URL,
            params={"serviceKey": NTS_API_KEY, "returnType": "JSON"},
            json={"b_no": [b_no]},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"[_verify_biz_reg_no] 국세청 조회 실패 ({b_no}): {e}")
        return False

    results = data.get("data", [])
    if not results:
        return False
    return results[0].get("b_stt_cd", "") == "01"  # 01 = 계속사업자(정상)


def _is_complete(vendor: dict) -> bool:
    return (
        bool(vendor.get("contact"))
        and bool(vendor.get("email"))
        and bool(vendor.get("business_reg_no"))
    )