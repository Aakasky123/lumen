import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from goal_test_helpers import create_operator_goal_response
from app.db import SessionLocal, init_db
from app.cognition import OperatorChatService
from app.main import app
from app import models
from app.runtime import OperatorRuntimeEngine
from app.state_machine import OperatorState


init_db()


def test_cancel_run_supersedes_pending_questions() -> None:
    with SessionLocal() as db:
        user = models.User(email=f"cancel-{uuid.uuid4().hex}@example.com", name="Cancel Test")
        db.add(user)
        db.flush()
        run = models.OperatorRun(
            user_id=user.id,
            goal="Cancel a waiting run",
            target_url="https://example.com/apply",
            current_state=OperatorState.WAITING_FOR_USER.value,
            status_message="Waiting for user",
            confidence_state={"end_to_end": {"phase": "APPLICATION_EXECUTION"}},
        )
        db.add(run)
        db.flush()
        pending_question = models.OperatorQuestion(
            run_id=run.id,
            question_type="blocked_reason",
            status="pending",
            prompt="Need input",
        )
        applied_question = models.OperatorQuestion(
            run_id=run.id,
            question_type="field_approval",
            status="applied",
            prompt="Already applied",
        )
        pending_task = models.RuntimeTask(
            run_id=run.id,
            name="Pending user input",
            status="pending",
            task_type="field_approval",
        )
        approved_task = models.RuntimeTask(
            run_id=run.id,
            name="Approved but not executed",
            status="approved",
            task_type="field_approval",
        )
        completed_task = models.RuntimeTask(
            run_id=run.id,
            name="Already completed",
            status="completed",
            task_type="field_approval",
        )
        db.add_all([pending_question, applied_question, pending_task, approved_task, completed_task])
        db.flush()
        run.confidence_state = {
            "end_to_end": {
                "phase": "APPLICATION_EXECUTION",
                "pending_question_id": pending_question.id,
                "pending_question_type": pending_question.question_type,
                "pending_question_reason": "Waiting for user",
            }
        }
        db.commit()
        run_id = run.id
        pending_question_id = pending_question.id
        applied_question_id = applied_question.id
        pending_task_id = pending_task.id
        approved_task_id = approved_task.id
        completed_task_id = completed_task.id

    with SessionLocal() as db:
        cancelled = OperatorRuntimeEngine(db).cancel_run(run_id, notify_chat=False)
        assert cancelled.current_state == OperatorState.CANCELLED.value
        assert db.get(models.OperatorQuestion, pending_question_id).status == "superseded"
        assert db.get(models.OperatorQuestion, applied_question_id).status == "applied"
        assert db.get(models.RuntimeTask, pending_task_id).status == "superseded"
        assert db.get(models.RuntimeTask, approved_task_id).status == "superseded"
        assert db.get(models.RuntimeTask, completed_task_id).status == "completed"
        workspace = cancelled.confidence_state["end_to_end"]
        assert workspace["phase"] == "APPLICATION_EXECUTION"
        assert "pending_question_id" not in workspace
        assert "pending_question_type" not in workspace
        assert "pending_question_reason" not in workspace
        stale_question = models.OperatorQuestion(
            run_id=run_id,
            question_type="blocked_reason",
            status="pending",
            prompt="Legacy stale prompt",
        )
        late_pending_task = models.RuntimeTask(
            run_id=run_id,
            name="Late pending task",
            status="pending",
            task_type="field_approval",
        )
        late_approved_task = models.RuntimeTask(
            run_id=run_id,
            name="Late approved task",
            status="approved",
            task_type="field_approval",
        )
        db.add_all([stale_question, late_pending_task, late_approved_task])
        db.flush()
        cancelled.confidence_state = {
            **(cancelled.confidence_state or {}),
            "end_to_end": {
                **((cancelled.confidence_state or {}).get("end_to_end") or {}),
                "pending_question_id": stale_question.id,
                "pending_question_type": stale_question.question_type,
                "pending_question_reason": "Late race",
            },
        }
        db.commit()
        stale_question_id = stale_question.id
        late_pending_task_id = late_pending_task.id
        late_approved_task_id = late_approved_task.id

    with SessionLocal() as db:
        recancelled = OperatorRuntimeEngine(db).cancel_run(run_id, notify_chat=False)
        assert recancelled.current_state == OperatorState.CANCELLED.value
        assert db.get(models.OperatorQuestion, stale_question_id).status == "superseded"
        assert db.get(models.RuntimeTask, late_pending_task_id).status == "superseded"
        assert db.get(models.RuntimeTask, late_approved_task_id).status == "superseded"
        workspace = recancelled.confidence_state["end_to_end"]
        assert "pending_question_id" not in workspace
        assert "pending_question_type" not in workspace
        assert "pending_question_reason" not in workspace


def test_cancel_running_run_is_allowed() -> None:
    with SessionLocal() as db:
        user = models.User(email=f"cancel-running-{uuid.uuid4().hex}@example.com", name="Cancel Running")
        db.add(user)
        db.flush()
        run = models.OperatorRun(
            user_id=user.id,
            goal="Cancel active browser run",
            target_url="https://example.com/apply",
            current_state=OperatorState.RUNNING.value,
            status_message="Running in browser",
        )
        db.add(run)
        db.flush()
        pending_question = models.OperatorQuestion(
            run_id=run.id,
            question_type="field_approval",
            status="pending",
            prompt="Need input",
        )
        db.add(pending_question)
        db.commit()
        run_id = run.id
        pending_question_id = pending_question.id

    with SessionLocal() as db:
        cancelled = OperatorRuntimeEngine(db).cancel_run(run_id, notify_chat=False)
        assert cancelled.current_state == OperatorState.CANCELLED.value
        assert cancelled.status_message == "Session stopped. Start a new session to continue this application."
        assert db.get(models.OperatorQuestion, pending_question_id).status == "superseded"


def test_ats_specific_browser_repair_symbols_are_removed() -> None:
    with SessionLocal() as db:
        engine = OperatorRuntimeEngine(db)
        assert not hasattr(engine, "_greenhouse_pre_submit_repair")
        assert not hasattr(engine, "_active_form_fill_skill_value")
        assert not hasattr(engine, "_mapping_matches_field")
        assert not hasattr(engine, "_answer_from_learned_mapping")
        with pytest.raises(AttributeError):
            getattr(engine, "_greenhouse_pre_submit_repair")


def test_resume_is_blocked_while_operator_question_is_pending() -> None:
    with TestClient(app):
        run = create_operator_goal_response({"job_url": "https://boards.greenhouse.io/example/jobs/12345", "auto_start": False}).json()

    with SessionLocal() as db:
        db_run = db.get(models.OperatorRun, run["id"])
        db_run.current_state = "WAITING_FOR_USER"
        db.add(
            models.OperatorQuestion(
                run_id=db_run.id,
                question_type="field_approval",
                stuck_phase="APPLICATION_EXECUTION",
                stuck_reason="Need a required answer",
                prompt="What should I use?",
                expected_answer_schema={"task_ids": []},
                status="pending",
            )
        )
        db.commit()

        resumed = OperatorRuntimeEngine(db).resume_run(db_run.id)
        assert resumed.current_state == "WAITING_FOR_USER"
        assert "waiting for your answer" in resumed.status_message.lower()
        event_types = [event.event_type for event in db.scalars(select(models.CognitiveEvent).where(models.CognitiveEvent.run_id == db_run.id)).all()]
        assert "APPLICATION_EXECUTION_RESUME_BLOCKED" in event_types


def test_resume_running_in_browser_run_reattaches_extension_body(monkeypatch) -> None:
    with TestClient(app):
        run = create_operator_goal_response({"job_url": "https://boards.greenhouse.io/example/jobs/12345", "auto_start": False}).json()

    scheduled: list[int] = []

    def fake_schedule(run_id: int) -> bool:
        scheduled.append(run_id)
        return True

    monkeypatch.setattr("app.extension_run.schedule_resume_if_in_browser", fake_schedule)
    with SessionLocal() as db:
        db_run = db.get(models.OperatorRun, run["id"])
        db_run.current_state = OperatorState.RUNNING.value
        db_run.status_message = "Choosing the next visual browser action."
        db_run.confidence_state = {
            **(db_run.confidence_state or {}),
            "in_browser": {"url": db_run.target_url, "device_id": "dev-test"},
        }
        OperatorChatService(db).create_thread(user_id=db_run.user_id, run_id=db_run.id, title="Application")
        db.commit()

        resumed = OperatorRuntimeEngine(db).resume_run(db_run.id)

        assert scheduled == [db_run.id]
        assert resumed.current_state == OperatorState.RUNNING.value
        assert resumed.status_message == "Resuming the live browser session in Chrome."
        assert resumed.confidence_state["session_control"]["status"] == "running"
        event_types = [event.event_type for event in db.scalars(select(models.CognitiveEvent).where(models.CognitiveEvent.run_id == db_run.id)).all()]
        assert "IN_BROWSER_RESUME_SCHEDULED" in event_types
        messages = OperatorChatService(db).list_messages(db.scalars(select(models.ChatThread).where(models.ChatThread.run_id == db_run.id)).first().id)
        assert any(message.role == "assistant" and "Resuming the live browser session" in message.content for message in messages)


def _in_browser_run_in_state(state: str, **extra_state) -> int:
    with TestClient(app):
        run = create_operator_goal_response(
            {"job_url": "https://example.test/application", "auto_start": False}
        ).json()
    with SessionLocal() as db:
        db_run = db.get(models.OperatorRun, run["id"])
        db_run.current_state = state
        db_run.status_message = "Waiting for user"
        db_run.confidence_state = {
            **(db_run.confidence_state or {}),
            "in_browser": {"url": db_run.target_url, "device_id": "dev-test"},
            **extra_state,
        }
        OperatorChatService(db).create_thread(user_id=db_run.user_id, run_id=db_run.id, title="Application")
        db.commit()
        return db_run.id


def _event_types(run_id: int) -> list[str]:
    with SessionLocal() as db:
        return [
            event.event_type
            for event in db.scalars(select(models.CognitiveEvent).where(models.CognitiveEvent.run_id == run_id)).all()
        ]


@pytest.mark.parametrize("state", [OperatorState.WAITING_FOR_USER.value, OperatorState.BLOCKED.value])
def test_plain_resume_routes_in_browser_run_the_same_from_waiting_and_blocked(monkeypatch, state: str) -> None:
    # Regression: a plain resume of a WAITING_FOR_USER in-browser run used to fall through to the
    # removed legacy runtime (LEGACY_DIRECT_RUNTIME_REMOVED -> BLOCKED), while a resume from BLOCKED
    # reached the in-browser path. Both states must take the in-browser path.
    scheduled: list[int] = []
    monkeypatch.setattr(
        "app.extension_run.schedule_resume_if_in_browser",
        lambda run_id: scheduled.append(run_id) or True,
    )
    run_id = _in_browser_run_in_state(state)

    with SessionLocal() as db:
        resumed = OperatorRuntimeEngine(db).resume_run(run_id)

        assert scheduled == [run_id]
        assert resumed.current_state == OperatorState.RUNNING.value
        assert resumed.status_message == "Resuming the live browser session in Chrome."
    event_types = _event_types(run_id)
    assert "IN_BROWSER_RESUME_SCHEDULED" in event_types
    assert "LEGACY_DIRECT_RUNTIME_REMOVED" not in event_types


def test_plain_resume_of_waiting_in_browser_run_keeps_sensitive_takeover_for_explicit_continue(monkeypatch) -> None:
    scheduled: list[int] = []
    monkeypatch.setattr(
        "app.extension_run.schedule_resume_if_in_browser",
        lambda run_id: scheduled.append(run_id) or True,
    )
    run_id = _in_browser_run_in_state(
        OperatorState.WAITING_FOR_USER.value,
        sensitive_browser_takeover={"active": True, "kind": "credential_vault", "status": "waiting_for_explicit_continue"},
    )

    with SessionLocal() as db:
        resumed = OperatorRuntimeEngine(db).resume_run(run_id)

        # A plain resume is not the sensitive_browser_continue control: no drive, no block.
        assert scheduled == []
        assert resumed.current_state == OperatorState.WAITING_FOR_USER.value
        assert resumed.confidence_state["sensitive_browser_takeover"]["active"] is True
    assert "LEGACY_DIRECT_RUNTIME_REMOVED" not in _event_types(run_id)
    with SessionLocal() as db:
        state_changes = [
            event.payload
            for event in db.scalars(
                select(models.CognitiveEvent).where(
                    models.CognitiveEvent.run_id == run_id,
                    models.CognitiveEvent.event_type == "RUN_STATE_CHANGED",
                )
            ).all()
        ]
    assert all(payload.get("to_state") != "BLOCKED" for payload in state_changes)


def test_plain_resume_of_waiting_in_browser_run_respects_armed_final_action(monkeypatch) -> None:
    scheduled: list[int] = []
    monkeypatch.setattr(
        "app.extension_run.schedule_resume_if_in_browser",
        lambda run_id: scheduled.append(run_id) or True,
    )
    run_id = _in_browser_run_in_state(
        OperatorState.WAITING_FOR_USER.value,
        pending_final_action={"status": "armed", "label": "Submit"},
    )

    with SessionLocal() as db:
        resumed = OperatorRuntimeEngine(db).resume_run(run_id)

        # The same final-action gate that guards a BLOCKED resume still stops this path.
        assert scheduled == []
        assert resumed.current_state == OperatorState.WAITING_FOR_USER.value
    assert "LEGACY_DIRECT_RUNTIME_REMOVED" not in _event_types(run_id)


def test_resume_retries_committed_browser_answer_without_reasking(monkeypatch) -> None:
    with TestClient(app):
        run = create_operator_goal_response(
            {
                "job_url": "https://example.test/application",
                "auto_start": False,
            }
        ).json()

    scheduled: list[int] = []
    monkeypatch.setattr(
        "app.extension_run.schedule_resume_if_in_browser",
        lambda run_id: scheduled.append(run_id) or True,
    )
    with SessionLocal() as db:
        db_run = db.get(models.OperatorRun, run["id"])
        retry_message = "The committed answer could not resume because the browser body disconnected."
        db_run.current_state = OperatorState.WAITING_FOR_USER.value
        db_run.status_message = retry_message
        db_run.confidence_state = {
            **(db_run.confidence_state or {}),
            "in_browser": {
                "url": db_run.target_url,
                "device_id": "dev-test",
            },
            "in_browser_resume_retry": {
                "pending": True,
                "source": "operator_answer",
                "question_id": 123,
                "status_message": retry_message,
            },
        }
        thread = OperatorChatService(db).create_thread(
            user_id=db_run.user_id,
            run_id=db_run.id,
            title="Application",
        )
        diagnostic = models.OperatorQuestion(
            run_id=db_run.id,
            thread_id=thread.id,
            question_type="blocked_reason",
            stuck_phase="WAITING_FOR_USER",
            stuck_reason=retry_message,
            prompt="Incorrectly asks for another fact.",
            expected_answer_schema={
                "answer": "string",
                "memory_update": "optional object",
            },
            status="pending",
        )
        db.add(diagnostic)
        db.commit()

        resumed = OperatorRuntimeEngine(db).resume_run(db_run.id)

        assert scheduled == [db_run.id]
        assert resumed.current_state == OperatorState.RUNNING.value
        assert resumed.status_message == "Resuming the live browser session in Chrome."
        assert resumed.confidence_state["in_browser_resume_retry"]["pending"] is False
        assert db.get(models.OperatorQuestion, diagnostic.id).status == "superseded"
        event_types = [
            event.event_type
            for event in db.scalars(
                select(models.CognitiveEvent).where(
                    models.CognitiveEvent.run_id == db_run.id
                )
            ).all()
        ]
        assert "IN_BROWSER_RESUME_RETRY_QUESTIONS_SUPERSEDED" in event_types
        assert "IN_BROWSER_RESUME_SCHEDULED" in event_types


def test_resume_review_reopens_in_browser_run_with_unverified_value_actions(monkeypatch) -> None:
    with TestClient(app):
        run = create_operator_goal_response(
            {"job_url": "https://example.test/application", "auto_start": False}
        ).json()

    scheduled: list[int] = []
    monkeypatch.setattr(
        "app.extension_run.schedule_resume_if_in_browser",
        lambda run_id: scheduled.append(run_id) or True,
    )
    with SessionLocal() as db:
        db_run = db.get(models.OperatorRun, run["id"])
        db_run.current_state = OperatorState.READY_FOR_REVIEW.value
        db_run.status_message = "Ready for review."
        db_run.confidence_state = {
            **(db_run.confidence_state or {}),
            "in_browser": {"url": db_run.target_url, "device_id": "dev-test"},
            "vision_session_ledger": {
                "choice": {
                    "label": "A required choice",
                    "kind": "button",
                    "last_action": "click",
                    "attempts": 1,
                    "last_outcome": "surface_changed",
                    "verified_value": None,
                    "verified": False,
                }
            },
            "pending_final_action": {
                "status": "declined",
                "label": "Submit",
            },
        }
        OperatorChatService(db).create_thread(
            user_id=db_run.user_id,
            run_id=db_run.id,
            title="Application",
        )
        db.commit()

        resumed = OperatorRuntimeEngine(db).resume_run(db_run.id)

        assert scheduled == [db_run.id]
        assert resumed.current_state == OperatorState.RUNNING.value
        assert resumed.status_message == "Resuming the live browser session in Chrome."
        assert resumed.confidence_state["pending_final_action"]["status"] == "cancelled"
        assert resumed.confidence_state["end_to_end"]["review_reopen_reason"] == "unverified_browser_actions"
        event_types = [
            event.event_type
            for event in db.scalars(
                select(models.CognitiveEvent).where(
                    models.CognitiveEvent.run_id == db_run.id
                )
            ).all()
        ]
        assert "APPLICATION_REVIEW_REOPENED_FOR_BROWSER_VERIFICATION" in event_types
        assert "IN_BROWSER_RESUME_SCHEDULED" in event_types


def test_resume_review_renews_declined_exact_final_action_without_reusing_authority(monkeypatch) -> None:
    with TestClient(app):
        run = create_operator_goal_response(
            {"job_url": "https://example.test/application", "auto_start": False}
        ).json()

    scheduled: list[int] = []
    monkeypatch.setattr(
        "app.extension_run.schedule_resume_if_in_browser",
        lambda run_id: scheduled.append(run_id) or True,
    )
    with SessionLocal() as db:
        db_run = db.get(models.OperatorRun, run["id"])
        db_run.current_state = OperatorState.READY_FOR_REVIEW.value
        db_run.status_message = "Ready for review."
        db_run.confidence_state = {
            **(db_run.confidence_state or {}),
            "in_browser": {"url": db_run.target_url, "device_id": "dev-test"},
            "pending_final_action": {
                "status": "declined",
                "label": "Submit",
                "approval_token": "consumed-old-token",
                "authorization_consumed": True,
                "browser_activation_attempted": False,
                "browser_activation_may_have_occurred": False,
            },
        }
        OperatorChatService(db).create_thread(
            user_id=db_run.user_id,
            run_id=db_run.id,
            title="Application",
        )
        db.commit()

        resumed = OperatorRuntimeEngine(db).resume_run(db_run.id)

        assert scheduled == [db_run.id]
        assert resumed.current_state == OperatorState.RUNNING.value
        assert resumed.status_message == "Resuming the live browser session in Chrome."
        pending = resumed.confidence_state["pending_final_action"]
        assert pending["status"] == "cancelled"
        assert pending["fresh_approval_required"] is True
        assert pending["authorization_consumed"] is True
        assert resumed.confidence_state["end_to_end"]["review_reopen_reason"] == "fresh_exact_final_action"
        event_types = [
            event.event_type
            for event in db.scalars(
                select(models.CognitiveEvent).where(
                    models.CognitiveEvent.run_id == db_run.id
                )
            ).all()
        ]
        assert "APPLICATION_REVIEW_REOPENED_FOR_FRESH_FINAL_ACTION" in event_types
        assert "IN_BROWSER_RESUME_SCHEDULED" in event_types


def test_resume_review_does_not_retry_final_action_when_activation_may_have_occurred(
    monkeypatch,
) -> None:
    with TestClient(app):
        run = create_operator_goal_response(
            {"job_url": "https://example.test/application", "auto_start": False}
        ).json()

    scheduled: list[int] = []
    monkeypatch.setattr(
        "app.extension_run.schedule_resume_if_in_browser",
        lambda run_id: scheduled.append(run_id) or True,
    )
    with SessionLocal() as db:
        db_run = db.get(models.OperatorRun, run["id"])
        db_run.current_state = OperatorState.READY_FOR_REVIEW.value
        db_run.confidence_state = {
            **(db_run.confidence_state or {}),
            "in_browser": {"url": db_run.target_url, "device_id": "dev-test"},
            "pending_final_action": {
                "status": "failed",
                "browser_activation_attempted": True,
                "browser_activation_may_have_occurred": True,
            },
        }
        db.commit()

        resumed = OperatorRuntimeEngine(db).resume_run(db_run.id)

        assert scheduled == []
        assert resumed.current_state == OperatorState.READY_FOR_REVIEW.value


def test_resume_structured_system_block_reopens_in_browser_run_without_user_question(monkeypatch) -> None:
    with TestClient(app):
        run = create_operator_goal_response({"job_url": "https://boards.greenhouse.io/example/jobs/12345", "auto_start": False}).json()

    scheduled: list[int] = []
    monkeypatch.setattr("app.extension_run.schedule_resume_if_in_browser", lambda run_id: scheduled.append(run_id) or True)
    with SessionLocal() as db:
        db_run = db.get(models.OperatorRun, run["id"])
        db_run.current_state = OperatorState.BLOCKED.value
        db_run.status_message = "Browser body channel was interrupted."
        db_run.confidence_state = {
            **(db_run.confidence_state or {}),
            "in_browser": {"url": db_run.target_url, "device_id": "dev-test"},
            "system_block": {"reason": "extension_transport_unavailable", "needs_user": False, "block_kind": "awaiting_engineering"},
            "end_to_end": {
                **((db_run.confidence_state or {}).get("end_to_end") or {}),
                "system_block": True,
                "needs_user": False,
                "block_kind": "awaiting_engineering",
            },
        }
        question = models.OperatorQuestion(
            run_id=db_run.id,
            question_type="blocked_reason",
            stuck_reason=db_run.status_message,
            prompt="Incorrect legacy question",
            status="pending",
        )
        db.add(question)
        OperatorChatService(db).create_thread(user_id=db_run.user_id, run_id=db_run.id, title="Application")
        db.commit()

        resumed = OperatorRuntimeEngine(db).resume_run(db_run.id)

        assert scheduled == [db_run.id]
        assert resumed.current_state == OperatorState.RUNNING.value
        assert resumed.status_message == "Resuming the live browser session in Chrome."
        assert db.get(models.OperatorQuestion, question.id).status == "applied"
        confidence = resumed.confidence_state or {}
        assert confidence.get("system_block") is None
        assert (confidence.get("end_to_end") or {}).get("system_block") is False
        assert (confidence.get("end_to_end") or {}).get("block_kind") is None


def test_resume_answered_current_block_reason_uses_state_equality_not_message_keywords(monkeypatch) -> None:
    with TestClient(app):
        run = create_operator_goal_response({"job_url": "https://boards.greenhouse.io/example/jobs/12345", "auto_start": False}).json()

    scheduled: list[int] = []
    monkeypatch.setattr("app.extension_run.schedule_resume_if_in_browser", lambda run_id: scheduled.append(run_id) or True)
    with SessionLocal() as db:
        db_run = db.get(models.OperatorRun, run["id"])
        db_run.current_state = OperatorState.BLOCKED.value
        db_run.status_message = "Opaque blocker 8f2c"
        db_run.confidence_state = {
            **(db_run.confidence_state or {}),
            "in_browser": {"url": db_run.target_url, "device_id": "dev-test"},
        }
        answered = models.OperatorQuestion(
            run_id=db_run.id,
            question_type="blocked_reason",
            stuck_reason=db_run.status_message,
            prompt="Recovery instruction",
            status="applied",
            answer_payload={"answer": "Observe again and continue."},
        )
        pending = models.OperatorQuestion(
            run_id=db_run.id,
            question_type="blocked_reason",
            stuck_reason=db_run.status_message,
            prompt="Duplicated recovery prompt",
            status="pending",
        )
        db.add_all([answered, pending])
        OperatorChatService(db).create_thread(user_id=db_run.user_id, run_id=db_run.id, title="Application")
        db.commit()

        resumed = OperatorRuntimeEngine(db).resume_run(db_run.id)

        assert scheduled == [db_run.id]
        assert resumed.current_state == OperatorState.RUNNING.value
        assert db.get(models.OperatorQuestion, pending.id).status == "applied"


def test_resume_diagnostic_browser_block_reobserves_from_state_without_message_rules(monkeypatch) -> None:
    with TestClient(app):
        run = create_operator_goal_response({"job_url": "https://boards.greenhouse.io/example/jobs/12345", "auto_start": False}).json()

    scheduled: list[int] = []
    monkeypatch.setattr("app.extension_run.schedule_resume_if_in_browser", lambda run_id: scheduled.append(run_id) or True)
    with SessionLocal() as db:
        db_run = db.get(models.OperatorRun, run["id"])
        db_run.current_state = OperatorState.BLOCKED.value
        db_run.status_message = "Opaque diagnostic stop 5b8e"
        db_run.confidence_state = {
            **(db_run.confidence_state or {}),
            "in_browser": {"url": db_run.target_url, "device_id": "dev-test"},
            "end_to_end": {
                **((db_run.confidence_state or {}).get("end_to_end") or {}),
                "terminal_state": None,
                "submit_approval_status": "not_requested",
                "submission_lock": False,
            },
        }
        diagnostic = models.OperatorQuestion(
            run_id=db_run.id,
            question_type="blocked_reason",
            stuck_reason=db_run.status_message,
            prompt="Diagnostic recovery card",
            expected_answer_schema={"answer": "string", "memory_update": "optional object"},
            status="pending",
        )
        db.add(diagnostic)
        OperatorChatService(db).create_thread(user_id=db_run.user_id, run_id=db_run.id, title="Application")
        db.commit()

        resumed = OperatorRuntimeEngine(db).resume_run(db_run.id)

        assert scheduled == [db_run.id]
        assert resumed.current_state == OperatorState.RUNNING.value
        assert resumed.status_message == "Resuming the live browser session in Chrome."
        assert db.get(models.OperatorQuestion, diagnostic.id).status == "applied"
        events = list(db.scalars(select(models.CognitiveEvent).where(models.CognitiveEvent.run_id == db_run.id)).all())
        assert any(event.event_type == "APPLICATION_EXECUTION_RECOVERY_RESUME_STARTED" for event in events)
        assert any(event.event_type == "IN_BROWSER_RESUME_SCHEDULED" for event in events)


def test_resume_blocked_browser_run_does_not_bypass_pending_field_question(monkeypatch) -> None:
    with TestClient(app):
        run = create_operator_goal_response({"job_url": "https://boards.greenhouse.io/example/jobs/12345", "auto_start": False}).json()

    scheduled: list[int] = []
    monkeypatch.setattr("app.extension_run.schedule_resume_if_in_browser", lambda run_id: scheduled.append(run_id) or True)
    with SessionLocal() as db:
        db_run = db.get(models.OperatorRun, run["id"])
        db_run.current_state = OperatorState.BLOCKED.value
        db_run.status_message = "Waiting on a grounded answer"
        db_run.confidence_state = {
            **(db_run.confidence_state or {}),
            "in_browser": {"url": db_run.target_url, "device_id": "dev-test"},
        }
        db.add(
            models.OperatorQuestion(
                run_id=db_run.id,
                question_type="field_approval",
                stuck_reason=db_run.status_message,
                prompt="Choose a value",
                expected_answer_schema={"task_ids": [98765]},
                status="pending",
            )
        )
        db.commit()

        resumed = OperatorRuntimeEngine(db).resume_run(db_run.id)

        assert scheduled == []
        assert resumed.current_state == OperatorState.BLOCKED.value
        event = db.scalars(
            select(models.CognitiveEvent)
            .where(models.CognitiveEvent.run_id == db_run.id, models.CognitiveEvent.event_type == "APPLICATION_EXECUTION_RESUME_REQUESTED")
            .order_by(models.CognitiveEvent.id.desc())
        ).first()
        assert event.event_payload["recoverable"] is False
        assert event.event_payload["recovery_gate"] == "pending_operator_question"


def test_resume_blocked_browser_run_does_not_bypass_armed_final_action(monkeypatch) -> None:
    with TestClient(app):
        run = create_operator_goal_response({"job_url": "https://boards.greenhouse.io/example/jobs/12345", "auto_start": False}).json()

    scheduled: list[int] = []
    monkeypatch.setattr("app.extension_run.schedule_resume_if_in_browser", lambda run_id: scheduled.append(run_id) or True)
    with SessionLocal() as db:
        db_run = db.get(models.OperatorRun, run["id"])
        db_run.current_state = OperatorState.BLOCKED.value
        db_run.status_message = "Final action outcome requires review"
        db_run.confidence_state = {
            **(db_run.confidence_state or {}),
            "in_browser": {"url": db_run.target_url, "device_id": "dev-test"},
            "pending_final_action": {"status": "unverified", "approval_token": "approval-1"},
        }
        db.commit()

        resumed = OperatorRuntimeEngine(db).resume_run(db_run.id)

        assert scheduled == []
        assert resumed.current_state == OperatorState.BLOCKED.value
        event = db.scalars(
            select(models.CognitiveEvent)
            .where(models.CognitiveEvent.run_id == db_run.id, models.CognitiveEvent.event_type == "APPLICATION_EXECUTION_RESUME_REQUESTED")
            .order_by(models.CognitiveEvent.id.desc())
        ).first()
        assert event.event_payload["recovery_gate"] == "pending_final_action"


def test_resume_blocked_browser_run_reobserves_conclusively_rejected_final_action(
    monkeypatch,
) -> None:
    with TestClient(app):
        run = create_operator_goal_response(
            {"job_url": "https://example.test/application", "auto_start": False}
        ).json()

    scheduled: list[int] = []
    monkeypatch.setattr(
        "app.extension_run.schedule_resume_if_in_browser",
        lambda run_id: scheduled.append(run_id) or True,
    )
    with SessionLocal() as db:
        db_run = db.get(models.OperatorRun, run["id"])
        db_run.current_state = OperatorState.BLOCKED.value
        db_run.status_message = "The external workflow rejected the attempted action."
        db_run.confidence_state = {
            **(db_run.confidence_state or {}),
            "in_browser": {"url": db_run.target_url, "device_id": "dev-test"},
            "pending_final_action": {
                "status": "failed",
                "authorization_consumed": True,
                "browser_activation_attempted": True,
                "browser_activation_may_have_occurred": True,
                "outcome_status": "failed",
                "outcome_confidence": 0.92,
                "outcome_evidence": "The workflow displayed a definitive rejection.",
            },
        }
        db.commit()

        resumed = OperatorRuntimeEngine(db).resume_run(db_run.id)

        assert scheduled == [db_run.id]
        assert resumed.current_state == OperatorState.RUNNING.value
        assert resumed.confidence_state["pending_final_action"]["status"] == "failed"
        assert resumed.confidence_state["pending_final_action"]["authorization_consumed"] is True


def test_resume_blocked_browser_run_does_not_retry_uncertain_failed_final_action(
    monkeypatch,
) -> None:
    with TestClient(app):
        run = create_operator_goal_response(
            {"job_url": "https://example.test/application", "auto_start": False}
        ).json()

    scheduled: list[int] = []
    monkeypatch.setattr(
        "app.extension_run.schedule_resume_if_in_browser",
        lambda run_id: scheduled.append(run_id) or True,
    )
    with SessionLocal() as db:
        db_run = db.get(models.OperatorRun, run["id"])
        db_run.current_state = OperatorState.BLOCKED.value
        db_run.confidence_state = {
            **(db_run.confidence_state or {}),
            "in_browser": {"url": db_run.target_url, "device_id": "dev-test"},
            "pending_final_action": {
                "status": "failed",
                "authorization_consumed": True,
                "browser_activation_may_have_occurred": True,
                "outcome_status": "uncertain",
                "outcome_confidence": 0.3,
            },
        }
        db.commit()

        resumed = OperatorRuntimeEngine(db).resume_run(db_run.id)

        assert scheduled == []
        assert resumed.current_state == OperatorState.BLOCKED.value


def test_pause_active_session_marks_user_pause_and_writes_chat_message() -> None:
    with TestClient(app):
        run = create_operator_goal_response({"job_url": "https://boards.greenhouse.io/example/jobs/12345", "auto_start": False}).json()

    with SessionLocal() as db:
        db_run = db.get(models.OperatorRun, run["id"])
        db_run.current_state = OperatorState.FILLING_APPLICATION.value
        chat = OperatorChatService(db).create_thread(user_id=db_run.user_id, run_id=db_run.id, title="Lumen run chat")
        db.commit()

        paused = OperatorRuntimeEngine(db).pause_run(db_run.id)

        assert paused.current_state == OperatorState.WAITING_FOR_USER.value
        assert paused.status_message.startswith("Paused.")
        assert paused.confidence_state["user_pause"]["active"] is True
        assert paused.confidence_state["user_pause"]["paused_from_state"] == OperatorState.FILLING_APPLICATION.value
        messages = OperatorChatService(db).list_messages(chat.id)
        assert any(message.role == "assistant" and "Paused this session" in message.content for message in messages)
        event_types = [event.event_type for event in db.scalars(select(models.CognitiveEvent).where(models.CognitiveEvent.run_id == db_run.id)).all()]
        assert "APPLICATION_EXECUTION_PAUSED" in event_types
        assert "LUMEN_CHAT_RUNTIME_MESSAGE_CREATED" in event_types


def test_resume_paused_in_browser_session_reattaches_same_body(monkeypatch) -> None:
    with TestClient(app):
        run = create_operator_goal_response({"job_url": "https://boards.greenhouse.io/example/jobs/12345", "auto_start": False}).json()

    scheduled: list[int] = []

    def fake_schedule(run_id: int) -> bool:
        scheduled.append(run_id)
        return True

    monkeypatch.setattr("app.extension_run.schedule_resume_if_in_browser", fake_schedule)
    with SessionLocal() as db:
        db_run = db.get(models.OperatorRun, run["id"])
        db_run.current_state = OperatorState.RUNNING.value
        db_run.confidence_state = {
            **(db_run.confidence_state or {}),
            "in_browser": {"url": db_run.target_url, "device_id": "dev-test"},
        }
        OperatorChatService(db).create_thread(user_id=db_run.user_id, run_id=db_run.id, title="Application")
        db.commit()

        paused = OperatorRuntimeEngine(db).pause_run(db_run.id)
        assert paused.current_state == OperatorState.WAITING_FOR_USER.value

        pause_question = models.OperatorQuestion(
            run_id=db_run.id,
            question_type="blocked_reason",
            stuck_phase=OperatorState.WAITING_FOR_USER.value,
            stuck_reason=paused.status_message,
            prompt="Legacy pause diagnostic",
            expected_answer_schema={"answer": "string", "memory_update": "optional object"},
            status="pending",
        )
        db.add(pause_question)
        db.flush()
        pause_question_id = int(pause_question.id)
        pause_state = dict(db_run.confidence_state or {})
        pause_state["end_to_end"] = {
            **(pause_state.get("end_to_end") or {}),
            "pending_question_id": pause_question_id,
            "pending_question_type": "blocked_reason",
            "pending_question_reason": paused.status_message,
        }
        db_run.confidence_state = pause_state
        db.commit()

        resumed = OperatorRuntimeEngine(db).resume_run(db_run.id)

        assert scheduled == [db_run.id]
        assert resumed.current_state == OperatorState.RUNNING.value
        assert resumed.confidence_state["user_pause"]["active"] is False
        assert resumed.confidence_state["session_control"]["status"] == "running"
        assert resumed.status_message == "Resuming the live browser session in Chrome."
        assert db.get(models.OperatorQuestion, pause_question_id).status == "superseded"
        assert "pending_question_id" not in resumed.confidence_state.get("end_to_end", {})
        event_types = [
            event.event_type
            for event in db.scalars(select(models.CognitiveEvent).where(models.CognitiveEvent.run_id == db_run.id)).all()
        ]
        assert "PAUSE_DIAGNOSTIC_QUESTIONS_SUPERSEDED" in event_types


def test_resume_paused_blocked_browser_session_reuses_guarded_recovery(monkeypatch) -> None:
    with TestClient(app):
        run = create_operator_goal_response(
            {"job_url": "https://example.com/application", "auto_start": False}
        ).json()

    scheduled: list[int] = []
    monkeypatch.setattr(
        "app.extension_run.schedule_resume_if_in_browser",
        lambda run_id: scheduled.append(run_id) or True,
    )
    with SessionLocal() as db:
        db_run = db.get(models.OperatorRun, run["id"])
        db_run.current_state = OperatorState.BLOCKED.value
        db_run.status_message = "Opaque browser recovery block"
        db_run.confidence_state = {
            **(db_run.confidence_state or {}),
            "in_browser": {"url": db_run.target_url, "device_id": "dev-test"},
            "system_block": {
                "reason": "grounding_recovery_exhausted",
                "needs_user": False,
                "block_kind": "awaiting_engineering",
            },
            "end_to_end": {
                **((db_run.confidence_state or {}).get("end_to_end") or {}),
                "system_block": True,
                "needs_user": False,
                "block_kind": "awaiting_engineering",
            },
        }
        OperatorChatService(db).create_thread(
            user_id=db_run.user_id,
            run_id=db_run.id,
            title="Application",
        )
        db.commit()

        paused = OperatorRuntimeEngine(db).pause_run(db_run.id)
        assert paused.current_state == OperatorState.WAITING_FOR_USER.value
        assert (
            paused.confidence_state["user_pause"]["paused_from_state"]
            == OperatorState.BLOCKED.value
        )

        resumed = OperatorRuntimeEngine(db).resume_run(db_run.id)

        assert scheduled == [db_run.id]
        assert resumed.current_state == OperatorState.RUNNING.value
        assert resumed.confidence_state["user_pause"]["active"] is False
        assert resumed.confidence_state["session_control"]["status"] == "running"
        assert resumed.status_message == "Resuming the live browser session in Chrome."
        event_types = [
            event.event_type
            for event in db.scalars(
                select(models.CognitiveEvent).where(
                    models.CognitiveEvent.run_id == db_run.id
                )
            ).all()
        ]
        assert "APPLICATION_EXECUTION_RECOVERY_RESUME_STARTED" in event_types
        assert "IN_BROWSER_RESUME_SCHEDULED" in event_types


def test_paused_browser_session_does_not_create_generic_missing_fact_question() -> None:
    with TestClient(app):
        run = create_operator_goal_response(
            {"job_url": "https://boards.greenhouse.io/example/jobs/12345", "auto_start": False}
        ).json()

    with SessionLocal() as db:
        db_run = db.get(models.OperatorRun, run["id"])
        db_run.current_state = OperatorState.RUNNING.value
        db_run.confidence_state = {
            **(db_run.confidence_state or {}),
            "in_browser": {"url": db_run.target_url, "device_id": "dev-test"},
        }
        OperatorChatService(db).create_thread(user_id=db_run.user_id, run_id=db_run.id, title="Application")
        task = models.RuntimeTask(
            run_id=db_run.id,
            name="Grounded fact needed",
            status="pending",
            task_type="field_approval",
            payload={"field_key": "grounded_fact", "label": "Grounded fact", "required": True},
        )
        db.add(task)
        db.flush()
        field_question = models.OperatorQuestion(
            run_id=db_run.id,
            question_type="field_approval",
            stuck_phase="APPLICATION_EXECUTION",
            stuck_reason="A grounded fact is required.",
            prompt="Provide the grounded fact.",
            expected_answer_schema={"task_ids": [task.id]},
            status="pending",
        )
        db.add(field_question)
        db.commit()

        OperatorRuntimeEngine(db).pause_run(db_run.id)
        questions = OperatorChatService(db).list_questions(db_run.id)

        assert any(
            question.id == field_question.id
            and question.status == "pending"
            and question.question_type == "field_approval"
            for question in questions
        )
        assert not [
            question
            for question in questions
            if question.status == "pending" and question.question_type == "blocked_reason"
        ]


def test_stale_pending_question_is_ignored_when_task_is_already_approved() -> None:
    with TestClient(app):
        run = create_operator_goal_response({"job_url": "https://boards.greenhouse.io/example/jobs/12345", "auto_start": False}).json()

    with SessionLocal() as db:
        db_run = db.get(models.OperatorRun, run["id"])
        task = models.RuntimeTask(
            run_id=db_run.id,
            name="Location",
            status="approved",
            task_type="field_approval",
            payload={"field_key": "location", "approved_value": "Jersey City, New Jersey"},
        )
        db.add(task)
        db.flush()
        question = models.OperatorQuestion(
            run_id=db_run.id,
            question_type="field_approval",
            stuck_phase="APPLICATION_EXECUTION",
            stuck_reason="Need location",
            prompt="Location?",
            expected_answer_schema={"task_ids": [task.id]},
            status="pending",
        )
        db.add(question)
        db.commit()

        engine = OperatorRuntimeEngine(db)
        assert engine._pending_operator_question(db_run.id) is None
        db.refresh(question)
        assert question.status == "applied"


def test_startup_reconciliation_rescues_orphaned_running_runs() -> None:
    """At process startup every RUNNING run is orphaned (no drive coroutine can exist).

    Reconciliation must convert it into a resumable BLOCKED run, supersede unconsumed
    final-action authority so nothing stale replays, and stamp the discontinuity marker
    the drive later surfaces to the model.
    """
    with SessionLocal() as db:
        user = models.User(email=f"orphan-{uuid.uuid4().hex}@example.com", name="Orphan")
        db.add(user)
        db.flush()
        run = models.OperatorRun(
            user_id=user.id,
            goal="Fill the form",
            target_url="https://example.com/apply",
            current_state=OperatorState.RUNNING.value,
            status_message="Choosing the next visual browser action.",
            confidence_state={
                "in_browser": {"url": "https://example.com/apply", "device_id": "dev-x", "execution_surface": "browser"},
                "pending_final_action": {"status": "approved", "fingerprint": "abc"},
            },
        )
        untouched = models.OperatorRun(
            user_id=user.id,
            goal="Already waiting",
            target_url="https://example.com/other",
            current_state=OperatorState.WAITING_FOR_USER.value,
            status_message="Waiting for your answer.",
            confidence_state={},
        )
        db.add_all([run, untouched])
        db.commit()
        run_id, untouched_id = run.id, untouched.id

    with SessionLocal() as db:
        reconciled = OperatorRuntimeEngine(db).reconcile_orphaned_runs_after_restart()
        assert run_id in reconciled
        assert untouched_id not in reconciled

    with SessionLocal() as db:
        rescued = db.get(models.OperatorRun, run_id)
        assert rescued.current_state == OperatorState.BLOCKED.value
        state = dict(rescued.confidence_state or {})
        pending = state["pending_final_action"]
        assert pending["status"] == "superseded"
        assert pending["fresh_approval_required"] is True
        assert pending["authorization_consumed"] is False
        marker = state["runtime_discontinuity"]
        assert marker["reason"] == "runtime_restarted"
        assert "restarted" in (rescued.status_message or "")
        assert "fresh approval" in (rescued.status_message or "")
        # The rescued run must be resumable through the ordinary resume flow.
        assert OperatorRuntimeEngine(db)._blocked_run_can_resume(rescued) is True
        # A parked WAITING_FOR_USER run is legitimately durable and must not be touched.
        parked = db.get(models.OperatorRun, untouched_id)
        assert parked.current_state == OperatorState.WAITING_FOR_USER.value

    # A second startup pass is a no-op: nothing is RUNNING any more.
    with SessionLocal() as db:
        assert run_id not in OperatorRuntimeEngine(db).reconcile_orphaned_runs_after_restart()


def test_runtime_discontinuity_note_is_consumed_exactly_once() -> None:
    from app.extension_run import _consume_runtime_discontinuity_note

    with SessionLocal() as db:
        user = models.User(email=f"orphan-note-{uuid.uuid4().hex}@example.com", name="Note")
        db.add(user)
        db.flush()
        run = models.OperatorRun(
            user_id=user.id,
            goal="g",
            target_url="https://example.com",
            current_state=OperatorState.BLOCKED.value,
            status_message="blocked",
            confidence_state={"runtime_discontinuity": {"reason": "runtime_restarted", "at": "t"}},
        )
        db.add(run)
        db.commit()
        run_id = run.id

    first = _consume_runtime_discontinuity_note(run_id)
    assert "runtime_discontinuity" in first
    assert "re-verify" in first
    assert _consume_runtime_discontinuity_note(run_id) == ""
    with SessionLocal() as db:
        cleared = db.get(models.OperatorRun, run_id)
        assert "runtime_discontinuity" not in dict(cleared.confidence_state or {})
