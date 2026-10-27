from __future__ import annotations

import asyncio
import io
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile

from app import models
from app.application_artifacts import ApplicationArtifactService
from app.cognition import OperatorChatService
from app.db import SessionLocal, init_db
from app.runtime import OperatorRuntimeEngine


def _run_fixture(db):
    suffix = uuid4().hex[:8]
    user = models.User(email=f"artifact-{suffix}@example.com", name="Artifact User")
    db.add(user)
    db.flush()
    posting = models.JobPosting(
        user_id=user.id,
        url=f"https://example.test/jobs/{suffix}",
        company="Example",
        title="Engineer",
        description_text="Build reliable developer tools and production workflows.",
        source_platform="generic",
    )
    db.add(posting)
    db.flush()
    application = models.Application(user_id=user.id, job_posting_id=posting.id, status="draft", current_step="test")
    db.add(application)
    db.flush()
    resume = models.Resume(user_id=user.id, name="Base Resume", base_resume_text="Software engineer with production AI tooling experience.", is_base=True)
    db.add(resume)
    db.flush()
    run = models.OperatorRun(
        user_id=user.id,
        application_id=application.id,
        goal="Apply to Example",
        target_url=posting.url,
        current_state="WAITING_FOR_USER",
        confidence_state={"end_to_end": {}},
        status_message="Waiting for document.",
    )
    db.add(run)
    db.flush()
    thread = models.ChatThread(user_id=user.id, run_id=run.id, title="Artifact thread", mode="run", status="active")
    db.add(thread)
    db.flush()
    return user, run, thread


def _document_task(db, run, *, label="Cover letter", control_kind="file_input", modes=None, document_kind="cover_letter"):
    modes = modes or ["reply_text", "upload_file", "generate_artifact", "choose_artifact"]
    payload = {
        "field_key": "cover_letter",
        "label": label,
        "observed_label": label,
        "target": {"node_id": "cover-letter", "snapshot_id": "snap-doc"},
        "field_ref": {"run_id": run.id, "field_key": "cover_letter", "node_id": "cover-letter", "snapshot_id": "snap-doc"},
        "element_ref": {"node_id": "cover-letter", "snapshot_id": "snap-doc", "selector": "#cover-letter", "role": control_kind},
        "control_kind": control_kind,
        "observation_id": "snap-doc",
        "snapshot_id": "snap-doc",
        "answer_modes": modes,
        "document_capability": {
            "is_document_request": True,
            "document_kind": document_kind,
            "target_control_kind": control_kind,
            "accepted_file_extensions": ["pdf", "docx"],
            "allow_upload": True,
            "allow_generate": True,
            "allow_existing_artifact": True,
            "confidence": 0.91,
            "source": "test",
        },
        "accepted_file_extensions": ["pdf", "docx"],
        "required": True,
    }
    task = models.RuntimeTask(run_id=run.id, name=f"Answer field: {label}", status="pending", task_type="field_approval", payload=payload)
    db.add(task)
    db.flush()
    return task


def _question(db, run, thread, task):
    service = OperatorChatService(db)
    field = service._field_task_schema(task)
    question = models.OperatorQuestion(
        run_id=run.id,
        thread_id=thread.id,
        question_type="field_approval",
        stuck_phase="APPLICATION_EXECUTION",
        stuck_reason="Document required.",
        prompt="Lumen needs a document.",
        expected_answer_schema={"task_ids": [task.id], "binding_key": field.get("binding_key"), "fields": [field]},
        status="pending",
    )
    db.add(question)
    db.flush()
    return question


def test_document_pending_question_exposes_artifact_answer_modes() -> None:
    init_db()
    with SessionLocal() as db:
        _, run, thread = _run_fixture(db)
        existing = models.ApplicationArtifact(
            user_id=run.user_id,
            run_id=run.id,
            application_id=run.application_id,
            artifact_kind="cover_letter",
            source="generated",
            filename="Existing.docx",
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            text_content="Existing draft",
            text_preview="Existing draft",
            status="active",
            metadata_json={},
        )
        db.add(existing)
        db.flush()
        task = _document_task(db, run)
        question = _question(db, run, thread, task)
        field = question.expected_answer_schema["fields"][0]

        assert field["answer_modes"] == ["reply_text", "upload_file", "generate_artifact", "choose_artifact"]
        assert field["document_capability"]["document_kind"] == "cover_letter"
        assert field["artifact_options"][0]["id"] == existing.id


def test_artifact_preview_and_commit_store_generic_llm_metadata(monkeypatch) -> None:
    init_db()

    def _understand(self, payload, *, image=None):
        return {
            "suggested_artifact_kind": "model_selected_artifact",
            "suggested_display_name": "Model Selected Artifact",
            "summary": "A short generic summary.",
            "key_facts": [{"label": "Identifier", "value": "ABC-123", "evidence": "text"}],
            "confidence": 0.82,
            "warnings": ["Review extracted facts before use."],
            "metadata": {"provider": "test-provider", "model": "test-model"},
        }

    monkeypatch.setattr("app.lumen_model.LumenModelClient.understand_artifact", _understand)
    with SessionLocal() as db:
        user, _run, _thread = _run_fixture(db)
        service = ApplicationArtifactService(db)
        upload = UploadFile(filename="artifact.txt", file=io.BytesIO(b"Identifier: ABC-123"))

        preview = asyncio.run(service.preview_upload(user_id=user.id, file=upload))
        artifact = service.commit_preview(user_id=user.id, draft_id=preview["draft_id"], selected_type="resume", display_name="")

        assert preview["draft_id"]
        assert preview["suggested_artifact_kind"] == "model_selected_artifact"
        assert preview["suggested_display_name"] == "Model Selected Artifact"
        assert "Identifier: ABC-123" in preview["extracted_text_preview"]
        assert artifact.artifact_kind == "resume"
        assert artifact.text_content == "Identifier: ABC-123"
        assert artifact.metadata_json["selected_artifact_kind"] == "resume"
        assert artifact.metadata_json["llm_suggested_artifact_kind"] == "model_selected_artifact"
        assert artifact.metadata_json["key_facts"][0]["value"] == "ABC-123"
        assert artifact.storage_path and Path(artifact.storage_path).exists()


def test_artifact_commit_accepts_custom_and_suggested_types(monkeypatch) -> None:
    init_db()

    def _understand(self, payload, *, image=None):
        return {
            "suggested_artifact_kind": "model_owned_role",
            "suggested_display_name": "Model Owned Role",
            "summary": "Generic role suggestion.",
            "key_facts": [],
            "confidence": 0.74,
            "warnings": [],
            "metadata": {"provider": "test-provider", "model": "test-model"},
        }

    monkeypatch.setattr("app.lumen_model.LumenModelClient.understand_artifact", _understand)
    with SessionLocal() as db:
        user, _run, _thread = _run_fixture(db)
        service = ApplicationArtifactService(db)
        custom_preview = asyncio.run(service.preview_upload(user_id=user.id, file=UploadFile(filename="custom.txt", file=io.BytesIO(b"custom"))))
        suggested_preview = asyncio.run(service.preview_upload(user_id=user.id, file=UploadFile(filename="suggested.txt", file=io.BytesIO(b"suggested"))))

        custom = service.commit_preview(user_id=user.id, draft_id=custom_preview["draft_id"], selected_type="Vendor NDA", display_name="Vendor NDA")
        suggested = service.commit_preview(
            user_id=user.id,
            draft_id=suggested_preview["draft_id"],
            selected_type=suggested_preview["suggested_artifact_kind"],
            display_name="",
        )

        assert custom.artifact_kind == "vendor_nda"
        assert custom.metadata_json["selected_artifact_kind"] == "vendor_nda"
        assert suggested.artifact_kind == "model_owned_role"
        assert suggested.metadata_json["selected_artifact_kind"] == "model_owned_role"


def test_image_artifact_preview_requires_vision_provider(monkeypatch) -> None:
    init_db()
    monkeypatch.setattr("app.model_provider.ModelProviderService.vision_provider", lambda *args, **kwargs: None)
    with SessionLocal() as db:
        user, _run, _thread = _run_fixture(db)
        upload = UploadFile(filename="artifact.png", file=io.BytesIO(b"\x89PNG\r\n\x1a\n"))

        with pytest.raises(HTTPException) as exc:
            asyncio.run(ApplicationArtifactService(db).preview_upload(user_id=user.id, file=upload))

        assert exc.value.status_code == 400
        assert "vision-capable" in str(exc.value.detail)


def test_document_artifact_options_support_arbitrary_model_owned_roles() -> None:
    init_db()
    with SessionLocal() as db:
        _, run, thread = _run_fixture(db)
        existing = models.ApplicationArtifact(
            user_id=run.user_id,
            run_id=run.id,
            application_id=run.application_id,
            artifact_kind="custom_clearance",
            source="uploaded",
            filename="clearance-scan.pdf",
            mime_type="application/pdf",
            text_content="",
            text_preview="",
            status="active",
            metadata_json={"display_name": "Custom Clearance", "artifact_role": "custom_clearance"},
        )
        db.add(existing)
        db.flush()
        task = _document_task(db, run, label="Custom Clearance", document_kind="custom_clearance")
        question = _question(db, run, thread, task)
        field = question.expected_answer_schema["fields"][0]

        assert field["document_capability"]["document_kind"] == "custom_clearance"
        assert field["artifact_options"][0]["id"] == existing.id
        assert field["artifact_options"][0]["display_name"] == "Custom Clearance"


def test_generated_cover_letter_is_saved_as_preview_first_artifact() -> None:
    init_db()
    with SessionLocal() as db:
        _, run, thread = _run_fixture(db)
        task = _document_task(db, run, control_kind="textarea")
        question = _question(db, run, thread, task)

        result = ApplicationArtifactService(db).generate_for_question(question_id=question.id)

        artifact = result["artifact"]
        db.refresh(task)
        db.refresh(question)
        assert artifact.status == "draft"
        assert artifact.source == "generated"
        assert artifact.artifact_kind == "cover_letter"
        assert artifact.storage_path and artifact.storage_path.endswith(".docx")
        assert artifact.text_preview
        assert task.status == "pending"
        assert question.status == "pending"


def test_generated_artifact_requires_apply_confirmation_before_task_approval() -> None:
    init_db()
    with SessionLocal() as db:
        _, run, thread = _run_fixture(db)
        task = _document_task(db, run, control_kind="textarea")
        question = _question(db, run, thread, task)
        generated = ApplicationArtifactService(db).generate_for_question(question_id=question.id)["artifact"]

        db.refresh(task)
        assert task.status == "pending"

        result = ApplicationArtifactService(db).apply_to_question(question_id=question.id, artifact_id=generated.id, auto_resume=False)
        db.refresh(task)
        db.refresh(question)
        assert task.status == "approved"
        assert (task.payload or {}).get("answer_mode") == "artifact_text"
        assert question.status == "applied"
        assert result["applied"]["verified_live"] is False


def test_uploaded_artifact_applies_to_file_input_without_live_success_claim() -> None:
    init_db()
    with SessionLocal() as db:
        _, run, thread = _run_fixture(db)
        task = _document_task(db, run, control_kind="file_input")
        question = _question(db, run, thread, task)
        upload = UploadFile(filename="cover-letter.txt", file=io.BytesIO(b"Cover letter text"))

        result = asyncio.run(ApplicationArtifactService(db).upload_for_question(question_id=question.id, file=upload, auto_resume=False))
        artifact = result["artifact"]
        db.refresh(task)
        db.refresh(question)

        assert artifact.source == "uploaded"
        assert artifact.status == "active"
        assert task.status == "approved"
        assert (task.payload or {}).get("answer_mode") == "upload_file"
        assert (task.payload or {}).get("value_category") == "document_upload"
        assert question.status == "applied"
        assert result["applied"]["verified_live"] is False


def test_artifact_delete_hides_item_and_removes_file() -> None:
    init_db()
    with SessionLocal() as db:
        _, run, thread = _run_fixture(db)
        task = _document_task(db, run, control_kind="textarea")
        question = _question(db, run, thread, task)
        artifact = ApplicationArtifactService(db).generate_for_question(question_id=question.id)["artifact"]
        path = artifact.storage_path

        result = ApplicationArtifactService(db).delete(artifact.id)
        db.refresh(artifact)

        assert result["deleted"] is True
        assert artifact.status == "deleted"
        assert not ApplicationArtifactService(db).list_active(user_id=run.user_id)
        assert path in result["deleted_files"]
        assert path and not Path(path).exists()


def test_closed_required_select_with_hidden_options_requires_fresh_option_observation() -> None:
    init_db()
    with SessionLocal() as db:
        _, run, _ = _run_fixture(db)
        task_id = OperatorRuntimeEngine(db)._phase2_upsert_required_field_approval_task(
            run,
            field={
                "node_id": "llm-production",
                "snapshot_id": "snap-select",
                "label": "Have you built integrations with LLMs?",
                "role": "combobox",
                "is_choice": True,
                "options": [],
                "required": True,
            },
            source="test",
        )
        task = db.get(models.RuntimeTask, task_id)

        assert (task.payload or {}).get("needs_option_observation") is True
        assert (task.payload or {}).get("requires_fresh_observation") is True


def test_field_approval_task_uses_requiredness_projection_not_default_true() -> None:
    init_db()
    with SessionLocal() as db:
        _, run, thread = _run_fixture(db)
        task_id = OperatorRuntimeEngine(db)._phase2_upsert_required_field_approval_task(
            run,
            field={
                "node_id": "pronouns",
                "snapshot_id": "snap-pronouns",
                "label": "Pronouns",
                "role": "combobox",
                "is_choice": True,
                "options": [],
                "required": False,
                "requiredness": {
                    "state": "optional_likely",
                    "confidence": 0.82,
                    "evidence": ["same_form_uses_required_markers", "no_required_marker_near_label"],
                },
            },
            source="test",
        )
        task = db.get(models.RuntimeTask, task_id)
        field = OperatorChatService(db)._field_task_schema(task)

        assert (task.payload or {}).get("required") is False
        assert (task.payload or {}).get("requiredness")["state"] == "optional_likely"
        assert field["required"] is False
        assert field["requiredness"]["state"] == "optional_likely"
