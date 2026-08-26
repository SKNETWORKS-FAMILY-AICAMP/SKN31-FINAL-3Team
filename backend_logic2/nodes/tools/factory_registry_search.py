"""
nodes/factory_registry_search.py - 한국산업단지공단 공장등록정보로
"이미 아는 회사명"의 상세정보(전화,홈페이지,대표자,생산품 등)를 확인하는
보강용 도구.

⚠️ 확정된 사실 (직접 테스트로 검증됨):
  - mainProductCn(생산품명) 단독검색: 안 됨 (11번 에러, NO_MANDATORY_
    REQUEST_PARAMETERS_ERROR) — "품목 → 공장 찾기"(신규탐색) 용도로는
    못 씀, 애초에 그런 설계가 아니었음.
  - cmpnyNm(회사명) 단독검색: 됨 (resultCode 00 확인됨) — "이미 아는
    회사명 → 상세정보"(보강) 용도로는 정상 작동.
  - 응답이 type=json 요청해도 XML로 옴 — 이 오퍼레이션은 JSON 파라미터를
    무시하는 것으로 보임. 그래서 이 파일은 XML을 파싱함.

즉 이 모듈은 나라장터 "조달업체 기본정보" API와 같은 역할(보강용) —
gather_and_verify_suppliers.py에서 신규탐색 도구로는 안 쓰고, 이미 찾은
후보의 연락처를 한 번 더 검증/보강하고 싶을 때 선택적으로 쓰면 됨.

오퍼레이션: getFctryPrdctnService_v2 (회사명 기준 검색)
Base URL: https://apis.data.go.kr/B550624/fctryRegistInfo (실제 curl로 확인됨)

응답 필드: fctryManageNo(공장관리번호), cmpnyNm(회사명), rnAdres(도로명주소),
  rprsntvNm(대표자명), cmpnyTelno(전화번호), cmpnyFxnum(팩스번호),
  allEmplyCo(종업원수), frstFctryRegistDe(최초등록일), indutyNm(업종명),
  mainProductCn(주생산품), hmpadr(홈페이지주소), irsttNm(산업단지명)
  ※ 이메일 필드는 없음 — naver_contact_enrichment.py로 보강 필요.

.env에 DATA_GO_KR_SERVICE_KEY 필요.

폴더 구조: backend_logic2/erp_client.py, backend_logic2/nodes/이 파일

실행: python nodes/factory_registry_search.py
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from urllib.parse import unquote
import xml.etree.ElementTree as ET
import requests
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "https://apis.data.go.kr/B550624/fctryRegistInfo"


def _parse_xml_items(xml_text):
    """
    이 API는 type=json을 무시하고 항상 XML로 응답하는 것으로 확인됨.
    <response><body><items><item>...</item>...</items></body></response>
    구조를 파싱해서 dict 리스트로 변환.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"  [DEBUG] XML 파싱 실패: {e}")
        return [], None, None

    result_code_el = root.find(".//resultCode")
    result_msg_el = root.find(".//resultMsg")
    result_code = result_code_el.text if result_code_el is not None else None
    result_msg = result_msg_el.text if result_msg_el is not None else None

    items = []
    for item_el in root.findall(".//items/item"):
        item = {child.tag: child.text for child in item_el}
        items.append(item)

    return items, result_code, result_msg


def get_factory_by_company_name(company_name, num_rows=5):
    """
    회사명으로 공장 상세정보(전화,홈페이지,대표자,생산품 등) 조회.
    확인된 사실: 이 파라미터 단독으로 정상 작동함.

    반환: item dict 리스트 (없으면 빈 리스트)
    """
    service_key = unquote(os.environ["DATA_GO_KR_SERVICE_KEY"])

    params = {
        "serviceKey": service_key,
        "pageNo": 1,
        "numOfRows": num_rows,
        "cmpnyNm": company_name,
    }
    headers = {"Accept": "*/*;q=0.9"}

    res = requests.get(f"{BASE_URL}/getFctryPrdctnService_v2", params=params, headers=headers, timeout=10)
    print(f"  [DEBUG] 상태코드: {res.status_code}")

    if res.status_code != 200:
        print(f"  [DEBUG] 응답: {res.text[:500]}")
        return []

    items, result_code, result_msg = _parse_xml_items(res.text)

    if result_code != "00":
        print(f"  [DEBUG] API 응답: {result_code} - {result_msg}")
        return []

    return items


def factory_to_candidate(factory_item):
    """공장 정보를 gather_and_verify_suppliers.py 공통 후보 형식으로 변환.
    ⚠️ 이메일은 항상 None - naver_contact_enrichment.py로 보강 필요."""
    return {
        "name": factory_item.get("cmpnyNm"),
        "email": None,
        "phone": factory_item.get("cmpnyTelno"),
        "site_url": factory_item.get("hmpadr"),
        "source": "factory_registry",
        "extra": {
            "address": factory_item.get("rnAdres"),
            "representative": factory_item.get("rprsntvNm"),
            "industry": factory_item.get("indutyNm"),
            "main_product": factory_item.get("mainProductCn"),
            "employee_count": factory_item.get("allEmplyCo"),
        },
    }


if __name__ == "__main__":
    company_name = input("검색할 회사명 입력: ").strip()
    results = get_factory_by_company_name(company_name)

    print(f"\n=== '{company_name}' 공장등록정보 ({len(results)}건) ===")
    if not results:
        print("결과 없음 (등록 안 된 회사이거나 이름 표기가 다를 수 있음)")

    for item in results:
        c = factory_to_candidate(item)
        print(f"\n{c['name']}")
        print(f"  전화: {c['phone']} | 홈페이지: {c['site_url']}")
        print(f"  주소: {c['extra']['address']} | 대표자: {c['extra']['representative']}")
        print(f"  업종: {c['extra']['industry']} | 주생산품: {c['extra']['main_product']}")