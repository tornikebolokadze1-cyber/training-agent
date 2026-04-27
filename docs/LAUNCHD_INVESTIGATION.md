# LaunchD Phantom Commit Investigation

**Date**: 2026-04-08  
**Investigator**: Claude Code (read-only, no changes made)  
**Status**: CONCLUDED — source identified, launchd is NOT the cause

---

## 1. Executive Summary

The 4 "phantom" commits were **not made by launchd or any automated process running inside the training agent**. They were made by **an active Claude Code agent session** that Tornike was running interactively (or semi-interactively via a tool like `oh-my-claudecode:codex` or `ralph`). The launchd service itself only runs the FastAPI/APScheduler server — it contains zero git operations and has no PATH entry for git. The real mechanism is explained in Section 5.

---

## 2. Service Inventory

Six `com.aipulsegeorgia.*` LaunchAgent plists exist in `~/Library/LaunchAgents/`:

| Label | Type | Trigger |
|---|---|---|
| `com.aipulsegeorgia.training-agent` | Keep-alive daemon | On login + auto-restart |
| `com.aipulsegeorgia.health-check` | Periodic script | Every 5 minutes |
| `com.aipulsegeorgia.notebooklm-proxy` | (not investigated) | — |
| `com.aipulsegeorgia.notebooklm-auth-refresh` | (not investigated) | — |
| `com.aipulsegeorgia.notebooklm-caffeinate` | (not investigated) | — |
| `com.aipulsegeorgia.notebooklm-auth` | (not investigated) | — |

Only the first two are relevant to this investigation.

---

## 3. Plist Analysis: com.aipulsegeorgia.training-agent

### What it runs
```
/Users/tornikebolokadze/Desktop/Training Agent/.venv/bin/python -m tools.orchestrator
WorkingDirectory: /Users/tornikebolokadze/Desktop/Training Agent
```

### Launch behaviour
- `RunAtLoad: true` — starts at every login
- `KeepAlive: true` — restarts within 15 seconds if it crashes
- `ThrottleInterval: 15` — minimum 15s between restarts

### Environment (sanitized)
```
HOME=/Users/tornikebolokadze
PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
PYTHONUNBUFFERED=1
PYTHONDONTWRITEBYTECODE=1
```

### Git access
- The PATH includes `/usr/bin` and `/opt/homebrew/bin` — `git` **is** findable.
- However, no Python file under `tools/` contains a `git commit` or `git push` call. A full grep of all `.py` files found only:
  - `obsidian_sync.py` lines 143-144: string literals in a concept-mapping dict (`"commit (git commit)": "Git"`) — not executable code.
  - `scripts/deploy.sh` line 44: `git push` — but this script is only invoked manually by the user with `./scripts/deploy.sh --push-first`.
- **Conclusion**: the Python orchestrator never touches git.

### Log file
`StandardOutPath` and `StandardErrorPath` both write to `/tmp/training-agent-launchd.log`.

The last 100 lines of that log show only the Lecture 8 pipeline running normally (Gemini transcription → Claude analysis → Drive upload → WhatsApp → Pinecone → Obsidian sync), completing at `12:27:17`. No git operations appear anywhere in the log.

---

## 4. Plist Analysis: com.aipulsegeorgia.health-check

### What it runs
```
/bin/sh /Users/tornikebolokadze/Desktop/Training Agent/scripts/health_check.sh
StartInterval: 300 (every 5 minutes)
KeepAlive: false
```

### What health_check.sh does
Polls `http://localhost:5001/health`, and if it fails, sends a WhatsApp alert via Green API. That is the entirety of the script. It contains **no git commands**.

---

## 5. Source of the Phantom Commits

### The 4 commits in question
| Hash | Timestamp (GMT+4) | Message |
|---|---|---|
| `d602f12` | 2026-04-08 14:08:10 | fix: pattern-based dedup for lecture recordings |
| `8d120ea` | 2026-04-08 14:28:53 | fix: smaller Gemini chunks + Drive↔Pinecone audit |
| `3550d4b` | 2026-04-08 15:50:14 | fix: audit detects missing content-types |
| `8fe2028` | 2026-04-08 16:13:40 | feat: structural reliability layer (3 modules + health checks) |

### Key forensic observations

**1. Author identity is Tornike, not a bot.**  
Every commit in the repository — including the 4 in question — carries:
```
author: Tornike Bolokadze <tornikebolokadze@Tornikes-MacBook-Air.local>
```
There is no `noreply@anthropic.com` co-author, no generic service account. Git is configured with Tornike's real identity. An automated non-interactive process would produce the same signature if it ran inside the same working tree, but the content of the commits (multi-file refactors with test coverage and amend operations) is inconsistent with fully-unattended automation.

**2. The reflog shows `commit (amend)` operations.**  
```
3550d4b HEAD@{9}:  commit (amend): fix: audit detects missing content-types
ea0e121 HEAD@{10}: commit (amend): fix: audit detects missing content-types
4af1727 HEAD@{11}: commit (amend): fix: audit detects missing content-types
ac8e20c HEAD@{12}: commit: fix: audit detects missing content-types
8d120ea HEAD@{14}: commit (amend): fix: smaller Gemini chunks ...
```
A fully-automated daemon does not interactively amend commits three times. This is an agent (Claude Code) iterating — running tests, finding issues, amending the commit to fix them.

**3. Timing places commits entirely within a known Claude Code session.**  
The pipeline for Lecture 8 completed at `12:27:17`. The CHECKPOINT commit was made at `12:31:57`. The first "phantom" commit (`ce2ddab`, "stabilize pipeline") appeared at `13:18:04` — about 46 minutes later. The remaining 4 commits followed over the next ~1h45m (14:08 → 16:13). This is consistent with a Claude Code agent session initiated after the lecture pipeline finished, not an unattended daemon.

**4. The commit message style matches Claude Code / oh-my-claudecode agent output.**  
Messages like "structural reliability layer (3 modules + health checks)", "Drive↔Pinecone audit + dedup edge tests", and "Phases 1-5 per Codex report" are characteristic of an `oh-my-claudecode:codex` or `ralph` run that was either explicitly started by Tornike or left running in autopilot mode.

**5. No git-touching code exists anywhere in the automation stack.**  
A full grep of all `.py` and `.sh` files (excluding `.venv`) found zero instances of `git commit` or `git push` in executable positions.

### Most probable mechanism

Tornike started a Claude Code agent session (likely `oh-my-claudecode:codex`, `ralph`, or `autopilot`) earlier on 2026-04-08, and it continued working autonomously after Tornike stepped away. The session's built-in commit hooks (from `.claude/rules/01-auto-checkpoint.md` and `17-development-workflow.md`) caused it to commit each completed fix to the working tree — which launchd's training-agent was simultaneously running from that same working tree.

This matches the warning in `feedback_phantom_edits.md`: **"launchd runs working tree not HEAD"** — meaning if the agent commits new Python code, the running server picks up the changed `.py` files on its next import (or restart), creating the illusion that the server "caused" the commits when in reality it was the agent that committed and the server that accidentally picked up the changes.

---

## 6. Risk Assessment

| Risk | Level | Notes |
|---|---|---|
| launchd making unauthorized commits | **NONE** | Confirmed: no git code in the automation stack |
| Claude Code agent committing unreviewed code | **MEDIUM** | Happens during long autonomous sessions |
| Launchd server running mid-session uncommitted code | **LOW** | Server hot-picks `.py` changes from working tree |
| Obsidian vault committed with ephemeral WIP files | **LOW** | Vault is in the working tree and gets swept into agent commits |
| Accidental `git push` during an agent session | **LOW** | `deploy.sh` requires manual invocation; no auto-push in hooks |

---

## 7. Recommended Remediation

### Option A: No action (current situation is safe)
The launchd service is not the problem. If Tornike knowingly started a Claude Code agent session and the commits are the result of that session, there is nothing to fix. The "phantom" quality is simply because the session ran longer than expected.

**Recommended if**: Tornike remembers starting a code-improvement session on the morning of April 8.

### Option B: Restrict autonomous Claude Code sessions
To prevent future surprise commits:
1. Before starting a long `ralph`/`autopilot` session, create a feature branch:
   ```bash
   git checkout -b agent/YYYY-MM-DD-description
   ```
2. Review the branch before merging to `main`.
3. This does not require touching launchd at all.

### Option C: Isolate the launchd server from the working tree (belt-and-suspenders)
If future concern remains about launchd hot-loading uncommitted code:
1. Create a dedicated deploy directory (e.g., `~/training-agent-deploy/`)
2. Change the plist `WorkingDirectory` and `ProgramArguments` to point there
3. Deploy to it explicitly: `rsync` or `git worktree`

This is the architecturally cleanest separation but requires updating the plist and redoing the venv.

**Recommended only if**: it becomes operationally important that the running server is always on a pinned commit.

---

## 8. Disable Procedure (if ever needed — NOT recommended now)

The launchd training-agent service is NOT the problem and disabling it would stop the lecture processing pipeline entirely. If it ever does need to be disabled:

```bash
# Step 1: Stop the running process (launchd will try to restart it — that's OK for now)
launchctl stop com.aipulsegeorgia.training-agent

# Step 2: Unload the plist so launchd stops managing it
launchctl unload ~/Library/LaunchAgents/com.aipulsegeorgia.training-agent.plist

# Step 3: Verify it is gone
launchctl list | grep training-agent   # should return nothing

# To re-enable later:
launchctl load ~/Library/LaunchAgents/com.aipulsegeorgia.training-agent.plist
```

**Blast radius of disabling**:
- Lecture recording pipeline will NOT trigger automatically after meetings
- Pre-meeting WhatsApp/email reminders will NOT send
- WhatsApp assistant (`მრჩეველი`) will go offline
- Health-check alerts will report the server as down (expected)
- No data loss — all state is in SQLite (`data/scores.db`), Pinecone, and Google Drive

The `com.aipulsegeorgia.health-check` service can remain loaded independently; it will simply report failures until the server is restored.

**Rollback**: re-run the `launchctl load` command above. The server will restart within 15 seconds.

---

## 9. Conclusion

| Question | Answer |
|---|---|
| Is launchd running a Claude Code agent loop automatically? | **No.** It only runs the Python orchestrator. |
| Does the service have git commit/push enabled? | **No.** Zero git operations in the entire automation stack. |
| What triggered the commits? | A Claude Code agent session (ralph/autopilot/codex) running on 2026-04-08. |
| Is it safe to disable the service? | Yes, but doing so stops all lecture automation. Not recommended. |
| What is the blast radius of disabling? | Full lecture pipeline offline; no data loss. |

**Action required**: None for launchd. If the autonomous agent sessions are a concern, adopt a feature-branch workflow for agent runs (Option B above).
