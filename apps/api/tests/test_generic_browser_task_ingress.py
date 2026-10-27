from __future__ import annotations

import asyncio
import base64
import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import func, select

from app import extension_run, models
from app.agent_settings import update_agent_settings
from app.lumen_model import LumenModelClient
from app.cognition import OperatorChatService
from app.db import SessionLocal, init_db
from app.extension_hub import hub
from app.runtime import OperatorRuntimeEngine


def _scope(
    *,
    decision: str,
    execution_surface: str = "browser",
    goal: str = "",
    target_url: str = "",
    clarification: str = "",
    candidate_data_access: str = "none",
    candidate_data_scopes: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "action": "start_browser_task",
        "primary_intent": "start_browser_task",
        "in_scope": True,
        "action_request": True,
        "run_related": False,
        "out_of_scope": False,
        "confidence": 0.97,
        "browser_task_request": {
            "execution_surface": execution_surface,
            "decision": decision,
            "goal": goal,
            "target_url": target_url,
            "clarification": clarification,
            "candidate_data_access": candidate_data_access,
            "candidate_data_scopes": candidate_data_scopes or [],
        },
    }


def _stream_turn(service: OperatorChatService, *, thread_id: int, content: str) -> tuple[dict[str, Any], dict[str, Any]]:
    frames: Iterator[tuple[str, Any]] = service.stream_message(
        thread_id=thread_id,
        content=content,
        payload={"source": "user_mode_chat"},
    )
    meta: dict[str, Any] = {}
    done: dict[str, Any] = {}
    for kind, data in frames:
        if kind == "meta":
            meta = data
        elif kind == "done":
            done = data
    return meta, done


def test_movie_task_starts_exact_generic_run_and_routes_once(monkeypatch: pytest.MonkeyPatch) -> None:
    init_db()
    goal = (
        "Choose the 7:30 PM showing of Example Movie and select two adjacent standard seats, "
        "then stop before payment or confirmation."
    )
    target_url = "https://cinema.example/showtimes"
    route_calls: list[tuple[int, int]] = []

    monkeypatch.setattr(
        OperatorChatService,
        "_classify_chat_scope",
        lambda *args, **kwargs: _scope(decision="start", goal=goal, target_url=target_url),
    )

    def fake_route(run_id: int, thread_id: int, *, db: Any = None) -> dict[str, Any]:
        route_calls.append((run_id, thread_id))
        return {"status": "in_browser", "message": None, "active": None}

    monkeypatch.setattr(extension_run, "route_deferred_chat_run", fake_route)

    with SessionLocal() as db:
        user = OperatorRuntimeEngine(db).ensure_default_user()
        service = OperatorChatService(db)
        thread = service.create_thread(user_id=user.id)
        application_count_before = db.scalar(select(func.count()).select_from(models.Application))
        meta, done = _stream_turn(
            service,
            thread_id=thread.id,
            content=f"Use {target_url} to pick the movie, showtime, and seats. Stop before payment.",
        )

        task_request = meta["task_request"]
        assert task_request["action"] == "browser_task_started"
        assert task_request["goal"] == goal
        assert task_request["target_url"] == target_url
        assert task_request["deferred_task_start"] is True
        assert meta["application_request"] is None
        assert done["chat_response"]["model"] == "start_browser_task"
        assert done["chat_response"]["in_browser"] is True

        run = db.get(models.OperatorRun, task_request["run_id"])
        assert run is not None
        assert run.application_id is None
        assert db.scalar(select(func.count()).select_from(models.Application)) == application_count_before
        assert run.goal == goal
        assert run.target_url == target_url
        assert task_request["candidate_data_access"] == "none"
        assert task_request["candidate_data_scopes"] == []
        assert run.confidence_state["browser_task"]["candidate_data_access"] == "none"
        assert run.confidence_state["browser_task"]["candidate_data_scopes"] == []
        assert run.confidence_state["browser_task"]["safety_policy"] == {
            "policy_version": 2,
            "final_action_mode": "approval",
            "consequential_actions": "human_approval_required",
            "payments": "human_approval_required",
            "external_commitments": "human_approval_required",
            "approval_granted": False,
            "standing_authority_granted": False,
            "authority_source": "user_agent_setting_at_run_creation",
        }
        assert route_calls == [(run.id, thread.id)]


def test_windows_task_starts_desktop_surface_without_url(monkeypatch: pytest.MonkeyPatch) -> None:
    init_db()
    goal = "Open Notepad, type the supplied sentence, and stop after the text is visibly present."
    route_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        OperatorChatService,
        "_classify_chat_scope",
        lambda *args, **kwargs: _scope(
            decision="start",
            execution_surface="desktop",
            goal=goal,
            target_url="",
        ),
    )

    def fake_route(run_id: int, thread_id: int, *, db: Any = None) -> dict[str, Any]:
        route_calls.append((run_id, thread_id))
        return {"status": "in_desktop", "message": None, "active": None}

    monkeypatch.setattr(extension_run, "route_deferred_chat_run", fake_route)

    with SessionLocal() as db:
        user = OperatorRuntimeEngine(db).ensure_default_user()
        service = OperatorChatService(db)
        thread = service.create_thread(user_id=user.id)
        meta, _done = _stream_turn(
            service,
            thread_id=thread.id,
            content="Open Notepad, type: Lumen desktop body verified, and stop when it is visible.",
        )

        request = meta["task_request"]
        run = db.get(models.OperatorRun, int(request["run_id"]))
        assert request["action"] == "browser_task_started"
        assert request["execution_surface"] == "desktop"
        assert request["target_url"] == "desktop://windows"
        assert run is not None
        assert run.target_url == "desktop://windows"
        assert run.confidence_state["browser_task"]["execution_surface"] == "desktop"
        assert route_calls == [(run.id, thread.id)]


@pytest.mark.parametrize("provider", ["model_unavailable", "fallback_after_model_error"])
def test_unavailable_scope_model_fails_closed_before_desktop_task_routing(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    init_db()
    route_calls: list[tuple[int, int]] = []
    unavailable_scope = _scope(
        decision="start",
        execution_surface="desktop",
        goal="Launch Calculator on Windows.",
    )
    if provider == "model_unavailable":
        unavailable_scope.update(
            action="none",
            primary_intent="out_of_scope",
            in_scope=False,
            action_request=False,
            out_of_scope=True,
        )
        unavailable_scope["browser_task_request"].update(
            execution_surface="browser",
            decision="none",
            goal="",
        )
    unavailable_scope["metadata"] = {
        "provider": provider,
        "error": "provider unavailable",
    }
    monkeypatch.setattr(
        OperatorChatService,
        "_classify_chat_scope",
        lambda *args, **kwargs: unavailable_scope,
    )
    monkeypatch.setattr(
        extension_run,
        "route_deferred_chat_run",
        lambda run_id, thread_id, *, db=None: route_calls.append((run_id, thread_id))
        or {"status": "in_desktop", "message": None, "active": None},
    )

    with SessionLocal() as db:
        user = OperatorRuntimeEngine(db).ensure_default_user()
        service = OperatorChatService(db)
        thread = service.create_thread(user_id=user.id)
        run_count_before = db.scalar(select(func.count()).select_from(models.OperatorRun))
        meta, done = _stream_turn(
            service,
            thread_id=thread.id,
            content="Launch Calculator on Windows.",
        )

        assert meta["task_request"] is None
        assert done["chat_response"]["model"] == "model_unavailable"
        assert "Configure and validate a model provider in Settings" in done["assistant_message"].content
        assert db.scalar(select(func.count()).select_from(models.OperatorRun)) == run_count_before
        assert route_calls == []


def test_current_turn_model_can_grant_bounded_candidate_lookup_for_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_db()
    goal = "Use my stored profile to fill the visible form, then stop before any final action."
    target_url = "https://forms.example/start"
    monkeypatch.setattr(
        OperatorChatService,
        "_classify_chat_scope",
        lambda *args, **kwargs: _scope(
            decision="start",
            goal=goal,
            target_url=target_url,
            candidate_data_access="on_demand",
            candidate_data_scopes=["profile"],
        ),
    )
    monkeypatch.setattr(
        extension_run,
        "route_deferred_chat_run",
        lambda run_id, thread_id, *, db=None: {"status": "in_browser", "message": None, "active": None},
    )

    with SessionLocal() as db:
        user = OperatorRuntimeEngine(db).ensure_default_user()
        service = OperatorChatService(db)
        thread = service.create_thread(user_id=user.id)
        meta, _done = _stream_turn(
            service,
            thread_id=thread.id,
            content=f"Use my stored profile to fill the form at {target_url}, then stop before any final action.",
        )

        run = db.get(models.OperatorRun, int(meta["task_request"]["run_id"]))
        assert run is not None
        assert meta["task_request"]["candidate_data_access"] == "on_demand"
        assert meta["task_request"]["candidate_data_scopes"] == ["profile"]
        assert run.confidence_state["browser_task"]["candidate_data_access"] == "on_demand"
        assert run.confidence_state["browser_task"]["candidate_data_scopes"] == ["profile"]


def test_scope_classifier_does_not_receive_candidate_inventory_before_capability_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_db()
    captured: dict[str, Any] = {}

    with SessionLocal() as db:
        user = OperatorRuntimeEngine(db).ensure_default_user()
        service = OperatorChatService(db)
        thread = service.create_thread(user_id=user.id)

        def classify(payload: dict[str, Any]) -> dict[str, Any]:
            captured.update(payload)
            return _scope(
                decision="start",
                goal="Use my stored profile on the visible form, then stop.",
                target_url="https://forms.example/start",
                candidate_data_access="on_demand",
                candidate_data_scopes=["profile"],
            )

        monkeypatch.setattr(service.lumen_model, "classify_operator_chat_scope", classify)
        service._classify_chat_scope(
            thread,
            "Use my stored profile at https://forms.example/start and then stop.",
            payload={"source": "user_mode_chat"},
            context={},
            pending_question=None,
        )

    classifier_context = captured["context"]
    assert "profile_summary" not in classifier_context
    assert "resume_summary" not in classifier_context


def test_candidate_capability_normalization_is_source_scoped_and_fails_closed_without_scopes() -> None:
    client = LumenModelClient()
    base = {
        "in_scope": True,
        "action_request": True,
        "run_related": False,
        "profile_related": True,
        "resume_related": False,
        "approval_related": False,
        "out_of_scope": False,
        "primary_intent": "start_browser_task",
        "action": "start_browser_task",
        "confidence": 0.9,
        "rationale": "explicit current-turn request",
        "safe_to_answer": True,
        "expose_diagnostics": False,
    }
    scoped = client._normalize_operator_chat_scope(
        {
            **base,
            "browser_task_request": {
                "execution_surface": "browser",
                "decision": "start",
                "goal": "Use only my profile.",
                "target_url": "https://forms.example/start",
                "clarification": "",
                "candidate_data_access": "on_demand",
                "candidate_data_scopes": ["profile", "profile", "unsupported"],
            },
        }
    )
    assert scoped["browser_task_request"]["candidate_data_access"] == "on_demand"
    assert scoped["browser_task_request"]["candidate_data_scopes"] == ["profile"]

    unscoped = client._normalize_operator_chat_scope(
        {
            **base,
            "browser_task_request": {
                "execution_surface": "browser",
                "decision": "start",
                "goal": "Use my data.",
                "target_url": "https://forms.example/start",
                "clarification": "",
                "candidate_data_access": "on_demand",
                "candidate_data_scopes": [],
            },
        }
    )
    assert unscoped["browser_task_request"]["candidate_data_access"] == "none"
    assert unscoped["browser_task_request"]["candidate_data_scopes"] == []


def test_explicit_task_action_overrides_contradictory_out_of_scope_flag() -> None:
    # Observed live ("book me a movie ticket for new spider man movie"): the model
    # returned a perfect start_browser_task + decision=ask_user clarification AND
    # out_of_scope=true because it conflated "no URL yet" with "not my job". The flag
    # would gate the task handler off and ship a canned lane reply. The action is the
    # judgment; the boolean is envelope drift, reconciled deterministically.
    client = LumenModelClient()
    scoped = client._normalize_operator_chat_scope(
        {
            "in_scope": False,
            "action_request": True,
            "run_related": False,
            "profile_related": False,
            "resume_related": False,
            "approval_related": False,
            "out_of_scope": True,
            "primary_intent": "out_of_scope",
            "action": "start_browser_task",
            "confidence": 0.99,
            "rationale": "booking request without a starting URL",
            "safe_to_answer": False,
            "expose_diagnostics": False,
            "browser_task_request": {
                "execution_surface": "browser",
                "decision": "ask_user",
                "goal": "Book a ticket for the new Spider-Man movie.",
                "target_url": "",
                "clarification": "Which movie-ticket website or cinema should I use?",
                "candidate_data_access": "none",
                "candidate_data_scopes": [],
            },
        }
    )
    assert scoped["out_of_scope"] is False
    assert scoped["in_scope"] is True
    assert scoped["action"] == "start_browser_task"
    assert scoped["browser_task_request"]["decision"] == "ask_user"
    assert scoped["browser_task_request"]["clarification"].startswith("Which movie-ticket")

    # A memory action carries the same guarantee: the fact must never be silently
    # dropped because the flag drifted.
    remembered = client._normalize_operator_chat_scope(
        {
            "in_scope": False,
            "action_request": True,
            "run_related": False,
            "profile_related": True,
            "resume_related": False,
            "approval_related": False,
            "out_of_scope": True,
            "primary_intent": "out_of_scope",
            "action": "remember_user_fact",
            "confidence": 0.9,
            "rationale": "volunteered fact",
            "safe_to_answer": False,
            "expose_diagnostics": False,
            "remember_request": {"key": "favorite_theater", "value": "AMC Newport Centre"},
        }
    )
    assert remembered["out_of_scope"] is False
    assert remembered["action"] == "remember_user_fact"

    # An informational URL mention (decision=none) is NOT a task claim: the model's
    # out_of_scope verdict stands.
    informational = client._normalize_operator_chat_scope(
        {
            "in_scope": False,
            "action_request": False,
            "run_related": False,
            "profile_related": False,
            "resume_related": False,
            "approval_related": False,
            "out_of_scope": True,
            "primary_intent": "out_of_scope",
            "action": "start_browser_task",
            "confidence": 0.8,
            "rationale": "URL mentioned informationally",
            "safe_to_answer": False,
            "expose_diagnostics": False,
            "browser_task_request": {
                "execution_surface": "browser",
                "decision": "none",
                "goal": "",
                "target_url": "",
                "clarification": "",
                "candidate_data_access": "none",
                "candidate_data_scopes": [],
            },
        }
    )
    assert informational["out_of_scope"] is True


def test_confirmation_turn_binds_navigation_to_url_previously_rendered_in_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Live flow this pins: Lumen proposes the site in its clarification ("should I use
    # https://...?"), the user affirms WITHOUT re-pasting the URL. The model judges the
    # affirmation (decision=start); the deterministic gate accepts only a URL identity
    # already rendered in this thread and binds navigation to that exact transcript
    # string, so a model-added query/fragment the user never saw is dropped.
    init_db()
    rendered_url = "https://cinema.example/showtimes"
    model_url = f"{rendered_url}?utm=model-added#checkout"
    scopes = iter(
        [
            _scope(
                decision="ask_user",
                goal="Book a ticket for the new Spider-Man movie.",
                clarification=f"Should I use {rendered_url} for this booking?",
            ),
            _scope(
                decision="start",
                goal="Book a ticket for the new Spider-Man movie in North Bergen, NJ.",
                target_url=model_url,
            ),
        ]
    )
    monkeypatch.setattr(
        OperatorChatService,
        "_classify_chat_scope",
        lambda *args, **kwargs: next(scopes),
    )
    route_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        extension_run,
        "route_deferred_chat_run",
        lambda run_id, thread_id, *, db=None: route_calls.append((run_id, thread_id))
        or {"status": "in_browser", "message": None, "active": None},
    )

    with SessionLocal() as db:
        user = OperatorRuntimeEngine(db).ensure_default_user()
        service = OperatorChatService(db)
        thread = service.create_thread(user_id=user.id)
        ask_meta, _ = _stream_turn(
            service,
            thread_id=thread.id,
            content="book me a movie ticket for the new spider man movie",
        )
        assert ask_meta["task_request"]["action"] == "browser_task_clarification_required"

        confirm_meta, _ = _stream_turn(
            service,
            thread_id=thread.id,
            content="yes use that one - north bergen, new jersey",
        )
        task_request = confirm_meta["task_request"]
        assert task_request["action"] == "browser_task_started"
        run = db.get(models.OperatorRun, task_request["run_id"])
        assert run is not None
        assert run.target_url == rendered_url
        assert run.target_url != model_url
        assert route_calls == [(run.id, thread.id)]


def test_named_service_resolves_to_canonical_url_and_starts_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # "use atom site" IS the authorization: the model binds the named service to its
    # canonical URL and the task starts in the same turn — no confirmation friction
    # (owner call 2026-08-19). The start acknowledgment names the site so the user
    # can redirect or stop.
    init_db()
    canonical_url = "https://www.atomtickets.com/"
    monkeypatch.setattr(
        OperatorChatService,
        "_classify_chat_scope",
        lambda *args, **kwargs: _scope(
            decision="start",
            goal="Book two tickets for the new Spider-Man movie near North Bergen, NJ.",
            target_url=canonical_url,
        ),
    )
    route_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        extension_run,
        "route_deferred_chat_run",
        lambda run_id, thread_id, *, db=None: route_calls.append((run_id, thread_id))
        or {"status": "in_browser", "message": None, "active": None},
    )

    with SessionLocal() as db:
        user = OperatorRuntimeEngine(db).ensure_default_user()
        service = OperatorChatService(db)
        thread = service.create_thread(user_id=user.id)
        meta, done = _stream_turn(
            service,
            thread_id=thread.id,
            content="book 2 spiderman tickets, north bergen nj, after 9pm today, use atom site",
        )
        task_request = meta["task_request"]
        assert task_request["action"] == "browser_task_started"
        run = db.get(models.OperatorRun, task_request["run_id"])
        assert run is not None
        assert run.target_url == canonical_url
        assert route_calls == [(run.id, thread.id)]
        # The acknowledgment names the site the run went to.
        assert "atomtickets.com" in str(done["assistant_message"].content or "")


def test_classifier_context_carries_pending_task_clarification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The follow-up turn is fragile when the model must re-infer the clarification
    # exchange from raw transcript text (observed live: a bare site-name answer was
    # re-judged as chatter). The harness hands the classifier the pending
    # clarification explicitly - better memory for the model, no code interpretation.
    init_db()
    normalizer = LumenModelClient()
    contexts: list[dict[str, Any]] = []
    scopes = iter(
        [
            _scope(
                decision="ask_user",
                goal="Book a ticket for the new Spider-Man movie.",
                clarification="Which movie-ticket website should I use?",
            ),
            _scope(decision="none"),
        ]
    )

    def classify(payload: dict[str, Any]) -> dict[str, Any]:
        contexts.append(dict(payload.get("context") or {}))
        return normalizer._normalize_operator_chat_scope(next(scopes))

    with SessionLocal() as db:
        user = OperatorRuntimeEngine(db).ensure_default_user()
        service = OperatorChatService(db)
        monkeypatch.setattr(service.lumen_model, "classify_operator_chat_scope", classify)
        thread = service.create_thread(user_id=user.id)
        _stream_turn(service, thread_id=thread.id, content="book me a movie ticket")
        _stream_turn(service, thread_id=thread.id, content="atom")

    assert contexts[0].get("pending_task_clarification") is None
    pending = contexts[1].get("pending_task_clarification")
    assert pending is not None
    assert pending["question"] == "Which movie-ticket website should I use?"
    assert pending["goal"].startswith("Book a ticket")
    assert pending["execution_surface"] == "browser"
    init_db()
    human_url = (
        "https://cinema.example/showtimes"
        "?date=2026-07-24&ref=a%2Fb&ref=seat-map#available-seats"
    )
    model_url = "https://cinema.example/showtimes?date=2026-07-25#checkout"
    route_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        OperatorChatService,
        "_classify_chat_scope",
        lambda *args, **kwargs: _scope(
            decision="start",
            goal="Choose a showtime and seats, then stop before confirmation.",
            target_url=model_url,
        ),
    )
    monkeypatch.setattr(
        extension_run,
        "route_deferred_chat_run",
        lambda run_id, thread_id, *, db=None: route_calls.append((run_id, thread_id))
        or {"status": "in_browser", "message": None, "active": None},
    )

    with SessionLocal() as db:
        user = OperatorRuntimeEngine(db).ensure_default_user()
        service = OperatorChatService(db)
        thread = service.create_thread(user_id=user.id)
        meta, _done = _stream_turn(
            service,
            thread_id=thread.id,
            content=f"Use {human_url} to select seats, then stop before confirmation.",
        )

        task_request = meta["task_request"]
        run = db.get(models.OperatorRun, task_request["run_id"])
        assert task_request["action"] == "browser_task_started"
        assert task_request["target_url"] == human_url
        assert run is not None
        assert run.target_url == human_url
        assert run.target_url != model_url
        assert route_calls == [(run.id, thread.id)]


def test_model_authorized_generic_chat_uses_production_route_drive_and_vision_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Prove the live chat handoff without replacing any orchestration layer.

    Only the two external boundaries are controlled: the scope/vision model outputs and the
    connected Chrome transport. The production chat service, deferred router, ExtensionBody,
    drive_in_browser_application, and run_vision_loop all execute unchanged.
    """

    init_db()
    goal = "Inspect the current cinema listings and stop with the page ready for review."
    target_url = "https://cinema.example/showtimes"
    device_id = f"dev-generic-route-{uuid.uuid4().hex[:8]}"
    release_body = threading.Event()
    body_calls: list[tuple[str, dict[str, Any]]] = []
    model_calls: list[dict[str, Any]] = []

    class _BoundaryConnection:
        def __init__(self) -> None:
            self.device_id = device_id

        async def call(
            self,
            method: str,
            params: dict[str, Any] | None = None,
            *,
            timeout: float = 30.0,
        ) -> dict[str, Any]:
            body_calls.append((method, dict(params or {})))
            if method == "navigate":
                while not release_body.is_set():
                    await asyncio.sleep(0.01)
                return {"ok": True, "url": target_url}
            if method == "screenshot":
                return {
                    "ok": True,
                    "image": base64.b64encode(b"generic-route-test-jpeg").decode(),
                    "width": 800,
                    "height": 600,
                    "url": target_url,
                    "title": "Example Cinema",
                    "scroll_y": 0,
                    "scroll_height": 600,
                    "page_evidence": {
                        "url": target_url,
                        "title": "Example Cinema",
                        "text": "Cinema listings are visible and no further action is needed.",
                        "controls": [],
                    },
                }
            raise AssertionError(f"Unexpected browser mutation in route integration test: {method}")

    monkeypatch.setattr(
        OperatorChatService,
        "_classify_chat_scope",
        lambda *args, **kwargs: _scope(
            decision="start",
            goal=goal,
            target_url=target_url,
        ),
    )

    def model_boundary(
        user_id: int,
        image: dict[str, Any],
        view: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        model_calls.append(
            {
                "user_id": user_id,
                "image": dict(image),
                "view": dict(view),
                "context": dict(context),
            }
        )
        return {
            "action": "done",
            "reason": "The requested inspection is complete and the page is ready for review.",
            "skipped_optional": [],
        }

    monkeypatch.setattr(extension_run, "_decide_vision_action", model_boundary)

    previous_connections = dict(hub._connections)
    previous_loop = hub.loop
    settings = extension_run.get_settings()
    previous_artifact_root = settings.artifact_root
    browser_loop = asyncio.new_event_loop()
    loop_started = threading.Event()

    def run_browser_loop() -> None:
        asyncio.set_event_loop(browser_loop)
        loop_started.set()
        browser_loop.run_forever()

    loop_thread = threading.Thread(target=run_browser_loop, name="generic-route-browser-loop", daemon=True)
    loop_thread.start()
    assert loop_started.wait(timeout=2)

    run_id = 0
    thread_id = 0
    try:
        settings.artifact_root = tmp_path / "artifacts"
        hub._connections.clear()
        hub._connections[device_id] = _BoundaryConnection()
        hub.loop = browser_loop

        with SessionLocal() as db:
            user = OperatorRuntimeEngine(db).ensure_default_user()
            service = OperatorChatService(db)
            thread = service.create_thread(user_id=user.id)
            thread_id = int(thread.id)
            meta, done = _stream_turn(
                service,
                thread_id=thread_id,
                content=f"Use {target_url} to inspect the cinema listings and stop for review.",
            )
            run_id = int(meta["task_request"]["run_id"])

            assert meta["task_request"]["action"] == "browser_task_started"
            assert done["chat_response"]["model"] == "start_browser_task"
            assert done["chat_response"]["in_browser"] is True

        # Let the production drive continue only after the synchronous chat transaction has
        # finished, matching the production post-response handoff and avoiding SQLite overlap.
        release_body.set()
        deadline = time.monotonic() + 15
        run_state = ""
        event_types: set[str] = set()
        outcome_messages: list[models.ChatMessage] = []
        while time.monotonic() < deadline:
            with SessionLocal() as db:
                run = db.get(models.OperatorRun, run_id)
                run_state = str(run.current_state or "") if run else ""
                event_types = set(
                    db.scalars(
                        select(models.CognitiveEvent.event_type).where(
                            models.CognitiveEvent.run_id == run_id,
                        )
                    ).all()
                )
                outcome_messages = [
                    message
                    for message in db.scalars(
                        select(models.ChatMessage).where(models.ChatMessage.thread_id == thread_id)
                    ).all()
                    if (message.payload_json or {}).get("source") == "in_browser_outcome"
                ]
            if run_state == "READY_FOR_REVIEW" and outcome_messages:
                break
            time.sleep(0.05)

        assert run_state == "READY_FOR_REVIEW"
        assert {
            "LUMEN_CHAT_BROWSER_TASK_STARTED",
            "IN_BROWSER_DRIVE_STARTED",
            "VISION_CONTEXT_POLICY_APPLIED",
            "VISION_AGENT_DECISION",
        }.issubset(event_types)
        assert [method for method, _params in body_calls].count("navigate") == 1
        assert [method for method, _params in body_calls].count("screenshot") >= 1
        assert set(method for method, _params in body_calls) <= {
            "navigate",
            "screenshot",
            "set_control_indicator",
            # Read-only: the completion check reads the run tab's whole text before accepting done.
            "read_run_tab_text",
        }
        assert [method for method, _params in body_calls].count(
            "set_control_indicator"
        ) >= 1
        assert len(model_calls) >= 2  # first done is deliberately rechecked by the final-action handoff audit
        assert model_calls[0]["view"]["url"] == target_url
        assert model_calls[0]["context"]["goal"] == goal
        assert "profile" not in model_calls[0]["context"]
        assert "resume_facts" not in model_calls[0]["context"]
        assert "saved_answers" not in model_calls[0]["context"]
        assert "available_documents" not in model_calls[0]["context"]
        assert "No consequential final action was executed" not in outcome_messages[-1].content
    finally:
        release_body.set()
        hub._connections.clear()
        hub._connections.update(previous_connections)
        hub.loop = previous_loop
        settings.artifact_root = previous_artifact_root
        browser_loop.call_soon_threadsafe(browser_loop.stop)
        loop_thread.join(timeout=5)
        assert not loop_thread.is_alive()
        browser_loop.close()


def test_generic_task_is_owned_by_chat_thread_user_not_default_user(monkeypatch: pytest.MonkeyPatch) -> None:
    init_db()
    target_url = "https://cinema.example/showtimes"
    monkeypatch.setattr(
        OperatorChatService,
        "_classify_chat_scope",
        lambda *args, **kwargs: _scope(
            decision="start",
            goal="Choose a showtime and seats, then stop before confirmation.",
            target_url=target_url,
        ),
    )
    monkeypatch.setattr(
        extension_run,
        "route_deferred_chat_run",
        lambda run_id, thread_id, *, db=None: {"status": "in_browser", "message": None, "active": None},
    )

    with SessionLocal() as db:
        runtime = OperatorRuntimeEngine(db)
        default_user = runtime.ensure_default_user()
        thread_user = models.User(
            email=f"generic-thread-owner-{uuid.uuid4().hex}@example.test",
            name="Generic Thread Owner",
        )
        db.add(thread_user)
        db.flush()
        service = OperatorChatService(db)
        thread = service.create_thread(user_id=thread_user.id)

        meta, _done = _stream_turn(
            service,
            thread_id=thread.id,
            content=f"Use {target_url} to select a showtime and seats, then stop before confirmation.",
        )
        run = db.get(models.OperatorRun, meta["task_request"]["run_id"])

        assert run is not None
        assert run.user_id == thread_user.id
        assert run.user_id != default_user.id
        assert thread.run_id == run.id


def test_model_selected_url_must_match_single_url_in_current_human_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_db()
    human_url = "https://cinema.example/showtimes"
    hallucinated_url = "https://different.example/checkout"
    route_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        OperatorChatService,
        "_classify_chat_scope",
        lambda *args, **kwargs: _scope(
            decision="start",
            goal="Choose a showtime and seats, then stop before confirmation.",
            target_url=hallucinated_url,
        ),
    )
    monkeypatch.setattr(
        extension_run,
        "route_deferred_chat_run",
        lambda run_id, thread_id, *, db=None: route_calls.append((run_id, thread_id)),
    )

    with SessionLocal() as db:
        user = OperatorRuntimeEngine(db).ensure_default_user()
        service = OperatorChatService(db)
        thread = service.create_thread(user_id=user.id)
        run_count_before = db.scalar(select(func.count()).select_from(models.OperatorRun))
        meta, done = _stream_turn(
            service,
            thread_id=thread.id,
            content=f"Use {human_url} to select a showtime and seats, then stop before confirmation.",
        )
        run_count_after = db.scalar(select(func.count()).select_from(models.OperatorRun))

        assert meta["task_request"]["action"] == "browser_task_clarification_required"
        assert "doesn't match the one I planned to act on" in done["assistant_message"].content
        assert run_count_after == run_count_before
        assert thread.run_id is None
        assert route_calls == []


def test_terminal_run_detaches_so_same_chat_can_start_next_generic_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_db()
    target_url = "https://cinema.example/showtimes"
    monkeypatch.setattr(
        OperatorChatService,
        "_classify_chat_scope",
        lambda *args, **kwargs: _scope(
            decision="start",
            goal="Choose a showtime and seats, then stop before confirmation.",
            target_url=target_url,
        ),
    )
    monkeypatch.setattr(
        extension_run,
        "route_deferred_chat_run",
        lambda run_id, thread_id, *, db=None: {"status": "in_browser", "message": None, "active": None},
    )

    with SessionLocal() as db:
        user = OperatorRuntimeEngine(db).ensure_default_user()
        previous = models.OperatorRun(
            user_id=user.id,
            goal="Previous task",
            target_url="https://previous.example/",
            current_state="CANCELLED",
            confidence_state={},
        )
        db.add(previous)
        db.flush()
        service = OperatorChatService(db)
        thread = service.create_thread(user_id=user.id, run_id=previous.id)

        meta, _done = _stream_turn(
            service,
            thread_id=thread.id,
            content=f"Use {target_url} to select a showtime and seats, then stop before confirmation.",
        )
        next_run = db.get(models.OperatorRun, meta["task_request"]["run_id"])

        assert meta["task_request"]["action"] == "browser_task_started"
        assert next_run.id != previous.id
        assert thread.run_id == next_run.id
        assert db.get(models.OperatorRun, previous.id).current_state == "CANCELLED"


def test_live_attached_run_requires_explicit_switch_before_new_generic_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_db()
    target_url = "https://cinema.example/showtimes"
    monkeypatch.setattr(
        OperatorChatService,
        "_classify_chat_scope",
        lambda *args, **kwargs: _scope(
            decision="start",
            goal="Choose a showtime and seats, then stop before confirmation.",
            target_url=target_url,
        ),
    )

    with SessionLocal() as db:
        user = OperatorRuntimeEngine(db).ensure_default_user()
        current = models.OperatorRun(
            user_id=user.id,
            goal="Current task",
            target_url="https://current.example/",
            current_state="RUNNING",
            confidence_state={},
        )
        db.add(current)
        db.flush()
        service = OperatorChatService(db)
        thread = service.create_thread(user_id=user.id, run_id=current.id)

        meta, _done = _stream_turn(
            service,
            thread_id=thread.id,
            content=f"Use {target_url} to select a showtime and seats, then stop before confirmation.",
        )

        assert meta["task_request"]["action"] == "new_browser_task_confirmation_required"
        assert meta["task_request"]["run_id"] == current.id
        assert thread.run_id == current.id


def test_confirmed_stop_and_replace_supersedes_the_attached_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Live dead-end this pins: the runtime asked "confirm you want to stop that run",
    # the user said yes, and nothing consumed the answer. The confirmation is now
    # durable clarification state: the next go-ahead cancels the attached run through
    # the real lifecycle and starts the new task in the same turn.
    init_db()
    target_url = "https://cinema.example/showtimes"
    scopes = iter(
        [
            _scope(decision="start", goal="Book two tickets.", target_url=target_url),
            _scope(decision="start", goal="Book two tickets.", target_url=target_url),
        ]
    )
    monkeypatch.setattr(
        OperatorChatService,
        "_classify_chat_scope",
        lambda *args, **kwargs: next(scopes),
    )
    route_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        extension_run,
        "route_deferred_chat_run",
        lambda run_id, thread_id, *, db=None: route_calls.append((run_id, thread_id))
        or {"status": "in_browser", "message": None, "active": None},
    )

    with SessionLocal() as db:
        user = OperatorRuntimeEngine(db).ensure_default_user()
        old_run = models.OperatorRun(
            user_id=user.id,
            goal="Old task still attached to this chat.",
            target_url="https://old.example/task",
            current_state="READY_FOR_REVIEW",
            confidence_state={},
        )
        db.add(old_run)
        db.flush()
        old_run_id = int(old_run.id)
        service = OperatorChatService(db)
        thread = service.create_thread(user_id=user.id, run_id=old_run_id)

        ask_meta, ask_done = _stream_turn(
            service,
            thread_id=thread.id,
            content=f"book me two tickets at {target_url} instead",
        )
        assert ask_meta["task_request"]["action"] == "new_browser_task_confirmation_required"
        assert "Say yes to stop" in str(ask_done["assistant_message"].content)

        confirm_meta, _ = _stream_turn(service, thread_id=thread.id, content="yes")
        task_request = confirm_meta["task_request"]
        assert task_request["action"] == "browser_task_started"
        assert int(task_request["run_id"]) != old_run_id
        assert db.get(models.OperatorRun, old_run_id).current_state == "CANCELLED"
        assert thread.run_id == int(task_request["run_id"])
        assert route_calls == [(int(task_request["run_id"]), thread.id)]


def test_url_only_informational_question_does_not_launch_browser_task(monkeypatch: pytest.MonkeyPatch) -> None:
    init_db()
    route_calls: list[tuple[int, int]] = []
    informational_scope = {
        "action": "none",
        "primary_intent": "domain_chat",
        "in_scope": True,
        "action_request": False,
        "run_related": False,
        "out_of_scope": False,
        "confidence": 0.96,
        "browser_task_request": {
            "decision": "none",
            "goal": "",
            "target_url": "",
            "clarification": "",
        },
    }
    monkeypatch.setattr(OperatorChatService, "_classify_chat_scope", lambda *args, **kwargs: informational_scope)
    monkeypatch.setattr(
        extension_run,
        "route_deferred_chat_run",
        lambda run_id, thread_id, *, db=None: route_calls.append((run_id, thread_id)),
    )

    with SessionLocal() as db:
        user = OperatorRuntimeEngine(db).ensure_default_user()
        service = OperatorChatService(db)
        thread = service.create_thread(user_id=user.id)
        run_count_before = db.scalar(select(func.count()).select_from(models.OperatorRun))
        meta, _done = _stream_turn(
            service,
            thread_id=thread.id,
            content="What does https://cinema.example/showtimes offer?",
        )
        run_count_after = db.scalar(select(func.count()).select_from(models.OperatorRun))

        assert meta["chat_scope"]["action"] == "none"
        assert thread.run_id is None
        assert run_count_after == run_count_before
        assert route_calls == []


def test_live_scope_model_overrides_url_verb_fallback_for_informational_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = LumenModelClient()
    captured_schema: dict[str, Any] = {}

    monkeypatch.setattr(LumenModelClient, "live_enabled", property(lambda _self: True))

    def fake_call_model_json(**kwargs: Any) -> dict[str, Any]:
        captured_schema.update(kwargs["schema"])
        return {
            "ok": True,
            "data": {
                "in_scope": True,
                "action_request": False,
                "run_related": False,
                "profile_related": False,
                "resume_related": False,
                "approval_related": False,
                "out_of_scope": False,
                "primary_intent": "domain_chat",
                "action": "none",
                "confidence": 0.99,
                "rationale": "The user asked for information, not browser execution.",
                "safe_to_answer": True,
                "expose_diagnostics": False,
                "browser_task_request": {
                    "execution_surface": "browser",
                    "decision": "none",
                    "goal": "",
                    "target_url": "",
                    "clarification": "",
                    "candidate_data_access": "none",
                    "candidate_data_scopes": [],
                },
            },
            "metadata": {"provider": "test", "model": "scope-test"},
        }

    monkeypatch.setattr(client, "_call_model_json", fake_call_model_json)
    scope = client.classify_operator_chat_scope(
        {
            "message": "What does the complete checkout flow at https://cinema.example/checkout do?",
            "context": {"view_mode": "user"},
        }
    )

    assert scope["action"] == "none"
    assert scope["browser_task_request"]["decision"] == "none"
    assert scope["browser_task_request"]["candidate_data_access"] == "none"
    assert scope["browser_task_request"]["candidate_data_scopes"] == []
    properties = captured_schema["schema"]["properties"]
    assert properties["browser_task_request"]["properties"]["target_url"]["type"] == "string"
    assert properties["browser_task_request"]["properties"]["candidate_data_access"]["enum"] == ["none", "on_demand"]
    assert properties["browser_task_request"]["properties"]["candidate_data_scopes"]["items"]["enum"] == [
        "profile",
        "resume",
        "memory",
    ]


def test_scope_prompt_uses_task_semantics_instead_of_magic_candidate_context_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = LumenModelClient()
    model_calls: list[dict[str, Any]] = []
    target_url = "https://jobs.example/software-engineer"

    monkeypatch.setattr(LumenModelClient, "live_enabled", property(lambda _self: True))

    def fake_call_model_json(**kwargs: Any) -> dict[str, Any]:
        model_calls.append(kwargs)
        return {
            "ok": True,
            "data": {
                "in_scope": True,
                "action_request": True,
                "run_related": False,
                "profile_related": True,
                "resume_related": True,
                "approval_related": True,
                "out_of_scope": False,
                "primary_intent": "start_browser_task",
                "action": "start_browser_task",
                "confidence": 0.99,
                "rationale": "Completing the requested application needs bounded candidate context.",
                "safe_to_answer": False,
                "expose_diagnostics": False,
                "browser_task_request": {
                    "execution_surface": "browser",
                    "decision": "start",
                    "goal": f"Apply to the role at {target_url}.",
                    "target_url": target_url,
                    "clarification": "",
                    "candidate_data_access": "on_demand",
                    "candidate_data_scopes": ["profile", "resume", "memory"],
                },
                "remember_request": {"key": "", "value": ""},
            },
            "metadata": {"provider": "test", "model": "scope-test"},
        }

    monkeypatch.setattr(client, "_call_model_json", fake_call_model_json)
    scope = client.classify_operator_chat_scope(
        {
            "message": f"apply {target_url}",
            "context": {"view_mode": "user"},
        }
    )

    assert scope["browser_task_request"]["candidate_data_access"] == "on_demand"
    assert scope["browser_task_request"]["candidate_data_scopes"] == [
        "profile",
        "resume",
        "memory",
    ]
    instruction = model_calls[0]["prompt"]["instruction"]
    assert "without requiring magic wording" in instruction
    assert "says only 'apply' plus a URL" in instruction
    assert "Public browsing, research, navigation" in instruction


def test_live_scope_repairs_flattened_browser_task_contract_before_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for the exact malformed JSON returned by the live OpenRouter provider."""

    init_db()
    target_url = "https://job-boards.greenhouse.io/warp/jobs/4324888004"
    goal = (
        f"Apply for the job at {target_url} using saved profile, answer memory, and resume; "
        "ask user for any required ungrounded fact; skip ungrounded optional fields; stop "
        "before the final submission action for user review and explicit approval."
    )
    model_calls: list[dict[str, Any]] = []
    route_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(LumenModelClient, "live_enabled", property(lambda _self: True))

    malformed_live_response = {
        "decision": "start",
        "action": "start_browser_task",
        "browser_task_request": {
            "goal": goal,
            "target_url": target_url,
        },
        "candidate_data_access": "on_demand",
        "candidate_data_scopes": ["profile", "resume", "memory"],
        "clarification": None,
    }
    repaired_response = {
        "in_scope": True,
        "action_request": True,
        "run_related": False,
        "profile_related": True,
        "resume_related": True,
        "approval_related": True,
        "out_of_scope": False,
        "primary_intent": "start_browser_task",
        "action": "start_browser_task",
        "confidence": 0.99,
        "rationale": "The current message requests one explicit browser task.",
        "safe_to_answer": False,
        "expose_diagnostics": False,
        "browser_task_request": {
            "execution_surface": "browser",
            "decision": "start",
            "goal": goal,
            "target_url": target_url,
            "clarification": "",
            "candidate_data_access": "on_demand",
            "candidate_data_scopes": ["profile", "resume", "memory"],
        },
        "remember_request": {"key": "", "value": ""},
    }

    def fake_call_model_json(**kwargs: Any) -> dict[str, Any]:
        model_calls.append(kwargs)
        data = malformed_live_response if len(model_calls) == 1 else repaired_response
        return {
            "ok": True,
            "data": data,
            "metadata": {
                "provider": "openrouter",
                "provider_id": 4,
                "model": "deepseek/deepseek-v4-flash",
                "latency_ms": 7,
                "schema_requested": True,
                "schema_included": False,
            },
        }

    monkeypatch.setattr(LumenModelClient, "_call_model_json", lambda _self, **kwargs: fake_call_model_json(**kwargs))
    monkeypatch.setattr(
        extension_run,
        "route_deferred_chat_run",
        lambda run_id, thread_id, *, db=None: (
            route_calls.append((run_id, thread_id))
            or {"status": "in_browser", "message": None, "active": None}
        ),
    )

    with SessionLocal() as db:
        user = OperatorRuntimeEngine(db).ensure_default_user()
        service = OperatorChatService(db)
        thread = service.create_thread(user_id=user.id)
        message = (
            f"Start a new application run for {target_url}. Use my saved profile, answer memory, "
            "and resume; ask me for any required ungrounded fact; skip ungrounded optional fields; "
            "stop before the final submission action for my review and explicit approval."
        )
        meta, _done = _stream_turn(service, thread_id=thread.id, content=message)

        task_request = meta["task_request"]
        run = db.get(models.OperatorRun, int(task_request["run_id"]))
        assert task_request["action"] == "browser_task_started"
        assert task_request["goal"] == goal
        assert task_request["target_url"] == target_url
        assert task_request["candidate_data_access"] == "on_demand"
        assert task_request["candidate_data_scopes"] == ["profile", "resume", "memory"]
        assert run is not None
        assert run.goal == goal
        assert route_calls == [(run.id, thread.id)]

    assert [call["purpose"] for call in model_calls] == [
        "operator_chat_scope",
        "operator_chat_scope_protocol_repair",
    ]
    exact_contract = model_calls[0]["prompt"]["exact_response_contract"]
    assert exact_contract["browser_task_request_keys"] == [
        "execution_surface",
        "decision",
        "goal",
        "target_url",
        "clarification",
        "candidate_data_access",
        "candidate_data_scopes",
    ]
    assert model_calls[1]["prompt"]["previous_response"] == malformed_live_response
    assert "only inside browser_task_request" in model_calls[1]["prompt"]["protocol_repair"]


def test_live_scope_fails_closed_when_protocol_repair_is_still_malformed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = LumenModelClient()
    monkeypatch.setattr(LumenModelClient, "live_enabled", property(lambda _self: True))
    malformed = {
        "decision": "start",
        "action": "start_browser_task",
        "browser_task_request": {
            "execution_surface": "browser",
            "goal": "Select a showtime and seats, then stop before confirmation.",
            "target_url": "https://cinema.example/showtimes",
        },
    }
    monkeypatch.setattr(
        client,
        "_call_model_json",
        lambda **kwargs: {
            "ok": True,
            "data": malformed,
            "metadata": {"provider": "test", "model": "malformed", "latency_ms": 1},
        },
    )

    scope = client.classify_operator_chat_scope(
        {
            "message": (
                "Use https://cinema.example/showtimes to select a showtime and seats, "
                "then stop before confirmation."
            ),
            "context": {"view_mode": "user"},
        }
    )

    assert scope["action"] != "start_browser_task"
    assert scope["browser_task_request"]["decision"] == "none"
    assert scope["browser_task_request"]["candidate_data_access"] == "none"
    assert scope["browser_task_request"]["candidate_data_scopes"] == []
    assert scope["metadata"]["provider"] == "fallback_after_model_error"
    assert scope["metadata"]["error"] == "operator_chat_scope_protocol_invalid_after_repair"


def test_model_failure_never_turns_url_verb_fallback_into_browser_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = LumenModelClient()
    monkeypatch.setattr(LumenModelClient, "live_enabled", property(lambda _self: True))
    monkeypatch.setattr(
        client,
        "_call_model_json",
        lambda **kwargs: {"ok": False, "error": "provider unavailable", "metadata": {}},
    )

    scope = client.classify_operator_chat_scope(
        {
            "message": "Start booking at https://cinema.example/showtimes and stop before confirmation.",
            "context": {"view_mode": "user"},
        }
    )

    assert scope["action"] == "none"
    assert scope["action_request"] is False
    assert scope["metadata"]["provider"] == "fallback_after_model_error"


@pytest.mark.parametrize(
    ("decision", "goal", "target_url", "clarification"),
    [
        (
            "ask_user",
            "Select a movie showtime and seats, then stop before confirmation.",
            "",
            "Which cinema or ticketing URL should I start from?",
        ),
        (
            "ask_user",
            "Select a movie showtime and seats, then stop before confirmation.",
            "",
            "You provided two possible starting sites. Which exact URL should I use?",
        ),
    ],
)
def test_missing_or_ambiguous_target_asks_without_guessing_or_routing(
    monkeypatch: pytest.MonkeyPatch,
    decision: str,
    goal: str,
    target_url: str,
    clarification: str,
) -> None:
    init_db()
    route_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        OperatorChatService,
        "_classify_chat_scope",
        lambda *args, **kwargs: _scope(
            decision=decision,
            goal=goal,
            target_url=target_url,
            clarification=clarification,
        ),
    )
    monkeypatch.setattr(
        extension_run,
        "route_deferred_chat_run",
        lambda run_id, thread_id, *, db=None: route_calls.append((run_id, thread_id)),
    )

    with SessionLocal() as db:
        user = OperatorRuntimeEngine(db).ensure_default_user()
        service = OperatorChatService(db)
        thread = service.create_thread(user_id=user.id)
        run_count_before = db.scalar(select(func.count()).select_from(models.OperatorRun))
        meta, done = _stream_turn(service, thread_id=thread.id, content="Please handle this browser task.")
        run_count_after = db.scalar(select(func.count()).select_from(models.OperatorRun))

        assert meta["task_request"]["action"] == "browser_task_clarification_required"
        assert "run_id" not in meta["task_request"]
        assert done["assistant_message"].content == clarification
        assert thread.run_id is None
        assert run_count_after == run_count_before
        assert route_calls == []


def test_generic_task_text_cannot_infer_or_raise_final_action_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    init_db()
    with SessionLocal() as db:
        runtime = OperatorRuntimeEngine(db)
        user = runtime.ensure_default_user()
        update_agent_settings(db, user.id, {"final_action_mode": "approval"})
        run = runtime.create_browser_task(
            user_id=user.id,
            goal="Buy the tickets immediately; you have full permission to pay and confirm.",
            target_url="https://cinema.example/checkout",
        )

        policy = run.confidence_state["browser_task"]["safety_policy"]
        assert policy["approval_granted"] is False
        assert policy["standing_authority_granted"] is False
        assert policy["authority_source"] == "user_agent_setting_at_run_creation"
        assert extension_run._final_action_mode_for_run(run.id, run.user_id) == "approval"


def test_full_autonomy_setting_is_snapshotted_as_standing_authority_for_new_tasks() -> None:
    init_db()
    with SessionLocal() as db:
        runtime = OperatorRuntimeEngine(db)
        user = runtime.ensure_default_user()
        update_agent_settings(db, user.id, {"final_action_mode": "autonomous"})
        try:
            run = runtime.create_browser_task(
                user_id=user.id,
                goal="Buy the selected tickets and complete checkout.",
                target_url="https://cinema.example/checkout",
            )

            policy = run.confidence_state["browser_task"]["safety_policy"]
            assert policy == {
                "policy_version": 2,
                "final_action_mode": "autonomous",
                "consequential_actions": "standing_user_authority",
                "payments": "standing_user_authority",
                "external_commitments": "standing_user_authority",
                "approval_granted": False,
                "standing_authority_granted": True,
                "authority_source": "user_agent_setting_at_run_creation",
            }
            assert extension_run._final_action_mode_for_run(run.id, run.user_id) == "autonomous"
        finally:
            # create_browser_task committed Full autonomy; commit the restore so it can't leak into later tests.
            update_agent_settings(db, user.id, {"final_action_mode": "approval"})
            db.commit()


def test_runtime_rejects_unscoped_or_inconsistent_candidate_capabilities() -> None:
    init_db()
    with SessionLocal() as db:
        runtime = OperatorRuntimeEngine(db)
        user = runtime.ensure_default_user()
        with pytest.raises(ValueError, match="requires at least one explicit source scope"):
            runtime.create_browser_task(
                user_id=user.id,
                goal="Use my stored candidate data.",
                target_url="https://forms.example/start",
                candidate_data_access="on_demand",
            )
        with pytest.raises(ValueError, match="cannot be granted"):
            runtime.create_browser_task(
                user_id=user.id,
                goal="Inspect the page.",
                target_url="https://forms.example/start",
                candidate_data_access="none",
                candidate_data_scopes=["profile"],
            )
