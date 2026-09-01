"""
procurement.supplier_search_cache 데이터 미리보기 (캐시 잘 쌓이는지 확인용).

사용법 (레포 루트에서):
    python peek_supplier_search_cache.py                              # 기본 20건, 최신순
    python peek_supplier_search_cache.py 50                           # 50건
    python peek_supplier_search_cache.py 10 --where "normalized_item_name = '멀티탭'"
"""

import sys

from procurement_db import get_connection

TABLE = "procurement.supplier_search_cache"


def parse_args():
    limit = 20
    where = None
    args = sys.argv[1:]
    if args and args[0].isdigit():
        limit = int(args[0])
        args = args[1:]
    if args and args[0] == "--where" and len(args) > 1:
        where = args[1]
    return limit, where


def main():
    limit, where = parse_args()

    with get_connection(autocommit=True) as connection:
        total = connection.execute(
            f"SELECT COUNT(*) AS cnt FROM {TABLE}"
        ).fetchone()["cnt"]
        expired = connection.execute(
            f"SELECT COUNT(*) AS cnt FROM {TABLE} WHERE expires_at < now()"
        ).fetchone()["cnt"]
        print(f"전체 행 수: {total:,}건 (그 중 만료됨: {expired:,}건 - 다음 검색 실행시 자동 삭제됨)\n")

        query = f"""
            SELECT normalized_item_name, company_name, site_url, phone, email,
                   source, cached_at, expires_at,
                   (expires_at < now()) AS is_expired
            FROM {TABLE}
        """
        params = ()
        if where:
            query += f" WHERE {where}"
        query += " ORDER BY cached_at DESC"
        query += f" LIMIT {limit}"

        rows = connection.execute(query, params).fetchall()

        if not rows:
            print("조건에 맞는 행이 없습니다.")
            return

        columns = list(rows[0].keys())
        print(f"컬럼 ({len(columns)}개): {', '.join(columns)}\n")

        for i, row in enumerate(rows, start=1):
            print(f"--- {i} ---")
            for col in columns:
                print(f"  {col}: {row[col]}")
            print()


if __name__ == "__main__":
    main()
