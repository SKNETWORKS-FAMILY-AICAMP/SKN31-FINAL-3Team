-- Keep ERPNext quotation responses separate from the LangGraph checkpoint.
-- The checkpoint describes execution state; this JSONB column is a refreshable
-- read model populated by polling or Supplier Quotation webhooks.

ALTER TABLE procurement.procurement_case
    ADD COLUMN IF NOT EXISTS quotation_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS idx_procurement_case_rfq_name
    ON procurement.procurement_case ((workflow_snapshot #>> '{values,rfq_name}'))
    WHERE workflow_snapshot #>> '{values,rfq_name}' IS NOT NULL;

COMMENT ON COLUMN procurement.procurement_case.quotation_snapshot IS
    'Latest ERPNext Supplier Quotation response projection for the case RFQ';
