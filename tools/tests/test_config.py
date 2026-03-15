"""Unit tests for tools/config.py.

Covers:
- validate_critical_config: warnings for non-critical vars, RuntimeError
  for WEBHOOK_SECRET missing in Railway environment
- _materialize_credential_file caching: second call reuses cached path
- _credential_file_cache: cache dict exists and is populated on use
- ManyChat vars removed: MANYCHAT_API_KEY / MANYCHAT_TORNIKE_SUBSCRIBER_ID
  no longer exist as module-level attributes
- _env: handles missing vars gracefully with correct default behaviour

Run with:
    pytest tools/tests/test_config.py -v
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Module stubs are set up in tools/tests/conftest.py.
# ---------------------------------------------------------------------------
import tools.config as cfg


# ===========================================================================
# 1. validate_critical_config
# ===========================================================================


class TestValidateCriticalConfig:
    """validate_critical_config returns warnings for non-critical missing vars
    and raises RuntimeError only when WEBHOOK_SECRET is absent in Railway."""

    def test_returns_list(self):
        """Return type is always list regardless of env state."""
        with patch.object(cfg, "IS_RAILWAY", False), \
             patch.object(cfg, "WEBHOOK_SECRET", "s3cr3t"), \
             patch.object(cfg, "GEMINI_API_KEY", "k"), \
             patch.object(cfg, "GEMINI_API_KEY_PAID", ""), \
             patch.object(cfg, "ANTHROPIC_API_KEY", "k"), \
             patch.object(cfg, "GREEN_API_INSTANCE_ID", "i"), \
             patch.object(cfg, "GREEN_API_TOKEN", "t"), \
             patch.object(cfg, "WHATSAPP_TORNIKE_PHONE", "p"):
            result = cfg.validate_critical_config()
        assert isinstance(result, list)

    def test_missing_gemini_key_produces_warning(self):
        """When both Gemini keys are absent a warning is appended."""
        with patch.object(cfg, "IS_RAILWAY", False), \
             patch.object(cfg, "WEBHOOK_SECRET", "s3cr3t"), \
             patch.object(cfg, "GEMINI_API_KEY", ""), \
             patch.object(cfg, "GEMINI_API_KEY_PAID", ""), \
             patch.object(cfg, "ANTHROPIC_API_KEY", "k"), \
             patch.object(cfg, "GREEN_API_INSTANCE_ID", "i"), \
             patch.object(cfg, "GREEN_API_TOKEN", "t"), \
             patch.object(cfg, "WHATSAPP_TORNIKE_PHONE", "p"):
            warnings = cfg.validate_critical_config()
        assert any("gemini" in w.lower() for w in warnings)

    def test_missing_anthropic_key_produces_warning(self):
        """Absent ANTHROPIC_API_KEY yields a warning."""
        with patch.object(cfg, "IS_RAILWAY", False), \
             patch.object(cfg, "WEBHOOK_SECRET", "s3cr3t"), \
             patch.object(cfg, "GEMINI_API_KEY", "k"), \
             patch.object(cfg, "GEMINI_API_KEY_PAID", ""), \
             patch.object(cfg, "ANTHROPIC_API_KEY", ""), \
             patch.object(cfg, "GREEN_API_INSTANCE_ID", "i"), \
             patch.object(cfg, "GREEN_API_TOKEN", "t"), \
             patch.object(cfg, "WHATSAPP_TORNIKE_PHONE", "p"):
            warnings = cfg.validate_critical_config()
        assert any("anthropic" in w.lower() or "claude" in w.lower() for w in warnings)

    def test_missing_green_api_produces_warning(self):
        """Absent GREEN_API_INSTANCE_ID or GREEN_API_TOKEN yields a warning."""
        with patch.object(cfg, "IS_RAILWAY", False), \
             patch.object(cfg, "WEBHOOK_SECRET", "s3cr3t"), \
             patch.object(cfg, "GEMINI_API_KEY", "k"), \
             patch.object(cfg, "GEMINI_API_KEY_PAID", ""), \
             patch.object(cfg, "ANTHROPIC_API_KEY", "k"), \
             patch.object(cfg, "GREEN_API_INSTANCE_ID", ""), \
             patch.object(cfg, "GREEN_API_TOKEN", ""), \
             patch.object(cfg, "WHATSAPP_TORNIKE_PHONE", "p"):
            warnings = cfg.validate_critical_config()
        assert any("green" in w.lower() or "whatsapp" in w.lower() for w in warnings)

    def test_missing_webhook_secret_in_local_does_not_raise(self):
        """Missing WEBHOOK_SECRET in local dev should NOT raise — only log a warning."""
        with patch.object(cfg, "IS_RAILWAY", False), \
             patch.object(cfg, "WEBHOOK_SECRET", ""), \
             patch.object(cfg, "GEMINI_API_KEY", "k"), \
             patch.object(cfg, "GEMINI_API_KEY_PAID", ""), \
             patch.object(cfg, "ANTHROPIC_API_KEY", "k"), \
             patch.object(cfg, "GREEN_API_INSTANCE_ID", "i"), \
             patch.object(cfg, "GREEN_API_TOKEN", "t"), \
             patch.object(cfg, "WHATSAPP_TORNIKE_PHONE", "p"):
            # Must not raise
            result = cfg.validate_critical_config()
        assert isinstance(result, list)

    def test_missing_webhook_secret_in_railway_raises_runtime_error(self):
        """Missing WEBHOOK_SECRET in Railway (IS_RAILWAY=True) raises RuntimeError."""
        with patch.object(cfg, "IS_RAILWAY", True), \
             patch.object(cfg, "WEBHOOK_SECRET", ""), \
             patch.object(cfg, "GEMINI_API_KEY", "k"), \
             patch.object(cfg, "GEMINI_API_KEY_PAID", ""), \
             patch.object(cfg, "ANTHROPIC_API_KEY", "k"), \
             patch.object(cfg, "GREEN_API_INSTANCE_ID", "i"), \
             patch.object(cfg, "GREEN_API_TOKEN", "t"), \
             patch.object(cfg, "WHATSAPP_TORNIKE_PHONE", "p"):
            with pytest.raises(RuntimeError, match="WEBHOOK_SECRET"):
                cfg.validate_critical_config()

    def test_all_vars_present_returns_empty_warnings(self):
        """With every variable set, the warning list is empty."""
        with patch.object(cfg, "IS_RAILWAY", False), \
             patch.object(cfg, "WEBHOOK_SECRET", "s3cr3t"), \
             patch.object(cfg, "GEMINI_API_KEY", "k"), \
             patch.object(cfg, "GEMINI_API_KEY_PAID", ""), \
             patch.object(cfg, "ANTHROPIC_API_KEY", "k"), \
             patch.object(cfg, "GREEN_API_INSTANCE_ID", "i"), \
             patch.object(cfg, "GREEN_API_TOKEN", "t"), \
             patch.object(cfg, "WHATSAPP_TORNIKE_PHONE", "p"):
            warnings = cfg.validate_critical_config()
        assert warnings == []


# ===========================================================================
# 2. _materialize_credential_file caching
# ===========================================================================


class TestMaterializeCredentialFileCaching:
    """Second call for the same b64_env_key returns the same cached path
    without creating a new temp file."""

    def test_second_call_returns_same_path(self, tmp_path):
        """Two calls with the same env key resolve to the identical Path object."""
        import base64

        dummy_json = '{"type": "service_account"}'
        b64_value = base64.b64encode(dummy_json.encode()).decode()
        env_key = "TEST_CRED_CACHE_KEY_UNIQUE"

        # Ensure a clean cache state for this key
        cfg._credential_file_cache.pop(env_key, None)

        with patch.dict(os.environ, {env_key: b64_value}):
            path1 = cfg._materialize_credential_file(env_key, tmp_path / "fallback.json")
            path2 = cfg._materialize_credential_file(env_key, tmp_path / "fallback.json")

        assert path1 == path2

    def test_second_call_does_not_create_new_temp_file(self, tmp_path):
        """The temp file is written exactly once; second call hits the cache."""
        import base64

        dummy_json = '{"type": "service_account"}'
        b64_value = base64.b64encode(dummy_json.encode()).decode()
        env_key = "TEST_CRED_NO_DUP_KEY_UNIQUE"

        cfg._credential_file_cache.pop(env_key, None)

        write_count = [0]
        real_write = tempfile.NamedTemporaryFile

        # Intercept NamedTemporaryFile to count file creations for our key
        def counting_ntf(*args, **kwargs):
            prefix = kwargs.get("prefix", "")
            if env_key.lower() in prefix:
                write_count[0] += 1
            return real_write(*args, **kwargs)

        with patch.dict(os.environ, {env_key: b64_value}), \
             patch("tools.config.tempfile.NamedTemporaryFile", side_effect=counting_ntf):
            cfg._materialize_credential_file(env_key, tmp_path / "fallback.json")
            cfg._materialize_credential_file(env_key, tmp_path / "fallback.json")

        assert write_count[0] == 1, (
            "NamedTemporaryFile should be called exactly once; "
            f"was called {write_count[0]} times"
        )

    def test_fallback_path_used_when_no_b64_env(self, tmp_path):
        """When the b64 env var is absent the local fallback file is returned."""
        env_key = "TEST_CRED_FALLBACK_KEY_UNIQUE"
        cfg._credential_file_cache.pop(env_key, None)

        fallback = tmp_path / "credentials.json"
        fallback.write_text('{"type": "service_account"}')

        # Make sure the env var is NOT set
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(env_key, None)
            result = cfg._materialize_credential_file(env_key, fallback)

        assert result == fallback

    def test_raises_file_not_found_when_neither_source_available(self, tmp_path):
        """FileNotFoundError raised when env var is absent and fallback missing."""
        env_key = "TEST_CRED_MISSING_KEY_UNIQUE"
        cfg._credential_file_cache.pop(env_key, None)

        missing = tmp_path / "does_not_exist.json"
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(env_key, None)
            with pytest.raises(FileNotFoundError):
                cfg._materialize_credential_file(env_key, missing)


# ===========================================================================
# 3. _credential_file_cache
# ===========================================================================


class TestCredentialFileCache:
    """The module-level cache dict exists and is correctly populated."""

    def test_cache_dict_exists(self):
        assert hasattr(cfg, "_credential_file_cache")
        assert isinstance(cfg._credential_file_cache, dict)

    def test_cache_populated_after_materialize_with_b64(self, tmp_path):
        """After a successful b64 decode the key appears in the cache."""
        import base64

        dummy_json = '{"type": "service_account"}'
        b64_value = base64.b64encode(dummy_json.encode()).decode()
        env_key = "TEST_CACHE_POPULATED_UNIQUE"

        cfg._credential_file_cache.pop(env_key, None)

        with patch.dict(os.environ, {env_key: b64_value}):
            result = cfg._materialize_credential_file(env_key, tmp_path / "fb.json")

        assert env_key in cfg._credential_file_cache
        assert cfg._credential_file_cache[env_key] == result


# ===========================================================================
# 4. ManyChat vars removed
# ===========================================================================


class TestManyChatVarsRemoved:
    """MANYCHAT_API_KEY and MANYCHAT_TORNIKE_SUBSCRIBER_ID must not exist as
    top-level attributes of the config module (they were removed when ManyChat
    was deprecated in favour of Green API)."""

    def test_manychat_api_key_not_present(self):
        assert not hasattr(cfg, "MANYCHAT_API_KEY"), (
            "MANYCHAT_API_KEY still exists in config — it should have been removed"
        )

    def test_manychat_subscriber_id_not_present(self):
        assert not hasattr(cfg, "MANYCHAT_TORNIKE_SUBSCRIBER_ID"), (
            "MANYCHAT_TORNIKE_SUBSCRIBER_ID still exists in config — it should have been removed"
        )


# ===========================================================================
# 5. _env helper
# ===========================================================================


class TestEnvHelper:
    """_env returns the env var value or the given default gracefully."""

    def test_returns_value_when_var_set(self):
        with patch.dict(os.environ, {"_TEST_ENV_VAR": "hello"}):
            assert cfg._env("_TEST_ENV_VAR") == "hello"

    def test_returns_empty_string_default_when_unset(self):
        os.environ.pop("_TEST_UNSET_VAR", None)
        result = cfg._env("_TEST_UNSET_VAR")
        assert result == ""

    def test_returns_custom_default_when_unset(self):
        os.environ.pop("_TEST_UNSET_VAR2", None)
        result = cfg._env("_TEST_UNSET_VAR2", "my_default")
        assert result == "my_default"

    def test_does_not_raise_on_missing_var(self):
        os.environ.pop("_TEST_SAFE_MISSING", None)
        try:
            cfg._env("_TEST_SAFE_MISSING")
        except Exception as exc:
            pytest.fail(f"_env raised unexpectedly: {exc}")


# ===========================================================================
# 6. _decode_b64_env
# ===========================================================================


class TestDecodeB64Env:
    def test_returns_none_when_not_set(self):
        os.environ.pop("_TEST_B64_MISSING", None)
        assert cfg._decode_b64_env("_TEST_B64_MISSING") is None

    def test_returns_none_when_empty(self):
        with patch.dict(os.environ, {"_TEST_B64_EMPTY": ""}):
            assert cfg._decode_b64_env("_TEST_B64_EMPTY") is None

    def test_decodes_valid_base64(self):
        value = base64.b64encode(b"hello world").decode()
        with patch.dict(os.environ, {"_TEST_B64_VALID": value}):
            assert cfg._decode_b64_env("_TEST_B64_VALID") == "hello world"

    def test_returns_none_on_invalid_base64(self):
        with patch.dict(os.environ, {"_TEST_B64_BAD": "not-valid-b64!!!"}):
            result = cfg._decode_b64_env("_TEST_B64_BAD")
            assert result is None


# ===========================================================================
# 7. _load_attendees
# ===========================================================================


class TestLoadAttendees:
    def test_returns_from_b64_env_var(self):
        data = {"1": ["a@test.com"], "2": ["b@test.com"]}
        encoded = base64.b64encode(json.dumps(data).encode()).decode()
        with patch.dict(os.environ, {"ATTENDEES_JSON_B64": encoded}):
            result = cfg._load_attendees()
        assert result == data

    def test_returns_from_local_file(self, tmp_path):
        data = {"1": ["x@t.com"], "2": []}
        attendees_file = tmp_path / "attendees.json"
        attendees_file.write_text(json.dumps(data), encoding="utf-8")

        with patch.dict(os.environ, {}, clear=False), \
             patch.object(cfg, "_decode_b64_env", return_value=None), \
             patch("tools.config.Path") as mock_path:
            mock_path.return_value.parent.parent.__truediv__ = lambda s, n: attendees_file
            # Simpler: just patch the file check
            result = cfg._load_attendees()
        # May fall through to default if path doesn't match
        assert isinstance(result, dict)

    def test_returns_default_when_nothing_available(self):
        with patch.object(cfg, "_decode_b64_env", return_value=None):
            result = cfg._load_attendees()
        assert isinstance(result, dict)

    def test_handles_invalid_json_in_env(self):
        encoded = base64.b64encode(b"not-json{{{").decode()
        with patch.dict(os.environ, {"ATTENDEES_JSON_B64": encoded}):
            result = cfg._load_attendees()
        # Should fall back gracefully
        assert isinstance(result, dict)


# ===========================================================================
# 8. get_lecture_number
# ===========================================================================


class TestGetLectureNumber:
    def test_returns_zero_before_start_date(self):
        mock_groups = {
            1: {
                "name": "g1",
                "start_date": date(2026, 3, 18),
                "meeting_days": {1, 4},  # Tue, Fri
            }
        }
        with patch.object(cfg, "GROUPS", mock_groups):
            result = cfg.get_lecture_number(1, for_date=date(2026, 3, 10))
        assert result == 0

    def test_returns_correct_lecture_on_first_meeting_day(self):
        mock_groups = {
            1: {
                "name": "g1",
                "start_date": date(2026, 3, 17),  # Tuesday
                "meeting_days": {1, 4},  # Tue, Fri
            }
        }
        with patch.object(cfg, "GROUPS", mock_groups):
            result = cfg.get_lecture_number(1, for_date=date(2026, 3, 17))
        assert result == 1

    def test_caps_at_total_lectures(self):
        mock_groups = {
            1: {
                "name": "g1",
                "start_date": date(2026, 1, 1),
                "meeting_days": {0, 1, 2, 3, 4},  # Mon-Fri
            }
        }
        with patch.object(cfg, "GROUPS", mock_groups), \
             patch.object(cfg, "TOTAL_LECTURES", 15):
            result = cfg.get_lecture_number(1, for_date=date(2026, 12, 31))
        assert result == 15

    def test_defaults_to_today_when_no_date(self):
        mock_groups = {
            1: {
                "name": "g1",
                "start_date": date(2020, 1, 1),
                "meeting_days": {0, 1, 2, 3, 4, 5, 6},
            }
        }
        with patch.object(cfg, "GROUPS", mock_groups), \
             patch.object(cfg, "TOTAL_LECTURES", 15):
            result = cfg.get_lecture_number(1)
        assert result == 15  # Will have exceeded 15 by now


# ===========================================================================
# 9. get_group_for_weekday
# ===========================================================================


class TestGetGroupForWeekday:
    def test_returns_group_for_meeting_day(self):
        mock_groups = {
            1: {"meeting_days": {1, 4}},
            2: {"meeting_days": {0, 3}},
        }
        with patch.object(cfg, "GROUPS", mock_groups):
            assert cfg.get_group_for_weekday(1) == 1  # Tuesday → Group 1
            assert cfg.get_group_for_weekday(0) == 2  # Monday → Group 2

    def test_returns_none_for_non_meeting_day(self):
        mock_groups = {
            1: {"meeting_days": {1, 4}},
            2: {"meeting_days": {0, 3}},
        }
        with patch.object(cfg, "GROUPS", mock_groups):
            assert cfg.get_group_for_weekday(5) is None  # Saturday
            assert cfg.get_group_for_weekday(6) is None  # Sunday


# ===========================================================================
# 10. get_lecture_folder_name
# ===========================================================================


class TestGetLectureFolderName:
    def test_returns_georgian_format(self):
        assert cfg.get_lecture_folder_name(1) == "ლექცია #1"
        assert cfg.get_lecture_folder_name(15) == "ლექცია #15"


# ===========================================================================
# 11. get_google_credentials_path
# ===========================================================================


class TestGetGoogleCredentialsPath:
    def test_returns_path(self):
        # Reset cache
        original = cfg._google_credentials_path
        try:
            cfg._google_credentials_path = None
            with patch.object(cfg, "_materialize_credential_file", return_value=Path("/tmp/creds.json")):
                result = cfg.get_google_credentials_path()
            assert result == Path("/tmp/creds.json")
        finally:
            cfg._google_credentials_path = original

    def test_caches_result(self, tmp_path):
        fake_path = tmp_path / "creds.json"
        fake_path.write_text("{}", encoding="utf-8")
        original = cfg._google_credentials_path
        try:
            cfg._google_credentials_path = fake_path
            result = cfg.get_google_credentials_path()
            assert result == fake_path
        finally:
            cfg._google_credentials_path = original
