"""Google Drive operations: folder creation, file upload, Google Doc creation.

Upload features:
- Deduplication: skips upload if file with same name+size already exists
- Pagination: list_files_in_folder fetches ALL files via nextPageToken
- Auth discrimination: 401 triggers token refresh then retry; 403 fails immediately
- Post-upload verification: GET check after upload completes
- Progress logging: large files (>100MB) log every 25%
- Old version cleanup: trash_old_recordings removes stale retry artifacts
"""

from __future__ import annotations

import io
import logging
import re
import time
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload, MediaIoBaseUpload

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

# Size thresholds
LARGE_FILE_PROGRESS_THRESHOLD = 100 * 1024 * 1024  # 100 MB — log progress every 25%
SIZE_TOLERANCE = 0.01  # 1% tolerance for dedup size comparison


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

# Match lecture recording filenames produced by the pipeline.
# Examples:
#   group1_lecture8_20260408_065110.mp4
#   group2_lecture6_20260406_000500_seg0.mp4
# Captures (group, lecture) so dedup can match across timestamp variants.
_RECORDING_NAME_RE = re.compile(
    r"^group(?P<group>\d+)_lecture(?P<lecture>\d+)_.*\.mp4$",
    re.IGNORECASE,
)


def _trash_pattern_matches(
    service,
    folder_id: str,
    group: int,
    lecture: int,
    exclude_name: str | None = None,
) -> int:
    """Trash any lecture recording files in folder matching this group+lecture.

    Used by upload_file() to clean up older retry artifacts before uploading
    a fresh copy. The exclude_name argument prevents trashing the file we're
    about to upload (in case the same name already exists, exact-name dedup
    handles it earlier in the upload path).

    Returns the number of files trashed.
    """
    pattern = re.compile(
        rf"^group{group}_lecture{lecture}_.*\.mp4$",
        re.IGNORECASE,
    )
    listing = (
        service.files()
        .list(
            q=f"'{folder_id}' in parents and trashed=false and mimeType contains 'video/'",
            fields="files(id, name, mimeType)",
            pageSize=100,
        )
        .execute()
        .get("files", [])
    )
    trashed = 0
    for f in listing:
        name = f.get("name", "")
        if not pattern.match(name):
            continue
        if exclude_name and name == exclude_name:
            continue
        try:
            service.files().update(fileId=f["id"], body={"trashed": True}).execute()
            trashed += 1
        except Exception as exc:
            logger.warning("Failed to trash old recording %s: %s", name, exc)
    return trashed


def _find_existing_file(
    service,
    filename: str,
    folder_id: str,
    local_size: int,
) -> str | None:
    """Check if a file with the same name and similar size exists in folder.

    Returns the existing file ID if found and size matches within tolerance,
    or None if no suitable duplicate exists.
    """
    safe_name = filename.replace("\\", "\\\\").replace("'", "\\'")
    query = (
        f"name = '{safe_name}' "
        f"and '{folder_id}' in parents "
        f"and trashed = false"
    )
    existing = (
        service.files()
        .list(q=query, fields="files(id, name, size)", pageSize=1)
        .execute()
        .get("files", [])
    )
    if not existing:
        return None

    remote_file = existing[0]
    remote_size = int(remote_file.get("size", 0))
    file_id = remote_file["id"]

    # Size comparison with tolerance (within 1%)
    if local_size > 0 and remote_size > 0:
        ratio = abs(remote_size - local_size) / local_size
        if ratio <= SIZE_TOLERANCE:
            logger.info(
                "Skipping duplicate upload: %s already exists in folder "
                "(ID: %s, remote=%d bytes, local=%d bytes, diff=%.2f%%)",
                filename, file_id, remote_size, local_size, ratio * 100,
            )
            return file_id
        else:
            logger.info(
                "File '%s' exists but size differs (remote=%d, local=%d, diff=%.1f%%) "
                "— will re-upload",
                filename, remote_size, local_size, ratio * 100,
            )
            return None

    # If we can't determine size, skip by name only (legacy dedup behavior)
    logger.info(
        "Skipping duplicate upload: %s already exists in folder (ID: %s) — size unknown",
        filename, file_id,
    )
    return file_id


def _refresh_credentials_and_rebuild_service() -> None:
    """Force-refresh OAuth2 credentials and rebuild the Drive service cache.

    Called when a 401 (token expired) is encountered during upload.
    """
    global _drive_service_cache
    logger.info("Refreshing Google Drive credentials after 401 error")
    _drive_service_cache = None  # Clear cached service
    creds = _get_credentials()
    _drive_service_cache = build("drive", "v3", credentials=creds)
    logger.info("Drive service rebuilt with fresh credentials")


def _verify_upload(service, file_id: str, expected_size: int) -> bool:
    """Verify an uploaded file exists and has the correct size.

    Returns True if verification passes, False otherwise.
    """
    try:
        file_meta = (
            service.files()
            .get(fileId=file_id, fields="id, name, size")
            .execute()
        )
        remote_size = int(file_meta.get("size", 0))
        if expected_size > 0 and remote_size > 0:
            ratio = abs(remote_size - expected_size) / expected_size
            if ratio > SIZE_TOLERANCE:
                logger.warning(
                    "Post-upload verification FAILED: size mismatch "
                    "(remote=%d, expected=%d, diff=%.1f%%)",
                    remote_size, expected_size, ratio * 100,
                )
                return False
        logger.info(
            "Post-upload verification passed: %s (ID: %s, size=%d)",
            file_meta.get("name", "?"), file_id, remote_size,
        )
        return True
    except Exception as e:
        logger.warning("Post-upload verification failed: %s", e)
        return False


def _format_size(size_bytes: int) -> str:
    """Format bytes as human-readable string."""
    if size_bytes >= 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 ** 3):.1f}GB"
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 ** 2):.0f}MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.0f}KB"
    return f"{size_bytes}B"


def trash_old_recordings(folder_id: str, group: int, lecture: int) -> int:
    """Trash old recording versions matching group{N}_lecture{N}_*.mp4 pattern.

    Returns the number of files trashed.
    """
    service = get_drive_service()
    pattern = re.compile(
        rf"group{group}_lecture{lecture}_.*\.mp4",
        re.IGNORECASE,
    )

    all_files = list_files_in_folder(folder_id)
    trashed = 0
    for f in all_files:
        name = f.get("name", "")
        mime = f.get("mimeType", "")
        if mime.startswith("video/") and pattern.match(name):
            try:
                service.files().update(
                    fileId=f["id"],
                    body={"trashed": True},
                ).execute()
                logger.info("Trashed old recording: %s (ID: %s)", name, f["id"])
                trashed += 1
            except Exception as e:
                logger.warning("Failed to trash old recording %s: %s", name, e)

    if trashed:
        logger.info(
            "Trashed %d old recording(s) for group%d_lecture%d in folder %s",
            trashed, group, lecture, folder_id,
        )
    return trashed


def upload_file(
    file_path: str | Path,
    folder_id: str,
    mime_type: str | None = None,
) -> str:
    """Upload a file to Google Drive using resumable upload.

    Features:
    - Deduplication: skips if file with same name AND size (within 1%) exists
    - Auth discrimination: 401 refreshes token then retries; 403 fails immediately
    - Progress logging: files >100MB log every 25%
    - Post-upload verification: confirms file exists with correct size

    Returns the file ID.
    """
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    local_size = file_path.stat().st_size

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

    # Dedup: check if file with same name AND similar size already exists
    existing_id = _find_existing_file(service, file_path.name, folder_id, local_size)
    if existing_id:
        return existing_id

    # Pattern-based dedup for lecture recordings: trash older runs of the same
    # (group, lecture) before uploading the new one. Different pipeline runs
    # produce different timestamps in the filename, so the exact-name dedup
    # above misses them. Without this guard, retries leave duplicate videos
    # in the lecture folder.
    _recording_match = _RECORDING_NAME_RE.match(file_path.name)
    if _recording_match:
        try:
            _trashed = _trash_pattern_matches(
                service,
                folder_id,
                group=int(_recording_match.group("group")),
                lecture=int(_recording_match.group("lecture")),
                exclude_name=file_path.name,
            )
            if _trashed:
                logger.info(
                    "Pattern dedup: trashed %d older recording(s) for "
                    "group%s lecture%s before uploading %s",
                    _trashed, _recording_match.group("group"),
                    _recording_match.group("lecture"), file_path.name,
                )
        except Exception as exc:
            # Dedup is best-effort — never block the actual upload
            logger.warning("Pattern dedup skipped (non-fatal): %s", exc)

    metadata = {"name": file_path.name, "parents": [folder_id]}
    media = MediaFileUpload(
        str(file_path),
        mimetype=mime_type,
        chunksize=CHUNK_SIZE,
        resumable=True,
    )

    request = service.files().create(body=metadata, media_body=media, fields="id")

    is_large = local_size >= LARGE_FILE_PROGRESS_THRESHOLD
    last_logged_quarter = -1  # Track which 25% milestone was last logged

    response = None
    max_retries = 5
    auth_retried = False  # Only refresh credentials once per upload
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                progress = int(status.progress() * 100)
                if is_large:
                    quarter = progress // 25
                    if quarter > last_logged_quarter:
                        last_logged_quarter = quarter
                        uploaded = int(status.progress() * local_size)
                        logger.info(
                            "Uploading %s: %d%% (%s/%s)",
                            file_path.name,
                            progress,
                            _format_size(uploaded),
                            _format_size(local_size),
                        )
                else:
                    logger.info("Upload progress: %d%%", progress)
        except HttpError as e:
            status_code = e.resp.status if hasattr(e, "resp") else 0

            # 401: Token expired — refresh credentials, rebuild service, retry once
            if status_code == 401 and not auth_retried:
                auth_retried = True
                logger.warning("Got 401 during upload — refreshing credentials")
                _refresh_credentials_and_rebuild_service()
                service = get_drive_service()
                media = MediaFileUpload(
                    str(file_path),
                    mimetype=mime_type,
                    chunksize=CHUNK_SIZE,
                    resumable=True,
                )
                request = service.files().create(
                    body=metadata, media_body=media, fields="id"
                )
                response = None
                last_logged_quarter = -1
                continue

            # 403: Permission denied — fail immediately, no retry
            if status_code == 403:
                logger.error(
                    "Permission denied (HTTP 403) uploading '%s': %s",
                    file_path.name, e,
                )
                raise

            # 404: Folder not found — fail immediately
            if status_code == 404:
                logger.error("Folder not found (HTTP 404): %s", e)
                raise

            # Retryable: 500, 502, 503, 429, etc.
            max_retries -= 1
            if max_retries <= 0:
                logger.error("Upload failed after retries: %s", e)
                raise
            delay = 2 ** (5 - max_retries)
            logger.warning(
                "Upload chunk failed (%d retries left): %s — retrying in %ds",
                max_retries, e, delay,
            )
            time.sleep(delay)
        except Exception as e:
            max_retries -= 1
            if max_retries <= 0:
                logger.error("Upload failed after retries: %s", e)
                raise
            delay = 2 ** (5 - max_retries)
            logger.warning(
                "Upload chunk failed (%d retries left): %s — retrying in %ds",
                max_retries, e, delay,
            )
            time.sleep(delay)

    file_id = response["id"]
    logger.info("Uploaded '%s' to Drive (ID: %s)", file_path.name, file_id)

    # Post-upload verification
    if not _verify_upload(service, file_id, local_size):
        logger.warning(
            "Post-upload verification failed for '%s' — retrying upload",
            file_path.name,
        )
        try:
            service.files().update(
                fileId=file_id, body={"trashed": True}
            ).execute()
        except Exception as trash_err:
            logger.warning("Failed to trash bad upload %s: %s", file_id, trash_err)

        media2 = MediaFileUpload(
            str(file_path),
            mimetype=mime_type,
            chunksize=CHUNK_SIZE,
            resumable=True,
        )
        request2 = service.files().create(
            body=metadata, media_body=media2, fields="id"
        )
        response2 = None
        while response2 is None:
            _status, response2 = request2.next_chunk()
        file_id = response2["id"]
        logger.info(
            "Re-uploaded '%s' after verification failure (ID: %s)",
            file_path.name, file_id,
        )

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
    """List ALL files in a Google Drive folder using full pagination.

    Fetches every page (100 files per page) via nextPageToken to ensure
    no files are missed even in folders with >100 items.
    """
    service = get_drive_service()
    all_files: list[dict] = []
    page_token: str | None = None
    page_count = 0

    while True:
        response = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id, name, mimeType, modifiedTime, size)",
            pageSize=100,
            pageToken=page_token,
        ).execute()

        batch = response.get("files", [])
        all_files.extend(batch)
        page_count += 1
        page_token = response.get("nextPageToken")
        if not page_token:
            break

    if page_count > 1:
        logger.info(
            "Listed %d files in folder %s across %d pages",
            len(all_files), folder_id, page_count,
        )

    return all_files


# ---------------------------------------------------------------------------
# Google Doc Creation
# ---------------------------------------------------------------------------

def create_google_doc(title: str, content: str, folder_id: str) -> str:
    """Create or update a Google Doc with the given content in the specified folder.

    If a document with the same title already exists in the folder, it is
    updated in place (idempotent). Otherwise a new document is created.

    Retries up to 3 times on transient network errors (SSL timeout,
    connection reset, etc.) with exponential backoff.

    Returns the document ID (also the Drive file ID).
    """
    from tools.core.retry import retry_with_backoff

    return retry_with_backoff(
        _create_google_doc_inner,
        title,
        content,
        folder_id,
        max_retries=3,
        backoff_base=3.0,
        retryable_exceptions=(OSError, ConnectionError, TimeoutError, HttpError),
        operation_name=f"Drive doc upload '{title}'",
    )


def _create_google_doc_inner(title: str, content: str, folder_id: str) -> str:
    """Inner implementation of create_google_doc (no retry logic)."""
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
