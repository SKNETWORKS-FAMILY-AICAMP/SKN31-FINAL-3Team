-- Only timed, administrator-owned warm workers are controlled by this table.
CREATE TABLE IF NOT EXISTS procurement.runpod_worker_lease (
    endpoint_id text PRIMARY KEY,
    revision bigint NOT NULL DEFAULT 0,
    owned boolean NOT NULL DEFAULT false,
    expires_at timestamptz,
    updated_by text NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT now(),
    last_error text,
    warmup_job_id text,
    model_status text NOT NULL DEFAULT 'unknown'
);
CREATE TABLE IF NOT EXISTS procurement.runpod_worker_lease_event (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    endpoint_id text NOT NULL,
    revision bigint NOT NULL,
    actor text NOT NULL,
    action text NOT NULL,
    expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
