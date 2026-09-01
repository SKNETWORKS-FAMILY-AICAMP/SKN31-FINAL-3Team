"""
supplier_search.py - 최종 통합 파이프라인 (tools/ 밖, 최상위 실행파일).

2026-08-31 구조 개편: 예전엔 "1순위(나라장터 API+DB캐시)를 다 끝내고,
목표개수를 못 채웠을 때만 2순위(Tavily) 실행"이라는 순차 폴백 구조였음.
근데 '오일씰' 같은 산업부품 카테고리에서 나라장터 API+DB캐시가 둘 다
구조적으로 0건이 나오는 게 실측 확인되면서("1순위가 항상 채워줄 것"
이라는 전제 자체가 항상 성립하진 않음), 순차 구조가 느리기만 하고
실익이 없는 경우가 있다는 게 드러남.

그래서 이제 3개 소스(나라장터API, DB캐시, Tavily)를 처음부터 병렬로
돌려서 후보 "이름"만 모으고, 그 다음 출처 상관없이 동일한 enrichment
파이프라인(narajangteo_search_based_tool.enrich_candidates - 나라장터
출처면 정부DB 홈페이지 우선 시도, 아니면 바로 네이버 검색 -> Jina
Reader 폴백 -> AI로 이메일/전화/관련성 판단)에 같이 태움.

신뢰도 우선순위(dedup 시 이름이 겹치면 먼저 채택되는 순서):
나라장터API > DB캐시 > Tavily - 정부 검증 데이터를 웹검색 결과보다
신뢰도 높게 취급.

캐싱(2026-08-31 추가): 3소스 병렬수집 + enrichment(1~3단계, AI 배치
호출 여러 번 포함) 전체가 검색 1번마다 시간/API비용이 꽤 드는 게
실측 확인돼서, 정규화된 품목명 기준으로 procurement.supplier_search_cache
테이블에 결과(회사명/사이트/전화/이메일)를 캐싱함(TTL 기본 30일, 만료되면
행 자체가 삭제됨 - tools/supplier_search_cache.py 참고). 같은 정규화
품목명으로 재검색하면 캐시부터 확인해서 있으면 "[캐시 히트]" 로그와
함께 바로 반환하고, 3소스 수집+enrichment 파이프라인 자체를 생략함.
캐시 테이블은 최초 1회 create_supplier_search_cache_table.py(레포 루트)
실행해서 만들어야 함.

케이스/AI판단 이력(2026-08-31 추가, 같은 날 MR 단위로 재설계): 케이스는
더 이상 이 함수가 만들지 않음 - process_graph.py의 route_entrypoint_command
(그래프 맨 처음 노드, MR당 딱 1번만 실행됨)가 만든 case_id를 그래프가
resolve_suppliers_choice_command -> search_new_suppliers_command를 거쳐
이 함수까지 넘겨줌. 이 함수는 그 case_id로 캐시히트/신규탐색시작/
수집완료/최종완료를 case_status_history에 남기기만 함. case_id가
None이면(단독 __main__ 실행 등, MR 컨텍스트 없음) 로깅은 조용히
스킵되고 파이프라인 자체는 그대로 동작함. 그 안에서 벌어지는 AI 판단
(사이트선택/연락처추출/회사명추출)은 각 tools/*.py 함수가 이 case_id를
그대로 관통시켜받아 ai_decision_log에 남김. 테이블은 최초 1회
create_procurement_tracking_tables.py(레포 루트) 실행해서 만들어야 함
(tools/case_logging.py가 실제 기록 담당).

폴더 구조:
  supplier_search.py (이 파일)
  create_supplier_search_cache_table.py (레포 루트, 캐시 테이블 최초 생성용)
  tools/
    narajangteo_search_based_tool.py (search_all, search_db_cache, enrich_candidates)
    web_search_based_tool.py (normalize_item_name, tavily_collect_candidate_names)
    naver_contact_enrichment.py (공유 enrichment 헬퍼)
    supplier_search_cache.py (캐시 조회/저장/만료정리)

.env 필요: DATA_GO_KR_SERVICE_KEY, TAVILY_API_KEY, NAVER_CLIENT_ID,
NAVER_CLIENT_SECRET, OPENAI_API_KEY, NEXTERP_DATABASE_URL(DB캐시+캐시테이블용)

실행: python supplier_search.py (최초 1회는 create_supplier_search_cache_table.py 먼저)
"""

from concurrent.futures import ThreadPoolExecutor

from backend_logic2.nodes.supplier.tools.narajangteo_search_based_tool import (
    search_all,
    search_db_cache,
    enrich_candidates,
)
from backend_logic2.nodes.supplier.tools.web_search_based_tool import (
    normalize_item_name,
    tavily_collect_candidate_names,
)
from backend_logic2.nodes.supplier.tools.supplier_search_cache import (
    cleanup_expired_cache,
    get_cached_results,
    save_to_cache,
)
from backend_logic2.nodes.supplier.tools.case_logging import log_status_change


def _dedup_key(name):
    """
    이름 표기 차이(㈜/주식회사/공백 등)를 무시하고 동일회사인지 비교하기
    위한 정규화 키. 실제로 화면에 보여줄 이름(candidate["name"])은 안
    바꾸고 중복판단에만 씀. 2026-08-31 '멀티탭' 테스트에서 나라장터API가
    낸 '주식회사광명전기'와 Tavily가 낸 '㈜광명전기'가 문자열이 달라서
    완전일치 dedup을 통과해 같은 회사가 후보에 중복으로 남는 게 확인돼서
    추가함.
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
    print(f"품목명 정규화 중...")
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
    log_status_change(case_id, "searching", reason=f"'{normalized}' 캐시 미스, 3소스 병렬 수집 시작")

    collect_target = target_count * 2

    print(f"\n{'=' * 60}")
    print(f"[후보 수집] 나라장터API + DB캐시 + Tavily 3개 소스 동시 실행 중...")
    print(f"{'=' * 60}")

    with ThreadPoolExecutor(max_workers=3) as executor:
        api_future = executor.submit(search_all, normalized, collect_target)
        db_future = executor.submit(search_db_cache, normalized, collect_target)
        tavily_future = executor.submit(
            tavily_collect_candidate_names, normalized, collect_target, case_id=case_id
        )

        try:
            api_candidates = api_future.result()
        except Exception as e:
            print(f"  [나라장터API] 예외 발생, 건너뜀: {e}")
            api_candidates = []
        try:
            db_candidates = db_future.result()
        except Exception as e:
            print(f"  [DB캐시] 예외 발생, 건너뜀: {e}")
            db_candidates = []
        try:
            tavily_candidates = tavily_future.result()
        except Exception as e:
            print(f"  [Tavily] 예외 발생, 건너뜀: {e}")
            tavily_candidates = []

    print(f"\n{'=' * 60}")
    print(
        f"[수집 결과] 나라장터API {len(api_candidates)}건, "
        f"DB캐시 {len(db_candidates)}건, Tavily {len(tavily_candidates)}건"
    )
    print(f"{'=' * 60}")

    # 신뢰도 순서(나라장터API > DB캐시 > Tavily)로 합치고 이름 중복 제거.
    # 검증된 정부 데이터를 웹검색 결과보다 우선 채택.
    candidates = []
    seen_keys = set()
    for c in api_candidates + db_candidates + tavily_candidates:
        key = _dedup_key(c["name"])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        candidates.append(c)

    cap = target_count * 3
    if len(candidates) > cap:
        print(f"[병합+중복제거] {len(candidates)}건 -> {cap}건으로 제한 (나머지는 enrichment 생략)")
        candidates = candidates[:cap]
    else:
        print(f"[병합+중복제거] {len(candidates)}건")

    log_status_change(
        case_id, "collected",
        reason=(
            f"3소스 수집+병합 완료: 나라장터API {len(api_candidates)}건, "
            f"DB캐시 {len(db_candidates)}건, Tavily {len(tavily_candidates)}건, "
            f"병합 후 {len(candidates)}건"
        ),
    )

    results = enrich_candidates(
        candidates, item_name=normalized, target_count=target_count, batch_size=5, case_id=case_id
    )

    print(f"\n{'=' * 60}")
    print(f"[최종 결과] {len(results)}/{target_count}개 확보")
    print(f"{'=' * 60}")

    save_to_cache(normalized, results)

    log_status_change(
        case_id, "search_completed",
        reason=f"'{normalized}' 신규탐색 {len(results)}/{target_count}개 확보",
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
