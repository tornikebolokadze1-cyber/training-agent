"""Zoom meeting management using Server-to-Server OAuth.

Handles token acquisition, meeting creation, recording retrieval, and
recording file downloads for the Training Agent system.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from tools.core.api_resilience import resilient_api_call
from tools.core.config import (
    GROUPS,
    TBILISI_TZ,
    TMP_DIR,
    ZOOM_ACCOUNT_ID,
    ZOOM_CLIENT_ID,
    ZOOM_CLIENT_SECRET,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ZOOM_OAUTH_URL = "https://zoom.us/oauth/token"
ZOOM_API_BASE = "https://api.zoom.us/v2"
MEETING_DURATION_MINUTES = 180  # 3 hours — lectures often run 30 min over

# Retry configuration
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2.0  # seconds

# Download configuration — tuned for 2-4 GB lecture recordings
DOWNLOAD_TIMEOUT_SECONDS = 1800  # 30 minutes
PROGRESS_LOG_INTERVAL_BYTES = 100 * 1024 * 1024  # 100 MB
MAX_DOWNLOAD_RETRIES = 5
DISK_SPACE_SAFETY_MARGIN = 1.2  # require 1.2x file size free

# In-memory token cache: {"access_token": str, "expires_at": float}
_token_cache: dict[str, Any] = {}
_token_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ZoomAuthError(Exception):
    """Raised when Zoom OAuth authentication fails."""


class ZoomAPIError(Exception):
    """Raised when a Zoom API call returns a non-2xx response."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        super().__init__(f"Zoom API error {status_code}: {message}")


class ZoomDownloadError(Exception):
    """Raised when a recording file download fails."""


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def get_access_token() -> str:
    """Obtain a valid Zoom Server-to-Server OAuth access token.

    Tokens are cached in memory until 60 seconds before expiry to avoid
    redundant requests.

    Returns:
        A valid Bearer access token string.

    Raises:
        ZoomAuthError: If credentials are missing or the token request fails.
    """
    if not all([ZOOM_ACCOUNT_ID, ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET]):
        raise ZoomAuthError(
            "Missing Zoom credentials. Ensure ZOOM_ACCOUNT_ID, ZOOM_CLIENT_ID, "
            "and ZOOM_CLIENT_SECRET are set in .env."
        )

    # Return cached token if still valid (with 60-second safety margin)
    with _token_lock:
        if _token_cache.get("access_token") and time.time() < _token_cache.get(
            "expires_at", 0.0
        ) - 60:
            logger.debug("Using cached Zoom access token.")
            return _token_cache["access_token"]  # type: ignore[return-value]

    credentials = base64.b64encode(
        f"{ZOOM_CLIENT_ID}:{ZOOM_CLIENT_SECRET}".encode()
    ).decode()

    headers = {
        "Authorization": f"Basic {credentials}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {
        "grant_type": "account_credentials",
        "account_id": ZOOM_ACCOUNT_ID,
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with httpx.Client(timeout=30) as client:
                response = client.post(ZOOM_OAUTH_URL, headers=headers, data=data)

            if response.status_code == 200:
                payload = response.json()
                access_token: str = payload["access_token"]
                expires_in: int = payload.get("expires_in", 3600)

                with _token_lock:
                    _token_cache["access_token"] = access_token
                    _token_cache["expires_at"] = time.time() + expires_in

                logger.info("Zoom access token acquired (expires in %ds).", expires_in)
                return access_token

            logger.warning(
                "Token request failed (attempt %d/%d): HTTP %d — %s",
                attempt,
                MAX_RETRIES,
                response.status_code,
                response.text,
            )
        except httpx.RequestError as exc:
            logger.warning(
                "Token request network error (attempt %d/%d): %s",
                attempt,
                MAX_RETRIES,
                exc,
            )

        if attempt < MAX_RETRIES:
            sleep_seconds = RETRY_BACKOFF_BASE**attempt
            logger.debug("Retrying token request in %.1fs…", sleep_seconds)
            time.sleep(sleep_seconds)

    raise ZoomAuthError(
        f"Failed to obtain Zoom access token after {MAX_RETRIES} attempts."
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _zoom_request(
    method: str,
    endpoint: str,
    *,
    json: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute an authenticated Zoom API request with retry logic.

    Args:
        method: HTTP method (GET, POST, PATCH, DELETE).
        endpoint: API path relative to ZOOM_API_BASE (e.g. "/users/me/meetings").
        json: Optional request body as a dict.
        params: Optional query parameters.

    Returns:
        Parsed JSON response body.

    Raises:
        ZoomAuthError: If a fresh token cannot be obtained.
        ZoomAPIError: If the API returns a non-2xx status after all retries.
    """
    url = f"{ZOOM_API_BASE}{endpoint}"

    for attempt in range(1, MAX_RETRIES + 1):
        token = get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        try:
            with httpx.Client(timeout=60) as client:
                response = client.request(
                    method.upper(),
                    url,
                    headers=headers,
                    json=json,
                    params=params,
                )

            if response.status_code in (200, 201):
                return response.json()  # type: ignore[return-value]

            if response.status_code == 204:
                return {}

            # 401 means the cached token was invalidated — clear cache and retry
            if response.status_code == 401:
                logger.warning("Received 401; clearing token cache and retrying.")
                with _token_lock:
                    _token_cache.clear()
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF_BASE**attempt)
                    continue

            error_body = response.text
            logger.error(
                "Zoom API %s %s returned HTTP %d (attempt %d/%d): %s",
                method.upper(),
                endpoint,
                response.status_code,
                attempt,
                MAX_RETRIES,
                error_body,
            )

            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_BASE**attempt)
                continue

            raise ZoomAPIError(response.status_code, error_body)

        except httpx.RequestError as exc:
            logger.warning(
                "Network error on %s %s (attempt %d/%d): %s",
                method.upper(),
                endpoint,
                attempt,
                MAX_RETRIES,
                exc,
            )
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_BASE**attempt)
            else:
                raise ZoomAPIError(0, str(exc)) from exc

    # Unreachable, but satisfies the type checker
    raise ZoomAPIError(0, "Exhausted retries without a result.")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_meeting(
    group_number: int,
    lecture_number: int,
    start_time: datetime,
) -> dict[str, Any]:
    """Create a Zoom meeting for a training group lecture.

    The meeting topic follows the Georgian convention used across all
    Training Agent workflows. The start time is normalised to Tbilisi
    time (GMT+4) if it carries no timezone info.

    Args:
        group_number: Training group index (1 or 2).
        lecture_number: Lecture index within the group (1–15).
        start_time: Scheduled start time. May be naive (treated as Tbilisi
            local time) or timezone-aware.

    Returns:
        A dict containing at minimum:
            - ``id`` (int): Zoom meeting ID.
            - ``join_url`` (str): Participant join link.
            - ``start_url`` (str): Host start link.
            - ``topic`` (str): Meeting topic string.
            - ``start_time`` (str): ISO-8601 start time as stored by Zoom.

    Raises:
        ZoomAuthError: If credentials are invalid.
        ZoomAPIError: If meeting creation fails after retries.
    """
    # Normalise timezone: treat naive datetimes as Tbilisi local time
    if start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=TBILISI_TZ)

    # Zoom expects UTC ISO-8601 with a 'Z' suffix
    start_time_utc = start_time.astimezone(ZoneInfo("UTC"))
    start_time_str = start_time_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    topic = f"AI კურსი — ჯგუფი #{group_number}, ლექცია #{lecture_number}"

    # Build meeting invitees list from group's attendee emails
    group = GROUPS.get(group_number, {})
    attendee_emails = group.get("attendee_emails", [])
    meeting_invitees = [{"email": email} for email in attendee_emails]

    payload: dict[str, Any] = {
        "topic": topic,
        "type": 2,  # Scheduled meeting
        "start_time": start_time_str,
        "duration": MEETING_DURATION_MINUTES,
        "timezone": "Asia/Tbilisi",
        "settings": {
            "auto_recording": "cloud",
            "mute_upon_entry": True,
            "waiting_room": False,
            "join_before_host": False,
            "host_video": True,
            "participant_video": False,
            "approval_type": 2,  # No registration required
            "meeting_invitees": meeting_invitees,  # Zoom sends email invitations
        },
    }

    logger.info(
        "Creating Zoom meeting: group=%d, lecture=%d, start=%s",
        group_number,
        lecture_number,
        start_time_str,
    )

    data = _zoom_request("POST", "/users/me/meetings", json=payload)

    logger.info(
        "Meeting created: id=%s, join_url=%s",
        data.get("id"),
        data.get("join_url"),
    )

    return {
        "id": data["id"],
        "uuid": data.get("uuid", ""),  # instance-specific UUID for recordings API
        "join_url": data["join_url"],
        "start_url": data["start_url"],
        "topic": data["topic"],
        "start_time": data["start_time"],
    }


@resilient_api_call(service="zoom", operation="get_meeting_recordings", max_attempts=3)
def get_meeting_recordings(meeting_id: str | int) -> dict[str, Any]:
    """Retrieve cloud recording files for a completed Zoom meeting.

    Args:
        meeting_id: The Zoom meeting ID (numeric or string form).

    Returns:
        The raw Zoom recordings response dict, which includes:
            - ``recording_files`` (list): Each item has ``download_url``,
              ``file_type``, ``file_size``, ``status``, and ``recording_type``.
            - ``share_url`` (str): Public share link for the recording.
            - ``total_size`` (int): Total bytes across all files.

    Raises:
        ZoomAuthError: If credentials are invalid.
        ZoomAPIError: If the API returns an error (e.g. 404 if recording is
            not yet available or the meeting ID is wrong).
    """
    logger.info("Fetching recordings for meeting_id=%s", meeting_id)
    data = _zoom_request("GET", f"/meetings/{meeting_id}/recordings")
    file_count = len(data.get("recording_files", []))
    logger.info("Found %d recording file(s) for meeting_id=%s.", file_count, meeting_id)
    return data  # type: ignore[return-value]


def list_user_recordings(
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict[str, Any]]:
    """List cloud recordings for the authenticated user.

    Uses GET /v2/users/me/recordings (requires cloud_recording:read:list_user_recordings:admin).

    Args:
        from_date: Start date in YYYY-MM-DD format. Defaults to today.
        to_date: End date in YYYY-MM-DD format. Defaults to today.

    Returns:
        List of meeting dicts, each containing ``uuid``, ``id``, ``topic``,
        ``start_time``, and ``recording_files``.
    """
    from datetime import date as _date

    if from_date is None:
        from_date = _date.today().isoformat()
    if to_date is None:
        to_date = _date.today().isoformat()

    params = {"from": from_date, "to": to_date, "page_size": 30}
    logger.info("Listing user recordings from %s to %s", from_date, to_date)
    data = _zoom_request("GET", "/users/me/recordings", params=params)
    meetings = data.get("meetings", [])
    logger.info("Found %d recording meeting(s) in date range.", len(meetings))
    return meetings


# ---------------------------------------------------------------------------
# Download helpers: checksum, disk space, parallel segments
# ---------------------------------------------------------------------------


def compute_file_checksum(file_path: Path | str) -> str:
    """Compute SHA-256 checksum of a file (streamed, memory-safe)."""
    path = Path(file_path)
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _checksum_path_for(file_path: Path) -> Path:
    """Return the ``.sha256`` sidecar path for a given file."""
    return file_path.with_name(file_path.name + ".sha256")


def save_checksum(file_path: Path | str, checksum: str) -> Path:
    """Write a checksum sidecar file in standard ``sha256sum`` format."""
    path = Path(file_path)
    checksum_path = _checksum_path_for(path)
    checksum_path.write_text(f"{checksum}  {path.name}\n")
    return checksum_path


def load_checksum(file_path: Path | str) -> str | None:
    """Read checksum from the ``.sha256`` sidecar, or None if missing."""
    path = Path(file_path)
    checksum_path = _checksum_path_for(path)
    if not checksum_path.exists():
        return None
    content = checksum_path.read_text().strip()
    if not content:
        return None
    return content.split()[0]


def verify_download_integrity(file_path: Path | str) -> bool:
    """Verify a file against its stored ``.sha256`` checksum."""
    stored = load_checksum(file_path)
    if stored is None:
        return False
    return compute_file_checksum(file_path) == stored


def check_disk_space(dest_path: Path | str, required_bytes: int) -> None:
    """Ensure enough free disk space for ``required_bytes`` with safety margin.

    Raises:
        ZoomDownloadError: If free space is below required * DISK_SPACE_SAFETY_MARGIN.
    """
    dest = Path(dest_path)
    check_dir = dest.parent if dest.parent.exists() else Path.cwd()
    usage = shutil.disk_usage(check_dir)
    needed = int(required_bytes * DISK_SPACE_SAFETY_MARGIN)
    if usage.free < needed:
        raise ZoomDownloadError(
            f"Insufficient disk space: need {needed} bytes "
            f"(including {DISK_SPACE_SAFETY_MARGIN}x safety margin), "
            f"only {usage.free} bytes free at {check_dir}"
        )


def download_recording(
    download_url: str,
    access_token: str,
    dest_path: Path | str,
    resume: bool = True,
) -> Path:
    """Download a single Zoom recording file with retry, resume, and checksum.

    Features:
      - Disk space pre-check (1.2x file size).
      - HTTP Range resume support for partial files.
      - Retry up to ``MAX_DOWNLOAD_RETRIES`` on network errors.
      - Content-Length completeness validation.
      - SHA-256 checksum sidecar written on success.

    Raises:
        ZoomDownloadError: On HTTP error, incomplete download, exhausted
            retries, or insufficient disk space.
    """
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    separator = "&" if "?" in download_url else "?"
    authenticated_url = f"{download_url}{separator}access_token={access_token}"

    logger.info("Downloading recording to %s …", dest)

    last_exc: Exception | None = None

    for attempt in range(1, MAX_DOWNLOAD_RETRIES + 1):
        # Determine resume offset
        resume_from = dest.stat().st_size if (resume and dest.exists()) else 0
        headers: dict[str, str] = {}
        if resume_from > 0:
            headers["Range"] = f"bytes={resume_from}-"

        # Pre-flight disk space check — best-effort using HEAD
        try:
            with httpx.Client(
                timeout=httpx.Timeout(DOWNLOAD_TIMEOUT_SECONDS, connect=30.0),
                follow_redirects=True,
            ) as probe_client:
                head_resp = probe_client.head(authenticated_url)
                head_size = int(head_resp.headers.get("content-length", 0) or 0)
                if head_size > 0:
                    check_disk_space(dest, head_size - resume_from)
        except ZoomDownloadError:
            raise
        except Exception:  # noqa: BLE001 — HEAD is best-effort
            pass

        try:
            with httpx.Client(
                timeout=httpx.Timeout(DOWNLOAD_TIMEOUT_SECONDS, connect=30.0),
                follow_redirects=True,
            ) as client:
                with client.stream("GET", authenticated_url, headers=headers) as response:
                    if response.status_code not in (200, 206):
                        raise ZoomDownloadError(
                            f"Download failed: HTTP {response.status_code} for {download_url}"
                        )

                    total_bytes = int(response.headers.get("content-length", 0) or 0)

                    # Disk space check based on actual stream content-length
                    if total_bytes > 0:
                        check_disk_space(dest, total_bytes)

                    mode = "ab" if (resume_from > 0 and response.status_code == 206) else "wb"
                    downloaded = resume_from if mode == "ab" else 0
                    next_log_at = PROGRESS_LOG_INTERVAL_BYTES

                    with dest.open(mode) as fh:
                        for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                            fh.write(chunk)
                            downloaded += len(chunk)
                            if downloaded >= next_log_at:
                                logger.info(
                                    "Download progress: %.0f MB",
                                    downloaded / (1024 * 1024),
                                )
                                next_log_at += PROGRESS_LOG_INTERVAL_BYTES

            # Completeness check
            expected_header = response.headers.get("content-length")
            actual_size = dest.stat().st_size
            if expected_header:
                expected = int(expected_header)
                effective_expected = expected + (resume_from if mode == "ab" else 0)
                if actual_size < effective_expected:
                    dest.unlink(missing_ok=True)
                    raise ZoomDownloadError(
                        f"Incomplete download: got {actual_size} bytes, "
                        f"expected {effective_expected} bytes"
                    )

            # Success — write checksum sidecar
            checksum = compute_file_checksum(dest)
            save_checksum(dest, checksum)

            logger.info(
                "Download complete: %s (%.1f MB)",
                dest.name, actual_size / (1024 * 1024),
            )
            return dest

        except ZoomDownloadError:
            raise
        except (httpx.TransportError, AttributeError) as exc:
            last_exc = exc
            logger.warning(
                "Download attempt %d/%d failed: %s",
                attempt, MAX_DOWNLOAD_RETRIES, exc,
            )
            if attempt < MAX_DOWNLOAD_RETRIES:
                time.sleep(RETRY_BACKOFF_BASE * attempt)
            continue

    # All retries exhausted
    dest.unlink(missing_ok=True)
    raise ZoomDownloadError(
        f"Network error after {MAX_DOWNLOAD_RETRIES} attempts for {download_url}: {last_exc}"
    )


def download_segments_parallel(
    segments: list[dict[str, Any]],
    dest_dir: Path | str,
    max_workers: int = 3,
) -> list[Path]:
    """Download multiple recording segments in parallel.

    Args:
        segments: List of recording file dicts with ``id``, ``file_type``,
            ``recording_type``, ``download_url``, and optional ``file_size``.
        dest_dir: Destination directory for downloaded files.
        max_workers: Maximum concurrent downloads.

    Returns:
        List of downloaded file paths, in the same order as ``segments``.

    Raises:
        ZoomDownloadError: If any segment fails to download.
    """
    if not segments:
        return []

    dest_dir_path = Path(dest_dir)
    dest_dir_path.mkdir(parents=True, exist_ok=True)

    # Aggregate disk space check
    total_size = sum(int(seg.get("file_size", 0) or 0) for seg in segments)
    if total_size > 0:
        check_disk_space(dest_dir_path / ".probe", total_size)

    token = get_access_token()

    def _segment_path(seg: dict[str, Any]) -> Path:
        file_type = str(seg.get("file_type", "MP4")).lower()
        rec_type = seg.get("recording_type", "unknown")
        return dest_dir_path / f"{rec_type}_{seg['id']}.{file_type}"

    results: list[Path | None] = [None] * len(segments)

    def _download_one(idx: int, seg: dict[str, Any]) -> tuple[int, Path]:
        path = download_recording(
            seg["download_url"], token, _segment_path(seg),
        )
        return idx, path

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(_download_one, i, seg)
            for i, seg in enumerate(segments)
        ]
        for future in as_completed(futures):
            idx, path = future.result()  # re-raises ZoomDownloadError
            results[idx] = path

    return [p for p in results if p is not None]


# ---------------------------------------------------------------------------
# Convenience: download all recording files for a meeting
# ---------------------------------------------------------------------------


def download_all_recordings(
    meeting_id: str | int,
    dest_dir: Path | str | None = None,
) -> list[Path]:
    """Download every cloud recording file for a meeting.

    Skips files whose ``status`` is not ``"completed"``.

    Args:
        meeting_id: Zoom meeting ID.
        dest_dir: Directory to save files. Defaults to ``TMP_DIR / str(meeting_id)``.

    Returns:
        List of :class:`pathlib.Path` objects for successfully downloaded files.

    Raises:
        ZoomAuthError: If authentication fails.
        ZoomAPIError: If recording metadata cannot be fetched.
        ZoomDownloadError: If any individual file download fails.
    """
    if dest_dir is None:
        dest_dir = TMP_DIR / str(meeting_id)

    recordings_data = get_meeting_recordings(meeting_id)
    recording_files: list[dict[str, Any]] = recordings_data.get("recording_files", [])

    completed = [r for r in recording_files if r.get("status") == "completed"]
    for rec in recording_files:
        if rec.get("status") != "completed":
            logger.info(
                "Skipping file %s — status is '%s'.",
                rec.get("id"), rec.get("status"),
            )

    downloaded_paths: list[Path]

    if len(completed) > 1:
        downloaded_paths = download_segments_parallel(completed, Path(dest_dir))
    else:
        downloaded_paths = []
        for rec in completed:
            token = get_access_token()
            file_type: str = rec.get("file_type", "MP4").upper()
            rec_type: str = rec.get("recording_type", "unknown")
            file_name = f"{rec_type}_{rec['id']}.{file_type.lower()}"
            dest = Path(dest_dir) / file_name
            path = download_recording(
                download_url=rec["download_url"],
                access_token=token,
                dest_path=dest,
            )
            downloaded_paths.append(path)

    logger.info(
        "Downloaded %d/%d recording file(s) for meeting_id=%s.",
        len(downloaded_paths),
        len(recording_files),
        meeting_id,
    )
    return downloaded_paths
