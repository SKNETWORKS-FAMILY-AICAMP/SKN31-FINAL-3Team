"""
test_tavily_vendor_search.py — RAG 안 쓰고 Tavily만으로 후보군 찾는 것 테스트

실행: python test_tavily_vendor_search.py
"""

from pipeline_nodes import tavily_search_vendors, _enrich_contact_info

TEST_QUERIES = ["안전모", "냄비", "사무용 의자"]

for query in TEST_QUERIES:
    print(f"\n{'='*50}")
    print(f"검색어: '{query}' (Tavily 단독 검색)")
    print("=" * 50)

    try:
        candidates = tavily_search_vendors(query)
    except Exception as e:
        print(f"❌ 에러: {e}")
        continue

    if not candidates:
        print("검색결과 없음")
        continue

    print(f"{len(candidates)}건 발견\n")

    # 상위 2개만 연락처 보강까지 테스트 (Tavily 호출 부담 줄이려고)
    for c in candidates[:2]:
        c = _enrich_contact_info(c)
        print(f"  - {c['name']}")
        print(f"      출처: {c.get('source_url')}")
        print(f"      email={c.get('email')} | phone={c.get('phone')}")

    for c in candidates[2:]:
        print(f"  - {c['name']} (연락처 보강 생략)")
        print(f"      출처: {c.get('source_url')}")

print(f"\n{'='*50}")
print("완료")