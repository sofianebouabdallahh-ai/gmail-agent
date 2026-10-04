import base64
from unittest.mock import MagicMock

from gmail_agent.gmail.client import GmailClient, parse_message


def test_parse_message_headers_and_body(raw_message):
    email = parse_message(raw_message)
    assert email.id == "18f3a9c0deadbeef"
    assert email.thread_id == "18f3a9c0deadbe00"
    assert email.sender == "Acme Billing <billing@acme.example>"
    assert email.to == ["sarah@example.com"]
    assert email.cc == ["ap@acme.example", "ops@acme.example"]
    assert email.subject == "Invoice #4521 - due Oct 15"
    assert "EUR 1,250.00" in email.body_text  # text/plain preferred over html
    assert "<p>" not in email.body_text
    assert "<p>" in email.body_html  # kept for display, not sent to the agents
    assert "<p>" not in email.as_prompt_text()
    assert email.labels == ["UNREAD", "INBOX"]


def test_parse_message_attachments(raw_message):
    email = parse_message(raw_message)
    names = [a.filename for a in email.attachments]
    assert names == ["invoice-4521.pdf", "line-items.csv"]
    pdf = email.attachments[0]
    assert pdf.attachment_id == "ANGjdJ8attach001"
    assert pdf.mime_type == "application/pdf"
    assert pdf.size == 48213


def test_parse_message_falls_back_to_html(raw_message):
    alt = raw_message["payload"]["parts"][0]
    alt["parts"] = [p for p in alt["parts"] if p["mimeType"] == "text/html"]
    email = parse_message(raw_message)
    assert "Please find attached the invoice." in email.body_text


def test_prompt_text_marks_body_untrusted(raw_message):
    text = parse_message(raw_message).as_prompt_text()
    assert "untrusted" in text
    assert "invoice-4521.pdf" in text


def test_download_attachment_decodes_base64url():
    service = MagicMock()
    payload = base64.urlsafe_b64encode(b"hello,world").decode().rstrip("=")
    (service.users.return_value.messages.return_value.attachments.return_value
     .get.return_value.execute.return_value) = {"data": payload}
    client = GmailClient(service)
    assert client.download_attachment("m1", "a1") == b"hello,world"
