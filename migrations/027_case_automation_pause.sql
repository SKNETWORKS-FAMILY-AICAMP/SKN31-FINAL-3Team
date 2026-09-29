-- Per-case stop switch. Never mutates the company's versioned policy.
ALTER TABLE procurement.procurement_case
    ADD COLUMN IF NOT EXISTS automation_paused boolean NOT NULL DEFAULT false;

-- Rebid reuse searches by SQ identity and complete semantic input, not RFQ ID.
CREATE INDEX IF NOT EXISTS idx_spec_cache_quotation_source_hash
    ON procurement.quotation_specification_cache (quotation_id, evaluation_source, input_hash);
