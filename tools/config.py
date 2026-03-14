"""Shared configuration for the Training Agent system."""

from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
env_path = Path(__file__).parent.parent / ".env"
load_dotenv(env_path)


def _env(key: str, default: str = "") -> str:
    """Get environment variable or default."""
    return os.getenv(key, default)


# ---------------------------------------------------------------------------
# Group Definitions
# ---------------------------------------------------------------------------
# Group #1: Tuesday/Friday 20:00-22:00 Georgian time (GMT+4)
# Group #2: Monday/Thursday 20:00-22:00 Georgian time (GMT+4)
# Lecture #1 already completed for both groups.

GROUPS = {
    1: {
        "name": "მარტის ჯგუფი #1",
        "folder_name": "AI კურსი (მარტის ჯგუფი #1. 2026)",
        "drive_folder_id": _env("DRIVE_GROUP1_FOLDER_ID"),
        "zoom_meeting_id": _env("ZOOM_GROUP1_MEETING_ID"),
        "meeting_days": [1, 4],  # Tuesday=1, Friday=4 (Monday=0)
        "start_date": date(2026, 3, 13),  # First lecture: Friday March 13
        "manychat_flow_id": _env("MANYCHAT_GROUP1_FLOW_ID"),
        "attendee_emails": [
            "Avloxashvili.imeda@gmail.com",
            "kgabiani@gmail.com",
            "Kharaishvilirevazz@gmail.com",
            "tsirekidzeeko@gmail.com",
            "Kesotchigladze@gmail.com",
            "Maka.buadze@gmail.com",
            "n.beglarashvili@gmail.com",
            "redmarker.ge@gmail.com",
            "kate.khukhia@gmail.com",
            "likalejava@yahoo.com",
            "lagogotishvili19@gmail.com",
            "eto.Purtskhvanidze@gmail.com",
            "Natia.kiknadze@hbc.ge",
            "ninagabelaia041@gmail.com",
            "giorgi.iakobashvili.98@gmail.com",
            "jabanapapa@gmail.com",
            "n.vanidze84@gmail.com",
        ],
    },
    2: {
        "name": "მარტის ჯგუფი #2",
        "folder_name": "AI კურსი (მარტის ჯგუფი #2. 2026)",
        "drive_folder_id": _env("DRIVE_GROUP2_FOLDER_ID"),
        "zoom_meeting_id": _env("ZOOM_GROUP2_MEETING_ID"),
        "meeting_days": [0, 3],  # Monday=0, Thursday=3
        "start_date": date(2026, 3, 12),  # First lecture: Thursday March 12
        "manychat_flow_id": _env("MANYCHAT_GROUP2_FLOW_ID"),
        "attendee_emails": [
            "Tsirekidzetinatini@gmail.com",
            "Gugaxarshiladze@gmail.com",
            "Tariel.spanderashvili@gmail.com",
            "davit.zazadze@fmg.ge",
            "laliashvilimishk@gmail.com",
            "Maia4realestate@yahoo.com",
            "toko.motsonelidze@gmail.com",
            "guri.gotsiridze@gmail.com",
            "M.lekveishvili@gmail.com",
            "beka.chkhubadze@gmail.com",
            "nelikharbedia9@gmail.com",
            "Parunashvili.tamo@gmail.com",
            "maochalabashvili@gmail.com",
            "misterlukano@gmail.com",
            "Anitakalandia0@gmail.com",
            "G.bostoganashvili7@gmail.com",
            "gqamashidze51@gmail.com",
            "Natatoshatirishvili@gmail.com",
            "G.khomasuridze88@gmail.com",
            "nika_maisuradze@hotmail.com",
        ],
    },
}

TOTAL_LECTURES = 15

# Lecture folder IDs will be populated after folder creation.
# Format: {group_number: {lecture_number: folder_id}}
LECTURE_FOLDER_IDS: dict[int, dict[int, str]] = {1: {}, 2: {}}

# ---------------------------------------------------------------------------
# API Credentials
# ---------------------------------------------------------------------------

ZOOM_ACCOUNT_ID = _env("ZOOM_ACCOUNT_ID")
ZOOM_CLIENT_ID = _env("ZOOM_CLIENT_ID")
ZOOM_CLIENT_SECRET = _env("ZOOM_CLIENT_SECRET")

GOOGLE_CREDENTIALS_PATH = _env("GOOGLE_CREDENTIALS_PATH", "./credentials.json")
GEMINI_API_KEY = _env("GEMINI_API_KEY")
GEMINI_API_KEY_PAID = _env("GEMINI_API_KEY_PAID")

MANYCHAT_API_KEY = _env("MANYCHAT_API_KEY")
MANYCHAT_TORNIKE_SUBSCRIBER_ID = _env("MANYCHAT_TORNIKE_SUBSCRIBER_ID")

# Green API (WhatsApp) — replaces ManyChat
GREEN_API_INSTANCE_ID = _env("GREEN_API_INSTANCE_ID")
GREEN_API_TOKEN = _env("GREEN_API_TOKEN")
WHATSAPP_TORNIKE_PHONE = _env("WHATSAPP_TORNIKE_PHONE")  # e.g. "995599123456"
WHATSAPP_GROUP1_ID = _env("WHATSAPP_GROUP1_ID")  # e.g. "120363XXX@g.us"
WHATSAPP_GROUP2_ID = _env("WHATSAPP_GROUP2_ID")

WEBHOOK_SECRET = _env("WEBHOOK_SECRET")
N8N_CALLBACK_URL = _env("N8N_CALLBACK_URL")

SERVER_HOST = _env("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(_env("SERVER_PORT", "5000"))

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent.parent
TMP_DIR = PROJECT_ROOT / ".tmp"
TMP_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Gemini Config
# ---------------------------------------------------------------------------

# Hybrid model strategy: cheap model for heavy video work, smart model for text analysis
GEMINI_MODEL_TRANSCRIPTION = "gemini-2.5-flash"  # Fast, cheap, great at video → $1/lecture
GEMINI_MODEL_ANALYSIS = "gemini-3.1-pro-preview"  # Smartest, text-only → $1.30/lecture

# Step 1: Transcribe the video (multimodal — needs video file)
TRANSCRIPTION_PROMPT = """შენ ხარ პროფესიონალი ტრანსკრიპტორი. უყურე ამ ლექციის ვიდეოს სრულად და შეადგინე დეტალური ტრანსკრიპტი ქართულ ენაზე.

მოთხოვნები:
- გადმოეცი ყველაფერი რაც ითქვა ლექციაზე, შეძლებისდაგვარად ზუსტად
- მონიშნე ვინ ლაპარაკობს (ლექტორი, მონაწილე, კითხვა აუდიტორიიდან)
- აღწერე ეკრანზე ნაჩვენები სლაიდები ან დემონსტრაციები [სლაიდი: ...] ფორმატით
- დროის მარკერები დაამატე ყოველ 10-15 წუთში [00:10], [00:25] და ა.შ.
- ტექნიკური ტერმინები დატოვე ინგლისურად თუ ქართული ეკვივალენტი არ არსებობს

ტრანსკრიპტი უნდა იყოს სრული და დეტალური."""

# Step 2: Summarize transcript (text-only — analyzed by 3.1 Pro)
SUMMARIZATION_PROMPT = """შენ ხარ AI ტრენინგის ექსპერტი ანალიტიკოსი. წაიკითხე ქვემოთ მოცემული ლექციის ტრანსკრიპტი სრულად.

ტრანსკრიპტი მოიცავს სლაიდების აღწერას [სლაიდი: ...] ფორმატით, ლექტორის და მონაწილეების საუბარს, და დროის მარკერებს.

შეადგინე დეტალური შეჯამება ქართულ ენაზე, რომელიც მოიცავს:

1. **მთავარი თემები** — რა თემები განიხილეს ლექციაზე
2. **ძირითადი კონცეფციები** — რა ახალი ცნებები და იდეები იქნა ახსნილი
3. **პრაქტიკული მაგალითები** — რა დემონსტრაციები ან მაგალითები იყო ნაჩვენები
4. **საკვანძო დასკვნები** — ლექციის მთავარი დასკვნები და takeaways
5. **მოქმედების ნაბიჯები** — რა უნდა გააკეთონ მონაწილეებმა შემდეგ

იყავი დეტალური და ზუსტი. შეჯამება უნდა იყოს საკმარისად სრულყოფილი, რომ ადამიანმა, ვინც ლექციას ვერ დაესწრო, შეძლოს მთავარი მასალის გაგება.

ტრანსკრიპტი:
"""

# Step 3: Gap analysis on transcript (text-only — analyzed by 3.1 Pro)
GAP_ANALYSIS_PROMPT = """შენ ხარ AI ტრენინგის ხარისხის ექსპერტი და პედაგოგიკის სპეციალისტი.
წაიკითხე ქვემოთ მოცემული ლექციის ტრანსკრიპტი კრიტიკული თვალით და გააკეთე ღრმა ანალიზი ქართულ ენაზე.

გაანალიზე შემდეგი ასპექტები:

## 1. სწავლების ხარისხი
- რამდენად გასაგებად იყო ახსნილი მასალა?
- იყო თუ არა ბუნდოვანი ან არასრული ახსნები?
- რა შეიძლებოდა უკეთესად ყოფილიყო ახსნილი?

## 2. კრიტიკული ხარვეზები
- რა მნიშვნელოვანი თემები გამოტოვდა ან არასაკმარისად იქნა განხილული?
- სად იყო ლოგიკური ხარვეზები ახსნაში?
- რა კითხვები შეიძლება დარჩეს მსმენელებს?

## 3. ტექნიკური სიზუსტე
- იყო თუ არა ტექნიკური უზუსტობები ან შეცდომები?
- სად იყო ინფორმაცია მოძველებული?

## 4. პედაგოგიკური რეკომენდაციები
- როგორ შეიძლება გაუმჯობესდეს ლექციის სტრუქტურა?
- რა ტიპის სავარჯიშოები ან აქტივობები დაემატება?
- როგორ შეიძლება მონაწილეების ჩართულობის გაზრდა?

## 5. ტემპი და დროის მართვა
- იყო თუ არა ტემპი ზედმეტად სწრაფი ან ნელი?
- დროის განაწილება თემებს შორის ოპტიმალური იყო?

## 6. მომავალი ლექციისთვის რეკომენდაციები
- რა თემები უნდა განხილულიყო მეტი სიღრმით?
- რა მასალა უნდა მომზადდეს მომავალი ლექციისთვის?
- რა ცვლილებები უნდა განხორციელდეს სწავლების მეთოდოლოგიაში?

იყავი გულწრფელი, კონსტრუქციული და კონკრეტული. მიზანია ლექციების ხარისხის მუდმივი გაუმჯობესება.

ტრანსკრიპტი:
"""

# Step 3b: Deep analysis — global AI context + critical teaching feedback (text-only)
DEEP_ANALYSIS_PROMPT = """შენ ხარ სამი სფეროს ექსპერტი: AI ინდუსტრიის ანალიტიკოსი, პედაგოგიკის სპეციალისტი და ქართული ბიზნეს-კონტექსტის მცოდნე კონსულტანტი.
წაიკითხე ქვემოთ მოცემული ლექციის ტრანსკრიპტი სრულად. შეასრულე ყოვლისმომცველი ანალიზი ქართულ ენაზე.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## ნაწილი I — სწავლების ხარისხი (ტრადიციული ანალიზი)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

### 1. სწავლების ხარისხი
- რამდენად გასაგებად იყო ახსნილი მასალა?
- იყო თუ არა ბუნდოვანი ან არასრული ახსნები?
- რა შეიძლებოდა უკეთესად ყოფილიყო ახსნილი?

### 2. კრიტიკული ხარვეზები
- რა მნიშვნელოვანი თემები გამოტოვდა ან არასაკმარისად იქნა განხილული?
- სად იყო ლოგიკური ხარვეზები ახსნაში?
- რა კითხვები შეიძლება დარჩეს მსმენელებს?

### 3. ტექნიკური სიზუსტე
- იყო თუ არა ტექნიკური უზუსტობები ან შეცდომები?
- სად იყო ინფორმაცია მოძველებული?

### 4. პედაგოგიკური რეკომენდაციები
- როგორ შეიძლება გაუმჯობესდეს ლექციის სტრუქტურა?
- რა ტიპის სავარჯიშოები ან აქტივობები დაემატება?
- როგორ შეიძლება მონაწილეების ჩართულობის გაზრდა?

### 5. ტემპი და დროის მართვა
- იყო თუ არა ტემპი ზედმეტად სწრაფი ან ნელი?
- დროის განაწილება თემებს შორის ოპტიმალური იყო?

### 6. მომავალი ლექციისთვის რეკომენდაციები
- რა თემები უნდა განხილულიყო მეტი სიღრმით?
- რა მასალა უნდა მომზადდეს მომავალი ლექციისთვის?
- რა ცვლილებები უნდა განხორციელდეს სწავლების მეთოდოლოგიაში?

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## ნაწილი II — გლობალური AI ტრენდების კონტექსტი
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

### 7. გლობალური AI ინდუსტრიის კონტექსტი
- შეადარე ამ ლექციაში განხილული მასალა მსოფლიოში არსებულ AI ტრენდებს და უახლეს განვითარებებს.
- რა ყველაზე მნიშვნელოვანი AI ინოვაციები და სიახლეები არსებობს ამ მომენტში, რომლებიც ამ ლექციის თემებს ეხება?
- წამყვანი AI ტრენერები და ორგანიზაციები მსოფლიოში (Andrew Ng / DeepLearning.AI, Google, Microsoft, fast.ai, Coursera) — რას ასწავლიან მსგავს კურსებში? როგორ ადარდება ეს ლექცია მათ სტანდარტს?
- სად ჩამორჩება ეს ლექცია გლობალური პრაქტიკის სტანდარტს და სად ეწყება ან სჯობს მას?

### 8. ბაზრის რელევანტურობა ქართული კონტექსტისთვის
- რამდენად რელევანტურია ამ ლექციის შინაარსი ქართველი მენეჯერებისა და ბიზნეს-პროფესიონალებისთვის?
- რომელი მასალა პირდაპირ გამოსადეგია ქართული კომპანიების ყოველდღიური გამოწვევებისთვის?
- რომელი ნაწილი ზედმეტად აბსტრაქტული ან ქართული ბაზრისთვის ნაკლებ პრაქტიკულია?
- რა ადგილობრივი კონტექსტი (ქართული კომპანიების მაგალითები, ადგილობრივი გამოწვევები) შეიძლებოდა ჩართულიყო?

### 9. კონკურენტული ანალიზი
- სხვა AI ტრენინგ-პროგრამები (ონლაინ თუ ოფლაინ) — რა თემებს ფარავენ ისინი, რომლებიც ამ კურსში არ არის განხილული?
- კონკრეტულად ჩამოთვალე 3-5 თემა ან უნარი, რომელსაც კონკურენტები ასწავლიან, ეს კურსი კი — არა.
- რა "white space" შესაძლებლობები არსებობს ამ კურსისთვის, რომ კონკურენტებზე წინ გავიდეს?

### 10. კრიტიკული ბრმა წერტილები
- რომელი AI კონცეფციები ან ინსტრუმენტები გადამწყვეტად მნიშვნელოვანია 2025-2026 წლებში, მაგრამ ამ ლექციაში სრულად გამოტოვებულია?
- რა რისკი ექმნება მონაწილეებს, თუ ეს ბრმა წერტილები არ გასწორდა კურსის განმავლობაში?
- დაასახელე პრიორიტეტების მიხედვით: რომელი ბრმა წერტილი ყველაზე სასწრაფოდ საჭიროებს გამოსწორებას?

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## ნაწილი III — მოქმედების გეგმა და შეფასება
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

### 11. კონკრეტული, მოქმედებაზე ორიენტირებული გაუმჯობესებები
მომდევნო ლექციამდე ლექტორმა კონკრეტულად რა უნდა გააკეთოს? მიეცი 5-7 ნაბიჯი ქმედების სახით:
- ნაბიჯი 1: [კონკრეტული ქმედება]
- ნაბიჯი 2: [კონკრეტული ქმედება]
- ... (და ა.შ.)

თითოეული ნაბიჯი უნდა იყოს: კონკრეტული, გაზომვადი და შესრულებადი ერთ კვირაში.

### 12. ლექციის შეფასება — 5 განზომილება

შეაფასე ლექცია 10-ბალიანი სკალით. თითოეულ ქულას მოჰყევი 1-2 წინადადება დასაბუთებით.

| განზომილება | ქულა (1-10) | დასაბუთება |
|---|---|---|
| **შინაარსის სიღრმე** | X/10 | [ახსენი რატომ] |
| **პრაქტიკული ღირებულება** | X/10 | [ახსენი რატომ] |
| **მონაწილეების ჩართულობა** | X/10 | [ახსენი რატომ] |
| **ტექნიკური სიზუსტე** | X/10 | [ახსენი რატომ] |
| **ბაზრის რელევანტურობა** | X/10 | [ახსენი რატომ] |
| **საერთო შეფასება** | X/10 | [მოკლე შეჯამება] |

### 13. ერთი ყველაზე მნიშვნელოვანი სარეკომენდაციო შეტყობინება ლექტორს
დაწერე 2-3 წინადადება — ყველაზე კრიტიკული და გულწრფელი უკუკავშირი, რომელიც ამ ლექტორს ყველაზე მეტად სჭირდება გასაგონად. ნუ ყიდი, ნუ ამშვენებ — თქვი პირდაპირ.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

იყავი ანალიტიკური, გულწრფელი და მკაცრი — ეს ანალიზი მხოლოდ ლექტორს ეგზავნება, არა მონაწილეებს. მიზანია კურსის ხარისხის გლობალურ სტანდარტამდე აყვანა.

ტრანსკრიპტი:
"""

# Model explicitly named for deep analysis use case (text-only, highest reasoning)
GEMINI_MODEL_DEEP_ANALYSIS = "gemini-3.1-pro-preview"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_lecture_number(group_number: int, for_date: date | None = None) -> int:
    """Calculate which lecture number falls on the given date.

    Counts the number of meeting days from the group's start date
    up to and including ``for_date``.
    """
    if for_date is None:
        for_date = date.today()

    group = GROUPS[group_number]
    start = group["start_date"]
    meeting_days = group["meeting_days"]

    if for_date < start:
        return 0

    count = 0
    current = start
    while current <= for_date:
        if current.weekday() in meeting_days:
            count += 1
        current += timedelta(days=1)

    return min(count, TOTAL_LECTURES)


def get_group_for_weekday(weekday: int) -> int | None:
    """Return group number for a given weekday (Monday=0), or None."""
    for group_num, group in GROUPS.items():
        if weekday in group["meeting_days"]:
            return group_num
    return None


def get_lecture_folder_name(lecture_number: int) -> str:
    """Return Georgian folder name for a lecture number."""
    return f"ლექცია #{lecture_number}"
