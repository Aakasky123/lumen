"""A résumé saved in Documents is the base résumé application runs attach to job forms, and the bootstrap
account's placeholder name and email never reach the brain as facts about the user."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from uuid import uuid4

import pytest
from starlette.datastructures import UploadFile

from app import models
from app.application_artifacts import ApplicationArtifactService
from app.lumen_model import LumenModelClient
from app.candidate_context import CandidateContextEngine
from app.db import SessionLocal, init_db
from app.document_extraction import RESUME_STAMP_KEY, text_digest
from app.extension_run import _upload_documents_for_user
from app.resume_selection import active_base_resume, selected_resume_artifact


def _pdf(text: str) -> bytes:
    """A one-page PDF whose text layer is `text`."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


@pytest.fixture(autouse=True)
def _offline_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        LumenModelClient,
        "understand_artifact",
        lambda self, payload, *, image=None: {
            "suggested_artifact_kind": "resume",
            "suggested_display_name": "Resume",
            "summary": "A resume.",
            "key_facts": [],
            "confidence": 0.9,
            "warnings": [],
            "metadata": {},
        },
    )
    monkeypatch.setattr(LumenModelClient, "extract_user_facts_from_document", lambda self, payload: {"ok": True, "facts": []})


def _user(db) -> models.User:
    user = models.User(email=f"resume-base-{uuid4().hex[:8]}@example.com", name="Jane Doe")
    db.add(user)
    db.flush()
    db.add(models.UserProfile(user_id=user.id, profile_json={}))
    db.flush()
    return user


def _save(db, user: models.User, filename: str, content: bytes, selected_type: str) -> models.ApplicationArtifact:
    service = ApplicationArtifactService(db)
    preview = asyncio.run(service.preview_upload(user_id=user.id, file=UploadFile(filename=filename, file=io.BytesIO(content))))
    return service.commit_preview(user_id=user.id, draft_id=preview["draft_id"], selected_type=selected_type, display_name="")


def test_a_resume_saved_in_documents_is_the_one_job_forms_receive() -> None:
    init_db()
    with SessionLocal() as db:
        user = _user(db)
        artifact = _save(db, user, "Jane_Resume.pdf", _pdf("Jane Doe Software Engineer"), "resume")

        resume_artifact = selected_resume_artifact(db, user.id)
        assert resume_artifact is not None and resume_artifact.artifact_type == "BASE_PDF"
        document = _upload_documents_for_user(db, user.id)["resume"]
        assert document["filename"] == "Jane_Resume.pdf"
        assert document["download_path"] == f"/resume-artifacts/{resume_artifact.id}/download"
        # The extension attaches this computer's copy by path when a page refuses an injected file (no download).
        assert document["local_path"] == str(Path(resume_artifact.pdf_path).resolve())
        # A copy: deleting the document cannot pull the file out from under a run.
        assert Path(resume_artifact.pdf_path).is_file() and resume_artifact.pdf_path != artifact.storage_path
        base = active_base_resume(db, user.id)
        assert "Jane Doe" in base.base_resume_text
        assert artifact.metadata_json["base_resume_id"] == base.id
        # The upload already extracted this text's facts, so the run-start extraction skips it.
        profile = db.query(models.UserProfile).filter_by(user_id=user.id).one()
        assert profile.profile_json[RESUME_STAMP_KEY]["text_sha256"] == text_digest(base.base_resume_text)
        assert profile.profile_json[RESUME_STAMP_KEY]["resume_id"] == base.id


def test_the_newest_resume_replaces_the_base_and_deleting_it_stops_its_use() -> None:
    init_db()
    with SessionLocal() as db:
        user = _user(db)
        _save(db, user, "Old_Resume.pdf", _pdf("Old resume"), "resume")
        newer = _save(db, user, "New_Resume.pdf", _pdf("New resume"), "resume")
        assert _upload_documents_for_user(db, user.id)["resume"]["filename"] == "New_Resume.pdf"
        assert db.query(models.Resume).filter_by(user_id=user.id, is_base=True).count() == 1

        copy_path = Path(selected_resume_artifact(db, user.id).pdf_path)
        result = ApplicationArtifactService(db).delete(newer.id)
        assert selected_resume_artifact(db, user.id) is None
        assert "resume" not in _upload_documents_for_user(db, user.id)
        # Deleting takes the copy and the text with it, and a restart's repair does not bring it back.
        assert not copy_path.exists() and str(copy_path) in result["deleted_files"]
        assert db.get(models.Resume, newer.metadata_json["base_resume_id"]).base_resume_text == ""
        assert ApplicationArtifactService(db).promote_latest_resume_if_no_base(user.id) is None
        assert selected_resume_artifact(db, user.id) is None


def test_other_documents_and_non_resume_formats_leave_the_base_alone() -> None:
    init_db()
    with SessionLocal() as db:
        user = _user(db)
        _save(db, user, "Cover.pdf", _pdf("Dear hiring team"), "cover_letter")
        _save(db, user, "resume.txt", b"Jane Doe, engineer", "resume")
        assert active_base_resume(db, user.id) is None


def test_startup_repair_promotes_a_resume_saved_before_the_fix_once(tmp_path: Path) -> None:
    init_db()
    with SessionLocal() as db:
        user = _user(db)
        path = tmp_path / "Legacy_Resume.pdf"
        path.write_bytes(_pdf("Legacy resume"))
        db.add(
            models.ApplicationArtifact(
                user_id=user.id,
                artifact_kind="resume",
                source="uploaded",
                filename="Legacy_Resume.pdf",
                mime_type="application/pdf",
                storage_path=str(path),
                text_content="Legacy resume",
                text_preview="Legacy resume",
                status="active",
                metadata_json={"preview_draft_id": "legacy-draft"},
            )
        )
        db.flush()
        service = ApplicationArtifactService(db)
        assert service.promote_latest_resume_if_no_base(user.id) is not None
        assert _upload_documents_for_user(db, user.id)["resume"]["filename"] == "Legacy_Resume.pdf"
        assert service.promote_latest_resume_if_no_base(user.id) is None
        assert db.query(models.Resume).filter_by(user_id=user.id).count() == 1


def test_bootstrap_placeholder_identity_is_not_a_fact_about_the_user() -> None:
    init_db()
    with SessionLocal() as db:
        bootstrap = models.User(email=f"onboarding-{uuid4().hex[:12]}@example.com", name="Candidate")
        db.add(bootstrap)
        db.flush()
        db.add(models.UserProfile(user_id=bootstrap.id, profile_json={}))
        real = _user(db)
        db.flush()
        engine = CandidateContextEngine(db)
        assert engine.compact_defaults_with_provenance(bootstrap.id)["values"]["identity"] == {"name": None, "email": None}
        assert engine.compact_defaults_with_provenance(real.id)["values"]["identity"]["name"] == "Jane Doe"


def test_startup_repair_skips_run_uploads_and_deliberately_replaced_resumes(tmp_path: Path) -> None:
    init_db()
    with SessionLocal() as db:
        user = _user(db)
        old = _save(db, user, "Docs_Resume.pdf", _pdf("Docs resume"), "resume")
        service = ApplicationArtifactService(db)
        service.delete(old.id)
        run = models.OperatorRun(user_id=user.id, goal="Apply", target_url="https://example.test/job", current_state="RUNNING")
        db.add(run)
        db.flush()
        path = tmp_path / "Transcript_for_AcmeCo.pdf"
        path.write_bytes(_pdf("Transcript"))
        db.add(
            models.ApplicationArtifact(
                user_id=user.id,
                run_id=run.id,
                artifact_kind="resume",
                source="uploaded",
                filename=path.name,
                mime_type="application/pdf",
                storage_path=str(path),
                text_content="",
                text_preview="",
                status="active",
                metadata_json={},
            )
        )
        db.flush()
        # A file attached to one run's question is not the user's résumé for every job.
        assert service.promote_latest_resume_if_no_base(user.id) is None
        assert active_base_resume(db, user.id) is None
