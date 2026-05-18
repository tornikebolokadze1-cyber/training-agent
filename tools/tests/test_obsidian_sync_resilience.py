"""Resilience tests for obsidian_sync — US-004 + US-014.

Covers:
- Dynamic GROUPS lookup in sync_whatsapp (replaces hardcoded g_num==1/2 chain)
- Skip-if-exists sync-hash guard in sync_lecture
- Partial-run cleanup guard via .tmp/obsidian_sync_progress.json checkpoint
- Resumable sync_full that skips already-completed lectures
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from tools.integrations import obsidian_sync


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect VAULT_ROOT, TMP_DIR-based checkpoint, and ENTITIES_DIR to tmp_path."""
    vault = tmp_path / "obsidian-vault"
    tmpdir = tmp_path / "tmp"
    entities = tmpdir / "entities"
    vault.mkdir()
    tmpdir.mkdir()
    entities.mkdir()

    monkeypatch.setattr(obsidian_sync, "VAULT_ROOT", vault)
    monkeypatch.setattr(obsidian_sync, "TMP_DIR", tmpdir)
    monkeypatch.setattr(obsidian_sync, "ENTITIES_DIR", entities)
    monkeypatch.setattr(
        obsidian_sync, "_SYNC_CHECKPOINT_PATH", tmpdir / "obsidian_sync_progress.json"
    )
    return tmp_path


@pytest.fixture
def fake_groups() -> dict[int, dict]:
    """A GROUPS dict that includes a May cohort (g3) the old chain would miss."""
    return {
        1: {
            "name": "მარტის ჯგუფი #1",
            "whatsapp_chat_id": "111@g.us",
        },
        2: {
            "name": "მარტის ჯგუფი #2",
            "whatsapp_chat_id": "222@g.us",
        },
        3: {
            "name": "მაისის ჯგუფი #1",
            "whatsapp_chat_id": "333@g.us",
        },
    }


# ---------------------------------------------------------------------------
# US-004: Dynamic GROUPS lookup
# ---------------------------------------------------------------------------


def test_g_num_dynamic_lookup(
    isolated_vault: Path,
    fake_groups: dict[int, dict],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sync_whatsapp must route via GROUPS[g_num]['whatsapp_chat_id'] for ALL groups,
    including new May cohorts (g3) that the old `if g_num == 1 / elif g_num == 2`
    chain could never reach.
    """
    monkeypatch.setattr(obsidian_sync, "GROUPS", fake_groups)
    monkeypatch.setattr(obsidian_sync, "GREEN_API_INSTANCE_ID", "stub_id")
    monkeypatch.setattr(obsidian_sync, "GREEN_API_TOKEN", "stub_token")

    seen_chat_ids: list[str] = []

    class _FakeResponse:
        status_code = 200

        def json(self) -> list:
            return []

    class _FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self) -> "_FakeClient":
            return self

        def __exit__(self, *args) -> None:
            pass

        def post(self, url: str, json: dict) -> _FakeResponse:
            seen_chat_ids.append(json["chatId"])
            return _FakeResponse()

    import httpx

    monkeypatch.setattr(httpx, "Client", _FakeClient)

    obsidian_sync.sync_whatsapp()

    # All three configured chat_ids must have been hit — proves the dynamic
    # GROUPS lookup, not the hardcoded chain.
    assert "111@g.us" in seen_chat_ids
    assert "222@g.us" in seen_chat_ids
    assert "333@g.us" in seen_chat_ids, "May cohort (g3) was not routed — hardcoded chain regression"


def test_no_hardcoded_g_num_branches_in_production_paths() -> None:
    """Static check: the file no longer contains the hardcoded chain."""
    src = Path(obsidian_sync.__file__).read_text(encoding="utf-8")
    # Permit the pattern inside test files but not in obsidian_sync.py itself.
    assert "g_num == 1" not in src, "Hardcoded `g_num == 1` branch resurfaced"
    assert "g_num == 2" not in src, "Hardcoded `g_num == 2` branch resurfaced"
    assert "WHATSAPP_GROUP1_ID" not in src
    assert "WHATSAPP_GROUP2_ID" not in src


# ---------------------------------------------------------------------------
# US-014: skip-if-exists + checkpoint + partial-run guard
# ---------------------------------------------------------------------------


def test_skip_if_exists_same_hash(isolated_vault: Path) -> None:
    """If the destination .md already carries a matching sync-hash header,
    _write_if_changed must NOT rewrite it.
    """
    target = isolated_vault / "concept.md"
    payload = {"name": "Claude", "category": "tool"}

    # Initial write
    written = obsidian_sync._write_if_changed(target, "body line\n", payload)
    assert written is True
    first_mtime = target.stat().st_mtime_ns
    first_bytes = target.read_bytes()

    # Second call with identical payload — must skip
    import time as _time

    _time.sleep(0.01)  # ensure mtime would change if we wrote
    written_again = obsidian_sync._write_if_changed(target, "body line\n", payload)
    assert written_again is False
    assert target.stat().st_mtime_ns == first_mtime
    assert target.read_bytes() == first_bytes


def test_force_rewrites(isolated_vault: Path) -> None:
    """force=True must rewrite the file even when the sync-hash matches."""
    target = isolated_vault / "concept.md"
    payload = {"name": "Claude"}

    assert obsidian_sync._write_if_changed(target, "body\n", payload) is True
    # Sanity: a non-forced second call would skip
    assert obsidian_sync._write_if_changed(target, "body\n", payload) is False
    # force=True must rewrite
    assert obsidian_sync._write_if_changed(target, "body\n", payload, force=True) is True


def test_skip_if_exists_different_payload_rewrites(isolated_vault: Path) -> None:
    """A changed payload must trigger a rewrite (hash mismatch)."""
    target = isolated_vault / "concept.md"
    assert obsidian_sync._write_if_changed(target, "body\n", {"v": 1}) is True
    assert obsidian_sync._write_if_changed(target, "body\n", {"v": 2}) is True


def test_partial_run_blocks_cleanup(isolated_vault: Path) -> None:
    """When the checkpoint indicates an incomplete run, _cleanup_stale_files
    must refuse to delete anything — the 2026-05-07 regression guard.
    """
    # Populate vault with one orphan .md that the cleanup would normally delete.
    folder = obsidian_sync.VAULT_ROOT / "კონცეფციები"
    folder.mkdir(parents=True, exist_ok=True)
    orphan = folder / "OldConcept.md"
    orphan.write_text("legacy content", encoding="utf-8")

    # Simulate a partial run by writing an incomplete checkpoint.
    obsidian_sync._save_sync_checkpoint(
        {
            "completed": ["g1_l1"],
            "last_run_started": obsidian_sync._now_iso(),
            "last_lecture_completed": "g1_l1",
            "last_run_completed": None,  # <-- the gate
        }
    )

    # Empty concept_index would normally delete the orphan.
    deleted = obsidian_sync._cleanup_stale_files({})
    assert deleted == 0
    assert orphan.exists(), "cleanup must refuse during a partial run"


def test_partial_run_safe_flag_overrides(isolated_vault: Path) -> None:
    """partial_run_safe=True is the explicit opt-in that cleanup must honor."""
    folder = obsidian_sync.VAULT_ROOT / "კონცეფციები"
    folder.mkdir(parents=True, exist_ok=True)
    orphan = folder / "OldConcept.md"
    orphan.write_text("legacy", encoding="utf-8")

    obsidian_sync._save_sync_checkpoint(
        {
            "completed": [],
            "last_run_started": obsidian_sync._now_iso(),
            "last_lecture_completed": None,
            "last_run_completed": None,
        }
    )

    deleted = obsidian_sync._cleanup_stale_files({}, partial_run_safe=True)
    assert deleted == 1
    assert not orphan.exists()


def test_cleanup_proceeds_after_completed_run(isolated_vault: Path) -> None:
    """When the checkpoint shows last_run_completed != None, cleanup runs normally."""
    folder = obsidian_sync.VAULT_ROOT / "კონცეფციები"
    folder.mkdir(parents=True, exist_ok=True)
    orphan = folder / "OldConcept.md"
    orphan.write_text("legacy", encoding="utf-8")

    obsidian_sync._save_sync_checkpoint(
        {
            "completed": ["g1_l1"],
            "last_run_started": obsidian_sync._now_iso(),
            "last_lecture_completed": "g1_l1",
            "last_run_completed": obsidian_sync._now_iso(),
        }
    )

    deleted = obsidian_sync._cleanup_stale_files({})
    assert deleted == 1
    assert not orphan.exists()


def test_cleanup_when_no_checkpoint(isolated_vault: Path) -> None:
    """No checkpoint file at all = no partial run = cleanup proceeds."""
    folder = obsidian_sync.VAULT_ROOT / "კონცეფციები"
    folder.mkdir(parents=True, exist_ok=True)
    orphan = folder / "OldConcept.md"
    orphan.write_text("legacy", encoding="utf-8")

    assert not obsidian_sync._SYNC_CHECKPOINT_PATH.exists()
    deleted = obsidian_sync._cleanup_stale_files({})
    assert deleted == 1


def test_resume_from_checkpoint(
    isolated_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sync_full must skip lectures already listed in the checkpoint's completed
    set so a crashed run can resume from the next lecture instead of redoing
    everything (and triggering Gemini rate limits or wasting cost).
    """
    # Seed checkpoint with 2 completed lectures.
    obsidian_sync._save_sync_checkpoint(
        {
            "completed": ["g1_l1", "g1_l2"],
            "last_run_started": obsidian_sync._now_iso(),
            "last_lecture_completed": "g1_l2",
            "last_run_completed": None,
        }
    )

    # Mock the Pinecone discovery to return three lectures total.
    fake_idx = MagicMock()

    def _fake_list(prefix: str, limit: int):
        # Return one id only for the summary prefixes of l1, l2, l3.
        if "_summary_" in prefix:
            return iter([["fake_id"]])
        return iter([[]])

    fake_idx.list.side_effect = _fake_list

    monkeypatch.setattr(
        obsidian_sync,
        "GROUPS",
        {1: {"name": "G1", "whatsapp_chat_id": ""}},
    )

    # Build a list of (g, lec) the discovery loop will produce.
    # We directly stub sync_lecture so no real Gemini/Pinecone work happens.
    sync_calls: list[tuple[int, int]] = []

    def _fake_sync_lecture(g: int, lec: int, *, force: bool = False) -> dict[str, int]:
        sync_calls.append((g, lec))
        return {"concepts": 1, "relationships": 0, "files_updated": 1}

    monkeypatch.setattr(obsidian_sync, "sync_lecture", _fake_sync_lecture)

    # Stub knowledge_indexer.get_pinecone_index used inside sync_full.
    import types

    fake_ki = types.ModuleType("tools.integrations.knowledge_indexer")
    fake_ki.get_pinecone_index = lambda: fake_idx  # type: ignore[attr-defined]
    monkeypatch.setitem(__import__("sys").modules,
                        "tools.integrations.knowledge_indexer", fake_ki)

    # Make extract_entities range tiny via patching range inside sync_full's frame
    # is fragile; instead, rely on fake_idx returning empty for lec >= 4.
    def _fake_list_bounded(prefix: str, limit: int):
        # Match l1, l2, l3 only.
        if any(f"_l{n}_" in prefix for n in (1, 2, 3)) and "_summary_" in prefix:
            return iter([["fake_id"]])
        return iter([[]])

    fake_idx.list.side_effect = _fake_list_bounded

    result = obsidian_sync.sync_full()

    # Only l3 should have been synced — l1 and l2 are in the checkpoint.
    assert sync_calls == [(1, 3)], f"Expected only g1_l3, got {sync_calls}"

    # Checkpoint must now include all three and be marked complete.
    cp = obsidian_sync._load_sync_checkpoint()
    assert cp is not None
    assert set(cp["completed"]) == {"g1_l1", "g1_l2", "g1_l3"}
    assert cp["last_run_completed"] is not None
    assert result["files_updated"] >= 1


def test_force_clears_checkpoint(
    isolated_vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sync_full(force=True) must ignore the prior checkpoint and re-sync all."""
    obsidian_sync._save_sync_checkpoint(
        {
            "completed": ["g1_l1"],
            "last_run_started": obsidian_sync._now_iso(),
            "last_lecture_completed": "g1_l1",
            "last_run_completed": None,
        }
    )

    fake_idx = MagicMock()

    def _fake_list(prefix: str, limit: int):
        if "_l1_summary_" in prefix:
            return iter([["fake_id"]])
        return iter([[]])

    fake_idx.list.side_effect = _fake_list

    monkeypatch.setattr(obsidian_sync, "GROUPS", {1: {"name": "G1", "whatsapp_chat_id": ""}})

    sync_calls: list[tuple[int, int]] = []

    def _fake_sync_lecture(g: int, lec: int, *, force: bool = False) -> dict[str, int]:
        sync_calls.append((g, lec))
        assert force is True, "force flag must propagate to sync_lecture"
        return {"concepts": 0, "relationships": 0, "files_updated": 0}

    monkeypatch.setattr(obsidian_sync, "sync_lecture", _fake_sync_lecture)

    import sys as _sys
    import types as _types

    fake_ki = _types.ModuleType("tools.integrations.knowledge_indexer")
    fake_ki.get_pinecone_index = lambda: fake_idx  # type: ignore[attr-defined]
    monkeypatch.setitem(_sys.modules, "tools.integrations.knowledge_indexer", fake_ki)

    obsidian_sync.sync_full(force=True)
    # Even though g1_l1 was in the checkpoint, force=True must re-sync it.
    assert (1, 1) in sync_calls


def test_checkpoint_atomic_write(isolated_vault: Path) -> None:
    """The checkpoint write uses temp+os.replace — no partial file should remain."""
    state = {
        "completed": ["g1_l1"],
        "last_run_started": obsidian_sync._now_iso(),
        "last_lecture_completed": "g1_l1",
        "last_run_completed": None,
    }
    obsidian_sync._save_sync_checkpoint(state)
    assert obsidian_sync._SYNC_CHECKPOINT_PATH.exists()
    tmp = obsidian_sync._SYNC_CHECKPOINT_PATH.with_suffix(".json.tmp")
    assert not tmp.exists(), "temp file must be replaced, not left around"
    loaded = json.loads(obsidian_sync._SYNC_CHECKPOINT_PATH.read_text(encoding="utf-8"))
    assert loaded["completed"] == ["g1_l1"]


def test_hash_is_stable_across_dict_ordering() -> None:
    """sort_keys=True guarantees the same hash for permuted dicts."""
    h1 = obsidian_sync._compute_payload_hash({"a": 1, "b": 2})
    h2 = obsidian_sync._compute_payload_hash({"b": 2, "a": 1})
    assert h1 == h2
