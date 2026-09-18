-- Normalize the early audit schema to the UUID case model used by the API.
DO $$
DECLARE
    constraint_name text;
    case_type text;
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'procurement'
          AND table_name = 'ai_decision_log'
          AND column_name = 'node_name'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'procurement'
          AND table_name = 'ai_decision_log'
          AND column_name = 'node'
    ) THEN
        ALTER TABLE procurement.ai_decision_log RENAME COLUMN node_name TO node;
    END IF;

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'procurement'
          AND table_name = 'ai_decision_log'
          AND column_name = 'reasoning'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'procurement'
          AND table_name = 'ai_decision_log'
          AND column_name = 'reason'
    ) THEN
        ALTER TABLE procurement.ai_decision_log RENAME COLUMN reasoning TO reason;
    END IF;

    SELECT data_type INTO case_type
    FROM information_schema.columns
    WHERE table_schema = 'procurement'
      AND table_name = 'ai_decision_log'
      AND column_name = 'case_id';

    IF case_type IS DISTINCT FROM 'uuid' THEN
        ALTER TABLE procurement.ai_decision_log
            ADD COLUMN IF NOT EXISTS case_uuid uuid;

        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'procurement'
              AND table_name = 'procurement_case'
              AND column_name = 'id'
        ) THEN
            UPDATE procurement.ai_decision_log log
            SET case_uuid = pc.case_id
            FROM procurement.procurement_case pc
            WHERE log.case_id IS NOT NULL
              AND log.case_id::text = pc.id::text;
        END IF;

        FOR constraint_name IN
            SELECT conname
            FROM pg_constraint
            WHERE conrelid = 'procurement.ai_decision_log'::regclass
              AND contype = 'f'
        LOOP
            EXECUTE format(
                'ALTER TABLE procurement.ai_decision_log DROP CONSTRAINT %I',
                constraint_name
            );
        END LOOP;

        ALTER TABLE procurement.ai_decision_log DROP COLUMN case_id;
        ALTER TABLE procurement.ai_decision_log RENAME COLUMN case_uuid TO case_id;
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'procurement.ai_decision_log'::regclass
          AND contype = 'f'
          AND conname = 'ai_decision_log_case_id_fkey'
    ) THEN
        ALTER TABLE procurement.ai_decision_log
            ADD CONSTRAINT ai_decision_log_case_id_fkey
            FOREIGN KEY (case_id)
            REFERENCES procurement.procurement_case(case_id)
            ON DELETE SET NULL;
    END IF;
END
$$;

DROP INDEX IF EXISTS procurement.idx_ai_decision_log_case_created;
DROP INDEX IF EXISTS procurement.idx_ai_decision_log_node_created;
CREATE INDEX idx_ai_decision_log_case_created
    ON procurement.ai_decision_log (case_id, created_at DESC);
CREATE INDEX idx_ai_decision_log_node_created
    ON procurement.ai_decision_log (node, created_at DESC);
