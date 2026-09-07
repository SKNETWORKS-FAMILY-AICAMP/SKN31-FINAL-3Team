"""
supplier_search.py - 최종 통합 파이프라인 (tools/ 밖, 최상위 실행파일).

2026-09-07 구조 개편(2차): 후보 수집을 나라장터API+DB캐시+Tavily 3소스
병렬에서, "Tavily(구조화 추출 기반) 단일 소스"로 일단 축소함. 나라장터
API/DB캐시는 코드를 지우지 않고 이 파일에서 호출만 뺌(아래 주석 참고) -
필요해지면 언제든 병렬 수집에 다시 추가하면 됨. 대신 그 자리를
DART(금융감독원 전자공시) 매칭이 채움: Tavily가 뽑은 회사명 후보를 DART
기업코드와 매칭해서(정확+fuzzy) 신원을 검증하고, 매칭+홈페이지 확보된
후보는 그 홈페이지를 직접 스크래핑해 연락처를 채움. DART로 홈페이지까지
확보해도 이메일/전화가 둘 다 안 나오면 채택 안 함(같은 날 재검토 후
확정) - 프론트에서 이메일 없으면 RFQ 발송 자체가 안 돼서 사람이 결국
직접 채워야 하고, 그럴 거면 "사이트만 있고 연락처가 전혀 없는" 후보를
보여주는 실익이 없다고 판단함. 그래서 DART가 주는 실질적 이점은
"연락처 기준 완화"가 아니라 "네이버로 공식사이트 찾는 단계를 생략할
수 있다는 속도/신뢰도"임 - 이메일 또는 전화 중 하나는 있어야 채택하는
기준 자체는 네이버 폴백 경로와 동일하게 유지. DART로 못 걸러진 나머지
(미매칭/홈페이지없음/연락처없음)는 기존 enrich_candidates()의 네이버
폴백 경로로 보냄(거긴 이메일 또는 전화 중 하나는 있어야 채택하는 기존
정책 그대로).

2026-08-31 구조 개편(1차, 위 개편 이전 히스토리): 예전엔 "1순위(나라장터
API+DB캐시)를 다 끝내고, 목표개수를 못 채웠을 때만 2순위(Tavily) 실행"
이라는 순차 폴백 구조였음. 근데 '오일씰' 같은 산업부품 카테고리에서
나라장터 API+DB캐시가 둘 다 구조적으로 0건이 나오는 게 실측 확인되면서
("1순위가 항상 채워줄 것"이라는 전제 자체가 항상 성립하진 않음), 3소스를
처음부터 병렬로 돌리는 구조로 바꿨었음 (이번 2차 개편으로 일단 Tavily
단일소스로 축소 - 아래 신뢰도 우선순위 문단은 3소스 병렬 시절 히스토리).

신뢰도 우선순위(3소스 병렬 수집 시절, dedup 시 이름이 겹치면 먼저
채택되는 순서): 나라장터API > DB캐시 > Tavily - 정부 검증 데이터를
웹검색 결과보다 신뢰도 높게 취급했음. 지금은 단일소스라 이 순서 자체는
안 쓰이지만, DART 매칭이 사실상 동일한 역할(검증된 정부데이터 우선)을
이어받음.

캐싱(2026-08-31 추가): 정규화된 품목명 기준으로
procurement.supplier_search_cache 테이블에 결과(회사명/사이트/전화/
이메일)를 캐싱함(TTL 기본 30일, 만료되면 행 자체가 삭제됨 -
tools/supplier_search_cache.py 참고). 같은 정규화 품목명으로 재검색하면
캐시부터 확인해서 있으면 "[캐시 히트]" 로그와 함께 바로 반환하고, 수집+
enrichment 파이프라인 자체를 생략함. 캐시 테이블은 최초 1회
create_supplier_search_cache_table.py(레포 루트) 실행해서 만들어야 함.

케이스/AI판단 이력(2026-08-31 추가): 케이스는 이 함수가 만들지 않음 -
process_graph.py의 route_entrypoint_command(그래프 맨 처음 노드, MR당
딱 1번만 실행됨)가 만든 case_id를 그래프가 resolve_suppliers_choice_command
-> search_new_suppliers_command를 거쳐 이 함수까지 넘겨줌. 이 함수는 그
case_id로 캐시히트/신규탐색시작/수집완료/최종완료를 case_status_history에
남기기만 함. case_id가 None이면(단독 __main__ 실행 등, MR 컨텍스트 없음)
로깅은 조용히 스킵되고 파이프라인 자체는 그대로 동작함. 그 안에서 벌어지는
AI 판단(구조화추출/필터/회사명추출/DART연락처추출/네이버연락처추출)은
각 tools/*.py 함수가 이 case_id를 그대로 관통시켜받아 ai_decision_log에
남김. 테이블은 최초 1회 create_procurement_tracking_tables.py(레포 루트)
실행해서 만들어야 함(tools/case_logging.py가 실제 기록 담당).

폴더 구조:
  supplier_search.py (이 파일)
  create_supplier_search_cache_table.py (레포 루트, 캐시 테이블 최초 생성용)
  tools/
    structured_item_search_tool.py (구조화 추출 + intent별 Tavily 검색 - 후보수집)
    dart_verification_tool.py (DART 매칭 + 홈페이지 직접 연락처 확보 - 1차 검증관문)
    narajangteo_search_based_tool.py (search_all/search_db_cache - 지금 미사용,
      enrich_candidates만 씀 - DART가 못 거른 후보의 네이버 폴백 경로)
    web_search_based_tool.py (normalize_item_name - 캐시 키 정규화용으로 씀)
    naver_contact_enrichment.py (공유 enrichment 헬퍼)
    supplier_search_cache.py (캐시 조회/저장/만료정리)

.env 필요: TAVILY_API_KEY, NAVER_CLIENT_ID, NAVER_CLIENT_SECRET,
OPENAI_API_KEY, DART_API, NEXTERP_DATABASE_URL(캐시테이블용)

실행: python supplier_search.py (최초 1회는 create_supplier_search_cache_table.py 먼저)
"""

from backend_logic2.nodes.supplier.tools.narajangteo_search_based_tool import (
    enrich_candidates,
)
from backend_logic2.nodes.supplier.tools.web_search_based_tool import (
    normalize_item_name,
)
from backend_logic2.nodes.supplier.tools.structured_item_search_tool import (
    collect_candidate_names_structured,
)
from backend_logic2.nodes.supplier.tools.dart_verification_tool import (
    dart_verify_and_fetch_contacts,
)
from backend_logic2.nodes.supplier.tools.supplier_search_cache import (
    cleanup_expired_cache,
    get_cached_results,
    save_to_cache,
)
from backend_logic2.nodes.supplier.tools.case_logging import log_status_change

# 2026-09-07 기준 미사용(호출만 뺌, 함수 자체는 narajangteo_search_based_tool.py에
# 그대로 남아있음). 나라장터API/DB캐시를 다시 병렬 수집에 넣고 싶으면:
#   from backend_logic2.nodes.supplier.tools.narajangteo_search_based_tool import (
#       search_all, search_db_cache,
#   )
#   ThreadPoolExecutor로 search_all / search_db_cache /
#   collect_candidate_names_structured 를 병렬 실행 -> 결과 병합 후
#   _dedup_key로 중복제거 (git 히스토리의 이전 supplier_search.py 참고)


def _dedup_key(name):
    """
    이름 표기 차이(㈜/주식회사/공백 등)를 무시하고 동일회사인지 비교하기
    위한 정규화 키. 실제로 화면에 보여줄 이름(candidate["name"])은 안
    바꾸고 중복판단에만 씀.
    """
    key = name
    for token in ("주식회사", "(주)", "㈜", "유한회사", "합자회사", " "):
        key = key.replace(token, "")
    return key.strip()


def supplier_search(item_name, target_count=10, case_id=None):
    """
    case_id는 process_graph.py가 만든 MR 케이스를 그대로 받아씀(그래프
    경유 호출이면 항상 채워져 있음). 단독 실행(__main__ 등, MR 컨텍스트
    없음)이면 case_id=None으로 두면 됨 - 로깅만 조용히 스킵되고 검색
    자체는 그대로 동작함.
    """
    print(f"\n{'=' * 60}")
    print(f"품목명 정규화 중... (캐시 키 용도)")
    print(f"{'=' * 60}")
    normalized = normalize_item_name(item_name)

    cleanup_expired_cache()

    cached = get_cached_results(normalized)
    if cached:
        print(f"\n{'=' * 60}")
        print(f"[캐시 히트] '{normalized}' 캐시에서 {len(cached)}건 재사용 (신규탐색 생략)")
        print(f"{'=' * 60}")
        log_status_change(
            case_id, "completed_from_cache",
            reason=f"'{normalized}' 캐시에서 {len(cached)}건 재사용, 신규탐색 생략",
        )
        return cached[:target_count]

    print(f"\n{'=' * 60}")
    print(f"[캐시 미스] '{normalized}' 캐시 없음, 신규탐색 진행")
    print(f"{'=' * 60}")
    log_status_change(case_id, "searching", reason=f"'{normalized}' 캐시 미스, Tavily(구조화) 수집 시작")

    collect_target = target_count * 2

    print(f"\n{'=' * 60}")
    print(f"[후보 수집] Tavily(구조화 추출 기반) 실행 중... (나라장터API/DB캐시는 지금 안 씀)")
    print(f"{'=' * 60}")
    # 구조화추출은 정규화된 문자열(normalized)이 아니라 원본 item_name으로
    # 함 - 안전등급/규격코드(예: "2B 10K") 같은 신호가 normalize_item_name
    # 단계에서 이미 지워졌을 수 있어서, 구조화추출 자체는 원본을 다시 봄.
    try:
        candidates = collect_candidate_names_structured(item_name, collect_target, case_id=case_id)
    except Exception as e:
        print(f"  [Tavily-구조화] 예외 발생, 후보 없음으로 진행: {e}")
        candidates = []

    seen_keys = set()
    deduped = []
    for c in candidates:
        key = _dedup_key(c["name"])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(c)
    candidates = deduped

    print(f"\n{'=' * 60}")
    print(f"[수집 결과] Tavily(구조화) {len(candidates)}건 (중복제거 후)")
    print(f"{'=' * 60}")

    log_status_change(
        case_id, "collected",
        reason=f"Tavily(구조화) 수집+중복제거 완료: {len(candidates)}건",
    )

    print(f"\n{'=' * 60}")
    print(f"[1차 검증관문] DART 매칭 시도 (정확+fuzzy) -> 매칭+홈페이지 확보되면 연락처 직접 확보")
    print(f"{'=' * 60}")
    dart_verified, remaining = dart_verify_and_fetch_contacts(
        candidates, item_name=normalized, case_id=case_id
    )

    print(f"\n{'=' * 60}")
    print(
        f"[DART 결과] {len(dart_verified)}건 DART검증+연락처 확보 통과, "
        f"{len(remaining)}건 네이버 폴백으로 이관"
    )
    print(f"{'=' * 60}")

    naver_enriched = []
    if remaining:
        naver_enriched = enrich_candidates(
            remaining, item_name=normalized, target_count=target_count, batch_size=5, case_id=case_id
        )

    results = dart_verified + naver_enriched

    print(f"\n{'=' * 60}")
    print(
        f"[최종 결과] {len(results)}/{target_count}개 확보 "
        f"(DART직접 {len(dart_verified)}, 네이버폴백 {len(naver_enriched)})"
    )
    print(f"{'=' * 60}")

    save_to_cache(normalized, results)

    log_status_change(
        case_id, "search_completed",
        reason=(
            f"'{normalized}' 신규탐색 {len(results)}/{target_count}개 확보 "
            f"(DART직접 {len(dart_verified)}, 네이버폴백 {len(naver_enriched)})"
        ),
    )

    return results[:target_count]


if __name__ == "__main__":
    item_name = input("품목명 입력: ").strip()
    target_input = input("목표 개수 (그냥 엔터시 10개): ").strip()
    target = int(target_input) if target_input else 10

    results = supplier_search(item_name, target_count=target)

    print(f"\n{'=' * 60}")
    print(f"=== '{item_name}' 최종 결과 ({len(results)}건) ===")
    print(f"{'=' * 60}")

    if not results:
        print("결과 없음")

    for r in results:
        print(f"\n{r['name']}  [출처: {r.get('source')} / {r.get('operation', r.get('source'))}]")
        print(f"  이메일: {r.get('email') or '(없음)'}")
        print(f"  전화: {r.get('phone') or '(없음)'}")
        print(f"  사이트: {r.get('site_url') or '(없음)'}")
