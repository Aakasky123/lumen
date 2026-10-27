"""A re-ask of a field whose approved answer is already in place gets its own field card.

Run 1791: "Which of the following factors are most important to you? (Select 3)" was approved with one pick
("Working on challenging technical problems"). The brain's next ask for the same field (the rest of the
set) matched the approved task by its field key, the upsert returned that approved task without creating a
pending one, and the run fell through to a generic "I am stuck" card with no options. The in-browser path
also dropped the ask's `multiple` flag, so a checkbox group could never be a multi-select card.
"""

import uuid

from sqlalchemy import select

from app import models
from app.db import SessionLocal, init_db
from app.extension_run import _create_in_browser_question, _human_answers_for_run

LABEL = "Which of the following factors are most important to you? (Select 3)"
FACTORS = [
    "Working on challenging technical problems",
    "Fast-paced, high-growth startup environment",
    "Opportunities for ownership and autonomy",
    "Work-life balance",
]


def _run_id() -> int:
    with SessionLocal() as db:
        user = models.User(email=f"reask-{uuid.uuid4().hex}@example.com", name="Reask")
        db.add(user)
        db.flush()
        run = models.OperatorRun(
            user_id=user.id,
            goal="Apply",
            target_url="https://jobs.example.com/apply",
            current_state="WAITING_FOR_USER",
            status_message=f"Waiting for your answer: {LABEL}",
            confidence_state={"in_browser": {"url": "https://jobs.example.com/apply", "device_id": "test-device"}},
        )
        db.add(run)
        db.commit()
        return int(run.id)


def _ask(run_id: int, *, multiple: bool = True) -> bool:
    # The brain's ask bound to control 0: no node id, so the field key falls back to the label (run 1791).
    return _create_in_browser_question(
        run_id,
        {
            "label": LABEL,
            "options": list(FACTORS),
            "is_choice": True,
            "multiple": multiple,
            "required": True,
            "is_unsure": True,
            "role": "input/checkbox",
            "control_kind": "input/checkbox",
            "selector": "",
            "node_id": "",
        },
    )


def _approve(task_id: int, value: str) -> None:
    with SessionLocal() as db:
        task = db.get(models.RuntimeTask, task_id)
        task.status = "approved"
        task.payload = {
            **(task.payload or {}),
            "approved_value": value,
            "interpreted_answer": {"resolved_value": value, "source": "user_picked_options"},
        }
        db.commit()


def _field_tasks(run_id: int) -> list[models.RuntimeTask]:
    with SessionLocal() as db:
        return list(
            db.scalars(
                select(models.RuntimeTask)
                .where(models.RuntimeTask.run_id == run_id, models.RuntimeTask.task_type == "field_approval")
                .order_by(models.RuntimeTask.id.asc())
            ).all()
        )


def _pending_questions(run_id: int) -> list[models.OperatorQuestion]:
    with SessionLocal() as db:
        return list(
            db.scalars(
                select(models.OperatorQuestion).where(
                    models.OperatorQuestion.run_id == run_id,
                    models.OperatorQuestion.status == "pending",
                )
            ).all()
        )


def test_reask_of_an_approved_field_becomes_a_field_card_with_its_options() -> None:
    init_db()
    run_id = _run_id()
    assert _ask(run_id) is True
    [first] = _field_tasks(run_id)
    _approve(first.id, "Working on challenging technical problems")

    assert _ask(run_id) is True

    # The re-ask is a field card (not a generic blocked_reason) with this ask's options, multi-select kept.
    pending = _pending_questions(run_id)
    assert [question.question_type for question in pending] == ["field_approval"]
    schema = pending[0].expected_answer_schema
    assert schema["fields"][0]["options"] == FACTORS
    assert schema["fields"][0]["multiple"] is True

    # It is a new pending task that supersedes the approved one; the approved answer keeps its status
    # because human_answers (what the brain is told) reads approved tasks.
    new_task_id = int(schema["task_ids"][0])
    assert new_task_id != first.id
    with SessionLocal() as db:
        new_task = db.get(models.RuntimeTask, new_task_id)
        assert new_task.status == "pending"
        assert new_task.payload["supersedes_task_id"] == first.id
        assert db.get(models.RuntimeTask, first.id).status == "approved"

    # The brain still hears the approved pick through human_answers.
    assert any(item["answer"] == "Working on challenging technical problems" for item in _human_answers_for_run(run_id))


def test_an_in_browser_checkbox_group_is_a_multi_select_card() -> None:
    init_db()
    run_id = _run_id()
    assert _ask(run_id, multiple=True) is True

    [task] = _field_tasks(run_id)
    assert task.payload["multiple"] is True
    [question] = _pending_questions(run_id)
    assert question.expected_answer_schema["fields"][0]["multiple"] is True


def test_a_repeated_ask_while_the_card_is_pending_updates_that_card() -> None:
    init_db()
    run_id = _run_id()
    assert _ask(run_id) is True
    assert _ask(run_id) is True

    tasks = _field_tasks(run_id)
    assert len(tasks) == 1 and tasks[0].status == "pending"
    [question] = _pending_questions(run_id)
    assert question.question_type == "field_approval"
    assert question.expected_answer_schema["task_ids"] == [tasks[0].id]
