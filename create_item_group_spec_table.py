"""
create_item_group_spec_table.py - item_group별 필수규격 정의 테이블 생성.
IF NOT EXISTS라 여러 번 실행해도 안전 - 1회만 실행하면 됨.

backend_logic2/nodes/item/item_spec_validation.py(신규품목 규격검증)가
씀 - MR LangGraph 파이프라인(process_graph.py)과는 완전히 독립된 프로세스.

required_specs는 다른 procurement 테이블들과 같은 이유로 JSONB 대신 TEXT에
JSON 문자열로 저장(Jsonb() 래퍼 없이 단순하게, json.dumps/json.loads로
파이썬 쪽에서 직접 처리).

사용법 (레포 루트에서):
    python create_item_group_spec_table.py
"""

from procurement_db import get_connection

DDL = """
CREATE TABLE IF NOT EXISTS procurement.item_group_spec_requirements (
    item_group TEXT PRIMARY KEY,
    required_specs TEXT NOT NULL,
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def main():
    with get_connection(autocommit=True) as conn:
        conn.execute(DDL)
    print("완료: procurement.item_group_spec_requirements 테이블 준비됨")


if __name__ == "__main__":
    main()
