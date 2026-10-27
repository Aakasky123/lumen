"""Release-blocking answer-to-browser continuation capability from real Chrome Run 442.

The fixture protects the orchestration outcome, not a historical website route. A pending bound
question may coexist briefly with a RUNNING lifecycle projection; applying its answer must still
dispatch exactly one continuation for the same extension-owned run.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app import models
from app.agent_eval.capability_regressions import ANSWERED_FIELD_RESUMES_BROWSER
from app.cognition import OperatorChatService
from app.db import SessionLocal, init_db
from app.runtime import OperatorRuntimeEngine


QUESTION = "Which option should be used for this required choice?"
ANSWER = "Second option"


def test_promoted_answer_dispatches_owned_browser_resume_from_running_projection(monkeypatch) -> None:
    init_db()
    scheduled: list[int] = []
    monkeypatch.setattr(
        "app.extension_run.schedule_resume_if_in_browser",
        lambda run_id: scheduled.append(run_id) or True,
    )
    monkeypatch.setattr(
        OperatorRuntimeEngine,
        "resume_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("An extension-owned run must not use the controlled-live resume path.")
        ),
    )

    with SessionLocal() as db:
        user = models.User(
            email=f"answer-resume-{uuid.uuid4().hex}@example.test",
            name="Answer Resume",
        )
        db.add(user)
        db.flush()
        application = models.Application(
            user_id=user.id,
            status="draft",
            current_step="fixture",
        )
        db.add(application)
        db.flush()
        run = models.OperatorRun(
            application_id=application.id,
            user_id=user.id,
            goal="Continue the browser workflow",
            target_url="https://fixture.test/workflow",
            current_state="RUNNING",
            confidence_state={
                "in_browser": {
                    "url": "https://fixture.test/workflow",
                    "device_id": "fixture-device",
                }
            },
        )
        db.add(run)
        db.flush()
        thread = models.ChatThread(
            user_id=user.id,
            run_id=run.id,
            title="Fixture workflow",
            mode="run",
            status="active",
        )
        db.add(thread)
        db.flush()
        task_id = OperatorRuntimeEngine(db)._phase2_upsert_required_field_approval_task(
            run,
            field={
                "node_id": "fixture-control",
                "snapshot_id": "fixture-snapshot",
                "label": QUESTION,
                "options": ["First option", ANSWER],
                "is_choice": True,
                "required": True,
                "role": "combobox",
                "control_kind": "combobox",
                "selector": "#fixture-control",
                "element_ref": {
                    "selector": "#fixture-control",
                    "node_id": "fixture-control",
                    "control_signature": "fixture-signature",
                },
            },
            source="in_browser_field",
        )
        db.commit()
        question = OperatorChatService(db).ensure_question_for_run(
            run.id,
            thread_id=thread.id,
            announce=False,
        )
        assert question is not None
        assert question.status == "pending"
        db.commit()

        result = OperatorChatService(db).answer_question(
            question_id=question.id,
            answer=ANSWER,
            payload={
                "task_ids": [task_id],
                "selected_option": ANSWER,
                "save_scope": "one_time",
            },
            auto_resume=True,
            record_user_message=False,
        )

        db.expire_all()
        stored_run = db.get(models.OperatorRun, run.id)
        stored_question = db.get(models.OperatorQuestion, question.id)
        stored_task = db.get(models.RuntimeTask, task_id)
        events = list(
            db.scalars(
                select(models.CognitiveEvent)
                .where(models.CognitiveEvent.run_id == run.id)
                .order_by(models.CognitiveEvent.id.asc())
            ).all()
        )

    assert result["resumed"] is True
    assert scheduled == [run.id]
    assert stored_run.current_state == ANSWERED_FIELD_RESUMES_BROWSER.required_final_state
    assert stored_question.status == "applied"
    assert stored_question.resumed_at is not None
    assert stored_task.status == "approved"
    assert stored_task.payload["approved_value"] == ANSWER
    receipts = [
        *(result["applied"].get("memory_receipts") or []),
        *(result["applied"].get("field_receipts") or []),
    ]
    assert ANSWERED_FIELD_RESUMES_BROWSER.missing_receipts(receipts) == []
    resume_events = [event for event in events if event.event_type == "OPERATOR_CHAT_RESUME_REQUESTED"]
    assert len(resume_events) == 1
    assert resume_events[0].event_payload["run_state_before_answer"] == "RUNNING"
    assert resume_events[0].event_payload["run_state_after_apply"] == "RUNNING"
    assert not any(event.event_type == "OPERATOR_CHAT_RESUME_BLOCKED" for event in events)
    assert not any(event.event_type == "APPLICATION_EXECUTION_RESUME_REQUESTED" for event in events)
    assert not any(receipt.get("verification", {}).get("final_action") for receipt in receipts)


def test_promoted_answer_resume_contract_is_complete() -> None:
    spec = ANSWERED_FIELD_RESUMES_BROWSER
    assert spec.release_blocking is True
    assert spec.source_proof["surface"] == "real_chrome"
    assert spec.source_proof["run_id"] == 442
    assert spec.initial_browser_state and spec.minimum_context
    assert spec.success_criteria and spec.forbidden_side_effects and spec.required_receipts
    assert spec.required_final_state == "RUNNING"
    assert set(spec.body_modes) == {"fixture", "real_chrome"}
    assert "resume-requested or resume-blocked event" in spec.failure_artifacts
