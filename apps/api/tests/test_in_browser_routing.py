"""A chat-started application drives the connected extension body (the user's REAL Chrome) instead
of launching Playwright/Chromium — and only one application runs on a body at a time. Regression
guard for (a) the "latest run opened a Chromium tab" bug (routing now lives server-side, not in a
brittle frontend regex) and (b) the "body busy" guard (no second application on the same tab until
the current one is done). See memory: in-browser-extension-architecture.
"""

import uuid

import pytest
from sqlalchemy import select

from app import extension_run, models
from app import desktop_body
from app.db import SessionLocal, init_db
from app.extension_hub import REQUIRED_LIVE_ACTION_CAPABILITIES, hub


def _make_user() -> int:
    init_db()
    with SessionLocal() as db:
        user = models.User(email=f"route-{uuid.uuid4().hex}@example.com", name="Route")
        db.add(user)
        db.flush()
        uid = int(user.id)
        db.commit()
        return uid


def _make_deferred_run(uid: int, confidence: dict, *, url: str = "https://ats.example.com/x/apply", state: str = "STARTING_BROWSER") -> tuple[int, int]:
    """A run + its chat thread, shaped like the deferred chat-apply path creates them."""
    from app.cognition import OperatorChatService

    with SessionLocal() as db:
        run = models.OperatorRun(user_id=uid, goal="Apply", target_url=url, current_state=state, confidence_state=confidence)
        db.add(run)
        db.flush()
        thread = OperatorChatService(db).create_thread(user_id=uid, run_id=run.id, title="t")
        db.commit()
        return run.id, thread.id


@pytest.fixture(autouse=True)
def _isolate_hub():
    saved_conns = dict(hub._connections)
    saved_loop = hub.loop
    hub._connections.clear()
    hub.loop = None
    try:
        yield
    finally:
        hub._connections.clear()
        hub._connections.update(saved_conns)
        hub.loop = saved_loop


def _capture_schedule(monkeypatch) -> list:
    scheduled: list = []

    def fake_schedule(coro, loop):
        scheduled.append((coro, loop))
        coro.close()  # avoid "coroutine was never awaited"
        return None

    monkeypatch.setattr(extension_run.asyncio, "run_coroutine_threadsafe", fake_schedule)
    return scheduled


def _register_ready_body(device_id: str):
    connection = hub.register(device_id, object())
    assert connection.accept_hello(
        {
            "type": "hello",
            "device_id": device_id,
            "capabilities": {
                capability: True
                for capability in REQUIRED_LIVE_ACTION_CAPABILITIES
            },
        }
    )
    return connection


def test_routes_to_connected_free_body_and_preserves_confidence_state(monkeypatch) -> None:
    uid = _make_user()
    dev = f"dev-{uuid.uuid4().hex[:8]}"
    rid, tid = _make_deferred_run(uid, {"real_site_execution_requested": True, "mutation_budget": 60})
    _register_ready_body(dev)
    hub.loop = object()  # sentinel; run_coroutine_threadsafe is mocked
    scheduled = _capture_schedule(monkeypatch)

    assert extension_run.route_deferred_chat_run(rid, tid)["status"] == "in_browser"
    assert len(scheduled) == 1  # the in-browser loop was scheduled onto the uvicorn loop
    with SessionLocal() as db:
        cs = db.get(models.OperatorRun, rid).confidence_state
        assert cs["in_browser"] == {"url": "https://ats.example.com/x/apply", "device_id": dev}
        # the Playwright-shaped keys are MERGED, not overwritten (consent/budget survive)
        assert cs["real_site_execution_requested"] is True
        assert cs["mutation_budget"] == 60


def test_route_rejects_cross_user_thread_run_binding_before_browser_schedule(monkeypatch) -> None:
    run_user_id = _make_user()
    thread_user_id = _make_user()
    run_id, thread_id = _make_deferred_run(run_user_id, {"real_site_execution_requested": True})
    with SessionLocal() as db:
        thread = db.get(models.ChatThread, thread_id)
        run = db.get(models.OperatorRun, run_id)
        thread.user_id = thread_user_id
        run.status_message = "Victim run is still waiting to start."
        db.commit()

    _register_ready_body(f"dev-{uuid.uuid4().hex[:8]}")
    hub.loop = object()
    scheduled = _capture_schedule(monkeypatch)
    routed = extension_run.route_deferred_chat_run(run_id, thread_id)

    assert routed["status"] == "invalid"
    assert scheduled == []
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        events = list(
            db.scalars(
                select(models.CognitiveEvent).where(
                    models.CognitiveEvent.run_id == run_id,
                    models.CognitiveEvent.event_type == "DEFERRED_BROWSER_TASK_OWNERSHIP_REJECTED",
                )
            ).all()
        )
        assert run.current_state == "STARTING_BROWSER"
        assert run.status_message == "Victim run is still waiting to start."
        assert run.confidence_state == {"real_site_execution_requested": True}
        assert events == []


def test_route_rejects_same_user_wrong_thread_without_mutating_either_run(monkeypatch) -> None:
    uid = _make_user()
    victim_run_id, victim_thread_id = _make_deferred_run(
        uid,
        {
            "real_site_execution_requested": True,
            "browser_task": {"safety_policy": {"approval_granted": False}},
        },
        state="WAITING_FOR_USER",
    )
    other_run_id, other_thread_id = _make_deferred_run(
        uid,
        {"real_site_execution_requested": True},
        url="https://other.example/task",
    )
    with SessionLocal() as db:
        victim = db.get(models.OperatorRun, victim_run_id)
        other = db.get(models.OperatorRun, other_run_id)
        victim.status_message = "Victim is waiting for an exact answer."
        other.status_message = "Other task has not started."
        db.commit()

    _register_ready_body(f"dev-{uuid.uuid4().hex[:8]}")
    hub.loop = object()
    scheduled = _capture_schedule(monkeypatch)
    routed = extension_run.route_deferred_chat_run(victim_run_id, other_thread_id)

    assert routed["status"] == "invalid"
    assert scheduled == []
    with SessionLocal() as db:
        victim = db.get(models.OperatorRun, victim_run_id)
        other = db.get(models.OperatorRun, other_run_id)
        victim_thread = db.get(models.ChatThread, victim_thread_id)
        other_thread = db.get(models.ChatThread, other_thread_id)
        rejected_events = list(
            db.scalars(
                select(models.CognitiveEvent).where(
                    models.CognitiveEvent.run_id.in_([victim_run_id, other_run_id]),
                    models.CognitiveEvent.event_type == "DEFERRED_BROWSER_TASK_OWNERSHIP_REJECTED",
                )
            ).all()
        )

        assert victim.current_state == "WAITING_FOR_USER"
        assert victim.status_message == "Victim is waiting for an exact answer."
        assert victim.confidence_state == {
            "real_site_execution_requested": True,
            "browser_task": {"safety_policy": {"approval_granted": False}},
        }
        assert other.current_state == "STARTING_BROWSER"
        assert other.status_message == "Other task has not started."
        assert victim_thread.run_id == victim_run_id
        assert other_thread.run_id == other_run_id
        assert rejected_events == []


def test_no_body_connected_blocks_without_fallback() -> None:
    uid = _make_user()
    rid, tid = _make_deferred_run(uid, {"real_site_execution_requested": True})
    hub.loop = object()  # loop captured, but NO extension connected
    routed = extension_run.route_deferred_chat_run(rid, tid)
    assert routed["status"] == "no_body"
    assert "extension body" in routed["message"]
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, rid)
        assert run.current_state == "BLOCKED"
        assert "in_browser" not in (run.confidence_state or {})


def test_no_body_with_computer_use_self_launches_chrome_instead_of_blocking(monkeypatch) -> None:
    # The user never clicks an "Open Chrome" button: when a browser task starts with no
    # extension body connected and computer use can open applications, the runtime opens
    # Chrome itself and adopts the run once the extension connects.
    uid = _make_user()
    rid, tid = _make_deferred_run(uid, {"real_site_execution_requested": True})
    hub.loop = object()
    scheduled = _capture_schedule(monkeypatch)
    launches: list[str] = []
    monkeypatch.setattr(extension_run, "_chrome_launch_available", lambda: True)
    monkeypatch.setattr(desktop_body, "chrome_profiles", lambda: [{"directory": "Default", "name": "Jordan"}])
    monkeypatch.setattr(
        extension_run,
        "_launch_chrome_for_extension_body",
        lambda profile_directory="": launches.append(profile_directory) or True,
    )

    routed = extension_run.route_deferred_chat_run(rid, tid)
    try:
        assert routed["status"] == "launching_browser"
        assert "opening Chrome" in routed["message"]
        # A single enumerated profile needs no choice: it launches directly into it.
        assert launches == ["Default"]
        assert len(scheduled) == 1  # the connect-and-adopt waiter was scheduled
        with SessionLocal() as db:
            run = db.get(models.OperatorRun, rid)
            assert run.current_state != "BLOCKED"
            assert "Waiting for Chrome" in (run.status_message or "")

        # A duplicate route while the launch is pending reports progress and does NOT
        # start a second Chrome.
        again = extension_run.route_deferred_chat_run(rid, tid)
        assert again["status"] == "launching_browser"
        assert "still opening" in again["message"]
        assert launches == ["Default"]

        # The waiter re-routes with self-launch disabled: a body that is still absent
        # blocks normally instead of looping through launches forever.
        with extension_run._CHROME_SELF_LAUNCH_LOCK:
            extension_run._CHROME_SELF_LAUNCH_RUN_IDS.discard(rid)
        blocked = extension_run.route_deferred_chat_run(rid, tid, allow_body_self_launch=False)
        assert blocked["status"] == "no_body"
        assert launches == ["Default"]
    finally:
        with extension_run._CHROME_SELF_LAUNCH_LOCK:
            extension_run._CHROME_SELF_LAUNCH_RUN_IDS.discard(rid)


def test_no_body_without_launchable_chrome_blocks_with_timeout_guidance(monkeypatch) -> None:
    uid = _make_user()
    rid, tid = _make_deferred_run(uid, {"real_site_execution_requested": True})
    hub.loop = object()
    monkeypatch.setattr(extension_run, "_chrome_launch_available", lambda: True)
    monkeypatch.setattr(desktop_body, "chrome_profiles", lambda: [])
    monkeypatch.setattr(extension_run, "_launch_chrome_for_extension_body", lambda profile_directory="": False)
    routed = extension_run.route_deferred_chat_run(rid, tid)
    assert routed["status"] == "no_body"
    # And the timeout reason renders actionable guidance for the waiter's failure path.
    assert "chrome://extensions" in extension_run._extension_body_required_message(
        "chrome_self_launch_connect_timeout"
    )


def test_multiple_profiles_ask_the_user_then_launch_the_exact_choice(monkeypatch) -> None:
    # Which Chrome profile is the USER's decision: the runtime enumerates (eyes), asks
    # through the question machinery, stores the answer, and launches exactly that
    # profile (hands). Never chosen in code.
    uid = _make_user()
    rid, tid = _make_deferred_run(uid, {"real_site_execution_requested": True})
    hub.loop = object()
    _capture_schedule(monkeypatch)
    launches: list[str] = []
    monkeypatch.setattr(extension_run, "_chrome_launch_available", lambda: True)
    monkeypatch.setattr(
        desktop_body,
        "chrome_profiles",
        lambda: [
            {"directory": "Default", "name": "Jordan"},
            {"directory": "Profile 1", "name": "Work"},
        ],
    )
    monkeypatch.setattr(
        extension_run,
        "_launch_chrome_for_extension_body",
        lambda profile_directory="": launches.append(profile_directory) or True,
    )

    routed = extension_run.route_deferred_chat_run(rid, tid)
    assert routed["status"] == "awaiting_profile_choice"
    assert "Jordan" in routed["message"] and "Work" in routed["message"]
    assert launches == []

    with SessionLocal() as db:
        question = db.scalars(
            select(models.OperatorQuestion).where(
                models.OperatorQuestion.run_id == rid,
                models.OperatorQuestion.question_type == "chrome_profile_choice",
            )
        ).first()
        assert question is not None
        assert question.status == "pending"
        assert question.expected_answer_schema["profile_options"][1]["directory"] == "Profile 1"

    # Re-routing while the question is pending re-surfaces the same ask, no duplicates.
    again = extension_run.route_deferred_chat_run(rid, tid)
    assert again["status"] == "awaiting_profile_choice"
    with SessionLocal() as db:
        count = len(
            db.scalars(
                select(models.OperatorQuestion).where(
                    models.OperatorQuestion.run_id == rid,
                    models.OperatorQuestion.question_type == "chrome_profile_choice",
                )
            ).all()
        )
        assert count == 1

    # The user's answer binds to one exact option, is stored, and the launch continues
    # into exactly that profile.
    from app.agent_settings import get_agent_settings
    from app.cognition import OperatorChatService

    with SessionLocal() as db:
        service = OperatorChatService(db)
        thread = db.get(models.ChatThread, tid)
        question = db.scalars(
            select(models.OperatorQuestion).where(
                models.OperatorQuestion.run_id == rid,
                models.OperatorQuestion.question_type == "chrome_profile_choice",
            )
        ).first()
        result = service._answer_chrome_profile_choice_from_chat(thread, question, "use the work one")
        db.commit()
        assert result["applied"] is True
        assert result["resolved_value"] == "Work"
        assert get_agent_settings(db, uid)["chrome_profile_directory"] == "Profile 1"
    assert launches == ["Profile 1"]

    # Ambiguity never guesses: a reply matching nothing re-asks with the options.
    with SessionLocal() as db:
        service = OperatorChatService(db)
        thread = db.get(models.ChatThread, tid)
        question = db.scalars(
            select(models.OperatorQuestion).where(
                models.OperatorQuestion.run_id == rid,
            )
        ).first()
        question.status = "pending"
        unclear = service._answer_chrome_profile_choice_from_chat(thread, question, "the other other one")
        assert unclear["applied"] is False
        assert "Jordan" in unclear["assistant_text"]
    with extension_run._CHROME_SELF_LAUNCH_LOCK:
        extension_run._CHROME_SELF_LAUNCH_RUN_IDS.discard(rid)


def test_connected_stale_extension_requires_reload_before_live_route(monkeypatch) -> None:
    uid = _make_user()
    rid, tid = _make_deferred_run(uid, {"real_site_execution_requested": True})
    device_id = f"dev-stale-{uuid.uuid4().hex[:8]}"
    hub.register(device_id, object())
    hub.loop = object()
    scheduled = _capture_schedule(monkeypatch)

    routed = extension_run.route_deferred_chat_run(rid, tid)

    assert routed["status"] == "no_body"
    assert "Reload the extension" in routed["message"]
    assert scheduled == []
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, rid)
        assert run.current_state == "BLOCKED"
        assert run.status_message == routed["message"]
        assert "in_browser" not in (run.confidence_state or {})


def test_no_loop_blocks_without_fallback() -> None:
    uid = _make_user()
    rid, tid = _make_deferred_run(uid, {})
    _register_ready_body(f"dev-{uuid.uuid4().hex[:8]}")
    hub.loop = None  # no websocket ever connected this process
    routed = extension_run.route_deferred_chat_run(rid, tid)
    assert routed["status"] == "no_body"
    with SessionLocal() as db:
        assert db.get(models.OperatorRun, rid).current_state == "BLOCKED"


def test_busy_body_parks_second_run_and_does_not_schedule(monkeypatch) -> None:
    uid = _make_user()
    dev = f"dev-{uuid.uuid4().hex[:8]}"
    # an application already in flight on this body (mid-form, waiting on the user)
    active_rid, _ = _make_deferred_run(
        uid,
        {"in_browser": {"url": "https://ats.rippling.com/x/apply", "device_id": dev}},
        url="https://ats.rippling.com/x/apply",
        state="WAITING_FOR_USER",
    )
    # a second application the user tries to start while the first is still going
    new_rid, new_tid = _make_deferred_run(uid, {"real_site_execution_requested": True})
    _register_ready_body(dev)
    hub.loop = object()
    scheduled = _capture_schedule(monkeypatch)

    routed = extension_run.route_deferred_chat_run(new_rid, new_tid)
    assert routed["status"] == "busy"
    assert routed["active"]["id"] == active_rid
    assert f"run #{active_rid}" in routed["message"]
    assert scheduled == []  # the body is NOT asked to drive a second application

    with SessionLocal() as db:
        new_run = db.get(models.OperatorRun, new_rid)
        assert new_run.current_state == "CANCELLED"  # parked, not started
        # the first run is untouched and still in flight
        assert db.get(models.OperatorRun, active_rid).current_state == "WAITING_FOR_USER"
        # the user was told (in this run's thread) which application holds the body
        msgs = db.scalars(select(models.ChatMessage).where(models.ChatMessage.thread_id == new_tid)).all()
        assert any("in_browser_busy" in str(m.payload_json or "") for m in msgs)


def test_blocked_final_action_reserves_body_only_while_approved_or_executing(monkeypatch) -> None:
    uid = _make_user()
    dev = f"dev-{uuid.uuid4().hex[:8]}"
    reserved_rid, _ = _make_deferred_run(
        uid,
        {
            "in_browser": {"url": "https://ats.example.com/reserved/apply", "device_id": dev},
            "pending_final_action": {"status": "approved", "selector": "#submit", "label": "Submit application"},
        },
        url="https://ats.example.com/reserved/apply",
        state="BLOCKED",
    )
    new_rid, new_tid = _make_deferred_run(uid, {"real_site_execution_requested": True})
    _register_ready_body(dev)
    hub.loop = object()
    scheduled = _capture_schedule(monkeypatch)

    owner = extension_run.in_browser_body_busy_with(dev)
    assert owner and owner["id"] == reserved_rid
    assert owner["reserved_final_action"] is True
    routed = extension_run.route_deferred_chat_run(new_rid, new_tid)
    assert routed["status"] == "busy"
    assert routed["active"]["id"] == reserved_rid
    assert scheduled == []

    with SessionLocal() as db:
        reserved = db.get(models.OperatorRun, reserved_rid)
        state = dict(reserved.confidence_state or {})
        state["pending_final_action"] = {"status": "executing", "selector": "#submit"}
        reserved.confidence_state = state
        db.commit()
    owner = extension_run.in_browser_body_busy_with(dev)
    assert owner and owner["id"] == reserved_rid
    assert owner["reserved_final_action"] is True

    with SessionLocal() as db:
        reserved = db.get(models.OperatorRun, reserved_rid)
        state = dict(reserved.confidence_state or {})
        state["pending_final_action"] = {"status": "unverified", "selector": "#submit"}
        reserved.confidence_state = state
        db.commit()
    assert extension_run.in_browser_body_busy_with(dev) is None

    with SessionLocal() as db:
        reserved = db.get(models.OperatorRun, reserved_rid)
        state = dict(reserved.confidence_state or {})
        state["pending_final_action"] = {"status": "failed"}
        reserved.confidence_state = state
        db.commit()
    assert extension_run.in_browser_body_busy_with(dev) is None


def test_resume_schedule_fails_closed_when_another_run_has_approved_final_action() -> None:
    uid = _make_user()
    dev = f"dev-{uuid.uuid4().hex[:8]}"
    waiting_rid, _ = _make_deferred_run(
        uid,
        {"in_browser": {"url": "https://ats.example.com/old/apply", "device_id": dev}},
        url="https://ats.example.com/old/apply",
        state="WAITING_FOR_USER",
    )
    _make_deferred_run(
        uid,
        {
            "in_browser": {"url": "https://ats.example.com/reserved/apply", "device_id": dev},
            "pending_final_action": {"status": "approved", "selector": "#submit"},
        },
        url="https://ats.example.com/reserved/apply",
        state="BLOCKED",
    )
    _register_ready_body(dev)
    hub.loop = object()

    assert extension_run.schedule_resume_if_in_browser(waiting_rid) is False


def _rebind_with_connected_device(monkeypatch, rid: int, tid: int, device_id: str) -> dict | None:
    """The rebind the resume path would make for run `rid` when its thread's body is `device_id`."""
    from types import SimpleNamespace

    fake = SimpleNamespace(device_id=device_id, live_actions_ready=True)
    monkeypatch.setattr(hub, "get_for_thread", lambda thread_id: fake)
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, rid)
        state = dict(run.confidence_state or {})
        return extension_run._in_browser_binding_for_resume(run, state, tid, db=db)


def _record_device(rid: int, device_id: str) -> None:
    from app.event_store import emit_event

    with SessionLocal() as db:
        emit_event(db, event_type="IN_BROWSER_RESUME_SCHEDULED", payload={"device_id": device_id}, run_id=rid)
        db.commit()


def test_resume_refuses_a_rebind_to_another_device_than_the_run_recorded(monkeypatch) -> None:
    uid = _make_user()
    rid, tid = _make_deferred_run(uid, {"browser_task": {"execution_surface": "browser"}}, state="WAITING_FOR_USER")
    _record_device(rid, "dev-recorded")

    assert _rebind_with_connected_device(monkeypatch, rid, tid, "dev-other") is None


def test_resume_rebinds_to_the_device_the_run_recorded(monkeypatch) -> None:
    uid = _make_user()
    rid, tid = _make_deferred_run(uid, {"browser_task": {"execution_surface": "browser"}}, state="WAITING_FOR_USER")
    _record_device(rid, "dev-recorded")

    binding = _rebind_with_connected_device(monkeypatch, rid, tid, "dev-recorded")
    assert binding is not None and binding["device_id"] == "dev-recorded"


def test_resume_refuses_an_open_final_action_rebind_when_no_device_was_recorded(monkeypatch) -> None:
    uid = _make_user()
    rid, tid = _make_deferred_run(
        uid,
        {"browser_task": {"execution_surface": "browser"}, "pending_final_action": {"status": "awaiting_submit_approval"}},
        state="WAITING_FOR_USER",
    )

    assert _rebind_with_connected_device(monkeypatch, rid, tid, "dev-any") is None


def test_resume_still_rebinds_a_run_with_no_recorded_device_and_no_open_final_action(monkeypatch) -> None:
    uid = _make_user()
    rid, tid = _make_deferred_run(
        uid,
        {"browser_task": {"execution_surface": "browser"}, "pending_final_action": {"status": "declined"}},
        state="WAITING_FOR_USER",
    )

    binding = _rebind_with_connected_device(monkeypatch, rid, tid, "dev-any")
    assert binding is not None and binding["device_id"] == "dev-any"


def test_in_browser_body_busy_with_reports_active_run() -> None:
    uid = _make_user()
    dev = f"dev-{uuid.uuid4().hex[:8]}"
    active_rid, _ = _make_deferred_run(
        uid,
        {"in_browser": {"url": "https://ats.rippling.com/x/apply", "device_id": dev}},
        url="https://ats.rippling.com/x/apply",
        state="RUNNING",
    )
    busy = extension_run.in_browser_body_busy_with(dev)
    assert busy and busy["id"] == active_rid
    # a different device is free
    assert extension_run.in_browser_body_busy_with(f"dev-{uuid.uuid4().hex[:8]}") is None
    # READY_FOR_REVIEW (Lumen done) frees the body
    with SessionLocal() as db:
        db.get(models.OperatorRun, active_rid).current_state = "READY_FOR_REVIEW"
        db.commit()
    assert extension_run.in_browser_body_busy_with(dev) is None


def test_end_active_in_browser_run_frees_the_body() -> None:
    uid = _make_user()
    dev = f"dev-{uuid.uuid4().hex[:8]}"
    rid, _ = _make_deferred_run(
        uid,
        {"in_browser": {"url": "https://ats.rippling.com/x/apply", "device_id": dev}},
        url="https://ats.rippling.com/x/apply",
        state="WAITING_FOR_USER",
    )
    assert extension_run.in_browser_body_busy_with(dev) is not None  # busy before
    assert extension_run.end_active_in_browser_run(rid) is True
    assert extension_run.in_browser_body_busy_with(dev) is None  # freed after
    with SessionLocal() as db:
        assert db.get(models.OperatorRun, rid).current_state == "CANCELLED"


def test_cancelled_run_keeps_device_reserved_while_final_action_is_executing() -> None:
    uid = _make_user()
    dev = f"dev-{uuid.uuid4().hex[:8]}"
    rid, _ = _make_deferred_run(
        uid,
        {
            "in_browser": {"url": "https://ats.example.com/apply", "device_id": dev},
            "pending_final_action": {"status": "executing", "selector": "#submit"},
        },
        url="https://ats.example.com/apply",
        state="CANCELLED",
    )

    busy = extension_run.in_browser_body_busy_with(dev)
    assert busy and busy["id"] == rid
    assert busy["reserved_final_action"] is True

    with SessionLocal() as db:
        run = db.get(models.OperatorRun, rid)
        state = dict(run.confidence_state or {})
        state["pending_final_action"] = {"status": "unverified", "selector": "#submit"}
        run.confidence_state = state
        db.commit()
    assert extension_run.in_browser_body_busy_with(dev) is None


def test_launch_pending_never_falls_back_to_background_worker(monkeypatch) -> None:
    import app.main as main

    calls: list[str] = []

    for status in ("in_browser", "busy"):
        monkeypatch.setattr(extension_run, "route_deferred_chat_run", lambda r, t, _s=status: calls.append(_s) or {"status": _s, "message": "m", "active": None})
        main._launch_pending_chat_application({"run_id": 1, "thread_id": 2})
    assert calls == ["in_browser", "busy"]

    monkeypatch.setattr(extension_run, "route_deferred_chat_run", lambda r, t: calls.append("no_body") or {"status": "no_body", "message": "m", "active": None})
    main._launch_pending_chat_application({"run_id": 1, "thread_id": 2})
    assert calls == ["in_browser", "busy", "no_body"]
