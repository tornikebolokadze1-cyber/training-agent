"""Transcribe a lecture video and deliver results.

Full pipeline: transcribe → analyze → upload analysis docs to Drive → notify WhatsApp → index Pinecone.
Note: The recording video itself is uploaded by the caller (server.py or scheduler.py), not here.

Usage:
    python -m tools.transcribe_lecture <group_number> <lecture_number> <video_path>

Resumes from existing transcript if found in .tmp/ (avoids re-transcription).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from tools.config import GROUPS, TMP_DIR, get_lecture_folder_name
from tools.gdrive_manager import create_google_doc, ensure_folder, get_drive_service
from tools.gemini_analyzer import analyze_lecture
from tools.knowledge_indexer import index_lecture_content
from tools.whatsapp_sender import alert_operator, send_group_upload_notification, send_private_report

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Drive helpers
# ---------------------------------------------------------------------------

def _get_lecture_folder_id(group_number: int, lecture_number: int) -> str | None:
    """Find or create the lecture folder in Google Drive."""
    group = GROUPS.get(group_number)
    if not group or not group["drive_folder_id"]:
        logger.warning("No Drive folder configured for Group %d", group_number)
        return None

    service = get_drive_service()
    parent_id = group["drive_folder_id"]
    folder_name = get_lecture_folder_name(lecture_number)
    return ensure_folder(service, folder_name, parent_id)


def _upload_summary_to_drive(
    group_number: int,
    lecture_number: int,
    summary: str,
) -> str | None:
    """Upload lecture summary as a Google Doc to the correct lecture folder.

    Returns the document ID, or None on failure.
    """
    try:
        folder_id = _get_lecture_folder_id(group_number, lecture_number)
        if not folder_id:
            return None

        title = f"ლექცია #{lecture_number} — შეჯამება"
        doc_id = create_google_doc(title, summary, folder_id)
        logger.info(
            "Uploaded summary to Drive: Group %d, ლექცია #%d (doc ID: %s)",
            group_number, lecture_number, doc_id,
        )
        return doc_id
    except Exception as e:
        logger.error("Failed to upload summary to Drive: %s", e)
        try:
            alert_operator(f"Drive summary upload FAILED (G{group_number} L#{lecture_number}): {e}")
        except Exception as alert_err:
            logger.error("alert_operator also failed: %s", alert_err)
        return None


def _get_drive_file_url(file_id: str, is_doc: bool = False) -> str:
    """Build a shareable Google Drive/Docs URL."""
    if is_doc:
        return f"https://docs.google.com/document/d/{file_id}/edit"
    return f"https://drive.google.com/file/d/{file_id}/view"


def _find_recording_in_drive(group_number: int, lecture_number: int) -> str | None:
    """Find the video recording file ID in the lecture's Drive folder."""
    from tools.gdrive_manager import list_files_in_folder

    folder_id = _get_lecture_folder_id(group_number, lecture_number)
    if not folder_id:
        return None

    files = list_files_in_folder(folder_id)
    for f in files:
        mime = f.get("mimeType", "")
        if mime.startswith("video/"):
            return f["id"]
    return None


def _upload_private_report_to_drive(
    group_number: int,
    lecture_number: int,
    gap_analysis: str,
    deep_analysis: str,
) -> str | None:
    """Upload combined gap+deep analysis as a private Google Doc.

    Uses the dedicated private analysis folder (კურსი #4 ანალიზი / ჯგუფი #N)
    which is separate from the shared group folders — only Tornike has access.

    Returns the document ID, or None on failure.
    """
    try:
        group = GROUPS.get(group_number)
        if not group:
            logger.warning("No group config for Group %d", group_number)
            return None

        analysis_folder_id = group.get("analysis_folder_id")
        if not analysis_folder_id:
            logger.warning("No analysis folder configured for Group %d — skipping", group_number)
            return None

        # Combine gap + deep analysis into one report
        group_name = group.get("name", f"ჯგუფი #{group_number}")
        report_content = (
            f"ჯგუფი: {group_name}\n"
            f"{'━' * 50}\n\n"
            f"🔍 GAP ANALYSIS\n"
            f"{'─' * 50}\n\n"
            f"{gap_analysis}\n\n"
            f"{'━' * 50}\n\n"
            f"🧠 DEEP ANALYSIS\n"
            f"{'─' * 50}\n\n"
            f"{deep_analysis}"
        )

        title = f"ლექცია #{lecture_number}"
        doc_id = create_google_doc(title, report_content, analysis_folder_id)
        logger.info(
            "Private report uploaded to Drive: Group %d, ლექცია #%d (doc ID: %s)",
            group_number, lecture_number, doc_id,
        )
        return doc_id

    except Exception as e:
        logger.error("Failed to upload private report to Drive: %s", e)
        try:
            alert_operator(f"Drive private report upload FAILED (G{group_number} L#{lecture_number}): {e}")
        except Exception as alert_err:
            logger.error("alert_operator also failed: %s", alert_err)
        return None


# ---------------------------------------------------------------------------
# WhatsApp helpers
# ---------------------------------------------------------------------------

def _notify_group_whatsapp(
    group_number: int,
    lecture_number: int,
    recording_file_id: str | None,
    summary_doc_id: str | None,
) -> None:
    """Send WhatsApp notification to the training group about uploaded materials."""
    if not summary_doc_id:
        logger.warning("No summary doc ID — skipping WhatsApp group notification")
        return

    recording_url = _get_drive_file_url(recording_file_id) if recording_file_id else "ატვირთვა მიმდინარეობს"
    summary_url = _get_drive_file_url(summary_doc_id, is_doc=True)

    try:
        send_group_upload_notification(
            group_number=group_number,
            lecture_number=lecture_number,
            drive_recording_url=recording_url,
            summary_doc_url=summary_url,
        )
        logger.info("WhatsApp group notification sent for Group %d, Lecture #%d", group_number, lecture_number)
    except Exception as e:
        logger.error("Failed to send WhatsApp group notification: %s", e)
        try:
            alert_operator(f"WhatsApp group notification FAILED (G{group_number} L#{lecture_number}): {e}")
        except Exception as alert_err:
            logger.error("alert_operator also failed: %s", alert_err)


def _send_private_report_to_tornike(
    group_number: int,
    lecture_number: int,
    report_doc_id: str | None,
) -> None:
    """Send WhatsApp notification to Tornike with link to private analysis doc."""
    group_name = GROUPS.get(group_number, {}).get("name", f"ჯგუფი #{group_number}")

    if report_doc_id:
        doc_url = _get_drive_file_url(report_doc_id, is_doc=True)
        message = (
            f"📊 ლექცია #{lecture_number} — ანალიზი მზადაა\n"
            f"ჯგუფი: {group_name}\n\n"
            f"🔗 {doc_url}"
        )
    else:
        message = (
            f"📊 ლექცია #{lecture_number} — ანალიზი\n"
            f"ჯგუფი: {group_name}\n\n"
            f"⚠️ Drive-ზე ატვირთვა ვერ მოხერხდა"
        )

    try:
        send_private_report(message)
        logger.info("Private report link sent to Tornike for Group %d, Lecture #%d", group_number, lecture_number)
    except Exception as e:
        logger.error("Failed to send private report link: %s", e)
        # Don't alert_operator here — this IS the private channel to Tornike
        # Just log at CRITICAL level so it appears in rotating log
        logger.critical("CRITICAL: Private report delivery failed for G%d L#%d: %s", group_number, lecture_number, e)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def transcribe_and_index(
    group_number: int,
    lecture_number: int,
    video_path: str | Path,
) -> dict[str, int]:
    """Full pipeline: transcribe → analyze → Drive → WhatsApp → Pinecone.

    Workflow:
    1. Transcribe lecture video multimodally with Gemini 2.5 Pro (45-min chunks)
    2. Analyze with Claude Opus (reasoning) + Gemini (Georgian writing)
    3. Upload summary to Google Drive (same folder as video recording)
    4. Upload private analysis to Drive (📊 ანალიზი folder, owner-only)
    5. Send WhatsApp notification to training group (video + summary ready)
    6. Send private report link to Tornike via WhatsApp
    7. Index text content into Pinecone for RAG

    Automatically resumes from existing transcript if found in .tmp/.
    """
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(f"Video not found: {video_path}")

    logger.info(
        "Starting full pipeline for Group %d, Lecture #%d (%s)",
        group_number, lecture_number, video_path.name,
    )

    # Check for existing transcript (resume support)
    transcript_path = TMP_DIR / f"g{group_number}_l{lecture_number}_transcript.txt"
    existing_transcript = None
    if transcript_path.exists():
        candidate = transcript_path.read_text(encoding="utf-8")
        if len(candidate.strip()) >= 2000:
            existing_transcript = candidate
            logger.info(
                "Found existing transcript (%d chars) — skipping transcription",
                len(existing_transcript),
            )
        else:
            logger.warning(
                "Existing transcript too short (%d chars) — will re-transcribe",
                len(candidate),
            )

    # Step 1: Analysis pipeline (transcribe if needed + Claude reasoning + Gemini writing)
    logger.info("Step 1: Running analysis pipeline...")
    results = analyze_lecture(video_path, existing_transcript=existing_transcript)

    # Save raw outputs to .tmp immediately (crash resilience)
    for content_type in ("transcript", "summary", "gap_analysis", "deep_analysis"):
        text = results.get(content_type, "")
        if text:
            out_path = TMP_DIR / f"g{group_number}_l{lecture_number}_{content_type}.txt"
            out_path.write_text(text, encoding="utf-8")
            logger.info("Saved %s to %s (%d chars)", content_type, out_path.name, len(text))

    # Step 1.5: Extract and persist scores to analytics DB (non-fatal)
    try:
        from tools.analytics import save_scores_from_analysis
        if results.get("deep_analysis"):
            saved = save_scores_from_analysis(
                group_number, lecture_number, results["deep_analysis"]
            )
            if not saved:
                logger.warning(
                    "Score extraction returned no data for Group %d Lecture #%d "
                    "(score table missing or malformed in deep analysis)",
                    group_number, lecture_number,
                )
    except Exception as _analytics_err:
        logger.error("Score persistence failed (non-fatal): %s", _analytics_err)

    # Step 2: Upload summary to Google Drive
    summary = results.get("summary", "")
    summary_doc_id = None
    if summary:
        logger.info("Step 2: Uploading summary to Google Drive...")
        summary_doc_id = _upload_summary_to_drive(group_number, lecture_number, summary)

    # Step 3: Send WhatsApp notification to group (video + summary are ready)
    logger.info("Step 3: Notifying WhatsApp group...")
    recording_file_id = _find_recording_in_drive(group_number, lecture_number)
    _notify_group_whatsapp(group_number, lecture_number, recording_file_id, summary_doc_id)

    # Step 4: Upload private report to Drive (📊 ანალიზი folder, owner-only)
    gap_analysis = results.get("gap_analysis", "")
    deep_analysis = results.get("deep_analysis", "")
    report_doc_id = None
    if gap_analysis or deep_analysis:
        logger.info("Step 4: Uploading private analysis to Drive...")
        report_doc_id = _upload_private_report_to_drive(
            group_number, lecture_number, gap_analysis, deep_analysis,
        )

    # Step 5: Send private report link to Tornike via WhatsApp
    logger.info("Step 5: Sending private report link to Tornike...")
    _send_private_report_to_tornike(group_number, lecture_number, report_doc_id)

    # Step 6: Index all content types into Pinecone
    logger.info("Step 6: Indexing into Pinecone...")
    index_counts: dict[str, int] = {}
    for content_type in ("transcript", "summary", "gap_analysis", "deep_analysis"):
        text = results.get(content_type, "")
        if not text:
            logger.warning("No %s content to index", content_type)
            continue

        try:
            count = index_lecture_content(
                group_number=group_number,
                lecture_number=lecture_number,
                content=text,
                content_type=content_type,
            )
            index_counts[content_type] = count
            logger.info("Indexed %d vectors for %s", count, content_type)
        except Exception as e:
            logger.error("Failed to index %s into Pinecone: %s", content_type, e)
            index_counts[content_type] = 0
            try:
                alert_operator(f"Pinecone indexing FAILED for {content_type} (G{group_number} L#{lecture_number}): {e}")
            except Exception as alert_err:
                logger.error("alert_operator also failed: %s", alert_err)

    # Quality gate: warn if critical analysis outputs are empty
    empty_analyses = [k for k in ("summary", "gap_analysis", "deep_analysis") if not results.get(k)]
    if empty_analyses:
        warning_msg = (
            f"Pipeline completed with EMPTY analyses for G{group_number} L#{lecture_number}: "
            f"{', '.join(empty_analyses)}"
        )
        logger.warning(warning_msg)
        try:
            alert_operator(warning_msg)
        except Exception as alert_err:
            logger.error("alert_operator also failed: %s", alert_err)

    total = sum(index_counts.values())
    logger.info(
        "Pipeline complete for Group %d, Lecture #%d: %d total vectors indexed",
        group_number, lecture_number, total,
    )
    return index_counts


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if len(sys.argv) < 4:
        print("Usage: python -m tools.transcribe_lecture <group> <lecture> <video_path>")
        sys.exit(1)

    try:
        grp = int(sys.argv[1])
        lec = int(sys.argv[2])
    except ValueError:
        print("Error: group and lecture must be integers")
        sys.exit(1)

    if grp not in GROUPS:
        print(f"Error: group must be one of {list(GROUPS.keys())}")
        sys.exit(1)
    if not (1 <= lec <= 15):
        print("Error: lecture must be between 1 and 15")
        sys.exit(1)

    vid = sys.argv[3]

    counts = transcribe_and_index(grp, lec, vid)
    print(f"\nResults for Group {grp}, Lecture #{lec}:")
    for ctype, cnt in counts.items():
        print(f"  {ctype}: {cnt} vectors")
    print(f"  TOTAL: {sum(counts.values())} vectors")
