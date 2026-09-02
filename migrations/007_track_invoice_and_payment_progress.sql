-- Track the ERPNext documents that follow a Purchase Order.
--
-- Purchase Receipt, Purchase Invoice, and Payment Entry are independent ERP
-- documents and their webhooks can arrive while BiddingFlow is offline.  The
-- normalized records below let reconciliation rebuild the current projection
-- without treating the browser cache as the source of truth.

ALTER TABLE procurement.purchase_order_delivery
    ADD COLUMN IF NOT EXISTS invoice_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS latest_invoice_name VARCHAR(140),
    ADD COLUMN IF NOT EXISTS invoice_total NUMERIC(18, 2) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS outstanding_amount NUMERIC(18, 2) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS payment_status VARCHAR(30) NOT NULL DEFAULT 'NOT_INVOICED',
    ADD COLUMN IF NOT EXISTS paid_amount NUMERIC(18, 2) NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS latest_payment_entry VARCHAR(140),
    ADD COLUMN IF NOT EXISTS last_payment_date DATE;

CREATE TABLE IF NOT EXISTS procurement.purchase_invoice_record (
    invoice_name VARCHAR(140) NOT NULL,
    po_name VARCHAR(140) NOT NULL,
    posting_date DATE NOT NULL,
    docstatus SMALLINT NOT NULL,
    invoice_status VARCHAR(40),
    grand_total NUMERIC(18, 2) NOT NULL DEFAULT 0,
    outstanding_amount NUMERIC(18, 2) NOT NULL DEFAULT 0,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (invoice_name, po_name)
);

CREATE INDEX IF NOT EXISTS idx_purchase_invoice_record_po
    ON procurement.purchase_invoice_record (po_name, posting_date);

CREATE TABLE IF NOT EXISTS procurement.payment_entry_record (
    payment_entry_name VARCHAR(140) NOT NULL,
    invoice_name VARCHAR(140) NOT NULL,
    posting_date DATE NOT NULL,
    docstatus SMALLINT NOT NULL,
    allocated_amount NUMERIC(18, 2) NOT NULL DEFAULT 0,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (payment_entry_name, invoice_name)
);

CREATE INDEX IF NOT EXISTS idx_payment_entry_record_invoice
    ON procurement.payment_entry_record (invoice_name, posting_date);

DROP TRIGGER IF EXISTS trg_purchase_invoice_record_updated_at
    ON procurement.purchase_invoice_record;
CREATE TRIGGER trg_purchase_invoice_record_updated_at
BEFORE UPDATE ON procurement.purchase_invoice_record
FOR EACH ROW EXECUTE FUNCTION procurement.set_updated_at();

DROP TRIGGER IF EXISTS trg_payment_entry_record_updated_at
    ON procurement.payment_entry_record;
CREATE TRIGGER trg_payment_entry_record_updated_at
BEFORE UPDATE ON procurement.payment_entry_record
FOR EACH ROW EXECUTE FUNCTION procurement.set_updated_at();

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'biddingflow_team') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON procurement.purchase_invoice_record,
               procurement.payment_entry_record
            TO biddingflow_team;
    END IF;
END
$$;
