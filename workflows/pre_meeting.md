# Pre-Meeting Reminder Workflow

## Overview
Posts Zoom invitation link to WhatsApp group 2 hours before each training session.
Email reminders are handled automatically by Zoom Workflows (built-in Zoom automation with Gmail).

## Trigger
- **When**: 14:00 UTC (18:00 Georgian time)
- **Days**: Monday, Tuesday, Thursday, Friday
- **n8n Workflow ID**: Hsa5YDWrOytFxAL5

## Flow
1. Schedule fires at 18:00 Georgian time
2. Code node determines which group meets today, calculates lecture number, prepares attendee list
3. **Creates Zoom meeting** via API (`POST /users/me/meetings`) with all attendees — Zoom auto-sends email invitations
4. Extracts join URL from API response
5. Posts Zoom link to WhatsApp group via ManyChat API

**Note**: Zoom Workflows ("ლექცია (ჯგუფი #1/2)") are no longer needed — n8n creates meetings directly via Zoom API. Meeting settings: 2hr duration, auto cloud recording, Dubai/Tbilisi timezone.

**Note**: Emails are NOT sent by n8n. Zoom has its own workflows ("ლექცია (ჯგუფი #1)" and "ლექცია (ჯგუფი #2)") that schedule meetings and send email invites to all 17 attendees automatically via Gmail.

## Group Schedule
| Day | Group | Zoom Workflow |
|-----|-------|---------------|
| Monday | Group #2 | ლექცია (ჯგუფი #2) |
| Tuesday | Group #1 | ლექცია (ჯგუფი #1) |
| Thursday | Group #2 | ლექცია (ჯგუფი #2) |
| Friday | Group #1 | ლექცია (ჯგუფი #1) |

## Troubleshooting

### WhatsApp not posting
- Verify ManyChat API key is valid
- Check subscriber IDs are correct
- Verify ManyChat Pro subscription is active

### Wrong lecture number
- Check group start dates in the Code node (Group 1: 2026-03-03, Group 2: 2026-03-02)
- Lecture numbers auto-calculate from start date

### Zoom link not fetching
- Verify Zoom OAuth credentials are configured in n8n
- Check that the Zoom meeting ID is correct in environment variables
