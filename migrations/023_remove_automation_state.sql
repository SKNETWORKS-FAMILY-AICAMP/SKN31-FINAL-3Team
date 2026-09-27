-- 자동 진행(조건부 자동화)을 되돌리면서 그 흔적을 데이터에서도 걷어낸다.
--
-- ⚠️ 정책 키 정리가 없으면 앱이 뜨자마자 깨진다. PurchasingRules는
-- StrictModel(extra="forbid")이라, 스키마에 없는 키가 저장된 정책에 남아
-- 있으면 정책을 읽는 모든 경로가 ValidationError로 실패한다.
-- company_policy_version은 append-only지만 case_policy가 과거 버전을 그대로
-- 가리키므로 지난 버전까지 전부 손봐야 한다.
--
-- 자동 진행을 다시 만들 때는 키와 컬럼을 새 마이그레이션으로 추가하면 된다.
-- 모든 문장이 IF EXISTS / 조건부라, 이미 정리된 DB에서 다시 돌려도 안전하고
-- 자동화를 겪은 적 없는 새 DB에서도 그대로 통과한다.
UPDATE procurement.company_policy_version
SET policy = jsonb_set(
        policy,
        '{rules}',
        (policy -> 'rules')
            - 'automation_mode'
            - 'auto_rfq_dispatch'
            - 'auto_final_selection'
            - 'auto_selection_min_quotations'
            - 'auto_selection_score_gap'
            - 'auto_selection_max_amount'
            - 'auto_deadline_extension_days'
            - 'auto_deadline_extension_min_lead_days'
    )
WHERE policy -> 'rules' ?| ARRAY[
        'automation_mode',
        'auto_rfq_dispatch',
        'auto_final_selection',
        'auto_selection_min_quotations',
        'auto_selection_score_gap',
        'auto_selection_max_amount',
        'auto_deadline_extension_days',
        'auto_deadline_extension_min_lead_days'
    ];

-- 021/022가 만든 것들. 되돌린 코드는 더 이상 쓰지 않으므로 DB도 같이 맞춘다.
ALTER TABLE procurement.procurement_case
    DROP COLUMN IF EXISTS automation_hold,
    DROP COLUMN IF EXISTS automation_hold_reason,
    DROP COLUMN IF EXISTS automation_hold_by,
    DROP COLUMN IF EXISTS automation_hold_at,
    DROP COLUMN IF EXISTS auto_progress_signature,
    DROP COLUMN IF EXISTS auto_progress_at,
    DROP COLUMN IF EXISTS auto_deadline_extended_at;

DROP TABLE IF EXISTS procurement.automation_heartbeat;
