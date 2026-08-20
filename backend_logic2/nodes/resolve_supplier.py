"""
nodes/resolve_suppliers.py — 4번 모듈: 비딩 확정된 품목의 공급사 확보

흐름:
  ① 이 품목에 기존 승인 공급사(Item.supplier_items)가 있는지 확인
     있으면 → 그 목록 그대로 반환 (RFQ 생성/발송은 여기서 구현 안 함)
     없으면 → ②로
  ② RAG(벤더DB)로 후보 탐색 → 이메일 확보된 것만 채택
     목표개수(기본 2개) 못 채우면 네이버 웹검색으로 후보 추가탐색

⚠️ RFQ 생성/발송은 이 모듈 범위 밖. 여기는 "누구한테 보낼지"까지만 정함.

폴더 구조: backend_logic2/erp_client.py, backend_logic2/nodes/이 파일

실행: python nodes/resolve_suppliers.py
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
import requests
from erp_client import erp_get_one

TARGET_SUPPLIER_COUNT = 2  # MVP 단계라 적게. 나중에 실사용 단계에서 늘리면 됨

_EXCLUDED_CONTACT_DOMAINS = [
    "jobkorea.co.kr", "saramin.co.kr", "wanted.co.kr", "catch.co.kr",
    "incruit.com", "albamon.com", "job.co.kr", "linkedin.com",
]


# ────────────────────────────────
# ① 기존 승인 공급사 확인
# ────────────────────────────────

def get_existing_suppliers(item_code):
    """Item.supplier_items에 등록된 기존 승인 공급사 목록 조회"""
    item = erp_get_one("Item", item_code)
    if not item:
        return []
    return [row["supplier"] for row in item.get("supplier_items", []) if row.get("supplier")]


# ────────────────────────────────
# ② RAG(벤더DB) 검색
# ────────────────────────────────

_embedding_model = None  # 최초 1회만 로드해서 재사용


def _get_query_embedding(text):
    global _embedding_model
    if _embedding_model is None:
        from sentence_transformers import SentenceTransformer
        print("[resolve_suppliers] 임베딩 모델 최초 로딩 중...")
        _embedding_model = SentenceTransformer("BAAI/bge-m3")  # ⚠️ 실제 모델명 확인 필요
    return _embedding_model.encode(text).tolist()


def _clean_search_term(item_name):
    """"#132" 같은 내부관리 코드는 검색엔진 입장에서 노이즈라 떼어냄 (item_name 원본은 안 건드림)"""
    cleaned = re.sub(r"#\s*\d+", "", item_name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or item_name


def rag_search_vendors(item_name, limit_categories=5, limit_vendors=10):
    """Postgres(pgvector) 벤더풀에서 2단계 검색: 품목명 → 분류 → 그 분류의 실제 업체들"""
    import psycopg

    PG_HOST = os.environ["PG_HOST"]
    PG_PORT = os.environ["PG_PORT"]
    PG_USER = os.environ["PG_USER"]
    PG_PASSWORD = os.environ["PG_PASSWORD"]
    PG_DBNAME = os.environ["PG_DBNAME"]

    query_embedding = _get_query_embedding(_clean_search_term(item_name))

    conn = psycopg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname=PG_DBNAME)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT c.id FROM procurement_item_category_bge b
                JOIN procurement_item_category c ON c.id = b.category_id
                ORDER BY b.embedding <=> %s::vector
                LIMIT %s
            """, (query_embedding, limit_categories))
            category_ids = [r[0] for r in cur.fetchall()]
            if not category_ids:
                return []

            cur.execute("""
                SELECT v.id, v.business_no, v.company_name, v.address, v.email, v.phone, v.description
                FROM vendor_item_category vic
                JOIN vendor_catalog v ON v.id = vic.vendor_id
                WHERE vic.category_id = ANY(%s)
                LIMIT %s
            """, (category_ids, limit_vendors))
            rows = cur.fetchall()
    finally:
        conn.close()

    return [
        {"name": r[2], "business_no": r[1], "address": r[3], "email": r[4], "phone": r[5], "description": r[6]}
        for r in rows
    ]


# ────────────────────────────────
# 연락처 보강 (공식사이트 찾기 → 페이지 읽기 → AI 추출)
# ────────────────────────────────

def _search_naver_web(query, display=5):
    """네이버 웹문서검색 공통 헬퍼"""
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


def _find_official_site(company_name):
    """공식 홈페이지로 추정되는 검색결과 1건 (채용사이트 등은 걸러냄)"""
    for item in _search_naver_web(f"{company_name} 공식 홈페이지"):
        link = item.get("link", "")
        if any(domain in link for domain in _EXCLUDED_CONTACT_DOMAINS):
            continue
        return {"url": link, "content": item.get("description", "")}
    return None


def _fetch_page_text(url, max_chars=6000):
    """페이지 직접 요청해서 태그 벗겨낸 텍스트만 반환"""
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


def _extract_contact_from_page(company_name, source_url, page_text):
    """페이지 내용에서 회사명(clean_name)·이메일·전화번호 AI로 추출"""
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
        "- 이 페이지가 진짜 이 회사의 것이 맞는지 스스로 확인하고, 아니면 셋 다 null.\n"
        "- clean_name은 검색결과 제목을 그대로 베끼지 말고 실제 상호명만 간결하게.\n"
        "- 잡코리아·사람인 등 제3자 서비스 연락처는 절대 채택하지 마세요.\n"
        "- 전화번호와 팩스번호를 구분하세요. 확신 없으면 null.\n"
        '- JSON만 답하세요: {{"clean_name": "값 또는 null", "email": "값 또는 null", "phone": "값 또는 null"}}'
    )
    result_text = (prompt | llm).invoke({"company_name": company_name, "url": source_url, "text": page_text[:6000]}).content

    try:
        cleaned = result_text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        parsed = json.loads(cleaned)
        email = parsed.get("email") if parsed.get("email") not in (None, "null", "") else None
        if email and any(f"@{d}" in email for d in _EXCLUDED_CONTACT_DOMAINS):
            email = None  # AI가 놓쳐도 코드로 한 번 더 검증
        return {
            "clean_name": parsed.get("clean_name") if parsed.get("clean_name") not in (None, "null", "") else None,
            "email": email,
            "phone": parsed.get("phone") if parsed.get("phone") not in (None, "null", "") else None,
        }
    except (Exception,):
        return {"clean_name": None, "email": None, "phone": None}


def enrich_contact_info(vendor):
    """후보 업체의 이메일·전화·정식회사명 확보"""
    if vendor.get("email") and vendor.get("phone"):
        return vendor

    name = vendor.get("name", "")
    site = _find_official_site(name)
    if not site:
        return vendor

    page_text = _fetch_page_text(site["url"]) or site.get("content", "")
    extracted = _extract_contact_from_page(name, site["url"], page_text)

    if extracted.get("clean_name"):
        vendor["name"] = extracted["clean_name"]
    if not vendor.get("email") and extracted.get("email"):
        vendor["email"] = extracted["email"]
    if not vendor.get("phone") and extracted.get("phone"):
        vendor["phone"] = extracted["phone"]

    return vendor


def search_new_vendor_candidates(item_name, max_results=15):
    """새 벤더 후보 자체를 웹에서 찾음 (RAG로 부족할 때 보충용)"""
    items = _search_naver_web(f"{_clean_search_term(item_name)} 공급업체 제조사 견적", display=max_results)
    return [{"name": r.get("title", ""), "email": None, "phone": None} for r in items]


# ────────────────────────────────
# 최종 조합: 공급사 확보
# ────────────────────────────────

def resolve_suppliers_for_item(item_code, item_name, target_count=TARGET_SUPPLIER_COUNT):
    """
    품목 하나에 대해 공급사 확보.
    ① 기존 승인공급사 있으면 그대로 반환
    ② 없으면 RAG로 찾고, 이메일 없으면 버리고, 부족하면 네이버 웹검색으로 보충
    """
    existing = get_existing_suppliers(item_code)
    if existing:
        return {"source": "existing", "suppliers": existing}

    qualified = []
    seen_names, seen_emails = set(), set()

    def add_if_qualified(vendor):
        key = vendor.get("name", "").strip().lower()
        if not key or key in seen_names:
            return
        vendor = enrich_contact_info(vendor)
        email = vendor.get("email")
        if not email:
            print(f"  '{vendor.get('name')}' 이메일 없음 → 버림")
            return
        email_key = email.strip().lower()
        if email_key in seen_emails:
            print(f"  '{vendor.get('name')}' 이메일 중복 → 버림")
            return
        qualified.append(vendor)
        seen_names.add(key)
        seen_emails.add(email_key)

    rag_candidates = rag_search_vendors(item_name, limit_vendors=target_count * 3)
    print(f"[resolve_suppliers] RAG 후보 {len(rag_candidates)}건 → 확인 중...")
    for c in rag_candidates:
        if len(qualified) >= target_count:
            break
        add_if_qualified(c)

    if len(qualified) < target_count:
        print(f"[resolve_suppliers] {target_count - len(qualified)}건 부족 → 웹검색 보충")
        for c in search_new_vendor_candidates(item_name):
            if len(qualified) >= target_count:
                break
            add_if_qualified(c)

    return {"source": "new_search", "suppliers": qualified}


def resolve_suppliers_for_mr(mr_name, target_count=TARGET_SUPPLIER_COUNT):
    """MR 안의 각 품목마다 resolve_suppliers_for_item() 실행"""
    mr = erp_get_one("Material Request", mr_name)
    if not mr:
        return {}

    results = {}
    for line in mr.get("items", []):
        item_code = line["item_code"]
        item_name = line.get("item_name") or item_code
        results[item_code] = resolve_suppliers_for_item(item_code, item_name, target_count)

    return results


if __name__ == "__main__":
    mr_name = input("Material Request ID 입력: ").strip()

    results = resolve_suppliers_for_mr(mr_name)

    if not results:
        print("해당 MR을 찾을 수 없거나 품목이 없습니다.")

    for item_code, result in results.items():
        print(f"\n{'='*50}")
        print(f"[{item_code}] 출처: {result['source']}")
        print("=" * 50)
        for s in result["suppliers"]:
            print(f"  - {s}")