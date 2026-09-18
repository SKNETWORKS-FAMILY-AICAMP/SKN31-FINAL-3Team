ALTER TABLE procurement.quotation_specification_cache
    ADD COLUMN IF NOT EXISTS case_id UUID;

-- Link both the current RFQ and archived rebidding rounds to their purchase case.
UPDATE procurement.quotation_specification_cache cache
SET case_id = (
    SELECT pc.case_id
    FROM procurement.procurement_case pc
    WHERE pc.workflow_snapshot #>> '{values,rfq_name}' = cache.rfq_name
       OR EXISTS (
            SELECT 1
            FROM jsonb_array_elements(
                COALESCE(
                    pc.workflow_snapshot #> '{values,rfq_rounds}',
                    '[]'::jsonb
                )
            ) AS round_entry
            WHERE round_entry->>'rfq_name' = cache.rfq_name
       )
    ORDER BY pc.updated_at DESC
    LIMIT 1
)
WHERE cache.case_id IS NULL
  AND EXISTS (
      SELECT 1
      FROM procurement.procurement_case pc
      WHERE pc.workflow_snapshot #>> '{values,rfq_name}' = cache.rfq_name
         OR EXISTS (
              SELECT 1
              FROM jsonb_array_elements(
                  COALESCE(
                      pc.workflow_snapshot #> '{values,rfq_rounds}',
                      '[]'::jsonb
                  )
              ) AS round_entry
              WHERE round_entry->>'rfq_name' = cache.rfq_name
         )
  );

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'quotation_specification_cache_case_id_fkey'
          AND conrelid = 'procurement.quotation_specification_cache'::regclass
    ) THEN
        ALTER TABLE procurement.quotation_specification_cache
            ADD CONSTRAINT quotation_specification_cache_case_id_fkey
            FOREIGN KEY (case_id)
            REFERENCES procurement.procurement_case(case_id)
            ON DELETE SET NULL;
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS quotation_specification_cache_case_idx
    ON procurement.quotation_specification_cache (case_id, updated_at DESC);

COMMENT ON COLUMN procurement.quotation_specification_cache.case_id IS
    'Purchase workflow case that owns the RFQ; nullable only for orphaned or legacy RFQs';
