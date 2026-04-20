# Runbook — Rotate Training Agent Paperclip Webhook & Verify Contract

**Last verified:** 2026-04-20 (AIP-42 follow-up)
**Owner:** CTO / Training Ops Lead
**Related issues:** AIP-6 (original hookup), AIP-42 (422 on heartbeat POST)

---

## 1. Purpose

The Training Agent FastAPI bridge at `http://127.0.0.1:8000/paperclip/task` receives dispatch POSTs
from the Paperclip HTTP adapter whenever the **Training Operations Lead** agent
(`6808bc6e-243f-4cf5-8b0c-8cd8cb360fb3`) is woken — either automatically by heartbeat or manually
from the board.

This runbook covers:

1. The authoritative **payload contract** the bridge must accept.
2. How to **rotate** the shared bearer token (`PAPERCLIP_WEBHOOK_SECRET`).
3. How to **smoke-test** the bridge end-to-end after any change.

---

## 2. Payload contract (authoritative, as of 2026-04-20)

Paperclip's HTTP adapter (`packages/adapter-utils` → `adapters/http/execute.ts`) builds the body as:

```ts
const body = { ...payloadTemplate, agentId: agent.id, runId, context };
```

The bridge must accept **all three** shapes below. Missing `issueId` in any of them = treat as
idle no-op, not as an error.

### 2.1 Native HTTP-adapter dispatch (issue-linked)

Fires when Paperclip heartbeats an agent that has an assigned issue.

```json
{
  "agentId": "6808bc6e-243f-4cf5-8b0c-8cd8cb360fb3",
  "runId": "cd727a1c-f391-4847-bbad-ef81c9fc0824",
  "context": {
    "issueId": "88cd1329-b99e-4761-a476-99541086ea38",
    "taskId": "88cd1329-b99e-4761-a476-99541086ea38",
    "taskKey": "AIP-42",
    "projectId": "…",
    "projectWorkspaceId": "…"
  }
}
```

**Handler action:** extract `context.issueId`, hydrate title/description via
`GET /api/issues/{id}`, classify intent, schedule background dispatch, return **202**.

### 2.2 Manual/on-demand wake (no issue)

Fires when the board posts `POST /api/agents/{id}/wakeup` with no linked issue, or when
a scheduled heartbeat fires with no queue item. **No `issueId`** in the context.

```json
{
  "agentId": "6808bc6e-243f-4cf5-8b0c-8cd8cb360fb3",
  "runId": "…",
  "context": {
    "actorId": "local-board",
    "wakeSource": "on_demand",
    "triggeredBy": "board",
    "forceFreshSession": false,
    "wakeTriggerDetail": "manual"
  }
}
```

**Handler action:** return **202** with `{status:"accepted", issueId:null, intent:"idle_wake"}`.
**Do not 422.** A 422 flips the heartbeat-run to `failed` and pins the agent to status `error`
until the next successful run — which cannot happen while the bridge keeps rejecting wake-ups.

### 2.3 Legacy / direct-call shapes

Flat (`{issueId, title, description, runId}`) and wrapped (`{issue:{…}, runId}`) payloads remain
accepted for backwards compat with manual curl tests and older dispatch paths.

### 2.4 Authentication

Every request must carry `Authorization: Bearer <PAPERCLIP_WEBHOOK_SECRET>`.

| Condition | Response |
|---|---|
| Secret unset in `.env` | **503** — bridge refuses to accept dispatches |
| Missing header | **401** |
| Wrong secret | **401** |
| Correct secret + valid payload | **202** |

### 2.5 Response envelope

```json
{
  "status": "accepted",
  "runId": "…",
  "issueId": "…|null",
  "intent": "smoke_test|process_recording|pre_meeting_reminder|unknown|idle_wake"
}
```

Paperclip's adapter considers **any 2xx as success** (`res.ok` → `exitCode: 0`). Anything else
throws `HTTP invoke failed with status <N>` and the heartbeat-run is marked `failed`.

---

## 3. Rotating the webhook secret

### 3.1 Preconditions

- Bridge is currently healthy (`curl http://127.0.0.1:8000/healthz` returns 200).
- You have write access to both the bridge `.env` and the Paperclip server secrets store.

### 3.2 Steps

1. **Generate a new secret (43+ bytes, URL-safe):**
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
2. **Update the bridge `.env`:**
   ```bash
   cd "C:/Users/AI Pulse Georgia/training-agent"
   sed -i 's/^PAPERCLIP_WEBHOOK_SECRET=.*/PAPERCLIP_WEBHOOK_SECRET=<NEW>/' .env
   ```
3. **Update Paperclip's HTTP adapter config for the Training Ops Lead agent:**
   - Navigate to the agent's adapter config in the Paperclip admin UI (or patch via
     `PATCH /api/agents/{id}` with `adapterConfig.headers.authorization = "Bearer <NEW>"`).
   - Per `reference_paperclip_secret_resolution.md`: HTTP-adapter **headers** are NOT
     resolved from vault refs. The literal `Bearer …` string must be written to the config.
4. **Restart the bridge** (see §4.1).
5. **Smoke-test** (see §4.2). If the smoke fails, roll back both values simultaneously.

### 3.3 Rotation cadence

Rotate every 90 days, or immediately upon any suspected leak, lost machine, or personnel change.

---

## 4. Smoke-test procedure

### 4.1 Restart the bridge

```bash
# Find and stop the running uvicorn
PID=$(netstat -ano | grep ":8000 " | grep LISTENING | awk '{print $NF}')
powershell -Command "Stop-Process -Id $PID -Force"

# Start fresh (background)
cd "C:/Users/AI Pulse Georgia/training-agent"
python -m uvicorn tools.app.server:app --host 127.0.0.1 --port 8000 --log-level info \
  > /tmp/bridge.log 2>&1 &

# Verify listening
sleep 5 && netstat -ano | grep ":8000 "
```

### 4.2 Smoke #1 — no-issue idle wake (must return 202, not 422)

```bash
TOKEN=$(grep ^PAPERCLIP_WEBHOOK_SECRET "C:/Users/AI Pulse Georgia/training-agent/.env" | cut -d= -f2)
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8000/paperclip/task \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"agentId":"6808bc6e-243f-4cf5-8b0c-8cd8cb360fb3","runId":"smoke-idle-1","context":{"actorId":"local-board","wakeSource":"on_demand","triggeredBy":"board","forceFreshSession":false,"wakeTriggerDetail":"manual"}}'
# Expect: 202
```

### 4.3 Smoke #2 — full Paperclip wakeup path

```bash
# Fire the real wakeup
RUN=$(curl -s -X POST http://127.0.0.1:3100/api/agents/6808bc6e-243f-4cf5-8b0c-8cd8cb360fb3/wakeup \
  -H "Content-Type: application/json" -d '{}' | python -c 'import sys,json;print(json.load(sys.stdin)["id"])')

# Poll the heartbeat-run (use /api/heartbeat-runs/{id}, not /api/runs/{id})
sleep 4
curl -s http://127.0.0.1:3100/api/heartbeat-runs/$RUN \
  | python -c 'import sys,json;r=json.load(sys.stdin);print("status:",r["status"],"exit:",r["exitCode"],"err:",r["error"])'
# Expect: status: succeeded  exit: 0  err: None

# Confirm agent status flipped back
curl -s http://127.0.0.1:3100/api/agents/6808bc6e-243f-4cf5-8b0c-8cd8cb360fb3 \
  | python -c 'import sys,json;a=json.load(sys.stdin);print("status:",a["status"])'
# Expect: status: idle
```

### 4.4 Unit/integration tests

```bash
cd "C:/Users/AI Pulse Georgia/training-agent"
python -m pytest tools/tests/test_paperclip_bridge.py -q
# Expect: 34 passed
```

---

## 5. Known gotchas

- **Comments reopen `done` issues.** Per `reference_paperclip_comment_reopens_done_issue.md`,
  always PATCH status as the **final** action. Post the ship comment first, then PATCH; never
  comment again after close.
- **Headers are not vault-resolved.** `adapterConfig.headers.authorization` must contain the
  literal `Bearer <token>` — no `{{vault:…}}` substitution runs on headers.
- **Paperclip's run endpoint is `/api/heartbeat-runs/{runId}`** — not `/api/runs/*`,
  `/api/agents/{id}/runs`, or `/api/agent-runs/*` (all 404).
- **Auto-retry storm.** If multiple dispatches fail, Paperclip's auto-retry-continuation will
  fire repeated wake-ups. If the loop is acute, unassign the agent
  (`PATCH /api/issues/{id}` with `assigneeAgentId: null`) to break it.

---

## 6. Change log

| Date | Change | Link |
|---|---|---|
| 2026-04-20 | Added §2.2 no-issue wake + smoke #1 after AIP-42 follow-up | commit `cbc1404` |
| 2026-04-19 | Initial bridge + contract after AIP-6 | commit `b226e0c` |
