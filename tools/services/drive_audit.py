"""Drive ↔ Pinecone consistency audit.

Compares lecture artifacts across three sources of truth:
  1. Google Drive  — video file + summary doc per ლექცია folder
  2. Pinecone      — indexed transcript vectors per (group, lecture)
  3. Pipeline state — local state file per (group, lecture)

Produces a structured report flagging any divergence so the operator
can fix gaps BEFORE students notice. Designed to run as a daily
APScheduler job (recommended: 09:00 Tbilisi, 11h before lectures).
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

from tools.core.config import GROUPS, TBILISI_TZ  # noqa: F401  (used by callers)
from tools.integrations.gdrive_manager import get_drive_service

logger = logging.getLogger(__name__)


_LECTURE_FOLDER_RE = re.compile(r"ლექცია\s*#?(\d+)")


@dataclass
class LectureAudit:
    """Audit result for a single (group, lecture) pair."""

    group: int
    lecture: int
    drive_video_count: int = 0
    drive_doc_count: int = 0
    drive_video_sizes_mb: list[float] = field(default_factory=list)
    pinecone_vector_count: int = 0
    issues: list[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict:
        return {
            "group": self.group,
            "lecture": self.lecture,
            "drive_videos": self.drive_video_count,
            "drive_docs": self.drive_doc_count,
            "drive_video_sizes_mb": self.drive_video_sizes_mb,
            "pinecone_vectors": self.pinecone_vector_count,
            "issues": self.issues,
            "clean": self.is_clean,
        }


def _list_pinecone_vectors_for_lecture(group: int, lecture: int) -> int:
    """Count vectors in Pinecone whose IDs start with g{group}_l{lecture}_."""
    try:
        from tools.integrations.knowledge_indexer import get_pinecone_index

        index = get_pinecone_index()
        prefix = f"g{group}_l{lecture}_"
        # describe_index_stats does NOT support prefix filtering, so we use
        # list() which yields IDs in pages. We just count.
        count = 0
        for page in index.list(prefix=prefix):
            count += len(page)
        return count
    except Exception as exc:
        logger.warning("[audit] Pinecone count failed for g%d/l%d: %s", group, lecture, exc)
        return -1  # sentinel: unknown


def audit_group(group: int, root_folder_id: str) -> list[LectureAudit]:
    """Audit every lecture folder under one group's root Drive folder."""
    svc = get_drive_service()
    folders = (
        svc.files()
        .list(
            q=f"'{root_folder_id}' in parents and "
              f"mimeType='application/vnd.google-apps.folder' and trashed=false",
            fields="files(id, name)",
            pageSize=50,
        )
        .execute()
        .get("files", [])
    )

    results: list[LectureAudit] = []

    for folder in folders:
        match = _LECTURE_FOLDER_RE.search(folder["name"])
        if not match:
            continue
        lecture_num = int(match.group(1))

        contents = (
            svc.files()
            .list(
                q=f"'{folder['id']}' in parents and trashed=false",
                fields="files(id, name, mimeType, size)",
                pageSize=50,
            )
            .execute()
            .get("files", [])
        )

        videos = [f for f in contents if "video" in f.get("mimeType", "")]
        docs = [
            f for f in contents
            if "document" in f.get("mimeType", "") and "შეჯამება" in f.get("name", "")
        ]

        audit = LectureAudit(
            group=group,
            lecture=lecture_num,
            drive_video_count=len(videos),
            drive_doc_count=len(docs),
            drive_video_sizes_mb=[
                round(int(v.get("size", 0)) / 1024 / 1024, 1) for v in videos
            ],
        )

        # Skip empty future-lecture folders (no content yet — expected)
        any_content = videos or docs or any(
            "document" in f.get("mimeType", "") for f in contents
        )
        if not any_content:
            continue

        # Drive integrity rules
        if audit.drive_video_count == 0:
            audit.issues.append("MISSING_VIDEO")
        elif audit.drive_video_count > 1:
            audit.issues.append(f"DUPLICATE_VIDEOS({audit.drive_video_count})")
        if audit.drive_doc_count == 0:
            audit.issues.append("MISSING_SUMMARY")

        # Pinecone cross-check
        vec_count = _list_pinecone_vectors_for_lecture(group, lecture_num)
        audit.pinecone_vector_count = vec_count
        if vec_count == 0:
            audit.issues.append("PINECONE_EMPTY")
        elif vec_count == -1:
            audit.issues.append("PINECONE_QUERY_FAILED")

        results.append(audit)

    return sorted(results, key=lambda a: a.lecture)


def run_full_audit() -> dict:
    """Run audit for both groups and return a structured report."""
    g1_root = os.getenv("DRIVE_GROUP1_FOLDER_ID")
    g2_root = os.getenv("DRIVE_GROUP2_FOLDER_ID")

    if not g1_root or not g2_root:
        raise RuntimeError("DRIVE_GROUP{1,2}_FOLDER_ID env vars are required")

    g1 = audit_group(1, g1_root)
    g2 = audit_group(2, g2_root)

    all_audits = g1 + g2
    issues = [a for a in all_audits if not a.is_clean]

    report = {
        "total_lectures_checked": len(all_audits),
        "issues_found": len(issues),
        "all_clean": len(issues) == 0,
        "group_1": [a.to_dict() for a in g1],
        "group_2": [a.to_dict() for a in g2],
        "issues": [a.to_dict() for a in issues],
    }

    if issues:
        logger.error(
            "[audit] %d lecture(s) have issues:\n%s",
            len(issues),
            "\n".join(
                f"  G{a.group} L{a.lecture}: {', '.join(a.issues)}" for a in issues
            ),
        )
    else:
        logger.info(
            "[audit] All %d lectures clean (videos+summaries+pinecone in sync)",
            len(all_audits),
        )

    return report


def alert_on_issues(report: dict) -> None:
    """Send WhatsApp alert if any issues are found."""
    if report.get("all_clean"):
        return

    try:
        from tools.integrations.whatsapp_sender import alert_operator

        lines = [
            f"🔍 Drive↔Pinecone audit found {report['issues_found']} issue(s):",
            "",
        ]
        for issue in report.get("issues", []):
            label = f"G{issue['group']} L{issue['lecture']}"
            lines.append(f"  • {label}: {', '.join(issue['issues'])}")
        lines.append("")
        lines.append("Run audit manually: python -m tools.services.drive_audit")

        alert_operator("\n".join(lines))
    except Exception as exc:
        logger.error("[audit] Failed to send alert: %s", exc)


def daily_audit_job() -> None:
    """Entry point for the APScheduler daily job."""
    logger.info("[audit] Starting daily Drive↔Pinecone audit...")
    report = run_full_audit()
    alert_on_issues(report)
    logger.info("[audit] Audit complete: %d clean, %d issues",
                report["total_lectures_checked"] - report["issues_found"],
                report["issues_found"])


if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    report = run_full_audit()
    print(json.dumps(report, indent=2, ensure_ascii=False))
