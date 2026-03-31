"""Google Drive operations: folder creation, file upload, Google Doc creation."""

from __future__ import annotations

import io
import logging
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload, MediaIoBaseUpload

from tools.core.api_resilience import resilient_api_call
from tools.core.config import (
    GROUPS,
    IS_RAILWAY,
    LECTURE_FOLDER_IDS,
    PROJECT_ROOT,
    TOTAL_LECTURES,
    _materialize_credential_file,
    get_google_credentials_path,
    get_lecture_folder_name,
)

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/docs",
]

TOKEN_PATH = PROJECT_ROOT / "token.json"
CHUNK_SIZE = 50 * 1024 * 1024  # 50 MB chunks for resumable upload


_token_path_cache: Path | None = None


def _get_token_path() -> Path:
    """Resolve the Drive token.json file path (cached after first call)."""
    global _token_path_cache
    if _token_path_cache is not None and _token_path_cache.exists():
        return _token_path_cache
    _token_path_cache = _materialize_credential_file("GOOGLE_TOKEN_JSON_B64", TOKEN_PATH)
    return _token_path_cache


def _get_credentials() -> Credentials:
    """Load or refresh Google OAuth2 credentials.

    On Railway (no browser, no persistent filesystem):
    - Loads credentials from GOOGLE_TOKEN_JSON_B64 env var
    - Refreshes access_token in memory using the refresh_token
    - Does NOT write back to disk (the refresh_token is long-lived)
    - If the refresh_token itself is revoked, raises RuntimeError
      so the operator can re-authorize locally and update the env var
    """
    creds = None
    token_path = _get_token_path()

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            # On Railway, only log that we refreshed — do not write to disk
            # because the filesystem is ephemeral. The refresh_token in the
            # env var remains valid.
            if IS_RAILWAY:
                logger.info(
                    "Google Drive credentials refreshed in memory (Railway mode)"
                )
            else:
                TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
                TOKEN_PATH.chmod(0o600)
        else:
            if IS_RAILWAY:
                raise RuntimeError(
                    "Google OAuth refresh_token is invalid or missing. "
                    "Re-authorize locally: python -m tools.integrations.gdrive_manager, "
                    "then update GOOGLE_TOKEN_JSON_B64 in Railway with: "
                    "base64 -i token.json | tr -d '\\n'"
                )
            import os
            if not os.environ.get("DISPLAY") and not os.environ.get("BROWSER"):
                raise RuntimeError(
                    "OAuth token expired and cannot be refreshed. "
                    "Run the application locally with a browser to re-authorize: "
                    "python -m tools.integrations.gdrive_manager"
                )
            credentials_path = get_google_credentials_path()
            flow = InstalledAppFlow.from_client_secrets_file(
                str(credentials_path), SCOPES
            )
            creds = flow.run_local_server(port=0)
            TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
            TOKEN_PATH.chmod(0o600)

    return creds


_drive_service_cache = None
_docs_service_cache = None


def get_drive_service():
    """Build and return the Google Drive API service (cached)."""
    global _drive_service_cache
    if _drive_service_cache is None:
        _drive_service_cache = build("drive", "v3", credentials=_get_credentials())
    return _drive_service_cache


def get_docs_service():
    """Build and return the Google Docs API service (cached)."""
    global _docs_service_cache
    if _docs_service_cache is None:
        _docs_service_cache = build("docs", "v1", credentials=_get_credentials())
    return _docs_service_cache


# ---------------------------------------------------------------------------
# Folder Operations
# ---------------------------------------------------------------------------

def create_folder(service, name: str, parent_id: str) -> str:
    """Create a folder in Google Drive and return its ID."""
    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = service.files().create(body=metadata, fields="id").execute()
    folder_id = folder["id"]
    logger.info("Created folder '%s' (ID: %s) in parent %s", name, folder_id, parent_id)
    return folder_id


def find_folder(service, name: str, parent_id: str) -> str | None:
    """Find an existing folder by name inside a parent. Returns ID or None."""
    safe_name = name.replace("\\", "\\\\").replace("'", "\\'")
    query = (
        f"name = '{safe_name}' "
        f"and '{parent_id}' in parents "
        f"and mimeType = 'application/vnd.google-apps.folder' "
        f"and trashed = false"
    )
    results = service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])
    return files[0]["id"] if files else None


def ensure_folder(service, name: str, parent_id: str) -> str:
    """Find or create a folder. Returns its ID."""
    existing = find_folder(service, name, parent_id)
    if existing:
        logger.info("Folder '%s' already exists (ID: %s)", name, existing)
        return existing
    return create_folder(service, name, parent_id)


def create_all_lecture_folders() -> dict[int, dict[int, str]]:
    """Create ლექცია #1 through ლექცია #15 for both groups.

    Returns a nested dict: {group_number: {lecture_number: folder_id}}.
    Skips folders that already exist.
    """
    service = get_drive_service()
    result: dict[int, dict[int, str]] = {}

    for group_num, group in GROUPS.items():
        parent_id = group["drive_folder_id"]
        if not parent_id:
            logger.warning("No Drive folder ID configured for Group %d — skipping", group_num)
            continue

        result[group_num] = {}
        for lecture_num in range(1, TOTAL_LECTURES + 1):
            folder_name = get_lecture_folder_name(lecture_num)
            folder_id = ensure_folder(service, folder_name, parent_id)
            result[group_num][lecture_num] = folder_id

        logger.info(
            "Group %d: %d lecture folders ready", group_num, len(result[group_num])
        )

    # Update the global config
    LECTURE_FOLDER_IDS.update(result)
    return result


# ---------------------------------------------------------------------------
# File Upload
# ---------------------------------------------------------------------------

@resilient_api_call(service="drive", operation="upload_file", max_attempts=5)
def upload_file(
    file_path: str | Path,
    folder_id: str,
    mime_type: str | None = None,
) -> str:
    """Upload a file to Google Drive using resumable upload.

    Returns the file ID.
    """
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    if mime_type is None:
        suffix = file_path.suffix.lower()
        mime_map = {
            ".mp4": "video/mp4",
            ".m4a": "audio/mp4",
            ".txt": "text/plain",
            ".pdf": "application/pdf",
        }
        mime_type = mime_map.get(suffix, "application/octet-stream")

    service = get_drive_service()

    # Dedup: check if file with same name already exists in folder
    safe_name = file_path.name.replace("\\", "\\\\").replace("'", "\\'")
    query = (
        f"name = '{safe_name}' "
        f"and '{folder_id}' in parents "
        f"and trashed = false"
    )
    existing = (
        service.files()
        .list(q=query, fields="files(id)", pageSize=1)
        .execute()
        .get("files", [])
    )
    if existing:
        file_id = existing[0]["id"]
        logger.info(
            "File '%s' already exists in folder (ID: %s) — skipping upload",
            file_path.name, file_id,
        )
        return file_id

    metadata = {"name": file_path.name, "parents": [folder_id]}
    media = MediaFileUpload(
        str(file_path),
        mimetype=mime_type,
        chunksize=CHUNK_SIZE,
        resumable=True,
    )

    request = service.files().create(body=metadata, media_body=media, fields="id")

    response = None
    max_retries = 5
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                progress = int(status.progress() * 100)
                logger.info("Upload progress: %d%%", progress)
        except HttpError as e:
            if e.resp.status in (401, 403, 404):
                # Non-retryable: auth failure, quota exhausted, folder not found
                logger.error("Non-retryable Drive error (HTTP %d): %s", e.resp.status, e)
                raise
            # Retryable: 500, 502, 503, 429, etc.
            max_retries -= 1
            if max_retries <= 0:
                logger.error("Upload failed after retries: %s", e)
                raise
            import time
            delay = 2 ** (5 - max_retries)
            logger.warning(
                "Upload chunk failed (%d retries left): %s — retrying in %ds",
                max_retries, e, delay,
            )
            time.sleep(delay)
        except Exception as e:
            # Retry transient errors with exponential backoff
            max_retries -= 1
            if max_retries <= 0:
                logger.error("Upload failed after retries: %s", e)
                raise
            import time
            delay = 2 ** (5 - max_retries)
            logger.warning(
                "Upload chunk failed (%d retries left): %s — retrying in %ds",
                max_retries, e, delay,
            )
            time.sleep(delay)

    file_id = response["id"]
    logger.info("Uploaded '%s' to Drive (ID: %s)", file_path.name, file_id)
    return file_id


# ---------------------------------------------------------------------------
# File Download
# ---------------------------------------------------------------------------

def download_file(
    file_id: str,
    destination: str | Path,
) -> Path:
    """Download a file from Google Drive to a local path.

    Uses chunked download with progress reporting for large files.

    Args:
        file_id: Google Drive file ID.
        destination: Local path to save the file.

    Returns:
        Path to the downloaded file.
    """
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    service = get_drive_service()
    request = service.files().get_media(fileId=file_id)

    with open(destination, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request, chunksize=CHUNK_SIZE)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                progress = int(status.progress() * 100)
                logger.info("Download progress: %d%%", progress)

    file_size_mb = destination.stat().st_size / (1024 * 1024)
    logger.info("Downloaded '%s' (%.1f MB) to %s", file_id, file_size_mb, destination)
    return destination


def list_files_in_folder(folder_id: str) -> list[dict]:
    """List all files in a Google Drive folder with pagination."""
    service = get_drive_service()
    all_files: list[dict] = []
    page_token = None

    while True:
        response = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id, name, mimeType, modifiedTime)",
            pageSize=100,
            pageToken=page_token,
        ).execute()

        all_files.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    return all_files


# ---------------------------------------------------------------------------
# Google Doc Creation
# ---------------------------------------------------------------------------

@resilient_api_call(service="drive", operation="create_google_doc", max_attempts=3)
def create_google_doc(title: str, content: str, folder_id: str) -> str:
    """Create or update a Google Doc with the given content in the specified folder.

    If a document with the same title already exists in the folder, it is
    updated in place (idempotent). Otherwise a new document is created.

    Returns the document ID (also the Drive file ID).
    """
    service = get_drive_service()

    # Check for existing doc with same title (idempotency)
    safe_title = title.replace("\\", "\\\\").replace("'", "\\'")
    query = (
        f"name = '{safe_title}' "
        f"and '{folder_id}' in parents "
        f"and mimeType = 'application/vnd.google-apps.document' "
        f"and trashed = false"
    )
    existing = service.files().list(q=query, fields="files(id)").execute().get("files", [])
    if existing:
        doc_id = existing[0]["id"]
        logger.info("Updating existing Google Doc '%s' (ID: %s)", title, doc_id)
        media = MediaIoBaseUpload(
            io.BytesIO(content.encode("utf-8")),
            mimetype="text/plain",
            resumable=False,
        )
        service.files().update(
            fileId=doc_id,
            media_body=media,
        ).execute()
        return doc_id

    # Create the doc as a file in Drive
    metadata = {
        "name": title,
        "mimeType": "application/vnd.google-apps.document",
        "parents": [folder_id],
    }

    # Upload plain text content and convert to Google Doc
    media = MediaIoBaseUpload(
        io.BytesIO(content.encode("utf-8")),
        mimetype="text/plain",
        resumable=False,
    )

    doc = service.files().create(
        body=metadata,
        media_body=media,
        fields="id, webViewLink",
    ).execute()

    doc_id = doc["id"]
    link = doc.get("webViewLink", "")
    logger.info("Created Google Doc '%s' (ID: %s, Link: %s)", title, doc_id, link)
    return doc_id


# ---------------------------------------------------------------------------
# Permission Management
# ---------------------------------------------------------------------------

def restrict_to_owner(file_or_folder_id: str) -> None:
    """Remove all non-owner permissions from a file or folder.

    After this call, only the OAuth account owner can access the resource.
    Useful for private analysis docs that shouldn't be visible to group members.
    """
    service = get_drive_service()
    permissions = service.permissions().list(
        fileId=file_or_folder_id,
        fields="permissions(id, role, type)",
    ).execute().get("permissions", [])

    for perm in permissions:
        if perm["role"] != "owner":
            try:
                service.permissions().delete(
                    fileId=file_or_folder_id,
                    permissionId=perm["id"],
                ).execute()
                logger.info(
                    "Removed permission %s (role=%s, type=%s) from %s",
                    perm["id"], perm["role"], perm.get("type"), file_or_folder_id,
                )
            except Exception as e:
                logger.warning("Failed to remove permission %s: %s", perm["id"], e)
                try:
                    from tools.integrations.whatsapp_sender import alert_operator
                    alert_operator(
                        f"Drive permission removal FAILED for {file_or_folder_id}: {e}"
                    )
                except Exception as alert_err:
                    logger.error("alert_operator also failed: %s", alert_err)


def ensure_private_folder(service, name: str, parent_id: str) -> str:
    """Find or create a folder, then restrict access to owner only.

    Returns the folder ID.
    """
    folder_id = ensure_folder(service, name, parent_id)
    restrict_to_owner(folder_id)
    return folder_id


# ---------------------------------------------------------------------------
# CLI entrypoint for one-time folder setup
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    print("Creating lecture folders for all groups...")
    folders = create_all_lecture_folders()
    print("\nFolder IDs (add these to config if needed):")
    for gnum, lectures in folders.items():
        print(f"\nGroup {gnum}:")
        for lnum, fid in sorted(lectures.items()):
            print(f"  ლექცია #{lnum}: {fid}")
