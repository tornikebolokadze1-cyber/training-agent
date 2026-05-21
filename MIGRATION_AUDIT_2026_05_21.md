# Pinecone → Qdrant Migration Completeness Audit — 2026-05-21

## Verdict

**PARTIAL — the data plane is 100% on Qdrant in production, but one packaging/operational gotcha (`PINECONE_API_KEY` still listed as `required=True` in orchestrator startup validation) means removing the Railway env var would crash the server. Code paths that send network calls all go to Qdrant; no live Pinecone API traffic. Cleanup PRs needed for shims, docs, and CI test fixtures.**

Production proof (`GET /health`, auth Bearer …A4Q):
- `qdrant` check: `ok`, collection `training-course` has **4244 points** (above the 4132 regen baseline → indexing has continued after migration)
- **No `pinecone` health check key** in the response array
- Local `git rev-parse HEAD` = `aaf244b` = `origin/main` HEAD (PR #71 merged)

---

## Surface 1 — Source code (tools/): 35 file matches, but 0 real Pinecone API calls

### Active code paths — all route to Qdrant

| File | Line | What | Category | Safe? |
|---|---|---|---|---|
| `tools/integrations/knowledge_indexer.py` | 26-39 | `from qdrant_client import QdrantClient`, no `from pinecone …` | active import | YES (Qdrant only) |
| `tools/integrations/knowledge_indexer.py` | 202-209 | `get_pinecone_index()` returns `get_qdrant_client()` | backward-compat alias | YES |
| `tools/integrations/knowledge_indexer.py` | 533-548 | `PineconeHealthReport` + `QdrantHealthReport = PineconeHealthReport` | name shim | YES |
| `tools/integrations/knowledge_indexer.py` | 34-35 | `PINECONE_SCORE_THRESHOLD_DIRECT/_PASSIVE` config names | constant name still says "PINECONE_" | YES (just a name) |
| `tools/integrations/qdrant_client.py` | 1-50, 110-135 | uuid5 / legacy-ID helpers; comments reference Pinecone history | docstrings + helper for Pinecone-style ID compatibility | YES |
| `tools/integrations/obsidian_sync.py` | 471, 1341, 1502 | `extract_from_pinecone()` alias → `extract_from_qdrant()`; active code calls `extract_from_qdrant` | alias + docstrings | YES |
| `tools/services/analytics.py` | 1436, 1458, 1566 | `sync_from_pinecone`, `_safe_backup_to_pinecone`, `backup_scores_to_pinecone` all delegate to `*_qdrant` | aliases | YES |
| `tools/services/drive_audit.py` | 50-72, 123 | `pinecone_vector_count` property, `_list_pinecone_vectors_for_lecture`, JSON key `"pinecone_vectors"` | aliases + dual-emission JSON key | YES (legacy consumers still read old key) |
| `tools/services/whatsapp_assistant.py` | 455-494 | active `_retrieve_context()` calls `query_knowledge` — that function lives in `knowledge_indexer.py` and uses Qdrant. Docstring still says "Pinecone". | docstring drift | YES (text only) |
| `tools/services/data_reconciliation.py` | 72-87, 130-235 | `_scan_pinecone()` calls `lecture_exists_in_index` → Qdrant via knowledge_indexer; field name `in_pinecone_only` | aliases + field naming drift | YES (works correctly, names are misleading) |
| `tools/services/transcribe_lecture.py` | 238, 588-613 | DLQ operation tag `"pinecone_index"`; pipeline_state field `pinecone_indexed`; calls `index_lecture_content` from knowledge_indexer | string tag + state schema field name | YES (active path goes to Qdrant) |
| `tools/core/health_monitor.py` | 32-33 | `from tools.core.config import PINECONE_API_KEY, PINECONE_INDEX_NAME` with `# noqa: F401` — re-exported for tests | unused re-export | YES |
| `tools/core/health_monitor.py` | 423, 499, 768, 826 | `check_qdrant()` is real, `check_pinecone()` is a 1-line alias returning `check_qdrant()`; same for `*_scores_consistency` | aliases | YES |
| `tools/core/api_resilience.py` | 216 | `KNOWN_SERVICES = (..., "pinecone")` | string tag in service registry | YES (legacy metric label) |
| `tools/app/admin_routes.py` | 486-493, 789-802 | `_get_pinecone_counts()`, `_reconstruct_from_pinecone()` are 1-line aliases to `_get_vector_counts` / `_reconstruct_from_qdrant` | aliases | YES |
| `tools/app/admin_routes.py` | 1475-1549 | Backfill endpoint docstrings + error message `"Pinecone auto-detect failed"` | docstring + user-visible 503 message | LOW RISK (cosmetic) |
| `tools/app/server.py` | 330-2396 | ~16 docstring/log references; active code calls `lecture_exists_in_index`, `backup_scores_to_pinecone` (alias), `sync_from_pinecone` (alias) — all route to Qdrant | docstrings + logs say "Pinecone" but execute against Qdrant | YES (log noise only) |
| `tools/app/scheduler.py` | 104-1413 | Cron-job IDs `pinecone_score_backup`, `drive_pinecone_audit`; calls `backup_scores_to_pinecone` (alias) | string IDs + log strings | YES (job IDs are stable identifiers) |
| `tools/app/orchestrator.py` | 34, 60, 126-127, 191-231, 582-645, 990-998 | **(see Critical #1)** + DLQ handler key `"pinecone_index"` + log lines | mixed: critical config gate + log noise | **MIXED — see Critical #1** |
| `tools/app/paperclip_bridge.py` | 122, 268 | `"PINECONE_API_KEY": _env_key_present(...)` in /paperclip/health + /paperclip/status response | exposes legacy env-key presence | LOW RISK |
| `tools/services/unified_query.py` | 8, 13, 301 | docstrings reference Pinecone | docstring drift | YES |
| `tools/integrations/gemini_analyzer.py` | 1461 | log/comment about "garbage into Pinecone" | comment only | YES |

### Real Pinecone imports (`from pinecone …` / `import pinecone`) in tools/

**Zero matches.** No production code imports the `pinecone` package. Confirmed via `Grep -n "^(from pinecone\|import pinecone)" tools/`.

### Tests (tools/tests/) — 16 files with pinecone strings

All are alias tests, docstrings, or `pinecone_vectors_indexed=…` quality-gate kwargs that exercise the backward-compat surface. `tools/tests/conftest.py` ships a `pinecone` stub module so legacy imports don't crash (no live tests import it).

---

## Surface 2 — requirements.txt

| Line | Package | Verdict |
|---|---|---|
| 36 | `qdrant-client==1.12.1` | required, active |
| 39 | `pinecone==8.1.0` | **dead weight — no code imports it. Removing it does not break tests or production.** Safe to drop in cleanup PR. |

Comment on line 38 already labels it "deprecated, kept temporarily until all callers migrate to Qdrant" — that migration is **done**, so the package can be dropped.

---

## Surface 3 — GitHub PRs and main branch

- PR #69 `feat(vector-store): migrate knowledge_indexer from Pinecone to Qdrant` — merged (commit `116f482`)
- PR #70 `feat(qdrant): migration tools — health check + analytics + regen script` — merged (`472c568`)
- PR #71 `fix(admin): migrate _reconstruct_from_pinecone + _get_pinecone_counts to Qdrant` — merged (`aaf244b`, current HEAD)
- Open PRs: 12 total. **None mention Pinecone or Qdrant.** All are Dependabot bumps + the unrelated #39 cleanup branch.

---

## Surface 4 — Railway production

| Env var | Status | Recommendation |
|---|---|---|
| `QDRANT_URL` | ✅ set (`https://9603b013-7e07-4065-…0.aws.cloud.qdrant.io`) | keep |
| `QDRANT_API_KEY` | ✅ set (JWT) | keep |
| `QDRANT_COLLECTION_NAME` | ❌ **not set** | OK — `tools/core/config.py:431` defaults to `"training-course"`, matches the live collection (4244 points) |
| `PINECONE_API_KEY` | ⚠️ still set (`pcsk_…`) | **MUST keep until Critical #1 is fixed.** Removing it would trigger orchestrator startup failure (see below). |
| `PINECONE_INDEX_NAME` | ❌ not set | OK — `config.py:436` aliases it to `QDRANT_COLLECTION_NAME` |

Production `/health` confirms Qdrant is healthy and Pinecone is no longer being checked (no `pinecone` key in `checks` array).

---

## Surface 5 — Claude memory + project rules

- `…/memory/MEMORY.md` line 40 indexes `qdrant_migration_2026_05_20.md` ✅
- `pinecone_config.md` still present at line 16 (still listed in index) — stale, should be marked deprecated or replaced with `qdrant_config.md`
- `data_systems_state.md` index entry (line 11) still says "Pinecone 30/30" — stale
- `project_training_agent.md`: not opened in this audit, likely contains Pinecone references (cleanup follow-up)
- `.claude/rules/`: no Pinecone references found in rule files

---

## Surface 6 — Documentation

| File | Issue |
|---|---|
| `CLAUDE.md:42, :72` | Says `Pinecone RAG` and lists Pinecone as integration — needs update to Qdrant |
| `docs/decisions/005-pinecone-rag.md` | Status: `accepted` — should be marked **superseded** by a new ADR 011 (Qdrant migration) |
| `docs/RAILWAY_DEPLOYMENT.md:283` | Lists `PINECONE_API_KEY` in env-var table |
| `docs/CI_CD_PIPELINE.md:99` | Mentions `PINECONE_API_KEY` test stub |
| `SECURITY_REVIEW.md:702-709`, `SECURITY_FIXES.md:565`, `ERROR_HANDLING_AUDIT.md:460` | Contain Pinecone code snippets — historical, but should be annotated |
| `docs/plans/2026-03-15-whatsapp-assistant-design.md` | Historical plan, OK as-is |

No new ADR exists for the Qdrant migration. Recommend creating `docs/decisions/011-qdrant-migration.md`.

---

## Surface 7 — CI/CD

`.github/workflows/ci.yml`:
- Line 90: `PINECONE_API_KEY: test` — pytest env stub, harmless but stale
- Line 217: Docker import-test passes `PINECONE_API_KEY=test` — harmless but stale
- **No `QDRANT_URL` or `QDRANT_API_KEY` test stubs.** Tests work because `conftest.py` stubs `qdrant_client` at the module level, so no real URL is needed. Adding explicit env stubs (`QDRANT_URL=http://test`, `QDRANT_API_KEY=test`) would make CI parity match production wiring.

`.github/workflows/deploy.yml`: not opened in this audit (no Pinecone matches in initial scan).

---

## Critical leftovers (must fix before next deploy)

### #1 — `PINECONE_API_KEY` is hard-required at server startup

`tools/app/orchestrator.py:60`:
```python
("PINECONE_API_KEY", PINECONE_API_KEY, True),  # required=True
```

`validate_credentials()` (lines 66-94) raises `OSError` and refuses to start if any required credential is missing. Currently Railway has the legacy Pinecone key set, so startup works. **If the operator removes `PINECONE_API_KEY` from Railway (as a "cleanup"), the next deploy will crash on boot.**

Fix: flip the flag to `False` (or remove the row entirely) and add `("QDRANT_URL", QDRANT_URL, True)` + `("QDRANT_API_KEY", QDRANT_API_KEY, True)` to the list. One-line change, one test update.

This is the single blocker preventing a clean "remove `PINECONE_API_KEY` from Railway" operation.

---

## Safe leftovers (legacy aliases, OK to keep for now)

All of these execute correctly against Qdrant; they only carry "Pinecone" in their name:
- All function aliases (`get_pinecone_index`, `_get_pinecone_counts`, `_reconstruct_from_pinecone`, `sync_from_pinecone`, `backup_scores_to_pinecone`, `_safe_backup_to_pinecone`, `extract_from_pinecone`, `check_pinecone`, `check_pinecone_scores_consistency`, `_list_pinecone_vectors_for_lecture`, `PineconeHealthReport`)
- DLQ operation key `"pinecone_index"` (in-flight DLQ entries may still reference it; safe to keep)
- Cron-job IDs `pinecone_score_backup`, `drive_pinecone_audit` (APScheduler stores job IDs; renaming would invalidate existing schedule rows)
- Pipeline-state field `pinecone_indexed` (on-disk state files use this key — renaming would break recovery for any in-flight `.tmp/pipeline_state_g*_l*.json`)
- `api_resilience.KNOWN_SERVICES` includes `"pinecone"` (metric label, harmless)
- `config.PINECONE_API_KEY`, `config.PINECONE_INDEX_NAME` (compat shims explicitly documented)
- All docstring / log / comment references (≈100 lines across 12 files) — cosmetic, no functional impact

---

## Recommended cleanup follow-up PRs

| Order | PR title | Scope | Est size |
|---|---|---|---|
| 1 (**urgent**) | `fix(orchestrator): make PINECONE_API_KEY optional, require QDRANT_URL+API_KEY` | `tools/app/orchestrator.py` line 60 area, `_CREDENTIALS` list + matching test in `test_orchestrator.py` if present | XS, <30 lines |
| 2 | `docs: ADR 011 Qdrant migration + supersede ADR 005 + update CLAUDE.md/RAILWAY_DEPLOYMENT.md/CI_CD_PIPELINE.md` | new ADR, edits to 4 docs | S |
| 3 | `chore(deps): drop pinecone==8.1.0 from requirements.txt` | requirements.txt | XS — pair with PR #1 to be safe |
| 4 | `chore(ci): add QDRANT_URL + QDRANT_API_KEY test stubs, drop PINECONE_API_KEY stub` | `.github/workflows/ci.yml` lines 84-93 and 215-224 | XS |
| 5 (low priority) | `refactor: rename Pinecone aliases to Qdrant names` (functions + cron job IDs + state field `pinecone_indexed → vectors_indexed`) | Large — touches scheduler IDs and on-disk state schema. **Requires a migration script for existing `.tmp/pipeline_state_*.json` files and an APScheduler job-ID rebuild.** Defer until current cohort completes. | L |
| 6 | `chore(memory): replace pinecone_config.md with qdrant_config.md, update data_systems_state.md` | 2-3 memory files | XS |
| 7 (later) | `chore: clean up Pinecone references in docstrings/logs/comments` | ~100 lines across 12 files, cosmetic only | M |

PR #1 should be merged **before** anyone touches the Railway env vars.

---

## Confidence: HIGH

Evidence:
- `Grep -n "^(from pinecone\|import pinecone)" tools/` returns zero — no live imports.
- Production `/health` returns `qdrant: ok, collection 'training-course' has 4244 points` and no `pinecone` key.
- `git rev-parse origin/main` matches local HEAD `aaf244b` (PR #71); PRs #69/#70/#71 confirmed merged.
- Every `pinecone`-named callable in the active path was opened and verified to delegate to a `qdrant`-named implementation.
- Vector count 4244 > regen baseline 4132 confirms continued writes after migration cutoff, proving the write path is Qdrant.

One operational gotcha (orchestrator.py:60 — Critical #1) is the only thing keeping me from a PASS verdict. It's a 1-line code change, but until it ships, the migration is not safely reversible at the Railway env-var level.
