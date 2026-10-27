from __future__ import annotations

from uuid import uuid4

from app import models
from app.cognition import OperatorChatService
from app.db import SessionLocal, init_db
from app.operation_receipt import make_receipt
from app.runtime import OperatorRuntimeEngine


def _run_fixture(db):
    suffix = uuid4().hex[:8]
    user = models.User(email=f"pending-{suffix}@example.test", name="Pending User")
    db.add(user)
    db.flush()
    posting = models.JobPosting(
        user_id=user.id,
        url=f"https://example.test/jobs/{suffix}",
        company="Example",
        title="Engineer",
        source_platform="generic",
    )
    db.add(posting)
    db.flush()
    application = models.Application(user_id=user.id, job_posting_id=posting.id, status="draft", current_step="test")
    db.add(application)
    db.flush()
    run = models.OperatorRun(
        user_id=user.id,
        application_id=application.id,
        goal="Apply to Example",
        target_url=posting.url,
        current_state="WAITING_FOR_USER",
        confidence_state={"end_to_end": {}},
        status_message="Waiting for field answer.",
    )
    db.add(run)
    db.flush()
    thread = models.ChatThread(user_id=user.id, run_id=run.id, title="Test thread", mode="run", status="active")
    db.add(thread)
    db.flush()
    return user, run, thread


def _field_task(db, run, *, label="How many years of professional software engineering experience do you have?", options=None, binding_key="bind-field-1"):
    options = options or ["0", "1", "2", "3", "4", "5+"]
    payload = {
        "field_key": "experience_years",
        "label": label,
        "observed_label": label,
        "target": {"node_id": "field-years", "snapshot_id": "snap-1"},
        "field_ref": {"run_id": run.id, "field_key": "experience_years", "node_id": "field-years", "snapshot_id": "snap-1"},
        "element_ref": {"node_id": "field-years", "snapshot_id": "snap-1", "selector": "#years", "role": "combobox"},
        "control_kind": "combobox",
        "observation_id": "snap-1",
        "snapshot_id": "snap-1",
        "options": options,
        "visible_options": options,
        "option_refs": [{"label": option, "owner_ref": {"node_id": "field-years", "snapshot_id": "snap-1"}} for option in options],
        "options_binding_status": "matched",
        "binding_key": binding_key,
        "field_binding": {"binding_key": binding_key, "visible_options": options, "options_binding_status": "matched"},
        "is_choice": True,
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
        thread_id=thread.id if thread else None,
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


def test_typed_instruction_maps_to_visible_option_verbatim(monkeypatch) -> None:
    from app.lumen_model import LumenModelClient

    init_db()
    # The answer-fact judge keys the profile fact (tests run without a live model).
    monkeypatch.setattr(
        LumenModelClient,
        "extract_user_facts_from_answer",
        lambda self, payload: {"ok": True, "facts": [{"key": "years_of_experience", "value": "5+ years", "section": "work"}]},
    )
    with SessionLocal() as db:
        _, run, thread = _run_fixture(db)
        task = _field_task(db, run)
        question = _question(db, run, thread, task)

        result = OperatorChatService(db).answer_question(
            question_id=question.id,
            answer="use 5+ years and save it to my profile",
            payload={"save_scope": "profile"},
            auto_resume=False,
            record_user_message=False,
        )

        db.refresh(task)
        assert result["resolved_value"] == "5+"
        assert task.status == "approved"
        assert (task.payload or {}).get("approved_value") == "5+"
        assert any(
            receipt.get("type") == "memory_write"
            and receipt.get("outcome") == "verified_succeeded"
            and (receipt.get("target") or {}).get("scope") == "profile"
            for receipt in result["memory_receipts"]
        )


def test_pending_question_options_must_match_same_field_ref() -> None:
    init_db()
    with SessionLocal() as db:
        _, run, _ = _run_fixture(db)
        task_id = OperatorRuntimeEngine(db)._phase2_upsert_required_field_approval_task(
            run,
            field={
                "node_id": "field-a",
                "snapshot_id": "snap-1",
                "label": "Field A",
                "options": ["0", "1"],
                "option_refs": [
                    {"label": "0", "owner_ref": {"node_id": "field-b", "snapshot_id": "snap-1"}},
                    {"label": "1", "owner_ref": {"node_id": "field-b", "snapshot_id": "snap-1"}},
                ],
                "is_choice": True,
            },
            source="test",
        )
        task = db.get(models.RuntimeTask, task_id)

        assert (task.payload or {}).get("options") == []
        assert (task.payload or {}).get("visible_options") == []
        assert (task.payload or {}).get("options_binding_status") == "mismatched_reobserve_required"
        assert (task.payload or {}).get("requires_fresh_observation") is True


def test_field_approval_schema_exposes_field_backed_choice_contract() -> None:
    init_db()
    with SessionLocal() as db:
        _, run, _ = _run_fixture(db)
        label = "Have you built integrations with LLMs?"
        task = _field_task(db, run, label=label, options=["Yes", "No"], binding_key="bind-llm-choice")

        field = OperatorChatService(db)._field_task_schema(task)

        assert field["question_kind"] == "choice"
        assert field["display_title"] == label
        assert field["display_subtitle"].lower().startswith("required")
        assert field["display_reason"] == "Choose one of the live options observed from the form."
        assert field["option_snapshot_id"] == "bind-llm-choice"
        assert field["question_contract_valid"] is True
        assert field["question_contract_error"] is None


def test_grouped_choice_task_persists_option_snapshot() -> None:
    init_db()
    with SessionLocal() as db:
        _, run, _ = _run_fixture(db)
        task_id = OperatorRuntimeEngine(db)._phase2_upsert_required_field_approval_task(
            run,
            field={
                "node_id": "radiogroup:sms-yes|sms-no",
                "snapshot_id": "snap-sms",
                "label": "Consent to receiving text messages",
                "role": "radio_group",
                "options": [
                    "Yes - I consent to receiving text messages",
                    "No - I do not consent to receiving text messages",
                ],
                "option_refs": [
                    {"node_id": "sms-yes", "label": "Yes - I consent to receiving text messages", "snapshot_id": "snap-sms"},
                    {"node_id": "sms-no", "label": "No - I do not consent to receiving text messages", "snapshot_id": "snap-sms"},
                ],
                "is_choice": True,
                "required": True,
            },
            source="submit_enablement_scan",
        )
        task = db.get(models.RuntimeTask, task_id)
        payload = task.payload or {}
        snapshot = payload.get("choice_option_snapshot")

        assert payload["options"] == [
            "Yes - I consent to receiving text messages",
            "No - I do not consent to receiving text messages",
        ]
        assert payload["option_snapshot_id"]
        assert snapshot["snapshot_id"] == payload["option_snapshot_id"]
        assert snapshot["field_id"] == payload["field_id"]
        assert snapshot["control_kind"] == "radio_group"
        assert [option["label"] for option in snapshot["options"]] == payload["options"]


def test_field_approval_schema_marks_ambiguous_choice_contract_invalid() -> None:
    init_db()
    with SessionLocal() as db:
        _, run, _ = _run_fixture(db)
        task = _field_task(db, run, label="", options=["Yes", "No"], binding_key="bind-missing-label")

        field = OperatorChatService(db)._field_task_schema(task)

        assert field["question_kind"] == "choice"
        assert field["display_title"] == ""
        assert field["question_contract_valid"] is False
        assert "field label" in field["question_contract_error"]


def test_optionless_choice_task_is_internal_refresh_not_user_question() -> None:
    init_db()
    with SessionLocal() as db:
        _, run, thread = _run_fixture(db)
        task = models.RuntimeTask(
            run_id=run.id,
            name="Answer field: Consent",
            status="pending",
            task_type="field_approval",
            payload={
                "field_key": "radiogroup:sms",
                "label": "Consent to receiving text messages",
                "control_kind": "radio_group",
                "is_choice": True,
                "options": [],
                "visible_options": [],
                "answer_modes": ["reply_text"],
                "options_binding_status": "mismatched_reobserve_required",
                "requires_fresh_observation": True,
            },
        )
        db.add(task)
        db.flush()

        question = OperatorChatService(db).ensure_question_for_run(run.id, thread_id=thread.id)
        db.refresh(task)
        db.refresh(run)
        workspace = (run.confidence_state or {}).get("end_to_end") or {}

        assert question is None
        assert (task.payload or {}).get("answer_modes") == []
        assert (task.payload or {}).get("choice_question_state") == "reobserve_or_hydrate_required"
        assert workspace.get("requires_fresh_observation") is True


def test_typed_answer_to_stale_choice_queues_rehydrate_instead_of_rejecting() -> None:
    init_db()
    with SessionLocal() as db:
        _, run, thread = _run_fixture(db)
        task = models.RuntimeTask(
            run_id=run.id,
            name="Answer field: Consent",
            status="pending",
            task_type="field_approval",
            payload={
                "field_key": "radiogroup:sms",
                "label": "Consent to receiving text messages",
                "control_kind": "radio_group",
                "is_choice": True,
                "options": [],
                "visible_options": [],
                "answer_modes": ["reply_text"],
                "options_binding_status": "mismatched_reobserve_required",
                "requires_fresh_observation": True,
            },
        )
        db.add(task)
        db.flush()
        question = models.OperatorQuestion(
            run_id=run.id,
            thread_id=thread.id,
            question_type="field_approval",
            stuck_phase="APPLICATION_EXECUTION",
            stuck_reason="Field answer required.",
            prompt="Pick an option below.",
            expected_answer_schema={
                "task_ids": [task.id],
                "fields": [
                    {
                        "id": task.id,
                        "question_kind": "choice",
                        "label": "Consent to receiving text messages",
                        "options": [],
                        "is_choice": True,
                        "options_binding_status": "mismatched_reobserve_required",
                    }
                ],
            },
            status="pending",
        )
        db.add(question)
        db.flush()

        result = OperatorChatService(db).answer_question(
            question_id=question.id,
            answer="no",
            auto_resume=False,
            record_user_message=False,
        )
        db.refresh(task)

        assert result["applied"]["applied"] is False
        assert result["applied"]["choice_rehydrate_queued"] is True
        assert result["can_resume"] is True
        assert (task.payload or {}).get("pending_raw_user_answer") == "no"
        assert (task.payload or {}).get("choice_question_state") == "reobserve_or_hydrate_required"


def test_pending_raw_choice_answer_grounds_after_options_refresh() -> None:
    init_db()
    with SessionLocal() as db:
        _, run, thread = _run_fixture(db)
        task = models.RuntimeTask(
            run_id=run.id,
            name="Answer field: Consent",
            status="pending",
            task_type="field_approval",
            payload={
                "field_key": "radiogroup:sms",
                "label": "Consent to receiving text messages",
                "control_kind": "radio_group",
                "is_choice": True,
                "options": [
                    "Yes - I consent to receiving text messages",
                    "No - I do not consent to receiving text messages",
                ],
                "visible_options": [
                    "Yes - I consent to receiving text messages",
                    "No - I do not consent to receiving text messages",
                ],
                "answer_modes": ["choose_option", "reply_text"],
                "options_binding_status": "matched",
                "requires_fresh_observation": False,
                "pending_raw_user_answer": "no",
                "pending_answer_status": "awaiting_choice_option_refresh",
            },
        )
        db.add(task)
        db.flush()

        question = OperatorChatService(db).ensure_question_for_run(run.id, thread_id=thread.id)
        db.refresh(task)

        assert question is None
        assert task.status == "approved"
        assert (task.payload or {}).get("approved_value") == "No - I do not consent to receiving text messages"
        assert (task.payload or {}).get("pending_answer_status") == "grounded_after_choice_option_refresh"


def test_after_answer_previous_pending_question_is_cleared() -> None:
    init_db()
    with SessionLocal() as db:
        _, run, thread = _run_fixture(db)
        task = _field_task(db, run)
        question = _question(db, run, thread, task)
        other_task = _field_task(db, run, label="Another field", options=["A", "B"], binding_key="bind-field-2")
        other_question = _question(db, run, thread, other_task)

        OperatorChatService(db).answer_question(question_id=question.id, answer="5+", auto_resume=False, record_user_message=False)

        db.refresh(question)
        db.refresh(other_question)
        assert question.status == "applied"
        assert other_question.status == "superseded"


def test_new_pending_question_requires_fresh_observation_after_resume() -> None:
    init_db()
    with SessionLocal() as db:
        _, run, thread = _run_fixture(db)
        task = _field_task(db, run)
        question = _question(db, run, thread, task)

        OperatorChatService(db).answer_question(question_id=question.id, answer="5+", auto_resume=False, record_user_message=False)
        db.refresh(run)
        workspace = (run.confidence_state or {}).get("end_to_end") or {}

        assert workspace.get("requires_fresh_observation") is True
        assert workspace.get("requires_fresh_observation_reason") == "field_approval_answered"
        assert workspace.get("fresh_observation_after_task_ids") == [task.id]
        assert "pending_question_id" not in workspace


def test_active_dropdown_does_not_contaminate_next_field() -> None:
    init_db()
    with SessionLocal() as db:
        _, run, _ = _run_fixture(db)
        task_id = OperatorRuntimeEngine(db)._phase2_upsert_required_field_approval_task(
            run,
            field={
                "node_id": "field-salary",
                "snapshot_id": "snap-2",
                "label": "What is your expected compensation?",
                "options": ["0", "1", "2", "3", "4", "5+"],
                "option_refs": [
                    {"label": option, "owner_ref": {"node_id": "field-years", "snapshot_id": "snap-2"}}
                    for option in ["0", "1", "2", "3", "4", "5+"]
                ],
                "is_choice": True,
            },
            source="test",
        )
        task = db.get(models.RuntimeTask, task_id)

        assert (task.payload or {}).get("options") == []
        assert (task.payload or {}).get("requires_fresh_observation") is True


def test_no_applied_claim_without_field_verification() -> None:
    init_db()
    with SessionLocal() as db:
        _, run, thread = _run_fixture(db)
        task = _field_task(db, run)
        _question(db, run, thread, task)

        result = OperatorChatService(db)._try_answer_pending_question_from_chat(
            thread,
            "5+",
            payload={},
            chat_scope={"action": "answer_question"},
        )

        assert result is not None
        assert "I applied" not in result["assistant_text"]
        assert "I recorded" in result["assistant_text"]


def test_no_saved_claim_without_memory_receipt() -> None:
    init_db()
    with SessionLocal() as db:
        _, run, thread = _run_fixture(db)
        task = _field_task(db, run)
        _question(db, run, thread, task)
        service = OperatorChatService(db)

        def fake_answer_question(**_kwargs):
            return {
                "applied": {"applied": True, "target": "field_approval_tasks"},
                "run": run,
                "resolved_value": "5+",
                "confidence": 1.0,
                "rationale": "test",
                "can_resume": True,
                "resumed": False,
                "resume_error": None,
                "saved_common_answers": [],
                "memory_receipts": [],
                "field_receipts": [
                    make_receipt(
                        operation_id="field_value:test",
                        type="field_value",
                        target={"field": "experience_years"},
                        attempt={"status": "attempted"},
                        execution={"status": "succeeded"},
                        verification={"status": "succeeded"},
                    )
                ],
            }

        service.answer_question = fake_answer_question  # type: ignore[method-assign]
        result = service._try_answer_pending_question_from_chat(thread, "save it", payload={}, chat_scope={"action": "answer_question"})

        assert result is not None
        assert "saved it to your profile" not in result["assistant_text"]
        assert "remembered it" not in result["assistant_text"]
