"""Unit tests for tools/railway_setup.py.

Covers:
- FILES_TO_ENCODE structure and paths
- main(): skips missing files, encodes existing files, prints env vars

Run with:
    pytest tools/tests/test_railway_setup.py -v
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Module stubs are set up in tools/tests/conftest.py.
# ---------------------------------------------------------------------------
import tools.core.railway_setup as rs

# ===========================================================================
# 1. FILES_TO_ENCODE structure
# ===========================================================================


class TestFilesToEncode:
    def test_all_values_are_paths(self):
        for key, path in rs.FILES_TO_ENCODE.items():
            assert isinstance(path, Path), f"{key} should be a Path"

    def test_expected_keys_present(self):
        assert "GOOGLE_CREDENTIALS_JSON_B64" in rs.FILES_TO_ENCODE
        assert "GOOGLE_TOKEN_JSON_B64" in rs.FILES_TO_ENCODE

    def test_project_root_is_parent_of_tools(self):
        assert rs.PROJECT_ROOT == Path(__file__).parent.parent.parent


# ===========================================================================
# 2. main() — output behavior
# ===========================================================================


class TestMain:
    def test_skips_missing_files(self, tmp_path, capsys):
        mock_files = {
            "TEST_KEY": tmp_path / "nonexistent.json",
        }

        with patch.object(rs, "FILES_TO_ENCODE", mock_files), \
             patch("dotenv.dotenv_values", return_value={}):
            rs.main()

        output = capsys.readouterr().out
        assert "SKIP" in output

    def test_encodes_existing_files(self, tmp_path, capsys):
        fake_file = tmp_path / "creds.json"
        fake_file.write_text('{"key": "value"}', encoding="utf-8")

        mock_files = {
            "TEST_CRED_B64": fake_file,
        }

        with patch.object(rs, "FILES_TO_ENCODE", mock_files), \
             patch("dotenv.dotenv_values", return_value={}):
            rs.main()

        output = capsys.readouterr().out
        assert "TEST_CRED_B64" in output
        assert "SKIP" not in output
        # Should contain base64 output
        assert "eyJ" in output  # base64 of '{"k...'

    def test_prints_reminder_for_env_vars(self, tmp_path, capsys):
        with patch.object(rs, "FILES_TO_ENCODE", {}), \
             patch("dotenv.dotenv_values", return_value={
                 "ZOOM_CLIENT_ID": "some-id",
                 "WEBHOOK_SECRET": "",
             }):
            rs.main()

        output = capsys.readouterr().out
        assert "REMINDER" in output
        assert "ZOOM_CLIENT_ID" in output

    def test_prints_railway_environment_reminder(self, tmp_path, capsys):
        with patch.object(rs, "FILES_TO_ENCODE", {}), \
             patch("dotenv.dotenv_values", return_value={}):
            rs.main()

        output = capsys.readouterr().out
        assert "RAILWAY_ENVIRONMENT" in output
