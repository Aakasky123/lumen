"""A reply to a live browser run is routed by the run's durable surface, not by the transient in_browser
marker (run 1791: the marker was absent at the reply, so a reply to a blocked-reason question was filed to
the goal workspace with can_resume false and the run never woke). A reply to a waiting browser run is an
operator instruction with a resume request; the body decides only whether the resume starts now.
"""

from __future__ import annotations

import uuid

import pytest

from app import extension_run, models
from app.cognition import OperatorChatService
from app.db import SessionLocal, init_db
from app.extension_hub import REQUIRED_LIVE_ACTION_CAPABILITIES, hub

URL = "https://jobs.example.test/apply"
BLOCKED_PROMPT = (
    "I am stuck because: Waiting for your answer: Which of the following factors are most important to you? "
    "(Select 3). Tell me the missing fact, correction, or instruction to store before I continue."
)
ANSWER = "Strong engineering culture"


def _make_user() -> int:
    init_db()
    with SessionLocal() as db:
        user = models.User(email=f"reply-route-{uuid.uuid4().hex}@example.com", name="Reply")
        db.add(user)
        db.flush()
        user_id = int(user.id)
        db.commit()
        return user_id


def _browser_run(*, marker: bool, task: bool = True) -> tuple[int, int]:
    """A run waiting on a blocked-reason question, shaped like run 1791 after its park window closed.
    Returns (run_id, question_id). The durable browser_task is kept; the in_browser marker is optional."""
    user_id = _make_user()
    confidence: dict = {"final_action_policy": {"final_action_mode": "autonomous"}}
    if task:
        confidence["browser_task"] = {
            "schema_version": 4,
            "goal": "Apply for the job",
            "target_url": URL,
            "execution_surface": "browser",
        }
    if marker:
        confidence["in_browser"] = {"url": URL, "device_id": f"dev-{uuid.uuid4().hex[:8]}"}
    with SessionLocal() as db:
        run = models.OperatorRun(
            user_id=user_id,
            goal="Apply for the job",
            target_url=URL,
            current_state="WAITING_FOR_USER",
            confidence_state=confidence,
            status_message="Waiting for your answer: Which of the following factors are most important to you? (Select 3)",
        )
        db.add(run)
        db.flush()
        thread = models.ChatThread(user_id=user_id, run_id=run.id, title="reply", mode="run")
        db.add(thread)
        db.flush()
        question = models.OperatorQuestion(
            run_id=run.id,
            thread_id=thread.id,
            question_type="blocked_reason",
            stuck_phase="WAITING_FOR_USER",
            stuck_reason=run.status_message,
            prompt=BLOCKED_PROMPT,
            expected_answer_schema={"answer": "string", "memory_update": "optional object"},
            status="pending",
        )
        db.add(question)
        db.commit()
        return int(run.id), int(question.id)


def _answer(question_id: int) -> dict:
    with SessionLocal() as db:
        return OperatorChatService(db).answer_question(question_id=question_id, answer=ANSWER)


def _run_state(run_id: int) -> dict:
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        return {"state": str(run.current_state), "confidence": dict(run.confidence_state or {})}


@pytest.fixture(autouse=True)
def _isolate_hub():
    saved_connections = dict(hub._connections)
    saved_loop = hub.loop
    hub._connections.clear()
    hub.loop = None
    try:
        yield
    finally:
        hub._connections.clear()
        hub._connections.update(saved_connections)
        hub.loop = saved_loop


def _register_ready_body() -> str:
    device_id = f"dev-{uuid.uuid4().hex[:8]}"
    connection = hub.register(device_id, object())
    assert connection.accept_hello(
        {
            "type": "hello",
            "device_id": device_id,
            "capabilities": {capability: True for capability in REQUIRED_LIVE_ACTION_CAPABILITIES},
        }
    )
    return device_id


def _capture_schedule(monkeypatch) -> list:
    scheduled: list = []

    def fake_schedule(coro, loop):
        scheduled.append((coro, loop))
        coro.close()  # the drive itself is not under test here
        return None

    monkeypatch.setattr(extension_run.asyncio, "run_coroutine_threadsafe", fake_schedule)
    return scheduled


@pytest.mark.parametrize(
    ("state", "surface"),
    [
        ({"browser_task": {"execution_surface": "browser"}}, "browser"),
        ({"browser_task": {"execution_surface": "desktop"}}, "desktop"),
        ({"browser_task": {}}, "browser"),
        ({"in_browser": {"device_id": "dev-1"}}, "browser"),
        ({"in_browser": {"device_id": "dev-1", "execution_surface": "desktop"}}, "desktop"),
        ({"browser_task": {"execution_surface": "api"}}, None),
        ({}, None),
    ],
)
def test_the_run_surface_comes_from_the_durable_task_or_the_marker(state: dict, surface: str | None) -> None:
    assert extension_run.run_surface(state) == surface
    assert extension_run.run_is_in_browser(state) is (surface is not None)


def test_a_reply_without_the_marker_is_an_operator_instruction_kept_for_the_body() -> None:
    run_id, question_id = _browser_run(marker=False)

    result = _answer(question_id)

    assert result["applied"]["target"] == "operator_instruction", "the reply is not filed as a workspace note"
    assert result["can_resume"] is True
    assert result["resumed"] is False, "no body is connected, so the resume waits for one"
    after = _run_state(run_id)
    assert after["state"] == "WAITING_FOR_USER"
    assert after["confidence"]["operator_instructions"][-1]["instruction"] == ANSWER
    retry = after["confidence"]["in_browser_resume_retry"]
    assert retry["pending"] is True and retry["source"] == "operator_answer", "the saved reply is retried, not dropped"


def test_the_routing_event_records_the_marker_and_the_durable_surface() -> None:
    run_id, question_id = _browser_run(marker=False)

    _answer(question_id)

    with SessionLocal() as db:
        events = [
            event
            for event in db.query(models.CognitiveEvent).filter(models.CognitiveEvent.run_id == run_id).all()
            if event.event_type == "OPERATOR_ANSWER_APPLIED"
        ]
    assert events, "the answer is recorded"
    routing = events[-1].event_payload["routing"]
    assert routing == {
        "in_browser_marker": False,
        "durable_surface": "browser",
        "run_state_before_answer": "WAITING_FOR_USER",
    }


def test_a_reply_with_a_live_body_resumes_and_rebinds_the_marker(monkeypatch) -> None:
    run_id, question_id = _browser_run(marker=False)
    device_id = _register_ready_body()
    hub.loop = object()
    scheduled = _capture_schedule(monkeypatch)

    result = _answer(question_id)

    assert result["applied"]["target"] == "operator_instruction"
    assert result["resumed"] is True, result.get("resume_error")
    assert len(scheduled) == 1, "exactly one drive is scheduled onto the body"
    marker = _run_state(run_id)["confidence"]["in_browser"]
    assert marker == {"url": URL, "device_id": device_id}, "the rebuilt binding is kept on the run"


def test_a_reply_to_a_run_that_never_drove_a_body_stays_a_workspace_note() -> None:
    run_id, question_id = _browser_run(marker=False, task=False)

    result = _answer(question_id)

    assert result["applied"]["target"] == "goal_workspace"
    assert result["can_resume"] is False
    assert _run_state(run_id)["confidence"].get("in_browser") is None
