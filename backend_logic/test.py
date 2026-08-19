"""
test_vendor_search.py — RAG검색 + Tavily 연락처보강까지 전체 흐름 테스트

실행: python test_vendor_search.py
"""

from pipeline_nodes import rag_search_past_vendors, _enrich_contact_info

TEST_QUERIES = ["안전모", "냄비", "사무용 의자"]

for query in TEST_QUERIES:
    print(f"\n{'='*50}")
    print(f"검색어: '{query}'")
    print("=" * 50)

    try:
        results = rag_search_past_vendors(query)
    except Exception as e:
        print(f"❌ 에러: {e}")
        continue

    if not results:
        print("검색결과 없음")
        continue

    # ⚠️ RAG 결과 그대로는 email/phone이 비어있는 게 정상 (DB에 없으니까).
    # 여기서 Tavily로 연락처를 보강해야 실제 파이프라인이랑 같은 결과가 나옴.
    # 부담 줄이려고 상위 2개만 보강 테스트 (전체 다 하면 Tavily 호출이 많아짐)
    for r in results[:2]:
        r = _enrich_contact_info(r)
        print(f"  - {r['name']} ({r['category']}) | {r['address']} | email={r.get('email')} | phone={r.get('phone')}")

    for r in results[2:]:
        print(f"  - {r['name']} ({r['category']}) | {r['address']} | (연락처 보강 생략, RAG 원본만)")

print(f"\n{'='*50}")
print("완료 — 상위 2개는 email/phone까지 채워졌는지, 나머지는 카테고리 매칭이 의미상 맞는지 확인")