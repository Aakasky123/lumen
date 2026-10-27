"""Async background-run narration for the chat-driven application.

The live run is launched as a post-response BackgroundTask by the streaming chat
endpoint (app.main._launch_pending_chat_application), never mid-stream — the
mid-stream thread previously deadlocked the in-process SQLite write lock and hung
the server.
"""


from fastapi.testclient import TestClient

from app import models
from app import main
from app import extension_run
from app.config import get_settings
from app.cognition import OperatorChatService
from app.db import SessionLocal, init_db
from app.runtime import OperatorRuntimeEngine


def test_launch_pending_chat_application_routes_to_extension_body(monkeypatch) -> None:
    captured: dict[str, int] = {}

    def fake_route(run_id: int, thread_id: int) -> dict:
        captured["run_id"] = run_id
        captured["thread_id"] = thread_id
        return {"status": "in_browser", "message": None, "active": None}

    monkeypatch.setattr(extension_run, "route_deferred_chat_run", fake_route)
    main._launch_pending_chat_application({"run_id": 4242, "thread_id": 77})
    assert captured == {"run_id": 4242, "thread_id": 77}


def test_launch_pending_chat_application_noops_when_empty(monkeypatch) -> None:
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(extension_run, "route_deferred_chat_run", lambda r, t: calls.append((r, t)))
    main._launch_pending_chat_application({})
    assert calls == []


def test_deferred_browser_run_dispatch_survives_chat_finalize_failure(monkeypatch) -> None:
    init_db()
    settings = get_settings()
    previous_async = settings.chat_application_async
    settings.chat_application_async = True
    goal = "Inspect the example page and stop before any consequential final action."
    target_url = "https://example.com/browser-task"
    routed: list[tuple[int, int]] = []

    monkeypatch.setattr(
        OperatorChatService,
        "_classify_chat_scope",
        lambda *_args, **_kwargs: {
            "action": "start_browser_task",
            "primary_intent": "start_browser_task",
            "in_scope": True,
            "action_request": True,
            "run_related": False,
            "out_of_scope": False,
            "confidence": 0.97,
            "browser_task_request": {
                "decision": "start",
                "goal": goal,
                "target_url": target_url,
                "clarification": "",
                "candidate_data_access": "none",
                "candidate_data_scopes": [],
            },
        },
    )
    monkeypatch.setattr(
        OperatorChatService,
        "_finalize_chat_turn",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("forced post-creation transcript failure")
        ),
    )
    monkeypatch.setattr(
        extension_run,
        "route_deferred_chat_run",
        lambda run_id, thread_id: routed.append((run_id, thread_id))
        or {"status": "in_browser", "message": None, "active": None},
    )

    try:
        with SessionLocal() as db:
            user = OperatorRuntimeEngine(db).ensure_default_user()
            thread = OperatorChatService(db).create_thread(user_id=user.id)
            db.commit()
            thread_id = int(thread.id)

        with TestClient(main.app) as client:
            response = client.post(
                f"/operator/chat/threads/{thread_id}/messages",
                json={
                    "content": f"Start at {target_url} and inspect it.",
                    "payload": {"source": "user_mode_chat", "view_mode": "user"},
                },
            )

        assert response.status_code == 200
        assert "event: error" in response.text
        assert len(routed) == 1
        run_id, routed_thread_id = routed[0]
        assert routed_thread_id == thread_id
        with SessionLocal() as db:
            run = db.get(models.OperatorRun, run_id)
            assert run is not None
            assert run.current_state == "CREATED"
            assert run.target_url == target_url
    finally:
        settings.chat_application_async = previous_async
