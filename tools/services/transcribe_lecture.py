"""Transcribe a lecture video and deliver results.

Full pipeline: transcribe → analyze → upload analysis docs to Drive → notify WhatsApp → index Pinecone.
Note: The recording video itself is uploaded by the caller (server.py or scheduler.py), not here.

Usage:
    python -m tools.services.transcribe_lecture <group_number> <lecture_number> <video_path>

Resumes from existing transcript if found in .tmp/ (avoids re-transcription).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from tools.core.config import (
    GROUPS,
    TMP_DIR,
    get_drive_file_url,
    get_lecture_folder_name,
)
from tools.core.pipeline_state import (
    load_state,
    transition,
    mark_complete,
    mark_failed,
    PENDING,
    TRANSCRIBING,
    UPLOADING_DOCS,
    NOTIFYING,
    INDEXING,
)
from tools.core.retry import safe_operation
from tools.integrations.gdrive_manager import (
    create_google_doc,
    ensure_folder,
    get_drive_service,
)
from tools.integrations.gemini_analyzer import analyze_lecture, cleanup_checkpoints
from tools.integrations.knowledge_indexer import index_lecture_content
from tools.integrations.whatsapp_sender import (
    alert_operator,
    send_email_fallback,
    send_group_upload_notification,
    send_private_report,
)

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


@safe_operation("Drive summary upload", alert=True)
def _upload_summary_to_drive(
    group_number: int,
    lecture_number: int,
    summary: str,
) -> str | None:
    """Upload lecture summary as a Google Doc to the correct lecture folder.

    Returns the document ID, or None on failure.
    """
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


@safe_operation("Drive recording lookup", alert=False)
def _find_recording_in_drive(group_number: int, lecture_number: int) -> str | None:
    """Find the video recording file ID in the lecture's Drive folder."""
    from tools.integrations.gdrive_manager import list_files_in_folder

    folder_id = _get_lecture_folder_id(group_number, lecture_number)
    if not folder_id:
        return None

    files = list_files_in_folder(folder_id)
    for f in files:
        mime = f.get("mimeType", "")
        if mime.startswith("video/"):
            return f["id"]
    return None


@safe_operation("Drive private report upload", alert=True)
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


# ---------------------------------------------------------------------------
# WhatsApp helpers
# ---------------------------------------------------------------------------

@safe_operation("WhatsApp group notification", alert=True, default=None)
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

    recording_url = get_drive_file_url(recording_file_id) if recording_file_id else "ატვირთვა მიმდინარეობს"
    summary_url = get_drive_file_url(summary_doc_id, is_doc=True)

    send_group_upload_notification(
        group_number=group_number,
        lecture_number=lecture_number,
        drive_recording_url=recording_url,
        summary_doc_url=summary_url,
    )
    logger.info("WhatsApp group notification sent for Group %d, Lecture #%d", group_number, lecture_number)


def _send_private_report_to_tornike(
    group_number: int,
    lecture_number: int,
    report_doc_id: str | None,
) -> None:
    """Send WhatsApp notification to Tornike with link to private analysis doc."""
    group_name = GROUPS.get(group_number, {}).get("name", f"ჯგუფი #{group_number}")

    if report_doc_id:
        doc_url = get_drive_file_url(report_doc_id, is_doc=True)
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
        logger.error("Failed to send private report link via WhatsApp: %s", e)
        # Try email fallback before giving up
        email_sent = send_email_fallback(
            subject=f"📊 ლექცია #{lecture_number} ანალიზი — {group_name}",
            body=message,
        )
        if email_sent:
            logger.info("Private report link sent via email fallback for G%d L#%d", group_number, lecture_number)
        else:
            logger.critical("CRITICAL: Private report delivery failed (WhatsApp + Email) for G%d L#%d: %s", group_number, lecture_number, e)


# ---------------------------------------------------------------------------
# Safe wrappers for use inside loops / inline calls
# ---------------------------------------------------------------------------

@safe_operation("Pinecone indexing", alert=True, default=0)
def _safe_index(
    group_number: int,
    lecture_number: int,
    content: str,
    content_type: str,
) -> int:
    """Index a single content type into Pinecone, returning the vector count."""
    return index_lecture_content(
        group_number=group_number,
        lecture_number=lecture_number,
        content=content,
        content_type=content_type,
    )


@safe_operation("Quality gate alert", alert=False, default=None)
def _safe_alert(message: str) -> None:
    """Send an operator alert, swallowing failures."""
    alert_operator(message)


# ---------------------------------------------------------------------------
# Resume helpers
# ---------------------------------------------------------------------------


def _load_cached_results(group_number: int, lecture_number: int) -> dict[str, str]:
    """Load previously saved analysis results from .tmp/ files."""
    results: dict[str, str] = {}
    for content_type in ("transcript", "summary", "gap_analysis", "deep_analysis"):
        path = TMP_DIR / f"g{group_number}_l{lecture_number}_{content_type}.txt"
        if path.exists():
            text = path.read_text(encoding="utf-8")
            if text.strip():
                results[content_type] = text
    return results


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
    1.5. Extract and persist scores to analytics DB
    2. Upload summary to Google Drive (same folder as video recording)
    3. Upload private analysis to Drive (📊 ანალიზი folder, owner-only)
    4. Send WhatsApp notification to training group (video + summary ready)
    5. Send private report link to Tornike via WhatsApp
    6. Index text content into Pinecone for RAG
    7. Sync Obsidian knowledge vault

    Automatically resumes from existing transcript if found in .tmp/.
    """
    video_path = Path(video_path)
    if not video_path.exists():
        raise FileNotFoundError(f"Video not found: {video_path}")

    # Load pipeline state if it exists (created by server.py or scheduler.py)
    pipeline = load_state(group_number, lecture_number)

    # Resume support: if pipeline is past TRANSCRIBING, load cached results
    skip_analysis = False
    if pipeline and pipeline.state not in (PENDING, TRANSCRIBING, ""):
        cached = _load_cached_results(group_number, lecture_number)
        if cached.get("transcript") and cached.get("summary"):
            logger.info(
                "Resuming from state %s — loading cached results (%d content types)",
                pipeline.state, len(cached),
            )
            results = cached
            skip_analysis = True

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

    try:
        if not skip_analysis:
            # Step 1: Analysis pipeline (transcribe if needed + Claude reasoning + Gemini writing)
            if pipeline:
                pipeline = transition(pipeline, TRANSCRIBING)
            logger.info("Step 1: Running analysis pipeline...")
            results = analyze_lecture(
                video_path,
                existing_transcript=existing_transcript,
                group=group_number,
                lecture=lecture_number,
            )

            # Save raw outputs to .tmp immediately (crash resilience)
            for content_type in ("transcript", "summary", "gap_analysis", "deep_analysis"):
                text = results.get(content_type, "")
                if text:
                    out_path = TMP_DIR / f"g{group_number}_l{lecture_number}_{content_type}.txt"
                    out_path.write_text(text, encoding="utf-8")
                    logger.info("Saved %s to %s (%d chars)", content_type, out_path.name, len(text))

            # Quality gate: BLOCK completion if critical outputs are empty or too short
            # Runs BEFORE any delivery (Drive uploads, WhatsApp) to prevent garbage docs
            MIN_SUMMARY_CHARS = 500
            MIN_ANALYSIS_CHARS = 300
            quality_failures: list[str] = []
            for key, min_len in [
                ("summary", MIN_SUMMARY_CHARS),
                ("gap_analysis", MIN_ANALYSIS_CHARS),
                ("deep_analysis", MIN_ANALYSIS_CHARS),
            ]:
                text = results.get(key, "")
                if not text:
                    quality_failures.append(f"{key} is EMPTY")
                elif len(text.strip()) < min_len:
                    quality_failures.append(f"{key} too short ({len(text)} chars, need {min_len})")
            if quality_failures:
                failure_msg = (
                    f"Quality gate FAILED for G{group_number} L#{lecture_number}: "
                    f"{'; '.join(quality_failures)}"
                )
                logger.error(failure_msg)
                _safe_alert(failure_msg)
                raise ValueError(failure_msg)

            # Step 1.5: Extract and persist scores to analytics DB (non-fatal)
            try:
                from tools.services.analytics import save_scores_from_analysis
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
        if pipeline and pipeline.summary_doc_id:
            summary_doc_id = pipeline.summary_doc_id
            logger.info("Skipping summary upload — already done (doc ID: %s)", summary_doc_id)
        elif summary:
            if pipeline:
                pipeline = transition(pipeline, UPLOADING_DOCS)
            logger.info("Step 2: Uploading summary to Google Drive...")
            summary_doc_id = _upload_summary_to_drive(group_number, lecture_number, summary)
            if pipeline and summary_doc_id:
                pipeline = transition(pipeline, UPLOADING_DOCS, summary_doc_id=summary_doc_id)

        # Step 3: Upload private report to Drive (📊 ანალიზი folder, owner-only)
        gap_analysis = results.get("gap_analysis", "")
        deep_analysis = results.get("deep_analysis", "")
        report_doc_id = None
        if pipeline and pipeline.report_doc_id:
            report_doc_id = pipeline.report_doc_id
            logger.info("Skipping private report upload — already done (doc ID: %s)", report_doc_id)
        elif gap_analysis or deep_analysis:
            logger.info("Step 3: Uploading private analysis to Drive...")
            report_doc_id = _upload_private_report_to_drive(
                group_number, lecture_number, gap_analysis, deep_analysis,
            )
            if pipeline and report_doc_id:
                pipeline = transition(pipeline, pipeline.state, report_doc_id=report_doc_id)

        # Step 4: Send WhatsApp notification to group (video + summary are ready)
        if pipeline and pipeline.group_notified:
            logger.info("Skipping WhatsApp group notification — already sent")
        else:
            if pipeline:
                pipeline = transition(pipeline, NOTIFYING)
            logger.info("Step 4: Notifying WhatsApp group...")
            recording_file_id = _find_recording_in_drive(group_number, lecture_number)
            _notify_group_whatsapp(group_number, lecture_number, recording_file_id, summary_doc_id)
            if pipeline:
                pipeline = transition(pipeline, pipeline.state, group_notified=True)

        # Step 5: Send private report link to Tornike via WhatsApp
        if pipeline and pipeline.private_notified:
            logger.info("Skipping private report notification — already sent")
        else:
            logger.info("Step 5: Sending private report link to Tornike...")
            _send_private_report_to_tornike(group_number, lecture_number, report_doc_id)
            if pipeline:
                pipeline = transition(pipeline, pipeline.state, private_notified=True)

        # Step 6: Index all content types into Pinecone
        if pipeline and pipeline.pinecone_indexed:
            logger.info("Skipping Pinecone indexing — already done")
            index_counts: dict[str, int] = {}
        else:
            if pipeline:
                pipeline = transition(pipeline, INDEXING)
            logger.info("Step 6: Indexing into Pinecone...")
            index_counts = {}
            for content_type in ("transcript", "summary", "gap_analysis", "deep_analysis"):
                text = results.get(content_type, "")
                if not text:
                    logger.warning("No %s content to index", content_type)
                    continue

                count = _safe_index(group_number, lecture_number, text, content_type)
                index_counts[content_type] = count
                if count:
                    logger.info("Indexed %d vectors for %s", count, content_type)
            if pipeline:
                pipeline = transition(pipeline, pipeline.state, pinecone_indexed=True)

        # Clean up checkpoint files now that the full pipeline succeeded
        deleted = cleanup_checkpoints(group_number, lecture_number)
        if deleted:
            logger.info("Cleaned up %d checkpoint files after successful pipeline", deleted)

        # Step 7: Sync Obsidian knowledge vault (non-fatal)
        try:
            from tools.integrations.obsidian_sync import sync_lecture as obsidian_sync
            logger.info("Step 7: Syncing Obsidian vault...")
            sync_result = obsidian_sync(group_number, lecture_number)
            logger.info(
                "Obsidian sync: %d concepts, %d relationships, %d files updated",
                sync_result.get("concepts", 0),
                sync_result.get("relationships", 0),
                sync_result.get("files_updated", 0),
            )
        except Exception as _obsidian_err:
            logger.error("Obsidian sync failed (non-fatal): %s", _obsidian_err)

        if pipeline:
            pipeline = mark_complete(pipeline)

        total = sum(index_counts.values())
        logger.info(
            "Pipeline complete for Group %d, Lecture #%d: %d total vectors indexed",
            group_number, lecture_number, total,
        )
        return index_counts

    except Exception as e:
        if pipeline:
            mark_failed(pipeline, str(e))
        raise


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if len(sys.argv) < 4:
        print("Usage: python -m tools.services.transcribe_lecture <group> <lecture> <video_path>")
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
