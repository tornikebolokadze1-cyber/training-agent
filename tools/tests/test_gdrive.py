"""Unit tests for tools/gdrive_manager.py.

Covers:
- _get_token_path caching (Wave 1 fix: _token_path_cache)
- restrict_to_owner permission-failure alerting (Wave 1 fix)
- ensure_folder: creates when absent, returns existing ID when found
- create_google_doc: creates new doc with correct title and parent folder
- create_google_doc: updates in place when doc with same title exists
- upload_file: drives MediaFileUpload with resumable=True; returns file ID
- upload_file: raises FileNotFoundError for missing paths
- _get_credentials: returns valid creds when token file exists and is fresh
- _get_credentials: refreshes expired credentials
- create_folder: passes correct metadata to Drive API
- find_folder: returns None when no results
- find_folder: returns first file ID when results are present

Run with:
    pytest tools/tests/test_gdrive.py -v
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module stubs are set up in tools/tests/conftest.py.
# ---------------------------------------------------------------------------
import tools.integrations.gdrive_manager as gdrive

# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------

def _make_drive_service(list_response=None, create_response=None, update_response=None):
    """Build a minimal Drive service mock with chainable method stubs."""
    svc = MagicMock()

    # files().list().execute()
    list_exec = MagicMock(return_value=list_response or {"files": []})
    svc.files.return_value.list.return_value.execute = list_exec

    # files().create().execute()
    create_exec = MagicMock(return_value=create_response or {"id": "new-file-id"})
    svc.files.return_value.create.return_value.execute = create_exec

    # files().update().execute()
    update_exec = MagicMock(return_value=update_response or {"id": "existing-doc-id"})
    svc.files.return_value.update.return_value.execute = update_exec

    # Resumable upload: files().create() must also support next_chunk()
    svc.files.return_value.create.return_value.next_chunk.return_value = (
        None,
        {"id": "uploaded-file-id"},
    )

    return svc


# ===========================================================================
# 1. _get_token_path — caching behaviour
# ===========================================================================


class TestGetTokenPathCaching:
    """_token_path_cache must short-circuit on the second call."""

    def setup_method(self):
        # Reset module-level cache before every test so tests are independent.
        gdrive._token_path_cache = None

    def test_first_call_invokes_materialize(self, tmp_path):
        fake_token = tmp_path / "token.json"
        fake_token.write_text("{}", encoding="utf-8")

        with patch("tools.integrations.gdrive_manager._materialize_credential_file", return_value=fake_token) as mock_mat:
            result = gdrive._get_token_path()

        mock_mat.assert_called_once_with("GOOGLE_TOKEN_JSON_B64", gdrive.TOKEN_PATH)
        assert result == fake_token

    def test_second_call_uses_cache_not_materialize(self, tmp_path):
        fake_token = tmp_path / "token.json"
        fake_token.write_text("{}", encoding="utf-8")

        with patch("tools.integrations.gdrive_manager._materialize_credential_file", return_value=fake_token) as mock_mat:
            first = gdrive._get_token_path()
            second = gdrive._get_token_path()

        # _materialize_credential_file must be called exactly once despite two calls
        assert mock_mat.call_count == 1
        assert first == second


# ===========================================================================
# 2. restrict_to_owner — alert_operator on permission deletion failure
# ===========================================================================


class TestRestrictToOwnerAlerts:
    """When a permissions().delete() call raises, alert_operator must be called."""

    def _build_permissions_service(self, permissions, delete_side_effect=None):
        svc = MagicMock()
        svc.permissions.return_value.list.return_value.execute.return_value = {
            "permissions": permissions
        }
        if delete_side_effect is not None:
            svc.permissions.return_value.delete.return_value.execute.side_effect = delete_side_effect
        return svc

    def test_alert_operator_called_on_delete_failure(self):
        permissions = [
            {"id": "perm-1", "role": "writer", "type": "anyone"},
        ]
        svc = self._build_permissions_service(
            permissions, delete_side_effect=Exception("403 Forbidden")
        )

        mock_alert = MagicMock()

        with patch("tools.integrations.gdrive_manager.get_drive_service", return_value=svc):
            with patch("tools.integrations.whatsapp_sender.alert_operator", mock_alert):
                # Import happens lazily inside the except block; patch the module path
                with patch.dict(
                    sys.modules,
                    {"tools.integrations.whatsapp_sender": types.SimpleNamespace(alert_operator=mock_alert)},
                ):
                    gdrive.restrict_to_owner("file-id-123")

        mock_alert.assert_called_once()
        alert_msg = mock_alert.call_args[0][0]
        assert "file-id-123" in alert_msg

    def test_no_alert_when_delete_succeeds(self):
        permissions = [
            {"id": "perm-1", "role": "writer", "type": "user"},
        ]
        svc = self._build_permissions_service(permissions)
        mock_alert = MagicMock()

        with patch("tools.integrations.gdrive_manager.get_drive_service", return_value=svc):
            with patch.dict(
                sys.modules,
                {"tools.integrations.whatsapp_sender": types.SimpleNamespace(alert_operator=mock_alert)},
            ):
                gdrive.restrict_to_owner("file-id-ok")

        mock_alert.assert_not_called()

    def test_owner_permissions_are_not_deleted(self):
        permissions = [
            {"id": "owner-perm", "role": "owner", "type": "user"},
        ]
        svc = self._build_permissions_service(permissions)

        with patch("tools.integrations.gdrive_manager.get_drive_service", return_value=svc):
            gdrive.restrict_to_owner("any-id")

        svc.permissions.return_value.delete.assert_not_called()


# ===========================================================================
# 3. ensure_folder — find vs create logic
# ===========================================================================


class TestEnsureFolder:
    """ensure_folder must return the existing ID when found, create when absent."""

    def test_returns_existing_folder_id_without_create(self):
        svc = _make_drive_service(list_response={"files": [{"id": "existing-id"}]})

        result = gdrive.ensure_folder(svc, "ლექცია #1", "parent-id")

        assert result == "existing-id"
        svc.files.return_value.create.assert_not_called()

    def test_creates_folder_when_not_found(self):
        # list returns no results; create returns a new folder ID
        svc = _make_drive_service(
            list_response={"files": []},
            create_response={"id": "brand-new-folder"},
        )

        result = gdrive.ensure_folder(svc, "ლექცია #2", "parent-id")

        assert result == "brand-new-folder"
        svc.files.return_value.create.assert_called_once()

    def test_create_call_uses_folder_mime_type(self):
        svc = _make_drive_service(
            list_response={"files": []},
            create_response={"id": "folder-id"},
        )

        gdrive.ensure_folder(svc, "ლექცია #3", "parent-id")

        create_kwargs = svc.files.return_value.create.call_args
        body = create_kwargs[1].get("body") or create_kwargs[0][0]
        assert body["mimeType"] == "application/vnd.google-apps.folder"


# ===========================================================================
# 4. create_google_doc — new document creation path
# ===========================================================================


class TestCreateGoogleDoc:
    """create_google_doc must wire up metadata and media correctly."""

    def test_creates_new_doc_when_none_exists(self):
        svc = _make_drive_service(
            list_response={"files": []},
            create_response={"id": "doc-id-new", "webViewLink": "https://docs.google.com/..."},
        )

        with patch("tools.integrations.gdrive_manager.get_drive_service", return_value=svc):
            with patch("tools.integrations.gdrive_manager.MediaIoBaseUpload"):
                doc_id = gdrive.create_google_doc("ლექცია შეჯამება", "content text", "folder-id")

        assert doc_id == "doc-id-new"
        create_kwargs = svc.files.return_value.create.call_args[1]
        assert create_kwargs["body"]["name"] == "ლექცია შეჯამება"
        assert create_kwargs["body"]["mimeType"] == "application/vnd.google-apps.document"
        assert "folder-id" in create_kwargs["body"]["parents"]

    def test_updates_existing_doc_in_place(self):
        svc = _make_drive_service(
            list_response={"files": [{"id": "existing-doc-id"}]},
        )

        with patch("tools.integrations.gdrive_manager.get_drive_service", return_value=svc):
            with patch("tools.integrations.gdrive_manager.MediaIoBaseUpload"):
                doc_id = gdrive.create_google_doc("ლექცია შეჯამება", "updated content", "folder-id")

        assert doc_id == "existing-doc-id"
        # update must be called; create must NOT be called for file metadata
        svc.files.return_value.update.assert_called_once()
        # create should not be called when we are in the update branch
        svc.files.return_value.create.assert_not_called()


# ===========================================================================
# 5. upload_file — resumable upload mechanics
# ===========================================================================


class TestUploadFile:
    """upload_file must use MediaFileUpload with resumable=True and return file ID."""

    def test_returns_file_id_on_success(self, tmp_path):
        fake_file = tmp_path / "lecture.mp4"
        fake_file.write_bytes(b"\x00" * 100)

        svc = MagicMock()
        # next_chunk returns (None, response) on first call to signal completion
        svc.files.return_value.create.return_value.next_chunk.return_value = (
            None, {"id": "uploaded-id"}
        )

        with patch("tools.integrations.gdrive_manager.get_drive_service", return_value=svc):
            with patch("tools.integrations.gdrive_manager.MediaFileUpload"):
                file_id = gdrive.upload_file(fake_file, "folder-id")

        assert file_id == "uploaded-id"

    def test_media_file_upload_called_with_resumable_true(self, tmp_path):
        fake_file = tmp_path / "recording.mp4"
        fake_file.write_bytes(b"\x00" * 50)

        svc = MagicMock()
        svc.files.return_value.create.return_value.next_chunk.return_value = (
            None, {"id": "some-id"}
        )

        with patch("tools.integrations.gdrive_manager.get_drive_service", return_value=svc):
            with patch("tools.integrations.gdrive_manager.MediaFileUpload") as mock_mfu:
                gdrive.upload_file(fake_file, "folder-id", mime_type="video/mp4")

        mock_mfu.assert_called_once()
        _, kwargs = mock_mfu.call_args
        assert kwargs.get("resumable") is True

    def test_raises_file_not_found_for_missing_path(self, tmp_path):
        missing = tmp_path / "nonexistent.mp4"

        with pytest.raises(FileNotFoundError):
            gdrive.upload_file(missing, "folder-id")

    def test_mime_type_inferred_from_extension(self, tmp_path):
        fake_file = tmp_path / "notes.txt"
        fake_file.write_text("hello", encoding="utf-8")

        svc = MagicMock()
        svc.files.return_value.create.return_value.next_chunk.return_value = (
            None, {"id": "txt-id"}
        )

        with patch("tools.integrations.gdrive_manager.get_drive_service", return_value=svc):
            with patch("tools.integrations.gdrive_manager.MediaFileUpload") as mock_mfu:
                gdrive.upload_file(fake_file, "folder-id")

        _, kwargs = mock_mfu.call_args
        assert kwargs.get("mimetype") == "text/plain"


# ===========================================================================
# 6. _get_credentials — credential loading and refresh
# ===========================================================================


class TestGetCredentials:
    """_get_credentials must return valid creds; refresh when expired."""

    def test_returns_fresh_credentials_without_refresh(self, tmp_path):
        fake_token = tmp_path / "token.json"
        fake_token.write_text("{}", encoding="utf-8")

        mock_creds = MagicMock()
        mock_creds.valid = True

        with patch("tools.integrations.gdrive_manager._get_token_path", return_value=fake_token):
            with patch("tools.integrations.gdrive_manager.Credentials") as mock_cred_cls:
                mock_cred_cls.from_authorized_user_file.return_value = mock_creds
                result = gdrive._get_credentials()

        assert result is mock_creds
        mock_creds.refresh.assert_not_called()

    def test_refreshes_expired_credentials(self, tmp_path):
        fake_token = tmp_path / "token.json"
        fake_token.write_text("{}", encoding="utf-8")

        mock_creds = MagicMock()
        mock_creds.valid = False
        mock_creds.expired = True
        mock_creds.refresh_token = "some-refresh-token"

        with patch("tools.integrations.gdrive_manager._get_token_path", return_value=fake_token):
            with patch("tools.integrations.gdrive_manager.Credentials") as mock_cred_cls:
                mock_cred_cls.from_authorized_user_file.return_value = mock_creds
                with patch("tools.integrations.gdrive_manager.IS_RAILWAY", True):
                    result = gdrive._get_credentials()

        mock_creds.refresh.assert_called_once()
        assert result is mock_creds


# ===========================================================================
# 7. find_folder — direct Drive list query behaviour
# ===========================================================================


class TestFindFolder:
    """find_folder must return None when empty, first ID when results exist."""

    def test_returns_none_when_no_results(self):
        svc = _make_drive_service(list_response={"files": []})
        result = gdrive.find_folder(svc, "Missing Folder", "parent-id")
        assert result is None

    def test_returns_first_file_id_when_found(self):
        svc = _make_drive_service(
            list_response={"files": [{"id": "found-id"}, {"id": "second-id"}]}
        )
        result = gdrive.find_folder(svc, "ლექცია #5", "parent-id")
        assert result == "found-id"


# ===========================================================================
# 8. get_drive_service / get_docs_service — cached service building
# ===========================================================================


class TestGetDriveService:
    """get_drive_service must build once and cache on subsequent calls."""

    def setup_method(self):
        gdrive._drive_service_cache = None
        gdrive._token_path_cache = None

    def test_first_call_builds_service(self):
        mock_svc = MagicMock()
        with patch("tools.integrations.gdrive_manager._get_credentials", return_value=MagicMock()):
            with patch("tools.integrations.gdrive_manager.build", return_value=mock_svc) as mock_build:
                result = gdrive.get_drive_service()

        assert result is mock_svc
        mock_build.assert_called_once_with("drive", "v3", credentials=mock_build.call_args[1]["credentials"])

    def test_second_call_returns_cached(self):
        mock_svc = MagicMock()
        with patch("tools.integrations.gdrive_manager._get_credentials", return_value=MagicMock()):
            with patch("tools.integrations.gdrive_manager.build", return_value=mock_svc) as mock_build:
                first = gdrive.get_drive_service()
                second = gdrive.get_drive_service()

        assert first is second
        assert mock_build.call_count == 1

    def teardown_method(self):
        gdrive._drive_service_cache = None


class TestGetDocsService:
    """get_docs_service must build once and cache on subsequent calls."""

    def setup_method(self):
        gdrive._docs_service_cache = None
        gdrive._token_path_cache = None

    def test_first_call_builds_service(self):
        mock_svc = MagicMock()
        with patch("tools.integrations.gdrive_manager._get_credentials", return_value=MagicMock()):
            with patch("tools.integrations.gdrive_manager.build", return_value=mock_svc) as mock_build:
                result = gdrive.get_docs_service()

        assert result is mock_svc
        mock_build.assert_called_once_with("docs", "v1", credentials=mock_build.call_args[1]["credentials"])

    def test_second_call_returns_cached(self):
        mock_svc = MagicMock()
        with patch("tools.integrations.gdrive_manager._get_credentials", return_value=MagicMock()):
            with patch("tools.integrations.gdrive_manager.build", return_value=mock_svc) as mock_build:
                first = gdrive.get_docs_service()
                second = gdrive.get_docs_service()

        assert first is second
        assert mock_build.call_count == 1

    def teardown_method(self):
        gdrive._docs_service_cache = None


# ===========================================================================
# 9. create_all_lecture_folders — multi-group folder creation
# ===========================================================================


class TestCreateAllLectureFolders:
    """create_all_lecture_folders must iterate groups and create folders."""

    def setup_method(self):
        gdrive._drive_service_cache = None
        gdrive._token_path_cache = None

    def test_creates_folders_for_all_groups(self):
        svc = _make_drive_service(
            list_response={"files": []},
            create_response={"id": "new-folder-id"},
        )
        fake_groups = {
            1: {"drive_folder_id": "parent-1"},
            2: {"drive_folder_id": "parent-2"},
        }

        with patch("tools.integrations.gdrive_manager.get_drive_service", return_value=svc):
            with patch("tools.integrations.gdrive_manager.GROUPS", fake_groups):
                with patch("tools.integrations.gdrive_manager.TOTAL_LECTURES", 2):
                    with patch("tools.integrations.gdrive_manager.LECTURE_FOLDER_IDS", {}):
                        result = gdrive.create_all_lecture_folders()

        assert 1 in result
        assert 2 in result
        assert len(result[1]) == 2
        assert len(result[2]) == 2

    def test_skips_groups_with_empty_drive_folder_id(self):
        svc = _make_drive_service(
            list_response={"files": []},
            create_response={"id": "new-folder-id"},
        )
        fake_groups = {
            1: {"drive_folder_id": "parent-1"},
            2: {"drive_folder_id": ""},  # empty — should be skipped
        }

        with patch("tools.integrations.gdrive_manager.get_drive_service", return_value=svc):
            with patch("tools.integrations.gdrive_manager.GROUPS", fake_groups):
                with patch("tools.integrations.gdrive_manager.TOTAL_LECTURES", 2):
                    with patch("tools.integrations.gdrive_manager.LECTURE_FOLDER_IDS", {}):
                        result = gdrive.create_all_lecture_folders()

        assert 1 in result
        assert 2 not in result


# ===========================================================================
# 10. download_file — chunked download
# ===========================================================================


class TestDownloadFile:
    """download_file must use MediaIoBaseDownload and return the path."""

    def setup_method(self):
        gdrive._drive_service_cache = None

    def test_downloads_file_and_returns_path(self, tmp_path):
        dest = tmp_path / "subdir" / "file.mp4"
        svc = MagicMock()

        # MediaIoBaseDownload mock: first call returns (status, False), second returns (None, True)
        mock_downloader = MagicMock()
        mock_downloader.next_chunk.side_effect = [
            (MagicMock(progress=lambda: 0.5), False),
            (None, True),
        ]

        with patch("tools.integrations.gdrive_manager.get_drive_service", return_value=svc):
            with patch("tools.integrations.gdrive_manager.MediaIoBaseDownload", return_value=mock_downloader):
                # We need the file to exist for stat() at the end
                with patch("builtins.open", MagicMock()):
                    # Patch Path.stat to avoid real file check
                    with patch.object(type(dest), "stat", return_value=MagicMock(st_size=1024 * 1024)):
                        # Patch parent.mkdir
                        with patch.object(type(dest.parent), "mkdir"):
                            result = gdrive.download_file("file-id-abc", dest)

        assert result == dest

    def test_creates_parent_directories(self, tmp_path):
        dest = tmp_path / "deep" / "nested" / "file.mp4"
        svc = MagicMock()

        mock_downloader = MagicMock()
        mock_downloader.next_chunk.return_value = (None, True)

        with patch("tools.integrations.gdrive_manager.get_drive_service", return_value=svc):
            with patch("tools.integrations.gdrive_manager.MediaIoBaseDownload", return_value=mock_downloader):
                with patch("builtins.open", MagicMock()):
                    with patch.object(type(dest), "stat", return_value=MagicMock(st_size=100)):
                        with patch.object(type(dest.parent), "mkdir") as mock_mkdir:
                            gdrive.download_file("file-id", dest)

        mock_mkdir.assert_called_once_with(parents=True, exist_ok=True)


# ===========================================================================
# 11. list_files_in_folder
# ===========================================================================


class TestListFilesInFolder:
    """list_files_in_folder must return file dicts or empty list."""

    def setup_method(self):
        gdrive._drive_service_cache = None

    def test_returns_file_list(self):
        files = [{"id": "f1", "name": "a.mp4"}, {"id": "f2", "name": "b.txt"}]
        svc = _make_drive_service(list_response={"files": files})

        with patch("tools.integrations.gdrive_manager.get_drive_service", return_value=svc):
            result = gdrive.list_files_in_folder("folder-id")

        assert result == files

    def test_returns_empty_list_when_no_files(self):
        svc = _make_drive_service(list_response={"files": []})

        with patch("tools.integrations.gdrive_manager.get_drive_service", return_value=svc):
            result = gdrive.list_files_in_folder("empty-folder")

        assert result == []


# ===========================================================================
# 12. ensure_private_folder
# ===========================================================================


class TestEnsurePrivateFolder:
    """ensure_private_folder must call ensure_folder then restrict_to_owner."""

    def test_calls_ensure_folder_and_restrict_to_owner(self):
        with patch("tools.integrations.gdrive_manager.ensure_folder", return_value="folder-123") as mock_ef:
            with patch("tools.integrations.gdrive_manager.restrict_to_owner") as mock_rto:
                svc = MagicMock()
                result = gdrive.ensure_private_folder(svc, "Private", "parent-id")

        assert result == "folder-123"
        mock_ef.assert_called_once_with(svc, "Private", "parent-id")
        mock_rto.assert_called_once_with("folder-123")


# ===========================================================================
# 13. _get_credentials edge cases
# ===========================================================================


class TestGetCredentialsEdgeCases:
    """Cover Railway RuntimeError and no-DISPLAY RuntimeError branches."""

    def setup_method(self):
        gdrive._token_path_cache = None

    def test_railway_raises_runtime_error_without_refresh_token(self, tmp_path):
        fake_token = tmp_path / "token.json"
        fake_token.write_text("{}", encoding="utf-8")

        mock_creds = MagicMock()
        mock_creds.valid = False
        mock_creds.expired = False
        mock_creds.refresh_token = None

        with patch("tools.integrations.gdrive_manager._get_token_path", return_value=fake_token):
            with patch("tools.integrations.gdrive_manager.Credentials") as mock_cls:
                mock_cls.from_authorized_user_file.return_value = mock_creds
                with patch("tools.integrations.gdrive_manager.IS_RAILWAY", True):
                    with pytest.raises(RuntimeError, match="refresh_token"):
                        gdrive._get_credentials()

    def test_no_display_no_browser_raises_runtime_error(self, tmp_path):
        fake_token = tmp_path / "token.json"
        fake_token.write_text("{}", encoding="utf-8")

        mock_creds = MagicMock()
        mock_creds.valid = False
        mock_creds.expired = False
        mock_creds.refresh_token = None

        with patch("tools.integrations.gdrive_manager._get_token_path", return_value=fake_token):
            with patch("tools.integrations.gdrive_manager.Credentials") as mock_cls:
                mock_cls.from_authorized_user_file.return_value = mock_creds
                with patch("tools.integrations.gdrive_manager.IS_RAILWAY", False):
                    with patch.dict("os.environ", {}, clear=True):
                        with pytest.raises(RuntimeError, match="OAuth token expired"):
                            gdrive._get_credentials()

    def test_local_oauth_flow_runs_when_display_available(self, tmp_path):
        fake_token = tmp_path / "token.json"
        fake_token.write_text("{}", encoding="utf-8")

        mock_creds_invalid = MagicMock()
        mock_creds_invalid.valid = False
        mock_creds_invalid.expired = False
        mock_creds_invalid.refresh_token = None

        mock_new_creds = MagicMock()
        mock_new_creds.to_json.return_value = '{"token": "new"}'

        mock_flow = MagicMock()
        mock_flow.run_local_server.return_value = mock_new_creds

        with patch("tools.integrations.gdrive_manager._get_token_path", return_value=fake_token):
            with patch("tools.integrations.gdrive_manager.Credentials") as mock_cls:
                mock_cls.from_authorized_user_file.return_value = mock_creds_invalid
                with patch("tools.integrations.gdrive_manager.IS_RAILWAY", False):
                    with patch.dict("os.environ", {"DISPLAY": ":0"}):
                        with patch("tools.integrations.gdrive_manager.get_google_credentials_path", return_value=tmp_path / "creds.json"):
                            with patch("tools.integrations.gdrive_manager.InstalledAppFlow") as mock_iaf:
                                mock_iaf.from_client_secrets_file.return_value = mock_flow
                                # Patch TOKEN_PATH to use tmp_path so write succeeds
                                with patch("tools.integrations.gdrive_manager.TOKEN_PATH", fake_token):
                                    result = gdrive._get_credentials()

        assert result is mock_new_creds
        mock_flow.run_local_server.assert_called_once_with(port=0)


# ===========================================================================
# 14. upload_file retry logic
# ===========================================================================


class TestUploadFileRetry:
    """upload_file must retry transient errors with backoff."""

    def test_retries_on_transient_error_then_succeeds(self, tmp_path):
        fake_file = tmp_path / "video.mp4"
        fake_file.write_bytes(b"\x00" * 100)

        svc = MagicMock()
        # First call raises, second succeeds
        svc.files.return_value.create.return_value.next_chunk.side_effect = [
            Exception("Connection reset"),
            (None, {"id": "retry-success-id"}),
        ]

        with patch("tools.integrations.gdrive_manager.get_drive_service", return_value=svc):
            with patch("tools.integrations.gdrive_manager.MediaFileUpload"):
                with patch("time.sleep"):  # skip actual delay
                    file_id = gdrive.upload_file(fake_file, "folder-id")

        assert file_id == "retry-success-id"

    def test_raises_after_max_retries(self, tmp_path):
        fake_file = tmp_path / "video.mp4"
        fake_file.write_bytes(b"\x00" * 100)

        svc = MagicMock()
        # Always raises
        svc.files.return_value.create.return_value.next_chunk.side_effect = Exception("Persistent failure")

        with patch("tools.integrations.gdrive_manager.get_drive_service", return_value=svc):
            with patch("tools.integrations.gdrive_manager.MediaFileUpload"):
                with patch("time.sleep"):
                    with pytest.raises(Exception, match="Persistent failure"):
                        gdrive.upload_file(fake_file, "folder-id")


# ===========================================================================
# 15. create_folder — direct API call
# ===========================================================================


class TestCreateFolder:
    """create_folder must pass correct metadata to the Drive API."""

    def test_passes_correct_metadata(self):
        svc = _make_drive_service(create_response={"id": "created-folder-id"})

        result = gdrive.create_folder(svc, "ლექცია #7", "parent-xyz")

        assert result == "created-folder-id"
        create_kwargs = svc.files.return_value.create.call_args[1]
        body = create_kwargs["body"]
        assert body["name"] == "ლექცია #7"
        assert body["mimeType"] == "application/vnd.google-apps.folder"
        assert body["parents"] == ["parent-xyz"]

    def test_returns_folder_id(self):
        svc = _make_drive_service(create_response={"id": "abc-123"})
        result = gdrive.create_folder(svc, "Test", "parent")
        assert result == "abc-123"
