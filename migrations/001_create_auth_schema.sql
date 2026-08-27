CREATE SCHEMA IF NOT EXISTS auth;

CREATE TABLE IF NOT EXISTS auth.users (
    id UUID PRIMARY KEY,
    erp_user_id TEXT NOT NULL UNIQUE,
    email TEXT,
    username TEXT,
    full_name TEXT NOT NULL,
    user_type TEXT NOT NULL DEFAULT 'System User',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS auth.sessions (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    erp_sid_ciphertext TEXT NOT NULL,
    client_ip TEXT,
    user_agent TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    revoke_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_auth_sessions_user_id
    ON auth.sessions (user_id);

CREATE INDEX IF NOT EXISTS idx_auth_sessions_active
    ON auth.sessions (expires_at)
    WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS auth.refresh_tokens (
    id UUID PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES auth.sessions(id) ON DELETE CASCADE,
    token_hash CHAR(64) NOT NULL UNIQUE,
    parent_token_id UUID REFERENCES auth.refresh_tokens(id),
    issued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    used_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_auth_refresh_tokens_session_id
    ON auth.refresh_tokens (session_id);

CREATE INDEX IF NOT EXISTS idx_auth_refresh_tokens_active
    ON auth.refresh_tokens (expires_at)
    WHERE used_at IS NULL AND revoked_at IS NULL;

COMMENT ON SCHEMA auth IS
    'NextERP local authentication sessions linked to ERPNext identities.';

COMMENT ON COLUMN auth.sessions.erp_sid_ciphertext IS
    'Encrypted ERPNext sid. Never expose this value in a JWT or frontend response.';

COMMENT ON COLUMN auth.refresh_tokens.token_hash IS
    'SHA-256 digest of an opaque refresh token. The raw token is never stored.';
