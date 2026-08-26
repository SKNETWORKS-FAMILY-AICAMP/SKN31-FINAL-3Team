"""
nodes/public_data_api_search.py - 도구1: 정부 조달데이터 API로 검증된 공급사 찾기

1단계: 조달내역(getDlvrReqDtlInfoList) - 실제 이 품목을 납품한 회사 + 사업자번호 확보
   (ShoppingMallPrdctInfoService, 30일 단위로만 조회 가능함이 확인됨)
2단계: 조달업체 기본정보(getPrcrmntCorpBasicInfo02) - 그 사업자번호로 검증된
   전화번호 + 홈페이지주소 확보 (UsrInfoService02, 파라미터명 문서로 확인됨)
3단계: 검증된 홈페이지에서 이메일만 AI로 추출
   (정부가 URL을 이미 검증해줬으니 "이게 진짜 회사 맞나" 재확인 없이 바로 진행)

주의: 2단계의 ServiceKey 대소문자, inqryDiv 값은 최초 문서 기준 추정이라 첫
실행에서 에러나면 DEBUG 로그 보고 바로 수정 가능하게 만들어둠.

.env에 DATA_GO_KR_SERVICE_KEY 필요 (procurement_history_search.py와 동일 키).

폴더 구조: backend_logic2/erp_client.py, backend_logic2/nodes/이 파일

실행: python nodes/public_data_api_search.py
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
import json
from datetime import datetime, timedelta
from urllib.parse import unquote
import requests
from dotenv import load_dotenv

load_dotenv()

SHOPPING_BASE_URL = "https://apis.data.go.kr/1230000/at/ShoppingMallPrdctInfoService"
USRINFO_BASE_URL = "http://apis.data.go.kr/1230000/ao/UsrInfoService02"


def _parse_items(data):
    """나라장터 API들의 공통 응답 구조(response.body.items)를 리스트로 정규화"""
    body = data.get("response", {}).get("body", {})
    items = body.get("items", [])
    if isinstance(items, dict):
        items = items.get("item", [items]) if "item" in items else [items]
    if isinstance(items, dict):
        items = [items]
    return items


def search_procurement_history(item_name, num_rows=5, days_back=30):
    """1단계: 실제 납품기록 조회. 확인된 제약: 날짜범위는 최대 약 30일까지만 가능."""
    service_key = unquote(os.environ["DATA_GO_KR_SERVICE_KEY"])
    end_dt = datetime.now()
    bgn_dt = end_dt - timedelta(days=days_back)

    params = {
        "serviceKey": service_key,
        "pageNo": 1,
        "numOfRows": num_rows,
        "type": "json",
        "inqryDiv": "1",
        "prdctClsfcNoNm": item_name,
        "inqryBgnDate": bgn_dt.strftime("%Y%m%d"),
        "inqryEndDate": end_dt.strftime("%Y%m%d"),
    }
    headers = {"Accept": "*/*;q=0.9"}
    res = requests.get(f"{SHOPPING_BASE_URL}/getDlvrReqDtlInfoList", params=params, headers=headers, timeout=10)

    if res.status_code != 200:
        print(f"  [1단계실패] 상태코드 {res.status_code}: {res.text[:300]}")
        return []

    try:
        data = res.json()
    except ValueError:
        print(f"  [1단계실패] JSON 파싱 실패: {res.text[:300]}")
        return []

    if "response" not in data:
        print(f"  [1단계실패] 예상과 다른 응답 구조: {data}")
        return []

    return _parse_items(data)


def get_corp_basic_info(bizno):
    """2단계: 조달업체 기본정보로 검증된 연락처(전화, 홈페이지) 확보"""
    service_key = unquote(os.environ["DATA_GO_KR_SERVICE_KEY"])
    params = {
        "ServiceKey": service_key,
        "pageNo": 1,
        "numOfRows": 1,
        "type": "json",
        "inqryDiv": "3",  # 사업자등록번호 기준검색
        "bizno": bizno,
    }
    headers = {"Accept": "*/*;q=0.9"}
    res = requests.get(f"{USRINFO_BASE_URL}/getPrcrmntCorpBasicInfo02", params=params, headers=headers, timeout=10)

    if res.status_code != 200:
        print(f"    [2단계실패] '{bizno}' 상태코드 {res.status_code}: {res.text[:300]}")
        return None

    try:
        data = res.json()
    except ValueError:
        print(f"    [2단계실패] '{bizno}' JSON 파싱 실패: {res.text[:300]}")
        return None

    if "response" not in data:
        print(f"    [2단계실패] '{bizno}' 예상과 다른 응답 구조: {data}")
        return None

    items = _parse_items(data)
    return items[0] if items else None


def _fetch_page_text(url, max_chars=6000):
    """검증된 홈페이지에서 본문 텍스트만 뽑음"""
    if not url:
        return ""
    if not url.startswith("http"):
        url = "http://" + url
    try:
        res = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        res.raise_for_status()
        text = re.sub(r"<script.*?</script>", " ", res.text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<.*?>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars]
    except Exception as e:
        print(f"    [3단계경고] 페이지 가져오기 실패 ({url}): {e}")
        return ""


def _extract_email_from_verified_page(company_name, page_text):
    """
    3단계: 검증된 페이지에서 이메일만 AI로 추출.
    URL 자체가 정부에 의해 이미 검증됐으니, "이게 진짜 회사 홈페이지 맞나"
    재확인 없이 바로 이메일 추출에만 집중함 (resolve_suppliers.py의
    _extract_contact_from_page보다 훨씬 단순해짐 - 검증 부담이 줄어서).
    """
    if not page_text:
        return None

    from langchain_openai import ChatOpenAI
    from langchain_core.prompts import PromptTemplate

    llm = ChatOpenAI(model="gpt-4o-mini", temperature=0)
    prompt = PromptTemplate.from_template(
        "다음은 '{company_name}'의 정부에 등록된 검증된 공식 홈페이지 내용입니다. "
        "대표 이메일 주소를 찾아주세요.\n\n"
        "[페이지 내용]\n{text}\n\n"
        "확신 없으면 null로 답하세요.\n"
        '반드시 이 JSON 형식으로만 답하세요: {{"email": "값 또는 null"}}'
    )
    result = (prompt | llm).invoke({"company_name": company_name, "text": page_text[:6000]}).content

    try:
        cleaned = result.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        parsed = json.loads(cleaned)
        email = parsed.get("email")
        return email if email not in (None, "null", "") else None
    except Exception:
        return None


def _process_single_record(record):
    """
    조달내역 한 건을 상세정보(전화,홈페이지,이메일)까지 채워서 반환.
    (병렬실행용 — 다른 record들과 완전히 독립적인 작업)
    반환: (record_dict 또는 None)
    """
    corp_nm = record.get("corpNm")
    bizno = record.get("cntrctCorpBizno")
    if not bizno:
        return None

    print(f"  '{corp_nm}'({bizno}) 검증된 연락처 조회 중...")
    basic_info = get_corp_basic_info(bizno)

    if not basic_info:
        print(f"    -> 기본정보 없음, 스킵")
        return None

    phone = basic_info.get("telNo")
    homepage = basic_info.get("hmpgAdrs")
    address = basic_info.get("adrs")
    print(f"  '{corp_nm}' -> 전화: {phone} | 홈페이지: {homepage}")

    email = None
    if homepage:
        page_text = _fetch_page_text(homepage)
        email = _extract_email_from_verified_page(corp_nm, page_text)
        print(f"  '{corp_nm}' -> 이메일: {email}")

    return {
        "name": corp_nm,
        "business_no": bizno,
        "phone": phone,
        "homepage": homepage,
        "address": address,
        "email": email,
        "procurement_record": {
            "item": record.get("prdctClsfcNoNm"),
            "spec": record.get("prdctIdntNoNm"),
            "date": record.get("dlvrReqRcptDate"),
            "buyer": record.get("dminsttNm"),
        },
    }


def find_verified_suppliers(item_name, max_results=3, days_back=30):
    """
    1단계->2단계->3단계 전체 파이프라인 실행.
    ⚠️ 2,3단계(회사별 상세조회+이메일추출)는 서로 독립적이라 병렬로 돌림
    (호출 수는 그대로, 체감속도만 개선).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    print(f"\n[1단계] '{item_name}' 실제 납품기록 조회 중... (최근 {days_back}일)")
    history = search_procurement_history(item_name, num_rows=max_results, days_back=days_back)
    print(f"  -> {len(history)}건 발견")

    seen_bizno = set()
    unique_records = []
    for record in history:
        bizno = record.get("cntrctCorpBizno")
        if not bizno or bizno in seen_bizno:
            continue
        seen_bizno.add(bizno)
        unique_records.append(record)

    print(f"\n[2,3단계] {len(unique_records)}개 업체 상세정보 병렬 조회 중...")
    results = []
    with ThreadPoolExecutor(max_workers=min(len(unique_records), 8) or 1) as executor:
        futures = [executor.submit(_process_single_record, record) for record in unique_records]
        for future in as_completed(futures):
            result = future.result()
            if result:
                results.append(result)
            if len(results) >= max_results:
                break

    return results[:max_results]
    return results


if __name__ == "__main__":
    item_name = input("검색할 품목명 입력: ").strip()
    max_results_input = input("몇 개 업체까지 볼까요? (그냥 엔터시 3개): ").strip()
    max_results = int(max_results_input) if max_results_input else 3

    results = find_verified_suppliers(item_name, max_results=max_results)

    print(f"\n{'='*50}")
    print(f"=== '{item_name}' 검증된 공급사 (최대 {max_results}건) ===")
    print(f"{'='*50}")

    if not results:
        print("결과 없음")

    for r in results:
        print(f"\n{r['name']} ({r['business_no']})")
        print(f"  전화: {r['phone'] or '(없음)'}")
        print(f"  홈페이지: {r['homepage'] or '(없음)'}")
        print(f"  이메일: {r['email'] or '(추출 실패/없음)'}")
        print(f"  주소: {r['address']}")
        rec = r["procurement_record"]
        print(f"  실제 납품기록: {rec['item']} / {rec['spec']}")
        print(f"    ({rec['date']}, {rec['buyer']} 납품)")