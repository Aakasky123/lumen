"""Bring every document the user gave Lumen into memory once, instead of searching its text every run.

Uploading a document already asks a model for the user facts in it
(ApplicationArtifactService.remember_user_facts). A document that never went through that (the base
resume record, uploads from before it existed, facts lost in a memory migration) left runs searching
raw text, where a grounding judge could misread it (run 1796 missed the phone between PDF separator
glyphs). This extracts each document whose current text has not been extracted yet: the active base
resume and every live uploaded document with text. Each is keyed by its text's hash, so it runs once
per version. The model decides the facts; this only stores them and stamps the version.
"""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import select

from app import models

RESUME_STAMP_KEY = "resume_extraction"
ARTIFACT_STAMP_KEY = "user_facts_extracted_sha256"
# A run start extracts at most this many documents; the rest follow at the next run.
MAX_DOCUMENTS_PER_PASS = 3


def text_digest(text: str) -> str:
    return hashlib.sha256(str(text or "").strip().encode("utf-8")).hexdigest()


def stamp_artifact_extracted(artifact: models.ApplicationArtifact) -> None:
    metadata = dict(artifact.metadata_json or {}) if isinstance(artifact.metadata_json, dict) else {}
    metadata[ARTIFACT_STAMP_KEY] = text_digest(artifact.text_content or "")
    artifact.metadata_json = metadata


def ensure_documents_extracted(db: Any, user_id: int) -> dict[str, Any]:
    """Extract and remember facts from each not-yet-extracted document version. Never raises."""
    from app.application_artifacts import ApplicationArtifactService
    from app.lumen_model import LumenModelClient
    from app import user_memory
    from app.event_store import emit_event
    from app.resume_selection import active_base_resume

    written: list[str] = []
    documents = 0
    try:
        resume = active_base_resume(db, user_id)
        text = str((resume.base_resume_text if resume else "") or "").strip()
        if text:
            digest = text_digest(text)
            profile = db.scalars(select(models.UserProfile).where(models.UserProfile.user_id == user_id)).first()
            stamp = (profile.profile_json or {}).get(RESUME_STAMP_KEY) if profile and isinstance(profile.profile_json, dict) else None
            if not (isinstance(stamp, dict) and stamp.get("text_sha256") == digest):
                extraction = LumenModelClient(db, user_id=user_id).extract_user_facts_from_document(
                    {"display_name": resume.name or "resume", "artifact_kind": "resume", "text": text}
                )
                if extraction.get("ok"):
                    applied = user_memory.remember_all_reconciled(
                        db, user_id, extraction.get("facts") or [], source_note=f"from {str(resume.name or 'your resume')[:60]}"
                    )
                    written += [item["key"] for item in applied if item.get("action") in {"add", "update"}]
                    profile = db.scalars(select(models.UserProfile).where(models.UserProfile.user_id == user_id)).first()
                    if profile is not None:
                        profile_json = dict(profile.profile_json or {})
                        profile_json[RESUME_STAMP_KEY] = {
                            "resume_id": int(resume.id),
                            "text_sha256": digest,
                            "extracted_at": models.utcnow().isoformat(),
                        }
                        profile.profile_json = profile_json
                    documents += 1
                    db.commit()

        service = ApplicationArtifactService(db)
        artifacts = db.scalars(
            select(models.ApplicationArtifact)
            .where(models.ApplicationArtifact.user_id == user_id, models.ApplicationArtifact.status != "deleted")
            .order_by(models.ApplicationArtifact.id.desc())
        ).all()
        for artifact in artifacts:
            if documents >= MAX_DOCUMENTS_PER_PASS:
                break
            artifact_text = str(artifact.text_content or "").strip()
            metadata = artifact.metadata_json if isinstance(artifact.metadata_json, dict) else {}
            if not artifact_text or metadata.get(ARTIFACT_STAMP_KEY) == text_digest(artifact_text):
                continue
            outcome = service.remember_user_facts(artifact)
            if outcome.get("reason"):
                continue  # model unavailable: leave it unstamped so a later pass retries
            written += [item["key"] for item in outcome.get("remembered") or []]
            stamp_artifact_extracted(artifact)
            documents += 1
            db.commit()

        if documents:
            emit_event(
                db,
                event_type="DOCUMENT_FACTS_EXTRACTED",
                payload={"documents": documents, "keys": written[:60]},
                actor="document_extraction",
                confidence=0.85,
                risk_level="low",
            )
            db.commit()
        return {"status": "extracted" if documents else "current", "documents": documents, "keys": written}
    except Exception as exc:  # noqa: BLE001 - enrichment never blocks a run
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"status": "error", "reason": str(exc)[:200], "keys": written}
