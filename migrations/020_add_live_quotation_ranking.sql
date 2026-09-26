-- 견적 순위를 그래프(체크포인트) 밖에서도 볼 수 있게 케이스에 실시간 순위를 둔다.
-- 견적이 도착할 때마다(웹훅) 전체 차수 견적으로 다시 계산해 덮어쓴다.
-- ⚠️ 이 컬럼 갱신은 procurement_case.version을 올리지 않는다 - 그래프 쪽
-- update_case(expected_version)와 충돌(CaseConflictError)을 만들지 않기 위해서다.
ALTER TABLE procurement.procurement_case
    ADD COLUMN IF NOT EXISTS live_quotation_ranking JSONB;
ALTER TABLE procurement.procurement_case
    ADD COLUMN IF NOT EXISTS live_quotation_ranking_at TIMESTAMPTZ;
