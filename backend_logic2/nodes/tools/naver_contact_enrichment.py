"""
nodes/naver_contact_enrichment.py - 회사명으로 네이버 검색해서 공식
홈페이지를 찾고, 거기서 이메일/전화를 AI로 추출하는 단독 모듈.

resolve_suppliers.py에서 이 부분만 독립시킴 - resolve_suppliers.py는
RAG 검색 로직도 같이 갖고 있어서 gather_and_verify_suppliers.py가
그 파일 전체에 의존하면 혼란스러워짐 (RAG 로직이 rag_vendor_search.py랑
중복되는 것처럼 보임). 이 파일은 순수하게 "회사명 -> 연락처" 기능만 함.

폴더 구조: backend_logic2/erp_client.py, backend_logic2/nodes/이 파일

실행: python nodes/naver_contact_enrichment.py
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
import json
import requests
from dotenv import load_dotenv

load_dotenv()

_EXCLUDED_CONTACT_DOMAINS = [
    "jobkorea.co.kr", "saramin.co.kr", "wanted.co.kr", "catch.co.kr",
    "incruit.com", "albamon.com", "job.co.kr", "linkedin.com",
]


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


def _pick_best_site_candidate(company_name, candidates, item_name=None):
    """
    후보가 여럿이면, title+description을 AI한테 보여주고 "진짜 이 회사
    공식홈페이지 같은 것"을 고르게 함. 그냥 "제외리스트에 안 걸리는 첫
    번째"보다 훨씬 정확함 - 검색결과 자체의 텍스트를 실제 판단근거로 씀.
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
        "검색결과 목록입니다.\n\n{candidates}\n\n"
        "이 중에서 '{company_name}'의 진짜 공식 홈페이지일 가능성이 가장 높은 "
        "것을 하나 고르세요. 회사명이 정확히 일치하고, 뉴스기사·블로그·후기글이 "
        "아니라 그 회사가 직접 운영하는 사이트로 보이는 걸 우선하세요. "
        "확실한 게 하나도 없으면 null을 반환하세요.\n\n"
        '반드시 이 JSON 형식으로만 답하세요: {{"best_index": 숫자 또는 null}}'
    )

    result = (prompt | llm).invoke({
        "company_name": company_name, "item_context": item_context, "candidates": candidates_text,
    }).content

    try:
        cleaned = result.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        idx = json.loads(cleaned).get("best_index")
        if idx is not None and 0 <= idx < len(candidates):
            return candidates[idx]
    except Exception:
        pass
    return None


def _find_official_site(company_name, item_name=None):
    """
    공식 홈페이지 후보들(채용사이트 등 제외)을 다 모은 다음, title+
    description을 AI로 비교해서 제일 그럴듯한 것 하나를 선택.
    """
    candidates = []
    for item in _search_naver_web(f"{company_name} 공식 홈페이지"):
        link = item.get("link", "")
        if any(domain in link for domain in _EXCLUDED_CONTACT_DOMAINS):
            continue
        candidates.append({"title": item.get("title"), "link": link, "description": item.get("description", "")})

    if not candidates:
        return None
    if len(candidates) == 1:
        return {"url": candidates[0]["link"], "content": candidates[0]["description"]}

    best = _pick_best_site_candidate(company_name, candidates, item_name=item_name)
    if not best:
        return None
    return {"url": best["link"], "content": best["description"]}


def _fetch_page_text(url, max_chars=6000):
    """
    페이지 직접 요청해서 태그 벗겨낸 텍스트 반환.
    연락처(이메일,전화)는 보통 페이지 맨 아래에 있는데, <footer> 태그를
    안 쓰고 <div class="footer"> 같은 다른 형태로 만든 사이트가 많아서
    태그 기반 추출은 놓치는 경우가 있었음(실측 확인됨). 그래서 태그를
    찾는 대신, 그냥 "앞부분 절반 + 뒷부분 절반"을 통째로 같이 줘서,
    어떤 마크업을 쓰든 페이지 맨 끝부분이 항상 포함되게 함.
    """
    if not url:
        return ""
    try:
        res = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        res.raise_for_status()
        raw_html = res.text

        text = re.sub(r"<script.*?</script>", " ", raw_html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<.*?>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()

        if len(text) <= max_chars:
            return text

        head_len = max_chars // 2
        tail_len = max_chars - head_len
        return (
            f"[페이지 앞부분]\n{text[:head_len]}\n\n"
            f"...(중략)...\n\n"
            f"[페이지 뒷부분 - 연락처가 보통 여기 있음]\n{text[-tail_len:]}"
        )
    except Exception as e:
        print(f"    [_fetch_page_text] 페이지 가져오기 실패 ({url}): {e}")
        return ""


def _extract_contact_from_page(company_name, source_url, page_text, item_name=None):
    """
    페이지 내용에서 회사명(clean_name),이메일,전화번호 AI로 추출.
    item_name이 주어지면, 이 회사가 실제로 그 품목을 취급하는지도 엄격하게
    같이 검증함 - 근거 없으면 관련없음 처리하고 연락처도 null로 버림
    (Tavily가 뽑은 회사명 중 무관한 게 섞여있을 수 있어서, 여기서 최종
    관문 역할을 하게 함).
    """
    from langchain_openai import ChatOpenAI
    from langchain_core.prompts import PromptTemplate

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)

    if item_name:
        item_instruction = (
            f"\n이 페이지 내용을 보고, 이 회사가 실제로 '{item_name}'을(를) "
            f"제조·판매·유통한다는 **명확한 근거**가 있는지 엄격하게 판단하세요.\n"
            f"- 페이지에 그 품목/유사 품목·관련 업종이 명시적으로 언급되어 있으면 관련있음\n"
            f"- 언급이 전혀 없거나, '~할 수도 있음' 수준으로만 추정 가능한 정도면 관련없음\n"
            f"- 관련없다고 판단되면, is_relevant를 false로 하고 email/phone도 "
            f"null로 답하세요 (연락처를 알아도 일부러 버리는 것 — 무관한 업체한테 "
            f"연락하지 않기 위함)\n"
        )
        relevance_field = ', "is_relevant": true 또는 false, "relevance_reason": "짧은 판단근거"'
    else:
        item_instruction = ""
        relevance_field = ""

    prompt = PromptTemplate.from_template(
        "당신은 정보 추출 전문가입니다. 아래는 '{company_name}'의 공식 홈페이지로 "
        "추정되는 페이지({url})의 내용입니다.\n"
        "이 회사의 (1)정확한 회사명, (2)대표 이메일, (3)대표 전화번호를 찾아주세요.\n"
        "{item_instruction}\n"
        "[페이지 내용]\n{text}\n\n"
        "규칙:\n"
        "- 이 페이지가 진짜 이 회사의 것이 맞는지 스스로 확인하고, 아니면 셋 다 null.\n"
        "- clean_name은 검색결과 제목을 그대로 베끼지 말고 실제 상호명만 간결하게.\n"
        "- 잡코리아,사람인 등 제3자 서비스 연락처는 절대 채택하지 마세요.\n"
        "- 전화번호와 팩스번호를 구분하세요. 확신 없으면 null.\n"
        '- JSON만 답하세요: {{"clean_name": "값 또는 null", "email": "값 또는 null", '
        '"phone": "값 또는 null"' + relevance_field + '}}'
    )
    result_text = (prompt | llm).invoke({
        "company_name": company_name, "url": source_url, "text": page_text[:6000],
        "item_instruction": item_instruction,
    }).content

    try:
        cleaned = result_text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        parsed = json.loads(cleaned)

        is_relevant = parsed.get("is_relevant", True)  # item_name 안 줬으면 검증 자체를 안 하니 기본 True
        if item_name and not is_relevant:
            print(f"    [관련성 낮음, 참고만] '{company_name}': {parsed.get('relevance_reason')}")
            # ⚠️ 여기서 연락처를 버리지 않음 — 최종 검증단계(gather_and_verify_
            # suppliers.py의 _verify_and_standardize)에서 전체 맥락 보고 한 번 더
            # 판단하게 맡김. 여기서 너무 일찍/좁게 판단해서 버리면 "연락처를
            # 못 찾는" 문제가 커짐 (실제로 찾았는데 버려지는 셈이라).

        email = parsed.get("email") if parsed.get("email") not in (None, "null", "") else None
        if email and any(f"@{d}" in email for d in _EXCLUDED_CONTACT_DOMAINS):
            email = None
        return {
            "clean_name": parsed.get("clean_name") if parsed.get("clean_name") not in (None, "null", "") else None,
            "email": email,
            "phone": parsed.get("phone") if parsed.get("phone") not in (None, "null", "") else None,
            "is_relevant": is_relevant,
            "relevance_reason": parsed.get("relevance_reason"),
        }
    except Exception:
        return {"clean_name": None, "email": None, "phone": None, "is_relevant": None, "relevance_reason": None}


def enrich_contact_by_company_name(vendor, item_name=None):
    """
    회사명으로 네이버 검색 -> 공식 홈페이지 추정 -> 이메일/전화 AI 추출.
    vendor: {"name": ..., "email": ..., "phone": ..., "site_url": ...} 형태
    dict (email/phone/site_url은 없어도 됨).
    ⚠️ item_name을 주면 관련성도 같이 판단하지만, 이메일/전화 자체를 그
    이유로 버리진 않음 (최종 판단은 gather_and_verify_suppliers.py의
    검증단계에서 함) - is_relevant/relevance_reason 필드로 참고정보만 넘김.
    ⚠️ 홈페이지(메인페이지)에 연락처가 없으면, "{회사명} 연락처"로 한 번 더
    검색해서 보완 시도함 - 많은 회사가 메인이 아니라 "회사소개"·"오시는길"
    같은 별도 페이지에 연락처를 두는 경우가 많아서.
    ⚠️ vendor에 site_url이 이미 있으면(예: 공장등록정보처럼 검증된 출처에서
    미리 확보된 경우), 네이버로 홈페이지를 다시 찾지 않고 그 URL을 바로
    씀 — 이미 아는 걸 또 검색하는 낭비를 막기 위함.
    반환: 보강된 vendor dict (site_url, is_relevant, relevance_reason 필드 추가/유지됨)
    """
    if vendor.get("email") and vendor.get("phone"):
        return vendor

    name = vendor.get("name", "")

    if vendor.get("site_url"):
        site = {"url": vendor["site_url"], "content": ""}
    else:
        site = _find_official_site(name, item_name=item_name)
        if not site:
            return vendor

    page_text = _fetch_page_text(site["url"]) or site.get("content", "")
    extracted = _extract_contact_from_page(name, site["url"], page_text, item_name=item_name)

    # 메인페이지에서 이메일을 못 찾았으면, "{회사명} 연락처"로 한 번 더 시도
    # (연락처가 별도 페이지에 있는 경우가 많아서)
    if not extracted.get("email") and not vendor.get("site_url"):
        print(f"    '{name}' 메인페이지에서 연락처 못 찾음, '{name} 연락처'로 재시도...")
        contact_results = _search_naver_web(f"{name} 연락처")
        for item in contact_results:
            link = item.get("link", "")
            if any(domain in link for domain in _EXCLUDED_CONTACT_DOMAINS):
                continue
            contact_page_text = _fetch_page_text(link) or item.get("description", "")
            retry = _extract_contact_from_page(name, link, contact_page_text, item_name=item_name)
            if retry.get("email"):
                print(f"      -> 재시도로 이메일 확보: {retry['email']}")
                extracted = retry
                site = {"url": link}
                break

    result = dict(vendor)
    result["is_relevant"] = extracted.get("is_relevant")
    result["relevance_reason"] = extracted.get("relevance_reason")

    if extracted.get("clean_name"):
        result["name"] = extracted["clean_name"]
    if not result.get("email") and extracted.get("email"):
        result["email"] = extracted["email"]
    if not result.get("phone") and extracted.get("phone"):
        result["phone"] = extracted["phone"]
    result["site_url"] = site["url"]

    return result


if __name__ == "__main__":
    company_name = input("검색할 회사명 입력: ").strip()
    result = enrich_contact_by_company_name({"name": company_name})

    print(f"\n=== '{company_name}' 연락처 검색 결과 ===")
    print(f"이름: {result.get('name')}")
    print(f"이메일: {result.get('email') or '(없음)'}")
    print(f"전화: {result.get('phone') or '(없음)'}")
    print(f"사이트: {result.get('site_url') or '(못 찾음)'}")