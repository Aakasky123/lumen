"""The chat router and its fixed lines say the host OS's desktop, and a goal that names a non-Chrome browser
app (Safari) starts on the desktop on a Mac, since the browser surface is the Chrome extension."""

from __future__ import annotations

from typing import Any

import pytest

from app import desktop_body, desktop_body_macos, desktop_platform, extension_run
from app.lumen_model import LumenModelClient
from app.cognition import OperatorChatService
from app.db import SessionLocal, init_db
from app.runtime import OperatorRuntimeEngine

from tests.test_generic_browser_task_ingress import _scope, _stream_turn


def _fresh_user(db: Any) -> Any:
    """Each routing test chats as its own user: another test in the same worker can leave a sensitive-entry hand-off
    on the shared default user, and chat is locked for that user until it ends."""
    from uuid import uuid4

    from app import models

    user = models.User(email=f"router-{uuid4().hex[:10]}@example.test", name="Router User")
    db.add(user)
    db.flush()
    return user


def _pin(monkeypatch: pytest.MonkeyPatch, module: Any) -> None:
    monkeypatch.setattr(desktop_platform, "_PINNED", module)


_REAL_OS_TEXT = desktop_platform.os_text


class _Captured(Exception):
    pass


def _router_instruction(monkeypatch: pytest.MonkeyPatch) -> str:
    """The chat-scope router's instruction text, captured before any model call is made."""
    captured: dict[str, str] = {}
    original = _REAL_OS_TEXT

    def spy(text: str) -> str:
        out = original(text)
        if "execution_surface=desktop only when" in out:
            captured["instruction"] = out
            raise _Captured
        return out

    monkeypatch.setattr(desktop_platform, "os_text", spy)
    monkeypatch.setattr(LumenModelClient, "live_enabled", property(lambda self: True))
    monkeypatch.setattr(LumenModelClient, "_fallback_operator_chat_scope", lambda self, payload: {})
    monkeypatch.setattr(LumenModelClient, "_operator_chat_scope_fast_path_reason", lambda *a, **k: None)
    client = LumenModelClient.__new__(LumenModelClient)
    with pytest.raises(_Captured):
        client.classify_operator_chat_scope({"message": "open a file"})
    return captured["instruction"]


def test_router_prompt_names_the_host_os(monkeypatch: pytest.MonkeyPatch) -> None:
    _pin(monkeypatch, desktop_body_macos)
    mac = _router_instruction(monkeypatch)
    assert "native Mac applications or Finder" in mac and "Safari" in mac
    assert "Windows" not in mac and "Explorer" not in mac and "Settings" not in mac
    _pin(monkeypatch, desktop_body)
    win = _router_instruction(monkeypatch)
    assert "native Windows applications, Settings, or Explorer with no starting web page" in win
    assert "Safari" not in win and "Mac" not in win


def test_fixed_lines_say_mac_on_mac_and_stay_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = (
        "A Windows desktop task must not include a browser starting URL.",
        "Computer task: Windows",
        "work in a Windows app - and I'm on it.",
        "Waiting briefly for the Windows interface to settle.",
    )
    _pin(monkeypatch, desktop_body_macos)
    for line in lines:
        assert "Windows" not in desktop_platform.os_text(line)
    _pin(monkeypatch, desktop_body)
    for line in lines:
        assert desktop_platform.os_text(line) == line


_BROWSER_CASES = [
    ("In Safari open http://127.0.0.1:9/x and read the code", "Safari"),
    ("In Safari (the Safari app, not another browser) open http://x.test", "Safari"),
    ("open example.com in Safari", "Safari"),
    ("using Firefox, go to example.com", "Firefox"),
    ("open it using Firefox", "Firefox"),
    ("Launch Safari and search for flights", "Safari"),
    ("the Brave browser please", "Brave"),
    ("do it in the Safari app", "Safari"),
    ("book a safari tour in Kenya", None),
    ("open Brave New World on Goodreads", None),
    ("open Vivaldi's Four Seasons on YouTube", None),
    ("buy tickets from the Opera website", None),
    ("go on safari in Kenya, find lodges", None),
    ("don't use Safari, do it in Chrome", None),
    ("not in Safari, please", None),
    ("use Chrome instead of Safari", None),
    ("open example.com in Chrome rather than Safari", None),
    ("open it in Chrome, not Safari browser", None),
    ("in Chrome open example.com, then launch Safari and compare", None),
    ("go to https://example.com in Chrome", None),
]


@pytest.mark.parametrize("text,expected", _BROWSER_CASES)
def test_named_other_browser(monkeypatch: pytest.MonkeyPatch, text: str, expected: str | None) -> None:
    _pin(monkeypatch, desktop_body_macos)
    assert desktop_platform.named_other_browser(text) == expected


def test_named_other_browser_is_off_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    _pin(monkeypatch, desktop_body)
    assert desktop_platform.named_other_browser("in Safari open http://x.test") is None


def test_safari_goal_stays_on_browser_when_desktop_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    init_db()
    _pin(monkeypatch, desktop_body_macos)
    monkeypatch.setattr(desktop_platform, "desktop_surface_usable", lambda: False)
    content = "In Safari open http://127.0.0.1:9/x and read the visit code"
    monkeypatch.setattr(
        OperatorChatService,
        "_classify_chat_scope",
        lambda *args, **kwargs: _scope(
            decision="start", execution_surface="browser", goal=content, target_url="http://127.0.0.1:9/x"
        ),
    )
    monkeypatch.setattr(
        extension_run,
        "route_deferred_chat_run",
        lambda run_id, thread_id, *, db=None: {"status": "in_desktop", "message": None, "active": None},
    )
    with SessionLocal() as db:
        user = _fresh_user(db)
        service = OperatorChatService(db)
        thread = service.create_thread(user_id=user.id)
        meta, _done = _stream_turn(service, thread_id=thread.id, content=content)
        assert meta["task_request"]["execution_surface"] == "browser"


@pytest.mark.parametrize(
    "module,surface,url",
    [(desktop_body_macos, "desktop", "desktop://windows"), (desktop_body, "browser", "http://127.0.0.1:9/x")],
)
def test_safari_named_goal_routes_by_platform(
    monkeypatch: pytest.MonkeyPatch, module: Any, surface: str, url: str
) -> None:
    init_db()
    _pin(monkeypatch, module)
    monkeypatch.setattr(desktop_platform, "desktop_surface_usable", lambda: True)
    content = "In Safari open http://127.0.0.1:9/x and read the visit code"
    monkeypatch.setattr(
        OperatorChatService,
        "_classify_chat_scope",
        lambda *args, **kwargs: _scope(
            decision="start", execution_surface="browser", goal=content, target_url="http://127.0.0.1:9/x"
        ),
    )
    monkeypatch.setattr(
        extension_run,
        "route_deferred_chat_run",
        lambda run_id, thread_id, *, db=None: {"status": "in_desktop", "message": None, "active": None},
    )
    with SessionLocal() as db:
        user = _fresh_user(db)
        service = OperatorChatService(db)
        thread = service.create_thread(user_id=user.id)
        meta, _done = _stream_turn(service, thread_id=thread.id, content=content)
        request = meta["task_request"]
        assert request["execution_surface"] == surface
        assert request["target_url"] == url


def _route_chrome_internal_task(monkeypatch: pytest.MonkeyPatch, *, desktop_usable: bool) -> tuple[dict, str]:
    init_db()
    _pin(monkeypatch, desktop_body_macos)
    monkeypatch.setattr(desktop_platform, "desktop_surface_usable", lambda: desktop_usable)
    content = "Open Chrome, go to chrome://extensions, switch on Developer mode and load my unpacked extension"
    monkeypatch.setattr(
        OperatorChatService,
        "_classify_chat_scope",
        lambda *args, **kwargs: _scope(
            decision="start", execution_surface="browser", goal="Load the unpacked extension", target_url="chrome://extensions"
        ),
    )
    monkeypatch.setattr(
        extension_run,
        "route_deferred_chat_run",
        lambda run_id, thread_id, *, db=None: {"status": "in_desktop", "message": None, "active": None},
    )
    with SessionLocal() as db:
        user = _fresh_user(db)
        service = OperatorChatService(db)
        thread = service.create_thread(user_id=user.id)
        meta, done = _stream_turn(service, thread_id=thread.id, content=content)
        return meta, str(getattr(done.get("assistant_message"), "content", "") or "")


def test_chrome_internal_page_task_runs_on_the_desktop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The extension cannot open chrome:// pages, so a task that starts on one drives the Chrome app instead of
    asking which website to use."""
    meta, _text = _route_chrome_internal_task(monkeypatch, desktop_usable=True)
    request = meta["task_request"]
    assert request["execution_surface"] == "desktop"
    assert request["target_url"] == "desktop://windows"
    assert "chrome://extensions" in request["goal"]


def test_chrome_internal_page_task_says_why_when_the_desktop_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(desktop_platform, "desktop_control_status", lambda: {"available": True, "enabled": False})
    _meta, text = _route_chrome_internal_task(monkeypatch, desktop_usable=False)
    assert "Which website" not in text
    assert "chrome://extensions is one of the browser's own pages" in text
    assert "Turn computer use back on from the desktop status bar" in text


def test_router_forbids_inventing_a_stop_condition(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run 51: "Apply to this job for me" became "... Do not submit without explicit authorization", which stalled a
    Full-autonomy run in a verify-and-scroll loop. The router may not add stops the user never stated."""
    _pin(monkeypatch, desktop_body_macos)
    instruction = _router_instruction(monkeypatch)
    assert "never add a stop condition, approval requirement, or limit the user did not state" in instruction
    assert "browser's own pages (chrome://extensions" in instruction
