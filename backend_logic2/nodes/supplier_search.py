"""
supplier_search.py - 최종 통합 파이프라인 (tools/ 밖, 최상위 실행파일).

1순위: 나라장터 9개 오퍼레이션 캐스케이드 (tools/narajangteo_search_based_tool.py)
   - 홈페이지 없는 후보는 통째로 버림
   - 있으면 네이버/Tavily와 동일한 검증로직(이메일+전화+관련성판단 AI 1번)
   - 목표개수 채우면 여기서 끝

2순위(폴백): 1순위로 목표개수를 못 채웠을 때만
   Tavily+네이버 파이프라인(tools/web_search_based_tool.py) 실행,
   부족분만큼 채워서 합침

폴더 구조:
  supplier_search.py (이 파일)
  tools/
    narajangteo_search_based_tool.py
    web_search_based_tool.py
    naver_contact_enrichment.py

.env 필요: DATA_GO_KR_SERVICE_KEY, TAVILY_API_KEY, NAVER_CLIENT_ID,
NAVER_CLIENT_SECRET, OPENAI_API_KEY

실행: python supplier_search.py
"""

try:
    from .tools.narajangteo_search_based_tool import search_all_with_detail
    from .tools.web_search_based_tool import normalize_item_name, tavily_search_vendors
except ImportError:  # nodes 폴더에서 직접 실행할 때
    from tools.narajangteo_search_based_tool import search_all_with_detail
    from tools.web_search_based_tool import normalize_item_name, tavily_search_vendors


def supplier_search(item_name, target_count=10):
    print(f"\n{'=' * 60}")
    print(f"품목명 정규화 중...")
    print(f"{'=' * 60}")
    normalized = normalize_item_name(item_name)

    print(f"\n{'=' * 60}")
    print(f"[1순위] 나라장터 캐스케이드 검색 (narajangteo_search_based_tool)")
    print(f"{'=' * 60}")
    narajangteo_results = search_all_with_detail(normalized, target_count=target_count)

    all_candidates = []
    for r in narajangteo_results:
        all_candidates.append({
            "name": r["name"], "email": r.get("email"), "phone": r.get("phone"),
            "site_url": r.get("homepage"), "source": r["operation"],
        })

    print(f"\n{'=' * 60}")
    print(f"[1순위 결과] {len(all_candidates)}/{target_count}개 확보")
    print(f"{'=' * 60}")

    if len(all_candidates) >= target_count:
        print(f"\n목표 개수 채움, 2순위(web_search_based_tool) 생략")
        return all_candidates[:target_count]

    remaining = target_count - len(all_candidates)
    print(f"\n부족분 {remaining}개, 2순위(web_search_based_tool)로 보충 시도")

    print(f"\n{'=' * 60}")
    print(f"[2순위] Tavily + 네이버 검색 (web_search_based_tool)")
    print(f"{'=' * 60}")
    web_results = tavily_search_vendors(normalized, target_count=remaining, max_results_per_query=remaining)

    print(f"\n{'=' * 60}")
    print(f"[2순위 결과] {len(web_results)}개 추가 확보")
    print(f"{'=' * 60}")

    all_candidates.extend(web_results)
    return all_candidates[:target_count]


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
        print(f"\n{r['name']}  [출처: {r['source']}]")
        print(f"  이메일: {r.get('email') or '(없음)'}")
        print(f"  전화: {r.get('phone') or '(없음)'}")
        print(f"  사이트: {r.get('site_url') or '(없음)'}")
