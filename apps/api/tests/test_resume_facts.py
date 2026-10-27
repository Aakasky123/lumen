"""Resume text is parsed and available as grounding context."""

import uuid
from io import BytesIO

from app import models
from app.db import SessionLocal, init_db
from app.extension_run import _resume_facts_for_user
from app.pdf_text import extract_pdf_text


def test_extract_pdf_text_graceful_on_non_pdf() -> None:
    assert extract_pdf_text(b"") == ""
    assert extract_pdf_text(b"this is not a pdf") == ""


def test_extract_pdf_text_preserves_safe_document_links() -> None:
    from pypdf import PdfWriter
    from pypdf.annotations import Link

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_annotation(
        page_number=0,
        annotation=Link(
            rect=(10, 10, 100, 30),
            url="https://example.test/candidate",
        ),
    )
    writer.add_annotation(
        page_number=0,
        annotation=Link(
            rect=(10, 40, 100, 60),
            url="javascript:alert('not evidence')",
        ),
    )
    buffer = BytesIO()
    writer.write(buffer)

    extracted = extract_pdf_text(buffer.getvalue())

    assert "Document links:" in extracted
    assert "https://example.test/candidate" in extracted
    assert "javascript:" not in extracted


def test_extract_resume_text_parses_docx_and_routes_by_ext() -> None:
    import docx

    from app.pdf_text import extract_resume_text

    document = docx.Document()
    document.add_paragraph("Built DocuMindAI with FastAPI and Qdrant.")
    buffer = BytesIO()
    document.save(buffer)
    assert "DocuMindAI" in extract_resume_text(buffer.getvalue(), "resume.docx")
    assert extract_resume_text(b"whatever", "notes.txt") == ""


def test_resume_facts_for_user_returns_base_resume_text() -> None:
    init_db()
    with SessionLocal() as db:
        user = models.User(email=f"rf-{uuid.uuid4().hex}@example.com", name="RF")
        db.add(user)
        db.flush()
        assert _resume_facts_for_user(db, user.id) == {}
        db.add(models.Resume(user_id=user.id, name="base.pdf", base_resume_text="Built DocuMindAI with FastAPI and Qdrant; 70% faster lookup.", latex_source="", is_base=True))
        db.commit()
        facts = _resume_facts_for_user(db, user.id)
        assert "DocuMindAI" in facts["resume_text"]
