-- 자동 진행 제어용 컬럼.
--
-- automation_hold: 담당자가 "이 건은 내가 볼 테니 자동으로 진행하지 마라"고
-- 세워둔 상태. 그래프 state가 아니라 케이스 테이블에 두는 이유는, 그래프가
-- 멈춰 있는 동안에도 보류를 걸 수 있어야 하기 때문이다(그래프 state에 두면
-- 보류를 걸려고 그래프를 깨워야 하는 모순이 생긴다).
--
-- auto_progress_signature: 마지막으로 자동 진행을 시도했다가 조건에 걸린
-- 시점의 상황 지문. 상황이 그대로면 스캔 잡이 같은 건을 10분마다 다시
-- 평가하며 알림을 쌓지 않는다. 견적이 더 들어오거나 마감이 연장되면
-- 지문이 바뀌어 다시 평가된다.
ALTER TABLE procurement.procurement_case
    ADD COLUMN IF NOT EXISTS automation_hold BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE procurement.procurement_case
    ADD COLUMN IF NOT EXISTS automation_hold_reason TEXT;
ALTER TABLE procurement.procurement_case
    ADD COLUMN IF NOT EXISTS automation_hold_by VARCHAR(140);
ALTER TABLE procurement.procurement_case
    ADD COLUMN IF NOT EXISTS automation_hold_at TIMESTAMPTZ;
ALTER TABLE procurement.procurement_case
    ADD COLUMN IF NOT EXISTS auto_progress_signature TEXT;
ALTER TABLE procurement.procurement_case
    ADD COLUMN IF NOT EXISTS auto_progress_at TIMESTAMPTZ;
ALTER TABLE procurement.procurement_case
    ADD COLUMN IF NOT EXISTS auto_deadline_extended_at TIMESTAMPTZ;
