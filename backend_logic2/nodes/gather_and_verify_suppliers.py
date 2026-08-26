"""
nodes/gather_and_verify_suppliers.py - 우선순위 기반 공급사 탐색 파이프라인.
⚠️ RAG(내부 DB) 단계는 뺀 버전 — vendor_catalog가 사업자등록 기반이라
factory_registry(실제 공장등록) 및 정부조달실적이랑 관련성이 많이
어긋나서(관계없는 업체가 자주 섞임) 일단 빼고 테스트해보는 버전.

우선순위:
  1순위: public_data_api_search.py - 정부 조달실적 (실제 거래기록, 최고 신뢰도)
  2순위: Tavily 웹검색 - 최후 폴백 (1순위로 부족할 때만)
     "{품목} 대량구매/대량주문/조달업체/납품업체" 등 여러 검색어로 돌려서
     회사명만 넓게 추출 -> 각 회사명을 네이버로 재검색해서 연락처 확보
     (Tavily 스니펫이 짧아서 연락처까지 한번에 뽑는 건 실패율이 높았음 -
     그래서 "회사명 찾기"까지만 Tavily에 맡기고, 연락처는 이미 잘 되는
     네이버 파이프라인에 위임)

목표 개수(target_count) 채우면 그 아래 순위는 건너뜀 - 불필요한 API
호출/비용을 줄이기 위함.

연락처 보강 (이메일 없는 후보에):
  a) 공장등록정보(회사명 검색)로 먼저 시도 - 정부검증된 전화/홈페이지 확보
     (제조업 품목일 때 특히 잘 맞음, 유통/MRO 품목은 안 걸릴 수 있음 - 정상)
  b) 그래도 이메일 없으면 네이버로 최종 시도

마지막: 전체 후보를 AI 한 번에 검토해서 관련성 확인+중복병합+
신뢰도 순 정렬해서 상위 목표개수로 압축.

.env 필요 (키 이름 통일됨):
  DATA_GO_KR_SERVICE_KEY - 정부API 전체 공용. 조달실적(public_data_api_search)
    이랑 공장등록정보(factory_registry_search) 둘 다 이 키 하나로 씀 -
    공공데이터포털은 계정당 키 하나고, 서비스별로 활용신청만 각각 하면 됨.
  TAVILY_API_KEY, OPENAI_API_KEY, NAVER_CLIENT_ID, NAVER_CLIENT_SECRET

폴더 구조: backend_logic2/erp_client.py, backend_logic2/nodes/이 파일
(public_data_api_search.py, naver_contact_enrichment.py,
factory_registry_search.py도 같은 폴더에 있어야 함)

실행: python nodes/gather_and_verify_suppliers.py
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
from dotenv import load_dotenv

from tools.public_data_api_search import find_verified_suppliers
from tools.naver_contact_enrichment import enrich_contact_by_company_name
from tools.factory_registry_search import get_factory_by_company_name, factory_to_candidate

load_dotenv()


def _extract_company_names_llm(item_name, text):
    """
    Tavily 검색결과 텍스트 뭉치에서 "회사명"만 뽑아냄 (연락처는 신경 안 씀).
    Tavily 스니펫이 너무 짧아서(실측 88~122자) 연락처까지 한 번에 뽑는 건
    무리였음 — 그래서 부담을 낮춰서 "회사명 찾기"라는 쉬운 일만 시키고,
    연락처는 뒤에서 네이버로 따로 확보하는 방식으로 바꿈.
    """
    from langchain_openai import ChatOpenAI
    from langchain_core.prompts import PromptTemplate

    if not text.strip():
        return []

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    prompt = PromptTemplate.from_template(
        "다음은 '{item_name}' 관련 검색결과 텍스트 모음입니다. "
        "이 안에서 실제 판매,공급업체로 보이는 회사명들을 찾아서 목록으로 뽑아주세요.\n\n"
        "{text}\n\n"
        "규칙:\n"
        "- 확실히 회사명으로 보이는 것만 뽑으세요 (개인 블로그, 커뮤니티, 뉴스매체 이름 등은 제외)\n"
        "- 잡코리아, 사람인 등 제3자 채용/중개 사이트 자체는 회사명으로 뽑지 마세요\n"
        "- 중복은 하나로 합치세요\n\n"
        '반드시 이 JSON 형식으로만 답하세요: {{"company_names": ["회사명1", "회사명2", ...]}}'
    )
    result = (prompt | llm).invoke({"item_name": item_name, "text": text[:8000]}).content

    try:
        cleaned = result.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(cleaned).get("company_names", [])
    except Exception as e:
        print(f"    [_extract_company_names_llm 파싱 실패] {e}")
        return []


def tavily_search_vendors(item_name, max_results_per_query=10):
    """
    2순위(최후 폴백): 검색어 여러 변형으로 Tavily를 돌려서 "회사명"만
    넓게 모으고, 각 회사명을 네이버로 재검색해서 연락처(이메일/전화)를
    확보함. (naver_contact_enrichment.py가 이미 잘 동작하는 걸 재사용)
    """
    from tavily import TavilyClient

    client = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])
    query_suffixes = ["대량구매", "대량주문", "조달업체", "납품업체"]

    all_company_names = set()
    tavily_call_count = 0

    for suffix in query_suffixes:
        query = f"{item_name} {suffix}"
        tavily_call_count += 1
        try:
            response = client.search(query=query, max_results=max_results_per_query, include_raw_content=True)
        except Exception as e:
            print(f"  [Tavily 호출 #{tavily_call_count}] '{query}' 실패: {e}")
            continue

        raw_results = response.get("results", [])
        print(f"  [Tavily 호출 #{tavily_call_count}] '{query}' -> {len(raw_results)}건")

        combined_text = "\n\n".join(
            f"[{r.get('url')}]\n{(r.get('raw_content') or r.get('content', ''))[:1000]}"
            for r in raw_results
        )
        names = _extract_company_names_llm(item_name, combined_text)
        print(f"    -> 추출된 회사명: {names}")
        all_company_names.update(names)

    print(f"\n  [Tavily 총 호출 횟수: {tavily_call_count}회] 누적 회사명 {len(all_company_names)}개: {sorted(all_company_names)}")
    print(f"  네이버로 연락처 조회 중...")

    candidates = []
    for name in all_company_names:
        enriched = enrich_contact_by_company_name({"name": name}, item_name=item_name)
        if enriched.get("email") or enriched.get("phone"):
            candidates.append({
                "name": enriched.get("name", name),
                "email": enriched.get("email"),
                "phone": enriched.get("phone"),
                "site_url": enriched.get("site_url"),
                "source": "tavily",
            })
        else:
            if enriched.get("is_relevant") is False:
                print(f"      - '{name}': 관련성 검증 실패로 제외 ({enriched.get('relevance_reason')})")
            else:
                print(f"      - '{name}': 연락처 확보 실패, 제외")

    return candidates


def _enrich_contact(vendor, item_name):
    """
    이메일이 없는 후보에 연락처 보강. 순서:
    a) 공장등록정보(회사명 검색) 먼저 시도 - 정부검증된 전화/홈페이지 확보
    b) 그래도 이메일 없으면 네이버로 최종 시도
    """
    if vendor.get("email"):
        return vendor

    name = vendor["name"]

    if os.environ.get("DATA_GO_KR_SERVICE_KEY"):
        print(f"    '{name}' 이메일 없음, 공장등록정보 확인 중...")
        try:
            factory_matches = get_factory_by_company_name(name, num_rows=5)
        except Exception as e:
            print(f"      -> 공장등록정보 조회 실패: {e}")
            factory_matches = []

        if factory_matches:
            with_phone = [f for f in factory_matches if f.get("cmpnyTelno")]
            best = with_phone[0] if with_phone else factory_matches[0]
            fc = factory_to_candidate(best)
            if not vendor.get("phone") and fc.get("phone"):
                vendor = {**vendor, "phone": fc["phone"]}
            if not vendor.get("site_url") and fc.get("site_url"):
                vendor = {**vendor, "site_url": fc["site_url"]}
                print(f"      -> 검증된 홈페이지 확보: {fc['site_url']}")

    print(f"    네이버 기반 이메일 추출 시도 중...")
    enriched = enrich_contact_by_company_name(dict(vendor), item_name=item_name)

    if enriched.get("email"):
        print(f"      -> 이메일 확보: {enriched['email']}")
    else:
        print(f"      -> 이메일 추출 실패")

    return enriched


def _normalize_url(url):
    """
    URL을 루트 도메인만 남기고 정규화. Tavily/공장등록정보 등에서 나온
    site_url이 깊은 링크(예: abc.co.kr/products/battery/item123.html)
    이거나 프로토콜이 빠진 형태(www.abc.co.kr)로 들어오는 경우가 있어서,
    항상 "https://도메인" 형태의 홈페이지 주소로 통일해줌.
    """
    if not url:
        return None
    from urllib.parse import urlparse
    candidate = url if url.startswith("http") else f"http://{url}"
    parsed = urlparse(candidate)
    if not parsed.netloc:
        return url  # 파싱 실패하면 원본 그대로 (정보 손실 방지)
    return f"{parsed.scheme}://{parsed.netloc}"


def _verify_and_standardize(item_name, candidates, top_n=10):
    """최종 검증+정형화: AI 한 번에 관련성확인+중복병합+신뢰도순 정렬."""
    from langchain_openai import ChatOpenAI
    from langchain_core.prompts import PromptTemplate

    if not candidates:
        return []

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    candidates_json = [
        {
            "name": c["name"], "email": c.get("email"), "phone": c.get("phone"),
            "site_url": _normalize_url(c.get("site_url")), "source": c["source"],
        }
        for c in candidates
    ]

    prompt = PromptTemplate.from_template(
        "'{item_name}'을 공급할 만한 업체 후보 목록입니다. 서로 다른 출처"
        "(정부조달실적, 웹검색)에서 모아온 것이라 형식이 제각각이고, "
        "관련없는 것이 섞여있을 수 있습니다.\n\n"
        "후보 목록:\n{candidates}\n\n"
        "다음을 해주세요:\n"
        "1. 실제로 이 품목을 공급할 만한 업체만 남기세요 (명백히 관련없으면 제외)\n"
        "2. 같은 회사가 여러 출처에서 중복되면 하나로 합치세요 (source는 합쳐서 표기)\n"
        "3. 신뢰도 순으로 정렬해서 상위 {top_n}개만 남기세요. 신뢰도 판단기준:\n"
        "   - source에 public_data_api(정부 실거래 실적)가 포함되면 가장 높음\n"
        "   - email과 phone 둘 다 있으면 하나만 있는 것보다 높음\n"
        "   - site_url이 있으면(홈페이지 확인됨) 없는 것보다 높음\n"
        "   - source가 tavily(단순 웹검색, 미검증)뿐이면 가장 낮게 취급\n"
        "4. reason 작성 시, 단순히 '관련있음' 같은 뭉뚱그린 표현 대신, 이 업체가 "
        "'{item_name}'과 관련해 구체적으로 어떤 세부 유형·제품군·규격을 취급하는 "
        "것으로 보이는지 파악해서 1문장으로 기술하세요 (예: '2차전지용 리튬이온 "
        "배터리 셀 전문 제조', '산업용 안전화 중 방수·내화학 라인 위주 생산' 등 "
        "가능한 만큼 구체적으로)\n"
        "5. 각 후보에 confidence(신뢰도)를 다음 기준으로 반드시 매기세요:\n"
        "   - high: 실제로 이 품목을 제조/판매한다는 명확한 근거가 있음 "
        "(정부 실거래기록, 홈페이지에 명시된 제품군 등)\n"
        "   - medium: 업종상 취급할 가능성이 높지만 명시적 확인은 안 됨\n"
        "   - low: '~일 수도 있음', '~로 추정됨' 수준의 막연한 추측일 뿐임\n"
        "6. confidence가 low인 후보는 최종 목록에서 아예 빼세요 (high/medium만 포함)\n\n"
        '반드시 이 JSON 형식으로만 답하세요: '
        '{{"suppliers": [{{"name": "...", "email": "값 또는 null", "phone": "값 또는 null", '
        '"site_url": "값 또는 null", "source": "출처(들)", "confidence": "high 또는 medium", '
        '"reason": "구체적인 취급품목/유형 기술"}}]}}'
    )

    result = (prompt | llm).invoke({
        "item_name": item_name,
        "candidates": json.dumps(candidates_json, ensure_ascii=False, indent=2),
        "top_n": top_n,
    }).content

    try:
        cleaned = result.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        suppliers = json.loads(cleaned).get("suppliers", [])

        # 이중 안전장치: 프롬프트로 low는 빼라고 지시했지만, LLM이 놓칠 수 있어서
        # 코드에서도 한 번 더 필터링 (confidence 필드 자체가 없는 경우는 일단 통과시킴)
        before_count = len(suppliers)
        suppliers = [s for s in suppliers if s.get("confidence") != "low"]
        if before_count != len(suppliers):
            print(f"  [코드단 필터] confidence=low {before_count - len(suppliers)}건 추가 제외")

        return suppliers[:top_n]
    except Exception as e:
        print(f"  [verify_and_standardize 파싱 실패] {e}, 원본 상위 {top_n}건 그대로 반환")
        return candidates[:top_n]


def gather_and_verify_suppliers(item_name, target_count=10):
    """
    우선순위 순으로 시도: 정부조달실적(1순위) -> Tavily(2순위,최후폴백)
    target_count 채우면 그 아래 순위는 건너뜀.
    """
    all_candidates = []

    print(f"\n[1순위] 정부 조달실적 검색 중... (최우선 신뢰도)")
    try:
        gov_results = find_verified_suppliers(item_name, max_results=target_count)
        for r in gov_results:
            all_candidates.append({
                "name": r["name"], "email": r["email"], "phone": r["phone"],
                "site_url": r.get("homepage"), "source": "public_data_api",
            })
        print(f"  -> {len(gov_results)}건")
    except Exception as e:
        print(f"  [실패] {e}")

    if len(all_candidates) < target_count:
        print(f"\n[2순위] Tavily 웹검색 중... (최후 폴백)")
        tavily_results = tavily_search_vendors(item_name, max_results_per_query=target_count)
        all_candidates.extend(tavily_results)
        print(f"  -> {len(tavily_results)}건")
    else:
        print(f"\n[2순위] 1순위로 목표 개수 채워서 Tavily 생략")

    print(f"\n총 {len(all_candidates)}건 수집 (필터 전)")

    def _has_sufficient_contact(c):
        if c.get("email"):
            return True
        if c.get("phone") and c.get("site_url"):
            return True
        return False

    qualified = [c for c in all_candidates if _has_sufficient_contact(c)]
    print(f"연락 가능한 것만(이메일 또는 전화+사이트): {len(qualified)}건 "
          f"(제외 {len(all_candidates) - len(qualified)}건)")

    print(f"\n최종 검증+정형화 중... (신뢰도 상위 {target_count}개로 압축)")
    final = _verify_and_standardize(item_name, qualified, top_n=target_count)
    print(f"최종 결과: {len(final)}건")

    return final


if __name__ == "__main__":
    item_name = input("검색할 품목명 입력: ").strip()
    target_input = input("최종 몇 개까지 뽑을까요? (그냥 엔터시 10개): ").strip()
    target_count = int(target_input) if target_input else 10

    results = gather_and_verify_suppliers(item_name, target_count=target_count)

    print(f"\n{'='*50}")
    print(f"=== '{item_name}' 최종 공급사 후보 ===")
    print(f"{'='*50}")

    if not results:
        print("결과 없음")

    for r in results:
        print(f"\n{r.get('name')}")
        print(f"  이메일: {r.get('email') or '(없음)'}")
        print(f"  전화: {r.get('phone') or '(없음)'}")
        print(f"  사이트: {r.get('site_url') or '(없음)'}")
        print(f"  출처: {r.get('source')}")
        print(f"  이유: {r.get('reason')}")