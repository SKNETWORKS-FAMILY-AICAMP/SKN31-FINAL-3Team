"""
tools/naver_contact_enrichment.py - 회사명으로 네이버 검색해서 공식
홈페이지를 찾고, 거기서 이메일/전화를 추출하는 모듈. web_search_based_tool.py
와 narajangteo_search_based_tool.py 둘 다 이 파일의 함수를 공유해서 씀.

핵심 변경(배치처리 도입):
  이전: 회사 1개당 AI를 최대 3번(사이트선택+추출+재시도추출) 호출 ->
        회사 N개면 최대 3N번, 회사수에 비례해서 계속 늘어남
  지금: "사이트 선택"은 AI 없이 네이버 검색 1등 결과를 그대로 채택
        (정확도는 약간 낮아질 수 있으나, 네이버 자체 랭킹을 신뢰).
        "이메일/전화 추출"은 여러 회사를 묶어서(기본 5개씩) AI 1번에
        같이 처리 -> 호출횟수가 회사수에 거의 비례하지 않게 됨.

핵심 변경(Jina Reader 폴백):
  requests.get()은 JS로 콘텐츠를 채우는 최신 사이트에서 빈 껍데기만
  받아오는 경우가 있음. 받아온 텍스트가 너무 짧으면(200자 미만),
  Jina Reader(r.jina.ai, 무료티어)로 재시도해서 JS 렌더링된 진짜
  콘텐츠를 받아옴.

단건 처리가 필요한 경우를 위해 기존 함수(enrich_contact_by_company_name)
도 그대로 남겨둠 - 새 배치 함수(enrich_contacts_batch)가 여러 회사를
한 번에 처리할 때 기본적으로 씀.

폴더 구조: backend_logic2/erp_client.py, backend_logic2/nodes/이 파일

실행: python nodes/naver_contact_enrichment.py
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
import json
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

load_dotenv()

_EXCLUDED_CONTACT_DOMAINS = [
    "jobkorea.co.kr", "saramin.co.kr", "wanted.co.kr", "catch.co.kr",
    "incruit.com", "albamon.com", "job.co.kr", "linkedin.com",
    # 회사 자체 사이트가 아닌 제3자 플랫폼(공식홈페이지로 오인되기 쉬움)
    "story.kakao.com", "kakao.com", "weseb.com", "blog.naver.com",
    "cafe.naver.com", "post.naver.com", "band.us", "instagram.com",
    "facebook.com", "youtube.com", "smartstore.naver.com",
    # 제3자 산업정보/거래 디렉토리 플랫폼 (web_search_based_tool.py와 동일)
    "komachine.com", "webify.kr", "toolok.co.kr", "bizno.net",
    "kind.krx.co.kr", "kpi.or.kr", "valves-suppliers.com", "g2bmarket.com",
    # 2026-08-31 추가: '오일씰' 테스트에서 실제로 걸린 제3자 사이트.
    # grandculture.net(한국학중앙연구원 향토문화전자대전)은 회사 소개
    # 백과사전 페이지지 회사 자체 사이트가 아니고, SSL 인증서도 깨져있어서
    # 연락처 출처로 부적합. myfactory.co.kr은 제3자 공장정보 디렉토리로 추정.
    "grandculture.net", "myfactory.co.kr",
]

# 제3자 "기업정보/스타트업 프로필" 플랫폼 URL 구조 탐지 (2026-08-31 추가).
# 도메인을 블랙리스트에 넣는 방식은 새 서비스가 나올 때마다 계속
# 추가해야 하는 두더지잡기라 한계가 있음(사용자 지적: "블랙리스트는
# 아무리 추가해도 의미없음"). 대신 이런 플랫폼들이 공통으로 쓰는 URL
# 구조(/company/<UUID>, /companies/<숫자ID> 등 - 아래 AI 프롬프트에도
# 이미 "목록형 구조"로 명시된 판단 신호)를 정규식으로 미리 걸러서,
# AI가 이 신호를 놓치는 경우(실측 확인: rndcircle.io - 스타트업/기업
# 프로필 조회 플랫폼으로 추정 - 가 정확히 이 URL 패턴이었는데도 AI
# 판단을 통과한 사례가 실제 '멀티탭' 테스트에서 나옴)에도 결정적으로
# 제외되도록 함.
_DIRECTORY_URL_PATTERNS = [
    re.compile(r"/compan(?:y|ies)/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"),
    re.compile(r"/compan(?:y|ies)/\d{3,}"),
    re.compile(r"maker_cd="),
    re.compile(r"goods_view\.php"),
]


def _looks_like_directory_url(url):
    """URL 구조만 보고 제3자 기업정보/카탈로그 플랫폼인지 판단(AI 호출 없음, 결정적)."""
    if not url:
        return False
    return any(p.search(url) for p in _DIRECTORY_URL_PATTERNS)


def _parse_json_response(text):
    """AI 응답에서 JSON만 안전하게 파싱"""
    try:
        cleaned = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return json.loads(cleaned)
    except Exception:
        return None


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
        print(f"    [_search_naver_web] 검색 실패 ('{query}'): {e}")
        return []

    for item in items:
        item["title"] = re.sub(r"<.*?>", "", item.get("title", ""))
        item["description"] = re.sub(r"<.*?>", "", item.get("description", ""))
    return items


# ---------- 사이트 찾기 (AI 없이, 네이버 1등 결과 채택) ----------

def _pick_best_site_candidate(company_name, candidates, item_name=None, case_id=None):
    """
    후보가 여럿이면, title+description을 AI한테 보여주고 "진짜 이 회사
    공식홈페이지 같은 것"을 고르게 함. "그냥 1등 채택"은 Tavily가 뽑은
    회사명이 정확한 법인명이 아니거나 흔한 이름일 때, 동명이인 회사나
    무관한 페이지를 잘못 채택하는 문제가 실제로 확인되어 다시 복원함.

    2026-08-31 판단기준 강화: 도메인 블랙리스트는 새 제3자 사이트가
    나올 때마다 계속 추가해야 하는 두더지잡기라 한계가 있음(실제로
    'grandculture.net'-한국학중앙연구원 향토문화전자대전 같은, "여러
    회사를 나열하는 디렉토리"가 아니라 "회사 하나만 다루는 단일 소개/
    역사 서술 페이지"는 기존 판단기준(디렉토리 URL패턴)에 안 걸려서
    통과된 사례가 실측 확인됨). 그래서 "제외 신호를 찾으면 제외"가
    아니라 "회사가 직접 운영한다는 확신이 서지 않으면 기본적으로
    제외"로 판단 방향 자체를 바꿈. 회사명-도메인 연관성(로마자 표기/
    이니셜/영문 상호 등 어떤 형태로든 겹치는 게 있는지)도 명시적
    판단기준에 포함시킴.

    2026-08-31 버그 수정: 원래 여기 "후보가 1개면 AI 호출 없이 그냥
    채택"하는 지름길이 있었는데, 이게 바로 위 판단기준 강화를 통째로
    무력화시키는 버그였음 - 네이버 검색이 블랙리스트 안 걸리는 후보를
    1개만 주면(실제로 'forum.38.co.kr'의 비상장주식 포럼 페이지가
    이 경로로 그대로 통과된 게 확인됨) AI 판단 자체가 한 번도 안 불림.
    그래서 후보 개수와 무관하게 항상 AI가 판단하도록 지름길을 제거함
    - AI 호출 1번 늘지만(비용 미미), 정확도가 이 함수의 존재 이유라
    맞바꿀 가치가 있음.
    """
    from langchain_openai import ChatOpenAI
    from langchain_core.prompts import PromptTemplate

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    item_context = f" (참고: 이 회사는 '{item_name}' 관련 업체로 추정됨)" if item_name else ""
    candidates_text = "\n".join(
        f"{i}. 제목: {c['title']} | URL: {c['link']} | 설명: {c['description']}"
        for i, c in enumerate(candidates)
    )
    prompt = PromptTemplate.from_template(
        "다음은 '{company_name}'{item_context}의 공식 홈페이지를 찾기 위한 "
        "검색결과입니다.\n\n{candidates}\n\n"
        "기본 원칙: '제외할 이유가 없으면 채택'이 아니라 '이 회사가 직접 "
        "운영한다는 확신이 서지 않으면 제외'입니다. 아래 3가지를 모두 "
        "따져보고, 셋 다 통과하는 후보만 고르세요.\n\n"
        "1) 운영주체 확인 - 이 페이지/사이트를 실제로 운영하는 게 그 "
        "회사 자신인가, 아니면 제3자인가? 제3자에는 다음이 전부 "
        "포함됩니다: 뉴스기사, 블로그, 후기글, 채용/구인 사이트, "
        "여러 회사를 나열하는 산업정보 디렉토리, **그리고 회사를 "
        "'하나만' 다루더라도 공공기관·지자체·연구원이 편찬한 백과사전/"
        "향토지/아카이브(예: 한국학중앙연구원 향토문화전자대전, 시군구"
        "디지털향토지), 상공회의소/협회/조합 소개 페이지, 공시정보 "
        "사이트** 등. 이런 곳은 회사명이 정확히 일치하고 내용도 그 "
        "회사에 대한 것이라도, 그 회사 '소유' 사이트가 아니므로 제외.\n"
        "2) 도메인-상호 연관성 - 도메인(또는 사이트명)이 회사명과 어떤 "
        "형태로든 연관이 있나요(로마자 표기, 영문 상호, 이니셜, 업종+"
        "회사이름 조합 등)? 전혀 무관한 이름(범용 플랫폼명, 지역/기관명, "
        "품목명만 있고 회사 고유명사가 전혀 없는 도메인 등)이면 강하게 "
        "의심하세요.\n"
        "3) URL 구조 - /companies/, /company/, /기업/company/ 같은 "
        "목록형 구조, /article/숫자ID·goods_view.php?goodsNo=... 같은 "
        "카탈로그/게시판 게시물 구조, maker_detail·maker_cd= 같은 "
        "제조사조회 파라미터가 있으면 제3자 플랫폼으로 판단.\n\n"
        "1)~3) 중 하나라도 제3자로 의심되면 그 후보는 제외하고, 남는 "
        "후보가 없으면 null을 반환하세요 - 억지로 하나를 고르지 마세요.\n\n"
        '반드시 이 JSON 형식으로만: {{"best_index": 숫자 또는 null}}'
    )
    result = (prompt | llm).invoke({
        "company_name": company_name, "item_context": item_context, "candidates": candidates_text,
    }).content
    parsed = _parse_json_response(result)

    picked = None
    if parsed and parsed.get("best_index") is not None:
        idx = parsed["best_index"]
        if 0 <= idx < len(candidates):
            picked = candidates[idx]

    try:
        from .case_logging import log_ai_decision
    except ImportError:
        from backend_logic2.nodes.supplier.tools.case_logging import log_ai_decision
    if picked:
        reason = f"'{company_name}' 홈페이지로 '{picked['link']}' 선택함"
    else:
        reason = f"'{company_name}' 후보 {len(candidates)}개 중 회사가 직접 운영한다고 확신할 만한 곳이 없어 전부 제외"
    log_ai_decision(case_id, "site_selection", reason=reason)

    return picked


def _find_official_site_simple(company_name, item_name=None, case_id=None):
    """
    네이버 검색결과 중 제외리스트(채용사이트 등)에 안 걸리는 후보들을
    모은 다음, 후보가 여럿이면 AI로 제일 그럴듯한 것을 선택.
    """
    query = f"{company_name} {item_name} 공식 홈페이지" if item_name else f"{company_name} 공식 홈페이지"
    candidates = []
    for item in _search_naver_web(query):
        link = item.get("link", "")
        if any(domain in link for domain in _EXCLUDED_CONTACT_DOMAINS):
            continue
        if _looks_like_directory_url(link):
            print(f"    [URL구조 제외] {link} (제3자 플랫폼 패턴)")
            continue
        candidates.append({"title": item.get("title"), "link": link, "description": item.get("description", "")})

    if not candidates:
        return None
    best = _pick_best_site_candidate(company_name, candidates, item_name=item_name, case_id=case_id)
    if not best:
        return None
    return {"url": best["link"], "content": best["description"]}


def retry_find_contact_page(name, item_name=None, case_id=None):
    """
    1단계에서 이메일/전화를 못 찾은 회사에 대해 "{회사명} {품목} 연락처"로
    재검색(1단계와 동일하게 URL구조 필터 + AI 사이트판단을 거침).

    2026-08-31 공용 함수로 추출: 원래 enrich_contacts_batch() 안에
    _fetch_retry_one()으로만 있던 로직인데, narajangteo_search_based_tool.
    enrich_candidates()(3소스 통합 경로)에는 이 재시도 단계 자체가 아예
    없어서("멀티탭" 테스트에서 15개 후보 중 7개만 홈페이지 확보, 그마저
    4개만 최종 통과 - 재시도 기회 없이 1차 실패=탈락이었던 게 원인 중
    하나로 확인됨) 최종 통과율이 낮게 나오는 문제가 있었음. 두 경로가
    동일한 재시도 로직을 쓰도록 공용 함수로 뺌.
    """
    retry_query = f"{name} {item_name} 연락처" if item_name else f"{name} 연락처"
    candidates = []
    for item in _search_naver_web(retry_query):
        link = item.get("link", "")
        if any(d in link for d in _EXCLUDED_CONTACT_DOMAINS):
            continue
        if _looks_like_directory_url(link):
            print(f"    [URL구조 제외] {link} (제3자 플랫폼 패턴)")
            continue
        candidates.append({"title": item.get("title"), "link": link, "description": item.get("description", "")})

    if not candidates:
        return None
    best = _pick_best_site_candidate(name, candidates, item_name=item_name, case_id=case_id)
    if not best:
        return None
    retry_text = _fetch_page_text(best["link"]) or best.get("description", "")
    return {"site_url": best["link"], "page_text": retry_text}


# ---------- 페이지 가져오기 (requests -> Jina Reader 폴백) ----------

def _fetch_page_text(url, max_chars=6000):
    """
    requests로 먼저 시도하고, 받아온 텍스트가 너무 짧으면(200자 미만 -
    JS 빈 껍데기로 의심) Jina Reader로 재시도. Jina 단독으로 전환했다가
    문제가 생겨서 되돌림 - 병렬처리(동시 8개)랑 Jina 무료요청제한(분당
    20회)이 겹쳐서 요청제한에 자주 걸렸을 가능성이 높음. 이 방식이면
    대부분의 페이지는 requests로 바로 끝나서 Jina 호출 자체가 크게
    줄어듦.
    """
    if not url:
        return ""
    if not url.startswith("http"):
        url = "http://" + url

    try:
        res = requests.get(url, timeout=10, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        })
        res.raise_for_status()

        # ⚠️ 인코딩 자동감지 보정: 한국 사이트는 EUC-KR을 쓰는 경우가
        # 흔한데, 서버가 charset을 헤더에 명시 안 하면 requests가
        # ISO-8859-1/ascii로 잘못 추측해서 한글이 깨짐. 이러면 페이지에
        # 이메일/전화가 있어도 텍스트가 깨져서 AI가 못 찾음 - apparent_encoding
        # (내용 기반 실제 인코딩 감지)으로 재보정.
        if res.encoding is None or res.encoding.lower() in ("iso-8859-1", "ascii"):
            res.encoding = res.apparent_encoding

        text = re.sub(r"<script.*?</script>", " ", res.text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<.*?>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
    except Exception as e:
        print(f"    [requests 실패] ({url}): {e}")
        text = ""

    if len(text) < 200:
        print(f"    페이지 내용 부족({len(text)}자), Jina Reader로 재시도: {url}")
        try:
            res = requests.get(f"https://r.jina.ai/{url}", timeout=15)
            res.raise_for_status()
            jina_text = res.text.strip()
            if jina_text and len(jina_text) > len(text):
                text = jina_text
        except Exception as e:
            print(f"    [Jina Reader 실패] ({url}): {e}")

    if len(text) <= max_chars:
        return text
    head_len = max_chars // 2
    tail_len = max_chars - head_len
    return f"[앞부분]\n{text[:head_len]}\n\n...(중략)...\n\n[뒷부분]\n{text[-tail_len:]}"


# ---------- 이메일/전화 추출 (배치 처리) ----------

def _extract_contacts_batch(companies, item_name=None, case_id=None):
    """
    여러 회사의 (name, page_text)를 한 번의 AI 호출로 같이 처리해서
    각각의 이메일/전화/관련성을 추출. companies: [{"name":..., "page_text":...}, ...]
    반환: {name: {"email":..., "phone":..., "is_relevant":...}, ...}
    """
    from langchain_openai import ChatOpenAI
    from langchain_core.prompts import PromptTemplate

    if not companies:
        return {}

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)

    item_instruction = (
        f"\n각 회사가 실제로 '{item_name}'을(를) 취급한다는 근거가 페이지에 "
        f"있는지도 참고로 is_relevant(true/false)로 판단하세요.\n" if item_name else ""
    )
    relevance_field = ', "is_relevant": true 또는 false' if item_name else ""

    # ⚠️ [:3000]으로 재차 자르지 않음 - _fetch_page_text가 이미 최대
    # 6000자로 관리하면서 "앞부분+...+뒷부분" 구조로 반환하는데(연락처가
    # 보통 페이지 하단에 있어서), 여기서 또 [:3000]으로 자르면 그 "뒷부분"
    # (진짜 연락처 있는 곳)이 통째로 잘려나가는 버그가 있었음.
    #
    # ⚠️ 2026-08-31 버그 수정: 아래 프롬프트가 "URL 구조(companies/,
    # maker_cd, goods_view 등)"를 판단 신호로 쓰라고 지시하면서 정작
    # URL 자체를 companies_text에 안 넣고 있었음 - AI가 볼 수 없는
    # 정보를 판단 기준으로 준 것과 같아서, 그 기준이 사실상 무의미했음
    # ('forum.38.co.kr'의 comkor_view.php?no=... 페이지가 이 상태로도
    # 통과된 게 실측 확인됨). 이제 회사별 헤더에 URL을 같이 넣음.
    companies_text = "\n\n".join(
        f"=== 회사 {i}: {c['name']} (출처 URL: {c.get('site_url') or c.get('url') or '알수없음'}) ===\n"
        f"{c.get('page_text') or ''}"
        for i, c in enumerate(companies)
    )

    prompt = PromptTemplate.from_template(
        "다음은 여러 회사의 홈페이지(로 추정되는) 페이지 내용입니다. 각 "
        "회사마다 대표 이메일과 전화번호를 찾아주세요.{item_instruction}\n"
        "{companies_text}\n\n"
        "규칙:\n"
        "- 전화번호와 팩스번호를 구분하세요. 확신 없으면 null.\n"
        "- 이메일/전화번호 일부가 별표(*)나 ●로 마스킹되어 있으면 "
        "(예: '02-3402-****') 그건 완전한 정보가 아니므로 null로 답하세요 "
        "- 이런 마스킹은 회사 자체 사이트가 아니라 제3자 플랫폼이 "
        "개인정보 보호 차원에서 일부만 노출하는 경우에 흔함.\n"
        "- 잡코리아,사람인 등 제3자 서비스 연락처는 채택하지 마세요.\n"
        "- ⚠️ 중요(기본원칙: 확신 없으면 제외): 이 페이지가 진짜 그 회사 "
        "소유의 페이지가 맞는지, 아니면 그 회사를 '소개하는' 제3자 "
        "콘텐츠일 뿐인지 판단하세요. 제3자 콘텐츠에는 뉴스기사, 블로그 "
        "후기, 디렉토리 사이트, B2B 거래플랫폼, SNS 채널뿐 아니라 "
        "**공공기관·지자체·연구원이 편찬한 백과사전/향토지/디지털아카이브"
        "(예: 향토문화전자대전 같은 지역 백과사전), 상공회의소/협회/조합 "
        "소개 페이지**도 포함됩니다 - 이런 곳은 회사 하나만 다루는 "
        "서술형 페이지라서 '디렉토리(여러 회사 나열)' 신호가 없어도, "
        "그 회사가 '직접 운영'하는 게 아니므로 여전히 제외 대상입니다. "
        "페이지에 이메일/전화가 있어도, 그게 그 회사 것이 아니라 그 "
        "페이지를 운영하는 다른 주체(언론사, 블로거, 플랫폼 운영자, "
        "편찬기관)의 연락처일 수 있습니다.\n"
        "  판단 신호: (a) 1인칭 표현('당사', '저희', '문의하기', "
        "'오시는길' 등 기업 자체 사이트 특유의 어투)이 있는지 vs 3인칭 "
        "서술('~는 …에 설립되었다', '연혁', '~라 한다' 같은 백과사전/"
        "기사체)인지, (b) 도메인이나 사이트명이 회사명과 어떤 형태로든 "
        "연관 있는지(로마자표기/영문상호/이니셜 등) - 전혀 무관하면 "
        "의심, (c) 여러 회사를 나열하는 목록형 URL구조(companies/, "
        "company/, 기업/ 등)나 제조사조회/카탈로그 시스템 느낌(maker_cd, "
        "goods_view 등 파라미터), 범용 플랫폼명(~머신, ~몰, B2B, ~정보 "
        "등). 이 중 하나라도 해당하면 그 페이지에서 뽑은 연락처는 "
        "채택하지 말고 null로 답하세요.\n"
        "- 각 회사를 위 '회사 N' 번호로 정확히 매칭해서 답하세요.\n\n"
        '반드시 이 JSON 형식으로만: {{"results": [{{"index": 번호, "email": "값 또는 null", '
        '"phone": "값 또는 null"' + relevance_field + '}}]}}'
    )

    result = (prompt | llm).invoke({
        "companies_text": companies_text, "item_instruction": item_instruction,
    }).content

    parsed = _parse_json_response(result)
    if not parsed:
        print(f"    [_extract_contacts_batch 파싱 실패], 전체 빈 값 처리")
        return {c["name"]: {"email": None, "phone": None} for c in companies}

    output = {}
    for r in parsed.get("results", []):
        idx = r.get("index")
        if idx is None or not (0 <= idx < len(companies)):
            continue
        name = companies[idx]["name"]
        email = r.get("email")
        phone = r.get("phone")

        # ⚠️ 마스킹된 연락처(별표 등) 무효화 - "02-3402-****" 같은 건
        # 제3자 플랫폼이 개인정보 보호로 일부만 보여주는 전형적 패턴이라,
        # 회사 자체 사이트에선 나올 이유가 없음. 완전한 정보가 아니므로
        # 애초에 못 찾은 것과 동일하게 처리.
        if email and ("*" in email or "●" in email):
            email = None
        if phone and ("*" in phone or "●" in phone):
            phone = None

        output[name] = {
            "email": email if email not in (None, "null", "") else None,
            "phone": phone if phone not in (None, "null", "") else None,
            "is_relevant": r.get("is_relevant"),
        }

    # 매칭 안 된 회사는 빈 값으로 채워둠 (파싱 누락 대비)
    for c in companies:
        if c["name"] not in output:
            output[c["name"]] = {"email": None, "phone": None}

    try:
        from .case_logging import log_ai_decision
    except ImportError:
        from backend_logic2.nodes.supplier.tools.case_logging import log_ai_decision
    for c in companies:
        contact = output[c["name"]]
        site_url = c.get("site_url") or c.get("url") or "알수없음"
        if contact.get("is_relevant") is False:
            reason = f"'{c['name']}' ({site_url}): '{item_name}' 취급 근거가 페이지에 없어 관련성 낮음으로 제외"
        elif contact.get("email") or contact.get("phone"):
            reason = f"'{c['name']}' ({site_url}): 이메일={contact.get('email')}, 전화={contact.get('phone')} 채택"
        else:
            reason = f"'{c['name']}' ({site_url}): 페이지에서 이메일/전화 둘 다 확인 못해 제외"
        log_ai_decision(case_id, "contact_extraction", reason=reason)

    return output


# ---------- 여러 회사 배치 처리 (메인 진입점) ----------

def _fetch_one_site_and_page(name, item_name=None, case_id=None):
    """회사 하나에 대해 사이트 찾고(AI없음) 페이지 텍스트 가져오기 (병렬실행용)"""
    site = _find_official_site_simple(name, item_name=item_name, case_id=case_id)
    if not site:
        return {"name": name, "page_text": "", "site_url": None}
    page_text = _fetch_page_text(site["url"]) or site.get("content", "")
    return {"name": name, "page_text": page_text, "site_url": site["url"]}


def enrich_contacts_batch(company_names, item_name=None, batch_size=5, target_count=None, case_id=None):
    """
    회사명 리스트를 받아서, 각각 네이버로 사이트 찾고(AI 없음) 페이지
    가져온 다음(requests->Jina 폴백, 병렬), batch_size개씩 묶어서 AI로
    연락처 추출. 이메일/전화 둘 다 없는 회사만 "{회사명} {품목} 연락처"로
    재시도(이것도 배치로 처리).

    target_count를 주면, 2단계 배치 처리 도중 목표개수만큼 확보되는 순간
    남은 배치는 건너뜀 (불필요한 AI호출 절감).

    반환: [{"name":..., "email":..., "phone":..., "site_url":...}, ...]
    """
    # 1단계: 회사별로 사이트 찾고 페이지 텍스트 가져오기 (AI 없음, 병렬)
    prepared = []
    with ThreadPoolExecutor(max_workers=min(len(company_names), 8) or 1) as executor:
        futures = [executor.submit(_fetch_one_site_and_page, name, item_name, case_id) for name in company_names]
        for f in as_completed(futures):
            prepared.append(f.result())

    # 2단계(병렬): batch_size개씩 묶은 배치들을 전부 동시에 AI 호출.
    # 이전엔 순서대로 돌면서 target_count 도달하면 나머지 배치를
    # 생략했는데, 병렬로 바꾸면서 그 조기종료는 못 씀 - latency 우선으로
    # 결정됨(AI호출 비용은 약간 늘 수 있음).
    all_contacts = {}
    batches = [prepared[i:i + batch_size] for i in range(0, len(prepared), batch_size)]
    with ThreadPoolExecutor(max_workers=len(batches) or 1) as executor:
        future_to_chunk = {}
        for chunk in batches:
            chunk_with_text = [c for c in chunk if c["page_text"]]
            if chunk_with_text:
                future = executor.submit(_extract_contacts_batch, chunk_with_text, item_name, case_id)
                future_to_chunk[future] = chunk
            else:
                for c in chunk:
                    all_contacts[c["name"]] = {"email": None, "phone": None}

        for future in as_completed(future_to_chunk):
            chunk = future_to_chunk[future]
            try:
                batch_result = future.result()
                all_contacts.update(batch_result)
            except Exception as e:
                print(f"    배치 처리 중 오류, 이 배치 건너뜀: {e}")
            for c in chunk:
                if c["name"] not in all_contacts:
                    all_contacts[c["name"]] = {"email": None, "phone": None}

    confirmed_count = sum(1 for c in all_contacts.values() if c.get("email") or c.get("phone"))

    # 3단계: 이메일도 전화도 둘 다 없는 회사만 재시도 (검색+가져오기 병렬, AI도 병렬)
    need_retry = [c for c in prepared if not all_contacts[c["name"]].get("email") and not all_contacts[c["name"]].get("phone")]
    if need_retry and (target_count is None or confirmed_count < target_count):
        print(f"    이메일/전화 둘 다 없는 {len(need_retry)}개 회사, '연락처' 재검색...")

        def _fetch_retry_one(c):
            """
            2026-08-31 버그 수정: 예전엔 블랙리스트만 거르고 네이버
            검색 1등 결과를 그냥 채택했음 - _find_official_site_simple()
            (1단계 검색)이 쓰는 _pick_best_site_candidate() AI 판단을
            여긴 아예 안 거쳤음. 그 결과 'forum.38.co.kr'(비상장주식
            정보 포럼, 명백한 제3자 사이트)이 재시도 경로로 그대로
            채택된 게 실측 확인됨. 1단계와 동일하게 후보를 모아서
            _pick_best_site_candidate로 판단하도록 통일함.

            2026-08-31 추가 리팩터: 실제 재시도 로직은 공용 함수
            retry_find_contact_page()로 뺐음 - narajangteo_search_based_tool.
            enrich_candidates()도 동일 로직이 필요해서 중복 방지.
            """
            result = retry_find_contact_page(c["name"], item_name=item_name, case_id=case_id)
            if not result:
                return None
            return {"name": c["name"], "page_text": result["page_text"], "site_url": result["site_url"]}

        retry_prepared = []
        with ThreadPoolExecutor(max_workers=min(len(need_retry), 8) or 1) as executor:
            futures = [executor.submit(_fetch_retry_one, c) for c in need_retry]
            for f in as_completed(futures):
                result = f.result()
                if result:
                    retry_prepared.append(result)

        retry_batches = [retry_prepared[i:i + batch_size] for i in range(0, len(retry_prepared), batch_size)]
        with ThreadPoolExecutor(max_workers=len(retry_batches) or 1) as executor:
            future_to_chunk = {}
            for chunk in retry_batches:
                chunk_with_text = [c for c in chunk if c["page_text"]]
                if chunk_with_text:
                    future = executor.submit(_extract_contacts_batch, chunk_with_text, item_name, case_id)
                    future_to_chunk[future] = chunk

            for future in as_completed(future_to_chunk):
                chunk = future_to_chunk[future]
                try:
                    retry_result = future.result()
                except Exception as e:
                    print(f"    재시도 배치 처리 중 오류, 건너뜀: {e}")
                    continue
                for name, contact in retry_result.items():
                    if contact.get("email") or contact.get("phone"):
                        all_contacts[name] = contact
                        for rp in retry_prepared:
                            if rp["name"] == name:
                                for p in prepared:
                                    if p["name"] == name:
                                        p["site_url"] = rp["site_url"]

    # 최종 결과 조립
    final = []
    for c in prepared:
        contact = all_contacts.get(c["name"], {})
        final.append({
            "name": c["name"],
            "email": contact.get("email"),
            "phone": contact.get("phone"),
            "site_url": c["site_url"],
        })
    return final


# ---------- 단건 처리 (하위호환용, 여전히 개별 호출이 필요한 곳에서 사용) ----------

def enrich_contact_by_company_name(vendor, item_name=None, case_id=None):
    """
    회사 1개만 처리하는 단건 버전 (배치가 필요없는 소규모 호출용).
    내부적으로 enrich_contacts_batch를 1개짜리 리스트로 호출함.
    """
    if vendor.get("email") and vendor.get("phone"):
        return vendor

    name = vendor.get("name", "")
    if vendor.get("site_url"):
        page_text = _fetch_page_text(vendor["site_url"])
        contacts = _extract_contacts_batch(
            [{"name": name, "page_text": page_text}], item_name=item_name, case_id=case_id
        )
        contact = contacts.get(name, {})
        result = dict(vendor)
        if not result.get("email") and contact.get("email"):
            result["email"] = contact["email"]
        if not result.get("phone") and contact.get("phone"):
            result["phone"] = contact["phone"]
        result["is_relevant"] = contact.get("is_relevant")
        return result

    results = enrich_contacts_batch([name], item_name=item_name, batch_size=1, case_id=case_id)
    if results:
        r = results[0]
        return {**vendor, "name": r["name"], "email": r["email"], "phone": r["phone"], "site_url": r["site_url"]}
    return vendor


if __name__ == "__main__":
    print("=== 배치처리 테스트 ===")
    names_input = input("회사명 여러 개 입력 (쉼표로 구분): ").strip()
    company_names = [n.strip() for n in names_input.split(",") if n.strip()]
    item_name = input("관련 품목명 (선택, 그냥 엔터 가능): ").strip() or None

    results = enrich_contacts_batch(company_names, item_name=item_name)

    print(f"\n=== 결과 ({len(results)}건) ===")
    for r in results:
        print(f"\n{r['name']}")
        print(f"  이메일: {r.get('email') or '(없음)'}")
        print(f"  전화: {r.get('phone') or '(없음)'}")
        print(f"  사이트: {r.get('site_url') or '(없음)'}")