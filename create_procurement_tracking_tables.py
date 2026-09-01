"""
create_procurement_tracking_tables.py - procurement_case / case_status_history /
ai_decision_log 3개 테이블 생성 (MR 단위 재설계, 2026-08-31).

2026-08-31 재설계 히스토리:
  1차: 품목(item_code) 단위로 케이스를 만드는 구조로 설계했었음.
  2차(이 버전): 실제 `--mr` CLI(backend_logic2/workflow/process_cli.py)를
    까보니 전체 프로세스가 MR(mr_name) 단위로 돎(thread_id=MR명, 재개도
    같은 thread로 이어짐) - 대체품확인/비딩판정 같은 최상위 단계는 특정
    item_code 하나에 안 묶임. 사용자 확인 결과 "MR 1건 = 품목 1건"이라
    item_code 컬럼을 mr_name으로 바꿔서 "MR 1건 = 케이스 1건"으로
    재설계함. supplier_performance 테이블은 만들지 않기로 함(아직 실적이
    실제로 발생하는 단계가 없어서 불필요한 조기설계로 판단).

설계:
  - procurement_case가 허브. case_status_history/ai_decision_log가
    case_id로 참조(ai_decision_log는 nullable - 케이스 없이 단독 실행
    테스트해도 안 죽게).
  - 케이스 생성은 process_graph.py의 route_entrypoint_command(그래프
    맨 처음 노드, MR당 딱 1번만 실행됨) 한 곳에서만 함 - 이후 모든 노드는
    같은 case_id를 state로 넘겨받아 재사용.
  - case_status_history/ai_decision_log 둘 다 JSONB metadata 컬럼 없음 -
    구조화된 부가정보 대신 reason(TEXT) 한 줄로 다 담기로 함(사용자
    요청: "이유도 기록을 해" 정도로 단순하게).

사용법 (레포 루트에서):
    python create_procurement_tracking_tables.py
"""

from procurement_db import get_connection

DDL = """
CREATE TABLE IF NOT EXISTS procurement.procurement_case (
    case_id UUID PRIMARY KEY,
    mr_name TEXT,
    status TEXT NOT NULL DEFAULT 'created',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_procurement_case_mr_name
    ON procurement.procurement_case (mr_name);
CREATE INDEX IF NOT EXISTS idx_procurement_case_status
    ON procurement.procurement_case (status);

CREATE TABLE IF NOT EXISTS procurement.case_status_history (
    id BIGSERIAL PRIMARY KEY,
    case_id UUID NOT NULL REFERENCES procurement.procurement_case(case_id) ON DELETE CASCADE,
    from_status TEXT,
    to_status TEXT NOT NULL,
    reason TEXT,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_case_status_history_case_id
    ON procurement.case_status_history (case_id, occurred_at);

CREATE TABLE IF NOT EXISTS procurement.ai_decision_log (
    id BIGSERIAL PRIMARY KEY,
    case_id UUID REFERENCES procurement.procurement_case(case_id) ON DELETE SET NULL,
    node TEXT NOT NULL,
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ai_decision_log_case_id
    ON procurement.ai_decision_log (case_id);
CREATE INDEX IF NOT EXISTS idx_ai_decision_log_node
    ON procurement.ai_decision_log (node, created_at);
"""


def main():
    with get_connection(autocommit=True) as conn:
        conn.execute(DDL)
    print("완료: procurement_case / case_status_history / ai_decision_log 준비됨 (MR 단위 구조)")


if __name__ == "__main__":
    main()
