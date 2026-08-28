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

.env 필요: DATA_GO_KR_SERVICE_KEY

실행: python search_all_narajangteo.py
"""

import os
from urllib.parse import unquote
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from dotenv import load_dotenv

try:
    from .naver_contact_enrichment import _fetch_page_text
except ImportError:  # tools 폴더에서 직접 실행할 때
    from naver_contact_enrichment import _fetch_page_text

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
            results.append({"name": name, "raw": it, "operation": operation})
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
            results.append({"name": name, "raw": it, "operation": operation})
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


def _fetch_basic_and_page(candidate):
    """
    1단계(병렬, AI 호출 없음): 조달업체 기본정보로 홈페이지 조회하고,
    있으면 페이지 텍스트까지 가져옴. 홈페이지 없으면 여기서 바로 제외
    (None 반환). 이 단계는 회사마다 다른 API/URL을 호출해야 해서
    원래도 배치가 안 되는 부분이라 그대로 병렬화만 함.
    """
    name = candidate["name"]
    print(f"\n  [{name}] 조달업체 기본정보 조회 중...")

    raw = candidate["raw"]
    bizno = raw.get("cntrctCorpBizno") or raw.get("dminsttBizno")

    if bizno:
        print(f"    사업자번호({bizno})로 조회")
        basic_info = get_corp_basic_info(bizno)
    else:
        print(f"    사업자번호 없음, 회사명으로 조회")
        basic_info = get_corp_basic_info_by_name(name)

    if not basic_info:
        print(f"    -> 기본정보 자체가 없음, 제외")
        return None

    homepage = basic_info.get("hmpgAdrs")
    if not homepage:
        print(f"    -> 홈페이지 없음, 제외 (검증 불가능하므로)")
        return None

    print(f"    -> 검증된 홈페이지 확보: {homepage}, 페이지 가져오는 중...")
    page_text = _fetch_page_text(homepage)
    print(f"    -> {len(page_text)}자 확보")

    if not page_text:
        print(f"    -> 페이지 내용을 못 가져옴, 제외")
        return None

    return {
        "candidate": candidate, "homepage": homepage,
        "page_text": page_text, "gov_phone": basic_info.get("telNo"),
    }


def search_all_with_detail(item_name, target_count=10, batch_size=5):
    """
    search_all()로 후보 찾고(원본 품목검색은 목표개수보다 넉넉하게
    가져와서, 상세검증 단계에서 일부 걸러져도 최종 target_count를
    채울 여유를 둠), 2단계로 나눠서 처리:

    1단계(병렬): 회사마다 정부DB조회+페이지가져오기 (AI 호출 없음)
    2단계(배치, 병렬): batch_size개씩 묶은 배치들을 전부 동시에
      AI 호출 (이메일/전화/관련성 판단). 이전엔 배치를 순서대로 돌면서
      목표개수 도달하면 나머지 배치를 생략했는데, 병렬로 바꾸면서
      그 조기종료는 못 씀 - latency 우선으로 결정됨(AI호출 비용은 약간
      늘 수 있음).
    """
    try:
        from .naver_contact_enrichment import _extract_contacts_batch
    except ImportError:  # tools 폴더에서 직접 실행할 때
        from naver_contact_enrichment import _extract_contacts_batch

    candidates = search_all(item_name, target_count=target_count * 2)

    print(f"\n[상세정보 검증 - 1단계] {len(candidates)}개 후보의 기본정보+홈페이지+텍스트 조회 중... (병렬, AI없음)")
    prepared = []
    with ThreadPoolExecutor(max_workers=min(len(candidates), 8) or 1) as executor:
        futures = [executor.submit(_fetch_basic_and_page, c) for c in candidates]
        for f in as_completed(futures):
            result = f.result()
            if result is not None:
                prepared.append(result)

    print(f"\n[상세정보 검증 - 1단계 완료] {len(candidates)}개 중 {len(prepared)}개 홈페이지 확보됨")

    batches = [prepared[i:i + batch_size] for i in range(0, len(prepared), batch_size)]
    print(f"\n[상세정보 검증 - 2단계] {len(batches)}개 배치를 동시에 AI로 이메일/전화/관련성 검증 중... (병렬)")

    enriched = []
    with ThreadPoolExecutor(max_workers=len(batches) or 1) as executor:
        future_to_chunk = {}
        for batch_idx, chunk in enumerate(batches):
            chunk_input = [{"name": p["candidate"]["name"], "page_text": p["page_text"]} for p in chunk]
            print(f"  배치 {batch_idx + 1} 제출: {[c['name'] for c in chunk_input]}")
            future = executor.submit(_extract_contacts_batch, chunk_input, item_name)
            future_to_chunk[future] = chunk

        for future in as_completed(future_to_chunk):
            chunk = future_to_chunk[future]
            try:
                results = future.result()
            except Exception as e:
                print(f"    배치 처리 중 오류, 이 배치 건너뜀: {e}")
                continue

            for p in chunk:
                name = p["candidate"]["name"]
                contact = results.get(name, {})

                if contact.get("is_relevant") is False:
                    print(f"    '{name}': 관련성 낮음, 제외")
                    continue

                email = contact.get("email")
                phone = contact.get("phone") or p["gov_phone"]  # AI추출 우선, 없으면 정부DB값 폴백

                if not email and not phone:
                    print(f"    '{name}': 이메일/전화 둘 다 못 찾음, 제외")
                    continue

                print(f"    '{name}': 채택 (이메일={email}, 전화={phone})")
                candidate = p["candidate"]
                candidate["email"] = email
                candidate["phone"] = phone
                candidate["homepage"] = p["homepage"]
                enriched.append(candidate)

    print(f"\n[상세정보 검증 완료] {len(candidates)}개 중 {len(enriched)}개 최종 통과")
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
    item_name = input("검색할 품목명 입력: ").strip()
    target_input = input("목표 개수 (그냥 엔터시 10개): ").strip()
    target = int(target_input) if target_input else 10

    results = search_all_with_detail(item_name, target_count=target)

    print(f"\n{'=' * 50}")
    print(f"=== '{item_name}' 최종 결과 ({len(results)}건) ===")
    print(f"{'=' * 50}")

    if not results:
        print("결과 없음")

    for r in results:
        raw = r["raw"]
        print(f"\n{r['name']}  [출처: {r['operation']}]")
        print(f"  전화: {r.get('phone') or '(없음)'}")
        print(f"  이메일: {r.get('email') or '(없음)'}")
        print(f"  홈페이지: {r.get('homepage') or '(없음)'}")
        addr = raw.get("hdoffceLocplc") or ""
        if addr:
            print(f"  주소: {addr}")
        spec = raw.get("prdctSpecNm") or raw.get("prdctClsfcNoNm") or ""
        if spec:
            print(f"  품목: {spec}")
