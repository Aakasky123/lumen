"""Every document the user gave Lumen reaches memory once per version, not searched in every run."""

import uuid

from app import models, user_memory
from app.lumen_model import LumenModelClient
from app.db import SessionLocal, init_db
from app.document_extraction import ARTIFACT_STAMP_KEY, ensure_documents_extracted


def _user_with_documents(db, *, resume_text: str, artifact_text: str) -> int:
    user = models.User(email=f"docs-{uuid.uuid4().hex}@example.test", name="Docs")
    db.add(user)
    db.flush()
    db.add(models.UserProfile(user_id=user.id, profile_json={}))
    db.add(models.Resume(user_id=user.id, name="Resume.pdf", base_resume_text=resume_text, is_base=True))
    for status, text in (("active", artifact_text), ("deleted", "Deleted doc with Phone 555")):
        db.add(
            models.ApplicationArtifact(
                user_id=user.id,
                artifact_kind="transcript",
                source="upload",
                filename=f"{status}.pdf",
                status=status,
                text_content=text,
                metadata_json={},
            )
        )
    db.commit()
    return int(user.id)


def test_each_document_version_is_extracted_once(monkeypatch) -> None:
    init_db()
    calls: list[str] = []

    def fake_extract(self, payload):
        calls.append(str(payload.get("artifact_kind")))
        if payload.get("artifact_kind") == "resume":
            return {"ok": True, "facts": [{"key": "phone", "value": "+1 (555)-010-0142", "section": ""}]}
        return {"ok": True, "facts": [{"key": "masters_gpa", "value": "3.80/4.0", "section": ""}]}

    monkeypatch.setattr(LumenModelClient, "extract_user_facts_from_document", fake_extract)
    monkeypatch.setattr(LumenModelClient, "live_enabled", property(lambda self: True))
    with SessionLocal() as db:
        user_id = _user_with_documents(db, resume_text="NAME\nphone +1 (555)-010-0142", artifact_text="GPA 3.80/4.0")

        first = ensure_documents_extracted(db, user_id)
        assert first["status"] == "extracted" and first["documents"] == 2
        assert calls == ["resume", "transcript"], "the deleted document is never read"
        facts = {fact.key: fact.value for fact in user_memory.facts(user_id)}
        assert facts["phone"] == "+1 (555)-010-0142" and facts["masters_gpa"] == "3.80/4.0"

        # Same text: nothing is extracted again.
        assert ensure_documents_extracted(db, user_id)["status"] == "current"
        assert len(calls) == 2

        # A new resume version is extracted; the unchanged upload is not.
        resume = db.query(models.Resume).filter(models.Resume.user_id == user_id).first()
        resume.base_resume_text = "NAME\nphone +1 (555)-010-0142\nNew job"
        db.commit()
        assert ensure_documents_extracted(db, user_id)["documents"] == 1
        assert calls == ["resume", "transcript", "resume"]
        artifact = (
            db.query(models.ApplicationArtifact)
            .filter(models.ApplicationArtifact.user_id == user_id, models.ApplicationArtifact.status == "active")
            .first()
        )
        assert artifact.metadata_json[ARTIFACT_STAMP_KEY]


def test_an_unavailable_model_leaves_the_document_for_a_later_pass(monkeypatch) -> None:
    init_db()
    monkeypatch.setattr(
        LumenModelClient,
        "extract_user_facts_from_document",
        lambda self, payload: {"ok": False, "facts": [], "reason": "No live model is available."},
    )
    with SessionLocal() as db:
        user_id = _user_with_documents(db, resume_text="NAME", artifact_text="GPA 3.9")
        assert ensure_documents_extracted(db, user_id)["documents"] == 0
        profile = db.query(models.UserProfile).filter(models.UserProfile.user_id == user_id).first()
        assert "resume_extraction" not in (profile.profile_json or {})
