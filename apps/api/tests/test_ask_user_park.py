"""An ask_user parks the in-browser loop in place until the user's answer (or the window) arrives."""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from typing import Any

from sqlalchemy import select

from app import extension_run, models
from app.cognition import OperatorChatService
from app.db import SessionLocal, init_db
from app.extension_run import drive_in_browser_application
from app.runtime import OperatorRuntimeEngine

QUESTION = "Which colour should the banner use?"


class _ParkBody:
    device_id = "park-device"

    def __init__(self) -> None:
        self.captures = 0

    async def capture_screenshot(self) -> dict:
        self.captures += 1
        return {
            "ok": True,
            "image": "jpeg",
            "width": 800,
            "height": 600,
            "url": "https://example.test",
            "title": "Example",
            "scroll_y": 0,
            "scroll_height": 600,
            "page_evidence": {"viewport_text": "Pick a banner colour: Red or Blue.", "controls": []},
        }

    async def vision_wait(self, wait_ms: int, **_kw: Any) -> dict:
        return {"ok": True, "action": "wait", "waited_ms": wait_ms}


class _ContextEngine:
    def __init__(self, db: object) -> None:
        pass

    def compact_defaults_with_provenance(self, user_id: int, include_seeded_facts: bool = True) -> dict:
        return {"values": {}}


def _make_run() -> tuple[int, int]:
    init_db()
    with SessionLocal() as db:
        user = models.User(email=f"ask-park-{uuid.uuid4().hex}@example.com", name="Park")
        db.add(user)
        db.flush()
        run = models.OperatorRun(
            user_id=user.id,
            goal="Set the banner colour",
            target_url="https://example.test",
            current_state="RUNNING",
            confidence_state={"in_browser": {"device_id": f"park-{uuid.uuid4().hex}", "execution_surface": "browser"}},
        )
        db.add(run)
        db.flush()
        thread = models.ChatThread(user_id=user.id, run_id=run.id, title="park", mode="run")
        db.add(thread)
        db.commit()
        return int(run.id), int(thread.id)


def _pending_question_id(run_id: int) -> int | None:
    with SessionLocal() as db:
        question = db.scalars(
            select(models.OperatorQuestion).where(
                models.OperatorQuestion.run_id == run_id,
                models.OperatorQuestion.status == "pending",
            )
        ).first()
        return int(question.id) if question else None


def _state(run_id: int) -> str:
    with SessionLocal() as db:
        return str(db.get(models.OperatorRun, run_id).current_state)


def _when_parked(run_id: int, then, *, timeout: float = 20.0) -> threading.Thread:
    """Run `then()` from another thread (like the answer endpoint) once the loop is parked."""

    def wait_and_act() -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if run_id in extension_run._PARKED_ASKS and _pending_question_id(run_id) and _state(run_id) == "WAITING_FOR_USER":
                then()
                return
            time.sleep(0.02)

    worker = threading.Thread(target=wait_and_act, daemon=True)
    worker.start()
    return worker


def _install(monkeypatch, decide, *, window: float, body_reachable: bool = True) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    real_db_event = extension_run._db_event

    def record(run_id: int, event_type: str, payload: dict) -> None:
        events.append((event_type, dict(payload or {})))
        real_db_event(run_id, event_type, payload)

    monkeypatch.setattr("app.candidate_context.CandidateContextEngine", _ContextEngine)
    monkeypatch.setattr(extension_run, "_db_event", record)
    monkeypatch.setattr(extension_run, "_record_context_usage_for_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(extension_run, "_decide_vision_action", decide)
    monkeypatch.setattr(
        extension_run,
        "_call_secret_request_classifier",
        lambda *args, **kwargs: {
            "verdict": "ordinary",
            "confidence": 0.99,
            "reason": "A colour preference.",
            "evidence": "Pick a banner colour.",
            "metadata": {"provider": "fixture", "model": "fixture", "normalization": "validated"},
        },
    )
    monkeypatch.setattr(extension_run, "_ask_user_park_seconds", lambda: window)
    monkeypatch.setattr(extension_run, "_ASK_USER_PARK_POLL_SECONDS", 0.05)
    monkeypatch.setattr(extension_run, "_parked_ask_body_reachable", lambda run_id: body_reachable)
    return events


def _ask() -> dict:
    return {"action": "ask_user", "question": QUESTION, "options": ["Red", "Blue"], "reason": "The colour is the user's call."}


def _drive(run_id: int, thread_id: int, body: _ParkBody, *, max_loops: list[int]) -> dict:
    real_loop = extension_run.run_vision_loop

    async def counted_loop(*args: Any, **kwargs: Any) -> dict:
        max_loops.append(1)
        kwargs["max_steps"] = 4
        return await real_loop(*args, **kwargs)

    extension_run.run_vision_loop = counted_loop
    try:
        return asyncio.run(
            drive_in_browser_application(body=body, run_id=run_id, thread_id=thread_id, url=None, skip_navigate=True)
        )
    finally:
        extension_run.run_vision_loop = real_loop


def test_an_answer_while_parked_continues_the_same_loop_with_the_answer(monkeypatch) -> None:
    run_id, thread_id = _make_run()
    contexts: list[dict] = []
    decisions = iter([
        {"action": "wait", "wait_ms": 100, "reason": "Read the page.", "notes": "banner is on the home page"},
        _ask(),
    ])

    def decide(user_id, image, view, context):
        contexts.append(context)
        return dict(next(decisions, {"action": "wait", "wait_ms": 100, "reason": "Apply the answer."}))

    events = _install(monkeypatch, decide, window=30.0)
    states_while_parked: list[str] = []

    def answer() -> None:
        with SessionLocal() as db:
            states_while_parked.append(str(db.get(models.OperatorRun, run_id).current_state))
            result = OperatorChatService(db).answer_question(question_id=_pending_question_id(run_id), answer="Blue")
            states_while_parked.append(f"resumed={result['resumed']}")

    worker = _when_parked(run_id, answer)
    loops: list[int] = []
    body = _ParkBody()
    summary = _drive(run_id, thread_id, body, max_loops=loops)
    worker.join(5)

    assert states_while_parked == ["WAITING_FOR_USER", "resumed=True"], (
        "while parked the run shows it is waiting for the user, and the answer path reports the resume"
    )
    assert len(loops) == 1, "the answer continued the same loop, it did not start a new one"
    after = contexts[2]
    assert any(item.get("answer") == "Blue" for item in after["human_answers"]), "the answer came through human_answers"
    assert QUESTION in after["already_asked"], "the loop still knows what it asked"
    assert any("user_replied" in line for line in after["recent_actions"])
    assert after["your_notes"] == ["step 1: banner is on the home page"]
    assert len(contexts) == 2 + 4, "the answer gives the loop a fresh loop's step budget (max_steps=4)"
    parked = [payload for kind, payload in events if kind == "VISION_ASK_PARK_ENDED"]
    assert parked and parked[0]["outcome"] == "answered"
    assert run_id not in extension_run._PARKED_ASKS
    assert summary["reason"] == "max_steps", "the continued loop ran on to its (fake) budget"


def _published_shape(run_id: int) -> dict:
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        questions = db.scalars(select(models.OperatorQuestion).where(models.OperatorQuestion.run_id == run_id)).all()
        tasks = db.scalars(select(models.RuntimeTask).where(models.RuntimeTask.run_id == run_id)).all()
        messages = db.scalars(
            select(models.ChatMessage).where(models.ChatMessage.thread_id == db.scalars(
                select(models.ChatThread.id).where(models.ChatThread.run_id == run_id)
            ).first())
        ).all()
        return {
            "state": run.current_state,
            "status": run.status_message,
            "needs_user": (run.confidence_state or {}).get("end_to_end", {}).get("needs_user"),
            "questions": sorted((q.question_type, q.status) for q in questions),
            "tasks": sorted((t.task_type, t.status) for t in tasks),
            "asks": [m.content for m in messages if (m.payload_json or {}).get("source") == "in_browser_question"],
            "journal": bool((run.confidence_state or {}).get("vision_run_journal")),
        }


def test_no_answer_in_the_window_ends_exactly_as_an_unparked_ask(monkeypatch) -> None:
    shapes = []
    summaries = []
    for window in (0.0, 0.4):
        run_id, thread_id = _make_run()
        decisions = iter([_ask()])
        _install(monkeypatch, lambda *a, **k: dict(next(decisions, _ask())), window=window)
        summaries.append(_drive(run_id, thread_id, _ParkBody(), max_loops=[]))
        shapes.append(_published_shape(run_id))
        assert run_id not in extension_run._PARKED_ASKS

    unparked, timed_out = shapes
    assert unparked == timed_out
    assert timed_out["state"] == "WAITING_FOR_USER"
    assert len(timed_out["asks"]) == 1, "the question is published once"
    assert timed_out["questions"] == [("field_approval", "pending")]
    for summary in summaries:
        assert (summary["state"], summary["reason"], summary["ask"]["label"]) == ("WAITING_FOR_USER", "needs_answer", QUESTION)


def test_cancel_while_parked_ends_the_run_cancelled(monkeypatch) -> None:
    run_id, thread_id = _make_run()
    decisions = iter([_ask()])
    _install(monkeypatch, lambda *a, **k: dict(next(decisions, _ask())), window=30.0)

    def cancel() -> None:
        with SessionLocal() as db:
            OperatorRuntimeEngine(db).cancel_run(run_id)

    worker = _when_parked(run_id, cancel)
    started = time.monotonic()
    summary = _drive(run_id, thread_id, _ParkBody(), max_loops=[])
    worker.join(5)

    assert summary["state"] == "CANCELLED"
    assert time.monotonic() - started < 15, "cancel does not wait out the park window"
    assert run_id not in extension_run._PARKED_ASKS
    with SessionLocal() as db:
        assert db.get(models.OperatorRun, run_id).current_state == "CANCELLED"
    # A late answer finds nothing parked.
    assert extension_run._wake_parked_ask(run_id) is False


def test_a_restart_while_parked_leaves_an_ordinary_published_question(monkeypatch) -> None:
    run_id, thread_id = _make_run()
    decisions = iter([
        {"action": "wait", "wait_ms": 100, "reason": "Read the page.", "notes": "banner is on the home page"},
        _ask(),
    ])
    _install(monkeypatch, lambda *a, **k: dict(next(decisions, _ask())), window=1.5)
    seen: dict[str, Any] = {}

    def restart() -> None:
        # What a new process sees: no drive coroutine and no parked registry, only the database.
        with SessionLocal() as db:
            seen["reconciled"] = OperatorRuntimeEngine(db).reconcile_orphaned_runs_after_restart()
        seen["shape"] = _published_shape(run_id)
        seen["journal"] = extension_run._load_run_journal_for_run(run_id)

    worker = _when_parked(run_id, restart)
    _drive(run_id, thread_id, _ParkBody(), max_loops=[])
    worker.join(5)

    assert run_id not in seen["reconciled"], "a parked run is WAITING_FOR_USER, not an orphaned RUNNING row"
    assert seen["shape"]["state"] == "WAITING_FOR_USER"
    assert seen["shape"]["questions"] == [("field_approval", "pending")]
    assert [note["text"] for note in seen["journal"]["notes"]] == ["banner is on the home page"], (
        "the journal is persisted before parking, so a resume after a restart has it"
    )


def test_an_answer_with_the_body_gone_takes_the_ordinary_resume_path(monkeypatch) -> None:
    run_id, thread_id = _make_run()
    decisions = iter([_ask()])
    events = _install(monkeypatch, lambda *a, **k: dict(next(decisions, _ask())), window=1.0, body_reachable=False)
    results: list[Any] = []

    def answer() -> None:
        with SessionLocal() as db:
            results.append(OperatorChatService(db).answer_question(question_id=_pending_question_id(run_id), answer="Blue")["resumed"])

    worker = _when_parked(run_id, answer)
    loops: list[int] = []
    summary = _drive(run_id, thread_id, _ParkBody(), max_loops=loops)
    worker.join(5)

    assert results == [False], "the parked loop is not woken onto a body that is gone"
    assert len(loops) == 1 and summary["reason"] == "needs_answer"
    assert [payload["outcome"] for kind, payload in events if kind == "VISION_ASK_PARK_ENDED"] == ["expired"]
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        assert run.current_state == "WAITING_FOR_USER"
        assert (run.confidence_state or {}).get("in_browser_resume_retry", {}).get("pending") is True, (
            "the saved answer waits for the body exactly as it does without parking"
        )


def test_the_stall_watchdog_ignores_a_suspended_run() -> None:
    watchdog = extension_run._RunStallWatchdog(0, threshold=0.05, poll=0.01)

    async def main() -> bool:
        task = asyncio.ensure_future(asyncio.sleep(0.4))
        watchdog.start(asyncio.get_running_loop(), task)
        watchdog.suspend()
        await asyncio.sleep(0.3)
        fired_while_suspended = watchdog.fired
        watchdog.resume()
        watchdog.stop()
        task.cancel()
        return fired_while_suspended

    assert asyncio.run(main()) is False


def test_schedule_resume_wakes_only_a_parked_run() -> None:
    async def main() -> tuple[bool, bool, bool]:
        parked = extension_run._ParkedAsk(asyncio.get_running_loop())
        extension_run._PARKED_ASKS[987654] = parked
        try:
            first = extension_run._wake_parked_ask(987654)
            await asyncio.sleep(0)
            second = extension_run._wake_parked_ask(987654)
            return first, second, parked.event.is_set()
        finally:
            extension_run._PARKED_ASKS.pop(987654, None)

    assert asyncio.run(main()) == (True, False, True)


def test_a_turn_that_does_not_answer_the_parked_question_ends_the_park(monkeypatch) -> None:
    # Review: every human turn woke the parked loop, so approving an older final action while parked on
    # another question continued the loop past the driver's approved-action path. Only the parked
    # question's answer wakes it; any other turn closes the park (the run ends as its published question).
    run_id, thread_id = _make_run()
    decisions = iter([_ask()])

    def decide(user_id, image, view, context):
        return dict(next(decisions, {"action": "wait", "wait_ms": 100, "reason": "Pause."}))

    events = _install(monkeypatch, decide, window=30.0)
    woke: list[bool] = []
    worker = _when_parked(run_id, lambda: woke.append(extension_run.schedule_resume_if_in_browser(run_id)))
    loops: list[int] = []
    summary = _drive(run_id, thread_id, _ParkBody(), max_loops=loops)
    worker.join(5)

    assert woke == [False], "the turn did not wake the parked loop"
    assert _pending_question_id(run_id) is not None, "the parked question is still unanswered"
    ended = [payload for kind, payload in events if kind == "VISION_ASK_PARK_ENDED"]
    assert ended and ended[0]["outcome"] == "expired", "the park closed instead of continuing as answered"
    assert summary["state"] == "WAITING_FOR_USER"
    assert run_id not in extension_run._PARKED_ASKS


def test_the_default_park_window_is_thirty_minutes() -> None:
    from app.config import Settings

    assert Settings.model_fields["ask_user_park_seconds"].default == 1800.0


def test_an_answer_after_the_window_closed_resumes_the_run_with_the_answer(monkeypatch) -> None:
    # The owner answers from a phone while away (runs 1791/1796): the window closes with the question
    # published, the run waits WAITING_FOR_USER, and a much later answer must resume it from its body.
    # The in_browser marker is gone by then; the run's durable browser task still makes it a browser run.
    from app.extension_hub import REQUIRED_LIVE_ACTION_CAPABILITIES, hub

    run_id, thread_id = _make_run()
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        run.confidence_state = {
            **(run.confidence_state or {}),
            "browser_task": {
                "schema_version": 4,
                "goal": "Set the banner colour",
                "target_url": "https://example.test",
                "execution_surface": "browser",
            },
        }
        db.commit()
    contexts: list[dict] = []
    decisions = iter([_ask()])

    def decide(user_id, image, view, context):
        contexts.append(context)
        return dict(next(decisions, {"action": "wait", "wait_ms": 100, "reason": "Apply the answer."}))

    events = _install(monkeypatch, decide, window=0.2)
    first = _drive(run_id, thread_id, _ParkBody(), max_loops=[])

    assert (first["state"], first["reason"]) == ("WAITING_FOR_USER", "needs_answer")
    assert [payload["outcome"] for kind, payload in events if kind == "VISION_ASK_PARK_ENDED"] == ["expired"]
    assert run_id not in extension_run._PARKED_ASKS, "nothing is parked any more"
    question_id = _pending_question_id(run_id)
    assert question_id is not None, "the question stays published and pending"

    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        state = dict(run.confidence_state or {})
        state.pop("in_browser", None)
        run.confidence_state = state
        db.commit()

    saved_connections = dict(hub._connections)
    saved_loop = hub.loop
    try:
        hub._connections.clear()
        device_id = f"park-{uuid.uuid4().hex[:8]}"
        connection = hub.register(device_id, object())
        assert connection.accept_hello(
            {
                "type": "hello",
                "device_id": device_id,
                "capabilities": {capability: True for capability in REQUIRED_LIVE_ACTION_CAPABILITIES},
            }
        )
        hub.loop = object()
        scheduled: list = []
        monkeypatch.setattr(extension_run.asyncio, "run_coroutine_threadsafe", lambda coro, loop: scheduled.append(coro))
        monkeypatch.setattr("app.extension_body.ExtensionBody", lambda connection: _ParkBody())

        with SessionLocal() as db:
            result = OperatorChatService(db).answer_question(question_id=question_id, answer="Blue")

        assert result["resumed"] is True, "the late answer schedules the drive"
        assert len(scheduled) == 1, "exactly one drive is scheduled"
        with SessionLocal() as db:
            marker = db.get(models.OperatorRun, run_id).confidence_state["in_browser"]
        assert marker["device_id"] == device_id, "the body is rebound from the live connection"

        counted: list[int] = []
        real_loop = extension_run.run_vision_loop

        async def counted_loop(*args: Any, **kwargs: Any) -> dict:
            counted.append(1)
            kwargs["max_steps"] = 4
            return await real_loop(*args, **kwargs)

        monkeypatch.setattr(extension_run, "run_vision_loop", counted_loop)
        summary = asyncio.run(scheduled[0])
    finally:
        hub._connections.clear()
        hub._connections.update(saved_connections)
        hub.loop = saved_loop

    assert counted == [1], "the resumed drive runs one loop"
    assert summary["reason"] == "max_steps"
    resumed_context = contexts[1]
    assert any(item.get("answer") == "Blue" for item in resumed_context["human_answers"]), (
        "the drive sees the answer that arrived after the window closed"
    )
