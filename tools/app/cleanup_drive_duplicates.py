"""Clean up duplicate video files from Google Drive lecture folders.

Scans all lecture folders (both groups, lectures 1-15), identifies
duplicate .mp4 files, keeps the NEWEST one, and moves the rest to
Drive Trash (recoverable for 30 days).

Usage:
    python -m tools.app.cleanup_drive_duplicates              # dry run (default)
    python -m tools.app.cleanup_drive_duplicates --execute     # actually trash files
    python -m tools.app.cleanup_drive_duplicates --group 1     # only Group 1
    python -m tools.app.cleanup_drive_duplicates --lectures 5,6,7  # specific lectures
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field

from tools.core.config import GROUPS, TOTAL_LECTURES, get_lecture_folder_name
from tools.integrations.gdrive_manager import (
    find_folder,
    get_drive_service,
    list_files_in_folder,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TrashCandidate:
    """A file identified as a duplicate to be trashed."""

    file_id: str
    name: str
    size_bytes: int
    modified_time: str
    group: int
    lecture: int


@dataclass
class CleanupResult:
    """Aggregated results from the cleanup scan."""

    total_folders_scanned: int = 0
    total_videos_found: int = 0
    duplicates_found: int = 0
    duplicates_trashed: int = 0
    bytes_freed: int = 0
    errors: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    trashed: list[str] = field(default_factory=list)


def _format_size(size_bytes: int) -> str:
    """Format bytes as human-readable string."""
    if size_bytes >= 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 ** 3):.2f} GB"
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 ** 2):.1f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.0f} KB"
    return f"{size_bytes} B"


def _is_video_file(file_info: dict) -> bool:
    """Check if a Drive file is a video based on MIME type or extension."""
    mime = file_info.get("mimeType", "")
    name = file_info.get("name", "").lower()
    return mime.startswith("video/") or name.endswith(".mp4")


def find_duplicates_in_folder(
    folder_id: str,
    group: int,
    lecture: int,
) -> tuple[dict | None, list[TrashCandidate]]:
    """Scan a lecture folder and identify duplicate videos.

    Returns:
        (keeper, duplicates) — the file to keep and list of duplicates.
        keeper is None if 0 or 1 videos found (no duplicates).
    """
    files = list_files_in_folder(folder_id)
    videos = [f for f in files if _is_video_file(f)]

    if len(videos) <= 1:
        return None, []

    # Sort by modifiedTime descending (newest first), then by size descending
    # as tiebreaker — keeps the newest, largest file.
    videos.sort(
        key=lambda v: (
            v.get("modifiedTime", ""),
            int(v.get("size", 0)),
        ),
        reverse=True,
    )

    keeper = videos[0]
    duplicates = [
        TrashCandidate(
            file_id=v["id"],
            name=v.get("name", "unknown"),
            size_bytes=int(v.get("size", 0)),
            modified_time=v.get("modifiedTime", "?"),
            group=group,
            lecture=lecture,
        )
        for v in videos[1:]
    ]

    return keeper, duplicates


def trash_file(service, file_id: str) -> None:
    """Move a file to Google Drive trash (recoverable for 30 days)."""
    service.files().update(
        fileId=file_id,
        body={"trashed": True},
    ).execute()


def run_cleanup(
    groups: list[int],
    lectures: list[int],
    dry_run: bool,
) -> CleanupResult:
    """Scan lecture folders and trash duplicate videos.

    Args:
        groups: Which group numbers to scan (e.g. [1, 2]).
        lectures: Which lecture numbers to scan (e.g. [1, 2, ..., 15]).
        dry_run: If True, only report what would be trashed.

    Returns:
        CleanupResult with scan and action totals.
    """
    service = get_drive_service()
    result = CleanupResult()

    for group_num in groups:
        group_config = GROUPS.get(group_num)
        if not group_config:
            msg = f"Group {group_num} not found in config"
            logger.error(msg)
            result.errors.append(msg)
            continue

        parent_folder_id = group_config["drive_folder_id"]
        if not parent_folder_id:
            msg = f"No Drive folder ID for Group {group_num}"
            logger.error(msg)
            result.errors.append(msg)
            continue

        logger.info("")
        logger.info(
            "=" * 60 + "\n"
            "  Group %d — %s\n" + "=" * 60,
            group_num,
            group_config["name"],
        )

        for lecture_num in lectures:
            folder_name = get_lecture_folder_name(lecture_num)
            folder_id = find_folder(service, folder_name, parent_folder_id)

            if not folder_id:
                logger.debug(
                    "  %s not found in Group %d — skipping",
                    folder_name, group_num,
                )
                continue

            result.total_folders_scanned += 1

            # Count all videos
            all_files = list_files_in_folder(folder_id)
            video_count = sum(1 for f in all_files if _is_video_file(f))
            result.total_videos_found += video_count

            keeper, duplicates = find_duplicates_in_folder(
                folder_id, group_num, lecture_num,
            )

            if not duplicates:
                if video_count == 1:
                    logger.info(
                        "  %s: 1 video, no duplicates",
                        folder_name,
                    )
                elif video_count == 0:
                    logger.info("  %s: no videos", folder_name)
                continue

            # Report keeper
            keeper_size = _format_size(int(keeper.get("size", 0)))
            keeper_modified = keeper.get("modifiedTime", "?")[:19]
            logger.info(
                "  %s: %d videos found, keeping newest:",
                folder_name, video_count,
            )
            logger.info(
                "    KEEP: %s (%s, %s)",
                keeper.get("name", "?"), keeper_size, keeper_modified,
            )
            result.kept.append(
                f"Group {group_num}, {folder_name}: {keeper.get('name', '?')}"
            )

            # Process duplicates
            for dup in duplicates:
                result.duplicates_found += 1
                dup_size = _format_size(dup.size_bytes)
                dup_modified = dup.modified_time[:19]

                if dry_run:
                    logger.info(
                        "    [DRY RUN] Would trash: %s (%s, %s)",
                        dup.name, dup_size, dup_modified,
                    )
                    result.bytes_freed += dup.size_bytes
                    result.trashed.append(
                        f"Group {group_num}, {folder_name}: "
                        f"{dup.name} ({dup_size})"
                    )
                else:
                    try:
                        trash_file(service, dup.file_id)
                        result.duplicates_trashed += 1
                        result.bytes_freed += dup.size_bytes
                        logger.info(
                            "    TRASHED: %s (%s, %s)",
                            dup.name, dup_size, dup_modified,
                        )
                        result.trashed.append(
                            f"Group {group_num}, {folder_name}: "
                            f"{dup.name} ({dup_size})"
                        )
                    except Exception as e:
                        msg = f"Failed to trash {dup.name}: {e}"
                        logger.error("    ERROR: %s", msg)
                        result.errors.append(msg)

    return result


def print_summary(result: CleanupResult, dry_run: bool) -> None:
    """Print a clear summary of the cleanup operation."""
    separator = "=" * 60
    logger.info("")
    logger.info(separator)
    logger.info("  SUMMARY / ᲨᲔᲯᲐᲛᲔᲑᲐ")
    logger.info(separator)
    logger.info("")
    logger.info(
        "  Folders scanned / დასკანერებული საქაღალდეები: %d",
        result.total_folders_scanned,
    )
    logger.info(
        "  Total videos found / ნაპოვნი ვიდეოები:       %d",
        result.total_videos_found,
    )
    logger.info(
        "  Duplicates found / დუბლიკატები:              %d",
        result.duplicates_found,
    )

    if dry_run:
        logger.info(
            "  Storage to free / გასათავისუფლებელი:         %s",
            _format_size(result.bytes_freed),
        )
    else:
        logger.info(
            "  Duplicates trashed / წაშლილი:                %d",
            result.duplicates_trashed,
        )
        logger.info(
            "  Storage freed / გათავისუფლებული:             %s",
            _format_size(result.bytes_freed),
        )

    if result.errors:
        logger.info("")
        logger.info("  Errors / შეცდომები: %d", len(result.errors))
        for err in result.errors:
            logger.info("    - %s", err)

    if result.trashed:
        logger.info("")
        if dry_run:
            logger.info("  Files that WOULD be trashed / წასაშლელი ფაილები:")
        else:
            logger.info("  Trashed files / წაშლილი ფაილები:")
        for item in result.trashed:
            logger.info("    - %s", item)

    logger.info("")
    if dry_run:
        logger.info(
            "  This was a DRY RUN. Use --execute to actually trash files."
        )
        logger.info(
            "  ეს იყო სატესტო გაშვება. გამოიყენე --execute ფაილების წასაშლელად."
        )
    else:
        logger.info(
            "  Files moved to Drive Trash (recoverable for 30 days)."
        )
        logger.info(
            "  ფაილები გადატანილია Drive-ის ნაგავში (აღდგენა შესაძლებელია 30 დღის განმავლობაში)."
        )
    logger.info(separator)


def main() -> None:
    """CLI entry point for the cleanup script."""
    parser = argparse.ArgumentParser(
        description=(
            "Clean up duplicate video files from Google Drive lecture folders.\n"
            "Default: dry run (shows what would be trashed without doing it)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually trash duplicate files (default is dry run)",
    )
    parser.add_argument(
        "--group",
        type=int,
        choices=[1, 2],
        help="Scan only this group (default: both groups)",
    )
    parser.add_argument(
        "--lectures",
        type=str,
        help="Comma-separated lecture numbers, e.g. '1,5,6' (default: all 1-15)",
    )
    args = parser.parse_args()

    dry_run = not args.execute

    # Determine which groups to scan
    groups = [args.group] if args.group else list(GROUPS.keys())

    # Determine which lectures to scan
    if args.lectures:
        try:
            lectures = [int(x.strip()) for x in args.lectures.split(",")]
            for lec in lectures:
                if not 1 <= lec <= TOTAL_LECTURES:
                    logger.error(
                        "Lecture number %d out of range (1-%d)", lec, TOTAL_LECTURES,
                    )
                    sys.exit(1)
        except ValueError:
            logger.error("Invalid --lectures format. Use comma-separated numbers: 1,5,6")
            sys.exit(1)
    else:
        lectures = list(range(1, TOTAL_LECTURES + 1))

    # Banner
    mode = "DRY RUN" if dry_run else "EXECUTE"
    logger.info("=" * 60)
    logger.info("  Drive Duplicate Cleanup — %s", mode)
    logger.info("  Groups: %s | Lectures: %s", groups, lectures)
    if dry_run:
        logger.info("  (use --execute to actually trash files)")
    logger.info("=" * 60)

    result = run_cleanup(groups, lectures, dry_run)
    print_summary(result, dry_run)

    # Exit with error code if there were failures
    if result.errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
