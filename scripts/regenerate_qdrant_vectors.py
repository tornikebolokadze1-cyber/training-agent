"""Regenerate Qdrant vectors for every lecture by reading Drive docs.

After the 2026-05-20 Pinecone → Qdrant migration, the existing Pinecone
vectors cannot be exported (the 1M monthly read limit is what triggered
the migration in the first place). Instead, the source-of-truth lecture
artifacts in Google Drive are re-chunked, re-embedded, and upserted into
Qdrant. Because vector IDs are deterministic UUIDv5s derived from the
legacy ``g{N}_l{N}_{type}_{chunk}`` string, re-running the script is
idempotent — it never creates duplicate points.

Usage::

    # Full regen, all groups, all lectures
    python -m scripts.regenerate_qdrant_vectors

    # Preview what would happen without contacting Gemini or Qdrant
    python -m scripts.regenerate_qdrant_vectors --dry-run

    # One specific lecture
    python -m scripts.regenerate_qdrant_vectors --group 3 --lecture 5

    # Restrict to a single content type
    python -m scripts.regenerate_qdrant_vectors --content-type transcript

Output format (one line per content type)::

    g3 l5 transcript: 87 vectors uploaded ($0.018)

Final summary::

    Migrated 28 lectures, 4,123 vectors. Estimated cost: $0.92

The cost estimate is derived from the Gemini embedding price per character
(see ``GEMINI_EMBEDDING_PRICE_PER_M_CHARS`` below) so it tracks the public
Gemini Cloud pricing page. Treat it as an upper bound.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path

# Ensure project root is on sys.path so ``tools.*`` imports resolve.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:  # dotenv is optional in the regen-script context
    pass

# Configure logging early so the rest of the script can use logger calls.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("regen_qdrant")

# Mute the verbose Gemini-client debug stream while keeping our own logs visible.
logging.getLogger("google_genai").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

#: Gemini text embedding price (US$ per million input characters).
#: Source: https://cloud.google.com/vertex-ai/generative-ai/pricing — keep in
#: sync with the public price page. Used only for cost estimation in logs;
#: actual billing is whatever Google charges.
GEMINI_EMBEDDING_PRICE_PER_M_CHARS = 0.025


# ---------------------------------------------------------------------------
# Content type mapping
# ---------------------------------------------------------------------------

#: Maps Qdrant content type names to the Drive doc-name discriminator used
#: in lecture folder listings. ``deep_analysis`` and ``gap_analysis`` live in
#: the *private* analysis folder (per group), not in the shared lecture
#: folder. ``transcript`` is stored locally under ``.tmp/`` after recording
#: and may not exist in Drive at all for older lectures.
_CONTENT_TYPES = ("transcript", "summary", "gap_analysis", "deep_analysis")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class LectureRunStats:
    """Per-lecture rollup so the final summary can be a single line."""

    group: int
    lecture: int
    vectors_uploaded: int = 0
    chars_embedded: int = 0
    content_types_processed: int = 0
    skipped: bool = False
    error: str | None = None


# ---------------------------------------------------------------------------
# Drive content extraction
# ---------------------------------------------------------------------------


def _export_doc_text(svc: object, file_id: str) -> str:
    """Download a Google Doc as plain text. Returns "" on error."""
    try:
        content = (
            svc.files()  # type: ignore[attr-defined]
            .export(fileId=file_id, mimeType="text/plain")
            .execute()
        )
        if isinstance(content, bytes):
            return content.decode("utf-8", errors="replace")
        return str(content)
    except Exception as exc:
        logger.warning("Failed to export doc %s: %s", file_id, exc)
        return ""


def _find_lecture_summary_doc(svc: object, lecture_folder_id: str, lecture: int) -> str:
    """Return the plain-text body of the summary doc for one lecture folder.

    Returns "" if no summary doc is found.
    """
    query = (
        f"'{lecture_folder_id}' in parents "
        f"and mimeType='application/vnd.google-apps.document' "
        f"and trashed=false"
    )
    try:
        docs = (
            svc.files()  # type: ignore[attr-defined]
            .list(q=query, fields="files(id, name)", pageSize=50)
            .execute()
            .get("files", [])
        )
    except Exception as exc:
        logger.warning("Drive list failed for folder %s: %s", lecture_folder_id, exc)
        return ""

    for doc in docs:
        name_lower = doc["name"].lower()
        if "შეჯამება" in doc["name"] or "summary" in name_lower:
            return _export_doc_text(svc, doc["id"])
    return ""


def _find_analysis_docs(
    svc: object,
    private_folder_id: str,
    lecture: int,
) -> tuple[str, str]:
    """Return (gap_analysis_text, deep_analysis_text) for one lecture.

    Both live in the same private analysis folder, distinguished by a
    keyword in the doc name. Older lectures may have only one or neither.
    """
    if not private_folder_id:
        return "", ""

    query = (
        f"'{private_folder_id}' in parents "
        f"and mimeType='application/vnd.google-apps.document' "
        f"and name contains 'ლექცია #{lecture}' "
        f"and trashed=false"
    )
    try:
        docs = (
            svc.files()  # type: ignore[attr-defined]
            .list(q=query, fields="files(id, name)", pageSize=50)
            .execute()
            .get("files", [])
        )
    except Exception as exc:
        logger.warning(
            "Drive list failed for analysis folder %s: %s", private_folder_id, exc
        )
        return "", ""

    gap_text = ""
    deep_text = ""
    for doc in docs:
        name = doc["name"]
        text = _export_doc_text(svc, doc["id"])
        if not text:
            continue
        # The pipeline writes "Gap Analysis" / "ნაკლოვანებათა ანალიზი" and
        # "Deep Analysis" / "ღრმა ანალიზი" — accept either spelling.
        lowered = name.lower()
        if "deep" in lowered or "ღრმა" in name:
            deep_text = text
        elif "gap" in lowered or "ნაკლოვან" in name:
            gap_text = text
    return gap_text, deep_text


def _find_lecture_folder(
    svc: object,
    group_root_folder_id: str,
    lecture: int,
) -> str | None:
    """Find the Drive folder ID for one (group, lecture) pair.

    Folder names look like ``ლექცია #5`` or ``ლექცია 5`` — accept both.
    Returns None if not found.
    """
    query = (
        f"'{group_root_folder_id}' in parents "
        f"and mimeType='application/vnd.google-apps.folder' "
        f"and trashed=false "
        f"and name contains 'ლექცია'"
    )
    try:
        folders = (
            svc.files()  # type: ignore[attr-defined]
            .list(q=query, fields="files(id, name)", pageSize=50)
            .execute()
            .get("files", [])
        )
    except Exception as exc:
        logger.warning(
            "Drive list failed for group root %s: %s", group_root_folder_id, exc
        )
        return None

    target_patterns = (f"#{lecture}", f" {lecture}", f"  {lecture}")
    for folder in folders:
        name = folder["name"]
        # Strict match — avoid "ლექცია #15" matching when we wanted #1.
        for pat in target_patterns:
            if name.endswith(pat) or pat + " " in name:
                return folder["id"]
    return None


def _find_local_transcript(group: int, lecture: int) -> str:
    """Return the transcript text from .tmp/ if it was kept locally.

    The pipeline saves the full transcript under
    ``.tmp/g{N}_l{N}_transcript.txt`` after analysis completes.
    Older runs may not have left a file behind.
    """
    from tools.core.config import TMP_DIR

    candidates = [
        TMP_DIR / f"g{group}_l{lecture}_transcript.txt",
        TMP_DIR / "merged_data" / f"g{group}_l{lecture}_transcript.txt",
    ]
    for path in candidates:
        if path.exists():
            try:
                text = path.read_text(encoding="utf-8")
                if text.strip():
                    return text
            except OSError as exc:
                logger.warning("Could not read %s: %s", path, exc)
    return ""


def collect_lecture_content(group: int, lecture: int) -> dict[str, str]:
    """Gather every available content type for one lecture into a dict.

    Returns:
        Dict mapping content_type -> raw_text. Missing content types are
        omitted (not present in the returned dict).
    """
    from tools.core.config import GROUPS
    from tools.integrations.gdrive_manager import get_drive_service

    group_cfg = GROUPS.get(group)
    if not group_cfg:
        logger.warning("Group %d not configured — skipping", group)
        return {}

    svc = get_drive_service()
    out: dict[str, str] = {}

    # Step 1 — lecture folder (videos + summary doc)
    group_root = group_cfg.get("drive_folder_id")
    if not group_root:
        logger.warning("Group %d has no drive_folder_id — skipping summary", group)
    else:
        lecture_folder_id = _find_lecture_folder(svc, group_root, lecture)
        if lecture_folder_id:
            summary = _find_lecture_summary_doc(svc, lecture_folder_id, lecture)
            if summary.strip():
                out["summary"] = summary
        else:
            logger.info("No Drive folder found for g%d l%d", group, lecture)

    # Step 2 — private analysis folder (gap + deep)
    private_folder = group_cfg.get("analysis_folder_id")
    gap, deep = _find_analysis_docs(svc, private_folder, lecture)
    if gap.strip():
        out["gap_analysis"] = gap
    if deep.strip():
        out["deep_analysis"] = deep

    # Step 3 — transcript (local file, not Drive)
    transcript = _find_local_transcript(group, lecture)
    if transcript.strip():
        out["transcript"] = transcript

    return out


# ---------------------------------------------------------------------------
# Embedding + upsert (delegates to the migrated knowledge_indexer)
# ---------------------------------------------------------------------------


def _estimate_cost(chars: int) -> float:
    """Return the estimated US$ cost of embedding ``chars`` characters."""
    return (chars / 1_000_000) * GEMINI_EMBEDDING_PRICE_PER_M_CHARS


def regenerate_lecture(
    group: int,
    lecture: int,
    *,
    content_types: tuple[str, ...] = _CONTENT_TYPES,
    dry_run: bool = False,
) -> LectureRunStats:
    """Re-embed and upsert every available content type for one lecture.

    Args:
        group: Group number (1, 2, 3, ...).
        lecture: Lecture sequence number (1-15).
        content_types: Restrict to this subset of content types.
        dry_run: When True, count and report without touching Gemini or Qdrant.

    Returns:
        A ``LectureRunStats`` summarising what was uploaded.
    """
    stats = LectureRunStats(group=group, lecture=lecture)

    content = collect_lecture_content(group, lecture)
    if not content:
        stats.skipped = True
        stats.error = "no content found in Drive or .tmp"
        logger.info("g%d l%d: skipped — %s", group, lecture, stats.error)
        return stats

    # Importing here keeps the script usable in --dry-run mode without
    # any vector-DB env vars being set.
    from tools.integrations.knowledge_indexer import (
        chunk_text,
        index_lecture_content,
    )

    for ctype in content_types:
        text = content.get(ctype)
        if not text:
            continue

        chunks = chunk_text(text)
        if not chunks:
            continue

        stats.content_types_processed += 1
        stats.chars_embedded += len(text)
        cost = _estimate_cost(len(text))

        if dry_run:
            logger.info(
                "g%d l%d %s: would embed %d chunks (~%d chars, ~$%.4f) [dry-run]",
                group, lecture, ctype, len(chunks), len(text), cost,
            )
            stats.vectors_uploaded += len(chunks)
            continue

        # ``index_lecture_content`` is idempotent — it already checks
        # whether a lecture is fully indexed and skips when so. Passing
        # ``force=True`` would cause unnecessary re-uploads; default
        # behaviour is what we want here.
        try:
            uploaded = index_lecture_content(
                group_number=group,
                lecture_number=lecture,
                content=text,
                content_type=ctype,
            )
        except Exception as exc:
            logger.error(
                "g%d l%d %s: indexing failed: %s", group, lecture, ctype, exc,
            )
            stats.error = f"{ctype}: {exc}"
            continue

        stats.vectors_uploaded += uploaded
        logger.info(
            "g%d l%d %s: %d vectors uploaded ($%.4f)",
            group, lecture, ctype, uploaded, cost,
        )

    return stats


# ---------------------------------------------------------------------------
# CLI orchestration
# ---------------------------------------------------------------------------


def _iter_target_lectures(
    only_group: int | None,
    only_lecture: int | None,
) -> list[tuple[int, int]]:
    """Yield (group, lecture) pairs to process, honouring CLI filters."""
    from tools.core.config import GROUPS, TOTAL_LECTURES

    groups = sorted(GROUPS.keys())
    if only_group is not None:
        if only_group not in GROUPS:
            logger.error("--group %d not in GROUPS config", only_group)
            return []
        groups = [only_group]

    out: list[tuple[int, int]] = []
    for g in groups:
        if only_lecture is not None:
            if 1 <= only_lecture <= TOTAL_LECTURES:
                out.append((g, only_lecture))
        else:
            for lec in range(1, TOTAL_LECTURES + 1):
                out.append((g, lec))
    return out


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Regenerate Qdrant vectors for the Training Agent by re-reading "
            "lecture docs from Google Drive. Idempotent — uses deterministic "
            "UUIDs so re-running never duplicates points."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--group",
        type=int,
        default=None,
        help="Restrict to one group (e.g. 3). If omitted, iterates every group in GROUPS.",
    )
    parser.add_argument(
        "--lecture",
        type=int,
        default=None,
        help="Restrict to one lecture number (1-15). If omitted, iterates every lecture.",
    )
    parser.add_argument(
        "--content-type",
        choices=_CONTENT_TYPES,
        default=None,
        help=(
            "Restrict to a single content type. If omitted, processes "
            f"all: {', '.join(_CONTENT_TYPES)}."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without contacting Gemini or Qdrant.",
    )
    return parser


def main() -> int:
    args = _build_arg_parser().parse_args()

    content_types: tuple[str, ...] = _CONTENT_TYPES
    if args.content_type:
        content_types = (args.content_type,)

    targets = _iter_target_lectures(args.group, args.lecture)
    if not targets:
        logger.error("No lectures to process — check --group / --lecture flags.")
        return 1

    logger.info(
        "Starting regen for %d lecture(s)%s%s",
        len(targets),
        " [dry-run]" if args.dry_run else "",
        f" content_type={args.content_type}" if args.content_type else "",
    )

    total_vectors = 0
    total_chars = 0
    total_lectures = 0
    total_errors = 0

    for group, lecture in targets:
        stats = regenerate_lecture(
            group,
            lecture,
            content_types=content_types,
            dry_run=args.dry_run,
        )
        if stats.skipped:
            continue
        total_lectures += 1
        total_vectors += stats.vectors_uploaded
        total_chars += stats.chars_embedded
        if stats.error:
            total_errors += 1

    cost = _estimate_cost(total_chars)
    summary = (
        f"Migrated {total_lectures} lecture(s), "
        f"{total_vectors} vectors. "
        f"Estimated cost: ${cost:.2f}"
    )
    if args.dry_run:
        summary += " [dry-run — no API calls made]"
    if total_errors:
        summary += f" — {total_errors} lecture(s) had errors"

    logger.info(summary)
    return 0 if total_errors == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
