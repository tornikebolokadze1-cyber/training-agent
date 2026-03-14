"""CLI tool for manually processing a Zoom recording.

Use this when you need to:
- Reprocess a failed recording
- Test the pipeline with a sample video
- Process a recording that was missed by the webhook

Usage:
    python -m tools.process_recording <video_file> --group 1 --lecture 2
    python -m tools.process_recording <video_file> --group 2 --lecture 5 --skip-drive
"""

import argparse
import logging
from pathlib import Path

from tools.config import GROUPS, get_lecture_folder_name
from tools.gdrive_manager import (
    create_google_doc,
    ensure_folder,
    get_drive_service,
    upload_file,
)
from tools.gemini_analyzer import analyze_lecture
from tools.whatsapp_sender import send_group_upload_notification, send_private_report

logger = logging.getLogger(__name__)


def process(
    video_path: str,
    group_number: int,
    lecture_number: int,
    skip_drive: bool = False,
    skip_whatsapp: bool = False,
) -> dict:
    """Process a recording through the full pipeline.

    Args:
        video_path: Path to the video file.
        group_number: 1 or 2.
        lecture_number: Lecture number (1-15).
        skip_drive: Skip Google Drive upload (for testing).
        skip_whatsapp: Skip WhatsApp message (for testing).

    Returns:
        Dict with processing results.
    """
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(f"Video file not found: {video_path}")

    group = GROUPS.get(group_number)
    if not group:
        raise ValueError(f"Invalid group number: {group_number}")

    lecture_folder_name = get_lecture_folder_name(lecture_number)
    results = {"group": group_number, "lecture": lecture_number}

    # Step 1: Upload recording to Google Drive
    if not skip_drive:
        logger.info("Uploading recording to Google Drive...")
        service = get_drive_service()
        lecture_folder_id = ensure_folder(
            service, lecture_folder_name, group["drive_folder_id"]
        )
        recording_id = upload_file(video_path, lecture_folder_id)
        results["drive_recording_url"] = f"https://drive.google.com/file/d/{recording_id}/view"
        logger.info("Recording uploaded: %s", results["drive_recording_url"])
    else:
        logger.info("Skipping Google Drive upload")

    # Step 2: Gemini analysis
    logger.info("Starting Gemini multimodal analysis...")
    analysis = analyze_lecture(str(video_path))
    results["summary"] = analysis["summary"]
    results["gap_analysis"] = analysis["gap_analysis"]
    results["deep_analysis"] = analysis.get("deep_analysis", "")
    logger.info("Analysis complete")

    # Step 3: Create summary doc in Drive
    if not skip_drive:
        summary_title = f"{lecture_folder_name} — შეჯამება"
        doc_id = create_google_doc(
            summary_title, analysis["summary"], lecture_folder_id
        )
        results["summary_doc_url"] = f"https://docs.google.com/document/d/{doc_id}/edit"
        logger.info("Summary doc: %s", results["summary_doc_url"])

    # Step 4: Notify WhatsApp group that materials are uploaded
    if not skip_whatsapp and not skip_drive:
        try:
            send_group_upload_notification(
                group_number, lecture_number,
                results.get("drive_recording_url", ""),
                results.get("summary_doc_url", ""),
            )
            logger.info("WhatsApp group notified about uploaded materials")
        except Exception as exc:
            logger.error("WhatsApp group notification failed: %s", exc)

    # Step 5: Send gap analysis privately to Tornike via WhatsApp
    if not skip_whatsapp:
        gap_header = (
            f"📊 ლექცია #{lecture_number} — ანალიზი\n"
            f"ჯგუფი: {group_number}\n"
            f"{'─' * 30}\n\n"
        )
        send_private_report(gap_header + analysis["gap_analysis"])
        logger.info("Gap analysis sent via WhatsApp")

        # Deep analysis
        deep = analysis.get("deep_analysis", "")
        if deep:
            deep_header = (
                f"🌍 ლექცია #{lecture_number} — ღრმა ანალიზი (გლობალური კონტექსტი)\n"
                f"ჯგუფი: {group_number}\n"
                f"{'━' * 30}\n\n"
            )
            send_private_report(deep_header + deep)
            logger.info("Deep analysis sent via WhatsApp")
    else:
        logger.info("Skipping WhatsApp messages")

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
                        help="Skip Google Drive upload")
    parser.add_argument("--skip-whatsapp", action="store_true",
                        help="Skip WhatsApp message")

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
        skip_whatsapp=args.skip_whatsapp,
    )

    print("\n" + "=" * 60)
    print("RESULTS:")
    print("=" * 60)
    for key, value in results.items():
        if key in ("summary", "gap_analysis"):
            print(f"\n{key.upper()}:")
            print("-" * 40)
            print(value[:500] + "..." if len(value) > 500 else value)
        else:
            print(f"{key}: {value}")


if __name__ == "__main__":
    main()
