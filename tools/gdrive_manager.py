"""Google Drive operations: folder creation, file upload, Google Doc creation."""

from __future__ import annotations

import io
import logging
from pathlib import Path

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload

from tools.config import (
    GOOGLE_CREDENTIALS_PATH,
    GROUPS,
    LECTURE_FOLDER_IDS,
    PROJECT_ROOT,
    TOTAL_LECTURES,
    get_lecture_folder_name,
)

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/docs",
]

TOKEN_PATH = PROJECT_ROOT / "token.json"
CHUNK_SIZE = 50 * 1024 * 1024  # 50 MB chunks for resumable upload


def _get_credentials() -> Credentials:
    """Load or refresh Google OAuth2 credentials."""
    creds = None

    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                GOOGLE_CREDENTIALS_PATH, SCOPES
            )
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json())

    return creds


def get_drive_service():
    """Build and return the Google Drive API service."""
    return build("drive", "v3", credentials=_get_credentials())


def get_docs_service():
    """Build and return the Google Docs API service."""
    return build("docs", "v1", credentials=_get_credentials())


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
    query = (
        f"name = '{name}' "
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
    """Create ლექცია #2 through ლექცია #15 for both groups.

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
    metadata = {"name": file_path.name, "parents": [folder_id]}
    media = MediaFileUpload(
        str(file_path),
        mimetype=mime_type,
        chunksize=CHUNK_SIZE,
        resumable=True,
    )

    request = service.files().create(body=metadata, media_body=media, fields="id")

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            progress = int(status.progress() * 100)
            logger.info("Upload progress: %d%%", progress)

    file_id = response["id"]
    logger.info("Uploaded '%s' to Drive (ID: %s)", file_path.name, file_id)
    return file_id


# ---------------------------------------------------------------------------
# Google Doc Creation
# ---------------------------------------------------------------------------

def create_google_doc(title: str, content: str, folder_id: str) -> str:
    """Create a Google Doc with the given content in the specified folder.

    Returns the document ID (also the Drive file ID).
    """
    service = get_drive_service()

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
