"""read_clipboard: the brain reads only text this run's own actions copied, never the user's clipboard."""

from __future__ import annotations

import asyncio

from app import desktop_body, extension_run, vision_agent
from app.agent_eval.osworld.body import OSWorldBody
from app.db import init_db
from app.extension_run import run_vision_loop
from tests.test_extension_run import _DummyContextEngine, _VisionBody


# --- the action contract -------------------------------------------------------------------------


def test_read_clipboard_is_a_parameterless_action_that_may_follow_a_copy_in_then() -> None:
    schema = vision_agent.action_schema()["schema"]
    assert "read_clipboard" in schema["properties"]["action"]["enum"]
    assert "read_clipboard" in schema["properties"]["then"]["items"]["properties"]["action"]["enum"]
    action = vision_agent.normalize_action({"action": "read_clipboard", "reason": "Read what I copied."})
    assert action["action"] == "read_clipboard"
    assert not action.get("invalid_reason")
    assert vision_agent.normalize_action({"action": "get_clipboard", "reason": "x"})["action"] == "read_clipboard"


def test_the_desktop_prompt_teaches_copy_then_read_and_shows_the_text_once() -> None:
    context = {
        "goal": "Copy the order number into the form.",
        "execution_surface": "desktop",
        "clipboard_text": {"text": "ORD-77812", "chars": 9, "truncated": False, "note": "n"},
    }
    prompt = vision_agent.build_prompt({"width": 800, "height": 600}, context)
    assert "read_clipboard" in prompt["instruction"]
    assert prompt["clipboard_text"]["text"] == "ORD-77812"
    assert "clipboard_text" not in vision_agent.build_prompt({"width": 800, "height": 600}, {"goal": "g"})


# --- the loop ------------------------------------------------------------------------------------


class _CopyingDesktopBody:
    """The real screen with a value to copy; read_clipboard returns what the fixture copied."""

    execution_surface = "desktop"
    device_id = "windows-desktop"

    def __init__(self, text: str) -> None:
        self.text = text
        self.keys: list[str] = []
        self.reads = 0

    async def capture_screenshot(self) -> dict:
        return {
            "ok": True, "image": "jpeg", "width": 1600, "height": 900, "url": "desktop://notepad", "title": "Notepad",
            "scroll_y": 0, "scroll_height": 900,
            "page_evidence": {"viewport_text": "Order ORD-77812 shipped", "controls": []},
        }

    def tool_context(self) -> dict:
        return {"windows": [], "applications": []}

    async def vision_key(self, key: str, **_kwargs: object) -> dict:
        self.keys.append(key)
        # Like the Windows body: a copy draws nothing, the clipboard changing is its observed effect.
        return {"ok": True, "action": "key", "surface_changed": False, "presses": 1,
                **({"clipboard_changed": True} if key.upper() == "CTRL+C" else {})}

    async def vision_read_clipboard(self) -> dict:
        self.reads += 1
        return {"ok": True, "action": "read_clipboard", "text": self.text, "chars": len(self.text),
                "truncated": False, "copied_by_run_action": True, "desktop_surface": True}


def _run(monkeypatch, body, decisions: list[dict], *, max_steps: int = 4) -> list[dict]:
    pending = iter(decisions)
    done = {"action": "done", "x": 0, "y": 0, "artifact_role": "", "reason": "Done."}
    contexts: list[dict] = []

    def fake_decide(user_id: int, image: dict, view: dict, context: dict) -> dict:
        contexts.append(dict(context))
        return dict(next(pending, done))

    async def narrate(kind: str, data: dict) -> None:
        pass

    monkeypatch.setattr("app.candidate_context.CandidateContextEngine", _DummyContextEngine)
    monkeypatch.setattr(extension_run, "_db_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(extension_run, "_record_context_usage_for_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(extension_run, "_decide_vision_action", fake_decide)
    asyncio.run(run_vision_loop(body, url=None, narrate=narrate, run_id=0, user_id=0, skip_navigate=True, max_steps=max_steps))
    return contexts


def test_a_copy_then_read_shows_the_text_to_the_next_decision_only_and_never_in_recent_lines(monkeypatch) -> None:
    init_db()
    body = _CopyingDesktopBody("Order ORD-77812, verification code: 482913")
    contexts = _run(
        monkeypatch,
        body,
        [
            {"action": "key", "key": "Ctrl+C", "reason": "Copy the order line.", "then": [{"action": "read_clipboard"}]},
            {"action": "key", "key": "Tab", "reason": "Move on."},
        ],
    )

    assert body.keys[:1] == ["Ctrl+C"] and body.reads == 1
    shown = contexts[1].get("clipboard_text")
    assert shown and "ORD-77812" in shown["text"]
    # One-time codes are hidden exactly as in page text.
    assert "482913" not in shown["text"]
    recent = " ".join(str(item) for item in contexts[1].get("recent_actions") or [])
    assert "clipboard read:" in recent and "ORD-77812" not in recent
    # After the brain acted on it, it is not shown again.
    assert "clipboard_text" not in contexts[2]


def test_the_browser_surface_has_no_clipboard_read(monkeypatch) -> None:
    init_db()
    contexts = _run(monkeypatch, _VisionBody(), [{"action": "read_clipboard", "reason": "Read it."}], max_steps=2)
    recent = " ".join(str(item) for item in contexts[1].get("recent_actions") or [])
    assert "clipboard_unavailable" in recent
    assert "clipboard_text" not in contexts[1]


# --- the Windows body's guards -------------------------------------------------------------------


def _body(monkeypatch, *, start: int, read: dict | None = None) -> desktop_body.DesktopBody:
    monkeypatch.setattr(desktop_body, "_require_enabled", lambda: None)
    monkeypatch.setattr(desktop_body, "_clipboard_sequence", lambda: start)
    body = desktop_body.DesktopBody()
    monkeypatch.setattr(desktop_body, "_read_clipboard_text", lambda: dict(read or {}))
    return body


def test_the_users_clipboard_from_before_the_run_is_never_read(monkeypatch) -> None:
    body = _body(monkeypatch, start=10, read={"text": "my bank PIN 0000", "sequence": 10})
    result = asyncio.run(body.vision_read_clipboard())
    assert result == {"ok": False, "action": "read_clipboard", "failure_reason": "clipboard_not_from_this_run", "desktop_surface": True}
    # A change no action of this run made (the user copied, another app wrote) is not readable either.
    monkeypatch.setattr(desktop_body, "_clipboard_sequence", lambda: 11)
    assert asyncio.run(body.vision_read_clipboard())["failure_reason"] == "clipboard_not_from_this_run"


def _copying_method_body(
    monkeypatch, sequences: list[int], *, password_focus: bool = False, owner_pid: int = 41, owner_exe: str = "notepad.exe"
):
    readings = iter(sequences)
    monkeypatch.setattr(desktop_body, "_clipboard_sequence", lambda: next(readings))
    monkeypatch.setattr(desktop_body, "_focused_state", lambda: {"password": password_focus})
    monkeypatch.setattr(
        desktop_body,
        "_clipboard_owner_process",
        lambda: {"pid": owner_pid, "exe": owner_exe, "credential_store": owner_exe in desktop_body._CREDENTIAL_STORE_PROCESSES},
    )
    monkeypatch.setattr(desktop_body, "_foreground_process_id", lambda: 41)

    class _Copier:
        _password_control_shown = False

        def __init__(self) -> None:
            self.copies: dict[int, dict] = {}

        def _note_clipboard_copy(self, sequence: int, *, sensitive: bool) -> None:
            self.copies[sequence] = {"sensitive": sensitive}

        @desktop_body._records_clipboard_copies
        async def vision_key(self, key: str) -> dict:
            return {"ok": True}

    return _Copier()


def test_an_input_action_that_changes_the_clipboard_records_a_run_copy(monkeypatch) -> None:
    copier = _copying_method_body(monkeypatch, [5, 6])
    assert asyncio.run(copier.vision_key("CTRL+C")) == {"ok": True, "clipboard_changed": True}
    assert copier.copies == {6: {"sensitive": False}}
    assert extension_run._vision_action_verified("key", {"ok": True, "clipboard_changed": True})
    unchanged = _copying_method_body(monkeypatch, [7, 7])
    assert asyncio.run(unchanged.vision_key("TAB")) == {"ok": True}
    assert unchanged.copies == {}
    in_password = _copying_method_body(monkeypatch, [8, 9], password_focus=True)
    asyncio.run(in_password.vision_key("CTRL+C"))
    assert in_password.copies == {9: {"sensitive": True}}
    near_password = _copying_method_body(monkeypatch, [8, 9])
    near_password._password_control_shown = True
    asyncio.run(near_password.vision_key("CTRL+C"))
    assert near_password.copies == {9: {"sensitive": True}}
    # Review: a copy another process made during the action (a cloud-clipboard push) is not the run's,
    # and anything a password manager puts on the clipboard is a secret.
    pushed = _copying_method_body(monkeypatch, [10, 11], owner_pid=77)
    assert asyncio.run(pushed.vision_key("CTRL+C")) == {"ok": True}
    assert pushed.copies == {}
    vault = _copying_method_body(monkeypatch, [12, 13], owner_exe="bitwarden.exe")
    asyncio.run(vault.vision_key("CTRL+C"))
    assert vault.copies == {13: {"sensitive": True}}


def test_a_run_copy_is_read_and_capped(monkeypatch) -> None:
    long_text = "x" * (desktop_body.CLIPBOARD_READ_LIMIT_CHARS + 50)
    body = _body(monkeypatch, start=10, read={"text": long_text, "sequence": 12, "private": False})
    body._note_clipboard_copy(12, sensitive=False)
    monkeypatch.setattr(desktop_body, "_clipboard_sequence", lambda: 12)
    result = asyncio.run(body.vision_read_clipboard())
    assert result["ok"] is True
    assert len(result["text"]) == desktop_body.CLIPBOARD_READ_LIMIT_CHARS
    assert result["chars"] == len(long_text) and result["truncated"] is True


def test_a_copy_near_a_password_or_marked_private_is_refused(monkeypatch) -> None:
    body = _body(monkeypatch, start=10, read={"text": "hunter2", "sequence": 12, "private": False})
    body._note_clipboard_copy(12, sensitive=True)
    monkeypatch.setattr(desktop_body, "_clipboard_sequence", lambda: 12)
    assert asyncio.run(body.vision_read_clipboard())["failure_reason"] == "clipboard_sensitive_source"

    body = _body(monkeypatch, start=10, read={"text": "s3cret", "sequence": 12, "private": True})
    body._note_clipboard_copy(12, sensitive=False)
    monkeypatch.setattr(desktop_body, "_clipboard_sequence", lambda: 12)
    assert asyncio.run(body.vision_read_clipboard())["failure_reason"] == "clipboard_sensitive_source"


def test_a_copy_replaced_between_the_check_and_the_read_is_refused(monkeypatch) -> None:
    body = _body(monkeypatch, start=10, read={"text": "someone else's", "sequence": 13, "private": False})
    body._note_clipboard_copy(12, sensitive=False)
    monkeypatch.setattr(desktop_body, "_clipboard_sequence", lambda: 12)
    assert asyncio.run(body.vision_read_clipboard())["failure_reason"] == "clipboard_not_from_this_run"


# --- the OSWorld body ----------------------------------------------------------------------------


def test_the_osworld_body_reads_only_a_clipboard_changed_since_the_run_began(monkeypatch) -> None:
    body = OSWorldBody("http://vm.test")
    clipboard = {"text": "left by setup"}

    def fake_execute(command, *, shell=False):
        if command[:1] == ["which"]:
            return {"status": "success", "output": "/usr/bin/xclip" if command[1] == "xclip" else ""}
        return {"status": "success", "output": clipboard["text"], "returncode": 0}

    monkeypatch.setattr(body, "_execute", fake_execute)
    assert body._read_clipboard_sync()["failure_reason"] == "clipboard_not_from_this_run"
    body._clipboard_start_digest = body._clipboard_digest(body._clipboard_text_sync()[0])
    assert body._read_clipboard_sync()["failure_reason"] == "clipboard_not_from_this_run"
    clipboard["text"] = "copied in the run"
    result = body._read_clipboard_sync()
    assert result["ok"] is True and result["text"] == "copied in the run"
    body._password_control_shown = True
    assert body._read_clipboard_sync()["failure_reason"] == "clipboard_sensitive_source"
