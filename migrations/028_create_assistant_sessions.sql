-- Chat-only storage: no foreign keys, triggers, or changes to purchase tables.
CREATE TABLE IF NOT EXISTS procurement.assistant_session (
    session_id UUID PRIMARY KEY,
    owner_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '새 대화',
    messages JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(messages) = 'array'),
    dialogue JSONB,
    version INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS assistant_session_owner_updated_idx
    ON procurement.assistant_session(owner_id, updated_at DESC);
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'biddingflow_team') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON procurement.assistant_session TO biddingflow_team;
    END IF;
END $$;
