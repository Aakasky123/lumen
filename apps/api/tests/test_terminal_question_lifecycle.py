from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier, Event
import uuid

import pytest
from sqlalchemy import select

from app import models
import app.cognition as cognition_module
import app.runtime as runtime_module
from app.cognition import OperatorChatService
from app.db import SessionLocal, init_db
from app.runtime import OperatorRuntimeEngine
from app.state_machine import OperatorState


init_db()


def _terminal_run_fixture(state: str) -> tuple[int, int, int]:
    with SessionLocal() as db:
        user = models.User(
            email=f"terminal-question-{state.lower()}-{uuid.uuid4().hex}@example.com",
            name="Terminal Question Test",
        )
        db.add(user)
        db.flush()
        run = models.OperatorRun(
            user_id=user.id,
            goal="Do not revive terminal work",
            target_url="https://example.test/task",
            current_state=state,
            status_message=f"Run is {state.lower()}",
            confidence_state={"end_to_end": {"phase": "APPLICATION_EXECUTION"}},
        )
        db.add(run)
        db.flush()
        task = models.RuntimeTask(
            run_id=run.id,
            name="Stale field task",
            status="pending",
            task_type="field_approval",
            payload={"field_key": "stale_field", "label": "Stale field", "required": True},
        )
        db.add(task)
        db.flush()
        question = models.OperatorQuestion(
            run_id=run.id,
            question_type="field_approval",
            stuck_phase="APPLICATION_EXECUTION",
            stuck_reason="Stale input request",
            prompt="Provide stale input",
            expected_answer_schema={"task_ids": [task.id]},
            status="pending",
        )
        db.add(question)
        db.flush()
        run.confidence_state = {
            "end_to_end": {
                "phase": "APPLICATION_EXECUTION",
                "pending_question_id": question.id,
                "pending_question_type": question.question_type,
                "pending_question_reason": question.stuck_reason,
            }
        }
        db.commit()
        return run.id, task.id, question.id


def _live_blocked_run_fixture(*, with_question: bool) -> tuple[int, int | None]:
    with SessionLocal() as db:
        user = models.User(
            email=f"question-race-{uuid.uuid4().hex}@example.com",
            name="Question Race Test",
        )
        db.add(user)
        db.flush()
        run = models.OperatorRun(
            user_id=user.id,
            goal="Ask only while this run is live",
            target_url="https://example.test/task",
            current_state=OperatorState.BLOCKED.value,
            status_message="A real user choice is missing",
            confidence_state={"end_to_end": {"phase": "APPLICATION_EXECUTION"}},
        )
        db.add(run)
        db.flush()
        question_id = None
        if with_question:
            question = models.OperatorQuestion(
                run_id=run.id,
                question_type="blocked_reason",
                stuck_phase="APPLICATION_EXECUTION",
                stuck_reason=run.status_message,
                prompt="Provide the missing choice",
                expected_answer_schema={"answer": "string", "memory_update": "optional object"},
                status="pending",
            )
            db.add(question)
            db.flush()
            question_id = int(question.id)
        db.commit()
        return int(run.id), question_id


@pytest.mark.parametrize("terminal_state", ["CANCELLED", "COMPLETED", "FAILED"])
def test_terminal_question_reconciliation_and_polling_never_recreate(terminal_state: str) -> None:
    run_id, task_id, question_id = _terminal_run_fixture(terminal_state)

    with SessionLocal() as db:
        service = OperatorChatService(db)
        run = db.get(models.OperatorRun, run_id)
        assert service._question_spec(run) is None
        assert service.ensure_question_for_run(run_id, announce=False) is None
        db.commit()

        question = db.get(models.OperatorQuestion, question_id)
        assert question.status == "superseded"
        assert question.answer_payload["resolved_by"] == "terminal_run_reconciliation"
        assert question.answer_payload["terminal_state"] == terminal_state
        assert db.get(models.RuntimeTask, task_id).status == "pending"
        workspace = db.get(models.OperatorRun, run_id).confidence_state["end_to_end"]
        assert workspace["phase"] == "APPLICATION_EXECUTION"
        assert "pending_question_id" not in workspace
        assert "pending_question_type" not in workspace
        assert "pending_question_reason" not in workspace

        first_poll = service.list_questions(run_id)
        second_poll = service.list_questions(run_id)
        assert [item.id for item in first_poll] == [question_id]
        assert [item.id for item in second_poll] == [question_id]
        assert not [item for item in second_poll if item.status == "pending"]


def test_blocked_run_remains_question_capable() -> None:
    with SessionLocal() as db:
        user = models.User(
            email=f"blocked-question-{uuid.uuid4().hex}@example.com",
            name="Blocked Question Test",
        )
        db.add(user)
        db.flush()
        run = models.OperatorRun(
            user_id=user.id,
            goal="Ask for the missing fact",
            target_url="https://example.test/task",
            current_state="BLOCKED",
            status_message="A real user choice is missing",
            confidence_state={"end_to_end": {"phase": "APPLICATION_EXECUTION"}},
        )
        db.add(run)
        db.commit()

        question = OperatorChatService(db).ensure_question_for_run(run.id, announce=False)

        assert question is not None
        assert question.status == "pending"
        assert question.question_type == "blocked_reason"


@pytest.mark.parametrize("terminal_state", ["CANCELLED", "COMPLETED", "FAILED"])
def test_answer_question_rejects_terminal_run_before_mutation(terminal_state: str) -> None:
    run_id, _, question_id = _terminal_run_fixture(terminal_state)

    with SessionLocal() as db:
        question = db.get(models.OperatorQuestion, question_id)
        original_updated_at = question.updated_at

        with pytest.raises(ValueError, match="operator run is terminal"):
            OperatorChatService(db).answer_question(
                question_id=question_id,
                answer="This must not be stored",
                payload={"answer": "This must not be stored"},
            )

        db.refresh(question)
        assert question.status == "pending"
        assert question.answer_payload == {}
        assert question.updated_at == original_updated_at


def test_question_poll_creation_serializes_with_concurrent_cancel(monkeypatch) -> None:
    run_id, _ = _live_blocked_run_fixture(with_question=False)
    poll_reached_spec = Barrier(2)
    release_poll = Event()
    poll_gate_used = Event()
    cancel_guard_attempted = Event()
    cancel_finished = Event()
    original_question_spec = OperatorChatService._question_spec
    original_runtime_guard = runtime_module.run_lifecycle_guard

    def gated_question_spec(self, run):
        spec = original_question_spec(self, run)
        if int(run.id) == run_id and not poll_gate_used.is_set():
            poll_gate_used.set()
            poll_reached_spec.wait(timeout=5)
            assert release_poll.wait(timeout=5)
        return spec

    @contextmanager
    def observed_cancel_guard(guarded_run_id):
        if int(guarded_run_id) == run_id:
            cancel_guard_attempted.set()
        with original_runtime_guard(guarded_run_id):
            yield

    monkeypatch.setattr(OperatorChatService, "_question_spec", gated_question_spec)
    monkeypatch.setattr(runtime_module, "run_lifecycle_guard", observed_cancel_guard)

    def poll_questions():
        with SessionLocal() as db:
            return OperatorChatService(db).list_questions(run_id)

    def cancel():
        try:
            with SessionLocal() as db:
                return OperatorRuntimeEngine(db).cancel_run(run_id, notify_chat=False).current_state
        finally:
            cancel_finished.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        poll_future = pool.submit(poll_questions)
        poll_reached_spec.wait(timeout=5)
        cancel_future = pool.submit(cancel)
        assert cancel_guard_attempted.wait(timeout=5)
        assert not cancel_finished.wait(timeout=0.1)
        release_poll.set()
        assert any(question.status == "pending" for question in poll_future.result(timeout=5))
        assert cancel_future.result(timeout=5) == OperatorState.CANCELLED.value

    with SessionLocal() as db:
        assert db.get(models.OperatorRun, run_id).current_state == OperatorState.CANCELLED.value
        questions = list(
            db.scalars(
                select(models.OperatorQuestion).where(models.OperatorQuestion.run_id == run_id)
            ).all()
        )
        assert len(questions) == 1
        assert questions[0].status == "superseded"


def test_cancel_wins_before_concurrent_answer_can_mutate(monkeypatch) -> None:
    run_id, question_id = _live_blocked_run_fixture(with_question=True)
    assert question_id is not None
    cancel_reached_transition = Barrier(2)
    release_cancel = Event()
    answer_guard_attempted = Event()
    original_transition = OperatorRuntimeEngine._transition
    original_answer_guard = cognition_module.run_lifecycle_guard

    def gated_transition(self, run, to_state, *, reason, actor, confidence):
        original_transition(
            self,
            run,
            to_state,
            reason=reason,
            actor=actor,
            confidence=confidence,
        )
        if int(run.id) == run_id and to_state == OperatorState.CANCELLED:
            cancel_reached_transition.wait(timeout=5)
            assert release_cancel.wait(timeout=5)

    @contextmanager
    def observed_answer_guard(guarded_run_id):
        if int(guarded_run_id) == run_id:
            answer_guard_attempted.set()
        with original_answer_guard(guarded_run_id):
            yield

    monkeypatch.setattr(OperatorRuntimeEngine, "_transition", gated_transition)
    monkeypatch.setattr(cognition_module, "run_lifecycle_guard", observed_answer_guard)

    def cancel():
        with SessionLocal() as db:
            return OperatorRuntimeEngine(db).cancel_run(run_id, notify_chat=False).current_state

    def answer():
        with SessionLocal() as db:
            return OperatorChatService(db).answer_question(
                question_id=question_id,
                answer="Do not store this after cancellation",
                auto_resume=False,
                record_user_message=False,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        cancel_future = pool.submit(cancel)
        cancel_reached_transition.wait(timeout=5)
        answer_future = pool.submit(answer)
        assert answer_guard_attempted.wait(timeout=5)
        assert not answer_future.done()
        release_cancel.set()
        assert cancel_future.result(timeout=5) == OperatorState.CANCELLED.value
        with pytest.raises(ValueError, match="operator run is terminal"):
            answer_future.result(timeout=5)

    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        question = db.get(models.OperatorQuestion, question_id)
        assert run.current_state == OperatorState.CANCELLED.value
        assert question.status == "superseded"
        assert question.answer_payload == {}
