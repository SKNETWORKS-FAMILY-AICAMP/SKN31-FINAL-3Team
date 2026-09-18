CREATE TABLE procurement.quotation_specification_cache (
    rfq_name TEXT NOT NULL,
    quotation_id TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    evaluation_source TEXT NOT NULL,
    assessment_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (rfq_name, quotation_id, input_hash, evaluation_source)
);

CREATE INDEX quotation_specification_cache_lookup_idx
    ON procurement.quotation_specification_cache
       (rfq_name, quotation_id, evaluation_source, updated_at DESC);

COMMENT ON TABLE procurement.quotation_specification_cache IS
    'Reusable AI specification scores/reasons keyed by RFQ, quotation and canonical model-input hash';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'biddingflow_team') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
        ON procurement.quotation_specification_cache TO biddingflow_team;
    END IF;
END
$$;
