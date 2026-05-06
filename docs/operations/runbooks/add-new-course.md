# ახალი კურსის დამატება (Add New Course)

**დაკრიტიკული ოპერაციული სახელმძღვანელო** — უფროსი ოპერატორი (Tornike) აძლიერებს ახალ AI ტრენინგის კურსს ორი ახალი WhatsApp ჯგუფით.

---

## მოთხოვნილი ინფორმაცია

სანამ დაიწყებთ, შეაგროვეთ და დაამტკიცეთ ეს ღირებულებები:

- **WhatsApp group ID** (ფორმატი: `120363XXX@g.us`) — ლოკალური ნომერი და ღრმა ჯგუფის ID
- **Attendee email list** — CSV ან თითო ელ.ფოსტა თითო სტრიქონზე (მოსწავლე + მასწავლებელი)
- **Schedule parameters**:
  - Meeting days: რომელი კვირის დღეები (მაგ. ორშაბათი + ხუთშაბათი)
  - Start time: 20:00 Tbilisi GMT+4 (ან სხვა თუ სპეციფიკური)
  - Course start date: პირველი ლექციის თარიღი (ISO ფორმატი: YYYY-MM-DD)
  - Total lecture count: 15 (ან სხვა, თუ განსხვავებული კურსი)
- **Course display name** — ქართულად (მაგ. "მაისის ჯგუფი #3")
- **Google Drive setup**:
  - Parent folder ID (სადაც ყველა ლექცია აიტვირთება)
  - Separate analysis folder ID (private reports — სამი ნაბიჯი უკან, არ კვეთთან)
- **Zoom meeting ID** (თუ რეგისტრაციის მეთოდი დაელოდება)
- **Green API verification** — დაამტკიცეთ რომ bot (995579225809) დამატებულია WhatsApp ჯგუფში

---

## ნაბიჯები

### ნაბიჯი 1: WhatsApp გრუპის მომზადება

შექმენით ახალი WhatsApp ჯგუფი, დაამატეთ მოსწავლეები და დამტკიცეთ ბოტის წევრობა.

1. Telegram ან WhatsApp Desktop-ზე შექმენით ახალი ჯგუფი სახელით (მაგ. "AI კურსი #3") და დაამატეთ ყველა ელ.ფოსტის მფლობელი.
2. დაამატეთ ბოტი: დაიწვიეთ ნომერი `995579225809` Green API ინსტანციის სახელით (Tornike-ს ელ.ფოსტის მეშვეობით).
3. Group ID აღწერის საჭიროა: მოგვანიშვნეთ ხელახლა WhatsApp Web-ზე Green API getChatHistory endpoint-ისთვის:
   ```bash
   # White ხაკი: ლოკალურ შელში Green API JSON credentials-დან
   curl -X GET "https://api.green-api.com/waInstance${INSTANCE_ID}/getChatHistory/${TOKEN}" \
     -H "Content-Type: application/json" \
     -d '{"chatId":"120363XXX@g.us"}'
   # გამორეკი ჯგუფის ID სიტემატური გამოხმელი ჩანაწერი
   ```
4. ან უფრო მარტივი: გაუგზავნეთ Test message ბოტს ("გამარჯობა"), შემდეგ চেک Flask server logs `/status` endpoint-ზე:
   ```bash
   curl http://localhost:5001/status
   # ძებნეთ "WhatsApp group X ID" სიღ, მაგ. "120363412345@g.us"
   ```

### ნაბიჯი 2: Google Drive ფოლდერების შექმნა

შექმენით მთავარი კურსის ფოლდერი და ცალკე analysis ფოლდერი.

1. [Google Drive-ზე](https://drive.google.com) გახსენით **AI კურსი** ფოლდერი (parent).
2. მან ახალი ფოლდერი: დააწკაპეთ **+ დამატება** → **ფოლდერი** და დააწერეთ ქართული სახელი (მაგ. "AI კურსი (მაისის ჯგუფი #3. 2026)").
3. გახსენით ფოლდერი და ნახეთ URL: `https://drive.google.com/drive/folders/1Abc2Def3Ghi4Jkl5Mno6Pqr7Stu8Vwx`
   - ფოლდერის ID = `1Abc2Def3Ghi4Jkl5Mno6Pqr7Stu8Vwx` (copy-paste და შენახეთ)
4. იმავე ფოლდერში შექმენით **ქვე-ფოლდერი** სახელით "ანალიზი": მან დაქვემდებარებული ფოლდერი (gap reports, deep analysis docs).
5. ნახეთ ქვე-ფოლდერის URL და extract მისი ID.

### ნაბიჯი 3: რედაქტირება `tools/core/config.py`-ში

გახსენით `tools/core/config.py` (Unix editor ან VS Code) და დაამატეთ ახალი ჯგუფის entry `GROUPS` dict-ში.

```python
GROUPS: dict[int, GroupConfig] = {
    1: { ... },  # Group 1 (existing)
    2: { ... },  # Group 2 (existing)
    3: {  # Group 3 (NEW)
        "name": "მაისის ჯგუფი #3",
        "folder_name": "AI კურსი (მაისის ჯგუფი #3. 2026)",
        "drive_folder_id": _env("DRIVE_GROUP3_FOLDER_ID"),
        "analysis_folder_id": _env("DRIVE_GROUP3_ANALYSIS_FOLDER_ID"),
        "zoom_meeting_id": _env("ZOOM_GROUP3_MEETING_ID"),
        "meeting_days": [0, 3],  # ორშაბათი=0, ხუთშაბათი=3 (Monday=0...Sunday=6)
        "start_date": date(2026, 5, 12),  # პირველი ლექციის თარიღი
        "attendee_emails": _ATTENDEES.get("3", []),
    },
    4: { ... },  # Group 4, if needed
}
```

**მნიშვნელოვანი**: weekday ნომერი არის Python `datetime.weekday()` სტანდარტი: ორშაბათი=0, ... კვირა=6.

შემდეგ ხედი initialize `LECTURE_FOLDER_IDS` dict (line 203):
```python
LECTURE_FOLDER_IDS: dict[int, dict[int, str]] = {1: {}, 2: {}, 3: {}, 4: {}}
```

### ნაბიჯი 4: attendees.json-ის განახლება (Local) ან env var (Railway)

**ლოკალურად:**

გახსენით ან შექმენით `attendees.json` პროექტის root-ში:

```json
{
  "1": ["student1@example.com", "student2@example.com", "teacher1@example.com"],
  "2": ["student3@example.com", "student4@example.com", "teacher1@example.com"],
  "3": ["student5@example.com", "student6@example.com", "student7@example.com", "teacher2@example.com"],
  "4": ["student8@example.com", "student9@example.com", "teacher2@example.com"]
}
```

დარწმუნდით რომ JSON უღიად არის ფორმატირებული (`python -m json.tool attendees.json` რო შემოწმებული).

**Railway-ზე:**

1. Railway dashboard-ზე ხელი აჭირეთ **Variables** tab.
2. დაამატეთ ან განაახლეთ `ATTENDEES_JSON_B64`:
   ```bash
   # ლოკალურად, terminal-ში
   python3 -c "import base64, json; print(base64.b64encode(json.dumps({'1': [...], '2': [...], '3': [...], '4': [...]}).encode()).decode())"
   ```
3. Copy entire base64 string და paste რა Railway variable value-სახელი.

### ნაბიჯი 5: Railway env var-ების დამატება

Railway dashboard-ზე **Variables** tab-ში დაამატეთ (ან განაახლეთ, თუ Group 3/4 უკვე ტემპორალურად):

```
DRIVE_GROUP3_FOLDER_ID=1Abc2Def3Ghi4Jkl5...  (folder ID from Step 2)
DRIVE_GROUP3_ANALYSIS_FOLDER_ID=2Xyz9...     (analysis subfolder ID)
WHATSAPP_GROUP3_ID=120363412345@g.us         (from Step 1)
ZOOM_GROUP3_MEETING_ID=12345678901           (if different; optional if shared)

DRIVE_GROUP4_FOLDER_ID=...
DRIVE_GROUP4_ANALYSIS_FOLDER_ID=...
WHATSAPP_GROUP4_ID=...
```

დარწმუნდით რომ `GOOGLE_CREDENTIALS_JSON_B64` და `ATTENDEES_JSON_B64` უკვე დეფინირებულია (უნდა იყოს ჩართული წინა setup-დან).

### ნაბიჯი 6: Railway redeploy

1. **Local terminal** (main branch-ზე):
   ```bash
   cd /path/to/training-agent
   git add tools/core/config.py
   git commit -m "feat: add Group 3 and 4 configuration"
   git push origin main
   ```
2. Railway GitHub hook automatically redeploy ან manually ბილიკელი Railway CLI:
   ```bash
   railway up --source .
   ```
3. დაელოდეთ ~2-3 წუთი deployment-ის დასრულებას. შემოწმეთ Railway dashboard **Deployments** tab შედეგი.

### ნაბიჯი 7: Verify cron jobs და smoke test

1. **Health check** — დაამტკიცეთ სერვერი ცოცხალია:
   ```bash
   curl https://<railway-public-url>/health
   # Expected: HTTP 200, {"status": "ok"}
   ```

2. **Full status inspection** — ნახეთ რომ ახალი Group-ის cron jobs დარეგისტრირებულია:
   ```bash
   curl https://<railway-public-url>/status
   # Look for lines like:
   # "pre_group3_monday: scheduled at 18:00 GMT+4"
   # "pre_group3_thursday: scheduled at 18:00 GMT+4"
   # "pre_group4_monday: scheduled at 18:00 GMT+4"
   # "pre_group4_thursday: scheduled at 18:00 GMT+4"
   ```

3. **Smoke test** — გაუგზავნეთ test message ახალი WhatsApp group-ში:
   ```
   მოგელით 30 წამი. ბოტმა უნდა უპასუხოს, მაგ.:
   "მოგესალმები 👋 მე ვარ მრჩეველი — AI კურსის ადვაიზერი. გთხოვთ დაწერეთ 'მრჩეველო' + თქვენი კითხვა."
   ```
   თუ პასუხ არ მიიღეთ:
   - შემოწმეთ `/logs` endpoint: `curl https://<railway-url>/logs` (ან Railway log stream)
   - მოიძებნეთ WebhookValidationError ან Green API connection errors
   - მოწმეთ რომ WHATSAPP_GROUP3_ID სწორი და bot არის group member

---

## შემოწმების ჩამონათვალი

ეს curl ბრძანებები და /status ფრაგმენტები უნდა გაუშვათ deployment-ის შემდეგ:

### curl ბრძანებები (Railway URL-ით ჩანაცვლებული)

```bash
# 1. Health endpoint — დაამტკიცეთ სერვერი სკალარული
curl -i https://<railway-public-url>/health
# Expected:
# HTTP/1.1 200 OK
# Content-Type: application/json
# {"status": "ok", "railway_environment": "production", "timestamp": "..."}

# 2. Status endpoint — ნახეთ ყველა cron job დეტალი
curl https://<railway-public-url>/status | python3 -m json.tool
# Look for "scheduler_jobs" array და verify Group 3/4 entries

# 3. Paperclip bridge health (თუ Paperclip integrate ა):
curl https://<railway-public-url>/paperclip/health
# Expected: HTTP 200, {"status": "healthy"}
```

### Status endpoint expected output (snippet)

სახელმძღვანელო `/status` JSON structure:

```json
{
  "status": "healthy",
  "scheduler_jobs": [
    {
      "id": "pre_group1_tuesday",
      "name": "Pre-meeting reminder — Group 1 (Tuesday 18:00)",
      "next_run": "2026-05-13T14:00:00+00:00"
    },
    {
      "id": "pre_group3_monday",
      "name": "Pre-meeting reminder — Group 3 (Monday 18:00)",
      "next_run": "2026-05-12T14:00:00+00:00"
    },
    ...
  ],
  "groups_configured": [1, 2, 3, 4],
  "green_api_state": "authorized"
}
```

დაამტკიცეთ რომ `"groups_configured"` მოიცავს `3` და `4` და უმცროსი Group-ის cron jobs ჩანს `"scheduler_jobs"` arrays-ში.

---

## კურსის დროებითი გათიშვა

სანამ კურსი დასრულდა (მაგ. 15 ლექციის შემდეგ), შეგიძლიათ pre-meeting reminders გათიშვა რაც მოსწავლეებმა ინსტიკტურად იმაზე ფიქრი თუ არაფერი უშუშ მოცემულ კვირაში.

1. გახსენით `tools/core/config.py` და იპოვეთ Group entry (მაგ. Group 1):
   ```python
   1: {
       "name": "მარტის ჯგუფი #1",
       ...
       "course_completed": False,  # Add this line if not present
   },
   ```

2. შეცვალეთ `"course_completed": True` (თუ field არ ყოს, დაამატეთ):
   ```python
   1: {
       "name": "მარტის ჯგუფი #1",
       ...
       "course_completed": True,
   },
   ```

3. Commit და push:
   ```bash
   git add tools/core/config.py
   git commit -m "feat: mark Group 1 course as completed"
   git push origin main
   ```

4. Railway redeploy ხდება automatically. სამი წუთის შემდეგ:
   - Pre-meeting cron jobs გათიშული იქნება Group 1-სთვის
   - მრჩეველი advisory assistant-ი რჩება აქტიური (student შეკითხვებს პასუხობს) — ეს სერვერი არ გამხმელი ხელმძღვანელი

---

## გავრცელებული პრობლემები და მოგვარება

### პრობლემა 1: ბოტი არ არის WhatsApp group-ში

**증상**: მოგიცემთ message group-ში და ბოტი უპასუხოდ დგას.

**მოგვარება**:
1. დაამტკიცეთ რომ bot (995579225809) არის group participant ბისკვის მიერ. თუ არა:
   - Group settings-ში **Group info** → **Participants** → დააწკაპეთ **+** → დაიწვიეთ `995579225809`
2. დაელოდეთ 30 წამი და აღდეს ისიც ხელახლა.
3. Green API `getStateInstance` endpoint-ი შემოწმებს auth state:
   ```bash
   curl "https://api.green-api.com/waInstance${INSTANCE_ID}/getStateInstance/${TOKEN}"
   # Expected: {"stateInstance": "authorized"}
   ```

### პრობლემა 2: Pre-meeting reminder არ გამოფხვეთ დაგეგმილ დროს

**증상**: ლექციის დღე გაემართა, მაგრამ 18:00-ზე (GMT+4) რიმენდერ SMS არ გაიწერა.

**მოგვარება**:
1. შემოწმეთ `/status` endpoint:
   ```bash
   curl https://<railway-url>/status | grep "pre_group3"
   # თუ cron job არ ჩანს → config.py-ში Group მინდობა გამოტოვებული
   ```
2. შემოწმეთ Railway env vars:
   ```bash
   railway variables
   # დაამტკიცეთ DRIVE_GROUP3_FOLDER_ID, WHATSAPP_GROUP3_ID დეფინირებული
   ```
3. Railway logs-ი:
   ```bash
   railway logs --follow
   # მოიძებნეთ ხაზები "scheduled job fired" ან error messages
   ```

### პრობლემა 3: Recording ჩამოტვირთვა ვერ ხერხდება

**증상**: Zoom recording completed event მივიდა, მაგრამ ჩამოტვირთვა timeout ან 403 Forbidden.

**მოგვარება**:
1. შემოწმეთ Zoom OAuth token:
   - Railway dashboard: Variables → `ZOOM_CLIENT_ID`, `ZOOM_CLIENT_SECRET` დეფინირებული?
   - Zoom account-ზე: [ეს OAuth აპ](https://marketplace.zoom.us) რეგისტრირებული და `recording:read` scope დაკომიტირებული?
2. Recording auto-save enabled:
   - Zoom account settings → **Recording** → **Auto-save to cloud** enabled (იხილეთ Zoom ადმინ ცენტრი)
3. Watchdog timing:
   - Green API rate limit: თუ 2+ recordings same day → stagger ~30 წამი შორსი (config.py-ში `RECORDING_PROCESSING_DELAY_SECONDS`)

### პრობლემა 4: მრჩეველი advisor არ პასუხობს WhatsApp-ზე

**증상**: Student "მრჩეველო, რა არის ხელოვნური ინტელექტი?" დაწერს და ბოტი მდუმარდება 30+ წამი.

**მოგვარება**:
1. დაამტკიცეთ Green API authorized:
   ```bash
   curl "https://api.green-api.com/waInstance${INSTANCE_ID}/getStateInstance/${TOKEN}"
   # Expected: {"stateInstance": "authorized"}
   # თუ "notAuthorized" → re-login Green API web interface (QR code scan)
   ```
2. რეკი Railway logs:
   ```bash
   railway logs --follow
   # მოიძებნეთ "Assistant processing" ან "Pinecone search error" ან "Claude API error"
   ```
3. Pinecone connectivity:
   ```bash
   curl https://<railway-url>/status | grep pinecone
   # არის "pinecone_status": "connected"? თუ "disconnected" → ელ.ფოსტა Tornike
   ```

### პრობლემა 5: Green API "3 message limit" error

**증状**: WhatsApp notifications დაწყებული გაიხმელი ძველი 3 messages per second limit მერე.

**მოგვარება**:
- **Green API plan upgrade** საჭირო. შეამოწმეთ [Green API pricing](https://green-api.com/en/pricing/):
  - **Starter**: 3 msg/s, unlimited contacts — რა Group 1-2-3 საკმარი
  - **Professional**: 10 msg/s — Group 4+ დამატებული load-სთვის
  - **Enterprise**: custom — მაღალი volume
- ამჟამად: **WHATSAPP_RATE_LIMIT_PER_SECOND=3** (config.py გამოწერილი), რო queue messages თუკი საჭირო.

---

## დამატებითი რეფერენსი

### Meeting days weekday ნომერი

Python `datetime.weekday()` სტანდარტი (ყველა system-ი ამას იყენებს):

| Weekday | Number |
|---------|--------|
| ორშაბათი (Monday) | 0 |
| სამშაბათი (Tuesday) | 1 |
| wednesday | 2 |
| ხუთშაბათი (Thursday) | 3 |
| პარასკევი (Friday) | 4 |
| Saturday | 5 |
| კვირა (Sunday) | 6 |

პროგრამმული მაგალითი: `"meeting_days": [0, 3]` = ორშაბათი + ხუთშაბათი.

### Config validation უნდა გაიშვა

სერვერი startup-ზე (`tools/core/config.py::validate_critical_config()`) შემოწმებს:

```
✗ Missing DRIVE_GROUP3_FOLDER_ID — Group 3 Drive uploads will fail
✗ Missing WHATSAPP_GROUP3_ID — Group 3 WhatsApp notifications will fail
```

თუ რომელიმე env var გამოტოვებული → სერვერი logs განიცხადებს, მაგრამ **არა crash**. კითხეთ `/logs` endpoint:

```bash
curl https://<railway-url>/logs?lines=50
```

### სწრაფი ხელმისაწვდომი

- **Railway Dashboard**: https://railway.app → Training Agent project
- **Green API Console**: https://app.green-api.com/waInstance/{INSTANCE_ID}
- **Google Drive**: https://drive.google.com
- **Zoom Account**: https://zoom.us/account
- **Observability**: Railway **Logs** tab (real-time streaming)
