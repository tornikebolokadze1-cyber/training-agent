# Pre-Meeting Reminder Workflow

## Current state (2026-05-19)

**Authoritative path:** `tools/app/scheduler.py` → `pre_meeting_job()`. The
Python APScheduler is the single source of truth for pre-lecture WhatsApp
reminders and Zoom meeting creation. Only groups with
`course_completed=False` are scheduled — for the May 2026 cohort that is
groups 3 and 4 (`მაისის ჯგუფი #1` / `მაისის ჯგუფი #2`).

**Deprecated:** the legacy n8n workflow `Hsa5YDWrOytFxAL5` ("Pre-Meeting
Reminders") on aipulsegeorgia2025.app.n8n.cloud. It targets the March
groups (#1, #2) by their old WhatsApp chat IDs and must stay **Inactive**.
If it is reactivated, March students receive duplicate reminders while
the May groups also receive theirs — the exact symptom observed on
2026-05-18 / 2026-05-19.

If you see WhatsApp reminders landing in the March chats:
1. Open the n8n UI → Workflows → `Hsa5YDWrOytFxAL5`.
2. Toggle Active → Inactive.
3. Confirm by waiting for the next 18:00 Tbilisi tick — only the active
   May cohort chat for that weekday should receive the reminder.

## Overview (Python implementation)
Posts Zoom invitation link to the active cohort's WhatsApp group two hours
before each training session. Email invitations are handled by Zoom
itself (attendee list passed at meeting creation time).

## Trigger
- **When**: 18:00 Tbilisi time (14:00 UTC)
- **Days**: depends on each active group's `meeting_days` — for May 2026:
  - `მაისის ჯგუფი #1` (internal id 3): Mon + Thu
  - `მაისის ჯგუფი #2` (internal id 4): Tue + Fri
- **Source**: `tools/app/scheduler.py::start_scheduler()` registers one
  cron job per `(group, meeting_day)` only for groups returned by
  `iter_active_groups()`.

## Flow
1. APScheduler fires `pre_meeting_job(group_number)` at 18:00 Tbilisi.
2. `get_lecture_number(group_number, today)` computes which lecture (1–15) it is.
3. `tools/integrations/zoom_manager.py::create_meeting()` creates the Zoom
   meeting with the cohort's attendee emails — Zoom auto-sends email
   invites and the schedules a watchdog that force-starts cloud
   recording 2 min after the meeting starts.
4. `tools/integrations/whatsapp_sender.py::send_group_reminder()` posts
   the join URL to the cohort's WhatsApp chat (Green API).
5. A post-meeting safety-net job is added for 23:30 in case the
   `meeting.ended` webhook fails.

## Group Schedule (current cohort)
| Day | Group (internal id) | WhatsApp chat target |
|-----|---------------------|----------------------|
| Monday | მაისის ჯგუფი #1 (3) | `WHATSAPP_GROUP3_ID` |
| Tuesday | მაისის ჯგუფი #2 (4) | `WHATSAPP_GROUP4_ID` |
| Thursday | მაისის ჯგუფი #1 (3) | `WHATSAPP_GROUP3_ID` |
| Friday | მაისის ჯგუფი #2 (4) | `WHATSAPP_GROUP4_ID` |

> **Why "internal id 3/4" for the May cohort?** Internal IDs 1 and 2 are
> permanently reserved for the completed March cohort. Each new cohort
> takes the next free internal id, but the user-facing name resets to
> "#1" / "#2" per cohort. The env vars (`WHATSAPP_GROUP3_ID`,
> `DRIVE_GROUP3_FOLDER_ID`, ...) reflect the internal id and stay stable
> across cohort lifecycles.

## Troubleshooting

### Duplicate WhatsApp reminders (legacy n8n still firing)
- Symptom: both a March-cohort chat and a May-cohort chat receive a
  reminder at 18:00, on the same days the active cohort would normally
  meet (Mon → March #2 + May #1, Tue → March #1 + May #2, etc.).
- Root cause: n8n workflow `Hsa5YDWrOytFxAL5` was reactivated and is
  pointing at the old March WhatsApp chats via the legacy ManyChat /
  Green API node. The Python scheduler is correct on its own.
- Fix: open n8n UI, find the workflow, set it to **Inactive**.

### WhatsApp not posting (active cohort)
- Verify `GREEN_API_INSTANCE_ID` and `GREEN_API_TOKEN` env vars are set
  on Railway. Hit `GET /admin/groups-debug` (behind `WEBHOOK_SECRET`)
  to see masked chat IDs and which groups are loaded.
- Check `tools/integrations/whatsapp_sender.py` rate-limiter logs —
  Green API errors are retried via the DLQ.

### Wrong lecture number
- Check group start dates and `meeting_days` in `tools/core/config.py`
  and the `GROUP{N}_START_DATE` / `GROUP{N}_MEETING_DAYS` env vars for
  any newer cohort.
- Lecture numbers are computed by `get_lecture_number(group, date)`
  from the cohort's `start_date` minus `EXCLUDED_DATES`.

### Zoom link not fetching
- Verify Zoom S2S OAuth env vars (`ZOOM_ACCOUNT_ID`, `ZOOM_CLIENT_ID`,
  `ZOOM_CLIENT_SECRET`) are set on Railway.
- The `pre_meeting_job` falls back to a placeholder text and still
  sends the WhatsApp reminder so the lecture isn't silently dropped.
