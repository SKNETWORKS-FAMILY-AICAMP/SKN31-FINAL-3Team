"""
procurement 스키마 테이블 구조(컬럼) 확인 - 기존 테이블(ai_decision_log,
case_status_history 등)에 INSERT 코드를 짜기 전에 실제 컬럼명/타입을
짐작하지 않고 확인하기 위한 스크립트.

사용법 (레포 루트에서):
    python describe_tables.py                                    # 기본 4개 테이블
    python describe_tables.py ai_decision_log case_status_history # 특정 테이블만
"""

import sys

from procurement_db import get_connection

DEFAULT_TABLES = [
    "ai_decision_log",
    "case_status_history",
    "procurement_case",
    "supplier_performance",
]


def main():
    tables = sys.argv[1:] or DEFAULT_TABLES

    with get_connection(autocommit=True) as connection:
        for table in tables:
            print(f"\n{'=' * 60}")
            print(f"procurement.{table}")
            print(f"{'=' * 60}")

            columns = connection.execute(
                """
                SELECT column_name, data_type, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema = 'procurement' AND table_name = %(t)s
                ORDER BY ordinal_position
                """,
                {"t": table},
            ).fetchall()

            if not columns:
                print("  (테이블이 없거나 컬럼 조회 실패)")
                continue

            for c in columns:
                nullable = "NULL" if c["is_nullable"] == "YES" else "NOT NULL"
                default = f" DEFAULT {c['column_default']}" if c["column_default"] else ""
                print(f"  {c['column_name']}: {c['data_type']} {nullable}{default}")

            # 외래키/참조 관계도 같이 확인 (case_id 같은 게 어느 테이블을 가리키는지 파악용)
            fks = connection.execute(
                """
                SELECT
                    kcu.column_name,
                    ccu.table_schema AS foreign_table_schema,
                    ccu.table_name AS foreign_table_name,
                    ccu.column_name AS foreign_column_name
                FROM information_schema.table_constraints AS tc
                JOIN information_schema.key_column_usage AS kcu
                    ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
                JOIN information_schema.constraint_column_usage AS ccu
                    ON ccu.constraint_name = tc.constraint_name AND ccu.table_schema = tc.table_schema
                WHERE tc.constraint_type = 'FOREIGN KEY'
                    AND tc.table_schema = 'procurement' AND tc.table_name = %(t)s
                """,
                {"t": table},
            ).fetchall()
            if fks:
                print("\n  외래키:")
                for fk in fks:
                    print(f"    {fk['column_name']} -> {fk['foreign_table_schema']}.{fk['foreign_table_name']}.{fk['foreign_column_name']}")

            total = connection.execute(
                f"SELECT COUNT(*) AS cnt FROM procurement.{table}"
            ).fetchone()["cnt"]
            print(f"\n  전체 행 수: {total:,}건")

            if total:
                sample = connection.execute(
                    f"SELECT * FROM procurement.{table} ORDER BY 1 LIMIT 1"
                ).fetchone()
                print("  샘플 행 1개:")
                for k, v in sample.items():
                    print(f"    {k}: {v}")


if __name__ == "__main__":
    main()
