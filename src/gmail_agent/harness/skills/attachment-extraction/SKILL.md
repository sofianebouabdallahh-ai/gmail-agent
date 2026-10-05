---
name: attachment-extraction
description: How to read email attachments (PDF, spreadsheets, images, calendar invites) and turn them into summaries and key facts. Load before calling read_attachment.
---

# Attachment extraction

`read_attachment` downloads one attachment and returns its text, prefixed by a header
in brackets, for example `[PDF, 3 pages]`, `[CSV, 12 rows]`,
`[DOCX]`, or `[... truncated to N chars]`.

## Procedure

1. Call `read_attachment` once per attachment, passing `message_id`, `attachment_id`,
   `filename` and `mime_type` exactly as listed in the email.
2. Look at the bracketed header first and open the matching reference for the type:
   - PDF → `references/pdf.md`
   - CSV / spreadsheet → `references/spreadsheets.md`
   - image or `binary attachment, not parsed` → `references/images-and-scans.md`
   - `text/calendar` / `.ics` → `references/calendar-invites.md`
   Read a reference with `read_skill_file("attachment-extraction", "references/<file>")`.
3. For each attachment, fill one `ExtractedAttachment`:
   - `summary`: two or three sentences on what the document is and why it was sent.
   - `key_facts`: concrete values only: totals, due dates, reference/policy/booking
     numbers, names, addresses. One fact per item, quoted exactly.
4. If the text was truncated, say so in the summary and do not guess the rest.

## Quality bar

- An amount is always written with its currency and as printed (`EUR 1,250.00`, `240 $`).
- A date is written as printed; add the ISO form in parentheses only when unambiguous.
- Inline logos and social icons are not documents. Skip them in `attachments`.
