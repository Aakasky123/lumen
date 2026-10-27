from __future__ import annotations

import base64
import json
import mimetypes
import re
import shutil
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.artifact_naming import artifact_display_name, normalize_artifact_role
from app.lumen_model import LumenModelClient
from app.config import get_settings
from app.operation_receipt import make_receipt
from app.pdf_text import extract_resume_text
from app.resume_selection import active_base_resume


DOCUMENT_MIME_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
DOCUMENT_EXTENSIONS = {".pdf", ".doc", ".docx", ".txt", ".rtf"}
TEXT_ARTIFACT_EXTENSIONS = {".csv", ".json", ".md", ".rtf", ".txt"}
IMAGE_ARTIFACT_EXTENSIONS = {".heic", ".jpeg", ".jpg", ".png", ".webp"}
BLOCKED_GENERIC_ARTIFACT_EXTENSIONS = {".bat", ".cmd", ".dll", ".exe", ".js", ".msi", ".ps1", ".py", ".sh", ".vbs"}
GENERIC_ARTIFACT_EXTENSIONS = DOCUMENT_EXTENSIONS | TEXT_ARTIFACT_EXTENSIONS | IMAGE_ARTIFACT_EXTENSIONS
# A Documents résumé in one of these formats becomes the base résumé job forms receive.
BASE_RESUME_SUFFIXES = {".pdf", ".docx", ".doc"}
ARTIFACT_PREVIEW_PUBLIC_KEYS = {
    "draft_id",
    "filename",
    "mime_type",
    "file_size",
    "text_preview",
    "extracted_text_preview",
    "suggested_artifact_kind",
    "suggested_display_name",
    "summary",
    "key_facts",
    "confidence",
    "warnings",
    "extraction_status",
    "provider",
    "model",
}


def safe_artifact_filename(value: str, *, default: str = "artifact") -> str:
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", str(value or "").strip()).strip(" ._")
    if not name:
        name = default
    return name[:160]


class ApplicationArtifactService:
    def __init__(self, db: Session):
        self.db = db
        self.settings = get_settings()
        self.model = LumenModelClient(db)

    def list_active(self, *, user_id: int | None = None) -> list[models.ApplicationArtifact]:
        stmt = select(models.ApplicationArtifact).where(models.ApplicationArtifact.status != "deleted")
        if user_id is not None:
            stmt = stmt.where(models.ApplicationArtifact.user_id == user_id)
        return list(self.db.scalars(stmt.order_by(models.ApplicationArtifact.id.desc())).all())

    def get_active(self, artifact_id: int) -> models.ApplicationArtifact:
        artifact = self.db.get(models.ApplicationArtifact, artifact_id)
        if not artifact or artifact.status == "deleted":
            raise HTTPException(status_code=404, detail="Artifact not found")
        return artifact

    async def preview_upload(self, *, user_id: int, file: UploadFile) -> dict[str, Any]:
        filename = safe_artifact_filename(file.filename or "uploaded-artifact")
        ext = Path(filename).suffix.lower()
        if ext in BLOCKED_GENERIC_ARTIFACT_EXTENSIONS or (ext and ext not in GENERIC_ARTIFACT_EXTENSIONS):
            raise HTTPException(status_code=400, detail="Upload a safe document, image, text, or data artifact.")
        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="Uploaded artifact is empty.")
        mime_type = file.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        text_content, extraction_status, extraction_warnings = self._extract_artifact_text(filename=filename, content=content, mime_type=mime_type)
        image_payload = self._image_payload(filename=filename, content=content, mime_type=mime_type)
        if image_payload and not LumenModelClient(self.db, user_id=user_id).provider_service.vision_provider(user_id=user_id):
            raise HTTPException(status_code=400, detail="A vision-capable model provider is required to preview image artifacts.")
        model = LumenModelClient(self.db, user_id=user_id)
        understanding = model.understand_artifact(
            {
                "filename": filename,
                "mime_type": mime_type,
                "file_size": len(content),
                "extraction_status": extraction_status,
                "extraction_warnings": extraction_warnings,
                "extracted_text": text_content[:12000],
            },
            image=image_payload,
        )
        draft_id = uuid4().hex
        draft_dir = self._draft_dir(user_id=user_id, draft_id=draft_id, create=True)
        stored_path = draft_dir / filename
        stored_path.write_bytes(content)
        metadata = dict(understanding.get("metadata") or {})
        warnings = [str(item) for item in extraction_warnings + list(understanding.get("warnings") or []) if str(item).strip()]
        preview = {
            "draft_id": draft_id,
            "filename": filename,
            "original_filename": file.filename,
            "mime_type": mime_type,
            "file_size": len(content),
            "storage_path": str(stored_path),
            "text_content": text_content,
            "text_preview": self._text_preview(text_content),
            "extracted_text_preview": self._text_preview(text_content),
            "suggested_artifact_kind": normalize_artifact_role(understanding.get("suggested_artifact_kind"), default="artifact"),
            "suggested_display_name": artifact_display_name(
                filename=filename,
                display_name=str(understanding.get("suggested_display_name") or ""),
                artifact_kind=normalize_artifact_role(understanding.get("suggested_artifact_kind"), default="artifact"),
            ),
            "summary": str(understanding.get("summary") or "").strip(),
            "key_facts": self._normalized_key_facts(understanding.get("key_facts")),
            "confidence": self._confidence(understanding.get("confidence")),
            "warnings": warnings[:12],
            "extraction_status": extraction_status,
            "provider": metadata.get("provider"),
            "model": metadata.get("model"),
            "model_metadata": metadata,
        }
        (draft_dir / "preview.json").write_text(json.dumps(preview, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return self._public_preview(preview)

    def commit_preview(
        self,
        *,
        user_id: int,
        draft_id: str,
        selected_type: str | None,
        display_name: str | None = None,
        source: str = "uploaded",
    ) -> models.ApplicationArtifact:
        preview = self._load_preview(user_id=user_id, draft_id=draft_id)
        filename = safe_artifact_filename(str(preview.get("filename") or "uploaded-artifact"))
        selected_role = normalize_artifact_role(selected_type or preview.get("suggested_artifact_kind") or "artifact", default="artifact")
        name = artifact_display_name(
            filename=filename,
            display_name=display_name or str(preview.get("suggested_display_name") or ""),
            artifact_kind=selected_role,
        )
        text_content = str(preview.get("text_content") or "")
        metadata = {
            "display_name": name,
            "artifact_role": selected_role,
            "selected_artifact_kind": selected_role,
            "llm_suggested_artifact_kind": normalize_artifact_role(preview.get("suggested_artifact_kind"), default="artifact"),
            "llm_suggested_display_name": preview.get("suggested_display_name") or "",
            "summary": preview.get("summary") or "",
            "key_facts": self._normalized_key_facts(preview.get("key_facts")),
            "confidence": self._confidence(preview.get("confidence")),
            "warnings": [str(item) for item in preview.get("warnings") or [] if str(item).strip()][:12],
            "extraction_status": preview.get("extraction_status") or "unknown",
            "model_provider": preview.get("provider"),
            "model": preview.get("model"),
            "model_metadata": preview.get("model_metadata") or {},
            "original_filename": preview.get("original_filename") or filename,
            "preview_draft_id": draft_id,
            "source": source,
        }
        artifact = self._create_user_artifact_row(
            user_id=user_id,
            kind=selected_role,
            source=source,
            filename=filename,
            mime_type=str(preview.get("mime_type") or mimetypes.guess_type(filename)[0] or "application/octet-stream"),
            text_content=text_content,
            metadata=metadata,
        )
        source_path = self._safe_draft_file(user_id=user_id, draft_id=draft_id, filename=filename)
        path = self._artifact_path(artifact, filename)
        shutil.move(str(source_path), str(path))
        artifact.storage_path = str(path)
        artifact.file_size = path.stat().st_size
        artifact.updated_at = models.utcnow()
        self.db.flush()
        with suppress(Exception):
            shutil.rmtree(self._draft_dir(user_id=user_id, draft_id=draft_id, create=False))
        outcome = self.remember_user_facts(artifact)
        if not outcome.get("reason"):
            from app.document_extraction import stamp_artifact_extracted

            # Extracted at upload: the run-start backfill (document_extraction) skips this version.
            stamp_artifact_extracted(artifact)
        if selected_role == "resume":
            self.promote_to_base_resume(artifact)
        return artifact

    def promote_to_base_resume(self, artifact: models.ApplicationArtifact) -> models.Resume | None:
        """A résumé file saved in Documents becomes the base résumé: the one application runs attach to job forms
        and read screening answers from. The file is copied, so deleting the document later cannot break an
        upload in flight; the previous base stops being the base. Only a .pdf/.docx/.doc file qualifies."""
        from app.document_extraction import ARTIFACT_STAMP_KEY, RESUME_STAMP_KEY, text_digest

        source = Path(str(artifact.storage_path or ""))
        if source.suffix.lower() not in BASE_RESUME_SUFFIXES or not source.is_file():
            return None
        for previous in self.db.scalars(
            select(models.Resume).where(models.Resume.user_id == artifact.user_id, models.Resume.is_base.is_(True))
        ).all():
            previous.is_base = False
        text = str(artifact.text_content or "")
        resume = models.Resume(
            user_id=artifact.user_id,
            name=artifact.filename or source.name,
            base_resume_text=text,
            latex_source="",
            is_base=True,
        )
        self.db.add(resume)
        self.db.flush()
        target_dir = self.settings.artifact_root / "resumes" / str(resume.id)
        target_dir.mkdir(parents=True, exist_ok=True)
        # The form receives the file under this name, so it keeps the user's own filename.
        target = target_dir / safe_artifact_filename(artifact.filename or source.name)
        shutil.copy2(source, target)
        resume.pdf_url = str(target)
        self.db.add(
            models.ResumeArtifact(
                resume_id=resume.id,
                resume_version_id=None,
                artifact_type="BASE_PDF",
                source_path=str(target),
                pdf_path=str(target),
                compiler_used="uploaded",
                compile_status="SUCCESS",
                file_size=target.stat().st_size,
            )
        )
        metadata = dict(artifact.metadata_json or {}) if isinstance(artifact.metadata_json, dict) else {}
        metadata["base_resume_id"] = int(resume.id)
        artifact.metadata_json = metadata
        if text.strip() and metadata.get(ARTIFACT_STAMP_KEY) == text_digest(text):
            # The upload already brought this text's facts into memory: the run-start extraction of the base
            # résumé (document_extraction) would pay a second model call for the same facts.
            profile = self.db.scalars(
                select(models.UserProfile).where(models.UserProfile.user_id == artifact.user_id)
            ).first()
            if profile is not None:
                profile_json = dict(profile.profile_json or {})
                profile_json[RESUME_STAMP_KEY] = {
                    "resume_id": int(resume.id),
                    "text_sha256": text_digest(text),
                    "extracted_at": models.utcnow().isoformat(),
                }
                profile.profile_json = profile_json
        self.db.flush()
        return resume

    def promote_latest_resume_if_no_base(self, user_id: int) -> models.Resume | None:
        """Startup repair for résumés saved in Documents before saving one made it the base résumé.

        Only a Documents save qualifies (no run, saved through the preview), never a file attached to one run's
        question; and only one never promoted before, so a restart does not undo a deliberate delete or replace."""
        if active_base_resume(self.db, user_id) is not None:
            return None
        for artifact in self.db.scalars(
            select(models.ApplicationArtifact)
            .where(
                models.ApplicationArtifact.user_id == user_id,
                models.ApplicationArtifact.status == "active",
                models.ApplicationArtifact.storage_path.isnot(None),
                models.ApplicationArtifact.run_id.is_(None),
            )
            .order_by(models.ApplicationArtifact.id.desc())
        ).all():
            metadata = artifact.metadata_json if isinstance(artifact.metadata_json, dict) else {}
            if (
                normalize_artifact_role(artifact.artifact_kind, default="") != "resume"
                or not metadata.get("preview_draft_id")
                or metadata.get("base_resume_id")
            ):
                continue
            resume = self.promote_to_base_resume(artifact)
            if resume is not None:
                return resume
        return None

    def remember_user_facts(self, artifact: models.ApplicationArtifact) -> dict[str, Any]:
        """Model-judged enrichment: read the uploaded document, remember durable user facts.

        The model decides which facts (if any) belong in the user's memory and how to key
        them; this method only stores what the model chose, stamps the artifact as the
        source, and reports honestly. Best-effort: a model failure never breaks an upload.
        """
        from app import user_memory
        from app.lumen_model import LumenModelClient
        from app.event_store import emit_event

        text = str(artifact.text_content or "").strip()
        if not text:
            return {"remembered": [], "reason": "artifact has no extracted text"}
        metadata = artifact.metadata_json if isinstance(artifact.metadata_json, dict) else {}
        try:
            extraction = LumenModelClient(self.db).extract_user_facts_from_document(
                {
                    "display_name": metadata.get("display_name") or artifact.filename,
                    "artifact_kind": artifact.artifact_kind,
                    "text": text,
                }
            )
        except Exception as exc:  # noqa: BLE001 - enrichment must never break the upload
            return {"remembered": [], "reason": str(exc)[:300]}
        if not extraction.get("ok"):
            return {"remembered": [], "reason": str(extraction.get("reason") or "model unavailable")}
        applied = user_memory.remember_all_reconciled(
            self.db,
            artifact.user_id,
            extraction.get("facts") or [],
            source_note=f"from {str(metadata.get('display_name') or artifact.filename or 'a document')[:60]}",
        )
        remembered = [
            {"key": item["key"], "value": item["value"], "action": item["action"]}
            for item in applied
            if item["action"] in {"add", "update"}
        ]
        conflicts = [item for item in applied if item["action"] == "conflict"]
        if conflicts:
            # Contradicting values are the user's call. Nothing was written; ask in chat
            # where the user can answer naturally (keep one, rename both, forget one).
            self._ask_about_memory_conflicts(artifact, conflicts)
        with suppress(Exception):
            emit_event(
                self.db,
                event_type="USER_MEMORY_WRITTEN",
                payload={
                    "source": "artifact_upload",
                    "artifact_id": artifact.id,
                    "keys": [item["key"] for item in remembered],
                    "conflict_keys": [item["key"] for item in conflicts],
                },
                actor="artifact_intelligence",
                confidence=0.85,
                risk_level="low",
            )
        skipped = [
            {"key": item["key"], "value": item["value"]}
            for item in applied
            if item["action"] == "skip"
        ]
        return {"remembered": remembered, "conflicts": conflicts, "skipped": skipped, "reason": ""}

    def _ask_about_memory_conflicts(self, artifact: models.ApplicationArtifact, conflicts: list[dict[str, str]]) -> None:
        """Post the conflict question to chat; a failure here never breaks the upload."""
        try:
            from app.cognition import OperatorChatService

            metadata = artifact.metadata_json if isinstance(artifact.metadata_json, dict) else {}
            document_name = str(metadata.get("display_name") or artifact.filename)
            lines = [
                f"While reading “{document_name}” I found "
                + ("a conflict with what I have in memory:" if len(conflicts) == 1 else "conflicts with what I have in memory:")
            ]
            for item in conflicts:
                spoken = str(item["key"]).replace("_", " ")
                lines.append(
                    f"• {spoken}: I have “{item.get('existing_value') or ''}” stored, "
                    f"but the document says “{item['value']}”."
                )
            lines.append(
                "Which should I keep? You can also keep both under separate names (for example "
                "personal email and school email) or tell me to forget one. I haven't changed "
                "anything yet."
            )
            service = OperatorChatService(self.db)
            thread = service.create_thread(user_id=artifact.user_id, title="Memory check")
            service._add_message(
                thread,
                role="assistant",
                content="\n".join(lines),
                payload={"source": "user_memory_conflict", "artifact_id": artifact.id},
            )
        except Exception:  # noqa: BLE001 - asking is best-effort; the conflict event remains
            pass

    async def upload_for_question(self, *, question_id: int, file: UploadFile, auto_resume: bool = True) -> dict[str, Any]:
        question, run, task = self._question_task(question_id)
        filename = safe_artifact_filename(file.filename or "uploaded-document")
        ext = Path(filename).suffix.lower()
        if ext not in DOCUMENT_EXTENSIONS:
            raise HTTPException(status_code=400, detail="Upload a PDF, DOC, DOCX, TXT, or RTF document.")
        content = await file.read()
        if not content:
            raise HTTPException(status_code=400, detail="Uploaded artifact is empty.")
        kind = normalize_artifact_role(self._task_document_kind(task), default="other_document")
        artifact = self._create_artifact_row(
            run=run,
            kind=kind,
            source="uploaded",
            filename=filename,
            mime_type=file.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream",
            text_content="",
            metadata={
                "display_name": artifact_display_name(filename=filename, artifact_kind=kind),
                "artifact_role": kind,
                "question_id": question.id,
                "task_id": task.id,
                "original_filename": file.filename,
            },
        )
        path = self._artifact_path(artifact, filename)
        path.write_bytes(content)
        artifact.storage_path = str(path)
        artifact.file_size = path.stat().st_size
        artifact.updated_at = models.utcnow()
        return self.apply_to_question(question_id=question.id, artifact_id=artifact.id, auto_resume=auto_resume)

    def generate_for_question(self, *, question_id: int, instructions: str | None = None, artifact_kind: str | None = None) -> dict[str, Any]:
        question, run, task = self._question_task(question_id)
        kind = normalize_artifact_role(artifact_kind or self._task_document_kind(task) or "cover_letter", default="document")
        context = self._generation_context(run, task=task, instructions=instructions, artifact_kind=kind)
        draft = self.model.generate_application_document(context)
        text = str(draft.get("body") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="Lumen could not generate a document draft from the available context.")
        name = artifact_display_name(display_name=draft.get("title"), artifact_kind=kind)
        filename = safe_artifact_filename(name or f"{kind} draft", default=kind).removesuffix(".docx") + ".docx"
        artifact = self._create_artifact_row(
            run=run,
            kind=kind,
            source="generated",
            filename=filename,
            mime_type=DOCUMENT_MIME_DOCX,
            text_content=text,
            metadata={
                "question_id": question.id,
                "task_id": task.id,
                "display_name": name,
                "artifact_role": kind,
                "instructions": instructions or "",
                "generation": {key: draft.get(key) for key in ("confidence", "rationale", "source")},
                "preview_first": True,
            },
            status="draft",
        )
        path = self._artifact_path(artifact, filename)
        self._write_docx(path, title=str(draft.get("title") or "Application document"), body=text)
        artifact.storage_path = str(path)
        artifact.file_size = path.stat().st_size
        artifact.updated_at = models.utcnow()
        self.db.flush()
        return {"artifact": artifact, "question": question, "run": run, "applied": None}

    def apply_to_question(self, *, question_id: int, artifact_id: int, auto_resume: bool = True) -> dict[str, Any]:
        question, run, task = self._question_task(question_id)
        artifact = self.get_active(artifact_id)
        if artifact.user_id != run.user_id:
            raise HTTPException(status_code=403, detail="Artifact does not belong to this user.")
        payload = dict(task.payload or {})
        control_kind = str(((payload.get("document_capability") or {}).get("target_control_kind") or payload.get("control_kind") or "")).lower()
        artifact_path = str(artifact.storage_path or "").strip()
        use_file = control_kind in {"file_input", "upload_zone"} or bool(artifact_path and payload.get("answer_mode") == "upload_file")
        if use_file:
            if not artifact_path or not Path(artifact_path).exists():
                raise HTTPException(status_code=404, detail="Artifact file is not available for upload.")
            approved_value = artifact_path
            value_category = "document_upload"
        else:
            approved_value = str(artifact.text_content or artifact.text_preview or "").strip()
            if not approved_value:
                raise HTTPException(status_code=400, detail="Artifact has no text content to apply to this field.")
            value_category = "document_text"
        payload.update(
            {
                "approved_artifact_id": artifact.id,
                "approved_artifact_path": artifact_path,
                "approved_artifact_kind": artifact.artifact_kind,
                "approved_artifact_source": artifact.source,
                "value_category": value_category,
                "answer_mode": "upload_file" if use_file else "artifact_text",
            }
        )
        task.payload = payload
        task.updated_at = models.utcnow()
        from app.runtime import OperatorRuntimeEngine

        OperatorRuntimeEngine(self.db).approve_fields(run_id=run.id, task_ids=[task.id], approved=True, values={str(task.id): approved_value})
        question.status = "applied"
        question.answer_payload = {"artifact_id": artifact.id, "answer_mode": payload["answer_mode"], "auto_resume": auto_resume}
        question.applied_at = models.utcnow()
        question.updated_at = models.utcnow()
        for other in self.db.scalars(
            select(models.OperatorQuestion).where(
                models.OperatorQuestion.run_id == run.id,
                models.OperatorQuestion.question_type == "field_approval",
                models.OperatorQuestion.status == "pending",
                models.OperatorQuestion.id != question.id,
            )
        ).all():
            other.status = "superseded"
            other.updated_at = models.utcnow()
        artifact.status = "active"
        artifact.updated_at = models.utcnow()
        self._emit_artifact_receipt(run, artifact=artifact, task=task, use_file=use_file)
        resumed = False
        if auto_resume:
            try:
                from app.extension_run import schedule_resume_if_in_browser

                resumed = bool(schedule_resume_if_in_browser(run.id))
            except Exception:
                resumed = False
        question.resumed_at = models.utcnow() if resumed else question.resumed_at
        self.db.flush()
        return {"artifact": artifact, "question": question, "run": run, "applied": {"can_resume": True, "resumed": resumed, "verified_live": False}}

    def delete(self, artifact_id: int) -> dict[str, Any]:
        artifact = self.get_active(artifact_id)
        base_resume_id = (artifact.metadata_json or {}).get("base_resume_id") if isinstance(artifact.metadata_json, dict) else None
        deleted_files: list[str] = []
        if base_resume_id:
            # A deleted résumé stops being the one attached to job forms, and its copy and text go with it.
            base = self.db.get(models.Resume, int(base_resume_id))
            if base is not None and base.user_id == artifact.user_id:
                base.is_base = False
                base.base_resume_text = ""
                for copy in self.db.scalars(
                    select(models.ResumeArtifact).where(models.ResumeArtifact.resume_id == base.id)
                ).all():
                    copy_path = self._safe_existing_path(copy.pdf_path) if copy.pdf_path else None
                    if copy_path and copy_path.exists():
                        copy_path.unlink()
                        deleted_files.append(str(copy_path))
                    copy.compile_status = "DELETED"
                    copy.pdf_path = None
                    copy.source_path = None
                    copy.file_size = 0
        if artifact.storage_path:
            path = self._safe_existing_path(artifact.storage_path)
            if path and path.exists():
                path.unlink()
                deleted_files.append(str(path))
        artifact.status = "deleted"
        artifact.deleted_at = models.utcnow()
        artifact.storage_path = None
        artifact.file_size = 0
        artifact.updated_at = models.utcnow()
        self.db.flush()
        return {"deleted": True, "id": artifact.id, "deleted_files": deleted_files}

    def update_text(self, artifact_id: int, *, text_content: str) -> models.ApplicationArtifact:
        artifact = self.get_active(artifact_id)
        text = str(text_content or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="Artifact text cannot be empty.")
        artifact.text_content = text
        artifact.text_preview = re.sub(r"\s+", " ", text).strip()[:500]
        metadata = dict(artifact.metadata_json or {})
        metadata["edited_at"] = models.utcnow().isoformat()
        artifact.metadata_json = metadata
        path = Path(artifact.storage_path or "")
        if path.suffix.lower() == ".docx":
            safe_path = self._safe_existing_path(str(path))
            if safe_path:
                self._write_docx(safe_path, title=Path(artifact.filename or "Application document").stem, body=text)
                artifact.file_size = safe_path.stat().st_size
        artifact.updated_at = models.utcnow()
        self.db.flush()
        return artifact

    def _create_user_artifact_row(
        self,
        *,
        user_id: int,
        kind: str,
        source: str,
        filename: str,
        mime_type: str,
        text_content: str,
        metadata: dict[str, Any],
        status: str = "active",
    ) -> models.ApplicationArtifact:
        text_preview = re.sub(r"\s+", " ", text_content or "").strip()[:500]
        artifact = models.ApplicationArtifact(
            user_id=user_id,
            run_id=None,
            application_id=None,
            job_posting_id=None,
            artifact_kind=normalize_artifact_role(kind, default="artifact"),
            source=source,
            filename=filename,
            mime_type=mime_type,
            storage_path=None,
            text_content=text_content,
            text_preview=text_preview,
            status=status,
            metadata_json=metadata,
        )
        self.db.add(artifact)
        self.db.flush()
        return artifact

    def _question_task(self, question_id: int) -> tuple[models.OperatorQuestion, models.OperatorRun, models.RuntimeTask]:
        question = self.db.get(models.OperatorQuestion, question_id)
        if not question or question.status != "pending" or question.question_type != "field_approval":
            raise HTTPException(status_code=404, detail="Pending document question not found.")
        run = self.db.get(models.OperatorRun, question.run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found.")
        task_ids = [int(item) for item in ((question.expected_answer_schema or {}).get("task_ids") or [])]
        task = self.db.scalars(
            select(models.RuntimeTask).where(
                models.RuntimeTask.run_id == run.id,
                models.RuntimeTask.task_type == "field_approval",
                models.RuntimeTask.status == "pending",
                models.RuntimeTask.id.in_(task_ids),
            )
        ).first()
        if not task:
            raise HTTPException(status_code=404, detail="Pending field task not found.")
        return question, run, task

    def _create_artifact_row(
        self,
        *,
        run: models.OperatorRun,
        kind: str,
        source: str,
        filename: str,
        mime_type: str,
        text_content: str,
        metadata: dict[str, Any],
        status: str = "active",
    ) -> models.ApplicationArtifact:
        application = self.db.get(models.Application, run.application_id) if run.application_id else None
        text_preview = re.sub(r"\s+", " ", text_content or "").strip()[:500]
        artifact = models.ApplicationArtifact(
            user_id=run.user_id,
            run_id=run.id,
            application_id=run.application_id,
            job_posting_id=application.job_posting_id if application else None,
            artifact_kind=kind,
            source=source,
            filename=filename,
            mime_type=mime_type,
            storage_path=None,
            text_content=text_content,
            text_preview=text_preview,
            status=status,
            metadata_json=metadata,
        )
        self.db.add(artifact)
        self.db.flush()
        return artifact

    def _artifact_path(self, artifact: models.ApplicationArtifact, filename: str) -> Path:
        root = self.settings.artifact_root / "application-artifacts" / str(artifact.user_id) / str(artifact.id)
        root.mkdir(parents=True, exist_ok=True)
        return root / safe_artifact_filename(filename, default=f"artifact-{artifact.id}")

    def _draft_dir(self, *, user_id: int, draft_id: str, create: bool) -> Path:
        safe_id = re.sub(r"[^A-Za-z0-9_-]+", "", str(draft_id or ""))
        if not safe_id:
            raise HTTPException(status_code=400, detail="Artifact preview draft id is required.")
        root = (self.settings.artifact_root / "application-artifact-drafts" / str(user_id)).resolve()
        path = (root / safe_id).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid artifact preview draft id.") from exc
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def _load_preview(self, *, user_id: int, draft_id: str) -> dict[str, Any]:
        path = self._draft_dir(user_id=user_id, draft_id=draft_id, create=False) / "preview.json"
        if not path.exists():
            raise HTTPException(status_code=404, detail="Artifact preview draft not found.")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="Artifact preview draft is unreadable.") from exc
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="Artifact preview draft is invalid.")
        return data

    def _safe_draft_file(self, *, user_id: int, draft_id: str, filename: str) -> Path:
        draft_dir = self._draft_dir(user_id=user_id, draft_id=draft_id, create=False)
        path = (draft_dir / safe_artifact_filename(filename, default="artifact")).resolve()
        try:
            path.relative_to(draft_dir)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid artifact preview file path.") from exc
        if not path.exists():
            raise HTTPException(status_code=404, detail="Artifact preview file is missing.")
        return path

    def _safe_existing_path(self, raw_path: str) -> Path | None:
        root = self.settings.artifact_root.resolve()
        path = Path(raw_path).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Refusing to access artifact outside artifact root: {path}") from exc
        return path

    def _task_document_kind(self, task: models.RuntimeTask) -> str:
        payload = task.payload or {}
        capability = payload.get("document_capability") if isinstance(payload.get("document_capability"), dict) else {}
        return normalize_artifact_role(capability.get("artifact_role") or capability.get("document_kind") or payload.get("document_kind") or payload.get("upload_file_type") or "", default="")

    def _extract_artifact_text(self, *, filename: str, content: bytes, mime_type: str) -> tuple[str, str, list[str]]:
        ext = Path(filename or "").suffix.lower()
        warnings: list[str] = []
        if ext in {".pdf", ".doc", ".docx"}:
            text = extract_resume_text(content, filename)
            if text:
                return text, "text_extracted", warnings
            warnings.append("No text could be extracted from this document.")
            return "", "no_text_extracted", warnings
        if ext in TEXT_ARTIFACT_EXTENSIONS or str(mime_type or "").startswith("text/"):
            try:
                text = content.decode("utf-8", errors="ignore").strip()
            except Exception:
                text = ""
            if text:
                return text, "text_extracted", warnings
            warnings.append("No text could be decoded from this file.")
            return "", "no_text_extracted", warnings
        if ext in IMAGE_ARTIFACT_EXTENSIONS or str(mime_type or "").startswith("image/"):
            return "", "image_requires_vision", warnings
        return "", "unsupported_text_extraction", warnings

    def _image_payload(self, *, filename: str, content: bytes, mime_type: str) -> dict[str, Any] | None:
        ext = Path(filename or "").suffix.lower()
        if ext not in IMAGE_ARTIFACT_EXTENSIONS and not str(mime_type or "").startswith("image/"):
            return None
        return {
            "media_type": mime_type if str(mime_type or "").startswith("image/") else mimetypes.guess_type(filename)[0] or "image/jpeg",
            "data": base64.b64encode(content).decode("ascii"),
        }

    def _text_preview(self, value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()[:500]

    def _normalized_key_facts(self, value: Any) -> list[dict[str, str]]:
        facts: list[dict[str, str]] = []
        for item in list(value or [])[:12]:
            if not isinstance(item, dict):
                continue
            label = re.sub(r"\s+", " ", str(item.get("label") or "")).strip()[:80]
            fact_value = re.sub(r"\s+", " ", str(item.get("value") or "")).strip()[:220]
            if not label or not fact_value:
                continue
            facts.append({"label": label, "value": fact_value, "evidence": re.sub(r"\s+", " ", str(item.get("evidence") or "")).strip()[:180]})
        return facts

    def _confidence(self, value: Any) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return 0.0

    def _public_preview(self, preview: dict[str, Any]) -> dict[str, Any]:
        return {key: preview.get(key) for key in ARTIFACT_PREVIEW_PUBLIC_KEYS}

    def _generation_context(self, run: models.OperatorRun, *, task: models.RuntimeTask, instructions: str | None, artifact_kind: str) -> dict[str, Any]:
        application = self.db.get(models.Application, run.application_id) if run.application_id else None
        posting = self.db.get(models.JobPosting, application.job_posting_id) if application and application.job_posting_id else None
        resume = active_base_resume(self.db, run.user_id)
        user = self.db.get(models.User, run.user_id)
        payload = task.payload or {}
        return {
            "artifact_kind": artifact_kind,
            "instructions": instructions or "",
            "field": {
                "label": payload.get("label"),
                "observed_label": payload.get("observed_label"),
                "control_kind": payload.get("control_kind"),
            },
            "candidate": {
                "name": user.name if user else "",
                "resume_text": (resume.base_resume_text if resume else "")[:6000],
            },
            "job": {
                "company": posting.company if posting else "",
                "title": posting.title if posting else "",
                "description": (posting.description_text if posting else "")[:6000],
                "url": posting.url if posting else run.target_url,
            },
        }

    def _write_docx(self, path: Path, *, title: str, body: str) -> None:
        try:
            import docx
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail="python-docx is required to generate DOCX artifacts.") from exc
        document = docx.Document()
        document.add_heading(title or "Application document", level=1)
        for block in re.split(r"\n\s*\n", body.strip()):
            document.add_paragraph(block.strip())
        document.save(path)

    def _emit_artifact_receipt(self, run: models.OperatorRun, *, artifact: models.ApplicationArtifact, task: models.RuntimeTask, use_file: bool) -> None:
        receipt = make_receipt(
            operation_id=f"artifact_apply:{artifact.id}",
            type="artifact_apply",
            target={"artifact_id": artifact.id, "task_id": task.id, "mode": "upload_file" if use_file else "artifact_text"},
            attempt={"status": "attempted"},
            execution={"status": "succeeded", "mode": "approved_value_stored_for_fresh_browser_observation"},
            verification={"status": "inconclusive", "reason": "live_browser_field_verification_requires_resume_observation"},
        )
        from app.event_store import emit_event

        emit_event(self.db, event_type="OPERATION_RECEIPT", payload=receipt, run_id=run.id, application_id=run.application_id, actor="artifact", confidence=1.0)
