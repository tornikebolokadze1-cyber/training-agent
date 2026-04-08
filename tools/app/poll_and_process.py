"""Poll Zoom for Group 1 Lecture #4 recording and run full pipeline when ready.

One-shot script: polls every 2 minutes, downloads when ready, runs the full
transcribe_and_index pipeline, then exits.

Usage:
    python -m tools.app.poll_and_process
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Ensure dotenv is loaded before any other project imports
from dotenv import load_dotenv

env_path = Path(__file__).parent.parent.parent / ".env"
load_dotenv(env_path)

from tools.core.config import GROUPS, TMP_DIR, get_lecture_folder_name  # noqa: E402
from tools.integrations.zoom_manager import (  # noqa: E402
    ZoomAPIError,
    download_recording,
    get_access_token,
    get_meeting_recordings,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration for this run
# ---------------------------------------------------------------------------

GROUP_NUMBER = 1
LECTURE_NUMBER = 4
MEETING_UUID = "2KHgg6jbTey12pTtvFeS2A=="

POLL_INTERVAL_SECONDS = 120  # 2 minutes
MAX_POLL_DURATION_SECONDS = 4 * 60 * 60  # 4 hours absolute cap


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _poll_for_recording() -> list[dict]:
    """Poll Zoom until MP4 recording segments are completed.

    Returns:
        List of completed MP4 recording file dicts, or empty list on timeout.
    """
    elapsed = 0

    while elapsed < MAX_POLL_DURATION_SECONDS:
        logger.info(
            "[poll] Checking recording status for meeting %s (elapsed %d min)...",
            MEETING_UUID,
            elapsed // 60,
        )

        try:
            recordings = get_meeting_recordings(MEETING_UUID)
        except ZoomAPIError as exc:
            if exc.status_code == 404:
                logger.info(
                    "[poll] Recording not ready yet (404 — still processing). "
                    "Retrying in %d seconds...",
                    POLL_INTERVAL_SECONDS,
                )
                time.sleep(POLL_INTERVAL_SECONDS)
                elapsed += POLL_INTERVAL_SECONDS
                continue
            # Auth or unexpected error — log and retry
            logger.error("[poll] Zoom API error: %s — retrying...", exc)
            time.sleep(POLL_INTERVAL_SECONDS)
            elapsed += POLL_INTERVAL_SECONDS
            continue
        except Exception as exc:
            logger.error("[poll] Unexpected error: %s — retrying...", exc)
            time.sleep(POLL_INTERVAL_SECONDS)
            elapsed += POLL_INTERVAL_SECONDS
            continue

        # Check for completed MP4 files
        files = recordings.get("recording_files", [])
        mp4_files = [
            f
            for f in files
            if f.get("file_type", "").upper() in {"MP4", "VIDEO"}
            and f.get("status", "").upper() == "COMPLETED"
        ]

        if mp4_files:
            logger.info(
                "[poll] Found %d completed MP4 segment(s)! "
                "Waiting one extra poll to catch late segments...",
                len(mp4_files),
            )
            # One extra poll to catch late-arriving segments
            time.sleep(POLL_INTERVAL_SECONDS)
            try:
                recordings = get_meeting_recordings(MEETING_UUID)
                files = recordings.get("recording_files", [])
                mp4_files = [
                    f
                    for f in files
                    if f.get("file_type", "").upper() in {"MP4", "VIDEO"}
                    and f.get("status", "").upper() == "COMPLETED"
                ]
            except Exception as exc:
                logger.warning(
                    "[poll] Extra poll failed: %s — proceeding with %d segment(s)",
                    exc,
                    len(mp4_files),
                )

            logger.info(
                "[poll] Final count: %d MP4 segment(s) ready for download.",
                len(mp4_files),
            )
            return mp4_files

        # Not ready yet — show what statuses we see
        statuses = [f"{f.get('file_type')}={f.get('status')}" for f in files]
        logger.info(
            "[poll] %d files found but none completed yet: %s. "
            "Retrying in %d seconds...",
            len(files),
            statuses,
            POLL_INTERVAL_SECONDS,
        )
        time.sleep(POLL_INTERVAL_SECONDS)
        elapsed += POLL_INTERVAL_SECONDS

    logger.error(
        "[poll] Timed out after %d minutes waiting for recording.",
        MAX_POLL_DURATION_SECONDS // 60,
    )
    return []


def _concatenate_segments(segment_paths: list[Path], output_path: Path) -> None:
    """Concatenate multiple MP4 segments into one file using ffmpeg."""
    concat_list = output_path.parent / f"{output_path.stem}_segments.txt"
    concat_list.write_text(
        "\n".join(f"file '{p}'" for p in segment_paths),
        encoding="utf-8",
    )

    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(concat_list), "-c", "copy", str(output_path),
    ]
    logger.info("[download] Concatenating %d segments with ffmpeg...", len(segment_paths))

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    concat_list.unlink(missing_ok=True)

    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg concat failed: {result.stderr[:500]}")

    size_mb = output_path.stat().st_size / (1024 * 1024)
    logger.info("[download] Concatenation complete: %.1f MB", size_mb)


def _download_and_process(mp4_files: list[dict]) -> None:
    """Download recording segments, upload to Drive, and run full pipeline."""
    from tools.integrations.gdrive_manager import (
        ensure_folder,
        get_drive_service,
        upload_file,
    )
    from tools.services.transcribe_lecture import transcribe_and_index

    group = GROUPS[GROUP_NUMBER]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    segment_paths: list[Path] = []
    temp_files: list[Path] = []

    # Step 1: Download all segments
    logger.info("[download] Downloading %d recording segment(s)...", len(mp4_files))
    for i, rec in enumerate(mp4_files):
        seg_filename = f"group{GROUP_NUMBER}_lecture{LECTURE_NUMBER}_{timestamp}_seg{i}.mp4"
        seg_path = TMP_DIR / seg_filename

        access_token = get_access_token()
        download_recording(rec["download_url"], access_token, seg_path)
        segment_paths.append(seg_path)
        temp_files.append(seg_path)
        logger.info(
            "[download] Segment %d downloaded: %.1f MB",
            i,
            seg_path.stat().st_size / (1024 * 1024),
        )

    # Step 2: Concatenate if multiple segments
    local_filename = f"group{GROUP_NUMBER}_lecture{LECTURE_NUMBER}_{timestamp}.mp4"
    local_path = TMP_DIR / local_filename

    if len(segment_paths) == 1:
        segment_paths[0].rename(local_path)
        temp_files = [local_path]
    else:
        _concatenate_segments(segment_paths, local_path)
        temp_files.append(local_path)

    file_size_mb = local_path.stat().st_size / (1024 * 1024)
    logger.info(
        "[download] Recording ready: %.1f MB (%d segment(s))",
        file_size_mb,
        len(segment_paths),
    )

    # Step 3: Upload recording to Google Drive
    logger.info("[drive] Uploading recording to Google Drive...")
    service = get_drive_service()
    lecture_folder_name = get_lecture_folder_name(LECTURE_NUMBER)
    lecture_folder_id = ensure_folder(
        service,
        lecture_folder_name,
        group["drive_folder_id"],
    )
    upload_file(local_path, lecture_folder_id)
    logger.info("[drive] Recording uploaded to Drive")

    # Step 4: Full analysis pipeline
    logger.info("[pipeline] Starting full analysis pipeline...")
    index_counts = transcribe_and_index(GROUP_NUMBER, LECTURE_NUMBER, local_path)
    logger.info(
        "[pipeline] Pipeline complete — Group %d, Lecture #%d (%d vectors indexed)",
        GROUP_NUMBER,
        LECTURE_NUMBER,
        sum(index_counts.values()),
    )

    # Step 5: Cleanup temp files
    for p in temp_files:
        if p.exists():
            p.unlink()
            logger.info("[cleanup] Removed temp file: %s", p.name)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Poll for recording, then run the full pipeline."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    logger.info("=" * 60)
    logger.info("Training Agent — Poll & Process")
    logger.info("Group: %d, Lecture: #%d", GROUP_NUMBER, LECTURE_NUMBER)
    logger.info("Meeting UUID: %s", MEETING_UUID)
    logger.info("Poll interval: %d seconds", POLL_INTERVAL_SECONDS)
    logger.info("=" * 60)

    # Step 1: Poll until recording is ready
    mp4_files = _poll_for_recording()
    if not mp4_files:
        logger.error("No recording found. Exiting.")
        sys.exit(1)

    # Step 2: Download and run full pipeline
    try:
        _download_and_process(mp4_files)
    except Exception as exc:
        logger.exception("Pipeline failed: %s", exc)
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("All done! Group %d, Lecture #%d fully processed.", GROUP_NUMBER, LECTURE_NUMBER)
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
