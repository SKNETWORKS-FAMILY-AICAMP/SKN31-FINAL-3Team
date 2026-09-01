"""
tools/narajangteo_search_based_tool.py - 나라장터쇼핑몰 API 기반 공급사
검색 도구. naver_contact_enrichment.py(같은 tools 폴더)의 페이지읽기+
연락처추출 로직을 공유해서 씀.

ShoppingMallPrdctInfoService 9개 오퍼레이션을 "각 오퍼레이션 원본 설명에
실제로 뭐라고 쓰여있는지" 근거로 재분류해서 캐스케이드 검색. 이름이
비슷하다고 같은 패턴일 거라 짐작하지 않고, 설명 문구에 명시적 근거가
있는 것만 채택함.

분류 근거:
  검증됨(실사용):
    getDlvrReqDtlInfoList - 설명: "검색조건을...물품분류명,세부품명,
      품목명을 통해" - 실제 테스트로 작동 확인됨. 패턴: prdctClsfcNoNm
      + inqryBgnDate/inqryEndDate, 30일 제한 있음.
    getMASCntrctPrdctInfoList - 실제 테스트로 작동 확인됨. 패턴:
      prdctClsfcNoNm + rgstDtBgnDt/rgstDtEndDt.
    getUcntrctPrdctInfoList, getThptyUcntrctPrdctInfoList - MAS와 설명
      문장이 동일해서 같은 패턴 추론했고, 실제 테스트(철근 등)로 검증됨.

  제외됨(근거로 배제):
    getDlvrReqInfoList - 설명에 품목명이 검색조건으로 없음, 실제로
      무관한 결과(엘지전자가 석고보드 검색에 나오는 등) 확인되어 제외.
    getSpcifyPrdlstPrcureTotList - "집계"통계라 회사기록 없을 가능성.
    getShoppingMallPrdctInfoList, getSpcifyPrdlstPrcureInfoList,
      getVntrPrdctOrderDealDtlsInfoList - 검증 안 된 채 필드명만
      추측해서 썼던 것들, "철제" 검색시 매수기관 혼입 확인되어 제외.

핵심 최적화(latency, 정확도는 안 건드림):
  ① 캐스케이드 4개 오퍼레이션을 순차 호출 -> 병렬 호출로 전환.
     서로 완전히 독립적인 API 호출이라 병렬화해도 정확도 영향 없음.
     (트레이드오프: "목표개수 도달하면 나머지 오퍼레이션 생략"하던
     조기종료는 못 씀 - 근데 정부API가 무료라 비용영향 없이 순수
     latency 이득만 있음)
  ② 상세검증 2단계(배치AI)도 순차 -> 병렬로 전환.
     (트레이드오프: 배치 단위 조기종료를 잃음 - 이건 AI호출이라
     비용이 늘 수 있음, 사용자가 latency를 우선하기로 결정함)

DB 캐시 (2026-08-31 추가, 같은 날 수정):
  search_all_with_detail()이 procurement.narajangteo_company_info
  (전처리된 나라장터 업체 DB, 58,181건)를 라이브 API 캐스케이드
  (search_all)와 항상 동시에(병렬) 조회함(search_db_cache). 처음엔
  "DB로 개수 채우면 라이브 API 생략"으로 짰었는데, 검증 안 된 DB
  후보가 검증된 라이브 API보다 우선권을 가져가는 문제가 실측으로
  확인돼서(아래 참고) 제거함 - 지금은 둘 다 항상 돌려서 합치고,
  중복이면 라이브 API 쪽을 신뢰도 우선으로 채택함. 이 테이블엔
  연락처가 없어서 상세검증(홈페이지+연락처 추출) 단계는 라이브 API
  후보와 완전히 동일하게 거침. procurement_db 모듈을 못 찾거나 DB
  조회가 실패하면 이 소스만 조용히 건너뛰고 라이브 API 결과만 씀
  (전체 검색이 죽지 않게).

  DB 캐시 관련성 한계(2026-08-31 '목재' 테스트로 확인): 이 테이블은
  (bizno, main_item_no, main_biz_type) 복합키라 main_item_name이
  "그 회사 주력사업"이 아니라 "그 회사가 나라장터에 등록한 품목 중
  하나"일 뿐임. substring 매칭만으론 명백히 무관한 회사가 섞여
  나올 수 있어서(예: 건강기능식품 회사가 '목재' 검색에 나옴),
  search_db_cache의 ORDER BY에 main_biz_type 일치/제조사 여부를
  가중치로 넣었지만 WHERE 조건 자체를 좁히진 않았음 - 최종 관련성
  판단은 여전히 뒤쪽 AI 검증(2단계, is_relevant)에 의존함.

  정부DB 데이터 오염 방어(2026-08-31 추가): getPrcrmntCorpBasicInfo02의
  hmpgAdrs 필드에 홈페이지 대신 이메일이 들어있는 케이스가 실측
  확인돼서(_looks_like_valid_homepage), 스킴 없이 '@'가 있으면
  홈페이지로 취급하지 않고 제외하도록 방어 추가함.

3소스 통합 개편(2026-08-31, 같은 날 재수정): "나라장터(API+DB캐시)가
목표개수를 못 채우면 그제서야 Tavily"라는 순차 폴백 구조 자체를
버렸음 - '오일씰' 같은 산업부품 카테고리는 나라장터가 API+DB캐시
둘 다 구조적으로 0건일 수 있어서(실측 확인), "1순위가 항상 채워줄
것"이라는 전제가 성립하지 않았음. 그래서 이제:
  - search_all()/search_db_cache()는 각각 후보 "이름"만 모으는
    수집 함수로만 씀(이전과 동일한 시그니처, 변경 없음).
  - 후보 수집(나라장터API + DB캐시 + Tavily 3개 소스)은
    supplier_search.py가 처음부터 병렬로 돌려서 하나로 합침.
  - enrich_candidates()(예전 search_all_with_detail을 대체)가 출처
    상관없이 병합된 후보 전체를 동일한 파이프라인으로 처리 - 나라장터
    출처면 정부DB 홈페이지 우선 시도, 아니면(Tavily 등) 바로 네이버
    검색. _fetch_basic_and_page()가 candidate["source"]로 분기함.
  - 이 파일을 단독 실행(__main__)하면 나라장터API+DB캐시만 모아서
    테스트 가능(Tavily 제외) - 3소스 다 보려면 supplier_search.py 실행.

.env 필요: DATA_GO_KR_SERVICE_KEY, NEXTERP_DATABASE_URL(DB 캐시용,
procurement_db 모듈 경유)

실행: python search_all_narajangteo.py (또는 supplier_search.py로
3소스 통합 실행)
"""

import os
from urllib.parse import unquote
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from dotenv import load_dotenv

try:
    from .naver_contact_enrichment import _fetch_page_text, _find_official_site_simple
except ImportError:  # tools 폴더에서 직접 실행할 때
    from backend_logic2.nodes.supplier.tools.naver_contact_enrichment import (
        _fetch_page_text,
        _find_official_site_simple,
    )

load_dotenv()

BASE_URL = "https://apis.data.go.kr/1230000/at/ShoppingMallPrdctInfoService"
USRINFO_BASE_URL = "http://apis.data.go.kr/1230000/ao/UsrInfoService02"


def _parse_usrinfo_items(data):
    """UsrInfoService02 공통 응답구조(response.body.items) 정규화"""
    body = data.get("response", {}).get("body", {})
    items = body.get("items", [])
    if isinstance(items, dict):
        items = items.get("item", [items]) if "item" in items else [items]
    if isinstance(items, dict):
        items = [items]
    return items


def get_corp_basic_info(bizno):
    """조달업체 기본정보로 검증된 연락처(전화, 홈페이지) 확보 - 사업자번호 기준"""
    service_key = unquote(os.environ["DATA_GO_KR_SERVICE_KEY"])
    params = {
        "ServiceKey": service_key, "pageNo": 1, "numOfRows": 1, "type": "json",
        "inqryDiv": "3", "bizno": bizno,
    }
    headers = {"Accept": "*/*;q=0.9"}
    res = requests.get(f"{USRINFO_BASE_URL}/getPrcrmntCorpBasicInfo02", params=params, headers=headers, timeout=10)

    if res.status_code != 200:
        print(f"    [기본정보실패] '{bizno}' 상태코드 {res.status_code}: {res.text[:300]}")
        return None
    try:
        data = res.json()
    except ValueError:
        return None
    if "response" not in data:
        return None
    items = _parse_usrinfo_items(data)
    return items[0] if items else None


def get_corp_basic_info_by_name(corp_name):
    """사업자번호를 모를 때, 회사명으로 조달업체 기본정보 검색 (폴백용)"""
    service_key = unquote(os.environ["DATA_GO_KR_SERVICE_KEY"])
    params = {
        "ServiceKey": service_key, "pageNo": 1, "numOfRows": 1, "type": "json",
        "inqryDiv": "1", "corpNm": corp_name,
    }
    headers = {"Accept": "*/*;q=0.9"}
    res = requests.get(f"{USRINFO_BASE_URL}/getPrcrmntCorpBasicInfo02", params=params, headers=headers, timeout=10)

    if res.status_code != 200:
        print(f"    [회사명검색실패] '{corp_name}' 상태코드 {res.status_code}")
        return None
    try:
        data = res.json()
    except ValueError:
        return None
    if "response" not in data:
        return None
    items = _parse_usrinfo_items(data)
    return items[0] if items else None


def _get_service_key():
    return unquote(os.environ["DATA_GO_KR_SERVICE_KEY"])


def _fetch(operation, params, timeout=20):
    headers = {"Accept": "*/*;q=0.9"}
    try:
        res = requests.get(f"{BASE_URL}/{operation}", params=params, headers=headers, timeout=timeout)
    except requests.exceptions.Timeout:
        return None, "타임아웃"
    except Exception as e:
        return None, f"요청 실패: {e}"

    if res.status_code != 200:
        return None, f"상태코드 {res.status_code}: {res.text[:300]}"

    try:
        data = res.json()
    except ValueError:
        return None, f"JSON 파싱 실패: {res.text[:300]}"

    header = data.get("response", {}).get("header") or data.get("header") or {}
    result_code = header.get("resultCode")
    if result_code not in ("00", None):
        return None, f"resultCode={result_code}, {header.get('resultMsg')}"

    body = data.get("response", {}).get("body") or data.get("body") or {}
    items = body.get("items", [])
    if isinstance(items, dict):
        items = items.get("item", [items]) if "item" in items else [items]
    return items, None


def _date_range(days_back):
    end_dt = datetime.now()
    bgn_dt = end_dt - timedelta(days=days_back)
    return bgn_dt.strftime("%Y%m%d"), end_dt.strftime("%Y%m%d")


def _validate_relevance(item_name, raw_item):
    """
    안전장치: 파라미터가 조용히 무시돼서 엉뚱한 결과가 섞이는 걸 막기
    위해, item_name(핵심 키워드)이 결과의 관련 텍스트필드 어딘가에
    실제로 등장하는지 확인. getDlvrReqInfoList 오작동(엘지전자가
    석고보드 검색에 나온 것) 같은 사고를 다른 "추정" 오퍼레이션에서도
    막기 위함.
    """
    text_fields = [
        raw_item.get("prdctClsfcNoNm", ""), raw_item.get("prdctSpecNm", ""),
        raw_item.get("dtilPrdctClsfcNoNm", ""), raw_item.get("prdctMidclsfcNm", ""),
    ]
    combined = " ".join(str(f) for f in text_fields)
    return item_name in combined or any(part in combined for part in item_name.split() if len(part) >= 2)


def _try_dlvrreq_style(operation, item_name, days_back=30):
    """검증된 패턴: prdctClsfcNoNm + inqryBgnDate/inqryEndDate"""
    bgn, end = _date_range(days_back)
    params = {
        "serviceKey": _get_service_key(), "pageNo": 1, "numOfRows": 20, "type": "json",
        "inqryDiv": "1", "prdctClsfcNoNm": item_name,
        "inqryBgnDate": bgn, "inqryEndDate": end,
    }
    items, error = _fetch(operation, params)
    if error:
        print(f"    [{operation}] 실패: {error}")
        return []
    results = []
    for it in items:
        if not _validate_relevance(item_name, it):
            continue
        name = it.get("corpNm")
        if name:
            results.append({"name": name, "raw": it, "operation": operation, "source": "narajangteo_api"})
    return results


def _try_cntrct_style(operation, item_name, days_back=365):
    """검증된 패턴: prdctClsfcNoNm + rgstDtBgnDt/rgstDtEndDt"""
    bgn, end = _date_range(days_back)
    params = {
        "serviceKey": _get_service_key(), "pageNo": 1, "numOfRows": 20, "type": "json",
        "inqryDiv": "1", "prdctClsfcNoNm": item_name,
        "rgstDtBgnDt": bgn, "rgstDtEndDt": end,
    }
    items, error = _fetch(operation, params)
    if error:
        print(f"    [{operation}] 실패: {error}")
        return []
    results = []
    for it in items:
        if not _validate_relevance(item_name, it):
            continue
        name = it.get("cntrctCorpNm")
        if name:
            results.append({"name": name, "raw": it, "operation": operation, "source": "narajangteo_api"})
    return results


# ---------- 캐스케이드 ----------

CASCADE = [
    ("getDlvrReqDtlInfoList", "납품요구상세(검증됨)",
     lambda name: _try_dlvrreq_style("getDlvrReqDtlInfoList", name, 30)),
    ("getMASCntrctPrdctInfoList", "MAS품목정보(검증됨)",
     lambda name: _try_cntrct_style("getMASCntrctPrdctInfoList", name, 365)),
    ("getUcntrctPrdctInfoList", "일반단가계약(실제 테스트로 검증됨: 철근 등)",
     lambda name: _try_cntrct_style("getUcntrctPrdctInfoList", name, 365)),
    ("getThptyUcntrctPrdctInfoList", "제3자단가계약(실제 테스트로 검증됨: 철근 등)",
     lambda name: _try_cntrct_style("getThptyUcntrctPrdctInfoList", name, 365)),
]


def _looks_like_valid_homepage(value):
    """
    정부DB(getPrcrmntCorpBasicInfo02)의 hmpgAdrs 필드에 홈페이지 대신
    이메일 주소가 잘못 들어간 케이스를 걸러냄. 2026-08-31 '목재' 테스트
    실행에서 hmpgAdrs='namutek@naver.com'이 그대로 홈페이지로 취급돼서
    'http://namutek@naver.com'으로 fetch를 시도하다 실패하는 게 실측
    확인됨. 스킴(http/https) 없이 '@'가 있으면 이메일로 판단해서 제외.
    """
    if not value:
        return False
    value = value.strip()
    if not value:
        return False
    if "@" in value and not value.lower().startswith(("http://", "https://")):
        return False
    return True


def _fetch_basic_and_page(candidate, item_name=None, case_id=None):
    """
    통합 enrichment 1단계(병렬, AI 호출 없음*): 후보의 홈페이지를 확보하고
    페이지 텍스트까지 가져옴. 이 단계는 회사마다 다른 API/URL을 호출해야
    해서 원래도 배치가 안 되는 부분이라 그대로 병렬화만 함.

    2026-08-31 통합구조 개편: 나라장터API/DB캐시 후보뿐 아니라 Tavily
    후보까지 이 함수 하나로 처리하도록 일반화함(예전엔 나라장터 쪽
    전용이었고 Tavily는 naver_contact_enrichment.enrich_contacts_batch가
    따로 처리). candidate["source"]로 분기:
      - "narajangteo_api"/"db_cache": 정부DB(getPrcrmntCorpBasicInfo02)
        홈페이지를 먼저 시도(이미 검증된 사업자번호가 있어서 빠르고
        신뢰도 높음) - 없거나 형식이 이상하면(2단계 아래) 네이버로 폴백.
      - 그 외(예: "tavily"): 사업자번호가 없는 게 당연하므로 정부DB
        조회를 아예 생략하고 바로 네이버 검색으로 감 - Tavily로 찾은
        회사가 나라장터 등록 업체라는 보장이 없어서, 회사명만으로 정부
        조달업체 DB를 조회하는 건 대부분 헛수고(느리기만 함)였음.

    네이버 폴백은 naver_contact_enrichment.py의 _find_official_site_simple()
    사용(블랙리스트+AI 사이트선택 로직 공유). 후보 여럿이면 AI 호출 1번이
    추가로 들어감(*그래서 "AI 호출 없음"이 항상 성립하진 않음) - 다만
    이 단계 자체가 이미 병렬 실행 중이라, 전체 latency 증가는 병렬
    배치 하나 안에서 일부 스레드가 조금 더 오래 걸리는 정도로 제한됨.
    """
    name = candidate["name"]
    source = candidate.get("source", "")
    raw = candidate.get("raw") or {}
    print(f"\n  [{name}] (출처: {source or '알수없음'}) 홈페이지 확보 시도 중...")

    homepage = None
    gov_phone = None

    if source in ("narajangteo_api", "db_cache"):
        bizno = raw.get("cntrctCorpBizno") or raw.get("dminsttBizno")
        if bizno:
            print(f"    사업자번호({bizno})로 정부DB 조회")
            basic_info = get_corp_basic_info(bizno)
        else:
            print(f"    사업자번호 없음, 회사명으로 정부DB 조회")
            basic_info = get_corp_basic_info_by_name(name)

        if basic_info:
            gov_phone = basic_info.get("telNo")
            candidate_homepage = basic_info.get("hmpgAdrs")
            if _looks_like_valid_homepage(candidate_homepage):
                homepage = candidate_homepage
            else:
                reason = "이메일이 홈페이지 자리에 들어있음" if candidate_homepage else "정부DB에 홈페이지 없음"
                print(f"    -> {reason}")
        else:
            print(f"    -> 정부DB 기본정보 자체가 없음")
    else:
        print(f"    정부DB 조회 생략(나라장터 출처가 아님), 바로 네이버 검색")

    if not homepage:
        print(f"    네이버로 홈페이지 검색 시도")
        site = _find_official_site_simple(name, item_name=item_name, case_id=case_id)
        if not site:
            print(f"    -> 네이버 검색으로도 못 찾음, 제외")
            return None
        homepage = site["url"]
        print(f"    -> 네이버 검색으로 홈페이지 확보: {homepage}, 페이지 가져오는 중...")
    else:
        print(f"    -> 정부DB 홈페이지 확보: {homepage}, 페이지 가져오는 중...")

    page_text = _fetch_page_text(homepage)
    print(f"    -> {len(page_text)}자 확보")

    if not page_text:
        print(f"    -> 페이지 내용을 못 가져옴, 제외")
        return None

    return {
        "candidate": candidate, "homepage": homepage,
        "page_text": page_text, "gov_phone": gov_phone,
    }


def search_db_cache(item_name, target_count=10):
    """
    0순위: procurement.narajangteo_company_info (전처리된 나라장터 업체
    DB, 58,181건)에서 먼저 찾아본다. 라이브 API 캐스케이드보다 훨씬
    빠르고, bizno가 이미 있어서 회사명 검색 폴백 없이 바로
    get_corp_basic_info(bizno)로 넘어갈 수 있음.

    주의: 이 테이블엔 연락처(이메일/전화/홈페이지) 컬럼이 없음 - 여기서
    나온 후보도 enrich_candidates()의 1단계(_fetch_basic_and_page)를
    그대로 거쳐야 연락처가 채워짐. 즉 이 함수는 후보 "이름"만 모으는
    수집 함수이고, 뒤쪽 검증/연락처 로직은 라이브 API·Tavily 후보와
    완전히 동일하게 탄다.

    biz_status_code='01'을 "계속사업자(활성)"로 가정하고 필터링함 -
    실제 코드값 의미는 확인 필요(db_schema.md 또는 팀 확인).

    관련성 경고: narajangteo_company_info는 (bizno, main_item_no,
    main_biz_type) 복합키라, main_item_name은 "그 회사의 주력 사업"이
    아니라 "그 회사가 나라장터에 등록한 품목 중 하나"일 뿐임. 2026-08-31
    '목재' 테스트에서 명백히 무관해 보이는 회사(예: 건강기능식품 회사)가
    섞여 나오는 게 실측 확인됨. 아래 ORDER BY에서 main_biz_type도 같이
    매칭되는 걸 우선순위로 올리고 제조사(is_manufacturer='Y')를
    우대하지만, WHERE 조건 자체는 여전히 main_item_name 단순 substring
    매칭이라 이 문제가 완전히 해결되진 않음 - 최종 관련성 판단은
    뒤쪽 AI 검증(2단계, is_relevant)에 여전히 의존한다.
    """
    try:
        from procurement_db import get_connection
    except ImportError:
        print("    [DB캐시] procurement_db 모듈을 못 찾음, 이 소스는 건너뜀")
        return []

    try:
        with get_connection(autocommit=True) as connection:
            rows = connection.execute(
                """
                SELECT bizno, company_name, main_item_name, main_biz_type
                FROM procurement.narajangteo_company_info
                WHERE main_item_name ILIKE %(pattern)s
                  AND biz_status_code = '01'
                ORDER BY
                    (main_biz_type ILIKE %(pattern)s) DESC,
                    (is_manufacturer = 'Y') DESC,
                    reg_date DESC
                LIMIT %(limit)s
                """,
                {"pattern": f"%{item_name}%", "limit": target_count},
            ).fetchall()
    except Exception as e:
        print(f"    [DB캐시] 조회 실패, 이 소스는 건너뜀: {e}")
        return []

    results = []
    for row in rows:
        results.append({
            "name": row["company_name"],
            "raw": {
                "cntrctCorpBizno": row["bizno"],
                "prdctClsfcNoNm": row["main_item_name"] or "",
                "prdctSpecNm": row["main_item_name"] or "",
            },
            "operation": "db_cache(narajangteo_company_info)",
            "source": "db_cache",
        })
    return results


def enrich_candidates(candidates, item_name=None, target_count=10, batch_size=5, case_id=None):
    """
    출처(나라장터API/DB캐시/Tavily 등) 상관없이 후보 목록을 받아서
    동일한 2단계로 처리:

    1단계(병렬): 후보별 홈페이지 확보(_fetch_basic_and_page - 나라장터
      출처면 정부DB 우선, 아니면 바로 네이버) + 페이지 텍스트 가져오기
      (requests -> Jina Reader 폴백, AI 호출 없음)
    2단계(배치, 병렬): batch_size개씩 묶은 배치들을 전부 동시에 AI 호출
      (이메일/전화/관련성 판단). 배치 단위 조기종료는 안 씀 - latency
      우선(AI호출 비용은 약간 늘 수 있음).

    2026-08-31 구조 개편 히스토리:
      - 원래는 나라장터(API+DB캐시)만 처리하는 함수(search_all_with_detail)
        였고, Tavily는 완전히 별개 함수(web_search_based_tool.py)가
        처리했으며 "나라장터가 목표개수를 못 채우면 그제서야 Tavily"라는
        순차 폴백 구조였음.
      - "DB캐시로 목표개수 채우면 라이브 API 생략"했다가, 실제 '목재'
        테스트에서 검증 안 된 DB캐시가 검증된 라이브 API보다 우선권을
        가져가는 문제가 확인되어 "항상 둘 다 병렬 실행+dedup"으로 바꿈.
      - 근데 '오일씰' 같은 산업부품 카테고리는 나라장터(API+DB캐시)가
        구조적으로 0건이 나올 수 있음이 실측 확인되어("1순위가 항상
        채워줄 것"이라는 전제 자체가 틀림), 나라장터API/DB캐시/Tavily
        3개 소스를 처음부터 병렬로 돌려서 후보명을 모으고(수집은
        supplier_search.py가 담당), 여기서는 출처 상관없이 하나의
        enrichment 파이프라인으로 통일함. 이 함수는 이미 merge+dedup된
        candidates 리스트를 받는 게 전제.
    """
    try:
        from .naver_contact_enrichment import _extract_contacts_batch, retry_find_contact_page
    except ImportError:  # tools 폴더에서 직접 실행할 때
        from backend_logic2.nodes.supplier.tools.naver_contact_enrichment import (
            _extract_contacts_batch,
            retry_find_contact_page,
        )

    def _run_batches(items):
        """items: [{"name","page_text","site_url"}] -> batch_size개씩 묶어 AI 배치 호출(병렬), {name: contact} 반환"""
        batches = [items[i:i + batch_size] for i in range(0, len(items), batch_size)]
        out = {}
        if not batches:
            return out
        with ThreadPoolExecutor(max_workers=len(batches)) as executor:
            future_to_chunk = {}
            for batch_idx, chunk in enumerate(batches):
                print(f"  배치 {batch_idx + 1} 제출: {[c['name'] for c in chunk]}")
                future = executor.submit(_extract_contacts_batch, chunk, item_name, case_id)
                future_to_chunk[future] = chunk
            for future in as_completed(future_to_chunk):
                chunk = future_to_chunk[future]
                try:
                    out.update(future.result())
                except Exception as e:
                    print(f"    배치 처리 중 오류, 이 배치 건너뜀: {e}")
        return out

    print(f"\n[통합 enrichment - 1단계] {len(candidates)}개 후보 홈페이지+페이지 확보 중... (병렬)")
    prepared = []
    failed_stage1 = []  # 홈페이지 자체를 못 찾은 후보 (이름만 남음, 3단계 재시도 대상)
    with ThreadPoolExecutor(max_workers=min(len(candidates), 8) or 1) as executor:
        future_to_candidate = {executor.submit(_fetch_basic_and_page, c, item_name, case_id): c for c in candidates}
        for f in as_completed(future_to_candidate):
            c = future_to_candidate[f]
            result = f.result()
            if result is not None:
                prepared.append(result)
            else:
                failed_stage1.append(c)

    print(
        f"\n[통합 enrichment - 1단계 완료] {len(candidates)}개 중 {len(prepared)}개 홈페이지 확보됨"
        f" ({len(failed_stage1)}개는 3단계 재시도 대상)"
    )

    print(f"\n[통합 enrichment - 2단계] {len(prepared)}개 배치로 묶어 AI 이메일/전화/관련성 검증 중... (병렬)")
    chunk_input = [
        {"name": p["candidate"]["name"], "page_text": p["page_text"], "site_url": p["homepage"]}
        for p in prepared
    ]
    contacts_stage2 = _run_batches(chunk_input)

    enriched = []
    need_retry = list(failed_stage1)  # 1단계 실패 + 2단계에서 연락처 못 찾은 후보 = 3단계 재시도 대상
    for p in prepared:
        name = p["candidate"]["name"]
        contact = contacts_stage2.get(name, {})

        if contact.get("is_relevant") is False:
            print(f"    '{name}': 관련성 낮음, 제외")
            continue

        email = contact.get("email")
        phone = contact.get("phone") or p["gov_phone"]  # AI추출 우선, 없으면 정부DB값 폴백

        if not email and not phone:
            print(f"    '{name}': 1차 시도에서 이메일/전화 둘 다 못 찾음, 3단계 재시도 대상으로 이동")
            need_retry.append(p["candidate"])
            continue

        source = p["candidate"].get("source", "?")
        print(f"    '{name}': 채택 (이메일={email}, 전화={phone}, 출처={source})")
        candidate = dict(p["candidate"])
        candidate["email"] = email
        candidate["phone"] = phone
        candidate["site_url"] = p["homepage"]
        enriched.append(candidate)

    # 3단계: 2026-08-31 추가. 1단계에서 홈페이지를 못 찾았거나 2단계에서
    # 연락처를 못 찾은 후보를 "{회사명} {품목} 연락처"로 재검색.
    # naver_contact_enrichment.enrich_contacts_batch()(Tavily 하위호환
    # 경로)엔 원래 있던 재시도 단계인데, 3소스 통합 리팩터 때 이
    # enrich_candidates()엔 빠뜨렸던 게 실제 '멀티탭' 테스트(15개 후보
    # 중 7개만 홈페이지 확보, 4개만 최종 통과)로 확인돼서 추가함.
    if need_retry and len(enriched) < target_count:
        print(f"\n[통합 enrichment - 3단계] {len(need_retry)}개 재검색 중... (병렬)")
        retry_prepared = []
        with ThreadPoolExecutor(max_workers=min(len(need_retry), 8) or 1) as executor:
            future_to_candidate = {
                executor.submit(retry_find_contact_page, c["name"], item_name, case_id): c for c in need_retry
            }
            for f in as_completed(future_to_candidate):
                c = future_to_candidate[f]
                result = f.result()
                if result and result.get("page_text"):
                    retry_prepared.append((c, result))

        print(f"[통합 enrichment - 3단계] {len(need_retry)}개 중 {len(retry_prepared)}개 재검색으로 홈페이지 확보됨")

        retry_chunk_input = [
            {"name": c["name"], "page_text": r["page_text"], "site_url": r["site_url"]}
            for c, r in retry_prepared
        ]
        retry_contacts = _run_batches(retry_chunk_input)

        for c, r in retry_prepared:
            name = c["name"]
            contact = retry_contacts.get(name, {})
            if contact.get("is_relevant") is False:
                print(f"    (재시도) '{name}': 관련성 낮음, 제외")
                continue
            email = contact.get("email")
            phone = contact.get("phone")
            if not email and not phone:
                print(f"    (재시도) '{name}': 여전히 이메일/전화 못 찾음, 최종 제외")
                continue
            source = c.get("source", "?")
            print(f"    (재시도) '{name}': 채택 (이메일={email}, 전화={phone}, 출처={source})")
            candidate = dict(c)
            candidate["email"] = email
            candidate["phone"] = phone
            candidate["site_url"] = r["site_url"]
            enriched.append(candidate)

    print(f"\n[통합 enrichment 완료] {len(candidates)}개 중 {len(enriched)}개 최종 통과")
    return enriched[:target_count]


def search_all(item_name, target_count=10):
    """
    캐스케이드 4개 오퍼레이션을 병렬로 동시 호출. 서로 완전히 독립적인
    API 호출이라 병렬화해도 정확도 영향 없음 (트레이드오프: "목표개수
    도달하면 나머지 오퍼레이션 생략"하던 조기종료는 못 씀 - 정부API가
    무료라 비용영향은 없음, 순수 latency 이득).
    """
    print(f"\n{'#' * 50}")
    print(f"# [나라장터 캐스케이드 시작] '{item_name}', 목표 {target_count}개")
    print(f"{'#' * 50}")

    results_by_operation = {}
    with ThreadPoolExecutor(max_workers=len(CASCADE)) as executor:
        future_to_meta = {
            executor.submit(search_fn, item_name): (operation, label)
            for operation, label, search_fn in CASCADE
        }
        for future in as_completed(future_to_meta):
            operation, label = future_to_meta[future]
            print(f"\n[{label}] 처리 중...")
            try:
                results = future.result()
            except Exception as e:
                print(f"  예외 발생, 건너뜀: {e}")
                results = []
            results_by_operation[operation] = results
            print(f"  -> {len(results)}건")

    # CASCADE에 정의된 순서(신뢰도 순)대로 중복제거하며 병합
    all_results = []
    seen_names = set()
    for operation, label, _ in CASCADE:
        results = results_by_operation.get(operation, [])
        new_count = 0
        for r in results:
            if r["name"] in seen_names:
                continue
            seen_names.add(r["name"])
            all_results.append(r)
            new_count += 1
        print(f"  [{label}] 신규 {new_count}건 반영 (누적 {len(all_results)}건)")

    print(f"\n[캐스케이드 종료] 최종 원본 후보 {len(all_results)}건")
    return all_results[:target_count]


if __name__ == "__main__":
    # 단독 실행용(이 파일만 테스트): 나라장터API + DB캐시만 모아서 돌림.
    # Tavily까지 포함한 3소스 통합은 supplier_search.py 실행 참고.
    item_name = input("검색할 품목명 입력: ").strip()
    target_input = input("목표 개수 (그냥 엔터시 10개): ").strip()
    target = int(target_input) if target_input else 10

    print(f"\n[후보 수집] 나라장터API + DB캐시 동시 실행 중...")
    with ThreadPoolExecutor(max_workers=2) as _executor:
        _api_future = _executor.submit(search_all, item_name, target * 2)
        _db_future = _executor.submit(search_db_cache, item_name, target * 2)
        api_candidates = _api_future.result()
        db_candidates = _db_future.result()
    print(f"[나라장터API] {len(api_candidates)}건, [DB캐시] {len(db_candidates)}건")

    _seen = set()
    merged_candidates = []
    for c in api_candidates + db_candidates:
        if c["name"] in _seen:
            continue
        _seen.add(c["name"])
        merged_candidates.append(c)
    merged_candidates = merged_candidates[:target * 2]

    results = enrich_candidates(merged_candidates, item_name=item_name, target_count=target)

    print(f"\n{'=' * 50}")
    print(f"=== '{item_name}' 최종 결과 ({len(results)}건) ===")
    print(f"{'=' * 50}")

    if not results:
        print("결과 없음")

    for r in results:
        raw = r.get("raw") or {}
        print(f"\n{r['name']}  [출처: {r.get('operation', r.get('source'))}]")
        print(f"  전화: {r.get('phone') or '(없음)'}")
        print(f"  이메일: {r.get('email') or '(없음)'}")
        print(f"  홈페이지: {r.get('site_url') or '(없음)'}")
        addr = raw.get("hdoffceLocplc") or ""
        if addr:
            print(f"  주소: {addr}")
        spec = raw.get("prdctSpecNm") or raw.get("prdctClsfcNoNm") or ""
        if spec:
            print(f"  품목: {spec}")
