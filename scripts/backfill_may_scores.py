"""Backfill scores.db for May lectures whose deep_analysis lacks the
dimensional /10 score table.

The May-cohort deep_analysis prompt drifted away from the original
5-pillar dimensional format that ``tools.services.analytics.extract_scores``
expects. As a result, ``upsert_scores`` could not be called from the
normal pipeline for G3 L2, L3 and G4 L1, L2, L3 — only G3 L1 has a row.

This script does a one-shot derivation step using Claude Sonnet: it
reads the existing qualitative deep_analysis text from Drive and asks
Claude to produce the 5-pillar score table from the qualitative
content. The resulting table is then handed to the same
``save_scores_from_analysis`` function the live pipeline uses, so the
DB rows look identical to ones produced by the original pipeline.

Cost: ~5 lectures × $0.03 ≈ $0.15 total.

Usage::

    python -m scripts.backfill_may_scores --dry-run
    python -m scripts.backfill_may_scores            # do it for real
    python -m scripts.backfill_may_scores --group 4 --lecture 1
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("backfill_may_scores")
logging.getLogger("httpx").setLevel(logging.WARNING)

# Scoring prompt — instructs Claude to read the qualitative analysis
# and produce ONLY the 5-pillar table that extract_scores() can parse.
_PROMPT_TEMPLATE = """შენ ხარ AI კურსის ხარისხის შემფასებელი.

ქვემოთ მოცემულია მაისის კოჰორტის ერთი ლექციის გაფართოებული ანალიზი
ქართულად. შენი ამოცანაა ამ ხარისხობრივი ანალიზის საფუძველზე გამოიყვანო
ლექციის შეფასება 5 ცალკეულ ღერძზე და მისცე საერთო ქულა.

ღერძები:
1. შინაარსის სიღრმე — რამდენად დეტალურია ცნებების ახსნა
2. პრაქტიკული ღირებულება — რეალური ცხოვრებაში გამოყენების შესაძლებლობა
3. მონაწილეების ჩართულობა — ინტერაქცია, კითხვა-პასუხი, აქტიურობა
4. ტექნიკური სიზუსტე — ფაქტობრივი/ტექნიკური სისწორე
5. ბაზრის რელევანტურობა — როგორ შეესაბამება დღევანდელ AI ბაზრის რეალობას

დაბრუნე **მხოლოდ** ცხრილი ქვემოთ მოცემული ფორმატით — დამატებითი
ტექსტის გარეშე, კომენტარების გარეშე. თითოეული ქულა N/10 ფორმატით.

```
| **შინაარსის სიღრმე**     | X/10 | მოკლე დასაბუთება |
| **პრაქტიკული ღირებულება** | X/10 | მოკლე დასაბუთება |
| **მონაწილეების ჩართულობა** | X/10 | მოკლე დასაბუთება |
| **ტექნიკური სიზუსტე**     | X/10 | მოკლე დასაბუთება |
| **ბაზრის რელევანტურობა**  | X/10 | მოკლე დასაბუთება |
| **საერთო შეფასება**       | X.X/10 | სინთეზი |
```

ანალიზის ტექსტი:
---
{analysis}
---
"""


def _claude_score_table(analysis_text: str) -> str:
    """Send analysis_text to Claude Sonnet and return the score-table reply."""
    import anthropic

    client = anthropic.Anthropic()  # picks up ANTHROPIC_API_KEY from env

    # Truncate very long inputs — the prompt only needs the qualitative
    # signal, not the entire 25K-char doc. 15K chars is enough headroom.
    snippet = analysis_text[:15000] if len(analysis_text) > 15000 else analysis_text

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        messages=[
            {
                "role": "user",
                "content": _PROMPT_TEMPLATE.format(analysis=snippet),
            },
        ],
    )
    return response.content[0].text  # type: ignore[union-attr]


def _fetch_analysis_text(group: int, lecture: int) -> str:
    """Pull the deep_analysis doc for a single lecture from Drive."""
    from tools.core.config import GROUPS
    from tools.integrations.gdrive_manager import get_drive_service

    svc = get_drive_service()
    cfg = GROUPS[group]
    folder = cfg.get("analysis_folder_id")
    if not folder:
        return ""
    query = (
        f"'{folder}' in parents "
        f"and mimeType='application/vnd.google-apps.document' "
        f"and name contains 'ლექცია #{lecture}' "
        f"and trashed=false"
    )
    docs = (
        svc.files()
        .list(q=query, fields="files(id, name)", pageSize=10)
        .execute()
        .get("files", [])
    )
    if not docs:
        return ""
    body = svc.files().export(fileId=docs[0]["id"], mimeType="text/plain").execute()
    return body.decode("utf-8", errors="replace") if isinstance(body, bytes) else str(body)


def backfill_lecture(group: int, lecture: int, *, dry_run: bool = False) -> bool:
    """Derive 5-pillar scores for one lecture and persist to scores.db.

    Returns True on success, False on any failure (Drive miss, Claude error,
    extraction miss). Idempotent — re-running on a lecture with existing
    scores is harmless because ``upsert_scores`` upserts.
    """
    from tools.services.analytics import save_scores_from_analysis, init_db

    init_db()
    text = _fetch_analysis_text(group, lecture)
    if not text:
        logger.warning("g%d l%d: no Drive analysis doc", group, lecture)
        return False

    logger.info("g%d l%d: %d chars from Drive — asking Claude for score table", group, lecture, len(text))
    if dry_run:
        logger.info("g%d l%d: [dry-run] would call Claude + upsert", group, lecture)
        return True

    try:
        table = _claude_score_table(text)
    except Exception as exc:
        logger.error("g%d l%d: Claude failed: %s", group, lecture, exc)
        return False

    # Splice the Claude-derived table back into the original analysis text so
    # the existing analytics insight extractor still sees the qualitative
    # content. save_scores_from_analysis runs extract_scores AND
    # extract_and_save_insights — we want both to succeed.
    enhanced = f"{text}\n\n## დიმენსიური შეფასება\n\n{table}\n"

    ok = save_scores_from_analysis(group, lecture, enhanced)
    if ok:
        logger.info("g%d l%d: scores + insights saved", group, lecture)
    else:
        logger.error("g%d l%d: extraction still failed — Claude table:\n%s", group, lecture, table[:500])
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--group", type=int, default=None)
    parser.add_argument("--lecture", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    # Default targets: the 5 known May lectures with empty scores.db rows.
    default_targets = [(3, 2), (3, 3), (4, 1), (4, 2), (4, 3)]
    if args.group is not None and args.lecture is not None:
        targets = [(args.group, args.lecture)]
    elif args.group is not None:
        targets = [(g, l) for (g, l) in default_targets if g == args.group]
    else:
        targets = default_targets

    if not targets:
        logger.error("No targets — check --group / --lecture")
        return 1

    ok_count = 0
    for g, l in targets:
        if backfill_lecture(g, l, dry_run=args.dry_run):
            ok_count += 1

    logger.info(
        "backfill done: %d/%d succeeded%s",
        ok_count,
        len(targets),
        " [dry-run]" if args.dry_run else "",
    )
    return 0 if ok_count == len(targets) else 1


if __name__ == "__main__":
    sys.exit(main())
