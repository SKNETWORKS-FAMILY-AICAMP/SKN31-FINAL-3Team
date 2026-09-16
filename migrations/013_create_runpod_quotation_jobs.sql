-- Persist submission and completion independently of the API process lifetime.
CREATE TABLE procurement.quotation_extraction_job (
    job_id UUID PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    endpoint_id TEXT NOT NULL,
    runpod_job_id TEXT UNIQUE,
    status TEXT NOT NULL DEFAULT 'SUBMITTING'
        CHECK (status IN ('SUBMITTING', 'SUBMITTED', 'READY', 'REGISTERED',
                          'COMPLETED', 'FAILED', 'SUBMISSION_UNKNOWN')),
    context JSONB NOT NULL,
    prompt_sha256 TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    result_json JSONB,
    registration_json JSONB,
    last_error TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    callback_at TIMESTAMPTZ,
    last_checked_at TIMESTAMPTZ,
    next_check_at TIMESTAMPTZ NOT NULL DEFAULT now() + interval '120 seconds',
    processed_at TIMESTAMPTZ
);
CREATE INDEX quotation_extraction_job_pending_idx
    ON procurement.quotation_extraction_job (next_check_at)
    WHERE status IN ('SUBMITTING', 'SUBMITTED', 'READY', 'REGISTERED');
COMMENT ON TABLE procurement.quotation_extraction_job IS
    'RunPod quotation inbox; request context/results only, no API keys or document base64';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'biddingflow_team') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
        ON procurement.quotation_extraction_job TO biddingflow_team;
    END IF;
END
$$;
