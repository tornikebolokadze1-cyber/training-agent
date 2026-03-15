"""Gmail API email sender for Training Agent meeting reminders.

Sends Georgian-language meeting reminder emails to training group participants
using the Gmail API via Google OAuth2.
"""

import base64
import logging
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

from google.auth.exceptions import TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from tools.config import GOOGLE_CREDENTIALS_PATH, GROUPS, PROJECT_ROOT

logger = logging.getLogger(__name__)

# Gmail API scope required for sending email
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

# Separate token file for Gmail (Drive uses token.json with Drive scopes)
TOKEN_PATH = PROJECT_ROOT / "token_gmail.json"
CREDENTIALS_PATH = Path(GOOGLE_CREDENTIALS_PATH)

# Retry configuration
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 2  # seconds; delay = base ** attempt


# ---------------------------------------------------------------------------
# OAuth2 helpers
# ---------------------------------------------------------------------------


def _load_credentials() -> Credentials:
    """Load OAuth2 credentials, refreshing or re-authorising as needed.

    Returns:
        Valid :class:`google.oauth2.credentials.Credentials` instance.

    Raises:
        FileNotFoundError: If credentials.json is missing.
        RuntimeError: If the OAuth2 flow cannot be completed.
    """
    if not CREDENTIALS_PATH.exists():
        raise FileNotFoundError(
            f"credentials.json not found at {CREDENTIALS_PATH}. "
            "Download it from Google Cloud Console → APIs & Services → Credentials."
        )

    creds: Credentials | None = None

    # Attempt to load an existing token
    if TOKEN_PATH.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), GMAIL_SCOPES)
            logger.debug("Loaded existing token from %s", TOKEN_PATH)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not load token.json (%s) — will re-authorise.", exc)
            creds = None

    # Refresh or re-authorise
    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        logger.info("Refreshing expired Gmail credentials.")
        try:
            creds.refresh(Request())
        except TransportError as exc:
            logger.error("Failed to refresh credentials: %s", exc)
            raise RuntimeError("Gmail credentials refresh failed.") from exc
    else:
        logger.info("Running OAuth2 authorisation flow.")
        flow = InstalledAppFlow.from_client_secrets_file(
            str(CREDENTIALS_PATH), GMAIL_SCOPES
        )
        creds = flow.run_local_server(port=0)

    # Persist token for future runs (restricted permissions)
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    TOKEN_PATH.chmod(0o600)
    logger.info("Saved refreshed token to %s", TOKEN_PATH)

    return creds


def _build_gmail_service() -> Any:
    """Build and return an authenticated Gmail API service client.

    Returns:
        Authenticated Gmail API resource object.
    """
    creds = _load_credentials()
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    logger.debug("Gmail API service initialised.")
    return service


# ---------------------------------------------------------------------------
# Email construction
# ---------------------------------------------------------------------------


def _build_reminder_html(
    group_name: str,
    lecture_number: int,
    meeting_time: str,
    zoom_join_url: str,
) -> str:
    """Build a professional Georgian-language HTML email body.

    Args:
        group_name: Human-readable group name (e.g. "მარტის ჯგუფი #1").
        lecture_number: Sequential lecture index (1–15).
        meeting_time: Formatted meeting time string (e.g. "14 მარტი 2026 — 20:00").
        zoom_join_url: Full Zoom join URL for the meeting.

    Returns:
        Complete HTML string ready for use as email body.
    """
    return f"""<!DOCTYPE html>
<html lang="ka">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>AI კურსი — ლექცია #{lecture_number} შეხსენება</title>
</head>
<body style="margin:0;padding:0;background-color:#f4f6f9;font-family:'Segoe UI',Arial,sans-serif;color:#2d2d2d;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
    <tr>
      <td align="center" style="padding:32px 16px;">
        <table role="presentation" width="600" cellspacing="0" cellpadding="0" border="0"
               style="max-width:600px;background:#ffffff;border-radius:12px;overflow:hidden;
                      box-shadow:0 2px 12px rgba(0,0,0,0.08);">

          <!-- Header -->
          <tr>
            <td style="background:linear-gradient(135deg,#1a56db 0%,#0e3fad 100%);
                        padding:36px 40px;text-align:center;">
              <p style="margin:0 0 6px;font-size:13px;color:#a8c4ff;letter-spacing:2px;
                         text-transform:uppercase;">AI კურსი · {group_name}</p>
              <h1 style="margin:0;font-size:28px;font-weight:700;color:#ffffff;">
                ლექცია #{lecture_number} — შეხსენება
              </h1>
            </td>
          </tr>

          <!-- Body -->
          <tr>
            <td style="padding:36px 40px;">

              <!-- Greeting -->
              <p style="margin:0 0 20px;font-size:16px;line-height:1.6;color:#444;">
                გამარჯობა!
              </p>
              <p style="margin:0 0 28px;font-size:15px;line-height:1.7;color:#555;">
                გახსოვდეთ, რომ <strong>AI კურსის ლექცია #{lecture_number}</strong> იმართება
                მალე. ქვემოთ მოცემულია შეხვედრის დეტალები.
              </p>

              <!-- Details card -->
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"
                     style="background:#f0f4ff;border-radius:10px;margin-bottom:28px;">
                <tr>
                  <td style="padding:24px 28px;">

                    <!-- Time row -->
                    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"
                           style="margin-bottom:16px;">
                      <tr>
                        <td width="28" valign="top" style="padding-top:2px;">
                          <span style="font-size:18px;">🕗</span>
                        </td>
                        <td style="padding-left:10px;">
                          <p style="margin:0;font-size:12px;color:#6b7280;text-transform:uppercase;
                                     letter-spacing:1px;font-weight:600;">შეხვედრის დრო</p>
                          <p style="margin:4px 0 0;font-size:16px;font-weight:700;color:#1a1a2e;">
                            {meeting_time}
                          </p>
                        </td>
                      </tr>
                    </table>

                    <!-- Group row -->
                    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0">
                      <tr>
                        <td width="28" valign="top" style="padding-top:2px;">
                          <span style="font-size:18px;">👥</span>
                        </td>
                        <td style="padding-left:10px;">
                          <p style="margin:0;font-size:12px;color:#6b7280;text-transform:uppercase;
                                     letter-spacing:1px;font-weight:600;">ჯგუფი</p>
                          <p style="margin:4px 0 0;font-size:16px;font-weight:700;color:#1a1a2e;">
                            {group_name}
                          </p>
                        </td>
                      </tr>
                    </table>

                  </td>
                </tr>
              </table>

              <!-- Zoom CTA -->
              <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"
                     style="margin-bottom:28px;">
                <tr>
                  <td align="center">
                    <a href="{zoom_join_url}"
                       style="display:inline-block;background:#1a56db;color:#ffffff;
                              font-size:16px;font-weight:600;text-decoration:none;
                              padding:14px 36px;border-radius:8px;letter-spacing:0.3px;">
                      Zoom-ში შესვლა →
                    </a>
                  </td>
                </tr>
              </table>

              <!-- Fallback link -->
              <p style="margin:0 0 28px;font-size:13px;color:#888;text-align:center;">
                თუ ღილაკი არ მუშაობს, გადადი ამ ბმულზე:<br/>
                <a href="{zoom_join_url}" style="color:#1a56db;word-break:break-all;">{zoom_join_url}</a>
              </p>

              <!-- Divider -->
              <hr style="border:none;border-top:1px solid #e5e7eb;margin:0 0 24px;"/>

              <!-- Tips -->
              <p style="margin:0 0 10px;font-size:14px;font-weight:600;color:#374151;">
                რჩევები კარგი შეხვედრისთვის:
              </p>
              <ul style="margin:0 0 24px;padding-left:20px;font-size:14px;color:#555;line-height:1.9;">
                <li>შემოუერთდი 2–3 წუთით ადრე</li>
                <li>შეამოწმე მიკროფონი და კამერა</li>
                <li>მოამზადე კითხვები წინასწარ</li>
              </ul>

              <p style="margin:0;font-size:15px;color:#444;line-height:1.7;">
                ველოდები შენს შეხვედრაში!<br/>
                <strong>AI Pulse Georgia</strong>
              </p>

            </td>
          </tr>

          <!-- Footer -->
          <tr>
            <td style="background:#f9fafb;padding:20px 40px;text-align:center;
                        border-top:1px solid #e5e7eb;">
              <p style="margin:0;font-size:12px;color:#9ca3af;line-height:1.6;">
                ეს ავტომატური შეხსენება გამოგზავნა AI Pulse Georgia-ს სასწავლო სისტემამ.<br/>
                კითხვების შემთხვევაში მიმართე ტრენერს პირდაპირ.
              </p>
            </td>
          </tr>

        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def _encode_message(to_email: str, subject: str, html_body: str) -> dict[str, str]:
    """Encode an HTML email as a base64url Gmail API message payload.

    Args:
        to_email: Recipient email address.
        subject: Email subject line.
        html_body: Full HTML content of the email body.

    Returns:
        Dictionary with a single ``"raw"`` key containing the encoded message,
        ready to pass directly to the Gmail API ``messages.send`` endpoint.
    """
    message = MIMEMultipart("alternative")
    message["To"] = to_email
    message["Subject"] = subject
    message["Content-Type"] = "text/html; charset=utf-8"

    html_part = MIMEText(html_body, "html", "utf-8")
    message.attach(html_part)

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
    return {"raw": raw}


# ---------------------------------------------------------------------------
# Core send functions
# ---------------------------------------------------------------------------


def send_email(to_email: str, subject: str, html_body: str) -> bool:
    """Send a single HTML email via the Gmail API.

    Retries up to ``MAX_RETRIES`` times with exponential backoff on transient
    HTTP errors (5xx) and transport failures.

    Args:
        to_email: Recipient email address.
        subject: Email subject line.
        html_body: Complete HTML body of the email.

    Returns:
        ``True`` if the email was delivered successfully, ``False`` otherwise.
    """
    payload = _encode_message(to_email, subject, html_body)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            service = _build_gmail_service()
            result = (
                service.users()
                .messages()
                .send(userId="me", body=payload)
                .execute()
            )
            logger.info(
                "Email sent to %s (message_id=%s)", to_email, result.get("id", "?")
            )
            return True

        except HttpError as exc:
            status = exc.resp.status
            if status in {429, 500, 502, 503, 504} and attempt < MAX_RETRIES:
                delay = RETRY_BACKOFF_BASE**attempt
                logger.warning(
                    "HTTP %d sending to %s — retrying in %ds (attempt %d/%d).",
                    status,
                    to_email,
                    delay,
                    attempt,
                    MAX_RETRIES,
                )
                time.sleep(delay)
            else:
                logger.error(
                    "Failed to send email to %s after %d attempt(s): %s",
                    to_email,
                    attempt,
                    exc,
                )
                return False

        except TransportError as exc:
            if attempt < MAX_RETRIES:
                delay = RETRY_BACKOFF_BASE**attempt
                logger.warning(
                    "Transport error sending to %s — retrying in %ds (%s).",
                    to_email,
                    delay,
                    exc,
                )
                time.sleep(delay)
            else:
                logger.error(
                    "Transport error sending to %s after %d attempt(s): %s",
                    to_email,
                    attempt,
                    exc,
                )
                return False

    return False


def send_meeting_reminder(
    group_number: int,
    lecture_number: int,
    zoom_join_url: str,
    meeting_time: str,
) -> dict[str, Any]:
    """Send meeting reminder emails to all attendees of the specified group.

    Iterates over every address in ``GROUPS[group_number]["attendee_emails"]``
    and calls :func:`send_email` for each one.  Results are collected and
    returned so the caller can act on partial failures.

    Args:
        group_number: Training group identifier (1 or 2).
        lecture_number: Sequential lecture number being announced (1–15).
        zoom_join_url: Full Zoom join URL for the meeting.
        meeting_time: Human-readable meeting time in Georgian format,
            e.g. ``"14 მარტი 2026 — 20:00"``.

    Returns:
        A summary dictionary with keys:
        - ``"total"`` (int): Total recipients attempted.
        - ``"sent"`` (int): Successfully delivered count.
        - ``"failed"`` (int): Failed delivery count.
        - ``"failed_emails"`` (list[str]): Addresses that could not be reached.

    Raises:
        KeyError: If ``group_number`` is not 1 or 2.
    """
    if group_number not in GROUPS:
        raise KeyError(
            f"Unknown group number: {group_number}. Must be one of {list(GROUPS.keys())}."
        )

    group = GROUPS[group_number]
    group_name: str = group["name"]
    attendees: list[str] = group["attendee_emails"]

    subject = f"AI კურსი — ლექცია #{lecture_number} შეხსენება"
    html_body = _build_reminder_html(
        group_name=group_name,
        lecture_number=lecture_number,
        meeting_time=meeting_time,
        zoom_join_url=zoom_join_url,
    )

    logger.info(
        "Sending lecture #%d reminders for group %d (%s) to %d recipients.",
        lecture_number,
        group_number,
        group_name,
        len(attendees),
    )

    sent: list[str] = []
    failed: list[str] = []

    for email_address in attendees:
        success = send_email(
            to_email=email_address,
            subject=subject,
            html_body=html_body,
        )
        if success:
            sent.append(email_address)
        else:
            failed.append(email_address)

    summary: dict[str, Any] = {
        "total": len(attendees),
        "sent": len(sent),
        "failed": len(failed),
        "failed_emails": failed,
    }

    if failed:
        logger.warning(
            "Reminder send complete — %d/%d succeeded. Failed: %s",
            len(sent),
            len(attendees),
            failed,
        )
    else:
        logger.info(
            "All %d reminder emails sent successfully for lecture #%d.",
            len(sent),
            lecture_number,
        )

    return summary
