-- Migration 001: Message archive schema
-- Target: SQLite (local dev) — Postgres-compatible subset.
-- When Supabase/Postgres is ready, run via `psql -f migrate_001_messages.sql`
-- with minor type adjustments noted inline.

-- ---------------------------------------------------------------
-- messages — raw conversational archive
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS messages (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,   -- Postgres: BIGSERIAL
    green_api_id        TEXT NOT NULL UNIQUE,                -- idempotency key
    chat_id             TEXT NOT NULL,                       -- e.g. "120363XXX@g.us"
    sender_hash         TEXT NOT NULL,                       -- HMAC-SHA256(phone, PEPPER)
    sender_display      TEXT,                                -- pushName, nullable
    ts_message          TEXT NOT NULL,                       -- ISO-8601 UTC; Postgres: TIMESTAMPTZ
    ts_ingested         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    direction           TEXT NOT NULL CHECK (direction IN ('incoming','outgoing')),
    msg_type            TEXT NOT NULL,
    content             TEXT,
    quoted_green_id     TEXT,
    raw_payload         TEXT NOT NULL,                       -- JSON; Postgres: JSONB
    group_number        INTEGER,                             -- 1|2|NULL for DMs
    lecture_context     INTEGER,                             -- backfilled later
    is_bot              INTEGER NOT NULL DEFAULT 0,          -- 0|1
    redacted            INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_messages_chat_ts
    ON messages (chat_id, ts_message DESC);
CREATE INDEX IF NOT EXISTS idx_messages_sender_ts
    ON messages (sender_hash, ts_message DESC);
CREATE INDEX IF NOT EXISTS idx_messages_group_lecture
    ON messages (group_number, lecture_context);

-- Postgres-only (comment out for SQLite):
-- CREATE INDEX idx_messages_fts
--     ON messages USING gin (to_tsvector('simple', coalesce(content,'')));

-- ---------------------------------------------------------------
-- senders — PII mapping (phone ↔ hash)
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS senders (
    sender_hash     TEXT PRIMARY KEY,
    phone_encrypted BLOB NOT NULL,                           -- AES-256-GCM; Postgres: BYTEA
    first_seen      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_seen       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    groups_json     TEXT DEFAULT '[]',                       -- JSON array; Postgres: SMALLINT[]
    display_names_json TEXT DEFAULT '[]',                    -- JSON array; Postgres: TEXT[]
    student_id      TEXT,                                    -- manual roster link
    gdpr_deleted    INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_senders_student ON senders (student_id);

-- ---------------------------------------------------------------
-- lecture_windows — map (group, lecture) to timestamp range
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lecture_windows (
    group_number    INTEGER NOT NULL,
    lecture_number  INTEGER NOT NULL,
    started_at      TEXT NOT NULL,
    ends_at         TEXT NOT NULL,
    PRIMARY KEY (group_number, lecture_number)
);

-- ---------------------------------------------------------------
-- Schema version marker
-- ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    description TEXT NOT NULL
);

INSERT OR IGNORE INTO schema_migrations (version, description)
VALUES (1, 'Initial message archive: messages, senders, lecture_windows');
