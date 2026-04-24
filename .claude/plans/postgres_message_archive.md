# Postgres Message Archive — Design Plan

**Status:** DRAFT (design only, not implemented)
**Date:** 2026-04-24
**Author:** design session with Claude Opus 4.7

---

## 1. Goal

Replace the ephemeral in-memory `_chat_history: dict[-15:]` in `whatsapp_assistant.py` with a durable, queryable archive of every WhatsApp message the bot has seen.

**Success criteria:**
1. Zero message loss across restarts.
2. Full raw payload preserved for retrospective analysis.
3. PII-safe by design (no plaintext phone numbers in content search paths).
4. Simple SQL queries answer course-improvement questions:
   - "Every message from student X across all lectures"
   - "All questions asked within 48h after Group 1 Lecture 7"
   - "Confusion-signal words (ვერ, რატომ, არ მესმის) frequency per lecture"

---

## 2. Schema

### Primary table — `messages`

```sql
CREATE TABLE messages (
    -- identity
    id                  BIGSERIAL PRIMARY KEY,
    green_api_id        TEXT UNIQUE NOT NULL,    -- idempotency key from webhook
    chat_id             TEXT NOT NULL,           -- "120363XXX@g.us" or DM "995...@c.us"
    sender_hash         TEXT NOT NULL,           -- HMAC-SHA256(phone, PEPPER); see §4
    sender_display      TEXT,                    -- pushName, nullable, PII-light

    -- message
    ts_message          TIMESTAMPTZ NOT NULL,    -- from Green API timestamp
    ts_ingested         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    direction           TEXT NOT NULL CHECK (direction IN ('incoming','outgoing')),
    msg_type            TEXT NOT NULL,           -- textMessage, imageMessage, reactionMessage, ...
    content             TEXT,                    -- plaintext body (may be NULL for media)
    quoted_green_id     TEXT,                    -- for reply threading
    raw_payload         JSONB NOT NULL,          -- full Green API response, for forensics

    -- derived (populated by background job, not by write path)
    group_number        SMALLINT,                -- 1 or 2, NULL for DMs
    lecture_context     SMALLINT,                -- which lecture was most recent when this was sent
    is_bot              BOOLEAN NOT NULL,        -- true for assistant replies
    redacted            BOOLEAN NOT NULL DEFAULT FALSE  -- GDPR deletion marker
);

CREATE INDEX idx_messages_chat_ts        ON messages (chat_id, ts_message DESC);
CREATE INDEX idx_messages_sender_ts      ON messages (sender_hash, ts_message DESC);
CREATE INDEX idx_messages_group_lecture  ON messages (group_number, lecture_context);
CREATE INDEX idx_messages_fts
    ON messages USING gin (to_tsvector('simple', coalesce(content,'')));
-- 'simple' config because Georgian has no built-in PG dictionary;
-- use regexp_matches() for morphology-sensitive queries.
```

### PII mapping table — `senders`

```sql
CREATE TABLE senders (
    sender_hash     TEXT PRIMARY KEY,
    phone_encrypted BYTEA NOT NULL,              -- AES-256-GCM; see §4
    first_seen      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    groups          SMALLINT[] DEFAULT '{}',     -- {1}, {2}, {1,2}
    display_names   TEXT[] DEFAULT '{}',         -- history of push names
    student_id      TEXT,                        -- manual link to training roster
    gdpr_deleted    BOOLEAN DEFAULT FALSE
);
```

**Why split:** Queries against `messages` never need the raw phone number. Only admin operations (lookup for support, GDPR deletion) touch `senders`, which has stricter access control.

### Lecture context — `lecture_windows`

```sql
CREATE TABLE lecture_windows (
    group_number    SMALLINT NOT NULL,
    lecture_number  SMALLINT NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL,
    ends_at         TIMESTAMPTZ NOT NULL,         -- started_at + 7d (until next lecture)
    PRIMARY KEY (group_number, lecture_number)
);
```

Used by the `lecture_context` backfill:
```sql
UPDATE messages m
SET group_number = lw.group_number,
    lecture_context = lw.lecture_number
FROM lecture_windows lw
WHERE m.chat_id IN (GROUP1_ID, GROUP2_ID)
  AND m.ts_message BETWEEN lw.started_at AND lw.ends_at
  AND m.group_number IS NULL;
```

---

## 3. Ingestion paths

### Path A — Live webhooks (primary)
Current flow: Green API → `POST /whatsapp-incoming` → `whatsapp_assistant.handle_message()`.

**Modification:** Insert **first**, process second. Write path:

```
webhook received
    → parse payload
    → INSERT INTO messages (green_api_id, ...) ON CONFLICT DO NOTHING
    → existing assistant logic (Mem0 extraction, reply generation)
    → if reply sent: INSERT outgoing row
```

**Why INSERT first:** if Mem0 or LLM fails, we still have the raw row. Durability > latency.

**Idempotency:** `ON CONFLICT (green_api_id) DO NOTHING`. Green API retries produce no duplicates.

### Path B — Green API backfill (one-shot recovery)
**Confirmed from probe (2026-04-24):**
- G1: 19.31 days of history at count=100
- G2: 15.52 days at count=100
- Plan allows count up to ~1000 (API docs limit)

**Backfill strategy:**
```
for chat_id in [GROUP1_ID, GROUP2_ID]:
    resp = getChatHistory(chat_id, count=1000)
    for msg in resp:
        INSERT ... ON CONFLICT DO NOTHING
    # write greenAPI cursor so we don't re-fetch
```

Runtime: 2 API calls, < 5 sec. Quota cost: 2 requests.

### Path C — Obsidian dump re-import (fallback)
The existing `obsidian-vault/WhatsApp დისკუსიები/ჯგუფი N -- ჩატი.md` files contain the last 100 messages as of 2026-04-01. Parse and insert as last-resort backfill for the March 29 → April 4 window that Green API may have dropped.

**Priority:** only if Path B doesn't reach back far enough. Verify first with `count=1000` probe.

---

## 4. PII & Security

### 4.1 Phone number hashing
- `sender_hash = HMAC-SHA256(phone, PEPPER)`
- `PEPPER` = 32-byte random, stored in env var `SENDER_HASH_PEPPER`, never in code.
- Rationale: deterministic (same phone → same hash across restarts, enabling joins) but pre-image resistant.
- **Do not use bcrypt** — we need deterministic hashing for JOINs.

### 4.2 Phone number encryption in `senders`
- `phone_encrypted = AES-256-GCM(phone, key=SENDER_ENC_KEY, aad=sender_hash)`
- `SENDER_ENC_KEY` in env, rotatable.
- Decryption only happens in the admin/support code path, never in the hot path.

### 4.3 Message content
- **Not encrypted at rest by column** — Postgres disk encryption is sufficient for our threat model (a malicious actor with DB access is already game over).
- **Exception:** if we store 1-on-1 DMs with Tornike, consider content-level encryption because those logs are more sensitive.

### 4.4 GDPR delete
- Soft-delete via `redacted = TRUE`, content replaced with `[REDACTED]`, raw_payload zeroed.
- Hard-delete on explicit request: CASCADE from `senders.gdpr_deleted`.

### 4.5 DB access
- Dedicated Postgres user `training_agent_app` with `SELECT, INSERT, UPDATE` on `messages`, read-only on `senders`.
- Separate user `training_agent_admin` for joins across tables (used only by analytics scripts, MFA enforced).

### 4.6 Retention
- Active course: indefinite.
- Post-course: archive `messages` older than 12 months to cold storage (JSON dump to Drive).
- Legal basis: legitimate interest for educational outcome analysis, disclosed in student enrollment.

---

## 5. Hosting choice — decision matrix

| Option | Pros | Cons | Fit score |
|---|---|---|---|
| **Railway Postgres add-on** | Same platform, shared secrets, zero-latency to app | **Railway deploy frozen 2026-04-16** — blocks this | 2/10 right now |
| **DigitalOcean Managed PG** | $15/mo, reliable, 1-region | New account setup, separate billing | 8/10 |
| **Supabase** | Postgres + auth + REST API free; we may need auth later anyway | 500MB free limit fills in ~6 months of messages; then $25/mo | 9/10 |
| **Local SQLite + off-site backup** | Free, simple | Single writer, no concurrent access, backup reliability | 4/10 |
| **Neon serverless PG** | $0 free tier, branching, fast cold-start | Cold starts add 500ms latency | 7/10 |

**Recommendation: Supabase.** Reasons:
1. Free tier survives initial rollout.
2. Postgres + REST auto-API means analytics dashboards can be built without a backend.
3. Row-level security built-in for PII columns.
4. Existing credentials in other projects (per CLAUDE.md ecosystem).
5. Migration away from Supabase is just `pg_dump`.

---

## 6. Code changes (scope inventory — for future PR, not this session)

| File | Change |
|---|---|
| `tools/integrations/postgres_client.py` | **NEW** — connection pool, INSERT helpers |
| `tools/services/message_archive.py` | **NEW** — write path, dedup, lecture-context backfill |
| `tools/services/whatsapp_assistant.py` | Hook archive write before Mem0 call (~5 lines) |
| `tools/app/server.py` | Webhook path: archive before assistant logic |
| `tools/app/scheduler.py` | Nightly job: `backfill_from_greenapi()`, `enrich_lecture_context()` |
| `tools/core/config.py` | `DATABASE_URL`, `SENDER_HASH_PEPPER`, `SENDER_ENC_KEY` |
| `requirements.txt` | `psycopg[binary]==3.2.x`, `cryptography` (already transitive) |
| `scripts/migrate_001_messages.sql` | **NEW** — schema migration |
| `scripts/backfill_messages.py` | **NEW** — one-shot Green API + Obsidian import |
| `tools/tests/test_message_archive.py` | **NEW** — write path, dedup, hash determinism |

**Total: 8 files touched + 4 new. Matches earlier scope estimate.**

---

## 7. Risk / mitigation checklist

| Risk | Mitigation |
|---|---|
| Write path latency hits webhook timeout | INSERT is <5ms local, <30ms remote; well under Green API's 30s webhook timeout |
| Postgres down → bot stops replying | Archive write wrapped in `try/except`, warn-log on failure, bot continues; replay queue fills missed rows from Green API on recovery |
| Webhook replay creates duplicates | `green_api_id UNIQUE` + `ON CONFLICT DO NOTHING` |
| Hash collision via pepper leak | Rotate pepper = all old hashes invalidated; require explicit re-hash migration |
| Supabase free tier exhaustion | Alert at 400MB usage; plan upgrade before hit |
| Group 2 live lecture during deploy | Deploy window: Sunday 03:00 Tbilisi (no lectures) |
| PII in logs | Sender logged as hash-only; `phone_encrypted` never touches log outputs |

---

## 8. Open decisions (blocking implementation)

**D1.** Supabase vs DigitalOcean — user choice (both are acceptable).
**D2.** Hard deadline for rollout — aligned with Railway unfreeze and Claude credits top-up.
**D3.** Whether to migrate existing Mem0 fragments into the archive or leave Mem0 as a parallel extraction layer.
**D4.** Should the archive include the bot's reply text, or only student inputs? (Recommendation: include both — reply quality analysis matters.)

---

## 9. Not in scope for this doc

- Pinecone index changes (separate plan)
- Rerank integration (separate plan)
- Graphiti / Cognee evaluation (separate POC)
- Analytics dashboard UI
