from __future__ import annotations

import asyncio
import base64
import io
import platform
import sys
import types

import pytest
from PIL import Image

from app import desktop_body, extension_run, vision_agent
from app.db import SessionLocal, init_db
from app.runtime import OperatorRuntimeEngine


class _FakePattern:
    def __init__(self, **attrs: object) -> None:
        self.__dict__.update(attrs)


class _FakeWrapper:
    """UIA wrapper double: configured iface_* patterns exist, everything else raises."""

    def __init__(self, **patterns: object) -> None:
        self._patterns = patterns

    def __getattr__(self, name: str):
        try:
            return self.__dict__["_patterns"][name]
        except KeyError:
            raise RuntimeError(f"pattern {name} unsupported") from None


class _Rect:
    def __init__(self, left: int, top: int, right: int, bottom: int) -> None:
        self.left, self.top, self.right, self.bottom = left, top, right, bottom


class _Point:
    def __init__(self, x: int, y: int) -> None:
        self.x, self.y = x, y


class _FakeInfo:
    def __init__(self, rect: _Rect, *, clickable: _Point | None = None, visible: bool = True, enabled: bool = True, control_type: str = "Button", name: str = "") -> None:
        self.rectangle = rect
        self.visible = visible
        self.enabled = enabled
        self.control_type = control_type
        self.name = name

        class _Element:
            def GetClickablePoint(self_inner):
                if clickable is None:
                    raise RuntimeError("no clickable point")
                return clickable, 1

        self.element = _Element()


class _FakeControl:
    def __init__(self, info: _FakeInfo, *, affording: bool = True, children: list | None = None) -> None:
        self.element_info = info
        self._affording = affording
        self._children = children or []
        if affording:
            self.iface_invoke = object()

    def descendants(self):
        return list(self._children)

    def __getattr__(self, name: str):
        raise RuntimeError(f"pattern {name} unsupported")


def test_clickable_point_prefers_uia_point_and_rejects_out_of_rect() -> None:
    inside = _FakeControl(_FakeInfo(_Rect(10, 10, 110, 40), clickable=_Point(30, 25)))
    assert desktop_body._clickable_point(inside) == (30, 25)

    # A clickable point OUTSIDE the element's rect is nonsense - fall back to center.
    outside = _FakeControl(_FakeInfo(_Rect(10, 10, 110, 40), clickable=_Point(500, 500)))
    assert desktop_body._clickable_point(outside) == (60, 25)

    # No clickable point at all - center.
    plain = _FakeControl(_FakeInfo(_Rect(0, 0, 100, 50), clickable=None))
    assert desktop_body._clickable_point(plain) == (50, 25)


def test_smallest_affording_control_snaps_to_the_actionable_element() -> None:
    checkbox = _FakeControl(
        _FakeInfo(_Rect(20, 20, 40, 40), clickable=_Point(30, 30), control_type="CheckBox", name="Remember"),
        affording=True,
    )
    label = _FakeControl(
        _FakeInfo(_Rect(45, 20, 200, 40), control_type="Text", name="Remember me"),
        affording=False,
    )
    row = _FakeControl(
        _FakeInfo(_Rect(10, 10, 210, 50), control_type="Pane"),
        affording=True,
        children=[checkbox, label],
    )

    # A point inside the checkbox resolves to the checkbox, not the affording row around it.
    assert desktop_body._smallest_affording_control(row, 25, 30) is checkbox
    # A point on the non-affording label falls through to the smallest affording container.
    assert desktop_body._smallest_affording_control(row, 100, 30) is row
    # A point outside everything resolves to nothing.
    assert desktop_body._smallest_affording_control(row, 500, 500) is None


def test_control_affordances_enumerate_raw_uia_patterns() -> None:
    checked_box = desktop_body._control_affordances(
        _FakeWrapper(iface_toggle=_FakePattern(CurrentToggleState=1))
    )
    assert checked_box["checked"] is True
    assert "toggle" in checked_box["affordances"]

    unchecked_box = desktop_body._control_affordances(
        _FakeWrapper(iface_toggle=_FakePattern(CurrentToggleState=0))
    )
    assert unchecked_box["checked"] is False

    # Indeterminate state stays honest: the toggle affordance is reported, but no
    # boolean checked fact is fabricated for a state that is neither on nor off.
    indeterminate = desktop_body._control_affordances(
        _FakeWrapper(iface_toggle=_FakePattern(CurrentToggleState=2))
    )
    assert "checked" not in indeterminate
    assert "toggle" in indeterminate["affordances"]

    entry = desktop_body._control_affordances(
        _FakeWrapper(iface_value=_FakePattern(CurrentIsReadOnly=False))
    )
    assert entry["accepts_text"] is True
    assert "set_value" in entry["affordances"]

    read_only = desktop_body._control_affordances(
        _FakeWrapper(iface_value=_FakePattern(CurrentIsReadOnly=True))
    )
    assert "accepts_text" not in read_only
    assert "affordances" not in read_only

    combo = desktop_body._control_affordances(
        _FakeWrapper(iface_expand_collapse=_FakePattern(CurrentExpandCollapseState=0))
    )
    assert combo["expanded"] is False
    assert "expand_collapse" in combo["affordances"]

    # LeafNode (3) means the pattern is present but inert - not an affordance.
    leaf = desktop_body._control_affordances(
        _FakeWrapper(iface_expand_collapse=_FakePattern(CurrentExpandCollapseState=3))
    )
    assert leaf == {}

    button = desktop_body._control_affordances(_FakeWrapper(iface_invoke=object()))
    assert button["affordances"] == ["invoke"]

    selected_row = desktop_body._control_affordances(
        _FakeWrapper(iface_selection_item=_FakePattern(CurrentIsSelected=True))
    )
    assert selected_row["selected"] is True

    plain = desktop_body._control_affordances(_FakeWrapper())
    assert plain == {}


def test_desktop_actions_are_part_of_the_model_contract() -> None:
    schema = vision_agent.action_schema()["schema"]
    assert {"launch_app", "focus_window", "wait"}.issubset(
        set(schema["properties"]["action"]["enum"])
    )
    assert schema["properties"]["target"]["type"] == "string"
    assert vision_agent.normalize_action(
        {"action": "launch_app", "target": "app:exact"}
    )["invalid_reason"] == ""
    assert vision_agent.normalize_action(
        {"action": "focus_window", "target": "hwnd:123"}
    )["invalid_reason"] == ""
    assert vision_agent.normalize_action(
        {"action": "wait", "wait_ms": 600}
    )["wait_ms"] == 600


def test_desktop_prompt_uses_windows_surface_and_tool_inventory() -> None:
    prepared = vision_agent.prepare_model_input(
        {"width": 1280, "height": 720, "url": "desktop://windows", "title": "Notepad"},
        {
            "goal": "Type the exact supplied sentence in Notepad.",
            "execution_surface": "desktop",
            "desktop_tools": {
                "windows": [{"window_id": "hwnd:1", "title": "Notepad"}],
                "applications": [{"app_id": "app:1", "name": "Notepad"}],
            },
        },
    )
    assert "Windows desktop" in prepared["system"]
    assert prepared["prompt"]["execution_surface"] == "desktop"
    assert prepared["prompt"]["desktop_tools"]["applications"][0]["app_id"] == "app:1"
    assert "never type the application name into the current window" in prepared["system"]


def test_desktop_control_indicator_tracks_exact_body_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(desktop_body, "_windows_available", lambda: True)
    monkeypatch.setattr(desktop_body, "_configured", lambda: True)
    monkeypatch.setitem(sys.modules, "pywinauto", types.ModuleType("pywinauto"))
    monkeypatch.setitem(sys.modules, "win32api", types.ModuleType("win32api"))
    monkeypatch.setitem(sys.modules, "win32gui", types.ModuleType("win32gui"))
    desktop_body.set_runtime_enabled(True)
    # Browser drive tests elsewhere mirror onto the same screen-indicator globals;
    # start from a clean slate so this test asserts only its own writes.
    with desktop_body._CONTROL_LOCK:
        desktop_body._ACTIVE_CONTROL_RUN_IDS.clear()
        desktop_body._CONTROL_STATUS_MESSAGE = ""
    body = desktop_body.DesktopBody()

    started = asyncio.run(body.set_control_indicator(True, run_id=912))
    status = desktop_body.desktop_control_status()
    stopped = asyncio.run(body.set_control_indicator(False, run_id=912))

    assert started == {
        "ok": True,
        "active": True,
        "visible": True,
        "surface": "desktop",
    }
    assert status["control_indicator"] == {
        "active": True,
        "message": "Lumen is using your computer",
        "run_ids": [912],
        "desktop_surface_active": True,
    }
    assert stopped["visible"] is False
    assert desktop_body.desktop_control_status()["control_indicator"]["active"] is False

    # A browser run's overlay mirror lights the pill but never reports the desktop
    # surface as active (the host must not minimize Lumen for a browser task).
    asyncio.run(body.set_control_indicator(True, run_id=914, surface="browser"))
    mirrored = desktop_body.desktop_control_status()["control_indicator"]
    assert mirrored["active"] is True
    assert mirrored["desktop_surface_active"] is False
    asyncio.run(body.set_control_indicator(False, run_id=914))


def test_desktop_control_indicator_carries_the_live_narration_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(desktop_body, "_windows_available", lambda: True)
    monkeypatch.setattr(desktop_body, "_configured", lambda: True)
    monkeypatch.setitem(sys.modules, "pywinauto", types.ModuleType("pywinauto"))
    monkeypatch.setitem(sys.modules, "win32api", types.ModuleType("win32api"))
    monkeypatch.setitem(sys.modules, "win32gui", types.ModuleType("win32gui"))
    desktop_body.set_runtime_enabled(True)
    # Browser drive tests elsewhere mirror onto the same screen-indicator globals;
    # start from a clean slate so this test asserts only its own writes.
    with desktop_body._CONTROL_LOCK:
        desktop_body._ACTIVE_CONTROL_RUN_IDS.clear()
        desktop_body._CONTROL_STATUS_MESSAGE = ""
    body = desktop_body.DesktopBody()

    # Each step's one-line thought replaces the pill message; the default is only a
    # fallback. The message clears with the last active run so a finished run can
    # never leave stale narration on screen.
    asyncio.run(body.set_control_indicator(True, run_id=913, message="Searching for showtimes"))
    assert (
        desktop_body.desktop_control_status()["control_indicator"]["message"]
        == "Searching for showtimes"
    )
    asyncio.run(body.set_control_indicator(True, run_id=913, message="Picking  the\n7:30 PM show"))
    assert (
        desktop_body.desktop_control_status()["control_indicator"]["message"]
        == "Picking the 7:30 PM show"
    )
    asyncio.run(body.set_control_indicator(False, run_id=913))
    assert (
        desktop_body.desktop_control_status()["control_indicator"]["message"]
        == "Lumen is using your computer"
    )


def test_application_inventory_combines_shell_apps_and_filters_shell_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        desktop_body,
        "_start_menu_app_candidates",
        lambda: [
            ("Notepad", "shortcut", "C:/Start/Notepad.lnk"),
            ("Git Bash", "shortcut", "C:/Start/Git Bash.lnk"),
        ],
    )
    monkeypatch.setattr(
        desktop_body,
        "_shell_app_candidates",
        lambda: [
            ("Calculator", "shell_app", "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"),
            ("Windows PowerShell", "shell_app", "Microsoft.PowerShell"),
        ],
    )
    monkeypatch.setattr(desktop_body, "_APP_CACHE", None)

    rows, index = desktop_body._application_inventory()
    names = {row["name"] for row in rows}
    assert names == {"Calculator", "Notepad"}
    calculator_id = next(row["app_id"] for row in rows if row["name"] == "Calculator")
    assert index[calculator_id]["kind"] == "shell_app"
    assert index[calculator_id]["target"] == "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"


def test_desktop_launch_action_executes_through_the_shared_vision_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_db()
    output = io.BytesIO()
    Image.new("RGB", (160, 100), "white").save(output, format="JPEG")
    image = base64.b64encode(output.getvalue()).decode("ascii")

    class FakeDesktopBody:
        execution_surface = "desktop"
        device_id = "fake-windows"

        def __init__(self) -> None:
            self.launched = False

        def tool_context(self) -> dict:
            return {
                "applications": [{"app_id": "app:test", "name": "Test App"}],
                "windows": [],
            }

        async def capture_screenshot(self, **_kwargs: object) -> dict:
            return {
                "ok": True,
                "image": image,
                "width": 160,
                "height": 100,
                "url": "desktop://windows",
                "title": "Test App" if self.launched else "Windows desktop",
                "scroll_y": 0,
                "scroll_height": 100,
                "page_evidence": {
                    "url": "desktop://windows",
                    "title": "Test App" if self.launched else "Windows desktop",
                    "viewport_text": "Test App" if self.launched else "Windows desktop",
                    "controls": [],
                },
            }

        async def vision_launch_app(self, app_id: str) -> dict:
            assert app_id == "app:test"
            self.launched = True
            return {
                "ok": True,
                "surface_changed": True,
                "title_changed": True,
                "launched_app_id": app_id,
                "new_window_id": "hwnd:1",
                "new_window_title": "Test App",
            }

    body = FakeDesktopBody()
    with SessionLocal() as db:
        user = OperatorRuntimeEngine(db).ensure_default_user()
        run = OperatorRuntimeEngine(db).create_browser_task(
            user_id=user.id,
            goal="Launch the exact Test App inventory entry.",
            target_url="",
            execution_surface="desktop",
        )
        run_id = int(run.id)
        user_id = int(user.id)
    monkeypatch.setattr(
        vision_agent,
        "decide_next_action",
        lambda *_args, **_kwargs: vision_agent.normalize_action(
            {
                "action": "launch_app",
                "target": "app:test",
                "reason": "The requested app is not open, so I am launching its exact inventory entry.",
            }
        ),
    )

    async def narrate(_kind: str, _data: dict) -> None:
        return None

    summary = asyncio.run(
        extension_run.run_vision_loop(
            body,
            url=None,
            narrate=narrate,
            run_id=run_id,
            user_id=user_id,
            skip_navigate=True,
            max_steps=1,
        )
    )
    assert body.launched is True
    assert summary["receipts"][0]["type"] == "desktop_app_launched"
    assert summary["receipts"][0]["outcome"] == "verified_succeeded"


@pytest.mark.skipif(platform.system() != "Windows", reason="Windows body is Windows-only")
def test_desktop_capture_returns_maskable_visual_and_raw_inventory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controls = [
        {
            "selector": "uia:hwnd=101;rid=1.2.3",
            "name": "Password",
            "role": "Edit",
            "box": [100, 100, 60, 60],
        }
    ]
    windows = [
        {
            "window_id": "hwnd:101",
            "title": "Test window",
            "pid": 202,
            "active": True,
            "box": [0, 0, 800, 600],
        }
    ]
    monkeypatch.setattr(desktop_body, "_configured", lambda: True)
    monkeypatch.setattr(
        desktop_body,
        "_capture_image",
        lambda: Image.new("RGB", (800, 600), "white"),
    )
    monkeypatch.setattr(
        desktop_body,
        "_active_controls",
        lambda: (controls, [(100, 100, 160, 160)]),
    )
    monkeypatch.setattr(desktop_body, "_window_inventory", lambda: windows)
    monkeypatch.setattr(
        desktop_body,
        "_application_inventory",
        lambda: ([{"app_id": "app:test", "name": "Test App"}], {}),
    )
    desktop_body.set_runtime_enabled(True)
    shot = asyncio.run(desktop_body.DesktopBody().capture_screenshot())
    assert shot["ok"] is True
    assert shot["width"] == 800 and shot["height"] == 600
    encoded_image = base64.b64decode(shot["image"])
    assert len(encoded_image) > 1000
    captured_image = Image.open(io.BytesIO(encoded_image)).convert("RGB")
    assert max(captured_image.getpixel((130, 130))) < 64
    assert shot["page_evidence"]["execution_surface"] == "desktop"
    assert shot["page_evidence"]["controls"] == controls
    assert shot["page_evidence"]["windows"] == windows
    assert shot["page_evidence"]["password_controls_masked"] == 1
    assert desktop_body.DesktopBody().tool_context()["windows"] == windows


@pytest.mark.skipif(platform.system() != "Windows", reason="Windows body is Windows-only")
def test_dangerous_system_shortcuts_are_blocked() -> None:
    # Only session-ending / secure-desktop shortcuts stay blocked.
    for shortcut in ("ALT+F4", "WIN+R", "WIN+L", "CTRL+ALT+DELETE"):
        with pytest.raises(ValueError):
            desktop_body._send_key(shortcut)


def test_typing_refuses_a_window_the_task_never_targeted(monkeypatch: pytest.MonkeyPatch) -> None:
    # Typing lands wherever Windows focus is. If the foreground window was never focused
    # or clicked by this task, it belongs to something else and the value must not go there.
    monkeypatch.setattr(desktop_body, "_configured", lambda: True)
    monkeypatch.setattr(desktop_body, "_windows_available", lambda: True)
    monkeypatch.setattr(
        desktop_body,
        "desktop_control_status",
        lambda: {"available": True, "enabled": True},
    )
    monkeypatch.setattr(desktop_body, "_focused_state", lambda: {"typable": True})
    monkeypatch.setattr(desktop_body, "_foreground_window", lambda: (77, "Some Other App"))
    monkeypatch.setattr(desktop_body, "_LAST_TARGET_WINDOW", 42)
    sent: list[str] = []
    monkeypatch.setattr(desktop_body, "_send_unicode", lambda text: sent.append(text))
    monkeypatch.setattr(desktop_body, "_send_key", lambda *args, **kwargs: None)

    result = asyncio.run(desktop_body.DesktopBody().vision_type("value for the real target"))

    assert result["ok"] is False
    assert result["failure_reason"] == "desktop_untargeted_type_window"
    assert result["input_dispatched"] is False
    assert "Some Other App" in result["failure_detail"]
    assert sent == [], "no text may reach an unintended window"

    # Once the task has targeted that exact window, typing proceeds.
    monkeypatch.setattr(desktop_body, "_LAST_TARGET_WINDOW", 77)
    monkeypatch.setattr(desktop_body, "_capture_image", lambda: None)
    monkeypatch.setattr(desktop_body, "_image_changed", lambda before, after: True)
    # The value must APPEAR because of the keystrokes: empty before, present after.
    typed_yet: list[bool] = []
    monkeypatch.setattr(
        desktop_body,
        "_send_unicode",
        lambda text: (sent.append(text), typed_yet.append(True)) and None,
    )
    monkeypatch.setattr(
        desktop_body,
        "_active_controls",
        lambda: (
            ([{"tag": "edit", "value": "value for the real target", "password": False}] if typed_yet else []),
            [],
        ),
    )
    ok = asyncio.run(desktop_body.DesktopBody().vision_type("value for the real target"))
    assert ok["ok"] is True
    assert ok["value_read_back"] is True
    assert ok["active_value_matches_expected"] is True
    assert sent == ["value for the real target"]


def test_typing_reports_when_the_value_never_appears(monkeypatch: pytest.MonkeyPatch) -> None:
    # Foregrounding a window does not put the caret in its text area. A blinking caret
    # still repaints the screen, so the body settles it by reading the value back: if the
    # window exposes text and the typed value is not in it, the keystrokes went nowhere.
    monkeypatch.setattr(desktop_body, "_configured", lambda: True)
    monkeypatch.setattr(desktop_body, "_windows_available", lambda: True)
    monkeypatch.setattr(
        desktop_body,
        "desktop_control_status",
        lambda: {"available": True, "enabled": True},
    )
    monkeypatch.setattr(desktop_body, "_focused_state", lambda: {"typable": False, "value": ""})
    monkeypatch.setattr(desktop_body, "_foreground_window", lambda: (77, "Untitled - Notepad"))
    monkeypatch.setattr(desktop_body, "_LAST_TARGET_WINDOW", 77)
    monkeypatch.setattr(desktop_body, "_capture_image", lambda: None)
    # The caret repaints the screen even though the value never arrived.
    monkeypatch.setattr(desktop_body, "_image_changed", lambda before, after: True)
    monkeypatch.setattr(
        desktop_body,
        "_active_controls",
        lambda: ([{"tag": "edit", "value": "some unrelated existing text", "password": False}], []),
    )
    monkeypatch.setattr(desktop_body, "_send_unicode", lambda text: None)
    monkeypatch.setattr(desktop_body, "_send_key", lambda *args, **kwargs: None)

    result = asyncio.run(desktop_body.DesktopBody().vision_type("nothing lands here"))

    assert result["ok"] is False
    assert result["failure_reason"] == "desktop_type_had_no_effect"
    assert "Click the middle of the exact text area first" in result["failure_detail"]


def test_full_human_keyboard_is_addressable() -> None:
    # Every key a person can press resolves to a virtual-key code, so the brain never has
    # to work around a missing editing or navigation key.
    for key in (
        "BACKSPACE",
        "DELETE",
        "HOME",
        "END",
        "PAGEUP",
        "PAGEDOWN",
        "INSERT",
        "TAB",
        "ENTER",
        "ESCAPE",
        "SPACE",
        "ARROWLEFT",
        "F1",
        "F12",
        "F24",
        "A",
        "9",
        ";",
        "/",
    ):
        assert desktop_body._key_code(key) > 0

    # Aliases a model is likely to emit normalize to the same keys.
    assert desktop_body._normalize_key_parts("ctrl+backspace") == ["CTRL", "BACKSPACE"]
    assert desktop_body._normalize_key_parts("Del") == ["DELETE"]
    assert desktop_body._normalize_key_parts("PgDn") == ["PAGEDOWN"]
    assert desktop_body._normalize_key_parts("Shift + End") == ["SHIFT", "END"]

    # Ordinary editing shortcuts are available, not blocked.
    for shortcut in ("CTRL+A", "CTRL+C", "CTRL+V", "CTRL+Z", "CTRL+S", "SHIFT+END"):
        parts = desktop_body._normalize_key_parts(shortcut)
        assert tuple(parts) not in desktop_body._BLOCKED_SHORTCUTS
        assert desktop_body._key_code(parts[-1]) > 0


def test_pointer_events_carry_geometry_only_and_page_by_seq() -> None:
    desktop_body.set_runtime_enabled(True)
    baseline = desktop_body.pointer_events()["last_seq"]

    clicked = desktop_body.publish_pointer_event("click", x=140, y=90)
    typed = desktop_body.publish_pointer_event("type")
    keyed = desktop_body.publish_pointer_event("key", label="CTRL+A")

    payload = desktop_body.pointer_events(baseline)
    kinds = [item["kind"] for item in payload["events"]]
    assert payload["last_seq"] == keyed["seq"]
    assert kinds == ["click", "type", "key"]
    assert payload["events"][0]["x"] == 140 and payload["events"][0]["y"] == 90
    assert "x" not in payload["events"][1], "typing without a target publishes no geometry"
    assert payload["events"][2]["label"] == "CTRL+A"
    for event in payload["events"]:
        assert set(event) <= {"seq", "kind", "x", "y", "dy", "label", "ts"}, (
            "pointer events must never grow value/text payloads"
        )

    newer = desktop_body.pointer_events(typed["seq"])
    assert [item["seq"] for item in newer["events"]] == [keyed["seq"]]
    assert clicked["seq"] < typed["seq"] < keyed["seq"]


def test_pointer_event_buffer_is_bounded_and_cleared_on_disable() -> None:
    desktop_body.set_runtime_enabled(True)
    baseline = desktop_body.pointer_events()["last_seq"]
    for index in range(80):
        desktop_body.publish_pointer_event("click", x=index, y=index)
    payload = desktop_body.pointer_events(baseline)
    assert payload["last_seq"] == baseline + 80
    assert len(payload["events"]) == 64, "the buffer keeps only the newest gestures"

    desktop_body.set_runtime_enabled(False)
    assert desktop_body.pointer_events()["events"] == []
    desktop_body.set_runtime_enabled(True)


def test_deliver_text_routes_long_values_through_paste_and_degrades_to_keystrokes(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        desktop_body, "_deliver_text_via_clipboard", lambda text, hold, **_: calls.append(("paste", text)) or True
    )
    monkeypatch.setattr(desktop_body, "_send_unicode", lambda text: calls.append(("keystrokes", text)))

    short = "Jordan"
    assert desktop_body._deliver_text(short, clipboard=desktop_body._ClipboardHold()) == "keystrokes"
    assert calls == [("keystrokes", short)]

    calls.clear()
    long_text = "I am writing to express my interest in the role. " * 6
    assert len(long_text) >= desktop_body._PASTE_THRESHOLD_CHARS
    assert desktop_body._deliver_text(long_text, clipboard=desktop_body._ClipboardHold()) == "paste"
    assert calls == [("paste", long_text)]

    # No one to settle the user's clipboard after a read-back: never pasted.
    calls.clear()
    assert desktop_body._deliver_text(long_text) == "keystrokes"
    assert calls == [("keystrokes", long_text)]

    # Clipboard unavailable (False) or broken before anything was pasted (raises): keystrokes.
    calls.clear()
    monkeypatch.setattr(desktop_body, "_deliver_text_via_clipboard", lambda text, hold, **_: False)
    assert desktop_body._deliver_text(long_text, clipboard=desktop_body._ClipboardHold()) == "keystrokes"
    assert calls == [("keystrokes", long_text)]

    calls.clear()

    def _broken(text: str, hold, **_: object) -> bool:
        raise OSError("clipboard held by another process")

    monkeypatch.setattr(desktop_body, "_deliver_text_via_clipboard", _broken)
    assert desktop_body._deliver_text(long_text, clipboard=desktop_body._ClipboardHold()) == "keystrokes"
    assert calls == [("keystrokes", long_text)]

    # Ctrl+V itself failed: the paste may have landed, so nothing is typed after it.
    calls.clear()

    def _paste_failed(text: str, hold, **_: object) -> bool:
        hold.hold("the user's clipboard")
        raise OSError("SendInput did not inject Ctrl+V")

    monkeypatch.setattr(desktop_body, "_deliver_text_via_clipboard", _paste_failed)
    hold = desktop_body._ClipboardHold()
    assert desktop_body._deliver_text(long_text, clipboard=hold) == "paste_unconfirmed"
    assert calls == [], "never typed on top of a paste that may have landed"
    assert hold.pending, "the caller's read-back settles the clipboard"


def test_chrome_profiles_enumerates_local_state_catalog(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Pure eyes: the browser's own profile catalog, directory + display name. Choosing
    # between profiles is the user's call through the question flow, never this code's.
    import json

    local_state_dir = tmp_path / "Google" / "Chrome" / "User Data"
    local_state_dir.mkdir(parents=True)
    (local_state_dir / "Local State").write_text(
        json.dumps(
            {
                "profile": {
                    "info_cache": {
                        "Default": {"name": "Jordan"},
                        "Profile 1": {"name": "Work"},
                        "Broken": "not-a-dict",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert desktop_body.chrome_profiles() == [
        {"directory": "Default", "name": "Jordan"},
        {"directory": "Profile 1", "name": "Work"},
    ]

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "nowhere"))
    assert desktop_body.chrome_profiles() == []


def test_a_proxied_control_is_re_found_by_point_only_when_its_name_matches(monkeypatch) -> None:
    body = desktop_body.DesktopBody()
    clicks: list[tuple[int, int]] = []

    async def fake_click(self, x: int, y: int, **_kwargs: object) -> dict:
        clicks.append((x, y))
        return {"ok": True, "action": "click", "desktop_surface": True}

    monkeypatch.setattr(desktop_body, "_require_enabled", lambda: None)
    monkeypatch.setattr(desktop_body, "_resolve_control", lambda selector: None)
    monkeypatch.setattr(desktop_body.DesktopBody, "vision_click", fake_click)

    monkeypatch.setattr(desktop_body, "_exact_click_target", lambda x, y: {"x": x, "y": y, "clicked_control_name": "Tools"})
    rebound = asyncio.run(body.vision_click_control("uia:1:2", 183, 130, expected_target_identity={"target_text": "Tools"}))
    assert rebound["ok"] is True and rebound["rebound_by_point"] is True
    assert clicks == [(183, 130)]

    monkeypatch.setattr(desktop_body, "_exact_click_target", lambda x, y: {"x": x, "y": y, "clicked_control_name": "Reset"})
    refused = asyncio.run(body.vision_click_control("uia:1:2", 183, 130, expected_target_identity={"target_text": "Tools"}))
    assert refused["ok"] is False and refused["failure_reason"] == "desktop_uia_target_stale"
    assert clicks == [(183, 130)], "a different control under the point is never clicked"


def test_an_unnamed_proxied_control_is_re_found_by_point_and_type(monkeypatch) -> None:
    body = desktop_body.DesktopBody()
    clicks: list[tuple[int, int]] = []

    async def fake_click(self, x: int, y: int, **_kwargs: object) -> dict:
        clicks.append((x, y))
        return {"ok": True, "action": "click", "desktop_surface": True}

    monkeypatch.setattr(desktop_body, "_require_enabled", lambda: None)
    monkeypatch.setattr(desktop_body, "_resolve_control", lambda selector: None)
    monkeypatch.setattr(desktop_body.DesktopBody, "vision_click", fake_click)
    expected = {"target_text": "", "target_role": "menuitem", "target_tag": "menuitem"}

    monkeypatch.setattr(desktop_body, "_exact_click_target", lambda x, y: {"clicked_control_name": "", "clicked_control_type": "MenuItem"})
    assert asyncio.run(body.vision_click_control("uia:1:2", 380, 255, expected_target_identity=expected))["ok"] is True

    monkeypatch.setattr(desktop_body, "_exact_click_target", lambda x, y: {"clicked_control_name": "", "clicked_control_type": "Button"})
    assert asyncio.run(body.vision_click_control("uia:1:2", 380, 255, expected_target_identity=expected))["ok"] is False
    assert clicks == [(380, 255)]


def test_pixel_change_at_the_caret_is_located_near_the_clicked_field() -> None:
    from PIL import ImageDraw

    before = Image.new("RGB", (1200, 800), (235, 235, 235))
    after = before.copy()
    ImageDraw.Draw(after).text((470, 445), "D9-JZF", fill=(0, 0, 0))
    assert desktop_body._changed_near_point(before, after, (460, 453)) is True
    assert desktop_body._changed_near_point(before, after, (100, 700)) is False
    assert desktop_body._changed_near_point(before, before.copy(), (460, 453)) is False


def test_a_double_click_on_a_bound_desktop_control_stays_a_double_click(monkeypatch) -> None:
    body = desktop_body.DesktopBody()
    calls: list[dict] = []

    async def fake_click(self, x: int, y: int, **kwargs: object) -> dict:
        calls.append({"x": x, "y": y, **kwargs})
        return {"ok": True, "action": "click", "desktop_surface": True}

    monkeypatch.setattr(desktop_body, "_require_enabled", lambda: None)
    monkeypatch.setattr(desktop_body, "_resolve_control", lambda selector: None)
    monkeypatch.setattr(desktop_body.DesktopBody, "vision_click", fake_click)
    monkeypatch.setattr(desktop_body, "_exact_click_target", lambda x, y: {"clicked_control_name": "inbox", "clicked_control_type": "ListItem"})

    result = asyncio.run(
        body.vision_click_control("uia:1:2", 204, 159, clicks=2, expected_target_identity={"target_text": "inbox"})
    )
    assert result["ok"] is True
    assert calls == [{"x": 204, "y": 159, "button": "left", "clicks": 2, "hold_ms": 0, "modifiers": []}]


def test_typing_at_the_caret_never_selects_the_whole_field(monkeypatch) -> None:
    import asyncio

    document = {"value": "Container ID: ________\nSeal: ________"}
    keys: list[str] = []
    restored: list[bool] = []

    def deliver(text: str, **_kwargs: object) -> str:
        document["value"] = document["value"].replace("________", text, 1)
        return "keystrokes"

    monkeypatch.setattr(desktop_body, "_require_enabled", lambda: None)
    monkeypatch.setattr(desktop_body, "_focused_state", lambda: {"typable": True, "runtime_id": "1"})
    monkeypatch.setattr(desktop_body, "_foreground_window", lambda: (7, "notes.txt - Notepad"))
    monkeypatch.setattr(desktop_body, "_LAST_TARGET_WINDOW", 7)
    monkeypatch.setattr(desktop_body, "_publish_pointer", lambda *a, **k: None)
    monkeypatch.setattr(desktop_body, "_restore_caret_at_last_click", lambda: restored.append(True) or True)
    monkeypatch.setattr(desktop_body, "_readable_active_values", lambda: [document["value"]])
    monkeypatch.setattr(desktop_body, "_capture_image", lambda: object())
    monkeypatch.setattr(desktop_body, "_image_changed", lambda a, b: True)
    monkeypatch.setattr(desktop_body, "_send_key", lambda key: keys.append(key))
    monkeypatch.setattr(desktop_body, "_deliver_text", deliver)
    monkeypatch.setattr(desktop_body, "_caret_paragraph", lambda: {})
    monkeypatch.setattr(desktop_body.time, "sleep", lambda *_: None)

    result = asyncio.run(desktop_body.DesktopBody().vision_type("WDLQ-8", at_caret=True))

    assert "CTRL+A" not in keys
    assert restored == [], "the caret the model placed is not moved while focus is still on the field"
    assert document["value"] == "Container ID: WDLQ-8\nSeal: ________"
    assert result["ok"] is True and result["active_value_matches_expected"] is True

    asyncio.run(desktop_body.DesktopBody().vision_type("SL-1"))
    assert keys == ["CTRL+A"], "a plain type still sets the field's whole value"


def test_a_control_bound_type_goes_only_where_focus_landed(monkeypatch) -> None:
    import asyncio
    from types import SimpleNamespace

    rect = SimpleNamespace(left=100, top=100, right=300, bottom=130)
    wrapper = SimpleNamespace(element_info=SimpleNamespace(rectangle=rect), set_focus=lambda: None)
    typed: list[dict] = []

    async def fake_type(self, text, **kwargs):
        typed.append({"text": text, **kwargs})
        return {"ok": True, "action": "type", "active_value_matches_expected": True}

    monkeypatch.setattr(desktop_body, "_require_enabled", lambda: None)
    monkeypatch.setattr(desktop_body, "_resolve_control", lambda selector: wrapper)
    monkeypatch.setattr(desktop_body, "_publish_pointer", lambda *a, **k: None)
    monkeypatch.setattr(desktop_body.time, "sleep", lambda *_: None)
    monkeypatch.setattr(desktop_body.DesktopBody, "vision_type", fake_type)

    # Focus did not move into the control: nothing is typed anywhere.
    monkeypatch.setattr(desktop_body, "_focus_within", lambda w: False)
    refused = asyncio.run(desktop_body.DesktopBody().vision_type_control("uia:hwnd=1;rid=2", r"C:\mission\outbox"))
    assert refused["failure_reason"] == "desktop_type_target_not_focused" and typed == []

    # Focus is on it: typing goes there, without refocusing the field of an earlier click.
    monkeypatch.setattr(desktop_body, "_focus_within", lambda w: True)
    done = asyncio.run(desktop_body.DesktopBody().vision_type_control("uia:hwnd=1;rid=2", r"C:\mission\outbox"))
    assert done["ok"] is True
    assert typed == [{"text": r"C:\mission\outbox", "at_caret": False, "restore_caret": False, "sensitive": False}]


def test_a_dialog_of_the_targeted_app_is_part_of_the_task(monkeypatch) -> None:
    import sys
    from types import SimpleNamespace

    pids = {100: 7, 200: 7, 300: 9}
    monkeypatch.setitem(sys.modules, "win32process", SimpleNamespace(GetWindowThreadProcessId=lambda h: (1, pids[h])))
    assert desktop_body._same_app_window(100, 100) is True
    assert desktop_body._same_app_window(200, 100) is True, "its own Save dialog"
    assert desktop_body._same_app_window(300, 100) is False, "another app"
    assert desktop_body._same_app_window(200, 0) is False


def test_unknown_windows_are_not_the_same_app(monkeypatch) -> None:
    import sys
    from types import SimpleNamespace

    monkeypatch.setitem(sys.modules, "win32process", SimpleNamespace(GetWindowThreadProcessId=lambda h: (0, -181832592)))
    assert desktop_body._same_app_window(77, 42) is False


def test_typing_keeps_a_caret_the_model_put_in_a_field_and_restores_only_stolen_focus(monkeypatch) -> None:
    import asyncio

    restored: list[bool] = []
    focus = {"state": {"typable": True, "runtime_id": (1,), "center": [640, 310]}}
    monkeypatch.setattr(desktop_body, "_require_enabled", lambda: None)
    monkeypatch.setattr(desktop_body, "_focused_state", lambda: dict(focus["state"]))
    monkeypatch.setattr(desktop_body, "_foreground_window", lambda: (7, "Save As"))
    monkeypatch.setattr(desktop_body, "_LAST_TARGET_WINDOW", 7)
    monkeypatch.setattr(desktop_body, "_publish_pointer", lambda *a, **k: None)
    monkeypatch.setattr(desktop_body, "_restore_caret_at_last_click", lambda: restored.append(True) or True)
    values = {"before": [], "after": [r"C:\mission\outbox"]}
    calls = {"n": 0}

    def readable() -> list:
        calls["n"] += 1
        return values["before"] if calls["n"] % 2 == 1 else values["after"]

    monkeypatch.setattr(desktop_body, "_readable_active_values", readable)
    monkeypatch.setattr(desktop_body, "_capture_image", lambda: object())
    monkeypatch.setattr(desktop_body, "_image_changed", lambda a, b: True)
    monkeypatch.setattr(desktop_body, "_send_key", lambda key: None)
    monkeypatch.setattr(desktop_body, "_deliver_text", lambda text, **_: "keystrokes")
    monkeypatch.setattr(desktop_body.time, "sleep", lambda *_: None)

    # Focus is in a text field (Alt+D put it in the address box): typing goes there.
    result = asyncio.run(desktop_body.DesktopBody().vision_type(r"C:\mission\outbox"))
    assert restored == [] and result["ok"] is True
    assert result["focus_point"] == [640, 310]

    # An overlay took focus: the last clicked field is refocused first.
    focus["state"] = {"typable": False, "runtime_id": (9,)}
    asyncio.run(desktop_body.DesktopBody().vision_type(r"C:\mission\outbox"))
    assert restored == [True]


def test_a_plain_type_into_a_document_goes_in_at_the_caret(monkeypatch) -> None:
    import asyncio

    keys: list[str] = []
    focus = {"state": {"typable": True, "document": True, "runtime_id": (4,), "center": [800, 500]}}
    monkeypatch.setattr(desktop_body, "_require_enabled", lambda: None)
    monkeypatch.setattr(desktop_body, "_focused_state", lambda: dict(focus["state"]))
    monkeypatch.setattr(desktop_body, "_foreground_window", lambda: (7, "notes.txt - Notepad"))
    monkeypatch.setattr(desktop_body, "_LAST_TARGET_WINDOW", 7)
    monkeypatch.setattr(desktop_body, "_publish_pointer", lambda *a, **k: None)
    monkeypatch.setattr(desktop_body, "_restore_caret_at_last_click", lambda: True)
    monkeypatch.setattr(desktop_body, "_readable_active_values", lambda: [])
    monkeypatch.setattr(desktop_body, "_capture_image", lambda: object())
    monkeypatch.setattr(desktop_body, "_image_changed", lambda a, b: True)
    monkeypatch.setattr(desktop_body, "_changed_near_point", lambda *a: True)
    monkeypatch.setattr(desktop_body, "_send_key", lambda key: keys.append(key))
    monkeypatch.setattr(desktop_body, "_deliver_text", lambda text, **_: "keystrokes")
    monkeypatch.setattr(desktop_body, "_caret_paragraph", lambda: {})
    monkeypatch.setattr(desktop_body.time, "sleep", lambda *_: None)

    asyncio.run(desktop_body.DesktopBody().vision_type("WDLQ-8"))
    assert keys == [], "a document is typed into at the caret, never select-all first"

    focus["state"] = {"typable": True, "document": False, "runtime_id": (5,)}
    asyncio.run(desktop_body.DesktopBody().vision_type("report-7S3X.txt"))
    assert keys == ["CTRL+A"], "a single field still has its whole value set"


def test_a_bound_click_lands_where_the_model_pointed_inside_the_control(monkeypatch) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(desktop_body, "_virtual_screen", lambda: (0, 0, 2560, 1600))
    editor = SimpleNamespace(element_info=SimpleNamespace(rectangle=SimpleNamespace(left=100, top=150, right=1500, bottom=990)))
    # Inside the editor: the model's spot (the blank on one line) is where the caret goes.
    assert desktop_body._point_within_control(editor, 520, 458) == (520, 458)
    # Outside it (stale or misaligned): no point, so the control's own clickable point is used.
    assert desktop_body._point_within_control(editor, 40, 60) is None
    assert desktop_body._point_within_control(editor, 0, 0) is None


def test_focus_on_an_editors_container_is_moved_into_its_one_text_control(monkeypatch) -> None:
    from types import SimpleNamespace

    focused_calls: list[str] = []

    def element(kind: str, name: str):
        return SimpleNamespace(
            element_info=SimpleNamespace(control_type=kind, visible=True, enabled=True),
            set_focus=lambda: focused_calls.append(name),
        )

    page = element("Document", "page")
    container = SimpleNamespace(descendants=lambda: [element("Button", "tab"), page])
    monkeypatch.setattr(desktop_body, "_os_focused_wrapper", lambda: container)
    monkeypatch.setattr(desktop_body, "_focused_state", lambda: {"typable": bool(focused_calls)})
    monkeypatch.setattr(desktop_body.time, "sleep", lambda *_: None)

    assert desktop_body._focus_text_control_within_focus() is True
    assert focused_calls == ["page"]

    # Two text controls inside: which one takes the typing is not known, so nothing is moved.
    focused_calls.clear()
    two = SimpleNamespace(descendants=lambda: [element("Edit", "a"), element("Edit", "b")])
    monkeypatch.setattr(desktop_body, "_os_focused_wrapper", lambda: two)
    assert desktop_body._focus_text_control_within_focus() is False
    assert focused_calls == []


def test_a_type_bound_to_an_editors_container_goes_into_its_text_control(monkeypatch) -> None:
    import asyncio
    from types import SimpleNamespace

    rect = SimpleNamespace(left=100, top=100, right=900, bottom=700)
    pane = SimpleNamespace(element_info=SimpleNamespace(rectangle=rect), set_focus=lambda: None)
    repaired: list[bool] = []
    typed: list[dict] = []

    async def fake_type(self, text, **kwargs):
        typed.append(kwargs)
        return {"ok": True, "action": "type"}

    monkeypatch.setattr(desktop_body, "_require_enabled", lambda: None)
    monkeypatch.setattr(desktop_body, "_resolve_control", lambda selector: pane)
    monkeypatch.setattr(desktop_body, "_publish_pointer", lambda *a, **k: None)
    monkeypatch.setattr(desktop_body, "_remember_target_window", lambda *a: 0)
    monkeypatch.setattr(desktop_body.time, "sleep", lambda *_: None)
    monkeypatch.setattr(desktop_body, "_focus_within", lambda w: True)
    monkeypatch.setattr(desktop_body, "_focused_state", lambda: {"typable": bool(repaired)})

    def repair() -> bool:
        repaired.append(True)
        desktop_body._LAST_FOCUS_REPAIR["result"] = "inside_focus:focused"
        return True

    monkeypatch.setattr(desktop_body, "_focus_text_control_within_focus", repair)
    monkeypatch.setattr(desktop_body.DesktopBody, "vision_type", fake_type)

    result = asyncio.run(desktop_body.DesktopBody().vision_type_control("uia:hwnd=1;rid=2", "WDLQ-8"))
    assert repaired == [True]
    assert result["focus_repair"] == "inside_focus:focused"
    assert typed == [{"at_caret": False, "restore_caret": False, "sensitive": False}]


def test_only_text_controls_count_as_readable_typed_values(monkeypatch) -> None:
    # A title bar or pane exposing some value says nothing about where typed text went.
    monkeypatch.setattr(
        desktop_body,
        "_active_controls",
        lambda: (
            [
                {"tag": "titlebar", "value": "Intake Terminal", "password": False},
                {"tag": "pane", "value": "50", "password": False},
                {"tag": "edit", "value": "Dagny K.", "password": False},
                {"tag": "edit", "value": "hunter2", "password": True},
                {"tag": "combobox", "value": "North Harbor District", "password": False},
            ],
            [],
        ),
    )
    assert desktop_body._readable_active_values() == ["Dagny K.", "North Harbor District"]


def test_a_change_is_located_inside_a_window_box() -> None:
    from PIL import ImageDraw

    before = Image.new("RGB", (1200, 800), (235, 235, 235))
    after = before.copy()
    ImageDraw.Draw(after).text((470, 445), "7Q-DK", fill=(0, 0, 0))
    assert desktop_body._changed_within_box(before, after, (400, 400, 700, 500)) is True
    assert desktop_body._changed_within_box(before, after, (0, 0, 300, 300)) is False
    assert desktop_body._changed_within_box(before, after, None) is False


def test_typing_into_a_field_with_no_readable_value_is_typed_unread(monkeypatch) -> None:
    # A Tab-focused field in an app that exposes no text values (Tk): the window changed, so the
    # keystrokes arrived; the next screenshot is the read-back, not a "no effect" failure.
    monkeypatch.setattr(desktop_body, "_require_enabled", lambda: None)
    monkeypatch.setattr(desktop_body, "_focused_state", lambda: {"typable": False, "value": ""})
    monkeypatch.setattr(desktop_body, "_foreground_window", lambda: (9, "Intake Terminal"))
    monkeypatch.setattr(desktop_body, "_LAST_TARGET_WINDOW", 9)
    monkeypatch.setattr(desktop_body, "_LAST_CLICK_POINT", None)
    monkeypatch.setattr(desktop_body, "_publish_pointer", lambda *a, **k: None)
    monkeypatch.setattr(desktop_body, "_restore_caret_at_last_click", lambda: False)
    monkeypatch.setattr(desktop_body, "_focus_text_control_within_focus", lambda: False)
    monkeypatch.setattr(
        desktop_body,
        "_active_controls",
        lambda: ([{"tag": "titlebar", "value": "Intake Terminal", "password": False}], []),
    )
    frames = iter([Image.new("RGB", (800, 600), (240, 240, 240)), Image.new("RGB", (800, 600), (240, 240, 240))])
    changed_after = Image.new("RGB", (800, 600), (240, 240, 240))
    changed_after.paste((0, 0, 0), (300, 200, 360, 214))

    def capture():
        try:
            return next(frames)
        except StopIteration:
            return changed_after

    shots: list = []

    def recording_capture():
        shot = capture() if not shots else changed_after
        shots.append(shot)
        return shot

    monkeypatch.setattr(desktop_body, "_capture_image", recording_capture)
    monkeypatch.setattr(desktop_body, "_window_capture_box", lambda handle: (100, 100, 700, 500))
    monkeypatch.setattr(desktop_body, "_send_key", lambda *a, **k: None)
    monkeypatch.setattr(desktop_body, "_deliver_text", lambda text, **_: "keystrokes")
    monkeypatch.setattr(desktop_body.time, "sleep", lambda *_: None)

    result = asyncio.run(desktop_body.DesktopBody().vision_type("REF-4471"))
    assert result["ok"] is True
    assert result["value_read_back"] is False

    # Nothing changed in the targeted window: the keystrokes went nowhere.
    shots.clear()
    still = Image.new("RGB", (800, 600), (240, 240, 240))
    monkeypatch.setattr(desktop_body, "_capture_image", lambda: still)
    result = asyncio.run(desktop_body.DesktopBody().vision_type("REF-4471"))
    assert result["ok"] is False
    assert result["failure_reason"] == "desktop_type_had_no_effect"


def test_a_wheel_lands_at_the_named_point_and_is_proven_around_it(monkeypatch) -> None:
    import sys
    import types

    moves: list = []
    wheels: list = []
    fake = types.SimpleNamespace(
        SetCursorPos=lambda point: moves.append(tuple(point)),
        mouse_event=lambda flag, x, y, delta, extra: wheels.append((flag, delta)),
    )
    monkeypatch.setitem(sys.modules, "win32api", fake)
    monkeypatch.setattr(desktop_body, "_require_enabled", lambda: None)
    monkeypatch.setattr(desktop_body, "_publish_pointer", lambda *a, **k: None)
    monkeypatch.setattr(desktop_body, "_virtual_screen", lambda: (0, 0, 1200, 800))
    monkeypatch.setattr(desktop_body.time, "sleep", lambda *_: None)
    base = Image.new("RGB", (1200, 800), (235, 235, 235))
    scrolled = base.copy()
    scrolled.paste((20, 20, 20), (380, 380, 520, 400))  # the list under the pointer moved
    far = base.copy()
    far.paste((20, 20, 20), (1100, 10, 1190, 30))  # a clock ticked elsewhere
    frames = iter([base, scrolled, base, far])
    monkeypatch.setattr(desktop_body, "_capture_image", lambda: next(frames))

    body = desktop_body.DesktopBody()
    result = asyncio.run(body.vision_wheel(450, 390, 0, 300))
    assert moves[-1] == (450, 390)
    assert wheels and wheels[-1][1] == -300
    assert result["scroll_changed"] is True and result["scroll_y"] == 1
    assert result["wheel_point"] == [450, 390]

    result = asyncio.run(body.vision_wheel(450, 390, 0, 300))
    assert result["scroll_changed"] is False, "a change far from the wheel point is not a scroll"
    assert result["surface_changed"] is True


def test_a_scroll_without_a_point_wheels_over_the_targeted_window(monkeypatch) -> None:
    import sys
    import types

    moves: list = []
    fake = types.SimpleNamespace(
        SetCursorPos=lambda point: moves.append(tuple(point)),
        mouse_event=lambda *args: None,
    )
    monkeypatch.setitem(sys.modules, "win32api", fake)
    monkeypatch.setattr(desktop_body, "_require_enabled", lambda: None)
    monkeypatch.setattr(desktop_body, "_publish_pointer", lambda *a, **k: None)
    monkeypatch.setattr(desktop_body, "_virtual_screen", lambda: (0, 0, 1200, 800))
    monkeypatch.setattr(desktop_body, "_LAST_TARGET_WINDOW", 42)
    monkeypatch.setattr(desktop_body, "_window_capture_box", lambda handle: (200, 100, 600, 500) if handle == 42 else None)
    monkeypatch.setattr(desktop_body.time, "sleep", lambda *_: None)
    monkeypatch.setattr(desktop_body, "_capture_image", lambda: Image.new("RGB", (1200, 800)))

    asyncio.run(desktop_body.DesktopBody().vision_scroll(300))
    assert moves == [(400, 300)]


def test_a_hidden_foreground_window_contributes_no_controls(monkeypatch) -> None:
    import sys
    import types

    visible = {"value": False}
    fake = types.SimpleNamespace(
        GetForegroundWindow=lambda: 8063376,
        IsWindowVisible=lambda handle: visible["value"],
    )
    monkeypatch.setitem(sys.modules, "win32gui", fake)
    wrapper = object()
    monkeypatch.setattr(
        desktop_body,
        "_desktop",
        lambda: types.SimpleNamespace(
            window=lambda handle: types.SimpleNamespace(wrapper_object=lambda: wrapper)
        ),
    )
    # A withdrawn app root holding the foreground: its title bar is not on screen.
    assert desktop_body._active_wrapper() is None
    visible["value"] = True
    assert desktop_body._active_wrapper() is wrapper


def test_select_all_settles_before_the_first_character_is_typed(monkeypatch) -> None:
    # Typed at once, the first character races the Ctrl release and arrives as Ctrl+<char>.
    events: list = []
    monkeypatch.setattr(desktop_body, "_require_enabled", lambda: None)
    monkeypatch.setattr(desktop_body, "_focused_state", lambda: {"typable": True, "document": False, "runtime_id": (5,)})
    monkeypatch.setattr(desktop_body, "_foreground_window", lambda: (7, "inbox - File Explorer"))
    monkeypatch.setattr(desktop_body, "_LAST_TARGET_WINDOW", 7)
    monkeypatch.setattr(desktop_body, "_publish_pointer", lambda *a, **k: None)
    monkeypatch.setattr(desktop_body, "_readable_active_values", lambda: [])
    monkeypatch.setattr(desktop_body, "_capture_image", lambda: object())
    monkeypatch.setattr(desktop_body, "_image_changed", lambda a, b: True)
    monkeypatch.setattr(desktop_body, "_window_capture_box", lambda handle: None)
    monkeypatch.setattr(desktop_body, "_send_key", lambda key: events.append(("key", key)))
    monkeypatch.setattr(desktop_body, "_deliver_text", lambda text, **_: events.append(("text", text)) or "keystrokes")
    monkeypatch.setattr(desktop_body.time, "sleep", lambda seconds: events.append(("sleep", seconds)))

    asyncio.run(desktop_body.DesktopBody().vision_type("declaration-CD7-0158.txt"))
    key_at = events.index(("key", "CTRL+A"))
    text_at = events.index(("text", "declaration-CD7-0158.txt"))
    assert any(kind == "sleep" and seconds >= 0.1 for kind, seconds in events[key_at + 1:text_at])


def test_caret_state_carries_the_exact_selection_with_its_spaces(monkeypatch) -> None:
    import pywinauto.uia_defines as uia_defines

    class Range:
        def __init__(self, text: str, kind: str = "selection") -> None:
            self.text = text
            self.kind = kind

        def GetText(self, _limit: int) -> str:
            return self.text

        def Clone(self) -> "Range":
            return Range("Container ID: ________", "line")

        def ExpandToEnclosingUnit(self, _unit: int) -> None:
            pass

        def MoveEndpointByRange(self, *_args) -> None:
            self.text = "Container ID:"  # the prefix up to the selection's start

    selection = types.SimpleNamespace(Length=1, GetElement=lambda index: Range(" ________"))
    text_pattern = types.SimpleNamespace(GetSelection=lambda: selection)
    pattern = types.SimpleNamespace(QueryInterface=lambda iface: text_pattern)
    element = types.SimpleNamespace(CurrentIsPassword=False, CurrentControlType=50030, GetCurrentPattern=lambda pid: pattern)
    dll = types.SimpleNamespace(
        UIA_TextPatternId=10014,
        IUIAutomationTextPattern=object(),
        TextUnit_Line=3,
        TextPatternRangeEndpoint_End=1,
        TextPatternRangeEndpoint_Start=0,
        UIA_DocumentControlTypeId=50030,
    )
    fake = types.SimpleNamespace(UIA_dll=dll, iuia=types.SimpleNamespace(GetFocusedElement=lambda: element))
    monkeypatch.setattr(uia_defines, "IUIA", lambda: fake)

    caret = desktop_body._caret_state()
    assert caret["line_text"] == "Container ID: ________"
    assert caret["column"] == 13
    assert caret["selected_text"] == " ________", "the leading space a type would remove is visible"
    assert caret["selection_length"] == 9


def test_text_line_window_numbers_raw_lines_around_the_caret() -> None:
    view = desktop_body.text_line_window(
        "Shipping manifest\r\nContainer ID:  ________\r\nSeal number: __\r\n", caret_line=2, control=4
    )
    assert view == {
        "control": 4,
        "line_count": 4,
        "lines": ["1|Shipping manifest", "2|Container ID:  ________", "3|Seal number: __", "4|"],
        "caret_line": 2,
    }
    assert desktop_body.text_line_window("A single line has nothing to number") == {}
    assert desktop_body.text_line_window("a\rb")["lines"] == ["1|a", "2|b"], "RichEdit's bare carriage returns"

    long_text = "\n".join(f"Line {number}" for number in range(1, 201))
    centred = desktop_body.text_line_window(long_text, caret_line=120)
    numbers = [int(line.split("|", 1)[0]) for line in centred["lines"]]
    assert len(numbers) == 40 and numbers == list(range(numbers[0], numbers[0] + 40))
    assert 120 in numbers and abs((numbers[0] + numbers[-1]) / 2 - 120) <= 1
    assert centred["line_count"] == 200 and centred["caret_line"] == 120
    top = desktop_body.text_line_window(long_text)
    assert top["lines"][0] == "1|Line 1" and len(top["lines"]) == 40 and "caret_line" not in top
    bottom = desktop_body.text_line_window(long_text, caret_line=999)
    assert bottom["lines"][-1] == "200|Line 200" and bottom["caret_line"] == 200

    wide = desktop_body.text_line_window("x" * 5000 + "\nshort")
    assert wide["lines"] == ["1|" + "x" * 400 + "...[+4600 chars]", "2|short"]
    dense = desktop_body.text_line_window("\n".join("y" * 300 for _ in range(30)))
    assert len(dense["lines"]) == 9, "about 3000 characters of lines at most"


def _focused_document_fakes(monkeypatch, *, text: str, prefix: str, read_only: object = False, password: bool = False,
                            control_type: int = 50030, value_pattern: object = None,
                            ancestors: list | None = None, walkable: bool = True) -> None:
    import pywinauto.uia_defines as uia_defines
    import pywinauto.uia_element_info as uia_element_info

    class Range:
        def __init__(self, content: str) -> None:
            self.content = content

        def GetText(self, _limit: int) -> str:
            return self.content

        def GetAttributeValue(self, _attribute: int) -> object:
            return read_only

        def Clone(self) -> "Range":
            return Range(self.content)

        def MoveEndpointByRange(self, *_args) -> None:
            self.content = prefix  # the document's text up to the selection's start

    selection = types.SimpleNamespace(Length=1, GetElement=lambda index: Range(""))
    text_pattern = types.SimpleNamespace(DocumentRange=Range(text), GetSelection=lambda: selection)
    patterns = {10014: types.SimpleNamespace(QueryInterface=lambda iface: text_pattern)}
    if value_pattern is not None:
        patterns[10002] = types.SimpleNamespace(QueryInterface=lambda iface: value_pattern)
    element = types.SimpleNamespace(
        CurrentIsPassword=password, CurrentControlType=control_type, GetCurrentPattern=lambda pid: patterns.get(pid)
    )
    # The control view's parent chain above the focused element (None past the desktop root).
    chain = [element, *(ancestors or [])]
    parents = {id(child): parent for child, parent in zip(chain, chain[1:])}

    def get_parent(item):
        if not walkable:
            raise RuntimeError("tree walk failed")
        return parents.get(id(item))

    dll = types.SimpleNamespace(
        UIA_TextPatternId=10014,
        UIA_ValuePatternId=10002,
        UIA_IsReadOnlyAttributeId=40015,
        IUIAutomationTextPattern=object(),
        IUIAutomationValuePattern=object(),
        TextPatternRangeEndpoint_End=1,
        TextPatternRangeEndpoint_Start=0,
        UIA_DocumentControlTypeId=50030,
        UIA_EditControlTypeId=50004,
    )
    fake = types.SimpleNamespace(
        UIA_dll=dll,
        iuia=types.SimpleNamespace(
            GetFocusedElement=lambda: element,
            ControlViewWalker=types.SimpleNamespace(GetParentElement=get_parent),
        ),
    )
    monkeypatch.setattr(uia_defines, "IUIA", lambda: fake)
    monkeypatch.setattr(uia_element_info, "UIAElementInfo", lambda raw: types.SimpleNamespace(runtime_id=[42, 7]))


def _uia_ancestor(control_type: int, value: str = "") -> types.SimpleNamespace:
    pattern = types.SimpleNamespace(
        QueryInterface=lambda iface: types.SimpleNamespace(CurrentIsReadOnly=True, CurrentValue=value)
    )
    return types.SimpleNamespace(
        CurrentControlType=control_type, GetCurrentPattern=lambda pid: pattern if pid == 10002 and value else None
    )


def test_focused_text_reading_reads_an_editable_document_whole_with_the_caret_line(monkeypatch) -> None:
    text = "Shipping manifest\rContainer ID: ________\rSeal number: __\r"
    _focused_document_fakes(monkeypatch, text=text, prefix="Shipping manifest\rContainer ID: ")
    assert desktop_body._focused_text_reading() == {"runtime_id": (42, 7), "text": text, "caret_line": 2}

    # A web page's document is read-only text, never an editor's page.
    _focused_document_fakes(monkeypatch, text=text, prefix="", read_only=True)
    assert desktop_body._focused_text_reading() == {}
    writable = types.SimpleNamespace(CurrentIsReadOnly=False, CurrentValue="Shipping manifest")
    read_only_value = types.SimpleNamespace(CurrentIsReadOnly=True, CurrentValue="Shipping manifest")
    _focused_document_fakes(monkeypatch, text=text, prefix="", read_only=object(), value_pattern=read_only_value)
    assert desktop_body._focused_text_reading() == {}
    _focused_document_fakes(monkeypatch, text=text, prefix="", read_only=object(), value_pattern=writable)
    assert desktop_body._focused_text_reading()["text"] == text
    # A password box or a control that is not an Edit/Document is never read.
    _focused_document_fakes(monkeypatch, text=text, prefix="", password=True)
    assert desktop_body._focused_text_reading() == {}
    _focused_document_fakes(monkeypatch, text=text, prefix="", control_type=50000)
    assert desktop_body._focused_text_reading() == {}


def test_focused_text_in_a_browsers_web_content_is_never_read_on_the_desktop(monkeypatch) -> None:
    # A textarea on a web page is a UIA Edit inside the browser's Document (whose value is the page's
    # address); the page's own sensitivity marks are not visible here, so a pasted key or recovery
    # phrase must not be read out line by line.
    phrase = "orbit maple canyon\rsecond-secret-line\r"
    window = _uia_ancestor(50032)
    for address in ("https://example.test/recovery", "file:///C:/notes.html"):
        web_document = _uia_ancestor(50030, address)
        _focused_document_fakes(
            monkeypatch, text=phrase, prefix="", control_type=50004, ancestors=[_uia_ancestor(50026), web_document, window]
        )
        assert desktop_body._focused_text_reading() == {}, address
    # An editable document whose own value is a web address is the web content itself (designMode).
    _focused_document_fakes(
        monkeypatch,
        text=phrase,
        prefix="",
        read_only=object(),
        value_pattern=types.SimpleNamespace(CurrentIsReadOnly=False, CurrentValue="https://example.test/"),
    )
    assert desktop_body._focused_text_reading() == {}
    # A native editor's Edit under its own window (a Document whose value is not an address) is read.
    _focused_document_fakes(
        monkeypatch, text=phrase, prefix="", control_type=50004, ancestors=[_uia_ancestor(50030, "Notes"), window]
    )
    assert desktop_body._focused_text_reading()["text"] == phrase
    # When the tree cannot be walked, the control counts as web content.
    _focused_document_fakes(monkeypatch, text=phrase, prefix="", control_type=50004, walkable=False)
    assert desktop_body._focused_text_reading() == {}


def test_the_focused_document_reaches_the_evidence_as_numbered_lines_never_a_sensitive_one(monkeypatch) -> None:
    import json

    selector = desktop_body._selector(1001, (42, 7))
    document_text = "Shipping manifest\rContainer ID: ________\rSeal number: __\r"
    sentinel = "Container ID: ________"
    controls = [
        {"selector": selector, "tag": "document", "role": "Document", "text": "Text editor", "name": "Text editor",
         "value": "Shipping manifest Container ID: ________ Seal number: __", "box": [10, 10, 600, 400],
         "focused": True, "password": False, "index": 3},
    ]
    reading = {"runtime_id": (42, 7), "text": document_text, "caret_line": 2}
    monkeypatch.setattr(desktop_body, "_require_enabled", lambda: None)
    monkeypatch.setattr(desktop_body, "_capture_image", lambda: Image.new("RGB", (800, 600), "white"))
    monkeypatch.setattr(desktop_body, "_window_inventory", lambda: [{"window_id": "hwnd:1001", "title": "manifest.txt - Notepad", "active": True}])
    monkeypatch.setattr(desktop_body, "_caret_state", lambda: {})
    monkeypatch.setattr(desktop_body, "_focus_identity", lambda: (42, 7))
    monkeypatch.setattr(desktop_body, "_focused_text_reading", lambda: dict(reading))

    def capture(rows: list[dict], body: desktop_body.DesktopBody | None = None) -> dict:
        monkeypatch.setattr(desktop_body, "_active_controls", lambda: ([dict(item) for item in rows], []))
        return asyncio.run((body or desktop_body.DesktopBody()).capture_screenshot())["page_evidence"]

    assert capture(controls)["focused_text_lines"] == {
        "control": 1,  # the id the body's registry gave it (first on a fresh body)
        "line_count": 4,
        "lines": ["1|Shipping manifest", "2|Container ID: ________", "3|Seal number: __", "4|"],
        "caret_line": 2,
    }
    marked = desktop_body.DesktopBody()
    marked._sensitive_selectors.add(selector)
    sensitive = capture(controls, marked)
    assert "focused_text_lines" not in sensitive and sentinel not in json.dumps(sensitive)
    # The same element marked under another window handle (so the inventory row is not redacted): its
    # runtime id still keeps its text out.
    elsewhere = desktop_body.DesktopBody()
    elsewhere._sensitive_selectors.add(desktop_body._selector(2002, (42, 7)))
    assert "focused_text_lines" not in capture(controls, elsewhere)
    assert "focused_text_lines" not in capture([{**controls[0], "password": True, "value": ""}])
    assert "focused_text_lines" not in capture([{**controls[0], "focused": False}])
    assert "focused_text_lines" not in capture([{**controls[0], "tag": "pane", "role": "Pane"}])
    # The element Windows reports focused must be that same inventory control.
    monkeypatch.setattr(desktop_body, "_focused_text_reading", lambda: {**reading, "runtime_id": (9, 9)})
    assert "focused_text_lines" not in capture(controls)
    # A browser window showing a web page (its document reports an address): the page's own
    # sensitivity marks live in the browser, so its focused textarea is never read out here.
    monkeypatch.setattr(desktop_body, "_focused_text_reading", lambda: dict(reading))
    web_page = {"selector": desktop_body._selector(1001, (42, 1)), "tag": "document", "role": "Document",
                "text": "Recovery", "name": "Recovery", "value": "https://example.test/recovery",
                "box": [0, 0, 800, 600], "focused": False, "password": False, "index": 1}
    textarea = {**controls[0], "tag": "edit", "role": "Edit"}
    in_browser = capture([web_page, textarea])
    assert in_browser.get("document_url") == "https://example.test/recovery"
    assert "focused_text_lines" not in in_browser


def _fake_input(monkeypatch) -> list:
    import sys
    import types

    events: list = []
    fake_api = types.SimpleNamespace(
        SetCursorPos=lambda point: events.append(("move", tuple(point))),
        mouse_event=lambda flag, x, y, data, extra: events.append(("mouse", flag)),
        keybd_event=lambda code, scan, flags, extra: events.append(("key_up" if flags & 0x0002 else "key_down", code)),
    )
    fake_con = types.SimpleNamespace(
        MOUSEEVENTF_LEFTDOWN=0x0002,
        MOUSEEVENTF_LEFTUP=0x0004,
        MOUSEEVENTF_RIGHTDOWN=0x0008,
        MOUSEEVENTF_RIGHTUP=0x0010,
        MOUSEEVENTF_MIDDLEDOWN=0x0020,
        MOUSEEVENTF_MIDDLEUP=0x0040,
        MOUSEEVENTF_WHEEL=0x0800,
        MOUSEEVENTF_HWHEEL=0x1000,
        KEYEVENTF_KEYUP=0x0002,
    )
    monkeypatch.setitem(sys.modules, "win32api", fake_api)
    monkeypatch.setitem(sys.modules, "win32con", fake_con)
    monkeypatch.setattr(desktop_body, "_virtual_screen", lambda: (0, 0, 1200, 800))
    monkeypatch.setattr(desktop_body.time, "sleep", lambda seconds: events.append(("sleep", round(float(seconds), 3))))
    return events


def test_a_ctrl_click_holds_ctrl_around_the_whole_gesture(monkeypatch) -> None:
    events = _fake_input(monkeypatch)
    desktop_body._mouse_click(100, 200, clicks=2, modifiers=["ctrl", "shift"])
    keys = [event for event in events if event[0] in {"key_down", "key_up"}]
    clicks = [index for index, event in enumerate(events) if event[0] == "mouse"]
    assert keys == [("key_down", 0x11), ("key_down", 0x10), ("key_up", 0x10), ("key_up", 0x11)]
    assert events.index(("key_down", 0x10)) < clicks[0] and clicks[-1] < events.index(("key_up", 0x10))
    assert [events[index][1] for index in clicks] == [0x0002, 0x0004, 0x0002, 0x0004]


def test_a_press_and_hold_click_keeps_the_button_down(monkeypatch) -> None:
    events = _fake_input(monkeypatch)
    desktop_body._mouse_click(100, 200, hold_ms=800)
    assert [event for event in events if event[0] in {"mouse", "sleep"}] == [("mouse", 0x0002), ("sleep", 0.8), ("mouse", 0x0004)]


def test_a_routed_drag_passes_its_waypoints_and_always_releases(monkeypatch) -> None:
    events = _fake_input(monkeypatch)
    desktop_body._mouse_drag(100, 100, 300, 100, path=[(200, 300)], hold_ms=500, modifiers=["ctrl"])
    moves = [event[1] for event in events if event[0] == "move"]
    assert (200, 300) in moves, "the held pointer passes through the waypoint"
    assert moves[-1] == (300, 100)
    assert events.index(("key_down", 0x11)) < events.index(("mouse", 0x0002))
    assert events.index(("mouse", 0x0002)) < events.index(("sleep", 0.5)) < events.index(("move", moves[1]))
    assert events[-2:] == [("mouse", 0x0004), ("key_up", 0x11)]

    # A drag that fails part way still lets go of the button and the held key.
    events.clear()
    moved = {"count": 0}

    def failing_move(point):
        moved["count"] += 1
        if moved["count"] > 3:
            raise OSError("cursor move refused")
        events.append(("move", tuple(point)))

    import sys

    monkeypatch.setattr(sys.modules["win32api"], "SetCursorPos", failing_move)
    try:
        desktop_body._mouse_drag(100, 100, 300, 100, modifiers=["shift"])
    except OSError:
        pass
    assert events[-2:] == [("mouse", 0x0004), ("key_up", 0x10)]


def test_a_held_key_is_one_press_held_down(monkeypatch) -> None:
    import ctypes

    sent: list = []

    class _User32:
        def SendInput(self, count, array, size):
            for item in list(array)[:count]:
                sent.append(("up" if item.ki.dwFlags & 0x0002 else "down", item.ki.wVk))
            return count

    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: _User32(), raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 0, raising=False)
    sleeps: list = []
    monkeypatch.setattr(desktop_body.time, "sleep", lambda seconds: sleeps.append(seconds))
    desktop_body._send_key("ARROWRIGHT", hold_ms=1200)
    assert sent == [("down", 0x27), ("up", 0x27)]
    assert sleeps == [1.2]


def test_a_modified_wheel_holds_the_key_during_the_wheel(monkeypatch) -> None:
    events = _fake_input(monkeypatch)
    monkeypatch.setattr(desktop_body, "_require_enabled", lambda: None)
    monkeypatch.setattr(desktop_body, "_publish_pointer", lambda *a, **k: None)
    monkeypatch.setattr(desktop_body, "_capture_image", lambda: Image.new("RGB", (1200, 800)))
    asyncio.run(desktop_body.DesktopBody().vision_wheel(450, 390, 0, -300, modifiers=["ctrl"]))
    wheel = events.index(("mouse", 0x0800))
    assert events.index(("key_down", 0x11)) < wheel < events.index(("key_up", 0x11))


def test_desktop_click_receipts_report_focus_change_only_when_it_happened() -> None:
    from app import desktop_body

    assert desktop_body._focus_change(None, None) is None, "unknown stays unknown, never a claimed change"
    assert desktop_body._focus_change((42, 7), (42, 7)) is False
    assert desktop_body._focus_change((42, 7), (42, 9)) is True
    assert desktop_body._focus_change(None, (42, 9)) is True


def test_desktop_wait_yields_the_event_loop_while_it_waits(monkeypatch) -> None:
    # A blocking sleep here froze the extension hub, cancel checks, and heartbeats for the whole wait.
    import asyncio

    from app import desktop_body

    monkeypatch.setattr(desktop_body, "_require_enabled", lambda: None)
    monkeypatch.setattr(desktop_body, "_capture_image", lambda: object())
    monkeypatch.setattr(desktop_body, "_image_changed", lambda before, after: False)
    ticks = []

    async def main():
        async def ticker():
            for _ in range(5):
                ticks.append(1)
                await asyncio.sleep(0.02)

        tick_task = asyncio.ensure_future(ticker())
        result = await desktop_body.DesktopBody().vision_wait(300)
        ticks_during_wait = len(ticks)
        await tick_task
        return result, ticks_during_wait

    result, ticks_during_wait = asyncio.run(main())
    assert result["ok"] is True
    assert ticks_during_wait == 5, "other coroutines ran while the desktop wait was in progress"


def test_a_marked_sensitive_desktop_control_keeps_its_text_out_of_the_evidence(monkeypatch) -> None:
    # The screenshot masked the control's pixels while the inventory and the caret still carried
    # its value as text to the model.
    import json

    from app import desktop_body

    selector = desktop_body._selector(1001, (42, 7))
    sentinel = "S3cret-Relay-7781"
    controls = [{"selector": selector, "tag": "edit", "role": "Edit", "text": "Recovery code", "name": "Recovery code", "value": sentinel, "box": [10, 10, 200, 30]}]
    monkeypatch.setattr(desktop_body, "_require_enabled", lambda: None)
    monkeypatch.setattr(desktop_body, "_capture_image", lambda: Image.new("RGB", (400, 300), (255, 255, 255)))
    monkeypatch.setattr(desktop_body, "_active_controls", lambda: ([dict(item) for item in controls], []))
    monkeypatch.setattr(desktop_body, "_window_inventory", lambda: [{"window_id": 1001, "title": "Vault", "active": True}])
    monkeypatch.setattr(desktop_body, "_caret_state", lambda: {"line_text": sentinel, "selected_text": sentinel, "column": 3, "selection_length": 0})
    monkeypatch.setattr(desktop_body, "_focus_identity", lambda: (42, 7))

    body = desktop_body.DesktopBody()
    body._sensitive_selectors.add(selector)
    shot = asyncio.run(body.capture_screenshot())

    evidence = shot["page_evidence"]
    assert sentinel not in json.dumps(evidence)
    assert evidence["controls"][0]["value_redacted"] is True
    assert evidence["caret"]["text_redacted"] is True and evidence["caret"]["column"] == 3


def test_a_final_desktop_click_never_rebinds_to_a_same_named_control_in_another_window(monkeypatch) -> None:
    from app import desktop_body

    approved = desktop_body._selector(1001, (42, 7))
    clicks: list[tuple[int, int]] = []
    monkeypatch.setattr(desktop_body, "_require_enabled", lambda: None)
    monkeypatch.setattr(desktop_body, "_resolve_control", lambda selector: None)
    body = desktop_body.DesktopBody()

    async def fake_click(self, x, y, **gesture):
        clicks.append((x, y))
        return {"ok": True, "action": "click"}

    monkeypatch.setattr(desktop_body.DesktopBody, "vision_click", fake_click)
    identity = {"target_text": "Submit", "target_role": "Button"}

    monkeypatch.setattr(desktop_body, "_exact_click_target", lambda x, y: {"x": x, "y": y, "clicked_control_name": "Submit", "clicked_control_type": "Button", "top_level_handle": 2002})
    refused = asyncio.run(body.vision_click_control(approved, 50, 60, allow_final_submit=True, expected_target_identity=identity))
    assert refused["ok"] is False and refused["failure_reason"] == "desktop_uia_target_stale"
    assert clicks == []

    monkeypatch.setattr(desktop_body, "_exact_click_target", lambda x, y: {"x": x, "y": y, "clicked_control_name": "Submit", "clicked_control_type": "Button", "top_level_handle": 1001})
    rebound = asyncio.run(body.vision_click_control(approved, 50, 60, allow_final_submit=True, expected_target_identity=identity))
    assert rebound["rebound_by_point"] is True and clicks == [(50, 60)], "the same window's control (a Tk proxy) still rebinds"

    clicks.clear()
    monkeypatch.setattr(desktop_body, "_exact_click_target", lambda x, y: {"x": x, "y": y, "clicked_control_name": "Submit", "clicked_control_type": "Button", "top_level_handle": 2002})
    ordinary = asyncio.run(body.vision_click_control(approved, 50, 60, expected_target_identity=identity))
    assert ordinary["rebound_by_point"] is True, "ordinary clicks keep point rebinding"


def test_desktop_held_keys_yield_the_loop_and_release_everything_on_cancel(monkeypatch) -> None:
    # 200 held presses blocked the event loop for minutes; a cancel must stop the sequence and
    # still release the held key and its modifiers.
    import ctypes

    from app import desktop_body

    sent: list[tuple[str, int]] = []

    class _FakeUser32:
        def SendInput(self, count, array, size):
            for item in array:
                sent.append(("up" if item.ki.dwFlags & 0x0002 else "down", int(item.ki.wVk)))
            return count

    real_windll = ctypes.WinDLL
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kwargs: _FakeUser32() if name == "user32" else real_windll(name, **kwargs))
    ticks: list[int] = []

    async def main():
        async def ticker():
            while True:
                ticks.append(1)
                await asyncio.sleep(0.01)

        tick_task = asyncio.ensure_future(ticker())
        press = asyncio.ensure_future(desktop_body._send_key_async("SHIFT+A", repeat=5, hold_ms=2000))
        # Cancel once the loop has demonstrably ticked during the hold, not after a wall-clock guess:
        # Windows timer granularity makes "5 ticks in 100 ms" flaky under load.
        while len(ticks) < 5:
            await asyncio.sleep(0.005)
        press.cancel()
        try:
            await press
        except asyncio.CancelledError:
            pass
        tick_task.cancel()

    asyncio.run(main())
    assert len(ticks) >= 5, "the loop kept running during the hold"
    downs = [code for kind, code in sent if kind == "down"]
    ups = [code for kind, code in sent if kind == "up"]
    assert sorted(downs) == sorted(ups), "every pressed key was released"
    assert len(downs) == 2, "one SHIFT and one A: the cancel stopped the remaining presses"


class _Rect:
    def __init__(self, left: int, top: int, right: int, bottom: int) -> None:
        self.left, self.top, self.right, self.bottom = left, top, right, bottom


class _Info:
    def __init__(self, runtime_id: tuple, rect: _Rect | None = None, parent=None) -> None:
        self.runtime_id, self.rectangle, self.parent = runtime_id, rect, parent


class _Wrapper:
    def __init__(self, info: _Info, docs: list | None = None) -> None:
        self.element_info, self._docs = info, docs or []

    def descendants(self, control_type: str | None = None) -> list:
        return list(self._docs)


def test_a_background_tabs_document_is_hidden_from_the_inventory() -> None:
    # v3_paperwork from the brain seat: Chrome exposed the v2_type_inferno tab's page as a second
    # "visible" Document; an x,y click on the live page bound to that page's "Mirror buffer".
    live = _Wrapper(_Info((1, 2), _Rect(278, 158, 1910, 1118)))
    stale = _Wrapper(_Info((1, 3), _Rect(278, 27, 1910, 987)))
    window = _Wrapper(_Info((1,), _Rect(270, 0, 1920, 1120)), docs=[stale, live])
    live_node = _Info((1, 2, 9), parent=live.element_info)

    assert desktop_body._hidden_document_ids(window, hit_test=lambda x, y: live_node) == {(1, 3)}
    # An overlay over every probe point confirms nothing, so nothing is hidden (fail open).
    overlay = _Info((7, 7))
    assert desktop_body._hidden_document_ids(window, hit_test=lambda x, y: overlay) == set()
    # One document: nothing to choose between.
    assert desktop_body._hidden_document_ids(_Wrapper(_Info((1,), _Rect(0, 0, 10, 10)), docs=[live])) == set()


def test_a_blind_focus_click_needs_the_control_to_be_what_is_at_its_point() -> None:
    # Review finding 4: when set_focus failed, the body clicked the stale control's centre unchecked.
    control = _Wrapper(_Info((5, 1), _Rect(100, 100, 200, 140)))
    inside = _Info((5, 1, 3), parent=control.element_info)
    dialog = _Info((9, 9))
    assert desktop_body._point_hits_element(150, 120, control, hit_test=lambda x, y: inside) is True
    assert desktop_body._point_hits_element(150, 120, control, hit_test=lambda x, y: dialog) is False

    def gone(x, y):
        raise RuntimeError("element not available")

    # Closure review M4: a UIA walk that fails mid-way is no proof of a hit.
    assert desktop_body._point_hits_element(150, 120, control, hit_test=gone) is False


def test_desktop_operations_run_on_the_native_thread_without_stalling_the_caller() -> None:
    import threading
    import time

    cleaned_up: list[bool] = []

    @desktop_body._on_native_thread
    class _Probe:
        async def where(self) -> str:
            return threading.current_thread().name

        async def nested(self) -> str:
            return await self.where()

        async def block(self, seconds: float) -> str:
            time.sleep(seconds)  # a native call that blocks its thread
            return "done"

        async def hold(self) -> None:
            try:
                await asyncio.sleep(30)
            finally:
                cleaned_up.append(True)

    probe = _Probe()

    async def scenario() -> float:
        assert await probe.where() == "desktop-native"
        assert await probe.nested() == "desktop-native"
        gaps: list[float] = []
        done = asyncio.Event()

        async def ticker() -> None:
            last = time.perf_counter()
            while not done.is_set():
                await asyncio.sleep(0.01)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now

        tick = asyncio.create_task(ticker())
        assert await probe.block(0.4) == "done"
        done.set()
        await tick
        held = asyncio.create_task(probe.hold())
        await asyncio.sleep(0.1)
        held.cancel()
        with pytest.raises(asyncio.CancelledError):
            await held
        for _ in range(50):
            if cleaned_up:
                break
            await asyncio.sleep(0.02)
        return max(gaps)

    longest_gap = asyncio.run(scenario())
    assert longest_gap < 0.2, "the caller's loop kept running while the native thread blocked"
    assert cleaned_up == [True], "cancelling the caller cancelled the native operation"


def test_focus_typability_is_true_false_or_unknown(monkeypatch) -> None:
    # A Tk entry reports its focus as a plain Pane: that is unknown, not "not typable", so a click->type chain
    # is not cut. A Win32 caret in the foreground thread settles it as typable.
    caret = {"present": None, "asked": 0}

    def caret_present():
        caret["asked"] += 1
        return caret["present"]

    monkeypatch.setattr(desktop_body, "_foreground_caret_present", caret_present)
    for kind in ("Edit", "Document", "ComboBox"):
        assert desktop_body._typability(kind) is True
    for kind in ("Button", "CheckBox", "RadioButton", "MenuItem", "TabItem", "ListItem", "Hyperlink", "TreeItem"):
        assert desktop_body._typability(kind) is False
    assert caret["asked"] == 0, "a known control type never needs the caret probe"
    for kind in ("Pane", "Custom", "Window", "Group", ""):
        assert desktop_body._typability(kind) is None
    caret["present"] = False
    assert desktop_body._typability("Pane") is None
    caret["present"] = True
    assert desktop_body._typability("Pane") is True
    assert desktop_body._typability("Button") is False, "a non-text control stays non-text"

    assert desktop_body._typable_receipt({"typable": True, "typability": True}) is True
    assert desktop_body._typable_receipt({"typable": False, "typability": False}) is False
    assert desktop_body._typable_receipt({"typable": False, "typability": None}) is None
    assert desktop_body._typable_receipt({}) is None, "focus that could not be read is unknown"


def test_a_caret_beside_a_container_never_skips_the_focus_repair(monkeypatch) -> None:
    # Notepad can hold focus on the pane around its page while typed characters are dropped. A caret makes
    # the click receipt say typable, but the body's own repair still sees "not a text control" and runs.
    pane = types.SimpleNamespace(
        element_info=types.SimpleNamespace(
            control_type="Pane", class_name="NotepadTextBox", name="", is_password=False, runtime_id=[42, 7],
            rectangle=_Rect(100, 100, 900, 700),
        )
    )
    monkeypatch.setattr(desktop_body, "_os_focused_wrapper", lambda: pane)
    monkeypatch.setattr(desktop_body, "_virtual_screen", lambda: (0, 0, 1600, 900))
    monkeypatch.setattr(desktop_body, "_foreground_caret_present", lambda: True)
    state = desktop_body._focused_state()
    assert state["typability"] is True and state["typable"] is False

    restored: list[bool] = []
    seen = _document_type_harness(monkeypatch, focus=dict(state), paragraphs=[])
    monkeypatch.setattr(desktop_body, "_restore_caret_at_last_click", lambda: restored.append(True) or False)
    asyncio.run(desktop_body.DesktopBody().vision_type("WDLQ-8"))
    assert restored == [True], "focus on a container is repaired before typing"
    assert seen["deliver"] == [("WDLQ-8", {})]


def test_foreground_caret_is_read_from_the_gui_thread_info(monkeypatch) -> None:
    import ctypes

    calls: dict[str, object] = {}

    class _Fn:
        def __init__(self, impl):
            self.impl = impl

        def __call__(self, *args):
            return self.impl(*args)

    def gui_thread_info(thread_id, info_ref):
        calls["thread"] = thread_id
        info = info_ref._obj
        calls["cb"] = info.cbSize
        info.hwndCaret = calls.get("caret_hwnd", 0)
        return 1

    user32 = types.SimpleNamespace(
        GetForegroundWindow=_Fn(lambda: 0x5150),
        GetWindowThreadProcessId=_Fn(lambda hwnd, pid: 4242),
        GetGUIThreadInfo=_Fn(gui_thread_info),
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **k: user32, raising=False)

    calls["caret_hwnd"] = 0x7788
    assert desktop_body._foreground_caret_present() is True
    assert calls["thread"] == 4242 and int(calls["cb"]) > 0
    calls["caret_hwnd"] = 0
    assert desktop_body._foreground_caret_present() is False
    user32.GetForegroundWindow = _Fn(lambda: 0)
    assert desktop_body._foreground_caret_present() is None, "no foreground window: unknown"


def test_a_click_into_a_pane_reported_field_is_unknown_not_untypable(monkeypatch) -> None:
    monkeypatch.setattr(desktop_body, "_require_enabled", lambda: None)
    monkeypatch.setattr(desktop_body, "_capture_image", lambda: object())
    monkeypatch.setattr(desktop_body, "_image_changed", lambda a, b: True)
    monkeypatch.setattr(desktop_body, "_focus_identity", lambda: (1,))
    monkeypatch.setattr(desktop_body, "_window_inventory", lambda: [{"title": "Intake Terminal", "active": True}])
    monkeypatch.setattr(desktop_body, "_exact_click_target", lambda x, y: {"x": x, "y": y})
    monkeypatch.setattr(desktop_body, "_publish_pointer", lambda *a, **k: None)
    monkeypatch.setattr(desktop_body, "_mouse_click", lambda *a, **k: None)
    monkeypatch.setattr(desktop_body, "_remember_target_window", lambda *a: 0)
    monkeypatch.setattr(desktop_body, "_caret_state", lambda: {})
    monkeypatch.setattr(desktop_body.time, "sleep", lambda *_: None)

    monkeypatch.setattr(
        desktop_body, "_focused_state", lambda: {"control_type": "Pane", "typable": False, "typability": None}
    )
    clicked = asyncio.run(desktop_body.DesktopBody().vision_click(300, 200))
    assert clicked["active_typable"] is None
    assert clicked["active_tag"] == "Pane"

    monkeypatch.setattr(
        desktop_body, "_focused_state", lambda: {"control_type": "Button", "typable": False, "typability": False}
    )
    assert asyncio.run(desktop_body.DesktopBody().vision_click(300, 200))["active_typable"] is False


def test_deliver_text_pastes_on_request_and_never_pastes_a_secret(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        desktop_body, "_deliver_text_via_clipboard", lambda text, hold, **_: calls.append(("paste", text)) or True
    )
    monkeypatch.setattr(desktop_body, "_send_unicode", lambda text: calls.append(("keystrokes", text)))

    hold = desktop_body._ClipboardHold()
    assert desktop_body._deliver_text("SL-826275", prefer_paste=True, clipboard=hold) == "paste"
    assert calls == [("paste", "SL-826275")]

    calls.clear()
    long_secret = "x" * (desktop_body._PASTE_THRESHOLD_CHARS + 10)
    hold = desktop_body._ClipboardHold()
    assert desktop_body._deliver_text(long_secret, allow_paste=False, clipboard=hold) == "keystrokes"
    assert desktop_body._deliver_text("hunter2", prefer_paste=True, allow_paste=False, clipboard=hold) == "keystrokes"
    assert calls == [("keystrokes", long_secret), ("keystrokes", "hunter2")], "a secret never reaches the clipboard"
    assert not hold.pending


_CF_UNICODETEXT = 13
_CF_DIB = 8
_CF_HDROP = 15
_HTML_FORMAT = 0xC100
# The ids the fake clipboard registers the history, sync, and monitor exclusion formats under.
_EXCLUSION_FORMAT_IDS = {name: 0xC001 + index for index, name in enumerate(desktop_body._CLIPBOARD_EXCLUSION_FORMATS)}


def _fake_clipboard(monkeypatch, board: dict) -> dict:
    """win32clipboard over ``board`` ({format: data}, in insertion order). Returns the exclusion formats' ids."""
    formats = dict(_EXCLUSION_FORMAT_IDS)

    def set_data(fmt, data):
        board[fmt] = data

    def enum_formats(fmt):
        keys = list(board)
        index = keys.index(fmt) + 1 if fmt else 0
        return keys[index] if index < len(keys) else 0

    fake_clipboard = types.SimpleNamespace(
        OpenClipboard=lambda hwnd: None,
        CloseClipboard=lambda: None,
        IsClipboardFormatAvailable=lambda fmt: fmt in board,
        GetClipboardData=lambda fmt: board[fmt],
        EmptyClipboard=board.clear,
        SetClipboardData=set_data,
        RegisterClipboardFormat=lambda name: formats[name],
        EnumClipboardFormats=enum_formats,
    )
    monkeypatch.setitem(sys.modules, "win32clipboard", fake_clipboard)
    monkeypatch.setitem(sys.modules, "win32con", types.SimpleNamespace(CF_UNICODETEXT=_CF_UNICODETEXT))
    monkeypatch.setattr(desktop_body.time, "sleep", lambda *_: None)
    return formats


def test_clipboard_paste_holds_the_users_text_and_puts_it_back_out_of_clipboard_history(monkeypatch) -> None:
    board: dict[object, object] = {_CF_UNICODETEXT: "the user's own clipboard"}
    formats = _fake_clipboard(monkeypatch, board)
    pasted: list[tuple[str, dict]] = []
    monkeypatch.setattr(desktop_body, "_send_key", lambda key: pasted.append((key, dict(board))))

    hold = desktop_body._ClipboardHold()
    assert desktop_body._deliver_text_via_clipboard("SL-826275", hold) is True
    key, at_paste = pasted[0]
    assert key == "CTRL+V"
    assert at_paste[_CF_UNICODETEXT] == "SL-826275"
    for fmt in formats.values():
        assert at_paste[fmt] == b"\x00\x00\x00\x00", "the transient value is excluded from history and sync"
    assert board[_CF_UNICODETEXT] == "SL-826275", "nothing goes back while the paste may still be read"
    assert hold.pending

    # The read-back saw the text land: the user's text goes back, marked out of history, sync, and monitors too
    # (a password manager's copy carries those marks; a plain write would push it into Win+V and the cloud).
    hold.settle(landed=True)
    marks = {fmt: b"\x00\x00\x00\x00" for fmt in formats.values()}
    assert board == {_CF_UNICODETEXT: "the user's own clipboard", **marks}
    assert not hold.pending
    hold.settle(landed=False)
    assert board[_CF_UNICODETEXT] == "the user's own clipboard", "a hold is settled once"

    # Not seen to land: the clipboard is left empty, never the user's value while a paste may be pending.
    board.clear()
    board[_CF_UNICODETEXT] = "the user's own clipboard"
    hold = desktop_body._ClipboardHold()
    desktop_body._deliver_text_via_clipboard("SL-826275", hold)
    hold.settle(landed=False)
    assert board == {}


def test_a_failed_clipboard_write_restores_at_once_and_a_failed_ctrl_v_waits_for_the_read_back(monkeypatch) -> None:
    board: dict[object, object] = {_CF_UNICODETEXT: "the user's own clipboard"}
    _fake_clipboard(monkeypatch, board)
    keys: list[str] = []
    monkeypatch.setattr(desktop_body, "_send_key", keys.append)
    real_set = sys.modules["win32clipboard"].SetClipboardData

    def refuse_the_run_text(fmt, data):
        if data == "SL-826275":
            raise OSError("SetClipboardData failed")
        real_set(fmt, data)

    monkeypatch.setattr(sys.modules["win32clipboard"], "SetClipboardData", refuse_the_run_text)
    hold = desktop_body._ClipboardHold()
    with pytest.raises(OSError):
        desktop_body._deliver_text_via_clipboard("SL-826275", hold)
    assert keys == [] and not hold.pending, "nothing was pasted"
    assert board[_CF_UNICODETEXT] == "the user's own clipboard", "the user's text went straight back"

    monkeypatch.setattr(sys.modules["win32clipboard"], "SetClipboardData", real_set)

    def ctrl_v_fails(key: str) -> None:
        raise OSError("SendInput did not inject Ctrl+V")

    monkeypatch.setattr(desktop_body, "_send_key", ctrl_v_fails)
    typed: list[str] = []
    monkeypatch.setattr(desktop_body, "_send_unicode", typed.append)
    hold = desktop_body._ClipboardHold()
    assert desktop_body._deliver_text("SL-826275", prefer_paste=True, clipboard=hold) == "paste_unconfirmed"
    assert typed == [], "never typed on top of a paste that may have landed"
    assert hold.pending and board[_CF_UNICODETEXT] == "SL-826275"


def test_a_short_paste_never_wipes_an_image_or_files_on_the_clipboard(monkeypatch) -> None:
    keys: list[str] = []
    typed: list[str] = []
    monkeypatch.setattr(desktop_body, "_send_key", keys.append)
    monkeypatch.setattr(desktop_body, "_send_unicode", typed.append)

    for content in (
        {_CF_DIB: b"image"},
        {_CF_HDROP: b"files"},
        {_CF_UNICODETEXT: "caption", _CF_DIB: b"image"},
        {_HTML_FORMAT: b"an app's own object, no text"},
    ):
        board = dict(content)
        _fake_clipboard(monkeypatch, board)
        hold = desktop_body._ClipboardHold()
        assert desktop_body._deliver_text("SL-826275", prefer_paste=True, clipboard=hold) == "keystrokes"
        assert board == content, "the clipboard is untouched"
        assert not hold.pending
    assert keys == [] and typed == ["SL-826275"] * 4

    # Text (with the formats an app registers beside it) or nothing: a short value is pasted.
    for content in ({}, {_CF_UNICODETEXT: "notes", _HTML_FORMAT: b"<b>notes</b>"}):
        board = dict(content)
        _fake_clipboard(monkeypatch, board)
        hold = desktop_body._ClipboardHold()
        assert desktop_body._deliver_text("SL-826275", prefer_paste=True, clipboard=hold) == "paste"
        hold.settle(landed=True)
        assert board.get(_CF_UNICODETEXT) == content.get(_CF_UNICODETEXT)

    # Text that cannot be read is not wiped for a short value either.
    board = {_CF_UNICODETEXT: "unreadable"}
    _fake_clipboard(monkeypatch, board)

    def unreadable(fmt):
        raise OSError("delayed rendering failed")

    monkeypatch.setattr(sys.modules["win32clipboard"], "GetClipboardData", unreadable)
    hold = desktop_body._ClipboardHold()
    assert desktop_body._deliver_text("SL-826275", prefer_paste=True, clipboard=hold) == "keystrokes"
    assert board == {_CF_UNICODETEXT: "unreadable"} and not hold.pending

    # A long value is still pasted over an image: the documented cost of a long paste.
    board = {_CF_DIB: b"image"}
    _fake_clipboard(monkeypatch, board)
    long_text = "x" * desktop_body._PASTE_THRESHOLD_CHARS
    assert desktop_body._deliver_text(long_text, clipboard=desktop_body._ClipboardHold()) == "paste"


def _document_type_harness(monkeypatch, *, focus: dict, paragraphs: list, values: dict | None = None) -> dict:
    """vision_type with every native call stubbed; _caret_paragraph returns ``paragraphs`` in order."""
    seen: dict = {"deliver": [], "paragraph_reads": 0, "clipboard": []}
    values = values if values is not None else {"before": [], "after": []}
    reads = {"n": 0}

    def readable() -> list:
        reads["n"] += 1
        return list(values["before"] if reads["n"] % 2 == 1 else values["after"])

    def paragraph() -> dict:
        seen["paragraph_reads"] += 1
        return paragraphs.pop(0) if paragraphs else {}

    def deliver(text: str, **kwargs: object) -> str:
        seen["clipboard"].append(kwargs.pop("clipboard", None))
        seen["deliver"].append((text, kwargs))
        return "paste" if kwargs.get("prefer_paste") else "keystrokes"

    monkeypatch.setattr(desktop_body, "_require_enabled", lambda: None)
    monkeypatch.setattr(desktop_body, "_focused_state", lambda: dict(focus))
    monkeypatch.setattr(desktop_body, "_foreground_window", lambda: (7, "manifest.txt - Notepad"))
    monkeypatch.setattr(desktop_body, "_LAST_TARGET_WINDOW", 7)
    monkeypatch.setattr(desktop_body, "_LAST_CLICK_POINT", None)
    monkeypatch.setattr(desktop_body, "_publish_pointer", lambda *a, **k: None)
    monkeypatch.setattr(desktop_body, "_restore_caret_at_last_click", lambda: False)
    monkeypatch.setattr(desktop_body, "_focus_text_control_within_focus", lambda: False)
    monkeypatch.setattr(desktop_body, "_readable_active_values", readable)
    monkeypatch.setattr(desktop_body, "_capture_image", lambda: object())
    monkeypatch.setattr(desktop_body, "_image_changed", lambda a, b: True)
    monkeypatch.setattr(desktop_body, "_window_capture_box", lambda handle: None)
    monkeypatch.setattr(desktop_body, "_send_key", lambda key: None)
    monkeypatch.setattr(desktop_body, "_caret_paragraph", paragraph)
    monkeypatch.setattr(desktop_body, "_deliver_text", deliver)
    monkeypatch.setattr(desktop_body.time, "sleep", lambda *_: None)
    return seen


def test_typing_into_a_document_is_pasted_and_a_secret_is_keyed(monkeypatch) -> None:
    document = {"typable": True, "document": True, "runtime_id": (4,), "center": [800, 500]}
    line = {"text": "Seal number: ________", "column": 13, "selected": "________"}
    done = {"text": "Seal number: SL-826275", "column": 22, "selected": ""}
    seen = _document_type_harness(monkeypatch, focus=document, paragraphs=[dict(line), dict(done)])

    result = asyncio.run(desktop_body.DesktopBody().vision_type("SL-826275"))
    assert seen["deliver"] == [("SL-826275", {"prefer_paste": True})]
    assert isinstance(seen["clipboard"][0], desktop_body._ClipboardHold)
    assert result["text_delivery"] == "paste"

    # The run marked the value secret: keystrokes only, and no line of it is read into the receipt.
    seen = _document_type_harness(monkeypatch, focus=document, paragraphs=[dict(line), dict(done)])
    secret = asyncio.run(desktop_body.DesktopBody().vision_type("hunter2", sensitive=True))
    assert seen["deliver"] == [("hunter2", {"allow_paste": False})]
    assert seen["clipboard"] == [None]
    assert seen["paragraph_reads"] == 0
    assert "edited_line_after" not in secret

    # A password box is a secret even when the run did not say so.
    seen = _document_type_harness(monkeypatch, focus={"typable": True, "password": True, "runtime_id": (5,)}, paragraphs=[])
    asyncio.run(desktop_body.DesktopBody().vision_type("hunter2"))
    assert seen["deliver"] == [("hunter2", {"allow_paste": False})]
    assert seen["clipboard"] == [None]

    # A single-line field keeps real keystrokes.
    seen = _document_type_harness(monkeypatch, focus={"typable": True, "document": False, "runtime_id": (6,)}, paragraphs=[])
    asyncio.run(desktop_body.DesktopBody().vision_type("report-7S3X.txt"))
    assert seen["deliver"] == [("report-7S3X.txt", {})]


def test_a_control_marked_sensitive_is_typed_as_a_secret(monkeypatch) -> None:
    rect = types.SimpleNamespace(left=100, top=100, right=300, bottom=130)
    wrapper = types.SimpleNamespace(element_info=types.SimpleNamespace(rectangle=rect), set_focus=lambda: None)
    typed: list[dict] = []

    async def fake_type(self, text, **kwargs):
        typed.append(kwargs)
        return {"ok": True, "action": "type"}

    monkeypatch.setattr(desktop_body, "_require_enabled", lambda: None)
    monkeypatch.setattr(desktop_body, "_resolve_control", lambda selector: wrapper)
    monkeypatch.setattr(desktop_body, "_publish_pointer", lambda *a, **k: None)
    monkeypatch.setattr(desktop_body, "_remember_target_window", lambda *a: 0)
    monkeypatch.setattr(desktop_body, "_focus_within", lambda w: True)
    monkeypatch.setattr(desktop_body, "_focused_state", lambda: {"typable": True})
    monkeypatch.setattr(desktop_body.time, "sleep", lambda *_: None)
    monkeypatch.setattr(desktop_body.DesktopBody, "vision_type", fake_type)

    body = desktop_body.DesktopBody()
    body._sensitive_selectors.add("uia:hwnd=1;rid=9")
    asyncio.run(body.vision_type_control("uia:hwnd=1;rid=9", "hunter2"))
    asyncio.run(body.vision_type_control("uia:hwnd=1;rid=2", "WDLQ-8", sensitive=True))
    asyncio.run(body.vision_type_control("uia:hwnd=1;rid=2", "WDLQ-8"))
    assert [item["sensitive"] for item in typed] == [True, True, False]


def test_paragraph_edit_is_the_line_with_exactly_the_selection_replaced() -> None:
    blank = {"text": "Seal number: ________", "column": 13, "selected": "________"}
    exact = desktop_body._paragraph_edit(blank, {"text": "Seal number: SL-826275"}, "SL-826275")
    assert exact["matches"] is True and exact["expected"] == "Seal number: SL-826275"

    # A leftover of the blank beside the text is not the intended line.
    leftover = desktop_body._paragraph_edit(blank, {"text": "Seal number: _SL-826275"}, "SL-826275")
    assert leftover["matches"] is False
    assert leftover["before"] == "Seal number: ________" and leftover["after"] == "Seal number: _SL-826275"

    garbled = desktop_body._paragraph_edit(blank, {"text": "Seal bbber:LLL-826275"}, "SL-826275")
    assert garbled["matches"] is False

    # A caret with nothing selected inserts at the column; spaces are kept exactly.
    caret = {"text": "Name:  ", "column": 6, "selected": ""}
    assert desktop_body._paragraph_edit(caret, {"text": "Name: Ada "}, "Ada")["matches"] is True

    # Not judged by the paragraph: a selection or text crossing a line break, a reading that does not add up.
    assert desktop_body._paragraph_edit({"text": "a", "column": 0, "selected": "a\r"}, {"text": "x"}, "x") == {}
    assert desktop_body._paragraph_edit(blank, {"text": "x"}, "two\nlines") == {}
    assert desktop_body._paragraph_edit({"text": "abc", "column": 2, "selected": "zz"}, {"text": "x"}, "x") == {}
    assert desktop_body._paragraph_edit({}, {"text": "x"}, "x") == {}


def test_caret_paragraph_reads_the_raw_paragraph_with_its_spaces(monkeypatch) -> None:
    import pywinauto.uia_defines as uia_defines

    units: list[int] = []

    class Range:
        def __init__(self, text: str) -> None:
            self.text = text

        def GetText(self, _limit: int) -> str:
            return self.text

        def Clone(self) -> "Range":
            return Range(self.text)

        def ExpandToEnclosingUnit(self, unit: int) -> None:
            units.append(unit)
            self.text = "Seal number:  ________\r"

        def MoveEndpointByRange(self, *_args) -> None:
            self.text = "Seal number:  "

    selection = types.SimpleNamespace(Length=1, GetElement=lambda index: Range("________"))
    text_pattern = types.SimpleNamespace(GetSelection=lambda: selection)
    pattern = types.SimpleNamespace(QueryInterface=lambda iface: text_pattern)
    element = types.SimpleNamespace(CurrentIsPassword=False, GetCurrentPattern=lambda pid: pattern)
    dll = types.SimpleNamespace(
        UIA_TextPatternId=10014,
        IUIAutomationTextPattern=object(),
        TextUnit_Paragraph=4,
        TextPatternRangeEndpoint_End=1,
        TextPatternRangeEndpoint_Start=0,
    )
    fake = types.SimpleNamespace(UIA_dll=dll, iuia=types.SimpleNamespace(GetFocusedElement=lambda: element))
    monkeypatch.setattr(uia_defines, "IUIA", lambda: fake)

    paragraph = desktop_body._caret_paragraph()
    assert units == [4], "a paragraph, so word wrap cannot reflow what is compared"
    assert paragraph == {"text": "Seal number:  ________", "column": 14, "selected": "________"}

    element.CurrentIsPassword = True
    assert desktop_body._caret_paragraph() == {}, "a password field's text is never read"


def test_an_at_caret_type_is_matched_only_when_the_line_is_exactly_right(monkeypatch) -> None:
    document = {"typable": True, "document": True, "runtime_id": (4,), "center": [800, 500]}
    blank = {"text": "Seal number: ________", "column": 13, "selected": "________"}
    # The document's head (all its read-back exposes) does not reach the edited line.
    head = ["Shipping manifest " * 14]

    _document_type_harness(
        monkeypatch,
        focus=document,
        paragraphs=[dict(blank), {"text": "Seal number: SL-826275"}],
        values={"before": head, "after": head},
    )
    exact = asyncio.run(desktop_body.DesktopBody().vision_type("SL-826275"))
    assert exact["ok"] is True and exact["active_value_matches_expected"] is True
    assert exact["value_read_back"] is True
    assert exact["edited_line_before"] == "Seal number: ________"
    assert exact["edited_line_after"] == "Seal number: SL-826275"

    _document_type_harness(
        monkeypatch,
        focus=document,
        paragraphs=[dict(blank), {"text": "Seal number: _SL-826275"}],
        values={"before": head, "after": head},
    )
    leftover = asyncio.run(desktop_body.DesktopBody().vision_type("SL-826275"))
    assert leftover["ok"] is True, "the text arrived"
    assert leftover["active_value_matches_expected"] is False, "but a leftover of the blank is not a match"
    assert leftover["edited_line_after"] == "Seal number: _SL-826275"


def test_the_paragraph_is_only_compared_within_the_field_the_caret_was_in(monkeypatch) -> None:
    blank = {"text": "Seal number: ________", "column": 13, "selected": "________"}
    seen = _document_type_harness(
        monkeypatch,
        focus={},
        paragraphs=[dict(blank), {"text": "Other field: something"}],
        values={"before": ["Seal number: ________"], "after": ["Seal number: SL-826275"]},
    )
    states = iter([
        {"typable": True, "document": True, "runtime_id": (4,)},
        {"typable": True, "document": True, "runtime_id": (9,)},
    ])
    monkeypatch.setattr(desktop_body, "_focused_state", lambda: next(states))
    result = asyncio.run(desktop_body.DesktopBody().vision_type("SL-826275"))
    assert seen["paragraph_reads"] == 1, "focus moved: the other field's paragraph is not read as this edit"
    assert "edited_line_after" not in result
    assert result["ok"] is True and result["active_value_matches_expected"] is True, "the read-back rule decides"


def test_garbled_typing_is_reported_with_what_landed_never_as_no_effect(monkeypatch) -> None:
    document = {"typable": True, "document": True, "runtime_id": (4,), "center": [800, 500]}
    blank = {"text": "Seal number: ________", "column": 13, "selected": "________"}
    _document_type_harness(
        monkeypatch,
        focus=document,
        paragraphs=[blank, {"text": "Seal bbber:LLL-826275"}],
        values={"before": ["Seal number: ________"], "after": ["Seal bbber:LLL-826275"]},
    )
    # Typed key by key (the clipboard held an image); a paste that lands wrong is covered below.
    monkeypatch.setattr(desktop_body, "_deliver_text", lambda text, **_: "keystrokes")
    result = asyncio.run(desktop_body.DesktopBody().vision_type("SL-826275"))
    assert result["ok"] is False
    assert result["failure_reason"] == "typed_text_corrupted"
    assert result["edited_line_before"] == "Seal number: ________"
    assert result["edited_line_after"] == "Seal bbber:LLL-826275"
    assert "no text area had the caret" not in result["failure_detail"]

    # A plain field whose value changed to something other than the typed text: the same verdict, with the
    # changed value before and after, instead of "the keystrokes went nowhere".
    _document_type_harness(
        monkeypatch,
        focus={"typable": True, "document": False, "runtime_id": (6,)},
        paragraphs=[],
        values={"before": ["Dagny"], "after": ["Dgany K"]},
    )
    field = asyncio.run(desktop_body.DesktopBody().vision_type("Dagny K."))
    assert field["ok"] is False and field["failure_reason"] == "typed_text_corrupted"
    assert (field["edited_line_before"], field["edited_line_after"]) == ("Dagny", "Dgany K")

    # Nothing changed in any readable text control: still the honest "no effect".
    _document_type_harness(
        monkeypatch,
        focus={"typable": True, "document": False, "runtime_id": (6,)},
        paragraphs=[],
        values={"before": ["Dagny"], "after": ["Dagny"]},
    )
    nothing = asyncio.run(desktop_body.DesktopBody().vision_type("Dagny K."))
    assert nothing["failure_reason"] == "desktop_type_had_no_effect"


def test_edit_excerpts_keep_short_lines_whole_and_window_long_ones_on_the_change() -> None:
    assert desktop_body._edit_excerpts("a b", "a  c") == ("a b", "a  c")
    before = "x" * 300 + "BLANK" + "y" * 300
    after = "x" * 300 + "VALUE" + "y" * 300
    cut_before, cut_after = desktop_body._edit_excerpts(before, after)
    assert "BLANK" in cut_before and "VALUE" in cut_after
    assert cut_before.startswith("...") and cut_before.endswith("...")
    assert len(cut_after) <= 166


def _pasted_document_type(
    monkeypatch, after: dict, *, board: dict | None = None, ctrl_v_fails: bool = False, capture_fails: bool = False
):
    """A real paste (clipboard faked over ``board``) into a document, read back by ``after``; returns the
    receipt, the clipboard text at each paragraph read, the clipboard afterwards, and anything typed key by key."""
    deliver = desktop_body._deliver_text
    document = {"typable": True, "document": True, "runtime_id": (4,), "center": [800, 500]}
    blank = {"text": "Seal number: ________", "column": 13, "selected": "________"}
    _document_type_harness(monkeypatch, focus=document, paragraphs=[])
    board = board if board is not None else {_CF_UNICODETEXT: "pw-from-the-password-manager"}
    _fake_clipboard(monkeypatch, board)
    monkeypatch.setattr(desktop_body, "_deliver_text", deliver)
    typed: list[str] = []
    monkeypatch.setattr(desktop_body, "_send_unicode", typed.append)

    def send_key(key: str) -> None:
        if ctrl_v_fails and key == "CTRL+V":
            raise OSError("SendInput did not inject Ctrl+V")

    monkeypatch.setattr(desktop_body, "_send_key", send_key)
    on_clipboard: list[object] = []
    paragraphs = [dict(blank), dict(after)]

    def paragraph() -> dict:
        on_clipboard.append(board.get(_CF_UNICODETEXT))
        return paragraphs.pop(0)

    monkeypatch.setattr(desktop_body, "_caret_paragraph", paragraph)
    captures = {"n": 0}

    def capture() -> object:
        captures["n"] += 1
        if capture_fails and captures["n"] > 1:
            raise RuntimeError("screen capture failed")
        return object()

    monkeypatch.setattr(desktop_body, "_capture_image", capture)
    result = asyncio.run(desktop_body.DesktopBody().vision_type("SL-826275"))
    return result, on_clipboard, board, typed


def test_the_users_clipboard_goes_back_only_after_the_read_back_saw_the_paste_land(monkeypatch) -> None:
    result, on_clipboard, board, typed = _pasted_document_type(monkeypatch, {"text": "Seal number: SL-826275"})
    assert result["ok"] is True and result["text_delivery"] == "paste" and typed == []
    assert on_clipboard[-1] == "SL-826275", "while the paste is read back, the user's value is not on the clipboard"
    assert board[_CF_UNICODETEXT] == "pw-from-the-password-manager", "then it goes back"
    assert all(board[fmt] == b"\x00\x00\x00\x00" for fmt in _EXCLUSION_FORMAT_IDS.values()), "out of history and sync"

    # The text is not seen (nothing changed, or no read-back): the clipboard is left empty, so a paste the app
    # has yet to handle gets nothing rather than the user's value.
    result, _, board, _ = _pasted_document_type(monkeypatch, {"text": "Seal number: ________"})
    assert board == {}


def test_a_paste_that_did_not_land_as_intended_does_not_forward_the_line(monkeypatch) -> None:
    landed_wrong = {"text": "Seal number: ________pw-from-the-password-manager"}
    result, _, board, _ = _pasted_document_type(monkeypatch, landed_wrong)
    assert result["ok"] is False and result["failure_reason"] == "typed_text_corrupted"
    assert "pasted content did not land as intended" in result["failure_detail"]
    assert "edited_line_before" not in result and "edited_line_after" not in result
    assert "pw-from-the-password-manager" not in repr(result)
    assert board == {}

    # A plain field whose value changed to something else after a long paste: the same, no changed value.
    deliver = desktop_body._deliver_text
    _document_type_harness(
        monkeypatch,
        focus={"typable": True, "document": False, "runtime_id": (6,)},
        paragraphs=[],
        values={"before": ["Dagny"], "after": ["pw-from-the-password-manager"]},
    )
    board = {_CF_UNICODETEXT: "pw-from-the-password-manager"}
    _fake_clipboard(monkeypatch, board)
    monkeypatch.setattr(desktop_body, "_deliver_text", deliver)
    field = asyncio.run(desktop_body.DesktopBody().vision_type("x" * desktop_body._PASTE_THRESHOLD_CHARS))
    assert field["failure_reason"] == "typed_text_corrupted" and field["text_delivery"] == "paste"
    assert "edited_line_after" not in field and "pw-from-the-password-manager" not in repr(field)
    assert board == {}


def test_a_failed_ctrl_v_is_never_retyped_and_the_clipboard_is_settled_even_on_error(monkeypatch) -> None:
    result, _, board, typed = _pasted_document_type(
        monkeypatch, {"text": "Seal number: ________"}, ctrl_v_fails=True
    )
    assert result["text_delivery"] == "paste_unconfirmed"
    assert typed == [], "the paste may have landed: typing too could put the text in twice"
    assert board == {}, "the user's value is not put back while the paste may be pending"

    # The paste landed after all: the read-back says so and the user's text goes back.
    result, _, board, typed = _pasted_document_type(
        monkeypatch, {"text": "Seal number: SL-826275"}, ctrl_v_fails=True
    )
    assert result["ok"] is True and typed == []
    assert board[_CF_UNICODETEXT] == "pw-from-the-password-manager"

    # The read-back itself failed: the clipboard is still settled (left empty).
    board = {_CF_UNICODETEXT: "pw-from-the-password-manager"}
    with pytest.raises(RuntimeError):
        _pasted_document_type(monkeypatch, {"text": "Seal number: SL-826275"}, board=board, capture_fails=True)
    assert board == {}


def test_window_lines_mark_windows_open_before_the_task_and_the_active_browser_address(monkeypatch) -> None:
    # v3_incident run 1648: a Responder Portal window left from a run hours earlier (its harness gone)
    # looked just like the one the task opens; its address was crowded out of the control inventory.
    windows = [
        {"window_id": "hwnd:1", "title": "Responder Portal - Google Chrome", "pid": 9, "active": True, "box": [0, 0, 800, 600]},
        {"window_id": "hwnd:2", "title": "Incident Console", "pid": 10, "active": False, "box": [0, 0, 400, 300]},
    ]
    monkeypatch.setattr(desktop_body, "_window_inventory", lambda: [dict(item) for item in windows])
    controls = [
        {"tag": "button", "value": ""},
        {"tag": "document", "value": "http://127.0.0.1:50373/eval/bench/v3_incident_portal?sid=a95891"},
    ]
    assert desktop_body._document_url(controls) == "http://127.0.0.1:50373/eval/bench/v3_incident_portal?sid=a95891"
    assert desktop_body._document_url([{"tag": "document", "value": "Untitled"}]) == ""
    assert desktop_body.window_ids_snapshot() == frozenset({"hwnd:1", "hwnd:2"})


def test_desktop_capture_lines_say_which_windows_predate_the_task(monkeypatch: pytest.MonkeyPatch) -> None:
    controls = [
        {"selector": "uia:hwnd=1;rid=1", "tag": "button", "role": "Button", "name": "Minimize", "value": "", "box": [0, 0, 10, 10]},
        {"selector": "uia:hwnd=1;rid=2", "tag": "document", "role": "Document", "name": "Responder Portal",
         "value": "http://127.0.0.1:50373/eval/bench/v3_incident_portal?sid=a95891", "box": [0, 40, 800, 560]},
    ]
    windows = [
        {"window_id": "hwnd:1", "title": "Responder Portal - Google Chrome", "pid": 9, "active": True, "box": [0, 0, 800, 600]},
        {"window_id": "hwnd:2", "title": "Incident Console", "pid": 10, "active": False, "box": [0, 0, 400, 300]},
    ]
    monkeypatch.setattr(desktop_body, "_configured", lambda: True)
    monkeypatch.setattr(desktop_body, "_capture_image", lambda: Image.new("RGB", (800, 600), "white"))
    monkeypatch.setattr(desktop_body, "_active_controls", lambda: (controls, []))
    monkeypatch.setattr(desktop_body, "_window_inventory", lambda: [dict(item) for item in windows])
    monkeypatch.setattr(desktop_body, "_application_inventory", lambda: ([], {}))
    desktop_body.set_runtime_enabled(True)

    shot = asyncio.run(desktop_body.DesktopBody(windows_before_task=frozenset({"hwnd:1"})).capture_screenshot())
    text = shot["page_evidence"]["viewport_text"]
    assert "[hwnd:1] Responder Portal - Google Chrome (active) (open before this task) at http://127.0.0.1:50373/" in text
    assert "[hwnd:2] Incident Console" in text and "hwnd:2] Incident Console (open before" not in text
    assert shot["page_evidence"]["document_url"].startswith("http://127.0.0.1:50373/")
    # Without a start-of-run snapshot nothing is marked.
    plain = asyncio.run(desktop_body.DesktopBody().capture_screenshot())
    assert "open before this task" not in plain["page_evidence"]["viewport_text"]


def test_browser_internal_pages_count_as_browser_documents() -> None:
    for address in ("chrome://newtab/", "about:blank", "chrome-extension://abc/sidepanel.html", "edge://settings"):
        assert desktop_body._document_url([{"tag": "document", "value": address}]) == address
    assert desktop_body._document_url([{"tag": "document", "value": "Untitled - Notepad"}]) == ""


def test_the_desktop_itself_in_front_gets_no_window_box(monkeypatch: pytest.MonkeyPatch) -> None:
    windows = [{"window_id": "hwnd:65896", "title": "Program Manager", "pid": 1, "active": True, "box": [0, 0, 1920, 1200]}]
    monkeypatch.setattr(desktop_body, "_configured", lambda: True)
    monkeypatch.setattr(desktop_body, "_capture_image", lambda: Image.new("RGB", (800, 600), "white"))
    monkeypatch.setattr(desktop_body, "_active_controls", lambda: ([], []))
    monkeypatch.setattr(desktop_body, "_window_inventory", lambda: [dict(item) for item in windows])
    monkeypatch.setattr(desktop_body, "_application_inventory", lambda: ([], {}))
    monkeypatch.setattr(desktop_body, "_window_class", lambda window_id: "Progman")
    desktop_body.set_runtime_enabled(True)
    shot = asyncio.run(desktop_body.DesktopBody().capture_screenshot())
    active = next(item for item in shot["page_evidence"]["windows"] if item.get("active"))
    assert "image_box" not in active


def test_windows_the_capture_never_shows_are_not_drawn_over_the_front_window(monkeypatch: pytest.MonkeyPatch) -> None:
    # On a real machine a cloaked full-screen frame and Lumen's own capture-excluded overlays sit above
    # every window; counting them blacked out every crop, so no screen text was ever read.
    import types

    stack = {10: 9, 9: 8, 8: 0}  # 10 is the front window; 9 a cloaked frame; 8 a real toast
    fake_gui = types.SimpleNamespace(
        GetWindow=lambda hwnd, _flag: stack.get(int(hwnd), 0),
        IsWindowVisible=lambda hwnd: True,
        GetWindowRect=lambda hwnd: (0, 0, 1920, 1200) if hwnd == 9 else (100, 100, 400, 200),
    )
    monkeypatch.setitem(sys.modules, "win32gui", fake_gui)
    monkeypatch.setitem(sys.modules, "win32con", types.SimpleNamespace(GW_HWNDPREV=3))
    monkeypatch.setattr(desktop_body, "_window_drawn_in_capture", lambda hwnd: hwnd != 9)
    covered = desktop_body._windows_drawn_over("hwnd:10", [0, 0, 800, 600], (0, 0))
    assert covered == [[100, 100, 300, 100]]
    # A stack deeper than the walk, or more covering windows than kept, is unknown, never truncated.
    monkeypatch.setattr(desktop_body, "_window_drawn_in_capture", lambda hwnd: True)
    endless = types.SimpleNamespace(
        GetWindow=lambda hwnd, _flag: int(hwnd) + 1,
        IsWindowVisible=lambda hwnd: False,
        GetWindowRect=lambda hwnd: (0, 0, 1, 1),
    )
    monkeypatch.setitem(sys.modules, "win32gui", endless)
    assert desktop_body._windows_drawn_over("hwnd:10", [0, 0, 800, 600], (0, 0)) == [[-1, -1, 0, 0]]


def test_the_focused_control_stays_in_a_capped_inventory(monkeypatch) -> None:
    # Run 1662: File Explorer's Home view filled every inventory slot before its focused address bar,
    # so a type at the bar bound to an empty pane over it and was refused three times.
    from types import SimpleNamespace

    window_info = SimpleNamespace(handle=77, parent=None)

    def element(runtime_id: int, kind: str, y: int, parent=window_info):
        info = SimpleNamespace(
            rectangle=_Rect(10, y, 200, y + 20), visible=True, enabled=True, runtime_id=(42, runtime_id),
            name=f"{kind} {runtime_id}", control_type=kind, class_name=kind, automation_id="", framework_id="",
            handle=0, parent=parent,
        )
        return SimpleNamespace(element_info=info)

    rows = [element(index, "ListItem", 300 + 20 * index) for index in range(5)]
    address = element(99, "Edit", 100, parent=SimpleNamespace(handle=0, parent=window_info))
    window = SimpleNamespace(handle=77, element_info=SimpleNamespace(**{**vars(rows[0].element_info), "runtime_id": (42, 77)}),
                             descendants=lambda: [*rows, address])
    monkeypatch.setattr(desktop_body, "_MAX_CONTROLS", 3)
    monkeypatch.setattr(desktop_body, "_active_wrapper", lambda: window)
    monkeypatch.setattr(desktop_body, "_hidden_document_ids", lambda *_a, **_k: set())
    monkeypatch.setattr(desktop_body, "_virtual_screen", lambda: (0, 0, 1920, 1200))
    monkeypatch.setattr(desktop_body, "_control_value", lambda *_a, **_k: "")
    monkeypatch.setattr(desktop_body, "_control_affordances", lambda *_a: {})
    monkeypatch.setattr(desktop_body, "_os_focused_wrapper", lambda: address)

    controls, _ = desktop_body._active_controls()
    # Added past the cap, never in place of a listed control (whose sensitive mask would go with it).
    assert len(controls) == 4
    focused = [control for control in controls if control["focused"]]
    assert [control["name"] for control in focused] == ["Edit 99"]
    assert focused[0]["selector"] == "uia:hwnd=77;rid=42.99"

    # Focus in another window is never listed as this window's control.
    stranger = element(55, "Edit", 100, parent=SimpleNamespace(handle=12, parent=None))
    monkeypatch.setattr(desktop_body, "_os_focused_wrapper", lambda: stranger)
    controls, _ = desktop_body._active_controls()
    assert len(controls) == 3 and not any(control["focused"] for control in controls)


def test_a_file_explorer_window_reports_its_folders_real_file_names(monkeypatch, tmp_path) -> None:
    # Run 1663: Explorer hid the extension, so "passport-6EBW.png.png" showed as "passport-6EBW.png" and
    # the model never saw that its rename had doubled the extension.
    import sys
    from types import ModuleType, SimpleNamespace

    def item(name: str):
        return SimpleNamespace(Path=str(tmp_path / name), Name=name.rsplit(".", 1)[0])

    def tab(location: str, path: str, names: list[str]):
        items = [item(name) for name in names]
        folder = SimpleNamespace(Self=SimpleNamespace(Path=path), Items=lambda: SimpleNamespace(Count=len(items), Item=lambda i: items[i]))
        return SimpleNamespace(HWND=55, LocationName=location, Document=SimpleNamespace(Folder=folder))

    windows: list = [tab("inbox", str(tmp_path), ["passport-6EBW.png.png", "scan-7291.png"])]
    client = ModuleType("win32com.client")
    client.Dispatch = lambda name: SimpleNamespace(Windows=lambda: list(windows))
    package = ModuleType("win32com")
    package.client = client
    monkeypatch.setitem(sys.modules, "win32com", package)
    monkeypatch.setitem(sys.modules, "win32com.client", client)
    monkeypatch.setitem(sys.modules, "pythoncom", SimpleNamespace(CoInitialize=lambda: None))

    view = desktop_body._explorer_folder_view("hwnd:55", "inbox - File Explorer")
    assert view == {"path": str(tmp_path), "items": ["passport-6EBW.png.png", "scan-7291.png"], "item_count": 2}
    # Another window: nothing.
    assert desktop_body._explorer_folder_view("hwnd:56", "Notes") == {}
    # Two tabs in one window: the one the title names.
    windows.append(tab("outbox", str(tmp_path), ["report.txt"]))
    assert desktop_body._explorer_folder_view("hwnd:55", "outbox - File Explorer")["items"] == ["report.txt"]
    assert desktop_body._explorer_folder_view("hwnd:55", "Downloads - File Explorer") == {}
    # A virtual folder (Home) is no folder on disk.
    windows[:] = [tab("Home", "::{f874310e-b6b7-47dc-bc84-b9e6b38f5903}", [])]
    assert desktop_body._explorer_folder_view("hwnd:55", "Home - File Explorer") == {}


def test_text_named_on_the_real_screen_is_found_by_reading_it_and_selected_without_a_drag(monkeypatch) -> None:
    # v4_native run 1696: a Tk paragraph exposes no text to UI Automation; the model spent 14 minutes
    # zooming and re-aiming drags across a phrase. The body reads the words around the point instead,
    # and selects with a click and a shift-click: a drag pressed inside an earlier selection would
    # drag that text away.
    from app import screen_ocr

    monkeypatch.setattr(desktop_body, "_select_text_by_uia", lambda text, x, y: None)
    monkeypatch.setattr(desktop_body, "_control_type_at", lambda x, y: "Pane")
    monkeypatch.setattr(desktop_body, "_require_enabled", lambda: None)
    monkeypatch.setattr(desktop_body, "_publish_pointer", lambda *args, **kwargs: None)
    monkeypatch.setattr(desktop_body.time, "sleep", lambda seconds: None)
    before = Image.new("RGB", (1600, 900), "white")
    after = before.copy()
    after.paste((51, 153, 255), (300, 530, 450, 546))  # the highlight drawn over the phrase
    frames = iter([before, after])
    monkeypatch.setattr(desktop_body, "_capture_image", lambda: next(frames))
    read: list[tuple[int, int]] = []

    def read_words(image: bytes) -> list:
        read.append(Image.open(io.BytesIO(image)).size)
        # Boxes in the band's pixels: it starts 400 px above the point (screen y 530 is 390 in it).
        return [[
            {"t": "the", "x": 260, "y": 390, "w": 30, "h": 16},
            {"t": "customs", "x": 300, "y": 390, "w": 70, "h": 16},
            {"t": "release", "x": 380, "y": 390, "w": 70, "h": 16},
            {"t": "form,", "x": 460, "y": 390, "w": 50, "h": 16},
        ]]

    monkeypatch.setattr(screen_ocr, "read_words", read_words)
    clicked: list = []
    monkeypatch.setattr(desktop_body, "_mouse_click", lambda x, y, **kwargs: clicked.append((x, y, list(kwargs.get("modifiers") or []))))
    monkeypatch.setattr(desktop_body, "_mouse_drag", lambda *args, **kwargs: pytest.fail("never a drag"))

    result = asyncio.run(desktop_body.DesktopBody().vision_select_text("customs  release", 600, 540))

    assert read == [(1600, 760)], "the band around the point (clipped at the screen's edge), read once"
    assert clicked == [(298, 538, []), (452, 538, ["shift"])]
    assert result["ok"] is True and result["mode"] == "screen_text"
    assert result["selected_text"] == "customs release"
    assert result["highlight_changed"] is True and result["changed_outside_text"] is False

    # Text on a control a press acts on (a list row, a file name): nothing is clicked.
    frames = iter([before, after])
    clicked.clear()
    monkeypatch.setattr(desktop_body, "_control_type_at", lambda x, y: "ListItem")
    refused = asyncio.run(desktop_body.DesktopBody().vision_select_text("customs release", 600, 540))
    assert (refused["ok"], refused["failure_reason"], refused["control_type"]) == (False, "select_text_not_text", "ListItem")
    assert clicked == []

    # Words nowhere near: nothing is clicked, and the enlarged band was read too.
    monkeypatch.setattr(desktop_body, "_control_type_at", lambda x, y: "Pane")
    frames = iter([before])
    read.clear()
    monkeypatch.setattr(screen_ocr, "read_words", lambda image: read.append(Image.open(io.BytesIO(image)).size) or [])
    missing = asyncio.run(desktop_body.DesktopBody().vision_select_text("customs release", 600, 540))
    assert (missing["ok"], missing["failure_reason"]) == (False, "select_text_not_found")
    assert read == [(1600, 760), (3200, 1520)]
    assert clicked == []


def test_highlighted_text_is_read_dark_on_light_and_the_page_around_it_is_untouched() -> None:
    from PIL import ImageDraw as _Draw

    image = Image.new("RGB", (220, 60), (245, 245, 245))
    draw = _Draw.Draw(image)
    draw.text((10, 20), "plain", fill=(0, 0, 0))
    draw.rectangle((80, 14, 160, 40), fill=(0, 120, 215))  # a selection highlight
    draw.text((90, 20), "picked", fill=(255, 255, 255))
    read = desktop_body._highlights_inverted(image)
    assert read is not None and read.mode == "L"
    inside = list(read.crop((82, 16, 158, 38)).getdata())
    assert min(inside) < 60, "the white letters read black"
    assert sum(1 for level in inside if level == 255) > len(inside) // 2, "the highlight's color reads white"
    # The page right outside the highlight and the unhighlighted words keep their grey.
    assert read.getpixel((77, 27)) == read.getpixel((200, 50)) == 245
    assert min(read.crop((8, 18, 50, 32)).getdata()) < 60
    # Thin colored strokes are not a highlight, and an image without color has none.
    thin = Image.new("RGB", (120, 40), (255, 255, 255))
    _Draw.Draw(thin).text((10, 14), "link text", fill=(0, 102, 204))
    assert desktop_body._highlights_inverted(thin) is None
    assert desktop_body._highlights_inverted(Image.new("RGB", (50, 50), (250, 250, 250))) is None


def test_a_control_exposing_its_text_is_selected_through_ui_automation(monkeypatch) -> None:
    import pywinauto.uia_defines as uia_defines

    text = "Any crate with a torn seal; the customs release form must be signed. The customs release form office."
    calls: list = []

    class Range:
        def __init__(self, start: int, end: int) -> None:
            self.start, self.end = start, end

        def Clone(self):
            return Range(self.start, self.end)

        def FindText(self, wanted, backward, ignore_case):
            index = text.find(wanted, self.start, self.end)
            return Range(index, index + len(wanted)) if index >= 0 else None

        def GetBoundingRectangles(self):
            # The first occurrence sits on the row at y=100, the second on y=300.
            return (40.0, 100.0 if self.start < 60 else 300.0, 120.0, 18.0)

        def MoveEndpointByRange(self, endpoint, other, other_endpoint):
            self.start = other.end

        def Select(self):
            calls.append(("select", self.start, self.end))
            selection.current = self

        def GetText(self, limit):
            return text[self.start : self.end]

    class Selection:
        current = None

        @property
        def Length(self):
            return 1 if self.current else 0

        def GetElement(self, index):
            return self.current

    selection = Selection()

    class Pattern:
        SupportedTextSelection = 1
        DocumentRange = Range(0, len(text))

        def QueryInterface(self, interface):
            return self

        def GetSelection(self):
            return selection

    class Element:
        def __init__(self, password: bool = False, pattern: bool = True) -> None:
            self.CurrentIsPassword = password
            self.pattern = pattern

        def GetCurrentPattern(self, pattern_id):
            return Pattern() if self.pattern else None

    state = {"element": Element()}

    class Walker:
        def GetParentElement(self, element):
            return None

    class FakeIUIA:
        UIA_dll = types.SimpleNamespace(
            UIA_TextPatternId=10014,
            IUIAutomationTextPattern=object,
            TextPatternRangeEndpoint_Start=0,
            TextPatternRangeEndpoint_End=1,
        )
        iuia = types.SimpleNamespace(
            ElementFromPoint=lambda point: state["element"],
            ControlViewWalker=Walker(),
        )

    monkeypatch.setattr(uia_defines, "IUIA", FakeIUIA)
    monkeypatch.setattr(desktop_body, "_virtual_screen", lambda: (0, 0, 1600, 900))

    # The occurrence nearest the point (the second, on the row at y=300), selected and read back.
    result = desktop_body._select_text_by_uia("customs  release form", 100, 305)
    assert result == {
        "ok": True, "action": "select_text", "mode": "text_pattern",
        "selected_text": "customs release form", "desktop_surface": True,
    }
    second = text.index("customs release form", 60)
    assert calls == [("select", second, second + len("customs release form"))]
    # Text the control does not hold, a password control, or no text pattern: the screen is read instead.
    assert desktop_body._select_text_by_uia("bill of lading", 100, 305) is None
    state["element"] = Element(password=True)
    assert desktop_body._select_text_by_uia("customs release form", 100, 305) is None
    state["element"] = Element(pattern=False)
    assert desktop_body._select_text_by_uia("customs release form", 100, 305) is None


def test_screen_selection_tries_ui_automation_first_and_highlighted_text_is_read_last(monkeypatch) -> None:
    from app import screen_ocr

    monkeypatch.setattr(desktop_body, "_require_enabled", lambda: None)
    monkeypatch.setattr(desktop_body, "_publish_pointer", lambda *args, **kwargs: None)
    monkeypatch.setattr(desktop_body.time, "sleep", lambda seconds: None)
    exact = {"ok": True, "action": "select_text", "mode": "text_pattern", "selected_text": "x", "desktop_surface": True}
    monkeypatch.setattr(desktop_body, "_select_text_by_uia", lambda text, x, y: exact)
    monkeypatch.setattr(desktop_body, "_capture_image", lambda: pytest.fail("no capture when the control selected it"))
    assert asyncio.run(desktop_body.DesktopBody().vision_select_text("x", 10, 10)) is exact

    # Drawn text: the plain and enlarged readings miss the highlighted words; the third reading has them.
    monkeypatch.setattr(desktop_body, "_select_text_by_uia", lambda text, x, y: None)
    screen = Image.new("RGB", (400, 200), "white")
    screen.paste((0, 120, 215), (40, 90, 200, 110))
    monkeypatch.setattr(desktop_body, "_capture_image", lambda: screen.copy())
    readings: list = []

    def read_words(image: bytes) -> list:
        readings.append(Image.open(io.BytesIO(image)).mode)
        if len(readings) < 3:
            return []
        return [[{"t": "customs", "x": 50, "y": 92, "w": 70, "h": 16}, {"t": "release", "x": 126, "y": 92, "w": 70, "h": 16}]]

    monkeypatch.setattr(screen_ocr, "read_words", read_words)
    monkeypatch.setattr(desktop_body, "_control_type_at", lambda x, y: "Pane")
    monkeypatch.setattr(desktop_body, "_mouse_click", lambda *args, **kwargs: None)
    result = asyncio.run(desktop_body.DesktopBody().vision_select_text("customs release", 100, 100))
    assert readings == ["RGB", "RGB", "L"]
    assert result["ok"] is True and result["mode"] == "screen_text"


def test_a_spelled_out_key_name_presses_and_an_unknown_one_fails_without_pressing(monkeypatch) -> None:
    # v5_reconcile run 1703 ended on "Unsupported keyboard key: PAGE UP" raised out of the body.
    assert desktop_body._normalize_key_parts("Page Up") == ["PAGEUP"]
    assert desktop_body._normalize_key_parts("Ctrl+Page Down") == ["CTRL", "PAGEDOWN"]
    assert desktop_body._normalize_key_parts("Numpad 5") == ["NUMPAD5"]
    assert desktop_body._normalize_key_parts("Ctrl+-") == ["CTRL", "-"]
    assert desktop_body._check_key("page_up") == ([], 0x21)
    monkeypatch.setattr(desktop_body, "_require_enabled", lambda: None)
    monkeypatch.setattr(desktop_body, "_capture_image", lambda: pytest.fail("nothing is captured or pressed"))
    result = asyncio.run(desktop_body.DesktopBody().vision_key("scroll_up"))
    assert (result["ok"], result["failure_reason"], result["input_dispatched"]) == (False, "unsupported_key", False)
    assert "SCROLLUP" in result["error"]


def test_a_click_that_names_its_option_moves_only_within_the_control_it_aimed_at(monkeypatch) -> None:
    # v5_memory run 1704: an "Approve" click aimed at the Deny radio beside it dispatched a denial. The
    # click preflight judged the control under the aimed point, so the click may move onto its label
    # only inside that control; a label on another control is reported back and nothing is clicked.
    from app import screen_ocr

    monkeypatch.setattr(desktop_body, "_require_enabled", lambda: None)
    monkeypatch.setattr(desktop_body, "_publish_pointer", lambda *args, **kwargs: None)
    monkeypatch.setattr(desktop_body.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(desktop_body, "_capture_image", lambda: Image.new("RGB", (1600, 900), "white"))
    monkeypatch.setattr(desktop_body, "_focus_identity", lambda: None)
    monkeypatch.setattr(desktop_body, "_window_inventory", lambda: [])
    monkeypatch.setattr(desktop_body, "_focused_state", lambda: {})
    monkeypatch.setattr(desktop_body, "_caret_state", lambda: {})
    monkeypatch.setattr(desktop_body, "_remember_target_window", lambda *args: 0)
    # Two radio controls: x < 560 is the Approve radio (indicator and label), x >= 560 the Deny radio.
    monkeypatch.setattr(
        desktop_body,
        "_exact_click_target",
        lambda x, y: {"x": x, "y": y, "snap_applied": False, "control_id": (42, 1) if x < 560 else (42, 2)},
    )
    clicks: list = []
    monkeypatch.setattr(desktop_body, "_mouse_click", lambda x, y, **kwargs: clicks.append((x, y)))
    # What the screen shows, in capture pixels: Approve's label at x 450-520, Deny's at 570-610.
    words = [[{"t": "Approve", "x": 450, "y": 490, "w": 70, "h": 20}, {"t": "Deny", "x": 570, "y": 490, "w": 40, "h": 20}]]
    monkeypatch.setattr(
        desktop_body, "_ocr_locate", lambda image, text, x, y, *, reach: screen_ocr.locate_phrase(text, words, (float(x), float(y)))
    )
    body = desktop_body.DesktopBody()

    # Aimed at the Approve radio's indicator (same control as its label): the click lands on the label.
    own = asyncio.run(body.vision_click(436, 500, label_text="Approve"))
    assert clicks[-1] == (485, 500) and own["label_bound"] is True
    # Aimed at the Deny radio: nothing is clicked; where the Approve label is goes back to the brain.
    clicks.clear()
    elsewhere = asyncio.run(body.vision_click(630, 500, label_text="Approve"))
    assert clicks == [] and elsewhere["ok"] is False and elsewhere["failure_reason"] == "label_elsewhere"
    assert elsewhere["label_point"] == [485, 500] and elsewhere["activation_dispatched"] is False
    # Aimed on the label itself: it is clicked there.
    asyncio.run(body.vision_click(500, 500, label_text="Approve"))
    assert clicks[-1] == (485, 500)
    # Two readings in reach, or one only approximately right: the point stands.
    words[0].append({"t": "Approve", "x": 450, "y": 560, "w": 70, "h": 20})
    twice = asyncio.run(body.vision_click(630, 500, label_text="Approve"))
    assert clicks[-1] == (630, 500) and twice["label_bound"] is False and twice["label_ambiguous"] is True
    words[0].pop()
    words[0][0]["t"] = "Approv"
    asyncio.run(body.vision_click(630, 500, label_text="Approve"))
    assert clicks[-1] == (630, 500)
    words[0][0]["t"] = "Approve"
    # A value with no letter, a final submission, or no label named: the point stands.
    asyncio.run(body.vision_click(630, 500, label_text="172"))
    assert clicks[-1] == (630, 500)
    asyncio.run(body.vision_click(630, 500, label_text="Approve", allow_final_submit=True))
    assert clicks[-1] == (630, 500)
    plain = asyncio.run(body.vision_click(630, 500))
    assert clicks[-1] == (630, 500) and "label_bound" not in plain


def test_a_type_aimed_at_one_control_never_lands_in_another_that_holds_focus(monkeypatch) -> None:
    # v5_memory run 1709: a Tk radio click left focus in the depot-code entry, and a type aimed at the
    # Route dropdown wrote "North" into the depot code, which re-locked the terminal.
    monkeypatch.setattr(desktop_body, "_configured", lambda: True)
    monkeypatch.setattr(desktop_body, "_windows_available", lambda: True)
    monkeypatch.setattr(desktop_body, "desktop_control_status", lambda: {"available": True, "enabled": True})
    monkeypatch.setattr(desktop_body, "_foreground_window", lambda: (77, "Field Terminal"))
    monkeypatch.setattr(desktop_body, "_LAST_TARGET_WINDOW", 77)
    monkeypatch.setattr(desktop_body, "_restore_caret_at_last_click", lambda: False)
    monkeypatch.setattr(desktop_body, "_focus_text_control_within_focus", lambda: False)
    # The depot-code entry has focus: x 500-640, y 150-175.
    monkeypatch.setattr(
        desktop_body, "_focused_state", lambda: {"typable": False, "control_type": "Pane", "box": [500, 150, 640, 175]}
    )
    sent: list[str] = []
    monkeypatch.setattr(desktop_body, "_send_unicode", lambda text: sent.append(text))
    monkeypatch.setattr(desktop_body, "_send_key", lambda *args, **kwargs: sent.append("KEY"))

    elsewhere = asyncio.run(desktop_body.DesktopBody().vision_type("North", target_point=(660, 306)))
    assert (elsewhere["ok"], elsewhere["failure_reason"], elsewhere["input_dispatched"]) == (False, "type_focus_elsewhere", False)
    assert elsewhere["focused_box"] == [500, 150, 640, 175]
    assert sent == [], "no keystroke reaches the focused entry"

    # Tk keeps focus on its window: UI Automation reports the whole window, which contains every point.
    # With no click on the named field just before, where the keys go cannot be told (v5_memory run 1713).
    monkeypatch.setattr(
        desktop_body, "_focused_state", lambda: {"typable": False, "control_type": "Pane", "box": [480, 60, 1040, 560]}
    )
    monkeypatch.setattr(desktop_body, "_virtual_screen", lambda: (0, 0, 1920, 1200))
    monkeypatch.setattr(desktop_body, "_LAST_CLICK_POINT", (600, 162))  # the depot field, clicked earlier
    unverified = asyncio.run(desktop_body.DesktopBody().vision_type("Harbor", target_point=(660, 306)))
    assert (unverified["ok"], unverified["failure_reason"], unverified["input_dispatched"]) == (False, "type_focus_unverified", False)
    assert sent == []

    # The named field was just clicked: typing goes ahead.
    monkeypatch.setattr(desktop_body, "_capture_image", lambda: None)
    monkeypatch.setattr(desktop_body, "_image_changed", lambda before, after: True)
    monkeypatch.setattr(desktop_body, "_readable_active_values", lambda: [])
    monkeypatch.setattr(desktop_body, "_deliver_text", lambda text, **kwargs: sent.append(text) or "keystrokes")
    monkeypatch.setattr(desktop_body, "_LAST_CLICK_POINT", (652, 300))
    asyncio.run(desktop_body.DesktopBody().vision_type("Harbor", target_point=(660, 306)))
    assert "Harbor" in sent
    # A real text control holding focus where the point is needs no click.
    sent.clear()
    monkeypatch.setattr(desktop_body, "_focused_state", lambda: {"typable": True, "control_type": "Edit", "box": [500, 150, 640, 175]})
    monkeypatch.setattr(desktop_body, "_LAST_CLICK_POINT", None)
    asyncio.run(desktop_body.DesktopBody().vision_type("DP-7633", target_point=(645, 162)))
    assert "DP-7633" in sent


def test_an_unreadable_field_is_read_back_from_its_own_pixels(monkeypatch) -> None:
    # v5_memory Sonnet brain (2026-10-06): a Tk entry exposes no value, so every type there was unverified
    # and broke the chain after it.
    from PIL import Image

    from app import screen_ocr

    after = Image.new("RGB", (1200, 800), "white")
    crops: list = []
    monkeypatch.setattr(screen_ocr, "read_lines", lambda images, **kwargs: crops.extend(images) or [["Courier:  North Depot 7"]])
    assert desktop_body._typed_text_read_near_point(after, (500, 300), "North Depot 7") is True
    assert desktop_body._typed_text_read_near_point(after, (500, 300), "South") is False
    # Too short to prove anything, or no point: no claim.
    assert desktop_body._typed_text_read_near_point(after, (500, 300), "7") is False
    assert desktop_body._typed_text_read_near_point(after, None, "North Depot 7") is False


# The user's three monitors (2026-10-07): a 150% laptop panel as primary, two 1080p screens to its right
# sitting 116 px lower. Physical pixels, as the DPI-aware process sees them.
_THREE_MONITORS = [
    {"monitor": 1, "rect": (0, 0, 1920, 1200), "primary": True},
    {"monitor": 2, "rect": (1920, 116, 1920, 1080), "primary": False},
    {"monitor": 3, "rect": (3840, 116, 1920, 1080), "primary": False},
]


def test_the_view_is_one_monitor_cut_from_the_whole_desktop_grab() -> None:
    union = (0, 0, 5760, 1200)
    assert desktop_body._view_crop(union, None) is None
    assert desktop_body._view_crop(union, union) is None
    assert desktop_body._view_crop(union, (1920, 116, 1920, 1080)) == (1920, 116, 3840, 1196)
    # A monitor left of the primary puts the grab's origin at negative desktop coordinates.
    assert desktop_body._view_crop((-1920, 0, 3840, 1080), (0, 0, 1920, 1080)) == (1920, 0, 3840, 1080)


def test_a_window_belongs_to_the_monitor_showing_most_of_it() -> None:
    assert desktop_body._monitor_of_box(_THREE_MONITORS, [2000, 200, 800, 600]) == 2
    assert desktop_body._monitor_of_box(_THREE_MONITORS, [1800, 200, 400, 600]) == 2  # 280 px of it on 2
    assert desktop_body._monitor_of_box(_THREE_MONITORS, [9000, 0, 10, 10]) == 0


def test_the_view_follows_the_foreground_window_unless_a_monitor_is_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    win32gui = pytest.importorskip("win32gui")
    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: 77)
    monkeypatch.setattr(win32gui, "GetWindowRect", lambda handle: (4000, 300, 4800, 900))
    desktop_body._select_view(_THREE_MONITORS)
    assert desktop_body._virtual_screen() == (3840, 116, 1920, 1080)
    # Clicks land on that monitor: capture pixel (10, 20) is desktop point (3850, 136).
    assert desktop_body._absolute_point(10, 20) == (3850, 136)
    desktop_body._VIEW["pinned"] = 2
    desktop_body._select_view(_THREE_MONITORS)
    assert desktop_body._virtual_screen() == (1920, 116, 1920, 1080)
    # A pin to a monitor that is gone falls back to the foreground window's.
    desktop_body._VIEW["pinned"] = 5
    desktop_body._select_view(_THREE_MONITORS)
    assert desktop_body._virtual_screen() == (3840, 116, 1920, 1080) and desktop_body._VIEW["pinned"] is None
    # One monitor: the whole desktop.
    desktop_body._select_view(_THREE_MONITORS[:1])
    assert desktop_body._VIEW["rect"] is None


def test_desktop_capture_with_several_monitors_says_which_one_it_shows(monkeypatch: pytest.MonkeyPatch) -> None:
    windows = [
        {"window_id": "hwnd:1", "title": "Ledger", "pid": 9, "active": True, "box": [2000, 200, 800, 600]},
        {"window_id": "hwnd:2", "title": "Mail", "pid": 10, "active": False, "box": [100, 100, 900, 700]},
    ]
    monkeypatch.setattr(desktop_body, "_configured", lambda: True)
    monkeypatch.setattr(desktop_body, "_monitors", lambda: [dict(item) for item in _THREE_MONITORS])
    monkeypatch.setattr(desktop_body, "_capture_image", lambda: Image.new("RGB", (1920, 1080), "white"))
    monkeypatch.setattr(desktop_body, "_active_controls", lambda: ([], []))
    monkeypatch.setattr(desktop_body, "_window_inventory", lambda: [dict(item) for item in windows])
    monkeypatch.setattr(desktop_body, "_application_inventory", lambda: ([], {}))
    monkeypatch.setattr(desktop_body, "_foreground_stacking", lambda: ("hwnd:1", [2000, 200, 800, 600], []))
    monkeypatch.setattr(desktop_body, "_window_class", lambda window_id: "LedgerWindow")
    win32gui = pytest.importorskip("win32gui")
    monkeypatch.setattr(win32gui, "GetForegroundWindow", lambda: 1)
    monkeypatch.setattr(win32gui, "GetWindowRect", lambda handle: (2000, 200, 2800, 800))
    desktop_body.set_runtime_enabled(True)

    body = desktop_body.DesktopBody()
    evidence = asyncio.run(body.capture_screenshot())["page_evidence"]
    assert [(item["monitor"], item["in_view"]) for item in evidence["monitors"]] == [(1, False), (2, True), (3, False)]
    assert "[hwnd:1] Ledger (active) on monitor 2" in evidence["viewport_text"]
    assert "[hwnd:2] Mail on monitor 1" in evidence["viewport_text"]
    # The active window's box is in the view's pixels.
    active = next(item for item in evidence["windows"] if item.get("active"))
    assert active["image_box"] == [80, 84, 800, 600]

    # focus_window monitor:3 pins the view there; a real window focus unpins it.
    result = asyncio.run(body.vision_focus_window("monitor:3"))
    assert result["ok"] and result["focus_changed"] and result["monitor"] == 3
    evidence = asyncio.run(body.capture_screenshot())["page_evidence"]
    assert [item["monitor"] for item in evidence["monitors"] if item["in_view"]] == [3]
    assert asyncio.run(body.vision_focus_window("monitor:9"))["failure_reason"] == "invalid_monitor"
    assert desktop_body._VIEW["pinned"] == 3


def test_one_monitor_adds_no_monitor_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    windows = [{"window_id": "hwnd:1", "title": "Ledger", "pid": 9, "active": True, "box": [0, 0, 800, 600]}]
    monkeypatch.setattr(desktop_body, "_configured", lambda: True)
    monkeypatch.setattr(desktop_body, "_capture_image", lambda: Image.new("RGB", (800, 600), "white"))
    monkeypatch.setattr(desktop_body, "_active_controls", lambda: ([], []))
    monkeypatch.setattr(desktop_body, "_window_inventory", lambda: [dict(item) for item in windows])
    monkeypatch.setattr(desktop_body, "_application_inventory", lambda: ([], {}))
    desktop_body.set_runtime_enabled(True)
    evidence = asyncio.run(desktop_body.DesktopBody().capture_screenshot())["page_evidence"]
    assert "monitors" not in evidence and "on monitor" not in evidence["viewport_text"]


def test_desktop_navigate_reports_failure_not_a_false_success(monkeypatch) -> None:
    body = desktop_body.DesktopBody()
    monkeypatch.setattr(body._native, "_require_enabled", lambda: None)
    result = asyncio.run(body.navigate("https://example.com/p?token=SECRET123#frag"))
    assert result["ok"] is False
    assert result["failure_reason"] == "navigate_unavailable_on_desktop"
    assert "SECRET123" not in str(result) and "example.com" not in result["error"]
