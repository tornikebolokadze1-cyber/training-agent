# ManyChat API Setup for WhatsApp

## Step 1: ManyChat Pro Subscription

1. Go to [ManyChat](https://manychat.com/)
2. Ensure you have **ManyChat Pro** (required for API access)
3. Connect your **WhatsApp Business** channel

## Step 2: Get API Key

1. Go to **Settings** → **API**
2. Copy the API key → paste into `.env` as `MANYCHAT_API_KEY`

## Step 3: Find Your Subscriber ID

Your personal subscriber ID is needed for private gap analysis messages:
1. Go to **Contacts** in ManyChat
2. Find your own contact
3. Copy the subscriber ID → paste into `.env` as `MANYCHAT_TORNIKE_SUBSCRIBER_ID`

## Step 4: Create Broadcast Flows

Create two flows for group meeting reminders:

### Group #1 Flow (Tuesday/Friday)
1. Go to **Automation** → **Flows**
2. Create a new flow: `Training Reminder - Group 1`
3. Add a **WhatsApp** message step with dynamic content (the Zoom link will be injected via API)
4. Note the Flow ID → paste into `.env` as `MANYCHAT_GROUP1_FLOW_ID`

### Group #2 Flow (Monday/Thursday)
1. Repeat for Group #2: `Training Reminder - Group 2`
2. Note the Flow ID → paste into `.env` as `MANYCHAT_GROUP2_FLOW_ID`

## Step 5: Add Training Participants

Ensure all training participants are added as WhatsApp subscribers in ManyChat:
1. Go to **Growth Tools** → share the opt-in link with participants
2. Or manually add contacts via **Contacts** → **Import**

## API Reference

**Send Content to Subscriber:**
```
POST https://api.manychat.com/fb/sending/sendContent
Headers: Authorization: Bearer {API_KEY}
Body: {
  "subscriber_id": "...",
  "data": { "messages": [{ "type": "text", "text": "..." }] }
}
```

**Trigger Flow for Subscriber:**
```
POST https://api.manychat.com/fb/sending/sendFlow
Headers: Authorization: Bearer {API_KEY}
Body: {
  "subscriber_id": "...",
  "flow_ns": "..."
}
```
