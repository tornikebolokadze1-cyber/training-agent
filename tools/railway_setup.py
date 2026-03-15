"""Helper script to generate base64-encoded env vars for Railway deployment.

Usage:
    python -m tools.railway_setup

Reads local credential files and outputs the base64 values you need to
paste into Railway's environment variable settings.

SECURITY: This script prints base64-encoded secrets to stdout. Run it
only on your local machine, never in CI/CD or shared environments.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent

FILES_TO_ENCODE = {
    "GOOGLE_CREDENTIALS_JSON_B64": PROJECT_ROOT / "credentials.json",
    "GOOGLE_TOKEN_JSON_B64": PROJECT_ROOT / "token.json",
    "GOOGLE_GMAIL_TOKEN_JSON_B64": PROJECT_ROOT / "token_gmail.json",
    "ATTENDEES_JSON_B64": PROJECT_ROOT / "attendees.json",
}


def main() -> None:
    print("=" * 70)
    print("Railway Base64 Environment Variables")
    print("=" * 70)
    print()
    print("Copy each value into Railway > Service > Variables.")
    print("DO NOT share these values or commit them anywhere.")
    print()

    for env_key, file_path in FILES_TO_ENCODE.items():
        if not file_path.exists():
            print(f"  SKIP  {env_key}")
            print(f"         File not found: {file_path}")
            print()
            continue

        content = file_path.read_bytes()
        encoded = base64.b64encode(content).decode("utf-8")

        print(f"  {env_key}")
        print(f"  Source: {file_path.name} ({len(content)} bytes)")
        print(f"  Value ({len(encoded)} chars):")
        print()
        # Print the value so it can be copied
        print(encoded)
        print()
        print("-" * 70)
        print()

    # Remind about non-file env vars
    print("REMINDER: Also configure these env vars directly in Railway:")
    print()

    from dotenv import dotenv_values
    env_values = dotenv_values(PROJECT_ROOT / ".env")

    skip_keys = {
        "GOOGLE_CREDENTIALS_PATH",  # Replaced by base64 approach
        "MANYCHAT_API_KEY", "MANYCHAT_TORNIKE_SUBSCRIBER_ID",
        "MANYCHAT_GROUP1_FLOW_ID", "MANYCHAT_GROUP2_FLOW_ID",
    }

    for key, value in env_values.items():
        if key in skip_keys:
            continue
        has_value = bool(value and value.strip())
        status = "SET" if has_value else "EMPTY"
        print(f"  [{status}]  {key}")

    print()
    print("Also add:  RAILWAY_ENVIRONMENT=production")
    print()


if __name__ == "__main__":
    main()
