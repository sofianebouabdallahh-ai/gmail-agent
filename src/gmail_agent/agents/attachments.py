"""Attachment bytes -> text. Used by the extractor's tools."""

from __future__ import annotations

import csv
import io

from gmail_agent.config import settings


def attachment_to_text(data: bytes, mime_type: str, filename: str,
                       limit: int | None = None) -> str:
    name = filename.lower()
    limit = limit if limit is not None else settings.attachment_text_limit

    if mime_type == "application/pdf" or name.endswith(".pdf"):
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        pages = [(p.extract_text() or "") for p in reader.pages]
        text = "\n\n".join(f"[page {i + 1}]\n{t}" for i, t in enumerate(pages))
        return _cap(text, limit, f"PDF, {len(pages)} pages")

    if name.endswith(".docx") or mime_type.endswith("wordprocessingml.document"):
        from docx import Document

        doc = Document(io.BytesIO(data))
        text = "\n".join(p.text for p in doc.paragraphs)
        return _cap(text, limit, "DOCX")

    if name.endswith(".csv") or mime_type == "text/csv":
        rows = list(csv.reader(io.StringIO(data.decode("utf-8", errors="replace"))))
        text = "\n".join(", ".join(r) for r in rows)
        return _cap(text, limit, f"CSV, {len(rows)} rows")

    if mime_type.startswith("text/") or name.endswith((".txt", ".md", ".json", ".xml")):
        return _cap(data.decode("utf-8", errors="replace"), limit, mime_type)

    if mime_type.startswith("image/"):
        return (
            f"[{filename}: image ({mime_type}, {len(data)} bytes). "
            "Image understanding is not wired yet; describe based on filename and email context.]"
        )

    return f"[{filename}: binary attachment ({mime_type}, {len(data)} bytes), not parsed.]"


def _cap(text: str, limit: int, label: str) -> str:
    text = text.strip()
    if len(text) > limit:
        return f"[{label}, truncated to {limit} chars]\n{text[:limit]}"
    return f"[{label}]\n{text}"
