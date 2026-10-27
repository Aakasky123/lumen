"""Document fields in the in-browser path + parked-card repair (Layers A & B).

The cover-letter card stayed a plain text box because the in-browser ask stripped the field to a thin
dict before classification, and a question parked with a stale (reply_text-only) schema never refreshed.
Layer A forwards the real control metadata so the document classifier runs; Layer B rebuilds a pending
field card's schema from its live task payload (and re-classifies a legacy thin task once) on read/resume.
"""

import uuid

from sqlalchemy import select

from app import models
from app.lumen_model import LumenModelClient
from app.cognition import OperatorChatService
from app.db import SessionLocal, init_db
from app.extension_run import _create_in_browser_question
from app.runtime import OperatorRuntimeEngine


def _cover_letter_capability(payload):
    # The model-owned classifier's verdict for a cover-letter field (mocked so CI doesn't need the live
    # model). Captured payload is asserted to prove the REAL control metadata reached classification.
    _cover_letter_capability.seen.append(dict(payload))
    return {
        "is_document_request": True,
        "document_kind": "cover_letter",
        "target_control_kind": "textarea",
        "accepted_file_extensions": ["pdf", "docx"],
        "allow_upload": True,
        "allow_generate": True,
        "allow_existing_artifact": True,
        "confidence": 0.9,
        "rationale": "cover letter",
    }


_cover_letter_capability.seen = []


def _fixture(db):
    suffix = uuid.uuid4().hex[:8]
    # @example.com -> a placeholder email (see OperatorRuntimeEngine._is_placeholder_email) so these test
    # users are excluded from ensure_default_user and don't shift other tests' default-user resolution.
    user = models.User(email=f"cover-{suffix}@example.com", name="Cover User")
    db.add(user)
    db.flush()
    run = models.OperatorRun(
        user_id=user.id,
        goal="Apply",
        target_url="https://ats.rippling.com/x/apply?step=application",
        # RUNNING (not WAITING_FOR_USER) so ensure_question_for_run does not fire StuckRunLearning, which
        # would persist a global lesson and pollute other tests' runs. The card behaviour under test is
        # independent of the run's lifecycle state.
        current_state="RUNNING",
        confidence_state={"end_to_end": {}},
        status_message="Waiting for your answer: Cover letter",
    )
    db.add(run)
    db.flush()
    thread = models.ChatThread(user_id=user.id, run_id=run.id, title="t", mode="run", status="active")
    db.add(thread)
    db.flush()
    return user, run, thread


def test_document_capability_schema_is_wrapped_for_openai_provider(monkeypatch) -> None:
    # Regression: classify_document_field_capability passed a BARE json-schema, but the OpenAI Responses
    # provider reads schema["name"]/schema["schema"] -> KeyError 'name' -> silent fallback to "not a
    # document" (the reply_text-only cover-letter card). Lock the {name, schema} wrapper in place.
    init_db()
    captured = {}

    def _fake_call(self, *, purpose, system, prompt, schema, provider=None):
        captured["schema"] = schema
        return {"ok": True, "data": {"is_document_request": True, "document_kind": "cover_letter", "target_control_kind": "textarea", "accepted_file_extensions": [], "allow_upload": False, "allow_generate": True, "allow_existing_artifact": True, "confidence": 0.95, "rationale": "x"}, "metadata": {}}

    monkeypatch.setattr(LumenModelClient, "live_enabled", property(lambda self: True))
    monkeypatch.setattr(LumenModelClient, "_call_model_json", _fake_call)
    with SessionLocal() as db:
        LumenModelClient(db).classify_document_field_capability({"observed_label": "Cover letter", "control_kind": "textarea"})
    assert "name" in captured["schema"] and "schema" in captured["schema"]
    assert captured["schema"]["schema"]["type"] == "object"


# --- Layer A: the in-browser ask reaches document classification with real control metadata -------------


def test_in_browser_cover_letter_exposes_document_modes(monkeypatch) -> None:
    init_db()
    monkeypatch.setattr(
        "app.lumen_model.LumenModelClient.classify_document_field_capability",
        lambda self, payload: _cover_letter_capability(payload),
    )
    _cover_letter_capability.seen.clear()
    with SessionLocal() as db:
        _, run, _thread = _fixture(db)
        run_id = int(run.id)
        db.commit()

    # The enriched ask _build_ask now produces (control_kind/refs carried, not a thin dict).
    field = {
        "node_id": "cl-1",
        "snapshot_id": "snap-1",
        "label": "Cover letter",
        "options": [],
        "option_refs": [],
        "is_choice": False,
        "required": True,
        "role": "textarea",
        "control_kind": "textarea",
        "input_type": "textarea",
        "value_state": "empty",
        "field_ref": {"selector": "#cover", "label": "Cover letter"},
        "element_ref": {"selector": "#cover", "role": "textarea"},
        "locator_ref": {"selector": "#cover"},
        "selector": "#cover",
        "observation_id": "snap-1",
    }
    _create_in_browser_question(run_id, field)

    with SessionLocal() as db:
        task = db.scalars(
            select(models.RuntimeTask).where(
                models.RuntimeTask.run_id == run_id,
                models.RuntimeTask.task_type == "field_approval",
            )
        ).first()
        payload = dict(task.payload or {})
    modes = payload.get("answer_modes") or []
    assert "generate_artifact" in modes  # cover letter can be drafted...
    assert "upload_file" in modes  # ...or uploaded...
    assert "choose_artifact" not in modes  # no saved cover-letter artifact exists yet
    # The classifier saw the REAL control, not the "field" fallback — proof the enrichment flowed through.
    assert _cover_letter_capability.seen, "document classifier must run on the in-browser path"
    assert _cover_letter_capability.seen[-1].get("control_kind") == "textarea"


# --- Layer B: a parked field card refreshes from its live task payload on read/resume -------------------


def _stale_field_task(db, run, *, answer_modes, document_capability=None):
    payload = {
        "field_key": "cover_letter",
        "label": "Cover letter",
        "observed_label": "Cover letter",
        "target": {"node_id": "cl-1", "snapshot_id": "snap-1"},
        "field_ref": {"run_id": run.id, "field_key": "cover_letter", "node_id": "cl-1", "snapshot_id": "snap-1", "selector": "#cover"},
        "element_ref": {"node_id": "cl-1", "snapshot_id": "snap-1", "selector": "#cover", "role": "textarea"},
        "control_kind": "textarea",
        "observation_id": "snap-1",
        "snapshot_id": "snap-1",
        "options": [],
        "visible_options": [],
        "field_binding": {"control_kind": "textarea", "observed_label": "Cover letter", "field_ref": {"selector": "#cover"}, "element_ref": {"selector": "#cover", "role": "textarea"}},
        "binding_key": "bind-cover-1",
        "is_choice": False,
        "required": True,
        "answer_modes": answer_modes,
        "document_capability": document_capability,
        "confidence": 0.0,
    }
    task = models.RuntimeTask(run_id=run.id, name="Answer required field: Cover letter", status="pending", task_type="field_approval", payload=payload)
    db.add(task)
    db.flush()
    return task


def _parked_question(db, run, thread, task):
    field = OperatorChatService(db)._field_task_schema(task)
    question = models.OperatorQuestion(
        run_id=run.id,
        thread_id=thread.id,
        question_type="field_approval",
        stuck_phase="APPLICATION_EXECUTION",
        stuck_reason="Field answer required.",
        prompt="Lumen needs a field answer.",
        expected_answer_schema={"task_ids": [task.id], "binding_key": field.get("binding_key"), "fields": [field]},
        status="pending",
    )
    db.add(question)
    db.flush()
    return question


def test_parked_reply_text_only_card_is_re_enriched_on_read(monkeypatch) -> None:
    # A legacy card parked as reply_text-only (no document capability) is re-classified ONCE and its
    # schema rebuilt, so the stuck cover-letter run gains its document controls without a new run.
    init_db()
    monkeypatch.setattr(
        "app.lumen_model.LumenModelClient.classify_document_field_capability",
        lambda self, payload: _cover_letter_capability(payload),
    )
    with SessionLocal() as db:
        _, run, thread = _fixture(db)
        task = _stale_field_task(db, run, answer_modes=["reply_text"], document_capability=None)
        question = _parked_question(db, run, thread, task)
        qid, tid, rid = int(question.id), int(task.id), int(run.id)
        db.commit()

    with SessionLocal() as db:
        OperatorChatService(db)._reconcile_stale_task_questions(rid)
        db.commit()

    with SessionLocal() as db:
        question = db.get(models.OperatorQuestion, qid)
        task = db.get(models.RuntimeTask, tid)
        fields = (question.expected_answer_schema or {}).get("fields") or []
        modes = (fields[0] if fields else {}).get("answer_modes") or []
        assert "generate_artifact" in modes and "upload_file" in modes
        assert (task.payload or {}).get("document_capability_refreshed") is True
        assert "generate_artifact" in ((task.payload or {}).get("answer_modes") or [])


def test_required_file_cover_letter_never_degrades_to_reply_text_only(monkeypatch) -> None:
    init_db()

    def _bad_classifier(self, payload):
        return {
            "is_document_request": True,
            "document_kind": "cover_letter",
            "target_control_kind": "file_input",
            "accepted_file_extensions": ["pdf"],
            "allow_upload": False,
            "allow_generate": False,
            "allow_existing_artifact": False,
            "confidence": 0.91,
            "rationale": "recognized cover letter but no capability flags",
        }

    monkeypatch.setattr("app.lumen_model.LumenModelClient.classify_document_field_capability", _bad_classifier)
    with SessionLocal() as db:
        _, run, _thread = _fixture(db)
        payload = OperatorRuntimeEngine(db)._field_approval_answer_mode_payload(
            run,
            field={
                "node_id": "cover-file",
                "label": "Cover letter",
                "resolved_label": "Cover letter",
                "role": "file_input",
                "control_kind": "file_input",
                "input_type": "file",
                "required": True,
            },
            binding={"control_kind": "file_input", "observed_label": "Cover letter", "element_ref": {"selector": "#cover-file"}},
            is_choice=False,
            visible_options=[],
        )

    assert "upload_file" in payload["answer_modes"]
    assert "generate_artifact" in payload["answer_modes"]
    assert payload["answer_modes"] != ["reply_text"]
    assert payload["document_capability"]["artifact_role"] == "cover_letter"


def test_document_artifact_role_preserves_model_owned_roles() -> None:
    init_db()
    with SessionLocal() as db:
        engine = OperatorRuntimeEngine(db)

        role = engine._document_artifact_role({"document_kind": "Vendor NDA"}, field={})

    assert role == "vendor_nda"


def test_schema_refresh_picks_up_task_modes_without_a_model_call(monkeypatch) -> None:
    # When the loop already enriched the TASK (a document, with document modes) but the QUESTION schema
    # was frozen earlier, the refresh mirrors the task — and never spends a classifier call to do it.
    init_db()

    def _boom(self, payload):  # the classifier must NOT be invoked for an already-document task
        raise AssertionError("classifier should not run when the task already exposes document modes")

    monkeypatch.setattr("app.lumen_model.LumenModelClient.classify_document_field_capability", _boom)
    with SessionLocal() as db:
        _, run, thread = _fixture(db)
        # Task already carries document modes + capability; question was created before that.
        task = _stale_field_task(
            db,
            run,
            answer_modes=["reply_text", "upload_file", "generate_artifact", "choose_artifact"],
            document_capability={"is_document_request": True, "document_kind": "cover_letter"},
        )
        # Freeze the question with an OUTDATED reply_text-only field schema.
        stale_field = OperatorChatService(db)._field_task_schema(task)
        stale_field = {**stale_field, "answer_modes": ["reply_text"]}
        question = models.OperatorQuestion(
            run_id=run.id,
            thread_id=thread.id,
            question_type="field_approval",
            stuck_phase="APPLICATION_EXECUTION",
            stuck_reason="Field answer required.",
            prompt="Lumen needs a field answer.",
            expected_answer_schema={"task_ids": [task.id], "fields": [stale_field]},
            status="pending",
        )
        db.add(question)
        db.flush()
        qid = int(question.id)
        rid = int(run.id)
        db.commit()

    with SessionLocal() as db:
        OperatorChatService(db)._reconcile_stale_task_questions(rid)
        db.commit()

    with SessionLocal() as db:
        question = db.get(models.OperatorQuestion, qid)
        modes = ((question.expected_answer_schema or {}).get("fields") or [{}])[0].get("answer_modes") or []
        assert "generate_artifact" in modes and "upload_file" in modes
