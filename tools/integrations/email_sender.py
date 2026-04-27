"""Gmail email sending via Google Gmail API.

Uses the same Google OAuth2 credentials as Drive/Docs, but with the
gmail.send scope. If the existing token does not include the Gmail scope,
a re-authorization flow will be triggered (local only).

This module is the email fallback when WhatsApp delivery fails.
"""

from __future__ import annotations

import base64
import logging
from email.mime.text import MIMEText
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from tools.core.config import (
    IS_RAILWAY,
    OPERATOR_EMAIL,
    PROJECT_ROOT,
    _materialize_credential_file,
    get_google_credentials_path,
)

logger = logging.getLogger(__name__)

# Gmail API requires its own scope — kept separate from Drive scopes
# so we can manage a dedicated token file without breaking Drive auth.
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

GMAIL_TOKEN_PATH = PROJECT_ROOT / "token_gmail.json"


def _get_gmail_token_path() -> Path:
    """Resolve the Gmail token.json file path."""
    return _materialize_credential_file(
        "GOOGLE_GMAIL_TOKEN_JSON_B64", GMAIL_TOKEN_PATH
    )


def _get_gmail_credentials() -> Credentials:
    """Load or refresh Gmail OAuth2 credentials.

    Uses a separate token file (token_gmail.json) to avoid mixing scopes
    with the Drive token. On Railway, decoded from GOOGLE_GMAIL_TOKEN_JSON_B64.
    """
    creds = None

    try:
        token_path = _get_gmail_token_path()
        if token_path.exists():
            creds = Credentials.from_authorized_user_file(
                str(token_path), GMAIL_SCOPES
            )
    except FileNotFoundError:
        logger.debug("No Gmail token file found — will need authorization")

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            if IS_RAILWAY:
                logger.info("Gmail credentials refreshed in memory (Railway)")
            else:
                GMAIL_TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
                GMAIL_TOKEN_PATH.chmod(0o600)
        else:
            if IS_RAILWAY:
                raise RuntimeError(
                    "Gmail OAuth refresh_token is invalid or missing. "
                    "Re-authorize locally: python -m tools.integrations.email_sender, "
                    "then update GOOGLE_GMAIL_TOKEN_JSON_B64 in Railway."
                )
            credentials_path = get_google_credentials_path()
            flow = InstalledAppFlow.from_client_secrets_file(
                str(credentials_path), GMAIL_SCOPES
            )
            creds = flow.run_local_server(port=0)
            GMAIL_TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
            GMAIL_TOKEN_PATH.chmod(0o600)
            logger.info("Gmail token saved to %s", GMAIL_TOKEN_PATH)

    return creds


def _get_gmail_service():
    """Build an authenticated Gmail API service."""
    creds = _get_gmail_credentials()
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def send_email_report(
    subject: str,
    body: str,
    to_email: str | None = None,
) -> dict:
    """Send a plain-text email via Gmail API.

    Args:
        subject: Email subject line (Georgian text is fine).
        body: Plain-text email body.
        to_email: Recipient email. Defaults to OPERATOR_EMAIL from config.

    Returns:
        Gmail API response dict with message id.

    Raises:
        ValueError: If no recipient email is configured.
        RuntimeError: If Gmail credentials are unavailable.
        HttpError: If the Gmail API call fails.
    """
    recipient = to_email or OPERATOR_EMAIL
    if not recipient:
        raise ValueError(
            "No recipient email configured. "
            "Set OPERATOR_EMAIL in .env or pass to_email parameter."
        )

    logger.info("Sending email report to %s — subject: %s", recipient, subject)

    message = MIMEText(body, "plain", "utf-8")
    message["To"] = recipient
    message["Subject"] = subject

    # Gmail API expects base64url-encoded message
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")

    service = _get_gmail_service()
    result = (
        service.users()
        .messages()
        .send(userId="me", body={"raw": raw})
        .execute()
    )

    logger.info("Email sent successfully — message ID: %s", result.get("id"))
    return result


# ---------------------------------------------------------------------------
# CLI: run this module directly to authorize Gmail OAuth locally
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logger.info("Authorizing Gmail API access...")
    creds = _get_gmail_credentials()
    logger.info("Gmail authorization successful. Token saved to %s", GMAIL_TOKEN_PATH)
    logger.info(
        "For Railway, encode with: base64 -i token_gmail.json | tr -d '\\n'"
    )
