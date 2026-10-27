"""A generic 'I am stuck' (blocked_reason) card must not linger after the run's blocker has moved on.

Regression: the answered "expected salary" blocker stayed on screen while the run had already moved to
the next field, because a generic blocked_reason question only mirrors run.status_message and wasn't
superseded when that changed."""

import uuid

from app import models
from app.cognition import OperatorChatService
from app.db import SessionLocal, init_db


def _run(db, status_message: str) -> models.OperatorRun:
    user = models.User(email=f"stale-{uuid.uuid4().hex}@example.com", name="U")
    db.add(user)
    db.flush()
    run = models.OperatorRun(
        user_id=user.id,
        goal="Apply",
        target_url="https://ats.example.com/apply",
        current_state="WAITING_FOR_USER",
        status_message=status_message,
    )
    db.add(run)
    db.commit()
    return run


def _blocked_question(db, run, stuck_reason: str) -> models.OperatorQuestion:
    q = models.OperatorQuestion(
        run_id=run.id,
        question_type="blocked_reason",
        stuck_phase="X",
        stuck_reason=stuck_reason,
        prompt=f"I am stuck because: {stuck_reason}.",
        expected_answer_schema={"answer": "string", "memory_update": "optional object"},
        status="pending",
    )
    db.add(q)
    db.commit()
    return q


def test_stale_blocked_reason_is_superseded_when_blocker_moved() -> None:
    init_db()
    with SessionLocal() as db:
        run = _run(db, "Waiting for your answer: Please identify your race")
        stale = _blocked_question(db, run, "Waiting for your answer: What is your expected salary?")
        OperatorChatService(db)._reconcile_stale_task_questions(run.id)
        db.commit()
        assert db.get(models.OperatorQuestion, stale.id).status == "superseded"


def test_current_blocked_reason_is_kept() -> None:
    init_db()
    with SessionLocal() as db:
        run = _run(db, "Waiting for your answer: Please identify your race")
        current = _blocked_question(db, run, "Waiting for your answer: Please identify your race")
        OperatorChatService(db)._reconcile_stale_task_questions(run.id)
        db.commit()
        assert db.get(models.OperatorQuestion, current.id).status == "pending"
