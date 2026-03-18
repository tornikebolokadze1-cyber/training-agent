"""CLI tool for manually processing a Zoom recording.

Use this when you need to:
- Reprocess a failed recording
- Test the pipeline with a sample video
- Process a recording that was missed by the webhook

Usage:
    python -m tools.app.process_recording <video_file> --group 1 --lecture 2
    python -m tools.app.process_recording <video_file> --group 2 --lecture 5 --skip-drive
"""

import argparse
import logging
from pathlib import Path

from tools.core.config import GROUPS, get_lecture_folder_name
from tools.integrations.gdrive_manager import ensure_folder, get_drive_service, upload_file
from tools.services.transcribe_lecture import transcribe_and_index

logger = logging.getLogger(__name__)


def process(
    video_path: str,
    group_number: int,
    lecture_number: int,
    skip_drive: bool = False,
) -> dict:
    """Process a recording through the full pipeline.

    Optionally uploads the recording to Drive first, then delegates to
    transcribe_and_index() for the full analysis pipeline.

    Args:
        video_path: Path to the video file.
        group_number: 1 or 2.
        lecture_number: Lecture number (1-15).
        skip_drive: Skip Google Drive recording upload (for testing).

    Returns:
        Dict with processing results including Pinecone index counts.
    """
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(f"Video file not found: {video_path}")

    group = GROUPS.get(group_number)
    if not group:
        raise ValueError(f"Invalid group number: {group_number}")

    results: dict = {"group": group_number, "lecture": lecture_number}

    # Step 1: Upload recording to Google Drive (optional)
    if not skip_drive:
        logger.info("Uploading recording to Google Drive...")
        service = get_drive_service()
        lecture_folder_name = get_lecture_folder_name(lecture_number)
        lecture_folder_id = ensure_folder(
            service, lecture_folder_name, group["drive_folder_id"]
        )
        recording_id = upload_file(video_path, lecture_folder_id)
        results["drive_recording_url"] = f"https://drive.google.com/file/d/{recording_id}/view"
        logger.info("Recording uploaded: %s", results["drive_recording_url"])
    else:
        logger.info("Skipping Google Drive recording upload")

    # Step 2: Full analysis pipeline (transcribe → analyze → Drive → WhatsApp → Pinecone)
    logger.info("Running full analysis pipeline...")
    index_counts = transcribe_and_index(group_number, lecture_number, video_path)
    results["index_counts"] = index_counts
    results["total_vectors"] = sum(index_counts.values())
    logger.info("Pipeline complete: %d vectors indexed", results["total_vectors"])

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Process a Zoom recording: upload, analyze, report"
    )
    parser.add_argument("video", help="Path to the video file")
    parser.add_argument("--group", type=int, required=True, choices=[1, 2],
                        help="Group number (1 or 2)")
    parser.add_argument("--lecture", type=int, required=True,
                        help="Lecture number (1-15)")
    parser.add_argument("--skip-drive", action="store_true",
                        help="Skip Google Drive recording upload")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    print(f"Processing: Group {args.group}, Lecture #{args.lecture}")
    print(f"Video: {args.video}")
    print()

    results = process(
        args.video,
        args.group,
        args.lecture,
        skip_drive=args.skip_drive,
    )

    print("\n" + "=" * 60)
    print("RESULTS:")
    print("=" * 60)
    for key, value in results.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
