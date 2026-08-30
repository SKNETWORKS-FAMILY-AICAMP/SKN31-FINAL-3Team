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
]


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

def _pick_best_site_candidate(company_name, candidates, item_name=None):
    """
    후보가 여럿이면, title+description을 AI한테 보여주고 "진짜 이 회사
    공식홈페이지 같은 것"을 고르게 함. "그냥 1등 채택"은 Tavily가 뽑은
    회사명이 정확한 법인명이 아니거나 흔한 이름일 때, 동명이인 회사나
    무관한 페이지를 잘못 채택하는 문제가 실제로 확인되어 다시 복원함.
    """
    if len(candidates) == 1:
        return candidates[0]

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
        "진짜 공식 홈페이지일 가능성이 가장 높은 것을 하나 고르세요. "
        "회사명이 정확히 일치하고, 뉴스기사·블로그·후기글이 아니라 그 "
        "회사가 직접 운영하는 사이트로 보이는 걸 우선하세요.\n\n"
        "⚠️ 특히 주의: '제3자 산업정보/거래 디렉토리 플랫폼'을 회사 자체 "
        "사이트로 착각하지 마세요. 이런 플랫폼은 URL 구조에 특징적인 "
        "패턴이 있습니다 - 예를 들어:\n"
        "  - /companies/{{회사명}}, /company/{{회사명}}, /기업/company/... "
        "처럼 경로에 '여러 회사를 나열하는 목록'을 암시하는 구조\n"
        "  - /article/숫자ID, /goods_view.php?goodsNo=... 처럼 큰 카탈로그/"
        "게시판 시스템 안의 게시물 하나로 보이는 구조\n"
        "  - maker_detail, maker_cd= 같은 '제조사 조회 시스템' 느낌의 파라미터\n"
        "  - 사이트명 자체가 여러 업체를 모아놓은 플랫폼처럼 보임(예: "
        "'~머신', '~디렉토리', '~정보', 'B2B', '~몰' 등 범용 플랫폼명이고 "
        "회사명과 무관)\n"
        "이런 패턴이 보이면, 회사명이 그 페이지에 언급되어 있어도 그 회사 "
        "'자체' 사이트가 아니라 그 회사를 '소개하는' 제3자 페이지일 "
        "가능성이 높으니 낮은 우선순위로 두거나 제외하세요.\n\n"
        "확실한 게 없으면 null.\n\n"
        '반드시 이 JSON 형식으로만: {{"best_index": 숫자 또는 null}}'
    )
    result = (prompt | llm).invoke({
        "company_name": company_name, "item_context": item_context, "candidates": candidates_text,
    }).content
    parsed = _parse_json_response(result)
    if parsed and parsed.get("best_index") is not None:
        idx = parsed["best_index"]
        if 0 <= idx < len(candidates):
            return candidates[idx]
    return None


def _find_official_site_simple(company_name, item_name=None):
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
        candidates.append({"title": item.get("title"), "link": link, "description": item.get("description", "")})

    if not candidates:
        return None
    best = _pick_best_site_candidate(company_name, candidates, item_name=item_name)
    if not best:
        return None
    return {"url": best["link"], "content": best["description"]}


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

def _extract_contacts_batch(companies, item_name=None):
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
    companies_text = "\n\n".join(
        f"=== 회사 {i}: {c['name']} ===\n{c.get('page_text') or ''}"
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
        "- ⚠️ 중요: 이 페이지가 진짜 그 회사 소유의 페이지가 맞는지, 아니면 "
        "그 회사를 소개하는 제3자 콘텐츠(뉴스기사, 블로그 후기, 디렉토리 "
        "사이트, B2B 거래플랫폼, SNS 채널 등)일 뿐인지 판단하세요. 페이지에 "
        "이메일/전화가 있어도, 그게 그 회사 것이 아니라 그 페이지를 운영하는 "
        "다른 주체(예: 언론사, 블로거, 플랫폼 운영자)의 연락처일 수 있습니다.\n"
        "  디렉토리/거래플랫폼 페이지의 특징: 여러 회사를 나열하는 목록형 "
        "URL구조(companies/, company/, 기업/ 등), 제조사조회/카탈로그 "
        "시스템 느낌(maker_cd, goods_view 등 파라미터), 사이트명이 특정 "
        "회사명이 아니라 범용 플랫폼명(~머신, ~몰, B2B, ~정보 등)인 경우. "
        "이런 특징이 보이면 그 페이지에서 뽑은 연락처는 채택하지 말고 null로 "
        "답하세요 - 이메일 도메인이 회사명과 전혀 무관해 보이는 경우도 "
        "마찬가지로 의심하세요.\n"
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

    return output


# ---------- 여러 회사 배치 처리 (메인 진입점) ----------

def _fetch_one_site_and_page(name, item_name=None):
    """회사 하나에 대해 사이트 찾고(AI없음) 페이지 텍스트 가져오기 (병렬실행용)"""
    site = _find_official_site_simple(name, item_name=item_name)
    if not site:
        return {"name": name, "page_text": "", "site_url": None}
    page_text = _fetch_page_text(site["url"]) or site.get("content", "")
    return {"name": name, "page_text": page_text, "site_url": site["url"]}


def enrich_contacts_batch(company_names, item_name=None, batch_size=5, target_count=None):
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
        futures = [executor.submit(_fetch_one_site_and_page, name, item_name) for name in company_names]
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
                future = executor.submit(_extract_contacts_batch, chunk_with_text, item_name)
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
            retry_query = f"{c['name']} {item_name} 연락처" if item_name else f"{c['name']} 연락처"
            retry_results = _search_naver_web(retry_query)
            for item in retry_results:
                if any(d in item.get("link", "") for d in _EXCLUDED_CONTACT_DOMAINS):
                    continue
                retry_text = _fetch_page_text(item.get("link", "")) or item.get("description", "")
                return {"name": c["name"], "page_text": retry_text, "site_url": item.get("link")}
            return None

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
                    future = executor.submit(_extract_contacts_batch, chunk_with_text, item_name)
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

def enrich_contact_by_company_name(vendor, item_name=None):
    """
    회사 1개만 처리하는 단건 버전 (배치가 필요없는 소규모 호출용).
    내부적으로 enrich_contacts_batch를 1개짜리 리스트로 호출함.
    """
    if vendor.get("email") and vendor.get("phone"):
        return vendor

    name = vendor.get("name", "")
    if vendor.get("site_url"):
        page_text = _fetch_page_text(vendor["site_url"])
        contacts = _extract_contacts_batch([{"name": name, "page_text": page_text}], item_name=item_name)
        contact = contacts.get(name, {})
        result = dict(vendor)
        if not result.get("email") and contact.get("email"):
            result["email"] = contact["email"]
        if not result.get("phone") and contact.get("phone"):
            result["phone"] = contact["phone"]
        result["is_relevant"] = contact.get("is_relevant")
        return result

    results = enrich_contacts_batch([name], item_name=item_name, batch_size=1)
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