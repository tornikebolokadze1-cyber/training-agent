"""One-shot: add the May 2026 students missing from their WhatsApp group chats.

Uses Green API (the operator's own WhatsApp instance — wid 995579225809@c.us)
to bulk-add 15 students into the two May group chats:
  - მაისის ჯგუფი #1 (Mon/Thu) — 120363409966993169@g.us
  - მაისის ჯგუფი #2 (Tue/Fri) — 120363426884083988@g.us

Each addition is preceded by a 2-second sleep to stay polite under
Green API's rate limits. Already-present participants are not re-added —
the script only targets the gap detected at 2026-05-11 15:50 Tbilisi.

Run once:

    python -m scripts.add_may_students_to_whatsapp
"""

from __future__ import annotations

import logging
import sys
import time

from tools.integrations.whatsapp_sender import _send_request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("add_may_students_to_whatsapp")

G3_CHAT = "120363409966993169@g.us"  # AI კურსი (მაისის ჯგუფი #1, 2026)
G4_CHAT = "120363426884083988@g.us"  # AI კურსი (მაისის ჯგუფი #2, 2026)

# (group_chat_id, label, [(student_name, participant_chat_id), ...])
TASKS: list[tuple[str, str, list[tuple[str, str]]]] = [
    (
        G3_CHAT,
        "მაისის ჯგუფი #1",
        [
            ("ლევან მამალაძე", "995598913234@c.us"),
            ("ალექსანდრე ურუშაძე", "995579444148@c.us"),
            ("ლანა ბლიაძე", "995568088822@c.us"),
            ("თორნიკე სისაური (2)", "995579000880@c.us"),
            ("ნათია ქავთარაძე", "995599140411@c.us"),
            ("აბო მენაბდე", "995599016114@c.us"),
            ("ვახო ჩიტოშვილი", "995557327219@c.us"),
            ("ნათია ჯიბლაძე", "995577307753@c.us"),
        ],
    ),
    (
        G4_CHAT,
        "მაისის ჯგუფი #2",
        [
            ("აჩი ბოლქვაძე", "995551499090@c.us"),
            ("შაკო ჯინჭარაძე", "995555145719@c.us"),
            ("მარიამ კვარაცხელია (US)", "17473585582@c.us"),
            ("ნიკოლოზ რეხვიაშვილი", "995591292111@c.us"),
            ("ნოე თოფაძე", "995555521377@c.us"),
            ("აჩი სურმანიძე", "995598260000@c.us"),
            ("ნანა ბერაძე", "995599763006@c.us"),
        ],
    ),
]


def main() -> int:
    print("=" * 70)
    print("Adding missing May 2026 students to WhatsApp group chats")
    print("=" * 70)

    overall: dict[str, dict[str, list]] = {}
    for group_id, label, students in TASKS:
        print(f"\n--- {label} ({group_id}) ---")
        print(f"  Target: {len(students)} student(s)")
        ok: list[str] = []
        fail: list[tuple[str, str]] = []
        for name, chat_id in students:
            try:
                resp = _send_request(
                    "addGroupParticipant",
                    {"groupId": group_id, "participantChatId": chat_id},
                    f"addGroupParticipant-{name}",
                )
                if resp.get("addParticipant"):
                    print(f"  [OK]   {name}  ({chat_id})")
                    ok.append(name)
                else:
                    print(f"  [FAIL] {name}  ({chat_id}) - {resp}")
                    fail.append((name, str(resp)))
            except Exception as e:
                print(f"  [ERR]  {name}  ({chat_id}) - {type(e).__name__}: {e}")
                fail.append((name, f"{type(e).__name__}: {e}"))
            time.sleep(2)  # polite gap
        overall[label] = {"ok": ok, "fail": fail}

    print("\n" + "=" * 70)
    print("Summary")
    print("=" * 70)
    total_ok, total_fail = 0, 0
    for label, r in overall.items():
        print(f"  {label}: {len(r['ok'])} ok, {len(r['fail'])} fail")
        total_ok += len(r["ok"])
        total_fail += len(r["fail"])
        for n, reason in r["fail"]:
            print(f"    FAILED: {n}  -  {reason}")
    print(f"  TOTAL: {total_ok} ok, {total_fail} fail")
    return 0 if total_fail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
