"""Thin wrapper over the Gmail REST API plus a pure parser from raw JSON to `Email`."""

from __future__ import annotations

import base64
from typing import Any

from bs4 import BeautifulSoup
from googleapiclient.discovery import Resource

from gmail_agent.schemas import Attachment, Email

USER = "me"


# --------------------------------------------------------------------------- #
# Pure parsing (unit-testable without network)
# --------------------------------------------------------------------------- #
def _b64url_decode(data: str) -> bytes:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded)


def _header(headers: list[dict[str, str]], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _split_addresses(value: str) -> list[str]:
    return [p.strip() for p in value.split(",") if p.strip()]


def _walk_parts(part: dict[str, Any]):
    """Depth-first walk over a MIME tree yielding every leaf part."""
    yield part
    for child in part.get("parts", []) or []:
        yield from _walk_parts(child)


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text("\n", strip=True)


def parse_message(raw: dict[str, Any]) -> Email:
    """Turn a `users.messages.get(format='full')` response into an `Email`."""
    payload = raw.get("payload", {}) or {}
    headers = payload.get("headers", []) or []

    text_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[Attachment] = []

    for part in _walk_parts(payload):
        mime = part.get("mimeType", "")
        body = part.get("body", {}) or {}
        filename = part.get("filename") or ""

        if filename and body.get("attachmentId"):
            attachments.append(
                Attachment(
                    attachment_id=body["attachmentId"],
                    filename=filename,
                    mime_type=mime or "application/octet-stream",
                    size=int(body.get("size", 0) or 0),
                    content_id=_header(part.get("headers", []) or [], "Content-ID").strip("<>"),
                )
            )
            continue

        data = body.get("data")
        if not data:
            continue
        decoded = _b64url_decode(data).decode("utf-8", errors="replace")
        if mime == "text/plain":
            text_parts.append(decoded)
        elif mime == "text/html":
            html_parts.append(decoded)

    body_text = "\n".join(text_parts).strip()
    if not body_text and html_parts:
        body_text = "\n".join(_html_to_text(h) for h in html_parts).strip()

    return Email(
        id=raw["id"],
        thread_id=raw.get("threadId", raw["id"]),
        sender=_header(headers, "From"),
        to=_split_addresses(_header(headers, "To")),
        cc=_split_addresses(_header(headers, "Cc")),
        subject=_header(headers, "Subject"),
        date=_header(headers, "Date"),
        snippet=raw.get("snippet", ""),
        body_text=body_text,
        body_html="\n".join(html_parts).strip(),
        labels=list(raw.get("labelIds", []) or []),
        attachments=attachments,
    )


# --------------------------------------------------------------------------- #
# API wrapper
# --------------------------------------------------------------------------- #
class GmailClient:
    def __init__(self, service: Resource):
        self.service = service

    # -- reading ----------------------------------------------------------- #
    def list_message_ids(self, query: str = "is:unread in:inbox", max_results: int = 10) -> list[str]:
        resp = (
            self.service.users()
            .messages()
            .list(userId=USER, q=query, maxResults=max_results)
            .execute()
        )
        return [m["id"] for m in resp.get("messages", [])]

    def get_raw_message(self, message_id: str) -> dict[str, Any]:
        return (
            self.service.users()
            .messages()
            .get(userId=USER, id=message_id, format="full")
            .execute()
        )

    def get_email(self, message_id: str) -> Email:
        return parse_message(self.get_raw_message(message_id))

    def download_attachment(self, message_id: str, attachment_id: str) -> bytes:
        resp = (
            self.service.users()
            .messages()
            .attachments()
            .get(userId=USER, messageId=message_id, id=attachment_id)
            .execute()
        )
        return _b64url_decode(resp["data"])

    def list_history(self, start_history_id: str) -> dict[str, Any]:
        """For phase 5 (push notifications): changes since a stored historyId."""
        return (
            self.service.users()
            .history()
            .list(userId=USER, startHistoryId=start_history_id, historyTypes=["messageAdded"])
            .execute()
        )

    # -- writing (drafts and labels only; sending is deliberately absent) ---- #
    def create_draft(self, to: str, subject: str, body: str, thread_id: str | None = None) -> str:
        from email.mime.text import MIMEText

        msg = MIMEText(body)
        msg["to"] = to
        msg["subject"] = subject
        encoded = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        message: dict[str, Any] = {"raw": encoded}
        if thread_id:
            message["threadId"] = thread_id
        draft = (
            self.service.users()
            .drafts()
            .create(userId=USER, body={"message": message})
            .execute()
        )
        return draft["id"]

    def list_labels(self) -> dict[str, str]:
        """Return {label_name: label_id}."""
        resp = self.service.users().labels().list(userId=USER).execute()
        return {l["name"]: l["id"] for l in resp.get("labels", [])}

    def ensure_label(self, name: str) -> str:
        labels = self.list_labels()
        if name in labels:
            return labels[name]
        created = (
            self.service.users()
            .labels()
            .create(userId=USER, body={"name": name, "labelListVisibility": "labelShow",
                                         "messageListVisibility": "show"})
            .execute()
        )
        return created["id"]

    def modify_labels(self, message_id: str, add: list[str] | None = None,
                      remove: list[str] | None = None) -> None:
        body: dict[str, Any] = {}
        if add:
            body["addLabelIds"] = [self.ensure_label(n) for n in add]
        if remove:
            body["removeLabelIds"] = remove  # system labels like INBOX, UNREAD are already ids
        if body:
            self.service.users().messages().modify(userId=USER, id=message_id, body=body).execute()

    def archive(self, message_id: str) -> None:
        self.modify_labels(message_id, remove=["INBOX"])
