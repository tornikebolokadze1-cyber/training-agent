# Zoom Server-to-Server OAuth Setup

## Step 1: Create a Server-to-Server OAuth App

1. Go to [Zoom Marketplace](https://marketplace.zoom.us/)
2. Sign in with your Zoom Pro account
3. Click **Develop** → **Build App**
4. Choose **Server-to-Server OAuth** app type
5. Name it: `Training Agent Automation`

## Step 2: Configure Scopes

Add these scopes:
- `recording:read` — download meeting recordings
- `meeting:read` — get meeting details and invitation links
- `user:read` — list meeting participants

## Step 3: Get Credentials

Copy these values to your `.env` file:
- **Account ID** → `ZOOM_ACCOUNT_ID`
- **Client ID** → `ZOOM_CLIENT_ID`
- **Client Secret** → `ZOOM_CLIENT_SECRET`

## Step 4: Enable Webhook (Event Subscriptions)

1. In your app, go to **Feature** → **Event Subscriptions**
2. Click **Add Event Subscription**
3. **Subscription Name**: `Recording Completed`
4. **Event notification endpoint URL**: Your n8n webhook URL (you'll get this after creating Workflow #2)
5. Add event: **Recording** → `recording.completed`
6. Save

## Step 5: Enable Auto-Recording

1. Go to [Zoom Settings](https://zoom.us/profile/setting)
2. Under **Recording**, enable **Automatic recording**
3. Choose **Record to the cloud**

## Step 6: Activate the App

1. Go back to your app in the Marketplace
2. Click **Activate your app**

## Verification

Run this to test your credentials:
```bash
curl -X POST "https://zoom.us/oauth/token?grant_type=account_credentials&account_id=YOUR_ACCOUNT_ID" \
  -H "Authorization: Basic $(echo -n 'CLIENT_ID:CLIENT_SECRET' | base64)"
```

You should get back a JSON with `access_token`.
