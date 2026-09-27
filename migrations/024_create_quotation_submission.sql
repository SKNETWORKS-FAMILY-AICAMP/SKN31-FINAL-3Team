-- 협력사가 견적을 "낸 시각"을 남긴다.
--
-- 왜 필요한가. 마감이 지났는지 판정할 때 기준이 되는 건 협력사가 낸 시각인데,
-- 지금은 그게 어디에도 남지 않는다. 남는 건 Supplier Quotation이 ERPNext에
-- 만들어진 시각뿐이고, 이메일 회신은 첨부를 RunPod으로 읽어 등록하기까지
-- 시간이 걸린다. 그 시간 때문에 마감 전에 낸 견적이 마감 후에 낸 것으로
-- 보이면 멀쩡한 견적을 버리게 된다.
--
-- 그래서 두 경로의 제출 시각을 여기 따로 기록한다.
--   portal : Supplier Quotation.creation (포털에서 직접 작성)
--   email  : 회신 메일의 communication_date (없으면 creation)
--
-- ⚠️ 한 번 기록한 제출 시각은 절대 뒤로 미루지 않는다(LEAST). 같은 견적에
-- 대해 SQ 변경 웹훅이 여러 번 오는데, 그때마다 SQ 생성 시각으로 덮어쓰면
-- 애써 기록한 메일 시각이 날아간다. 같은 이유로 source는 email이 이긴다 -
-- 이메일로 들어온 건은 SQ 생성 시각보다 메일 시각이 항상 진실에 가깝다.
CREATE TABLE IF NOT EXISTS procurement.quotation_submission (
    quotation_name TEXT PRIMARY KEY,
    rfq_name TEXT NOT NULL,
    supplier_id TEXT,
    supplier_name TEXT,
    source TEXT NOT NULL CHECK (source IN ('portal', 'email')),
    -- 협력사가 낸 시각. 마감 판정의 기준.
    submitted_at TIMESTAMPTZ NOT NULL,
    -- 우리가 처리(등록)한 시각. 지연이 얼마였는지 나중에 볼 수 있게 남긴다.
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    communication_name TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS quotation_submission_rfq_idx
    ON procurement.quotation_submission (rfq_name, submitted_at);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'biddingflow_team') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
        ON procurement.quotation_submission TO biddingflow_team;
    END IF;
END
$$;
