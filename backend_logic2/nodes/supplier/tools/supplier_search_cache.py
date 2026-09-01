"""
tools/supplier_search_cache.py - 신규탐색(3소스 병렬 수집 + enrichment) 결과 캐싱.

검색 1번 돌리는 데 나라장터API 4개 병렬호출 + 정부DB 조회 + 네이버검색
+ Jina Reader 폴백 + AI 배치호출 여러 번(관련성판단, 3단계 재시도)까지
겹쳐서 시간/API비용이 꽤 드는 게 실측 확인됨('멀티탭' 테스트 기준 수십
초대). 같은 품목명으로 다시 검색할 땐 DB에 캐싱해둔 결과를 그대로
재사용해서 이 전체 파이프라인(3소스 수집 + 1~3단계 enrichment)을
통째로 생략함.

캐시 테이블: procurement.supplier_search_cache (create_supplier_search_cache_table.py로
1회 생성 필요).

캐시 키: 정규화된 품목명(normalized_item_name, supplier_search.py가
normalize_item_name()으로 만든 값을 그대로 씀 - "목재"든 "원목 목재"든
같은 정규화 결과면 같은 캐시를 씀) + 회사명(company_name) 조합. 같은
품목명으로 재검색하면 그 품목명에 걸린 캐시 행을 전부 반환(안 만료
됐으면). 저장할 땐 (품목명, 회사명) UNIQUE 제약으로 upsert - 재검색
때 같은 회사가 다시 나오면 최신 정보로 갱신 + 만료시간 연장.

TTL(만료기한): 기본 30일. 회사 연락처(이메일/전화)는 자주 안 바뀌지만
사이트 URL은 시간 지나면 죽거나 바뀔 수 있어서 너무 길게 잡지 않음 -
필요하면 CACHE_TTL_DAYS만 바꾸면 됨.

만료된 행 삭제: 별도 스케줄러 없이도 알아서 정리되게, 검색이 실행될
때마다(supplier_search()가 cleanup_expired_cache()를 맨 먼저 호출)
전체 만료 행을 지우고 시작함(expires_at 인덱스 기반 DELETE라 비용
크지 않음) - "그 시간 지나면 지워지게" 요건대로 실제로 행 자체를 삭제.

이메일/전화 둘 다 없는 결과는 캐싱 안 함 - 재검색해도 다음번에 또
못 찾을 확률이 높고, 캐싱해봤자 다음 검색에 도움이 안 됨(오히려
"이미 찾아봤는데 없더라"를 캐싱하면 다음 검색에서 재시도 기회 자체가
없어짐).

.env 필요: NEXTERP_DATABASE_URL (procurement_db 모듈 경유)
"""

import datetime

CACHE_TTL_DAYS = 30

TABLE = "procurement.supplier_search_cache"


def cleanup_expired_cache():
    """만료된 캐시 행을 전부 삭제. 검색 시작할 때마다 호출해서 알아서 정리되게 함."""
    try:
        from procurement_db import get_connection
    except ImportError:
        return

    try:
        with get_connection(autocommit=True) as conn:
            deleted = conn.execute(
                f"DELETE FROM {TABLE} WHERE expires_at < now() RETURNING id"
            ).fetchall()
        if deleted:
            print(f"    [캐시 정리] 만료된 {len(deleted)}건 삭제")
    except Exception as e:
        print(f"    [캐시 정리 실패, 무시하고 진행]: {e}")


def get_cached_results(normalized_item_name):
    """
    캐시 조회. normalized_item_name에 해당하는, 아직 안 만료된 행을 전부
    반환(회사명/사이트/전화/이메일/원래출처). 비어있으면 캐시 미스 -
    호출부(supplier_search.py)가 빈 리스트면 신규탐색으로 넘어감.
    """
    try:
        from procurement_db import get_connection
    except ImportError:
        print("    [캐시] procurement_db 모듈을 못 찾음, 캐시 건너뜀")
        return []

    try:
        with get_connection(autocommit=True) as conn:
            rows = conn.execute(
                f"""
                SELECT company_name, site_url, phone, email, source, cached_at
                FROM {TABLE}
                WHERE normalized_item_name = %(name)s
                  AND expires_at > now()
                ORDER BY cached_at DESC
                """,
                {"name": normalized_item_name},
            ).fetchall()
    except Exception as e:
        print(f"    [캐시 조회 실패, 무시하고 진행]: {e}")
        return []

    return [
        {
            "name": row["company_name"],
            "site_url": row["site_url"],
            "phone": row["phone"],
            "email": row["email"],
            "source": row["source"] or "cache",
            "operation": f"cache({row['source'] or '?'}, {row['cached_at']:%Y-%m-%d} 저장)",
            "raw": {},
        }
        for row in rows
    ]


def save_to_cache(normalized_item_name, results):
    """
    검색 결과를 캐시에 저장(upsert). results: enrich_candidates()가
    반환하는 최종 후보 리스트(각 항목에 name/email/phone/site_url 필요).
    """
    cacheable = [r for r in results if r.get("email") or r.get("phone")]
    if not cacheable:
        return

    try:
        from procurement_db import get_connection
    except ImportError:
        return

    expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=CACHE_TTL_DAYS)

    try:
        with get_connection(autocommit=True) as conn:
            for r in cacheable:
                conn.execute(
                    f"""
                    INSERT INTO {TABLE}
                        (normalized_item_name, company_name, site_url, phone, email, source, cached_at, expires_at)
                    VALUES
                        (%(item_name)s, %(company_name)s, %(site_url)s, %(phone)s, %(email)s, %(source)s, now(), %(expires_at)s)
                    ON CONFLICT (normalized_item_name, company_name)
                    DO UPDATE SET
                        site_url = EXCLUDED.site_url,
                        phone = EXCLUDED.phone,
                        email = EXCLUDED.email,
                        source = EXCLUDED.source,
                        cached_at = now(),
                        expires_at = EXCLUDED.expires_at
                    """,
                    {
                        "item_name": normalized_item_name,
                        "company_name": r["name"],
                        "site_url": r.get("site_url"),
                        "phone": r.get("phone"),
                        "email": r.get("email"),
                        "source": r.get("source"),
                        "expires_at": expires_at,
                    },
                )
        print(f"    [캐시 저장] '{normalized_item_name}' {len(cacheable)}건 저장 (TTL {CACHE_TTL_DAYS}일)")
    except Exception as e:
        print(f"    [캐시 저장 실패, 무시하고 진행]: {e}")
