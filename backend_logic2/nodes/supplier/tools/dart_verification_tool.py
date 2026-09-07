"""
tools/dart_verification_tool.py - tests/supplier_search_test.py에서 검증된
"DART 매칭을 enrichment의 1차 관문으로 세우는" 구조를 프로덕션 tool로
승격한 것.

핵심 아이디어(2026-09-07 실측 확인 후 채택): Tavily로 뽑은 회사명 후보를
DART(금융감독원 전자공시) 기업코드와 매칭해보면, 매칭 성공 + 공식
홈페이지(hm_url) 확보되는 경우 그 홈페이지를 바로 스크래핑해서 연락처를
뽑을 수 있음 - naver_contact_enrichment의 "네이버로 공식사이트 찾기"
단계를 생략할 수 있어 빠르고, 정부 공시데이터로 신원이 이미 검증됐다는
신뢰도 이점도 있음.

정책(2026-09-07 최종 확정 - 최초엔 "홈페이지만 있으면 연락처 없어도
채택"이었다가 같은 날 재검토 후 수정됨):
  - DART 매칭 + 홈페이지 확보된 후보는 그 홈페이지를 스크래핑해서
    이메일/전화를 뽑아보고, 이메일 또는 전화 중 하나라도 나오면 채택.
    둘 다 안 나오면 제외함 - 어차피 프론트에서 이메일이 없으면 RFQ
    발송 자체가 안 되고(사람이 직접 채워넣어야 함), 그럴 거면 "사이트만
    있고 연락처는 하나도 없는" 후보를 보여주는 게 실익이 없다고 판단.
    즉 DART매칭이 주는 이점은 "연락처 기준 완화"가 아니라 "네이버로
    공식사이트 찾는 단계를 생략할 수 있다는 속도/신뢰도"임 - 연락처
    있어야 채택하는 기준 자체는 네이버 폴백 경로와 동일하게 유지.
  - DART 미매칭, 매칭됐지만 홈페이지가 없는 후보, 또는 홈페이지는
    있었지만 이메일/전화를 둘 다 못 찾은 후보는 그대로
    narajangteo_search_based_tool.enrich_candidates()의 네이버 폴백
    경로로 넘김 - 거기도 이메일 또는 전화 중 하나는 있어야 채택하는
    동일한 정책.
  - 재무제표(fnlttSinglAcnt.json)는 여기서 안 가져옴 - 공급업체 후보
    선정에 홈페이지/전화/이메일이면 충분하고, 재무데이터는 API 호출과
    시간만 추가로 드는 데 비해 이 단계에서 쓸 곳이 없음(필요해지면
    나중에 다시 추가하면 됨).

매칭은 1차 정확매치(㈜/주식회사 등 표기차이 무시) -> 실패시 2차
fuzzy매치(difflib, 표준 라이브러리만 사용, 외부 의존성 추가 없음) 순서.
fuzzy 컷오프(0.85)와 최소 연속일치 길이(3자) 조건은 "한국SKF씰"이
"한국TSK"(전혀 다른 회사)로 오매칭되던 실측 버그를 고치면서 정해짐 -
짧은 문자열끼리 우연히 몇 글자만 겹쳐도 ratio가 뻥튀기되는 문제 대응.

.env 필요: DART_API(opendart.fss.or.kr에서 발급), TAVILY_API_KEY,
OPENAI_API_KEY(연락처추출용)
"""

import difflib
import io
import os
import time
import zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

from backend_logic2.nodes.supplier.tools.naver_contact_enrichment import (
    _fetch_page_text,
    _extract_contacts_batch,
)

DART_BASE_URL = "https://opendart.fss.or.kr/api"
CORP_CODE_CACHE_PATH = Path(__file__).resolve().parent / "_dart_corp_code_cache.xml"
CORP_CODE_CACHE_MAX_AGE_DAYS = 7
FUZZY_MATCH_CUTOFF = 0.85  # 0.72였는데 "한국SKF씰"<->"한국TSK"(전혀 다른 회사)가
# 짧은 문자열이라 우연히 겹치는 글자 몇 개만으로 0.73이 나온 게 실측 확인돼서 올림
FUZZY_MIN_MATCH_BLOCK_LEN = 3  # 최장 연속 일치 구간이 이 미만이면(우연한 글자
# 겹침일 확률 높음) ratio가 컷오프를 넘어도 채택 안 함

_NAME_NOISE_TOKENS = ("주식회사", "(주)", "㈜", "유한회사", "합자회사", " ")

# 프로세스 안에서 재사용하는 메모리 캐시 - 매 supplier_search() 호출마다
# corpCode.xml(수만 건)을 다시 파싱하지 않으려고 파일 mtime 기준으로
# 재사용 여부 판단.
_NAME_MAP_MEMORY_CACHE = {"map": None, "mtime": None}


# ---------------------------------------------------------------------------
# 회사명 정규화 (supplier_search.py의 _dedup_key와 동일 발상 - 표기차이 무시)
# ---------------------------------------------------------------------------

def _normalize_company_name(name: str) -> str:
    key = name or ""
    for token in _NAME_NOISE_TOKENS:
        key = key.replace(token, "")
    return key.strip()


# ---------------------------------------------------------------------------
# DART OpenAPI
# ---------------------------------------------------------------------------

def _get_dart_api_key() -> str:
    api_key = os.environ.get("DART_API")
    if not api_key:
        raise RuntimeError(".env에 DART_API가 없음 (DART OpenAPI 인증키, opendart.fss.or.kr에서 발급)")
    return api_key


def _download_corp_code_xml(api_key: str) -> bytes:
    print("  [DART] corpCode.xml 다운로드 중... (전체 공시대상 기업 코드, 최초 1회 or 캐시만료시만)")
    resp = requests.get(f"{DART_BASE_URL}/corpCode.xml", params={"crtfc_key": api_key}, timeout=30)
    resp.raise_for_status()
    try:
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            xml_bytes = zf.read("CORPCODE.xml")
    except zipfile.BadZipFile:
        raise RuntimeError(
            f"DART corpCode.xml 응답이 zip이 아님(인증키 오류 의심) - 응답 앞부분: {resp.content[:200]!r}"
        )
    CORP_CODE_CACHE_PATH.write_bytes(xml_bytes)
    return xml_bytes


def _load_corp_code_map(api_key: str) -> dict:
    """정규화된 회사명 -> [(원본 DART명, corp_code), ...] 매핑."""
    if CORP_CODE_CACHE_PATH.exists():
        mtime = CORP_CODE_CACHE_PATH.stat().st_mtime
        if _NAME_MAP_MEMORY_CACHE["map"] is not None and _NAME_MAP_MEMORY_CACHE["mtime"] == mtime:
            return _NAME_MAP_MEMORY_CACHE["map"]

        age_days = (time.time() - mtime) / 86400
        xml_bytes = (
            CORP_CODE_CACHE_PATH.read_bytes()
            if age_days <= CORP_CODE_CACHE_MAX_AGE_DAYS
            else _download_corp_code_xml(api_key)
        )
    else:
        xml_bytes = _download_corp_code_xml(api_key)

    root = ET.fromstring(xml_bytes)
    name_map = {}
    for item in root.findall("list"):
        corp_name = (item.findtext("corp_name") or "").strip()
        corp_code = (item.findtext("corp_code") or "").strip()
        if not corp_name or not corp_code:
            continue
        name_map.setdefault(_normalize_company_name(corp_name), []).append((corp_name, corp_code))

    _NAME_MAP_MEMORY_CACHE["map"] = name_map
    _NAME_MAP_MEMORY_CACHE["mtime"] = CORP_CODE_CACHE_PATH.stat().st_mtime
    return name_map


def _find_corp_code(name_map: dict, candidate_name: str):
    """
    1차 정확매치 -> 실패시 2차 fuzzy매치(difflib, 표준 라이브러리).
    반환: (dart_name, corp_code, match_type, score) 또는 None.
    match_type: "exact" 또는 "fuzzy". score: exact는 1.0, fuzzy는 유사도(0~1).
    """
    key = _normalize_company_name(candidate_name)

    exact = name_map.get(key)
    if exact:
        dart_name, corp_code = exact[0]
        return dart_name, corp_code, "exact", 1.0

    # 후보 몇 개만 빠르게 추리고(get_close_matches 내부최적화), 그 중에서
    # "최장 연속 일치 구간"까지 확인해서 우연한 글자 겹침을 걸러냄.
    candidates = difflib.get_close_matches(key, name_map.keys(), n=3, cutoff=FUZZY_MATCH_CUTOFF)
    for cand_key in candidates:
        sm = difflib.SequenceMatcher(None, key, cand_key)
        longest_block = max((b.size for b in sm.get_matching_blocks()), default=0)
        if longest_block < FUZZY_MIN_MATCH_BLOCK_LEN:
            continue  # ratio는 넘었지만 우연한 짧은 겹침으로 판단, 기각
        score = round(sm.ratio(), 2)
        dart_name, corp_code = name_map[cand_key][0]
        return dart_name, corp_code, "fuzzy", score

    return None


def _dart_get(endpoint: str, api_key: str, **params):
    resp = requests.get(f"{DART_BASE_URL}/{endpoint}", params={"crtfc_key": api_key, **params}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _fetch_company_profile(api_key: str, corp_code: str):
    """
    기업개황(company.json)만 조회 - hm_url(홈페이지), phn_no(전화),
    bizr_no/jurir_no(사업자·법인등록번호) 등. 재무제표(fnlttSinglAcnt.json)는
    여기서 조회 안 함(공급업체 후보 선정엔 불필요, 필요시 별도 추가).
    """
    data = _dart_get("company.json", api_key, corp_code=corp_code)
    return data if data.get("status") == "000" else None


def _chunked(seq, n):
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def _fetch_contacts_via_dart_homepage(entries, item_name=None, case_id=None, batch_size=5):
    """
    entries: [{"name": 후보명, "hm_url": DART가 준 공식 홈페이지}, ...]
    naver_contact_enrichment.enrich_contacts_batch의 1단계(네이버로 사이트
    찾기)를 생략하고 바로 스크래핑, 2단계(배치 AI 연락처추출)는 동일 함수
    (_extract_contacts_batch)를 그대로 재사용해서 로직 중복을 피함.
    반환: {name: {"email":..., "phone":..., "site_url":...}, ...}
    (이메일/전화 둘 다 못 찾아도 여기선 일단 엔트리를 포함시킴 - 실제
    이메일/전화 유무로 최종 채택여부를 가르는 건 호출부인
    dart_verify_and_fetch_contacts()가 함)
    """
    prepared = []
    with ThreadPoolExecutor(max_workers=min(len(entries), 8) or 1) as executor:
        future_to_entry = {executor.submit(_fetch_page_text, e["hm_url"]): e for e in entries}
        for future in as_completed(future_to_entry):
            e = future_to_entry[future]
            try:
                page_text = future.result()
            except Exception as ex:
                print(f"    [DART홈페이지 스크래핑 실패] {e['name']} ({e['hm_url']}): {ex}")
                page_text = ""
            prepared.append({"name": e["name"], "page_text": page_text, "site_url": e["hm_url"]})

    all_contacts = {}
    batches = _chunked(prepared, batch_size)
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
                all_contacts.update(future.result())
            except Exception as e:
                print(f"    DART홈페이지 배치 AI 처리 오류, 건너뜀: {e}")
            for c in chunk:
                if c["name"] not in all_contacts:
                    all_contacts[c["name"]] = {"email": None, "phone": None}

    result = {}
    for c in prepared:
        contact = all_contacts.get(c["name"], {})
        result[c["name"]] = {
            "email": contact.get("email"),
            "phone": contact.get("phone"),
            "site_url": c["site_url"],
        }
    return result


# ---------------------------------------------------------------------------
# 고수준 함수 - supplier_search.py가 이거 하나만 호출하면 됨
# ---------------------------------------------------------------------------

def dart_verify_and_fetch_contacts(candidates, item_name=None, case_id=None, batch_size=5):
    """
    후보 리스트를 DART(정확+fuzzy)로 매칭 시도해서 두 그룹으로 나눔:

      1. dart_verified: DART 매칭 + 홈페이지(hm_url) 확보 + 그 홈페이지를
         스크래핑해서 이메일 또는 전화 중 하나라도 나온 최종 candidate.
         둘 다 못 찾으면 이 목록엔 안 들어감(제외) - 어차피 이메일 없으면
         프론트에서 RFQ 발송이 안 되니, 연락처가 전혀 없는 후보를 보여줄
         실익이 없다고 판단(2026-09-07).
      2. remaining: DART 미매칭, 매칭됐지만 홈페이지가 없는 후보, 또는
         홈페이지는 있었지만 이메일/전화를 둘 다 못 찾은 후보. 원래
         candidate 그대로 반환 - 호출부가 기존
         narajangteo_search_based_tool.enrich_candidates()에 넘겨서
         네이버 폴백을 태우면 됨(거기도 이메일 또는 전화 중 하나는 있어야
         채택하는 동일한 정책).

    DART_API가 없거나 corpCode.xml 다운로드가 실패하는 등 DART 자체를
    못 쓰는 상황이면 전부 remaining으로 돌려보내서(dart_verified=[])
    전체 파이프라인이 죽지 않고 네이버 폴백만으로라도 동작하게 함.

    반환: (dart_verified: list[dict], remaining: list[dict])
    """
    try:
        api_key = _get_dart_api_key()
        name_map = _load_corp_code_map(api_key)
    except Exception as e:
        print(f"  [DART] 사용 불가({e}), 전부 네이버 폴백으로 넘김")
        return [], list(candidates)

    print(f"  [DART] corp_code 매핑 {len(name_map)}건 로드 완료")

    matched_with_site = []
    remaining = []
    for c in candidates:
        name = c["name"]
        matched = _find_corp_code(name_map, name)
        if not matched:
            print(f"  [DART 미매칭] '{name}'")
            remaining.append(c)
            continue

        dart_name, corp_code, match_type, score = matched
        try:
            profile = _fetch_company_profile(api_key, corp_code)
        except Exception as e:
            print(f"  [DART] '{name}' 기업개황 조회 실패: {e}")
            profile = None

        hm_url = (profile or {}).get("hm_url")
        if not hm_url:
            print(
                f"  [DART {match_type}, {score}] '{name}' -> '{dart_name}' "
                f"매칭됐지만 홈페이지 없음, 네이버 폴백으로"
            )
            remaining.append(c)
            continue

        print(f"  [DART {match_type}, {score}] '{name}' -> '{dart_name}' ({corp_code}), 홈페이지 확보: {hm_url}")
        matched_with_site.append({
            "candidate": c, "dart_name": dart_name, "corp_code": corp_code,
            "match_type": match_type, "match_score": score, "profile": profile, "hm_url": hm_url,
        })

    if not matched_with_site:
        return [], remaining

    print(f"  [DART] {len(matched_with_site)}개 홈페이지 직접 스크래핑 -> 연락처 확보 (네이버 검색 생략)")
    contacts = _fetch_contacts_via_dart_homepage(
        [{"name": m["candidate"]["name"], "hm_url": m["hm_url"]} for m in matched_with_site],
        item_name=item_name, case_id=case_id, batch_size=batch_size,
    )

    dart_verified = []
    for m in matched_with_site:
        c = m["candidate"]
        name = c["name"]
        contact = contacts.get(name, {})
        profile = m["profile"] or {}

        email = contact.get("email")
        phone = contact.get("phone") or profile.get("phn_no")  # AI추출 우선, 없으면 DART 공시 전화 폴백

        if not email and not phone:
            # 2026-09-07 재확정: 이메일 없으면 프론트에서 RFQ 발송 자체가
            # 안 돼서(사람이 결국 직접 채워야 함), DART매칭+홈페이지는
            # 있어도 연락처가 전혀 없으면 채택 안 함 - 네이버 폴백 경로로
            # 넘겨서 한 번 더 기회를 줌(거긴 재검색 재시도 단계까지 있음).
            print(f"    '{name}': DART 홈페이지에서도 이메일/전화 둘 다 못 찾음, 네이버 폴백으로 이관")
            remaining.append(c)
            continue

        merged = dict(c)
        merged["source"] = "dart"
        merged["operation"] = "dart_verified"
        merged["email"] = email
        merged["phone"] = phone
        merged["site_url"] = m["hm_url"]
        merged["raw"] = {
            **(c.get("raw") or {}),
            "dart_name": m["dart_name"],
            "corp_code": m["corp_code"],
            "dart_match_type": m["match_type"],
            "dart_match_score": m["match_score"],
            "bizr_no": profile.get("bizr_no"),
            "jurir_no": profile.get("jurir_no"),
            "ceo_nm": profile.get("ceo_nm"),
            "adres": profile.get("adres"),
        }
        print(
            f"    '{name}': DART 검증+연락처 확보 채택 (이메일={email}, 전화={phone}, "
            f"홈페이지={merged['site_url']})"
        )
        dart_verified.append(merged)

    return dart_verified, remaining
