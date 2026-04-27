# Setup Guide — Training Agent

## Prerequisites Checklist

- [ ] Zoom Pro/Business account
- [ ] Google Cloud project with Drive API + Docs API enabled
- [ ] ManyChat Pro subscription with WhatsApp channel
- [ ] Python 3.12+ installed
- [ ] n8n cloud instance (aipulsegeorgia2025.app.n8n.cloud)

## Step 1: Clone & Install

```bash
cd "/Users/tornikebolokadze/Desktop/Training Agent"
pip install -r requirements.txt
```

## Step 2: Configure Credentials

Follow these setup guides in order:

1. **Zoom OAuth** → see `docs/setup/zoom_oauth_setup.md`
   - Create Server-to-Server OAuth app
   - Get Account ID, Client ID, Client Secret
   - Enable recording.completed webhook

2. **Google OAuth** → see `docs/setup/google_oauth_setup.md`
   - Enable Drive API + Docs API
   - Download credentials.json
   - Run initial auth flow
   - Get Drive folder IDs

3. **ManyChat API** → see `docs/setup/manychat_setup.md`
   - Get API key
   - Get subscriber IDs
   - Create broadcast flows

4. **Fill `.env`** with all credentials

## Step 3: Create Lecture Folders

```bash
python -m tools.gdrive_manager
```

This creates ლექცია #1 through ლექცია #15 in both group folders (skips existing).

## Step 4: Configure n8n Workflows

Four workflows exist on the n8n instance:

| Workflow | ID | Status | Purpose |
|----------|-----|--------|---------|
| Pre-Meeting Reminders | Hsa5YDWrOytFxAL5 | Inactive | Sends emails + WhatsApp 2hrs before |
| Recording Processor (v1) | zoz7yWVzTMqhfVyd | Inactive | Original Zoom webhook handler (superseded) |
| Zoom Recording → Python | 9K6kBOFPgG8xSuff | **Active** | Catches Zoom webhook + CRC validation, triggers Python |
| Post-Processing Delivery | 1mw2v47eliAk2l1s | Inactive | Delivers results after analysis |

> **Note:** Workflow `9K6kBOFPgG8xSuff` is the active recording processor.
> It supersedes the original `zoz7yWVzTMqhfVyd` with added Zoom CRC
> challenge support. Secrets in this workflow should be moved from
> hardcoded values to n8n environment variables before production use.

**For each workflow:**
1. Open in n8n UI
2. Configure credentials (Zoom OAuth, SMTP, ManyChat HTTP Header Auth)
3. Set environment variables in n8n Settings
4. Test manually before activating

## Step 5: Start Python Server

```bash
cd "/Users/tornikebolokadze/Desktop/Training Agent"
python -m tools.app.orchestrator   # starts scheduler + server together
# Or use the startup script:
./start.sh
```

Server runs at http://localhost:5001 (port 5001 — macOS uses 5000). Verify with:
```bash
curl http://localhost:5001/health
curl http://localhost:5001/status  # full dashboard with scheduler state
```

For auto-restart on crash, the launchd service is available:
```bash
launchctl load ~/Library/LaunchAgents/com.aipulsegeorgia.training-agent.plist
```

## Step 6: Set Zoom Webhook URL

In your Zoom Marketplace app, set the webhook URL to:
```
https://aipulsegeorgia2025.app.n8n.cloud/webhook/zoom-recording
```

> This matches the active workflow `9K6kBOFPgG8xSuff`. The inactive v1
> workflow used `/webhook/zoom-recording-complete` — do NOT use that path.

## Step 7: Set n8n Callback URL

In `.env`, set:
```
N8N_CALLBACK_URL=https://aipulsegeorgia2025.app.n8n.cloud/webhook/training-agent-callback
```

## Step 8: Activate Workflows

In n8n UI, activate workflows in this order:
1. Post-Processing Delivery (receives callbacks)
2. Zoom Recording → Python (receives Zoom webhooks — already active)
3. Pre-Meeting Reminders (schedule trigger)

**Before activating**, ensure:
- n8n environment variables are set (`WEBHOOK_SECRET`, `MANYCHAT_*`, etc.)
- All credentials are configured (Zoom OAuth, SMTP, ManyChat HTTP Header Auth)
- Python server is accessible from n8n cloud (via tunnel or public deployment)
- Hardcoded secrets in workflow `9K6kBOFPgG8xSuff` are replaced with `$env` variables

## Step 9: Test

1. **Test reminders**: Manually trigger Workflow #1 in n8n
2. **Test recording**: Start a short 5-min Zoom meeting, let it record, verify pipeline
3. **Monitor**: Check n8n execution logs and Python server logs
