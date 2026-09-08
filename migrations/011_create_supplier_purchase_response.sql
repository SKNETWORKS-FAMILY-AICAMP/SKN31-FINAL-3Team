CREATE TABLE IF NOT EXISTS procurement.supplier_purchase_response (
    pr_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL REFERENCES procurement.procurement_case(case_id) ON DELETE CASCADE,
    mr_name VARCHAR(140) NOT NULL,
    rfq_name VARCHAR(140),
    supplier_quotation VARCHAR(140),
    supplier_id VARCHAR(140) NOT NULL,
    supplier_email VARCHAR(320) NOT NULL,
    token_hash CHAR(64) NOT NULL UNIQUE,
    purchase_mode VARCHAR(20) NOT NULL DEFAULT 'quotation',
    direct_purchase_items JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(20) NOT NULL DEFAULT 'DRAFT',
    rejection_reason TEXT,
    expires_at TIMESTAMPTZ NOT NULL,
    sent_at TIMESTAMPTZ,
    responded_at TIMESTAMPTZ,
    po_name VARCHAR(140),
    po_error TEXT,
    processing_error TEXT,
    processing_error_stage VARCHAR(30),
    processing_failed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_supplier_pr_mode CHECK (purchase_mode IN ('quotation', 'direct')),
    CONSTRAINT ck_supplier_pr_status CHECK (status IN ('DRAFT', 'SENT', 'ACCEPTED', 'REJECTED', 'EXPIRED', 'CANCELLED', 'PO_CREATED', 'PO_FAILED')),
    CONSTRAINT ck_supplier_pr_rejection_reason CHECK (status <> 'REJECTED' OR length(trim(rejection_reason)) >= 2)
);

CREATE INDEX IF NOT EXISTS idx_supplier_pr_case_created
    ON procurement.supplier_purchase_response (case_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_supplier_pr_status_expires
    ON procurement.supplier_purchase_response (status, expires_at);

CREATE UNIQUE INDEX IF NOT EXISTS uq_supplier_pr_active_case
    ON procurement.supplier_purchase_response (case_id)
    WHERE status IN ('DRAFT', 'SENT', 'ACCEPTED', 'PO_FAILED');
