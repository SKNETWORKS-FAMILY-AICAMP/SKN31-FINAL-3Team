-- Durable, opt-in deadline dispatch. No ERP writes or changes to approval policy.
CREATE TABLE procurement.quotation_deadline_control (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    enabled BOOLEAN NOT NULL DEFAULT false,
    changed_by TEXT NOT NULL DEFAULT 'migration',
    reason TEXT NOT NULL DEFAULT '안전 검증 후 관리자가 활성화',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO procurement.quotation_deadline_control(singleton) VALUES (true);

CREATE TABLE procurement.quotation_deadline_job (
    case_id UUID PRIMARY KEY REFERENCES procurement.procurement_case(case_id) ON DELETE CASCADE,
    input_hash TEXT NOT NULL,
    rfq_name TEXT NOT NULL,
    due_at TIMESTAMPTZ NOT NULL,
    next_attempt_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN
        ('PENDING', 'CHECKING', 'READY', 'RUNNING', 'WAITING', 'DONE', 'BLOCKED', 'UNCERTAIN')),
    attempts INTEGER NOT NULL DEFAULT 0,
    claim_token UUID,
    lease_until TIMESTAMPTZ,
    prepared JSONB,
    reason TEXT,
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX quotation_deadline_due_idx ON procurement.quotation_deadline_job(next_attempt_at)
    WHERE status IN ('PENDING', 'READY');
CREATE INDEX quotation_extraction_rfq_idx ON procurement.quotation_extraction_job((context->>'rfq_name'));
CREATE TABLE procurement.quotation_deadline_revision (
    rfq_name TEXT PRIMARY KEY, revision BIGINT NOT NULL DEFAULT 1
);

-- Fingerprint business inputs, not case.updated_at/version (projection changes those).
-- All preparation gates read this DB view; an unchanged WAITING/DONE case is never
-- re-read from ERP. Cache input_hash + model + timestamp are included, not just counts.
CREATE VIEW procurement.quotation_deadline_input AS
SELECT c.case_id, c.workflow_snapshot, c.quotation_snapshot,
       c.workflow_snapshot #>> '{values,rfq_name}' AS rfq_name,
       COALESCE(extracts.unfinished, 0) AS unfinished_extractions,
       md5(jsonb_build_object(
           'rfq', c.workflow_snapshot #> '{values,rfq_name}',
           'rounds', c.workflow_snapshot #> '{values,rfq_rounds}',
           'deadline', c.workflow_snapshot #> '{values,quotation_deadline}',
           'quotes', c.quotation_snapshot,
           'policy', cp.version,
           'events', signals.revisions,
           'specifications', specs.revisions,
           'submissions', submitted.revisions,
           'extractions', extracts.revisions,
           'failures', failures.revisions
       )::text) AS input_hash
FROM procurement.procurement_case c
LEFT JOIN procurement.case_policy cp USING(case_id)
CROSS JOIN LATERAL (
    SELECT ARRAY(
        SELECT DISTINCT name FROM (
            SELECT c.workflow_snapshot #>> '{values,rfq_name}' AS name
            UNION ALL
            SELECT value->>'rfq_name' FROM jsonb_array_elements(CASE
                WHEN jsonb_typeof(c.workflow_snapshot #> '{values,rfq_rounds}')='array'
                THEN c.workflow_snapshot #> '{values,rfq_rounds}' ELSE '[]'::jsonb END)
        ) names WHERE name IS NOT NULL AND name <> ''
    ) AS names
) rfqs
LEFT JOIN LATERAL (
    SELECT jsonb_agg(jsonb_build_array(rfq_name, quotation_id, input_hash,
        evaluation_source, assessment_json) ORDER BY rfq_name, quotation_id, input_hash, evaluation_source) AS revisions
    FROM procurement.quotation_specification_cache WHERE rfq_name = ANY(rfqs.names)
) specs ON true
LEFT JOIN LATERAL (
    SELECT jsonb_agg(jsonb_build_array(quotation_name, submitted_at, source)
        ORDER BY quotation_name) AS revisions
    FROM procurement.quotation_submission WHERE rfq_name = ANY(rfqs.names)
) submitted ON true
LEFT JOIN LATERAL (
    SELECT jsonb_agg(jsonb_build_array(job_id, status) ORDER BY job_id) AS revisions,
        count(*) FILTER (WHERE status NOT IN ('COMPLETED', 'FAILED')) AS unfinished
    FROM procurement.quotation_extraction_job WHERE context->>'rfq_name' = ANY(rfqs.names)
) extracts ON true
LEFT JOIN LATERAL (
    SELECT jsonb_agg(jsonb_build_array(failure_id, updated_at) ORDER BY failure_id) AS revisions
    FROM procurement.quotation_intake_failure WHERE rfq_name = ANY(rfqs.names)
) failures ON true
LEFT JOIN LATERAL (
    SELECT jsonb_agg(jsonb_build_array(rfq_name,revision) ORDER BY rfq_name) AS revisions
    FROM procurement.quotation_deadline_revision WHERE rfq_name = ANY(rfqs.names)
) signals ON true
WHERE c.status = 'WAITING_INPUT' AND c.stage = 'QUOTATION_COLLECTION'
  AND NULLIF(c.workflow_snapshot #>> '{values,rfq_name}', '') IS NOT NULL;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'biddingflow_team') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON procurement.quotation_deadline_job,
            procurement.quotation_deadline_control, procurement.quotation_deadline_revision TO biddingflow_team;
        GRANT SELECT ON procurement.quotation_deadline_input TO biddingflow_team;
    END IF;
END $$;
