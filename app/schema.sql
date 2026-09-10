-- Clipstory schema. Applied on startup; every statement is idempotent so the
-- app can run this unconditionally rather than carrying a migration tool.

CREATE TABLE IF NOT EXISTS users (
    id            BIGSERIAL PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    is_admin      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ
);

-- The allowlist. Access is invite-only: an address absent from this table
-- cannot log in, no matter who sends them the URL.
CREATE TABLE IF NOT EXISTS invites (
    email      TEXT PRIMARY KEY,
    invited_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Magic link tokens. Only the hash is stored, so a database leak does not hand
-- over working login links.
CREATE TABLE IF NOT EXISTS login_tokens (
    token_hash TEXT PRIMARY KEY,
    email      TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at    TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS login_tokens_expires_idx ON login_tokens (expires_at);

CREATE TABLE IF NOT EXISTS jobs (
    id                 UUID PRIMARY KEY,
    user_id            BIGINT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    source_filename    TEXT NOT NULL,
    source_path        TEXT,
    source_bytes       BIGINT NOT NULL DEFAULT 0,
    received_bytes     BIGINT NOT NULL DEFAULT 0,
    status             TEXT NOT NULL,
    error              TEXT,
    duration_seconds   DOUBLE PRECISION,
    width              INTEGER,
    height             INTEGER,
    fps                DOUBLE PRECISION,
    variable_frame_rate BOOLEAN,
    claimed_by         TEXT,
    claimed_at         TIMESTAMPTZ,
    source_deleted_at  TIMESTAMPTZ,
    expires_at         TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS jobs_user_idx ON jobs (user_id, created_at DESC);
-- Supports the worker's claim query, which scans only for queued work.
CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs (status, created_at);

CREATE TABLE IF NOT EXISTS clips (
    id                BIGSERIAL PRIMARY KEY,
    job_id            UUID NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    sequence          INTEGER NOT NULL,
    label             TEXT,
    start_seconds     DOUBLE PRECISION NOT NULL,
    end_seconds       DOUBLE PRECISION NOT NULL,
    output_filename   TEXT,
    output_path       TEXT,
    output_bytes      BIGINT,
    status            TEXT NOT NULL DEFAULT 'pending',
    rendered_duration DOUBLE PRECISION,
    error             TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (job_id, sequence)
);
CREATE INDEX IF NOT EXISTS clips_job_idx ON clips (job_id, sequence);
