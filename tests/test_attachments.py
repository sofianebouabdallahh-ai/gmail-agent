import io

from gmail_agent.agents.attachments import attachment_to_text


def test_csv():
    out = attachment_to_text(b"item,qty\nwidget,2\n", "text/csv", "items.csv")
    assert "CSV, 2 rows" in out and "widget, 2" in out


def test_plain_text():
    out = attachment_to_text(b"note", "text/plain", "n.txt")
    assert out.endswith("note")


def test_pdf_roundtrip():
    from pypdf import PdfWriter

    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    w.write(buf)
    out = attachment_to_text(buf.getvalue(), "application/pdf", "x.pdf")
    assert "PDF, 1 pages" in out


def test_image_placeholder():
    out = attachment_to_text(b"\x89PNG", "image/png", "pic.png")
    assert "image" in out and "pic.png" in out


def test_truncation():
    out = attachment_to_text(b"x" * 100, "text/plain", "big.txt", limit=10)
    assert "truncated" in out and len(out.splitlines()[-1]) == 10
