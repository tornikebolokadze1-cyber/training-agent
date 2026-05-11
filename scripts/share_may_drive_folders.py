"""One-shot: share May 2026 lecture Drive folders with their students.

For each student in the May groups (Airtable export, 2026-05-11), add a
``reader`` permission on the appropriate lecture folder and send Google's
share-notification email so the student receives a link.

Idempotent at the permission level: Google Drive deduplicates by
(fileId, emailAddress, role), but the notification email will still send
on each re-grant. Run once at course start.
"""

from __future__ import annotations

import logging
import sys

from googleapiclient.errors import HttpError

from tools.integrations.gdrive_manager import get_drive_service

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("share_may_drive_folders")

GROUP_3_FOLDER_ID = "165JQVRq9ueas0wAJhFjHneEtBSvbt_bN"
GROUP_4_FOLDER_ID = "1K4XT7apK7ewI1_ihglb6ob8dWWKo9dOu"

GROUP_3_EMAILS = [
    "tornikesisauri03@gmail.com",
    "Lasha1@outlook.com",
    "ikabaiadze@gmail.com",
    "Mamaladze22@gmail.com",
    "Gio_bolota@yahoo.com",
    "a.urushadze9@gmail.com",
    "Nina.choniadze@gmail.com",
    "lanabliadzephotography@gmail.com",
    "gelashvili.ani@gmail.com",
    "chagunavanini@gmail.com",
    "Nino.kitiashvili@gmail.com",
    "tornikesissauri@gmail.com",
    "natiakav@gmail.com",
    "maisuradze.nino.nino@gmail.com",
    "a14menabde@gmail.com",
    "Treona221@gmail.com",
    "Vakhochitishvili@gmail.com",
    "natjibladze@cxhub.ge",
]

GROUP_4_EMAILS = [
    "guramtavkhelidze@gmail.com",
    "Qutatela25@gmail.com",
    "mariam.ratchvelishvili.1@btu.edu.ge",
    "achibolqvadze06@gmail.com",
    "tonatroshvili@gmail.com",
    "jincharadzeshako@gmail.com",
    "badu.jgenti@gmail.com",
    "Iremadzemail1@gmail.com",
    "Zhorzholianitornike@gmail.com",
    "gio.verdzadze@gmail.com",
    "Mariamikvaratskhelia55@gmail.com",
    "levansarishvili7@gmail.com",
    "teona.isashvili.geo@gmail.com",
    "Nikoloz.rekhviashvili13@gmail.com",
    "noetopadze26@gmail.com",
    "achiko.surmanidze@gmail.com",
    "nanikobera@gmail.com",
]

NOTIFICATION_MESSAGE_KA = (
    "გამარჯობა! ეს არის AI კურსის (მაისის ჯგუფი) ლექციების Drive საქაღალდე. "
    "აქ აიტვირთება ყველა ჩანაწერი და მათი მოკლე შინაარსი. წარმატებებს გისურვებთ!"
)


def share_with_email(service, folder_id: str, email: str) -> tuple[bool, str]:
    email = email.strip()
    body = {"type": "user", "role": "reader", "emailAddress": email}
    try:
        service.permissions().create(
            fileId=folder_id,
            body=body,
            sendNotificationEmail=True,
            emailMessage=NOTIFICATION_MESSAGE_KA,
            supportsAllDrives=True,
        ).execute()
        return True, "ok"
    except HttpError as e:
        return False, f"HttpError {e.resp.status}: {e._get_reason()}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def share_folder(group_num: int, folder_id: str, emails: list[str]) -> dict[str, list[str]]:
    print(f"\n--- Group {group_num} (folder {folder_id}) ---")
    print(f"  Sharing with {len(emails)} student(s)")
    service = get_drive_service()
    ok: list[str] = []
    failed: list[str] = []
    for email in emails:
        success, msg = share_with_email(service, folder_id, email)
        status = "OK" if success else f"FAIL ({msg})"
        print(f"  [{status}] {email.strip()}")
        (ok if success else failed).append(email.strip())
    return {"ok": ok, "failed": failed}


def main() -> int:
    print("=" * 70)
    print("Sharing May 2026 lecture Drive folders with students")
    print("=" * 70)

    summary = {}
    summary["group_3"] = share_folder(3, GROUP_3_FOLDER_ID, GROUP_3_EMAILS)
    summary["group_4"] = share_folder(4, GROUP_4_FOLDER_ID, GROUP_4_EMAILS)

    print("\n" + "=" * 70)
    print("Summary")
    print("=" * 70)
    for label, s in summary.items():
        print(f"  {label}: {len(s['ok'])} ok, {len(s['failed'])} failed")
        for f in s["failed"]:
            print(f"    FAILED: {f}")
    return 0 if not (summary["group_3"]["failed"] or summary["group_4"]["failed"]) else 2


if __name__ == "__main__":
    sys.exit(main())
