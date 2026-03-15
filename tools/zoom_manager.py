"""Zoom meeting management using Server-to-Server OAuth.

Handles token acquisition, meeting creation, recording retrieval, and
recording file downloads for the Training Agent system.
"""

from __future__ import annotations

import base64
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from tools.config import (
    GROUPS,
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
TBILISI_TZ = ZoneInfo("Asia/Tbilisi")
MEETING_DURATION_MINUTES = 120

# Retry configuration
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2.0  # seconds

# In-memory token cache: {"access_token": str, "expires_at": float}
_token_cache: dict[str, Any] = {}


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
        "join_url": data["join_url"],
        "start_url": data["start_url"],
        "topic": data["topic"],
        "start_time": data["start_time"],
    }


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


def download_recording(
    download_url: str,
    access_token: str,
    dest_path: Path | str,
) -> Path:
    """Download a single Zoom recording file to local storage.

    Uses streaming to avoid loading the entire file into memory, making it
    suitable for large video files.

    Args:
        download_url: The ``download_url`` value from a recording file entry.
            The access token is appended as a query parameter as required by
            the Zoom API.
        access_token: A valid Bearer token (obtain via :func:`get_access_token`).
        dest_path: Destination file path. Parent directories are created if
            they do not exist. If a string is supplied it is converted to
            :class:`pathlib.Path`.

    Returns:
        The resolved :class:`pathlib.Path` of the downloaded file.

    Raises:
        ZoomDownloadError: If the download request fails or the server returns
            a non-2xx status.
    """
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Zoom requires the access token appended as a query parameter for downloads
    separator = "&" if "?" in download_url else "?"
    authenticated_url = f"{download_url}{separator}access_token={access_token}"

    logger.info("Downloading recording to %s …", dest)

    try:
        with httpx.Client(timeout=httpx.Timeout(600.0, connect=30.0), follow_redirects=True) as client:
            with client.stream("GET", authenticated_url) as response:
                if response.status_code not in (200, 206):
                    raise ZoomDownloadError(
                        f"Download failed: HTTP {response.status_code} for {download_url}"
                    )

                total_bytes = int(response.headers.get("content-length", 0))
                downloaded = 0

                with dest.open("wb") as fh:
                    for chunk in response.iter_bytes(chunk_size=1024 * 1024):  # 1 MB
                        fh.write(chunk)
                        downloaded += len(chunk)

                        if total_bytes:
                            pct = downloaded / total_bytes * 100
                            logger.debug("Download progress: %.1f%%", pct)

    except httpx.RequestError as exc:
        raise ZoomDownloadError(
            f"Network error while downloading {download_url}: {exc}"
        ) from exc

    logger.info(
        "Recording saved to %s (%.2f MB).", dest, dest.stat().st_size / 1024 / 1024
    )
    return dest


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

    downloaded_paths: list[Path] = []

    for rec in recording_files:
        if rec.get("status") != "completed":
            logger.info(
                "Skipping file %s — status is '%s'.",
                rec.get("id"),
                rec.get("status"),
            )
            continue

        # Refresh token before each download — large files may take longer
        # than the token's 1-hour lifetime
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
