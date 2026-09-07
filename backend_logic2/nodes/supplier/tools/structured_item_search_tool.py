"""
tools/structured_item_search_tool.py - tests/structured_item_normalize_test.py
에서 검증된 "구조화 추출 기반 Tavily 검색"을 프로덕션 tool로 승격한 것.

기존 web_search_based_tool.py의 normalize_item_name은 품목명을 "하나의
정규화된 문자열"로 뭉뚱그려서 그 문자열 하나로만 검색했음. 이 파일은
그 대신 품목명을 구조화된 필드(품명/소재/안전등급/치수/intent/
exclude_keywords)로 쪼개서 뽑고, intent(제품유통 vs 가공서비스)에 맞는
검색어 템플릿과 exclude_keywords/exact_match를 적용해 Tavily 검색어를
따로 조합한다. 실측(2026-09-07, CNC아노다이징/2B10K가스켓 케이스)으로
효과 확인 후 프로덕션에 반영함:
  - "2B 10K" 같은 스펙코드가 연필심/숫자 1만으로 오인되는 문제 ->
    exact_match(따옴표 강제)로 방지
  - "CNC/아노다이징"(가공서비스)인데 장비 판매상이 잡히는 문제 ->
    intent별 suffix 템플릿 + exclude_keywords로 방지

collect_candidate_names_structured()가 web_search_based_tool.py의
tavily_collect_candidate_names()와 동일한 인터페이스(인자/반환형식)를
가지도록 만들어서 supplier_search.py에서 그대로 교체 가능하게 함.

.env 필요: OPENAI_API_KEY, TAVILY_API_KEY
"""

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv

load_dotenv()

from backend_logic2.nodes.supplier.tools.web_search_based_tool import (
    _BLACKLISTED_DOMAINS,
    _is_blacklisted,
    _filter_corporate_results,
    _extract_company_names_llm,
)

# intent별 Tavily 검색어 suffix 템플릿. 크게 두 버킷만 둠(스코프 과확장 방지) -
# "완제품/부품 유통을 찾는 경우" vs "가공/제작 서비스를 찾는 경우".
TAVILY_SUFFIX_TEMPLATES = {
    "product_distribution": ["국내 총판 대리점", "국내 제조 전문 주식회사", "국내 납품 공급 업체"],
    "processing_service": ["국내 가공 전문업체", "부품제작 외주업체", "국내 시공 전문업체"],
}


def extract_structured_item(raw_item_name: str) -> dict:
    """ERP 품목명을 품명/소재/안전등급/치수/의도/제외키워드로 구조화."""
    from langchain_openai import ChatOpenAI
    from langchain_core.prompts import PromptTemplate

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    prompt = PromptTemplate.from_template(
        "다음은 ERP에 등록된 품목명입니다. 이걸 아래 필드로 구조화해서 "
        "분해하세요.\n\n"
        "품목명: {item_name}\n\n"
        "필드 설명:\n"
        "- item_type (품명): 이 물건/서비스가 근본적으로 무엇인지 (예: "
        "오일씰, 장갑, CNC가공, 아노다이징). 브랜드/색상/포장단위 같은 "
        "장식적 요소는 다 떼어낸 일반명사.\n"
        "- material (소재): 원재료/재질 (예: 니트릴, 스테인리스, AL6061). "
        "없으면 null.\n"
        "- material_is_functional (소재가 기능적으로 중요한가): 이 소재가 "
        "내화학성/내구성 등 실제 용도에 영향을 주는지 true/false.\n"
        "- safety_grade (성능/안전등급/규격코드): 이 물건이 특정 위험/성능 "
        "기준이나 규격코드를 충족해야 하면 그대로 (예: Cut Level D, 2B 10K, "
        "방폭등급 Ex d). 숫자/영문이 섞인 규격코드는 검색엔진이 다른 뜻으로 "
        "오인하기 쉬우니(예: '10K'를 숫자 1만으로, '2B'를 연필심 종류로) "
        "여기 정확히 채워두는 게 중요합니다. 없으면 null.\n"
        "- exact_dimension (정확한 치수): 숫자로 된 정밀 치수 (예: "
        "30x47x7mm, 5T=두께5mm). 검색 단계가 아니라 나중에 견적 비교 "
        "단계에서 정밀 대조할 값이라 구분해둠. 없으면 null.\n"
        "- intent (검색 의도): 'product_distribution'(완제품/부품/소재를 "
        "유통·판매하는 회사를 찾는 경우, 예: 오일씰, 튜브, 원자재) 또는 "
        "'processing_service'(가공·제작·시공 등 서비스를 제공하는 업체를 "
        "찾는 경우, 예: CNC가공, 아노다이징, 열처리, 도금) 중 하나.\n"
        "- exclude_keywords (제외 키워드): 이 intent와 반대되는 회사를 "
        "걸러내기 위한 키워드 3~5개. 예를 들어 intent가 "
        "processing_service(가공서비스를 찾는 것)면, 그 가공에 쓰이는 "
        "'장비/기계 자체를 판매'하는 회사는 우리가 원하는 게 아니므로 "
        "['공작기계', '장비판매', '머시닝센터', '수입'] 같은 키워드를 "
        "뽑으세요. intent가 product_distribution이면 반대로 그 완제품을 "
        "'가공 원료/부자재로만 취급하는' 회사 등을 제외어로 뽑으세요.\n\n"
        '반드시 이 JSON 형식으로만 답하세요: {{"item_type": "...", '
        '"material": "... 또는 null", "material_is_functional": true 또는 '
        'false, "safety_grade": "... 또는 null", "exact_dimension": "... '
        '또는 null", "intent": "product_distribution 또는 '
        'processing_service", "exclude_keywords": ["...", "..."]}}'
    )
    result = (prompt | llm).invoke({"item_name": raw_item_name}).content
    cleaned = result.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(cleaned)


def build_source_queries(structured: dict) -> dict:
    """구조화된 필드로 소스별 검색어 조합을 만듦."""
    item_type = structured.get("item_type") or ""
    material = structured.get("material")
    material_is_functional = structured.get("material_is_functional")
    safety_grade = structured.get("safety_grade")
    intent = structured.get("intent") or "product_distribution"
    exclude_keywords = structured.get("exclude_keywords") or []

    gov_query_parts = [item_type]
    if material_is_functional and material:
        gov_query_parts.append(material)
    gov_query = " ".join(p for p in gov_query_parts if p)

    suffixes = TAVILY_SUFFIX_TEMPLATES.get(intent, TAVILY_SUFFIX_TEMPLATES["product_distribution"])

    core_phrase = item_type
    use_exact_match = False
    if safety_grade:
        core_phrase = f"{item_type} {safety_grade}"
        use_exact_match = True

    exclude_suffix = " ".join(f"-{kw}" for kw in exclude_keywords)

    tavily_queries = []
    for suffix in suffixes:
        base = f'"{core_phrase}"' if use_exact_match else core_phrase
        query = f"{base} {suffix}"
        if exclude_suffix:
            query = f"{query} {exclude_suffix}"
        tavily_queries.append(query)

    return {
        "gov_query": gov_query,
        "tavily_queries": tavily_queries,
        "tavily_exact_match": use_exact_match,
        "tavily_exclude_keywords": exclude_keywords,
        "intent": intent,
    }


def _run_structured_tavily_queries(item_type, query_result, max_results_per_query=10, case_id=None):
    """build_source_queries()가 만든 검색어를 실제로 실행해서 회사명만 뽑음."""
    from tavily import TavilyClient

    client = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])
    exact_match = query_result["tavily_exact_match"]
    queries = query_result["tavily_queries"]

    def _search_only(query):
        try:
            response = client.search(
                query=query, max_results=max_results_per_query, include_raw_content=True,
                country="South Korea", language="ko", filter_by_language=True,
                exact_match=exact_match, exclude_domains=_BLACKLISTED_DOMAINS,
            )
        except Exception as e:
            print(f"  [Tavily] '{query}' 실패: {e}")
            return []
        raw_results = response.get("results", [])
        print(f"  [Tavily] '{query}' -> {len(raw_results)}건")
        return [r for r in raw_results if not _is_blacklisted(r.get("url", ""))]

    print(f"  [Tavily-구조화] 검색어 {len(queries)}개 병렬 실행 중 (exact_match={exact_match})...")
    all_raw_results = []
    with ThreadPoolExecutor(max_workers=len(queries) or 1) as executor:
        futures = [executor.submit(_search_only, q) for q in queries]
        for f in as_completed(futures):
            all_raw_results.extend(f.result())

    print(f"  누적 원본결과 {len(all_raw_results)}건, 필터+회사명추출 중 (AI 각 1번)...")
    corporate_results = _filter_corporate_results(item_type, all_raw_results)
    print(f"  -> 필터 통과: {len(corporate_results)}/{len(all_raw_results)}건")

    combined_text = "\n\n".join(
        f"[{r.get('url')}]\n{(r.get('raw_content') or r.get('content', ''))[:1000]}"
        for r in corporate_results
    )
    return sorted(set(_extract_company_names_llm(item_type, combined_text, case_id=case_id)))


def collect_candidate_names_structured(item_name, target_count=10, max_results_per_query=10, case_id=None):
    """
    web_search_based_tool.tavily_collect_candidate_names()와 동일한
    인터페이스(인자/반환형식: [{"name","source","operation","raw"}, ...])를
    가짐 - supplier_search.py에서 그대로 교체해서 쓸 수 있게 하기 위함.

    AI 호출은 구조화추출(1) + 필터링(1) + 회사명추출(1) 총 3번으로,
    기존 tavily_collect_candidate_names와 AI 호출 횟수가 동일함.
    """
    structured = extract_structured_item(item_name)
    print(
        f"  [구조화추출] item_type='{structured.get('item_type')}' "
        f"intent={structured.get('intent')} safety_grade={structured.get('safety_grade')} "
        f"exclude_keywords={structured.get('exclude_keywords')}"
    )

    query_result = build_source_queries(structured)
    item_type = structured.get("item_type") or item_name

    company_names = _run_structured_tavily_queries(
        item_type, query_result, max_results_per_query=max_results_per_query, case_id=case_id
    )
    print(f"  [Tavily-구조화] 누적 회사명 {len(company_names)}개: {company_names}")

    cap = target_count * 2
    if len(company_names) > cap:
        print(f"  [Tavily-구조화] -> {cap}개로 제한 (나머지 {len(company_names) - cap}개는 생략)")
        company_names = company_names[:cap]

    return [
        {"name": n, "source": "tavily", "operation": "tavily_structured", "raw": {}}
        for n in company_names
    ]
