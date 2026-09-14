ALTER TABLE procurement.supplier_purchase_response
    ADD COLUMN IF NOT EXISTS reminder_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_reminded_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS reminder_error TEXT;

CREATE INDEX IF NOT EXISTS idx_supplier_pr_reminder_due
    ON procurement.supplier_purchase_response (
        status,
        sent_at,
        expires_at,
        last_reminded_at
    )
    WHERE status = 'SENT';