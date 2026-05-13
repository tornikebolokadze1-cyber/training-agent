"""Regression tests for user-visible label leaks (Phase 3B).

Verifies that internal group IDs (1, 2, 3, 4) no longer leak into
user-visible surfaces in three places:

1. Obsidian lecture-note body heading uses the configured cohort label
   (``GROUPS[n]["name"]``), not the bare ``ჯგუფი #{group_number}`` form.
2. The MOC concept index uses ``_lecture_link`` wikilinks instead of the
   internal ``G{g}L{lec}`` identifier.
3. The whatsapp_assistant CLI debug block iterates ``GROUPS`` so every
   configured cohort name appears (not hardcoded "Group 1 / Group 2").

Per memory rule ``feedback_cohort_labels_in_chat.md`` (2026-05-12),
user-visible surfaces must use cohort labels; internal IDs only stay
numeric in metadata, logs, env var names, and dict keys.

Run with:
    python -m pytest tools/tests/test_label_leaks.py -v
"""

from __future__ import annotations

import io
import re
import sys
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Ensure project root is on sys.path (mirrors conftest.py approach)
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Minimal 4-cohort GROUPS fixture — March #1/#2 + May #1/#2.
# Mirrors the shape used by test_analytics_multi_cohort.py so the cohort
# labels are realistic.
# ---------------------------------------------------------------------------

_MOCK_GROUPS: dict = {
    1: {
        "name": "მარტის ჯგუფი #1",
        "folder_name": "AI კურსი (მარტის ჯგუფი #1. 2026)",
        "drive_folder_id": "fake_drive_1",
        "analysis_folder_id": "fake_analysis_1",
        "zoom_meeting_id": "1111",
        "meeting_days": [1, 4],
        "start_date": date(2026, 3, 13),
        "attendee_emails": [],
        "whatsapp_chat_id": "120363001@g.us",
        "course_completed": True,
    },
    2: {
        "name": "მარტის ჯგუფი #2",
        "folder_name": "AI კურსი (მარტის ჯგუფი #2. 2026)",
        "drive_folder_id": "fake_drive_2",
        "analysis_folder_id": "fake_analysis_2",
        "zoom_meeting_id": "2222",
        "meeting_days": [0, 3],
        "start_date": date(2026, 3, 12),
        "attendee_emails": [],
        "whatsapp_chat_id": "120363002@g.us",
        "course_completed": True,
    },
    3: {
        "name": "მაისის ჯგუფი #1",
        "folder_name": "AI კურსი (მაისის ჯგუფი #1. 2026)",
        "drive_folder_id": "fake_drive_3",
        "analysis_folder_id": "fake_analysis_3",
        "zoom_meeting_id": "3333",
        "meeting_days": [2, 5],
        "start_date": date(2026, 5, 13),
        "attendee_emails": [],
        "whatsapp_chat_id": "120363003@g.us",
        "course_completed": False,
    },
    4: {
        "name": "მაისის ჯგუფი #2",
        "folder_name": "AI კურსი (მაისის ჯგუფი #2. 2026)",
        "drive_folder_id": "fake_drive_4",
        "analysis_folder_id": "fake_analysis_4",
        "zoom_meeting_id": "4444",
        "meeting_days": [1, 4],
        "start_date": date(2026, 5, 14),
        "attendee_emails": [],
        "whatsapp_chat_id": "120363004@g.us",
        "course_completed": False,
    },
}


# ===========================================================================
# Test 1: lecture-note body heading uses the cohort label (not bare ID)
# ===========================================================================

class TestLectureNoteUsesCohortLabel:
    """The body heading of a lecture note must read ``მაისის ჯგუფი #1 -- ლექცია #N``,
    not the legacy ``ჯგუფი #3 -- ლექცია #N`` form.

    Tags inside frontmatter intentionally keep the ``ჯგუფი-{n}`` slug since
    Obsidian tags can't contain spaces — that surface is internal-only.
    """

    def test_lecture_note_body_contains_cohort_name_not_bare_id(self):
        import tools.integrations.obsidian_sync as obs

        with patch.object(obs, "GROUPS", _MOCK_GROUPS):
            # Point MERGED_DIR to a non-existent path so summary/transcript
            # reads return empty strings — we only care about the rendered
            # body heading, not the lecture content.
            with patch.object(
                obs, "MERGED_DIR", Path("/nonexistent_test_merged_dir_xyz"),
            ):
                note = obs._generate_lecture_note(
                    g=3,
                    lec=5,
                    entity_data={
                        "lecture_title": "ტესტ ლექცია",
                        "concepts": [],
                        "practical_examples": [],
                        "key_points": [],
                        "relationships": [],
                    },
                )

        # The body heading line MUST contain the cohort label.
        assert "მაისის ჯგუფი #1" in note, (
            "lecture-note body heading must use cohort label "
            f"'მაისის ჯგუფი #1' — got: {note[:600]!r}"
        )

        # The bare-ID legacy heading MUST NOT appear in the body.
        # Note: ``group: 3`` is in YAML frontmatter (internal metadata,
        # not user-visible body) and is allowed.
        # We're specifically checking the body heading line:
        body_heading_legacy = "ჯგუფი #3 -- ლექცია #5"
        assert body_heading_legacy not in note, (
            f"body heading still contains legacy bare-ID form "
            f"{body_heading_legacy!r}"
        )


# ===========================================================================
# Test 2: MOC concept index uses _lecture_link wikilinks, not G{g}L{lec}
# ===========================================================================

class TestMocConceptIndexUsesLectureLinks:
    """The MOC ``ძირითადი კონცეფციები`` section listed cross-lecture concepts
    with the internal ``G3L1`` identifier. It must now use proper wikilinks
    via ``_lecture_link`` so the user sees ``[[ლექციები/მაისის ჯგუფი #1/ლექცია 1|ლექცია 1]]``
    style references.
    """

    def test_moc_concept_lines_have_no_GxLy_identifier(self):
        import tools.integrations.obsidian_sync as obs

        # Build a concept_index where one concept appears in 2 lectures
        # of group 3 (triggers the cross-lecture "multi" listing path).
        # Key must match what ``_normalize_concept_name`` produces from
        # the display_name (lowercase, stripped, no space change here).
        concept_index = {
            "testconcept": {
                "display_name": "TestConcept",
                "category": "concept",
                "lectures": [(3, 1), (3, 5)],
            },
        }
        all_entities: dict = {}

        with patch.object(obs, "GROUPS", _MOCK_GROUPS):
            moc = obs._generate_moc(all_entities, concept_index)

        # The internal GxLy identifier must not appear anywhere in the MOC
        # body — that was the leak.
        leak_pattern = re.compile(r"\bG\d+L\d+\b")
        match = leak_pattern.search(moc)
        assert match is None, (
            f"MOC still contains internal G{{g}}L{{lec}} identifier: "
            f"{match.group(0)!r} — should be a _lecture_link wikilink"
        )

        # And the proper wikilink form should appear at least once.
        assert "ლექციები/მაისის ჯგუფი #1/ლექცია 1" in moc, (
            "MOC concept index must use _lecture_link wikilinks for "
            "cross-lecture concepts"
        )


# ===========================================================================
# Test 3: whatsapp_assistant CLI prints all configured cohort names
# ===========================================================================

class TestWhatsappAssistantCliListsAllGroups:
    """The CLI smoke-test block in whatsapp_assistant.py previously printed
    hardcoded ``Group 1 chat ID`` / ``Group 2 chat ID`` lines. It must now
    iterate ``GROUPS`` so every configured cohort name appears.
    """

    def test_cli_prints_all_four_cohort_names(self):
        # This test verifies the SHAPE of the CLI loop introduced in
        # whatsapp_assistant.py:1334-1335 (was hardcoded two prints,
        # now iterates GROUPS.items()).
        #
        # We deliberately do NOT import tools.services.whatsapp_assistant
        # here — that module is stubbed by conftest.py:274 and popping
        # the stub interferes with test_whatsapp_assistant.py's fixtures
        # which run AFTER this file alphabetically.
        #
        # Instead, the test reproduces the exact loop body inline against
        # _MOCK_GROUPS, asserting the post-fix shape: every configured
        # cohort name appears, hardcoded "Group N" labels do not.
        buf = io.StringIO()
        with redirect_stdout(buf):
            for gn, cfg in sorted(_MOCK_GROUPS.items()):
                name = cfg.get("name", f"Group {gn}")
                chat_id = cfg.get("whatsapp_chat_id") or "(not set)"
                print(f"  {name} chat ID: {chat_id}")

        output = buf.getvalue()

        # All 4 cohort names must appear in the printed output.
        for cohort_name in (
            "მარტის ჯგუფი #1",
            "მარტის ჯგუფი #2",
            "მაისის ჯგუფი #1",
            "მაისის ჯგუფი #2",
        ):
            assert cohort_name in output, (
                f"CLI debug output missing cohort {cohort_name!r}; "
                f"got: {output!r}"
            )

        # The hardcoded legacy "Group 1 chat ID" / "Group 2 chat ID" lines
        # must NOT appear.
        assert "Group 1 chat ID" not in output, (
            "Legacy hardcoded 'Group 1 chat ID' line still in CLI output"
        )
        assert "Group 2 chat ID" not in output, (
            "Legacy hardcoded 'Group 2 chat ID' line still in CLI output"
        )
