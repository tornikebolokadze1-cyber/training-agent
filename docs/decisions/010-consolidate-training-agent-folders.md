# 010: Consolidate Training Agent Folders — Canonical `~/training-agent/` + Desktop Archive

## Date
2026-04-20

## Status
implemented — Phases 1-3 complete (2026-04-20) via AIP-43

## Context

Two parallel copies of the Training Agent codebase existed on the CTO workstation:

1. **Canonical** — `C:\Users\AI Pulse Georgia\training-agent\` — the live tree that
   `Training-Agent-Start.cmd`, the FastAPI `:8000` service, and the Paperclip
   `adapterConfig` have all pointed at for months.
2. **Legacy** — `C:\Users\AI Pulse Georgia\Desktop\Mac\AI Agents\Training Agent\` —
   a standalone pre-monorepo checkout that was never formally retired. It had its
   own `.git/` history and had silently diverged in a handful of files, most notably
   `tools/app/openclaw_bridge.py` (OpenClaw / Chief Research Officer gateway — the
   legacy copy had a meaningful superset of handlers that had never been ported to
   the canonical tree).

Keeping both folders caused three concrete failure modes:

- **Edit drift.** An agent that read the wrong copy could silently edit stale code.
- **Doctor confusion.** `training-agent-doctor.sh` had branches for "legacy folder
  present, warn until AIP-43" — those branches kept firing and masking real issues.
- **Onboarding ambiguity.** A new agent joining mid-session could not tell from
  `ls ~/Desktop/...` which tree was authoritative without reading the start script.

## Decision

Consolidate to the canonical `~/training-agent/` as the single source of truth.
Freeze the Desktop copy as a read-only, hash-verified archive retained for 90 days
as an incident-recovery fallback, then delete the archive during the next storage
cleanup pass after 2026-07-19.

Execution was three phases, all completed 2026-04-20 under AIP-43:

### Phase 1 — Reconcile
- Diff legacy vs canonical. The only functionally significant divergence was
  `tools/app/openclaw_bridge.py` (OpenClaw gateway).
- Port the legacy superset into the canonical tree, including the
  `fetch_paperclip_issue` fallback for context-only dispatches.
- Verify `tools/tests/test_openclaw_bridge.py` suite — 23/23 passing on the
  canonical tree.

### Phase 2 — Archive
- `robocopy` the legacy tree to `Desktop\Mac\AI Agents\Training Agent.archived-2026-04-20\`,
  excluding regenerable junk (`.venv/`, `.tmp/`, `.claude/` worktrees, Playwright
  caches, `__pycache__`, macOS AppleDouble metadata, `.coverage`).
- Final archive: 78 MB, 4,249 files — includes all source, full `.git/` history,
  docs, `logs/`, and the tiny `data/` SQLite state files.
- Generate `MANIFEST.sha256` with SHA-256 for every file; spot-check verified.
- Write `ARCHIVE_README.md` documenting what is / is not included, integrity
  verification procedure (`sha256sum -c MANIFEST.sha256`), the 90-day retention
  policy, and links back to this ADR and AIP-43.
- PowerShell-delete the original `Desktop\Mac\AI Agents\Training Agent\`.

### Phase 3 — Verify
- Patch `Paperclip/scripts/training-agent-doctor.sh` Check 2 path bug
  (`${folder}/server.py` → `${folder}/tools/app/server.py` — the canonical layout
  has `server.py` under `tools/app/`, not at the root).
- Run the doctor: exits 0, `DOCTOR: CLEAN` across all 7 checks (folder existence,
  bridge mount, `.env` key count, drift, live `:8000` health, adapter secret match,
  Training Ops Lead heartbeat).
- Confirm `Training-Agent-Start.cmd` still points at `%USERPROFILE%\training-agent`.

## Reasoning

**Why canonical over legacy.** `Training-Agent-Start.cmd`, the live FastAPI service,
and the Paperclip `adapterConfig` all already targeted `~/training-agent/`. Keeping
that tree meant zero changes to the orchestrator, zero secret rotation, zero agent
downtime. Moving to the Desktop copy would have required updating all three.

**Why archive instead of delete outright.** The legacy `.git/` contains commits that
are not in the canonical repo. If an incident three weeks from now needs to consult
an old migration script or a pre-consolidation audit log, we want that state recoverable
without restoring from a full backup.

**Why SHA-256 manifest.** An archive is only trustworthy if you can detect tampering
or silent corruption. `sha256sum -c MANIFEST.sha256` gives a one-command integrity
check, which is what the recovery procedure requires.

**Why exclude `.venv/`, `.tmp/`, `.claude/`.** These are regenerable (`.venv` rebuilds
from `pyproject.toml`; `.tmp` and `.claude` worktrees are scratch) and together were
~2.1 GB vs 78 MB of actual code + history. Archiving them would have 30× the storage
for zero recovery value.

**Why patch the doctor script instead of working around it.** The doctor is the
source of truth for "is the training agent healthy." A false-positive warning about
a legacy folder that no longer exists would quietly erode trust in every future run.
The single-line path fix was a smaller change than adding another exception.

## Consequences

**Positive:**
- One authoritative tree. No more "which copy did you edit?" ambiguity.
- Doctor runs CLEAN end-to-end. Future drift is detectable.
- `openclaw_bridge.py` is now tested in the live tree (23/23 passing), not sitting
  in a Desktop copy that no one exercises.

**Negative:**
- 78 MB of archive storage for 90 days minimum.
- If a future agent reflexively searches `Desktop\Mac\AI Agents\` for "Training
  Agent" code, they'll find only the archive and might be briefly confused until
  they read `ARCHIVE_README.md`. Mitigated by the README's prominent canonical
  pointer.

**Neutral:**
- `Paperclip/scripts/training-agent-doctor.sh` now assumes canonical layout
  (`tools/app/server.py`). If the Training Agent ever moves `server.py` back to
  root, the doctor must be updated. That's unlikely — the `tools/app/` layout is
  the long-standing monorepo convention.

## Related
- Issue: **AIP-43** — "Consolidate Training Agent folders (canonical + archive
  Desktop copy)"
- Archive: `C:\Users\AI Pulse Georgia\Desktop\Mac\AI Agents\Training Agent.archived-2026-04-20\`
- Runbook touched: `docs/operations/runbooks/aip-6-rotate-training-webhook.md`
- Verification script: `C:\Users\AI Pulse Georgia\Paperclip\scripts\training-agent-doctor.sh`
- Earliest legacy-archive deletion date: **2026-07-19**
