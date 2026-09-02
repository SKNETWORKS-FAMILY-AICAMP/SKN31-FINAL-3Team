-- BiddingFlow frontend/backend integration tables.
--
-- ERPNext remains the source of truth for Material Request and purchasing
-- documents.  PostgreSQL stores workflow state, UI projections, HITL tasks,
-- notifications, and idempotent inbound events.
--
-- This migration deliberately extends the existing procurement_case table
-- instead of replacing it.  Team environments currently contain either the
-- early integer-id draft schema or the later UUID case_logging schema.

CREATE SCHEMA IF NOT EXISTS procurement;

CREATE TABLE IF NOT EXISTS procurement.procurement_case (
    case_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    mr_name VARCHAR(140),
    status VARCHAR(80) NOT NULL DEFAULT 'AWAITING_MR_REVIEW',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE procurement.procurement_case
    ADD COLUMN IF NOT EXISTS case_id UUID,
    ADD COLUMN IF NOT EXISTS mr_name VARCHAR(140),
    ADD COLUMN IF NOT EXISTS thread_id VARCHAR(180),
    ADD COLUMN IF NOT EXISTS stage VARCHAR(80) NOT NULL DEFAULT 'MR_REVIEW',
    ADD COLUMN IF NOT EXISTS item_code VARCHAR(140),
    ADD COLUMN IF NOT EXISTS item_name VARCHAR(255),
    ADD COLUMN IF NOT EXISTS requester_id VARCHAR(140),
    ADD COLUMN IF NOT EXISTS assigned_user_id VARCHAR(140),
    ADD COLUMN IF NOT EXISTS summary JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS workflow_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS last_error TEXT,
    ADD COLUMN IF NOT EXISTS erp_modified_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS quotation_deadline_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMPTZ;

-- Backfill compatibility columns when this database started from migration 002.
UPDATE procurement.procurement_case
SET case_id = gen_random_uuid()
WHERE case_id IS NULL;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'procurement'
          AND table_name = 'procurement_case'
          AND column_name = 'mr_id'
    ) THEN
        EXECUTE 'UPDATE procurement.procurement_case '
                'SET mr_name = mr_id WHERE mr_name IS NULL';
        -- These early-schema columns described one MR item per case.  The
        -- confirmed product rule is now one item per MR, so canonical writes
        -- no longer need to provide the redundant identifiers.
        ALTER TABLE procurement.procurement_case
            ALTER COLUMN mr_id DROP NOT NULL;
    END IF;

    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'procurement'
          AND table_name = 'procurement_case'
          AND column_name = 'mr_item_id'
    ) THEN
        ALTER TABLE procurement.procurement_case
            ALTER COLUMN mr_item_id DROP NOT NULL;
    END IF;
END
$$;

UPDATE procurement.procurement_case
SET thread_id = COALESCE(mr_name, case_id::text)
WHERE thread_id IS NULL;

ALTER TABLE procurement.procurement_case
    ALTER COLUMN case_id SET DEFAULT gen_random_uuid(),
    ALTER COLUMN case_id SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_procurement_case_case_id
    ON procurement.procurement_case (case_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_procurement_case_mr_name
    ON procurement.procurement_case (mr_name)
    WHERE mr_name IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_procurement_case_thread_id
    ON procurement.procurement_case (thread_id)
    WHERE thread_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_procurement_case_stage_updated
    ON procurement.procurement_case (stage, updated_at DESC);

-- A canonical UUID audit table.  It avoids changing the type of the older
-- case_status_history.case_id column in place, which would be unsafe on a
-- database that already contains team test data.
CREATE TABLE IF NOT EXISTS procurement.workflow_status_history (
    history_id BIGSERIAL PRIMARY KEY,
    case_id UUID NOT NULL
        REFERENCES procurement.procurement_case(case_id) ON DELETE CASCADE,
    from_status VARCHAR(80),
    to_status VARCHAR(80) NOT NULL,
    stage VARCHAR(80),
    reason TEXT,
    triggered_by VARCHAR(140),
    changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_workflow_status_history_case_changed
    ON procurement.workflow_status_history (case_id, changed_at DESC);

CREATE TABLE IF NOT EXISTS procurement.human_task (
    task_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL
        REFERENCES procurement.procurement_case(case_id) ON DELETE CASCADE,
    task_type VARCHAR(80) NOT NULL,
    channel VARCHAR(40) NOT NULL DEFAULT 'BIDDINGFLOW',
    audience VARCHAR(40) NOT NULL DEFAULT 'BUYER',
    status VARCHAR(30) NOT NULL DEFAULT 'PENDING',
    title VARCHAR(255) NOT NULL,
    description TEXT,
    input_schema JSONB NOT NULL DEFAULT '{}'::jsonb,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    answer JSONB,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    answered_at TIMESTAMPTZ,
    answered_by VARCHAR(140)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_human_task_pending_type
    ON procurement.human_task (case_id, task_type, audience)
    WHERE status = 'PENDING';
CREATE INDEX IF NOT EXISTS idx_human_task_audience_status
    ON procurement.human_task (audience, status, created_at DESC);

CREATE TABLE IF NOT EXISTS procurement.integration_event (
    event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source VARCHAR(60) NOT NULL,
    event_type VARCHAR(100) NOT NULL,
    external_id VARCHAR(180),
    dedupe_key VARCHAR(255) NOT NULL UNIQUE,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    status VARCHAR(30) NOT NULL DEFAULT 'RECEIVED',
    retry_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_integration_event_status_received
    ON procurement.integration_event (status, received_at);

CREATE TABLE IF NOT EXISTS procurement.notification (
    notification_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID
        REFERENCES procurement.procurement_case(case_id) ON DELETE CASCADE,
    recipient_id VARCHAR(140),
    notification_type VARCHAR(80) NOT NULL,
    title VARCHAR(255) NOT NULL,
    message TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_read BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    read_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_notification_recipient_unread
    ON procurement.notification (recipient_id, is_read, created_at DESC);

CREATE TABLE IF NOT EXISTS procurement.purchase_order_delivery (
    delivery_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL
        REFERENCES procurement.procurement_case(case_id) ON DELETE CASCADE,
    po_name VARCHAR(140) NOT NULL UNIQUE,
    supplier VARCHAR(255),
    promised_delivery_date DATE,
    ordered_qty NUMERIC(18, 6) NOT NULL DEFAULT 0,
    received_qty NUMERIC(18, 6) NOT NULL DEFAULT 0,
    delivery_status VARCHAR(30) NOT NULL DEFAULT 'NOT_RECEIVED',
    first_receipt_date DATE,
    full_receipt_date DATE,
    scorecard_status VARCHAR(30) NOT NULL DEFAULT 'LOCKED',
    scorecard JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_purchase_order_delivery_case
    ON procurement.purchase_order_delivery (case_id);
CREATE INDEX IF NOT EXISTS idx_purchase_order_delivery_status
    ON procurement.purchase_order_delivery (delivery_status, updated_at DESC);

CREATE TABLE IF NOT EXISTS procurement.purchase_receipt_record (
    receipt_name VARCHAR(140) NOT NULL,
    po_name VARCHAR(140) NOT NULL,
    posting_date DATE NOT NULL,
    docstatus SMALLINT NOT NULL,
    accepted_qty NUMERIC(18, 6) NOT NULL DEFAULT 0,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (receipt_name, po_name)
);

CREATE INDEX IF NOT EXISTS idx_purchase_receipt_record_po
    ON procurement.purchase_receipt_record (po_name, posting_date);

CREATE TABLE IF NOT EXISTS procurement.idempotency_record (
    idempotency_key VARCHAR(255) PRIMARY KEY,
    operation VARCHAR(100) NOT NULL,
    request_hash VARCHAR(128),
    response_status INTEGER,
    response_body JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_idempotency_record_expires
    ON procurement.idempotency_record (expires_at);

-- Shared updated_at trigger.  It may already exist from migration 002.
CREATE OR REPLACE FUNCTION procurement.set_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_human_task_updated_at ON procurement.human_task;
CREATE TRIGGER trg_human_task_updated_at
BEFORE UPDATE ON procurement.human_task
FOR EACH ROW EXECUTE FUNCTION procurement.set_updated_at();

DROP TRIGGER IF EXISTS trg_integration_event_updated_at ON procurement.integration_event;
CREATE TRIGGER trg_integration_event_updated_at
BEFORE UPDATE ON procurement.integration_event
FOR EACH ROW EXECUTE FUNCTION procurement.set_updated_at();

DROP TRIGGER IF EXISTS trg_purchase_order_delivery_updated_at ON procurement.purchase_order_delivery;
CREATE TRIGGER trg_purchase_order_delivery_updated_at
BEFORE UPDATE ON procurement.purchase_order_delivery
FOR EACH ROW EXECUTE FUNCTION procurement.set_updated_at();

DROP TRIGGER IF EXISTS trg_purchase_receipt_record_updated_at ON procurement.purchase_receipt_record;
CREATE TRIGGER trg_purchase_receipt_record_updated_at
BEFORE UPDATE ON procurement.purchase_receipt_record
FOR EACH ROW EXECUTE FUNCTION procurement.set_updated_at();

-- Keep the existing team role usable even when its default privileges were
-- configured by a different database owner in an older environment.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'biddingflow_team') THEN
        GRANT USAGE ON SCHEMA procurement TO biddingflow_team;
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON procurement.procurement_case,
               procurement.workflow_status_history,
               procurement.human_task,
               procurement.integration_event,
               procurement.notification,
               procurement.idempotency_record,
               procurement.purchase_order_delivery,
               procurement.purchase_receipt_record
            TO biddingflow_team;
        GRANT USAGE, SELECT, UPDATE
            ON ALL SEQUENCES IN SCHEMA procurement TO biddingflow_team;
    END IF;
END
$$;
