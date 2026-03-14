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

This creates ლექცია #2 through ლექცია #15 in both group folders.

## Step 4: Configure n8n Workflows

Three workflows have been created on your n8n instance:

| Workflow | ID | Purpose |
|----------|-----|---------|
| Pre-Meeting Reminders | Hsa5YDWrOytFxAL5 | Sends emails + WhatsApp 2hrs before |
| Recording Processor | zoz7yWVzTMqhfVyd | Catches Zoom webhook, triggers Python |
| Post-Processing Delivery | 1mw2v47eliAk2l1s | Delivers results after analysis |

**For each workflow:**
1. Open in n8n UI
2. Configure credentials (Zoom OAuth, SMTP, ManyChat HTTP Header Auth)
3. Set environment variables in n8n Settings
4. Test manually before activating

## Step 5: Start Python Server

```bash
cd "/Users/tornikebolokadze/Desktop/Training Agent"
python -m tools.server
```

Server runs at http://localhost:5000. Verify with:
```bash
curl http://localhost:5000/health
```

## Step 6: Set Zoom Webhook URL

In your Zoom Marketplace app, set the webhook URL to:
```
https://aipulsegeorgia2025.app.n8n.cloud/webhook/zoom-recording-complete
```

## Step 7: Set n8n Callback URL

In `.env`, set:
```
N8N_CALLBACK_URL=https://aipulsegeorgia2025.app.n8n.cloud/webhook/training-agent-callback
```

## Step 8: Activate Workflows

In n8n UI, activate all 3 workflows (in this order):
1. Post-Processing Delivery (receives callbacks)
2. Recording Processor (receives Zoom webhooks)
3. Pre-Meeting Reminders (schedule trigger)

## Step 9: Test

1. **Test reminders**: Manually trigger Workflow #1 in n8n
2. **Test recording**: Start a short 5-min Zoom meeting, let it record, verify pipeline
3. **Monitor**: Check n8n execution logs and Python server logs
