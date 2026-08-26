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

import html
import re
import requests
from erp_client import erp_get_one

TARGET_SUPPLIER_COUNT = 5  # MVP 단계라 적게. 나중에 실사용 단계에서 늘리면 됨

_EXCLUDED_CONTACT_DOMAINS = [
    "jobkorea.co.kr", "saramin.co.kr", "wanted.co.kr", "catch.co.kr",
    "incruit.com", "albamon.com", "job.co.kr", "linkedin.com",
]

_DOCUMENT_RESULT_PATTERN = re.compile(
    r"(?:\.(?:pdf|hwp|hwpx|xls|xlsx|csv|doc|docx|ppt|pptx|zip)(?:$|[\s?#])"
    r"|filedown|file_download|download\.do|downloaddirect|/download(?:/|\?|$)|attachment)",
    re.IGNORECASE,
)


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
    """Postgres(pgvector) 벤더풀에서 품목명 → 분류 → 업체 순으로 검색.

    평가와 재현을 위해 각 후보에 검색 출처, 순위, 매칭 카테고리와 cosine
    distance/similarity를 함께 반환한다. 같은 업체가 여러 카테고리에 연결된
    경우 가장 가까운 카테고리 한 건만 남긴다.
    """
    import psycopg

    PG_HOST = os.environ["PG_HOST"]
    PG_PORT = os.environ["PG_PORT"]
    PG_USER = os.environ["PG_USER"]
    PG_PASSWORD = os.environ["PG_PASSWORD"]
    PG_DBNAME = os.environ["PG_DBNAME"]

    search_query = _clean_search_term(item_name)
    query_embedding = _get_query_embedding(search_query)

    conn = psycopg.connect(host=PG_HOST, port=PG_PORT, user=PG_USER, password=PG_PASSWORD, dbname=PG_DBNAME)
    try:
        with conn.cursor() as cur:
            cur.execute("""
                WITH nearest_categories AS (
                    SELECT
                        category_id,
                        category_distance,
                        ROW_NUMBER() OVER (
                            ORDER BY category_distance ASC, category_id
                        ) AS category_rank
                    FROM (
                        SELECT
                            c.id AS category_id,
                            b.embedding <=> %s::vector AS category_distance
                        FROM procurement_item_category_bge b
                        JOIN procurement_item_category c ON c.id = b.category_id
                        ORDER BY category_distance ASC, c.id
                        LIMIT %s
                    ) category_candidates
                ),
                best_vendor_match AS (
                    SELECT DISTINCT ON (v.id)
                        v.id,
                        v.business_no,
                        v.company_name,
                        v.address,
                        v.email,
                        v.phone,
                        v.description,
                        nc.category_id,
                        nc.category_rank,
                        nc.category_distance
                    FROM nearest_categories nc
                    JOIN vendor_item_category vic ON vic.category_id = nc.category_id
                    JOIN vendor_catalog v ON v.id = vic.vendor_id
                    ORDER BY v.id, nc.category_rank ASC, nc.category_distance ASC
                )
                SELECT
                    id, business_no, company_name, address, email, phone,
                    description, category_id, category_rank, category_distance
                FROM best_vendor_match
                ORDER BY category_rank ASC, category_distance ASC, company_name ASC, id ASC
                LIMIT %s
            """, (query_embedding, limit_categories, limit_vendors))
            rows = cur.fetchall()
    finally:
        conn.close()

    results = []
    for retrieval_rank, row in enumerate(rows, start=1):
        category_distance = float(row[9])
        results.append({
            "vendor_id": str(row[0]),
            "name": row[2],
            "business_no": row[1],
            "address": row[3],
            "email": row[4],
            "phone": row[5],
            "description": row[6],
            "source": "rag",
            "query": search_query,
            "retrieval_rank": retrieval_rank,
            "category_id": str(row[7]),
            "category_rank": int(row[8]),
            "category_distance": category_distance,
            "category_similarity": 1.0 - category_distance,
        })
    return results


# ────────────────────────────────
# 연락처 보강 (공식사이트 찾기 → 페이지 읽기 → AI 추출)
# ────────────────────────────────

def _search_naver_web(query, display=5, raise_on_error=False):
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
        if raise_on_error:
            raise
        return []

    for retrieval_rank, item in enumerate(items, start=1):
        item["title"] = html.unescape(re.sub(r"<.*?>", "", item.get("title", ""))).strip()
        item["description"] = html.unescape(
            re.sub(r"<.*?>", "", item.get("description", ""))
        ).strip()
        item["retrieval_rank"] = retrieval_rank
        item["query"] = query
    return items


def _is_document_search_result(item):
    """웹문서 검색의 첨부파일·다운로드 결과인지 판별."""
    target = " ".join(
        str(item.get(field) or "") for field in ("title", "link", "description")
    )
    return bool(_DOCUMENT_RESULT_PATTERN.search(target))


def _search_naver_local(query, display=5, raise_on_error=False):
    """업체·기관 엔터티를 반환하는 Naver Local 검색."""
    try:
        res = requests.get(
            "https://naverapihub.apigw.ntruss.com/search/v1/local",
            headers={
                "X-NCP-APIGW-API-KEY-ID": os.environ["NAVER_CLIENT_ID"],
                "X-NCP-APIGW-API-KEY": os.environ["NAVER_CLIENT_SECRET"],
            },
            params={"query": query, "display": min(max(display, 1), 5)},
            timeout=10,
        )
        res.raise_for_status()
        items = res.json().get("items", [])
    except Exception as error:
        print(f"[_search_naver_local] 검색 실패 ('{query}'): {error}")
        if raise_on_error:
            raise
        return []

    for item in items:
        item["title"] = html.unescape(
            re.sub(r"<.*?>", "", item.get("title", ""))
        ).strip()
        item["query"] = query
    return items


def _vendor_search_queries(item_name, item_group=None):
    """세부 규격명과 업종명을 함께 사용해 Local 검색 recall을 확보."""
    cleaned = _clean_search_term(item_name)
    base = re.sub(r"\s*\([^)]*\)", "", cleaned).strip()

    normalized_group = str(item_group or "").strip()
    if normalized_group == "사무용품":
        queries = [
            f"{base} 판매",
            f"{base} 납품",
            "사무용품 도매",
            "오피스용품 납품",
        ]
        return list(dict.fromkeys(query for query in queries if query.strip()))

    if normalized_group.startswith("시약"):
        queries = [
            f"{base} 판매",
            f"{base} 시약",
            "실험실 시약 납품",
            "연구용 시약 유통",
        ]
        return list(dict.fromkeys(query for query in queries if query.strip()))

    family_rules = (
        (("소화기", "소방", "방염"), "소방용품"),
        (("장갑",), "작업용 장갑"),
        (("안전모",), "안전모"),
        (("보안경", "안면", "고글"), "보안경 안전보호구"),
        (("마스크", "호흡", "방진", "방독"), "산업용 마스크"),
        (("안전화",), "안전화"),
        (("안전대", "하네스", "랜야드"), "안전대"),
        (("귀마개", "귀덮개"), "청력보호구"),
    )
    family = "산업안전용품"
    industry = "산업안전용품"
    for keywords, label in family_rules:
        if any(keyword in cleaned for keyword in keywords):
            family = label
            if label == "소방용품":
                industry = "소방용품"
            break

    queries = [f"{base} 판매", f"{family} 판매", industry, "안전보호구"]
    return list(dict.fromkeys(query for query in queries if query.strip()))


def _is_supplier_local_result(item):
    """Local 결과 중 협회·교육·공공기관처럼 RFQ 대상이 아닌 엔터티 제외."""
    category = str(item.get("category") or "")
    excluded_categories = ("협회", "단체", "학교", "교육", "공공기관", "정부기관")
    return not any(keyword in category for keyword in excluded_categories)


def _find_official_site(company_name):
    """공식 홈페이지로 추정되는 검색결과 1건 (채용사이트 등은 걸러냄)"""
    for item in _search_naver_web(f"{company_name} 공식 홈페이지"):
        link = item.get("link", "")
        if any(domain in link for domain in _EXCLUDED_CONTACT_DOMAINS):
            continue
        if _is_document_search_result(item):
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
    vendor["official_site_url"] = site["url"]

    if extracted.get("clean_name"):
        vendor["name"] = extracted["clean_name"]
    if not vendor.get("email") and extracted.get("email"):
        vendor["email"] = extracted["email"]
    if not vendor.get("phone") and extracted.get("phone"):
        vendor["phone"] = extracted["phone"]

    return vendor


def search_new_vendor_candidates(
    item_name,
    max_results=15,
    raise_on_error=False,
    item_group=None,
):
    """Naver Local에서 실제 업체·기관 엔터티만 새 벤더 후보로 수집."""
    candidates = []
    seen = set()
    queries = _vendor_search_queries(item_name, item_group=item_group)
    errors = []

    for query in queries:
        try:
            items = _search_naver_local(query, display=5, raise_on_error=True)
        except Exception as error:
            errors.append(error)
            continue
        for item in items:
            if not _is_supplier_local_result(item):
                continue
            name = item.get("title", "").strip()
            address = (item.get("roadAddress") or item.get("address") or "").strip()
            identity = (re.sub(r"\W", "", name).casefold(), address.casefold())
            if not name or identity in seen:
                continue
            seen.add(identity)
            category = item.get("category", "").strip()
            candidates.append({
                "vendor_id": None,
                "name": name,
                "business_no": None,
                "address": address or None,
                "email": None,
                "phone": item.get("telephone") or None,
                "description": " | ".join(value for value in (category, address) if value),
                "source": "naver",
                "source_channel": "naver_local",
                "candidate_type": "vendor",
                "query": query,
                "query_variants": queries,
                "retrieval_rank": len(candidates) + 1,
                "source_url": item.get("link", ""),
                "official_site_url": item.get("link", ""),
                "raw_title": item.get("title", ""),
                "naver_category": category,
                "mapx": item.get("mapx"),
                "mapy": item.get("mapy"),
            })
            if len(candidates) >= max_results:
                return candidates

    if not candidates and errors and raise_on_error:
        raise errors[0]
    return candidates


def collect_vendor_candidates_for_evaluation(
    item=None,
    limit_per_source=20,
    source_limits=None,
    category_limit=5,
    **legacy_options,
):
    """평가기가 의존하는 안정적인 원시 후보 수집 인터페이스.

    내부 검색 API가 바뀌더라도 반환 계약(``sources``와 ``errors``)만 유지하면
    evaluation 코드는 수정하지 않아도 된다. ``sources``의 키는 동적이므로
    Naver를 다른 검색기로 교체할 때 새 source 이름을 그대로 사용할 수 있다.
    """
    # 이전 호출 형식(item_name, rag_limit=..., ...)도 당분간 호환한다.
    if isinstance(item, str):
        item = {"item_name": item}
    item = dict(item or {})
    item_name = item.get("item_name") or legacy_options.get("item_name") or ""
    item_code = item.get("item_code")
    item_group = item.get("item_group")
    if source_limits is not None and not isinstance(source_limits, dict):
        # 구형 위치 인자 호출: (item_name, rag_limit, naver_limit, limit_categories)
        source_limits = {"rag": limit_per_source, "naver": source_limits}
    source_limits = dict(source_limits or {})
    rag_limit = int(source_limits.get("rag", legacy_options.get("rag_limit", limit_per_source)))
    naver_limit = int(source_limits.get("naver", legacy_options.get("naver_limit", limit_per_source)))
    category_limit = int(legacy_options.get("limit_categories", category_limit))

    sources = {}
    errors = {}

    if item_code:
        try:
            sources["existing"] = [
                {
                    "name": name,
                    "source": "existing",
                    "retrieval_rank": rank,
                }
                for rank, name in enumerate(get_existing_suppliers(item_code), start=1)
            ]
        except Exception as error:
            errors["existing"] = str(error)

    try:
        sources["rag"] = rag_search_vendors(
            item_name,
            limit_categories=category_limit,
            limit_vendors=rag_limit,
        )
    except Exception as error:
        errors["rag"] = str(error)
        sources["rag"] = []

    try:
        sources["naver"] = search_new_vendor_candidates(
            item_name,
            max_results=naver_limit,
            raise_on_error=True,
            item_group=item_group,
        )
    except Exception as error:
        errors["naver"] = str(error)
        sources["naver"] = []

    return {
        "contract_version": 1,
        "sources": sources,
        "errors": errors,
        "metadata": {"clean_query": _clean_search_term(item_name)},
    }


# ────────────────────────────────
# 최종 조합: 공급사 확보
# ────────────────────────────────

# ────────────────────────────────
# 적응형 탐색 (Agent) — RAG/웹검색을 도구로 두고 AI가 스스로 판단
# ────────────────────────────────

MAX_AGENT_ITERATIONS = 3  # 무한루프 방지, 최대 시도 횟수


def _agent_decide_next_action(item_name, current_candidates, target_count, attempted_queries, iteration):
    """
    AI가 "이번엔 뭘 검색할지, RAG를 쓸지 웹검색을 쓸지" 판단.
    이미 시도한 검색어는 피하고, 동의어/유사 카테고리 등으로 재시도하게 유도.
    """
    import json
    from langchain_openai import ChatOpenAI
    from langchain_core.prompts import PromptTemplate

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)

    candidate_names = [c.get("name") for c in current_candidates]

    prompt = PromptTemplate.from_template(
        "당신은 공급사(벤더) 탐색을 돕는 에이전트입니다. 목표는 '{item_name}'을 "
        "공급 가능한 업체를 {target_count}곳 찾는 것입니다.\n\n"
        "지금까지 찾은 업체: {candidates}\n"
        "지금까지 시도한 검색어: {attempted}\n"
        "몇 번째 시도인지: {iteration}\n\n"
        "다음에 뭘 할지 결정하세요:\n"
        "- tool: 'rag'(내부 벤더DB, 이미 검증된 업체 위주, 빠름) 또는 "
        "'web_search'(외부 웹검색, 더 넓지만 검증 안 됨) 중 선택\n"
        "- query: 검색에 쓸 검색어. 이미 시도한 검색어와 겹치면 다른 표현(정식명칭, "
        "업계 용어, 유사 카테고리 등)으로 바꿔서 제시하세요\n"
        "- reason: 왜 이 도구/검색어를 골랐는지 한 문장\n\n"
        '반드시 이 JSON 형식으로만 답하세요: '
        '{{"tool": "rag 또는 web_search", "query": "검색어", "reason": "이유"}}'
    )

    result = (prompt | llm).invoke({
        "item_name": item_name,
        "target_count": target_count,
        "candidates": json.dumps(candidate_names, ensure_ascii=False),
        "attempted": json.dumps(attempted_queries, ensure_ascii=False),
        "iteration": iteration,
    }).content

    try:
        cleaned = result.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        decision = json.loads(cleaned)
        if decision.get("tool") not in ("rag", "web_search"):
            raise ValueError("잘못된 tool 값")
        return decision
    except Exception:
        # 파싱 실패시 안전한 기본값 (원래 고정순서 로직과 비슷하게)
        fallback_tool = "rag" if iteration == 0 else "web_search"
        return {"tool": fallback_tool, "query": item_name, "reason": "AI 응답 파싱 실패, 기본값 사용"}


def _ai_confirm_relevant_vendor(item_name, vendor_name, vendor_description=None):
    """
    이 업체가 요청 품목을 실제로 공급할 만한 업체인지 AI로 확인.
    RAG/웹검색 결과에 전혀 관련없는 업체(예: "안전모" 검색했는데 화장품회사)가
    섞여 들어오는 걸 막는 관련성 검증. 연락처 보강(웹검색+AI추출, 비용 드는
    작업) 하기 *전에* 먼저 걸러내서 낭비를 줄임.

    반환: (관련있음 여부: bool, 이유: str)
    """
    import json
    from langchain_openai import ChatOpenAI
    from langchain_core.prompts import PromptTemplate

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    prompt = PromptTemplate.from_template(
        "다음 업체가 '{item_name}'을(를) 공급·판매·제조할 만한 업체인지 판단하세요.\n\n"
        "업체명: {vendor_name}\n"
        "업체 설명: {vendor_description}\n\n"
        "규칙:\n"
        "- 업체명이나 설명에서 해당 품목과 관련된 업종(제조·유통·판매)이 드러나면 '관련있음'\n"
        "- 완전히 다른 업종(예: 화장품회사가 안전모 검색결과에 나온 경우)이면 '관련없음'\n"
        "- 애매하면 '관련있음'으로 판단하세요 (최종 선택은 사람이 하므로, 너무 "
        "빡빡하게 걸러내면 후보 자체가 안 남는 게 더 문제)\n\n"
        '반드시 이 JSON 형식으로만 답하세요: '
        '{{"relevant": true 또는 false, "reason": "짧은 이유"}}'
    )
    result = (prompt | llm).invoke({
        "item_name": item_name,
        "vendor_name": vendor_name,
        "vendor_description": vendor_description or "(설명 없음)",
    }).content

    try:
        cleaned = result.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        parsed = json.loads(cleaned)
        return bool(parsed.get("relevant", True)), parsed.get("reason", "")
    except Exception:
        return True, "AI 응답 파싱 실패 (일단 포함, 사람이 최종 확인)"


def resolve_suppliers_agent(item_code, item_name, target_count=TARGET_SUPPLIER_COUNT):
    """
    적응형 공급사 탐색 (Agent 버전).
    RAG/웹검색을 "도구"로 두고, AI가 매 라운드마다 어떤 도구를 쓸지,
    검색어를 뭘로 바꿀지 스스로 판단하며 반복함.

    ⚠️ 기존 승인공급사 확인은 이 함수 밖(resolve_suppliers_for_item)에서
    먼저 처리하는 게 맞음 — 이건 순수 "신규탐색"만 담당.
    ⚠️ 최대 MAX_AGENT_ITERATIONS번까지만 시도 (무한루프/과금 방지 안전장치).

    반환: {"source": "agent_search", "suppliers": [...], "tool_log": [...]}
    tool_log에는 매 라운드 AI가 뭘 선택했는지 기록 (설명가능성용)
    """
    qualified = []
    seen_names, seen_emails = set(), set()
    attempted_queries = []
    tool_log = []

    def add_if_qualified(vendor):
        key = vendor.get("name", "").strip().lower()
        if not key or key in seen_names:
            return

        # 관련성 먼저 확인 (연락처 보강은 비용이 드는 작업이라, 그 전에 걸러냄)
        is_relevant, reason = _ai_confirm_relevant_vendor(
            item_name, vendor.get("name"), vendor.get("description")
        )
        if not is_relevant:
            print(f"  '{vendor.get('name')}' 관련없는 업체로 판단, 제외: {reason}")
            return

        vendor = enrich_contact_info(vendor)
        email = vendor.get("email")
        if not email:
            return
        email_key = email.strip().lower()
        if email_key in seen_emails:
            return
        qualified.append(vendor)
        seen_names.add(key)
        seen_emails.add(email_key)

    for iteration in range(MAX_AGENT_ITERATIONS):
        if len(qualified) >= target_count:
            break

        decision = _agent_decide_next_action(
            item_name, qualified, target_count, attempted_queries, iteration
        )
        tool_log.append(decision)
        attempted_queries.append(decision["query"])

        print(f"[Agent {iteration + 1}/{MAX_AGENT_ITERATIONS}] 도구: {decision['tool']} | "
              f"검색어: '{decision['query']}' | 이유: {decision['reason']}")

        if decision["tool"] == "rag":
            raw_candidates = rag_search_vendors(decision["query"], limit_vendors=target_count * 3)
        else:
            raw_candidates = search_new_vendor_candidates(decision["query"])

        print(f"  → 후보 {len(raw_candidates)}건 발견, 정제 중...")
        for c in raw_candidates:
            if len(qualified) >= target_count:
                break
            add_if_qualified(c)

        print(f"  → 현재까지 확보: {len(qualified)}/{target_count}건")

    return {"source": "agent_search", "suppliers": qualified, "tool_log": tool_log}


# ────────────────────────────────

def resolve_suppliers_for_item(item_code, item_name, target_count=TARGET_SUPPLIER_COUNT, force_new_search=False):
    """
    품목 하나에 대해 공급사 확보.
    ① 기존 승인공급사 있으면 그대로 반환 (force_new_search=True면 이 단계 건너뜀)
    ② 없으면(또는 강제로) RAG로 찾고, 이메일 없으면 버리고, 부족하면 네이버 웹검색으로 보충
    """
    if not force_new_search:
        existing = get_existing_suppliers(item_code)
        if existing:
            return {"source": "existing", "suppliers": existing}

    # 신규탐색은 적응형 agent에게 위임 — RAG/웹검색 중 뭘 쓸지,
    # 검색어를 어떻게 바꿔볼지 AI가 스스로 판단하며 반복함
    return resolve_suppliers_agent(item_code, item_name, target_count)


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
