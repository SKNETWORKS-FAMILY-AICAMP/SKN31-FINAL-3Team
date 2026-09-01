"""
create_supplier_search_cache_table.py - supplier_search 캐시 테이블 생성.
IF NOT EXISTS라 여러 번 실행해도 안전 - 1회만 실행하면 됨.

사용법 (레포 루트에서):
    python create_supplier_search_cache_table.py
"""

from procurement_db import get_connection

DDL = """
CREATE TABLE IF NOT EXISTS procurement.supplier_search_cache (
    id BIGSERIAL PRIMARY KEY,
    normalized_item_name TEXT NOT NULL,
    company_name TEXT NOT NULL,
    site_url TEXT,
    phone TEXT,
    email TEXT,
    source TEXT,
    cached_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    UNIQUE (normalized_item_name, company_name)
);

CREATE INDEX IF NOT EXISTS idx_supplier_search_cache_lookup
    ON procurement.supplier_search_cache (normalized_item_name, expires_at);

CREATE INDEX IF NOT EXISTS idx_supplier_search_cache_expiry
    ON procurement.supplier_search_cache (expires_at);
"""


def main():
    with get_connection(autocommit=True) as conn:
        conn.execute(DDL)
    print("완료: procurement.supplier_search_cache 테이블 준비됨 (이미 있었으면 그대로 유지)")


if __name__ == "__main__":
    main()
