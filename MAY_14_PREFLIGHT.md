# 14 მაისის ლექციამდე — სავალდებულო შემოწმებები

ეს არის წინასწარი შემოწმების სია, რომელიც უნდა გავაკეთო ხუთშაბათამდე
(2026-05-14, ხუთშაბათი) 18:00 თბილისის დროით. იქამდე Group #4
(მაისის ჯგუფი #2) ლექცია #2 დაიწყება 20:00 საათზე.
პარასკევს (2026-05-15) იწყება Group #3 (მაისის ჯგუფი #1) ლექცია #2.

დღევანდელი სესიის განმავლობაში 15 commit დაემატა ბრანჩზე
`fix/multi-cohort-cleanup-pr39`. ეს commit-ები ჯერ მერჯ-არ-არის main-ში.
გადაწყდი: მერჯავ თუ არა ლექციამდე.

---

## 1. Railway-ის environment ცვლადები

მაისის ჯგუფი #1 და #2-ის (env-ები `GROUP3_*` / `GROUP4_*`) Railway-ზე უნდა იყოს დაყენებული.
თუ რომელიმე ცვლადი არ არის — startup-ის დროს ახალი probe ხედავს ამას
და WhatsApp-ში გამცნობს (commit `075a49b`).

### როგორ შევამოწმო Railway-ის dashboard-ზე:

1. გადადი https://railway.app/dashboard
2. აირჩიე პროექტი `training-agent`
3. დააწექი deployed service-ს
4. გადადი ჩანართზე `Variables`
5. ქვემოთ ნაჩვენები ცვლადები უნდა იყოს დაყენებული

### სავალდებულო ცვლადები მაისის ჯგუფი #1-ისთვის (env-ები: `GROUP3_*`):

```
DRIVE_GROUP3_FOLDER_ID=<Drive საქაღალდის ID>
DRIVE_GROUP3_ANALYSIS_FOLDER_ID=<პრივატული ანალიზის Drive ID>
WHATSAPP_GROUP3_ID=<120363XXX@g.us ფორმატით>
ZOOM_GROUP3_MEETING_ID=<არასავალდებულო: საერთო Zoom ოთახიც კარგია>
GROUP3_NAME=მაისის ჯგუფი #1
GROUP3_FOLDER_NAME=AI კურსი (მაისის ჯგუფი #1. 2026)
GROUP3_MEETING_DAYS=1,4
GROUP3_START_DATE=<პირველი ლექციის ISO თარიღი>
GROUP3_COURSE_COMPLETED=0
```

შენიშვნა: `GROUP3_MEETING_DAYS=1,4` ნიშნავს სამშაბათი + პარასკევი.
(0=ორშაბათი, 1=სამშაბათი, 4=პარასკევი, 6=კვირა)

### სავალდებულო ცვლადები მაისის ჯგუფი #2-ისთვის (env-ები: `GROUP4_*`):

```
DRIVE_GROUP4_FOLDER_ID=<Drive საქაღალდის ID>
DRIVE_GROUP4_ANALYSIS_FOLDER_ID=<პრივატული ანალიზის Drive ID>
WHATSAPP_GROUP4_ID=<120363XXX@g.us ფორმატით>
ZOOM_GROUP4_MEETING_ID=<არასავალდებულო>
GROUP4_NAME=მაისის ჯგუფი #2
GROUP4_FOLDER_NAME=AI კურსი (მაისის ჯგუფი #2. 2026)
GROUP4_MEETING_DAYS=0,3
GROUP4_START_DATE=<პირველი ლექციის ISO თარიღი>
GROUP4_COURSE_COMPLETED=0
```

შენიშვნა: `GROUP4_MEETING_DAYS=0,3` ნიშნავს ორშაბათი + ხუთშაბათი.

---

## 2. Drive საქაღალდეების ხელით შემოწმება

რამდენიმე კვირის წინ აღმოვაჩინეთ, რომ მაისის ჯგუფი #1-ის Drive folder ID
არასწორი იყო. ახლა startup-ის დროს ავტომატური `Drive folder probe`
ხდება (commit `075a49b`), მაგრამ მაინც ერთხელ ხელით შემოწმე:

1. გახსენი https://drive.google.com
2. შემოწმე რომ ხედავ:
   - `AI კურსი (მაისის ჯგუფი #1. 2026)` — internal ID 3
   - `AI კურსი (მაისის ჯგუფი #2. 2026)` — internal ID 4
3. შემოწმე ანალიზის (პრივატული) საქაღალდეები:
   - `კურსი #3 ანალიზი`
   - `კურსი #4 ანალიზი`
4. გადადი Railway-ის Variables-ში და შეადარე folder ID-ები.
   Drive URL-ში folder ID ერთვის ბოლოს: `drive.google.com/.../folders/<ID>`

---

## 3. Google authentication (refresh token)

Codex-მა დიაგნოსტიკის დროს იპოვა, რომ ლოკალური Google token
ვადაგასულია. Railway-ის refresh token ცალკეა და სავარაუდოდ მუშაობს,
მაგრამ უნდა შევამოწმოთ:

1. Railway-ის logs-ში მოძებნე `Google token probe` (startup-ის დროს
   იწერება)
2. თუ წერია `Google token probe: OK` — ყველაფერი კარგად
3. თუ წერია `invalid_grant` ან `token expired` — დაუყოვნებლივ:
   - ლოკალურად გაუშვი `python scripts/oauth_setup.py`
   - browser-ში გაიხსნება Google login
   - დაასრულე authentication
   - მიღებული `token.json` base64-ით ჩასვი Railway-ის `GOOGLE_TOKEN_JSON_B64` ცვლადში
   - Railway service გადატვირთე

---

## 4. /healthz endpoint

ახალი public endpoint დაემატა Railway-ის deploy workflow-ისთვის
(commit `075a49b`):

```
https://<your-railway-url>/healthz
```

- პასუხი უნდა იყოს `{"ok": true, ...}`
- ეს endpoint authentication-ს არ ითხოვს — GitHub Actions deploy
  ჩექი ვერ აშავებდა ფიქს ვერსიამდე
- შემოწმე ბრაუზერში 18:00-ის წინ

---

## 5. ფინანსური ლიმიტი

დღევანდელი ხარჯი $85.40 ავიდა / $50.00-ის ლიმიტისგან. ახალი code-მა
ახლა WhatsApp-ში ცნობის გაგზავნა იცის 80% და 100%-ზე (commit `df8050e`).

შემოწმე Railway env vars:

```
LECTURE_COST_LIMIT_USD=5.0
```

თუ `DAILY_COST_LIMIT_USD` ცვლადი დაყენებულია — დარწმუნდი რომ
საკმარისად მაღალია ლექციისთვის. რეკომენდაცია: ერთი დღისთვის $100.

თუ `OVERRIDE_COST_CAP=1` — hard-stop ჩართულია და ლიმიტი დაბლოკავს
ახალ API call-ებს cap-ის მიღწევისას. ლექციის დღეს ეს ცვლადი არ უნდა
იყოს ჩართული, თუ არ ხარ დარწმუნებული.

---

## 6. რა შეიცვალა დღეს (15 commit)

ბრანჩი: `fix/multi-cohort-cleanup-pr39` (jet not merged into main)

ძირითადი ცვლილებები:
- Zoom OAuth token ახლა ხელახლა იღება ხანგრძლივი ჩამოტვირთვის დროს
  (აღარ წყდება საათიანი recording-ის შუაში)
- `analytics.py` სრულად დინამიკურია — მაისის ჯგუფი #1 + #2 ხილვადია
  dashboard-ში
- `obsidian-sync` skip-if-exists guard-ით (აღარ წაშლის უსისტემოდ)
- admin endpoints-ზე rate limit decorators (9 endpoint)
- `/admin/backfill-deep-analysis` size cap
- WhatsApp `alert_operator` dedup 5-წუთიან ფანჯარაში
- ცხელი ლექციის dashboard label-ები ცოცხალია მაისის ჯგუფი #1/#2-ისთვის
- HSTS + Permissions-Policy security headers
- X-Forwarded-For rate limit (Railway proxy-ის უკან IP-ის სწორი წაკითხვა)
- daily cost threshold alerts 80% / 100%
- `_processing_tasks` lock consistency
- `_remove_pending_job` atomic + locked
- `drive_video_id` დაემატა completion invariants-ში
- DLQ audit + cleanup tool (`scripts/audit_dlq.py`)
- langsmith CVE upgrade

დეტალები: `git log --oneline fix/multi-cohort-cleanup-pr39 -20`

---

## 7. შესასრულებელი დავალებები

ხუთშაბათ 18:00-მდე:

- [ ] Railway-ში ყველა `GROUP3_*` ცვლადი დადებულია
- [ ] Railway-ში ყველა `GROUP4_*` ცვლადი დადებულია
- [ ] Drive საქაღალდის ID-ები სწორია (ხელით შემოწმდა Drive-ში)
- [ ] Drive ანალიზის (პრივატული) საქაღალდე #3-ისთვის და #4-ისთვის
- [ ] Google refresh token Railway-ზე ცოცხალია (logs-ში `probe: OK`)
- [ ] `/healthz` endpoint Railway-ზე პასუხობს `{"ok": true}`-ით
- [ ] `LECTURE_COST_LIMIT_USD` და `DAILY_COST_LIMIT_USD` სწორი
      მნიშვნელობებია
- [ ] `OVERRIDE_COST_CAP` ჩართული არ არის (ან თუ ჩართულია — შენ იცი
      რატომ)
- [ ] გადაწყვიტე: `fix/multi-cohort-cleanup-pr39` მერჯავ თუ არა main-ში
      ლექციამდე (15 commit)
- [ ] WhatsApp-ის Green API ცოცხალია (გააგზავნე ტესტ-შეტყობინება)
- [ ] Zoom recording webhook URL Zoom-ის dashboard-ში სწორად
      რეგისტრირებულია (`/zoom-webhook` endpoint)

ლექციის დღეს:

- [ ] 18:00-ზე მაისის ჯგუფი #2-ის pre-meeting reminder ავტომატურად გავა
      (n8n workflow-ი)
- [ ] 20:00-ზე ლექცია იწყება
- [ ] 22:00-ის შემდეგ recording webhook ფაირდება, pipeline იწყება
- [ ] შემოწმე Railway logs რომ pipeline-ის ეტაპები გადის შეცდომის
      გარეშე
- [ ] 23:00-მდე WhatsApp-ში მიიღო notification რომ ლექცია დამუშავდა

---

## თუ რამე ვერ მუშაობს

Claude Code-ში დაბრუნდი და თქვი:

- `"გააუქმე ბოლო ცვლილება"` — Claude წინა შენახვის წერტილს დააბრუნებს
- `"რაღაც გაფუჭდა, გაასწორე"` — Claude-ის ავტო-დიაგნოსტიკა იწყება
- `"მაჩვენე რა შეცვალე დღეს"` — დღევანდელი 15 commit მარტივი ენით
- `"Railway-ის logs-ში რა შეცდომაა?"` — Claude შემოწმდება

თუ kritikuli შეცდომა — WhatsApp-ში გამოგიგზავნის `alert_operator()`
ფუნქცია ავტომატურად. 5-წუთიან ფანჯარაში მეორედ აღარ გაიგზავნება
(dedup).

---

## საკონტაქტო

თუ ლექციის დროს რამე ვერ მუშაობს და ვერ ხვდები რას აკეთო:
- გახსენი Claude Code
- მიეცი ეს ფაილი context-ად
- აღწერე რა ხდება (ერთი წინადადებით)
- Claude განახორციელებს დიაგნოსტიკას
