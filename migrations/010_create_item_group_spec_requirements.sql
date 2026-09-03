-- Canonical policy store for automatic validation of newly requested Items.
-- Keep required_specs as TEXT JSON for compatibility with the table already
-- deployed by the item-validation team member.

CREATE TABLE IF NOT EXISTS procurement.item_group_spec_requirements (
    item_group TEXT PRIMARY KEY,
    required_specs TEXT NOT NULL,
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE procurement.item_group_spec_requirements
    ADD COLUMN IF NOT EXISTS reason TEXT,
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

-- Preserve policies from the earlier JSONB table when it exists. Some shared
-- development databases were created by the team script and never had that
-- earlier table, so the copy must be conditional. Existing requirements rows
-- always win.
DO $$
BEGIN
    IF to_regclass('procurement.item_group_spec') IS NOT NULL THEN
        EXECUTE $copy$
            INSERT INTO procurement.item_group_spec_requirements (
                item_group, required_specs, reason
            )
            SELECT item_group, required_specs::TEXT, NULL
            FROM procurement.item_group_spec
            ON CONFLICT (item_group) DO NOTHING
        $copy$;
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION procurement.set_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_item_group_spec_requirements_updated_at
    ON procurement.item_group_spec_requirements;
CREATE TRIGGER trg_item_group_spec_requirements_updated_at
BEFORE UPDATE ON procurement.item_group_spec_requirements
FOR EACH ROW EXECUTE FUNCTION procurement.set_updated_at();

COMMENT ON TABLE procurement.item_group_spec_requirements IS
    'AI-generated minimum specification requirements for each ERPNext Item Group';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'biddingflow_team') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE '
            'ON procurement.item_group_spec_requirements TO biddingflow_team';
    END IF;
END
$$;
