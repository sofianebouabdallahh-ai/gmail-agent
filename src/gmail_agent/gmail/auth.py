"""OAuth helpers. First run opens a browser and writes token.json next to credentials.json."""

from __future__ import annotations

from pathlib import Path

from googleapiclient.discovery import Resource
from langchain_google_community.gmail.utils import (
    build_resource_service,
    get_google_credentials,
)

from gmail_agent.config import settings

# readonly: read mail and attachments.  compose: create drafts (never send).
# modify: add/remove labels and archive.  Drop scopes you do not need.
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.modify",
]


def get_gmail_service(
    credentials_file: Path | None = None,
    token_file: Path | None = None,
) -> Resource:
    creds_path = credentials_file or settings.credentials_file
    token_path = token_file or settings.token_file
    if not creds_path.exists():
        raise FileNotFoundError(
            f"{creds_path} not found. Download an OAuth 'Desktop app' client from the "
            "Google Cloud console (APIs & Services > Credentials) and save it there."
        )
    creds = get_google_credentials(
        scopes=SCOPES,
        token_file=str(token_path),
        client_secrets_file=str(creds_path),
    )
    return build_resource_service(credentials=creds)
