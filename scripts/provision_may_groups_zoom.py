"""One-shot script: provision recurring Zoom meetings for the two May 2026 groups.

Creates two recurring Zoom meetings (type=8) — one per May group — each covering
15 lectures starting on the configured weekday pattern at 20:00 Tbilisi time.
Zoom automatically emails calendar invitations to every attendee in the list.

Run once at course start:

    python -m scripts.provision_may_groups_zoom

Outputs the Zoom meeting ID and join URL for each group so they can be saved
as ZOOM_GROUP3_MEETING_ID / ZOOM_GROUP4_MEETING_ID on Railway.
"""

from __future__ import annotations

import logging
import sys
from datetime import date

from tools.integrations.zoom_manager import create_recurring_meeting

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# --- Group 3 ("AI კურსი #5" - 1, მაისის ჯგუფი #1) -------------------------
# Mon/Thu, first lecture 2026-05-11 20:00 Tbilisi, 15 occurrences
GROUP_3 = {
    "group_number": 3,
    "start_date": date(2026, 5, 11),
    "weekly_days": [0, 3],  # Mon=0, Thu=3
    "total_occurrences": 15,
    "attendee_emails": [
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
    ],
}

# --- Group 4 ("AI კურსი #5" - 2, მაისის ჯგუფი #2) -------------------------
# Tue/Fri, first lecture 2026-05-12 20:00 Tbilisi, 15 occurrences
GROUP_4 = {
    "group_number": 4,
    "start_date": date(2026, 5, 12),
    "weekly_days": [1, 4],  # Tue=1, Fri=4
    "total_occurrences": 15,
    "attendee_emails": [
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
    ],
}


def main() -> int:
    print("=" * 70)
    print("Provisioning May 2026 group recurring Zoom meetings")
    print("=" * 70)

    for cfg in (GROUP_3, GROUP_4):
        n = cfg["group_number"]
        print(f"\n--- Group {n} ---")
        print(f"  Start:       {cfg['start_date']} 20:00 Tbilisi")
        print(f"  Weekly days: {cfg['weekly_days']} (Python Mon=0..Sun=6)")
        print(f"  Lectures:    {cfg['total_occurrences']}")
        print(f"  Attendees:   {len(cfg['attendee_emails'])}")

        try:
            result = create_recurring_meeting(**cfg)
        except Exception as exc:
            print(f"  ERROR creating meeting for group {n}: {exc}")
            return 1

        print(f"  Meeting ID:  {result['id']}")
        print(f"  UUID:        {result['uuid']}")
        print(f"  Join URL:    {result['join_url']}")
        print(f"  Topic:       {result['topic']}")

    print("\n" + "=" * 70)
    print("Done. Save the Meeting IDs to Railway as:")
    print("  ZOOM_GROUP3_MEETING_ID  (May group #1)")
    print("  ZOOM_GROUP4_MEETING_ID  (May group #2)")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
