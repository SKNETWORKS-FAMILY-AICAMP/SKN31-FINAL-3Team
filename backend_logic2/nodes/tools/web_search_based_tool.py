"""
tools/web_search_based_tool.py - Tavily+네이버 기반 공급사 검색 도구.
supplier_search.py의 2순위(나라장터로 부족할 때만 쓰는 폴백)로 씀.

핵심 함수:
  normalize_item_name - ERP 품목명을 검색에 유리한 형태로 정규화
    (안전등급 등 기능적 스펙은 보존, 브랜드/색상 등 장식적 스펙만 제거)
  tavily_search_vendors - Tavily로 "{품목} 대량구매/조달업체/납품업체"
    검색어 3개를 병렬로 돌리고, 원본결과를 전부 합쳐서 필터+회사명추출을
    각각 딱 1번씩만 호출함(검색어마다 따로 하면 필터AI 3번+추출AI 3번=6번인데
    합쳐서 2번으로 줄임). 연락처는 naver_contact_enrichment.py(같은 tools
    폴더)의 enrich_contacts_batch로 위임 - 회사 여러 개를 묶어서(기본
    5개씩) AI 호출하므로 회사수가 늘어도 AI호출이 거의 비례해서 늘지
    않음. 페이지 가져오기도 requests 실패시(JS 렌더링 사이트 등) Jina
    Reader로 자동 폴백됨.

.env 필요: TAVILY_API_KEY, OPENAI_API_KEY, NAVER_CLIENT_ID, NAVER_CLIENT_SECRET

폴더 구조: tools/web_search_based_tool.py (이 파일),
tools/naver_contact_enrichment.py (같은 폴더에 있어야 함)

실행: python tools/web_search_based_tool.py
"""

import os
import json
from dotenv import load_dotenv

load_dotenv()


def normalize_item_name(item_name):
    """
    ERP 상세 품목명(브랜드+제품라인+모델번호+스펙 포함)을, 정부 조달분류명
    스타일의 카테고리명으로 정규화. 정부 API는 "더블에이 A4 복사용지
    (80g/2500매)" 같은 상세명이 아니라 "복사용지" 같은 일반명을 쓰고,
    Tavily 웹검색도 너무 구체적인 브랜드+스펙 조합보단 일반명으로 검색해야
    실제 결과가 잘 걸림 - 그래서 여기 맨 앞에서 딱 1번만 정규화해서,
    이후 모든 도구(정부API, Tavily)에 동일하게 넘김.

    중요: 스펙을 "장식적인 것"과 "기능적으로 핵심인 것"으로 구분해야 함.
    포장수량·색상·용량 같은 장식적 스펙은 떼어내되, 안전등급·성능등급·
    인증기준처럼 "이 물건이 실제로 그 용도에 쓸 수 있는지"를 결정하는
    스펙은 절대 떼면 안 됨. 예: "절단방지 니트릴 코팅장갑 (Cut Level D)"을
    그냥 "장갑"으로 뭉개면, 절단위험 전혀 없는 일반 목장갑 업체까지 다
    후보로 잡혀서 안전사고로 이어질 수 있음 - 실제로 발생했던 문제.
    (정부 조달분류 자체도 "목재"가 아니라 "목재판재"·"강화 및 재생 목재
    제조업"처럼 세부유형까지 구분하고 있어서, 과도하게 뭉개는 게 매칭에도
    오히려 불리함.)

    브랜드명/모델명만으로 카테고리가 텍스트에 명시적으로 안 드러나는
    경우(예: "동아 애니볼 501")도 있어서, 단순 정규식/형태소분석이 아니라
    AI가 일반지식으로 실제 제품 카테고리를 추론하게 함.
    """
    from langchain_openai import ChatOpenAI
    from langchain_core.prompts import PromptTemplate

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    prompt = PromptTemplate.from_template(
        "당신은 B2B 구매/검색 시스템의 품목명 정규화 전문가입니다.\n"
        "입력된 ERP 상세 품목명에서 브랜드, 모델명, 용량/수치/스펙(전압, 무게, 규격, 색상, 맛 등), 포장단위를 모두 제거하고,\n"
        "웹 검색 및 공급업체 찾기에 가장 적합한 '대표 카테고리(기본 품목명)' 단어만 추출하세요.\n\n"

        "[정규화 원칙]\n"
        "1. 수치, 단위, 스펙 정보(7000V, 3.3kg, 80g, 0.5mm, Cut Level 등)는 전량 제거합니다.\n"
        "2. 브랜드, 맛, 색상, 포장 형태는 전량 제거합니다.\n"
        "3. 품목의 핵심 정체성(용도/기능)을 나타내는 최소한의 명사 단어조합만 남깁니다.\n"
        "4. 오직 추출된 정규화 품목 단어만 출력하세요. (설명, 특수문자, 부연설명 절대 금지)\n\n"

        "[입출력 예시]\n"
        "입력: 꼬기닭가슴살 매운맛 150g -> 출력: 닭가슴살\n"
        "입력: 절연장갑 (7000V 내전압 Class 1) -> 출력: 절연장갑\n"
        "입력: 동아 애니볼 501 (0.5mm/3색혼합) -> 출력: 볼펜\n"
        "입력: 절단방지 니트릴 코팅장갑 (Cut Level D) -> 출력: 절단방지 장갑\n"
        "입력: ABC 분말소화기 (3.3kg 압력계형) -> 출력: 분말소화기\n"
        "입력: 산업용 HDPE 안전모 (백색, 래칫형) -> 출력: 안전모\n"
        "입력: 더블에이 A4 복사용지 (80g/2500매) -> 출력: 복사용지\n"
        "입력: 쓰리엠 포스트잇 크래프트 (76x76mm) -> 출력: 포스트잇\n"
        "입력: 3M N95 방진마스크 8210 -> 출력: 방진마스크\n\n"

        "품목명: {item_name}\n"
        "출력:"
    )
    result = (prompt | llm).invoke({"item_name": item_name}).content
    normalized = result.strip()
    print(f"[정규화] '{item_name}' -> '{normalized}'")
    return normalized


_BLACKLISTED_DOMAINS = [
    "namu.wiki", "wikipedia.org", "blog.naver.com", "cafe.naver.com",
    "kin.naver.com", "tistory.com", "velog.io", "brunch.co.kr",
    "youtube.com", "instagram.com", "facebook.com", "dcinside.com",
    "clien.net",
]


def _is_blacklisted(url):
    """URL 도메인 블랙리스트 체크 (무료, AI 호출 없음) - 명백한 것부터 걸러냄"""
    from urllib.parse import urlparse
    domain = urlparse(url or "").netloc.lower()
    return any(b in domain for b in _BLACKLISTED_DOMAINS)


def _filter_corporate_results(item_name, results):
    """
    검색결과를 페이지 열어보기 전에, 제목/URL만 보고 1차 판별.
    ⚠️ 기준을 "회사 공식사이트인가"에서 "회사명을 뽑아낼 수 있는가"로
    완화함. 이 단계 다음에 어차피 "회사명 추출"만 하고, 그 회사명으로
    네이버에서 진짜 공식사이트를 다시 찾으니, 원본이 뉴스기사여도
    실제 공급업체명이 언급되어 있으면 통과시켜야 함(예: 뉴스기사에
    "유니드", "대명케미칼" 같은 실제 취급업체명이 나온 사례가 실측으로
    확인됨) - "공식사이트만" 기준은 너무 엄격해서 쓸만한 정보를 놓쳤음.
    """
    from langchain_openai import ChatOpenAI
    from langchain_core.prompts import PromptTemplate

    if not results:
        return []

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    items_text = "\n".join(
        f"{i}. 제목: {r.get('title', '')}\n   URL: {r.get('url', '')}\n   내용: {(r.get('content') or r.get('raw_content') or '')[:150]}"
        for i, r in enumerate(results)
    )

    prompt = PromptTemplate.from_template(
        "'{item_name}' 관련 검색결과 목록입니다. 각 항목에서 실제 "
        "'{item_name}'을(를) 취급/공급하는 구체적인 회사명을 뽑아낼 수 "
        "있을지 판단하세요.\n\n"
        "통과시켜야 하는 것: 회사 공식홈페이지, 제품소개 페이지는 물론이고, "
        "뉴스기사·블로그·포럼이라도 그 안에 실제 취급업체명이 구체적으로 "
        "언급되어 있으면 통과 (예: '(주)미화가 생산하고 대명케미칼이 "
        "유통하는...' 같은 뉴스 문장에도 실제 회사명이 있으므로 통과).\n\n"
        "제외해야 하는 것: 회사명이 전혀 안 나오는 것 (순수 사고/사건 "
        "기사, 개인 신변잡기, 일반 백과사전 설명, 특정 업체 언급 없는 "
        "시장동향 리포트 등).\n\n"
        "애매하면(회사명이 있을지 없을지 확신이 안 서면) 일단 통과시키세요 "
        "- 이 단계는 느슨하게, 다음 단계(회사명 추출)에서 더 정확히 걸러집니다.\n\n"
        "{items}\n\n"
        '반드시 이 JSON 형식으로만 답하세요: {{"corporate_indices": [통과시킬 항목의 번호들]}}'
    )

    result = (prompt | llm).invoke({"item_name": item_name, "items": items_text}).content
    try:
        cleaned = result.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        indices = json.loads(cleaned).get("corporate_indices", [])
        return [results[i] for i in indices if 0 <= i < len(results)]
    except Exception as e:
        print(f"    [_filter_corporate_results 파싱 실패] {e}, 필터 없이 원본 통과")
        return results


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


def tavily_search_vendors(item_name, target_count=10, max_results_per_query=10):
    """
    2순위(최후 폴백): 검색어 3개(대량구매/조달업체/납품업체)를 병렬로
    Tavily 검색한 다음, 그 원본 결과를 전부 하나로 합쳐서 필터+회사명
    추출을 딱 1번씩만 돌림 (이전엔 검색어마다 따로 돌려서 필터AI 3번+
    추출AI 3번 = 6번이었는데, 이제 합쳐서 2번).

    연락처 확보는 naver_contact_enrichment.enrich_contacts_batch로 위임 -
    회사 여러 개를 묶어서 AI 호출하므로, 회사수가 늘어도 AI호출이 거의
    비례해서 늘지 않음.
    """
    from tavily import TavilyClient
    from concurrent.futures import ThreadPoolExecutor, as_completed
    try:
        from .naver_contact_enrichment import enrich_contacts_batch
    except ImportError:  # tools 폴더에서 직접 실행할 때
        from naver_contact_enrichment import enrich_contacts_batch

    client = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])
    query_suffixes = ["공식 홈페이지", "제조 전문 주식회사", "납품 공급 업체"]

    def _search_only(suffix):
        query = f"{item_name} {suffix}"
        try:
            response = client.search(
                query=query, max_results=max_results_per_query, include_raw_content=True,
                country="South Korea", exclude_domains=_BLACKLISTED_DOMAINS,
            )
        except Exception as e:
            print(f"  [Tavily] '{query}' 실패: {e}")
            return []
        raw_results = response.get("results", [])
        print(f"  [Tavily] '{query}' -> {len(raw_results)}건")
        return [r for r in raw_results if not _is_blacklisted(r.get("url", ""))]

    print(f"  [Tavily] 검색어 {len(query_suffixes)}개 병렬 실행 중...")
    all_raw_results = []
    with ThreadPoolExecutor(max_workers=len(query_suffixes)) as executor:
        futures = [executor.submit(_search_only, s) for s in query_suffixes]
        for future in as_completed(futures):
            all_raw_results.extend(future.result())

    print(f"  누적 원본결과 {len(all_raw_results)}건, 필터+회사명추출 중 (AI 각 1번)...")
    corporate_results = _filter_corporate_results(item_name, all_raw_results)
    print(f"  -> 필터 통과: {len(corporate_results)}/{len(all_raw_results)}건")

    combined_text = "\n\n".join(
        f"[{r.get('url')}]\n{(r.get('raw_content') or r.get('content', ''))[:1000]}"
        for r in corporate_results
    )
    all_company_names = set(_extract_company_names_llm(item_name, combined_text))
    print(f"  누적 회사명 {len(all_company_names)}개: {sorted(all_company_names)}")

    names_to_process = sorted(all_company_names)
    cap = target_count * 2
    if len(names_to_process) > cap:
        print(f"  -> {cap}개로 제한 (나머지 {len(names_to_process) - cap}개는 조회 생략)")
        names_to_process = names_to_process[:cap]

    print(f"  네이버 배치 연락처 조회 중...")
    enriched_list = enrich_contacts_batch(names_to_process, item_name=item_name, batch_size=5, target_count=target_count)

    candidates = []
    for enriched in enriched_list:
        # 채택기준: 이메일 필수 (전화만 있고 이메일 없으면 제외)
        # 채택기준: 이메일 OR 전화 (임시로 완화 - 지금 필터가 너무 많이 걸러내서
        # 디버깅 중, 안정화되면 다시 정책 논의)
        if enriched.get("email") or enriched.get("phone"):
            candidates.append({
                "name": enriched["name"], "email": enriched.get("email"),
                "phone": enriched.get("phone"), "site_url": enriched.get("site_url"),
                "source": "tavily",
            })
        else:
            print(f"      - '{enriched['name']}': 연락처 확보 실패, 제외")

    return candidates
