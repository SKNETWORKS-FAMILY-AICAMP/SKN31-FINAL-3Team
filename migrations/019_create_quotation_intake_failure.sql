-- 견적서를 받았는데 자동으로 읽지 못한(파싱/추출 실패) 기록.
-- 이메일 회신 첨부를 RunPod으로 추출하다 실패하면 Supplier Quotation 자체가
-- 만들어지지 않아서, 화면에서는 그 협력사가 '미회신'으로만 보였다.
-- 실패를 여기에 남겨 협력사 선정 화면 맨 아래에 "견적서를 읽지 못했습니다 -
-- 원본 파일을 확인해 주세요"로 보여준다. 문서 본문이나 API 키는 저장하지 않는다.
CREATE TABLE IF NOT EXISTS procurement.quotation_intake_failure (
    failure_id BIGSERIAL PRIMARY KEY,
    rfq_name TEXT NOT NULL,
    supplier_id TEXT,
    supplier_name TEXT,
    source_filename TEXT,
    file_id TEXT,
    communication_name TEXT,
    failure_kind TEXT NOT NULL
        CHECK (failure_kind IN ('parse', 'arithmetic', 'extraction')),
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS quotation_intake_failure_source_uidx
    ON procurement.quotation_intake_failure
       (rfq_name, (COALESCE(communication_name, '')), (COALESCE(file_id, '')));
CREATE INDEX IF NOT EXISTS quotation_intake_failure_rfq_idx
    ON procurement.quotation_intake_failure (rfq_name, created_at DESC);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'biddingflow_team') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
        ON procurement.quotation_intake_failure TO biddingflow_team;
        GRANT USAGE, SELECT ON SEQUENCE procurement.quotation_intake_failure_failure_id_seq
        TO biddingflow_team;
    END IF;
END
$$;
