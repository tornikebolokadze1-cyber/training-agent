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


#: Every completed lecture must have vectors from all four pipeline stages.
#: Missing any one of these means the pipeline silently dropped that step
#: and the "მრჩეველო" AI will give weaker answers for that lecture.
EXPECTED_CONTENT_TYPES: frozenset[str] = frozenset(
    {"transcript", "summary", "gap_analysis", "deep_analysis"}
)

#: Lectures completed before deep_analysis was added to the pipeline
#: (commit ae94a6c, 2026-03-28). The audit treats missing deep_analysis
#: for these as a known historical gap rather than a bug. Backfilling
#: requires rerunning the Claude deep-analysis prompt per lecture (~$0.30
#: each) — tracked separately, not a daily-alert concern.
_HISTORICAL_DEEP_ANALYSIS_GAP: frozenset[tuple[int, int]] = frozenset({
    (1, 1), (1, 2), (1, 3), (1, 4),
    (2, 1), (2, 2), (2, 3), (2, 4), (2, 5),
})


def _list_pinecone_vectors_for_lecture(group: int, lecture: int) -> tuple[int, set[str]]:
    """Count vectors and their distinct content types for one lecture.

    Returns (total_count, content_types_present). Returns (-1, set()) on error.
    """
    try:
        from tools.integrations.knowledge_indexer import get_pinecone_index

        index = get_pinecone_index()
        prefix = f"g{group}_l{lecture}_"
        count = 0
        types: set[str] = set()
        for page in index.list(prefix=prefix):
            count += len(page)
            for id_ in page:
                # ID shape: g{G}_l{L}_{content_type}_{chunk_index}
                parts = id_.split("_", 2)
                if len(parts) < 3:
                    continue
                tail = parts[2]
                # Strip the trailing "_N" chunk index to isolate the type.
                type_name = tail.rsplit("_", 1)[0] if tail.rsplit("_", 1)[-1].isdigit() else tail
                types.add(type_name)
        return count, types
    except Exception as exc:
        logger.warning("[audit] Pinecone count failed for g%d/l%d: %s", group, lecture, exc)
        return -1, set()  # sentinel: unknown


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

        # Pinecone cross-check — count + content-type completeness
        vec_count, content_types = _list_pinecone_vectors_for_lecture(group, lecture_num)
        audit.pinecone_vector_count = vec_count
        if vec_count == 0:
            audit.issues.append("PINECONE_EMPTY")
        elif vec_count == -1:
            audit.issues.append("PINECONE_QUERY_FAILED")
        else:
            missing_types = EXPECTED_CONTENT_TYPES - content_types
            # Suppress the known historical gap: deep_analysis was added to
            # the pipeline after these lectures had already been processed.
            if (
                missing_types == {"deep_analysis"}
                and (group, lecture_num) in _HISTORICAL_DEEP_ANALYSIS_GAP
            ):
                missing_types = set()
            if missing_types:
                audit.issues.append(
                    f"MISSING_CONTENT_TYPES({','.join(sorted(missing_types))})"
                )

        results.append(audit)

    return sorted(results, key=lambda a: a.lecture)


def _drive_folder_id_for_group(group_num: int, cfg: dict) -> str:
    """Return the Drive root folder ID for a configured group."""
    return os.getenv(f"DRIVE_GROUP{group_num}_FOLDER_ID") or cfg.get(
        "drive_folder_id", ""
    )


def run_full_audit() -> dict:
    """Run audit for every configured group and return a structured report."""
    group_results: dict[str, list[dict]] = {}
    all_audits: list[LectureAudit] = []
    config_issues: list[str] = []

    for group_num, cfg in sorted(GROUPS.items()):
        key = f"group_{group_num}"
        root_folder_id = _drive_folder_id_for_group(group_num, cfg)
        if not root_folder_id:
            group_results[key] = []
            config_issues.append(
                f"Missing DRIVE_GROUP{group_num}_FOLDER_ID for "
                f"{cfg.get('name', f'Group {group_num}')}"
            )
            continue

        audits = audit_group(group_num, root_folder_id)
        group_results[key] = [a.to_dict() for a in audits]
        all_audits.extend(audits)

    issues = [a for a in all_audits if not a.is_clean]

    report = {
        "total_lectures_checked": len(all_audits),
        "issues_found": len(issues),
        "config_issues_found": len(config_issues),
        "all_clean": len(issues) == 0 and len(config_issues) == 0,
        "groups": group_results,
        "config_issues": config_issues,
        "issues": [a.to_dict() for a in issues],
    }
    # Keep report["group_1"] / report["group_2"] compatibility while exposing
    # all newer cohorts through the same top-level "group_N" shape.
    report.update(group_results)

    if issues or config_issues:
        config_block = "\n".join(f"  CONFIG: {issue}" for issue in config_issues)
        lecture_block = "\n".join(
            f"  G{a.group} L{a.lecture}: {', '.join(a.issues)}" for a in issues
        )
        logger.error(
            "[audit] %d lecture(s) and %d config item(s) have issues:\n%s",
            len(issues),
            len(config_issues),
            "\n".join(part for part in (config_block, lecture_block) if part),
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

        total_issues = report.get("issues_found", 0) + report.get(
            "config_issues_found", 0
        )
        lines = [
            f"🔍 Drive↔Pinecone audit found {total_issues} issue(s):",
            "",
        ]
        for issue in report.get("issues", []):
            label = f"G{issue['group']} L{issue['lecture']}"
            lines.append(f"  • {label}: {', '.join(issue['issues'])}")
        for issue in report.get("config_issues", []):
            lines.append(f"  • CONFIG: {issue}")
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
    logger.info("[audit] Audit complete: %d clean, %d issues, %d config issues",
                report["total_lectures_checked"] - report["issues_found"],
                report["issues_found"],
                report.get("config_issues_found", 0))


if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    report = run_full_audit()
    print(json.dumps(report, indent=2, ensure_ascii=False))
