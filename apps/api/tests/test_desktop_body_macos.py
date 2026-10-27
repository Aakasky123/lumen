"""The macOS desktop body: its primitives behind the shared DesktopBody methods, faked so these run on any OS."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from PIL import Image

from app import desktop_body, desktop_body_macos as mac, desktop_platform


@pytest.fixture(autouse=True)
def _retina_view(monkeypatch: pytest.MonkeyPatch):
    # A 2x display whose top-left is at global point (100, 50): capture pixel (x, y) is absolute (200 + x, 100 + y).
    monkeypatch.setitem(mac._VIEW, "pinned", None)
    monkeypatch.setitem(mac._VIEW, "rect", (200, 100, 3024, 1964))
    monkeypatch.setitem(mac._VIEW, "scale", 2.0)
    monkeypatch.setitem(mac._VIEW, "origin_pt", (100.0, 50.0))
    monkeypatch.setattr(mac, "_clipboard_sequence", lambda: 1)


def _body() -> mac.DesktopBody:
    return mac.DesktopBody()


def test_the_body_is_the_shared_one_with_this_module_as_its_native_primitives() -> None:
    body = _body()
    assert isinstance(body, desktop_body.DesktopBody)
    assert body._native is mac and desktop_body.DesktopBody._native is desktop_body
    assert body.device_id == "macos-desktop"
    # No shell sandbox: the loop sees no callable run_command and leaves the action out.
    assert not callable(getattr(body, "vision_run_command", None))
    context_keys = mac.DesktopBody.tool_context.__qualname__
    assert context_keys.startswith("DesktopBody")


def test_tool_context_names_macos_and_its_command_chords(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mac, "_application_inventory", lambda: ([{"app_id": "app:1", "name": "TextEdit"}], {}))
    monkeypatch.setattr(mac, "_window_inventory", lambda: [])
    context = _body().tool_context()
    assert context["os"] == "macOS"
    assert "CMD+C" in context["key_format"] and "CTRL+" not in context["key_format"].split("with")[0]
    assert context["applications"] == [{"app_id": "app:1", "name": "TextEdit"}]


def test_retina_points_and_capture_pixels_convert_through_the_view() -> None:
    assert mac._absolute_point(10, 20) == (210, 120)
    # Clamped to the view, like the Windows body.
    assert mac._absolute_point(99999, -5) == (200 + 3024 - 1, 100)
    assert mac._to_points(210, 120) == (105.0, 60.0)
    # A window at global points (100, 50) sized 300x200 covers capture pixels (0, 0)-(600, 400).
    assert mac._box_from_pt(100, 50, 300, 200) == [200, 100, 600, 400]


def test_clicks_post_at_global_points_with_counted_click_state(monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[tuple] = []
    monkeypatch.setattr(mac, "_mouse_event", lambda kind, button, x, y, clicks=1: (kind, button, *mac._to_points(x, y), clicks))
    monkeypatch.setattr(mac, "_post", posted.append)
    monkeypatch.setattr(mac.time, "sleep", lambda _: None)
    mac._mouse_click(10, 20, clicks=2)
    assert posted[0] == ("move", "left", 105.0, 60.0, 1)
    assert [event[0] for event in posted[1:]] == ["down", "up", "down", "up"]
    # The second press of a double-click says so, as a hardware double-click does.
    assert [event[4] for event in posted[1:]] == [1, 1, 2, 2]
    assert mac._LAST_CLICK_POINT == (210, 120)
    with pytest.raises(ValueError):
        mac._mouse_click(1, 1, button="sideways")


def test_a_drag_always_releases_where_it_got_to(monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[tuple] = []

    def post(event):
        posted.append(event)
        if event[0] == "drag" and len([item for item in posted if item[0] == "drag"]) == 3:
            raise OSError("event tap went away")

    monkeypatch.setattr(mac, "_mouse_event", lambda kind, button, x, y, clicks=1: (kind, button, x, y))
    monkeypatch.setattr(mac, "_post", post)
    monkeypatch.setattr(mac.time, "sleep", lambda _: None)
    with pytest.raises(OSError):
        mac._mouse_drag(0, 0, 240, 0)
    assert posted[-1][0] == "up" and posted[-1][2:] == [item for item in posted if item[0] == "drag"][-1][2:]


def test_chords_map_to_mac_keys_and_system_shortcuts_stay_blocked() -> None:
    assert mac._check_key("CMD+C") == (["CMD"], 8)
    assert mac._check_key("WIN+V") == (["CMD"], 9)  # the Windows key's place is Command
    assert mac._check_key("cmd+shift+g") == (["CMD", "SHIFT"], 5)
    assert mac._check_key("OPTION+LEFT") == (["ALT"], 123)
    assert mac._check_key("Page Up") == ([], 116)
    assert mac._check_key("BACKSPACE") == ([], 51) and mac._check_key("DELETE") == ([], 117)
    assert mac._check_key("F12") == ([], 111)
    for blocked in ("CMD+Q", "CMD+SHIFT+Q", "CMD+CTRL+Q", "CMD+ALT+ESC", "CMD+SPACE", "META+Q"):
        with pytest.raises(ValueError, match="system shortcut"):
            mac._check_key(blocked)
    for unknown in ("HYPER+A", "CMD+NOPE", ""):
        with pytest.raises(ValueError):
            mac._check_key(unknown)


def test_key_steps_release_every_modifier_even_when_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[tuple[int, bool, int]] = []
    monkeypatch.setattr(mac, "_key_event", lambda code, down, flags: (code, down, flags))
    monkeypatch.setattr(mac, "_post", posted.append)
    steps = mac._key_steps("CMD+SHIFT+Z", repeat=3)
    next(steps)  # paused between the first and second press
    steps.close()
    assert posted[:2] == [(55, True, 0x100000), (56, True, 0x120000)]
    assert posted[-2:] == [(56, False, 0x100000), (55, False, 0)]
    downs = sum(1 for code, down, _ in posted if code == 6 and down)
    ups = sum(1 for code, down, _ in posted if code == 6 and not down)
    assert downs == ups


def test_wheel_deltas_become_lines_in_the_mac_direction() -> None:
    assert mac._wheel_lines(0, 120) == (-3, 0)  # a notch down
    assert mac._wheel_lines(0, -360) == (9, 0)
    assert mac._wheel_lines(120, 0) == (0, -3)
    assert mac._wheel_lines(0, 10) == (-1, 0)  # a nudge still moves
    assert mac._wheel_lines(0, 99999) == (-60, 0)  # capped like the Windows delta


def test_caret_and_paragraph_read_the_focused_text_by_utf16_ranges(monkeypatch: pytest.MonkeyPatch) -> None:
    value = "first line\nse😀cond word\nthird"
    # Columns count characters; the selection arrives as a UTF-16 range, where the emoji counts twice.
    start = value.index("word")
    monkeypatch.setattr(mac, "_focused_text_and_selection", lambda: ("el", value, start, start + 4))
    monkeypatch.setattr(mac, "_AXInfo", lambda element: type("Info", (), {"control_type": "Document"})())
    caret = mac._caret_state()
    assert caret["line_text"] == "se😀cond word" and caret["column"] == 8
    assert caret["selection_length"] == 4 and caret["selected_text"] == "word" and caret["document"] is True
    assert mac._caret_paragraph() == {"text": "se😀cond word", "column": 8, "selected": "word"}
    assert mac._utf16_to_index(value, mac._utf16_length(value[:start])) == start


def test_line_bounds_know_every_line_break() -> None:
    text = "a\r\nbb\rccc dd"
    assert mac._line_bounds(text, 0) == (0, 1)
    assert mac._line_bounds(text, 3) == (3, 5)
    assert mac._line_bounds(text, 7) == (6, 9)
    assert mac._line_bounds(text, 11) == (10, 12)


def test_selectors_carry_a_window_and_an_element_id_the_loop_can_read() -> None:
    import re

    selector = mac._selector(4242, 17)
    assert mac._parse_selector(selector) == (4242, (17,))
    # The loop reads the element id off every desktop selector this way (extension_run._selector_runtime_id).
    assert re.search(r";rid=([-0-9.]+)$", selector).group(1) == "17"
    assert mac._parse_selector("uia:hwnd=1;rid=2") is None


def test_element_ids_stay_with_their_element_and_forget_the_oldest() -> None:
    ids = mac._ElementIds(limit=2)
    first, second = ids.id_of("a"), ids.id_of("b")
    assert ids.id_of("a") == first and first != second
    third = ids.id_of("c")  # "b" is now the oldest and is forgotten
    assert ids.element(second) is None and ids.element(first) == "a" and ids.element(third) == "c"


def test_roles_read_in_the_shared_control_type_vocabulary() -> None:
    def info(role: str, **attrs) -> mac._AXInfo:
        return mac._AXInfo("el", {"AXRole": role, **attrs})

    assert info("AXTextField").control_type == "Edit"
    assert info("AXTextArea").control_type == "Document"
    assert info("AXPopUpButton").control_type == "ComboBox"
    assert info("AXStaticText", AXURL="https://x").control_type == "Hyperlink"
    assert info("AXSecureTextField").is_password and info("AXTextField", AXSubrole="AXSecureTextField").is_password
    assert info("AXButton", AXTitle="", AXDescription="Close").name == "Close"
    assert info("AXSomethingNew").control_type == "SomethingNew"


def test_a_capture_reads_as_the_macos_desktop(monkeypatch: pytest.MonkeyPatch) -> None:
    windows = [
        {"window_id": "cgw:7", "title": "Notes - TextEdit", "pid": 9, "active": True, "box": [200, 100, 1600, 1200]},
        {"window_id": "cgw:8", "title": "Finder", "pid": 10, "active": False, "box": [400, 300, 800, 600]},
    ]
    controls = [{"selector": "ax:win=7;rid=3", "tag": "document", "role": "Document", "name": "", "value": "hello", "box": [0, 40, 1600, 1000], "focused": True}]
    monkeypatch.setattr(mac, "_require_enabled", lambda: None)
    monkeypatch.setattr(mac, "_select_view", lambda: [])
    monkeypatch.setattr(mac, "_foreground_stacking", lambda: ("cgw:7", [200, 100, 1600, 1200], []))
    monkeypatch.setattr(mac, "_capture_image", lambda: Image.new("RGB", (3024, 1964), "white"))
    monkeypatch.setattr(mac, "_active_controls", lambda: ([dict(item) for item in controls], []))
    monkeypatch.setattr(mac, "_window_inventory", lambda: [dict(item) for item in windows])
    monkeypatch.setattr(mac, "_caret_state", lambda: {})
    monkeypatch.setattr(mac, "_window_class", lambda window_id: "com.apple.TextEdit")
    monkeypatch.setattr(mac, "_focused_text_lines", lambda controls, marked: {})

    shot = asyncio.run(_body().capture_screenshot())
    evidence = shot["page_evidence"]
    assert shot["url"] == "desktop://macos/cgw:7" and shot["title"] == "Notes - TextEdit"
    assert evidence["viewport_text"].startswith("Visible windows:\n[cgw:7] Notes - TextEdit (active)")
    assert (shot["width"], shot["height"]) == (3024, 1964)
    active = next(item for item in evidence["windows"] if item["active"])
    # The window's box in capture pixels: absolute minus the view's origin.
    assert active["image_box"] == [0, 0, 1600, 1200] and active["drawn_over"] == []
    assert "folder_view" not in evidence


def test_finder_in_front_reports_its_folder(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    (tmp_path / "scan.png").write_bytes(b"x")
    (tmp_path / ".DS_Store").write_bytes(b"x")
    monkeypatch.setattr(mac, "_parse_window_id", lambda window_id: 5)
    monkeypatch.setattr(mac, "_ax_window_for", lambda handle: "window")
    monkeypatch.setattr(mac, "_ax_attr", lambda element, name: tmp_path.as_uri() + "/" if name == "AXDocument" else None)
    view = mac._explorer_folder_view("cgw:5", "Downloads")
    assert view == {"path": str(tmp_path), "items": ["scan.png"], "item_count": 1}


def test_a_copy_made_with_a_password_manager_in_front_is_a_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mac, "_frontmost_pid", lambda: 31)
    monkeypatch.setattr(mac, "_secure_input_enabled", lambda: False)
    monkeypatch.setattr(mac, "_pasteboard_types", lambda: ["public.utf8-plain-text"])
    monkeypatch.setattr(mac, "_bundle_id", lambda pid: "com.apple.TextEdit")
    assert mac._clipboard_owner_process() == {"pid": 31, "exe": "com.apple.TextEdit", "credential_store": False}
    monkeypatch.setattr(mac, "_bundle_id", lambda pid: "com.1password.1password")
    assert mac._clipboard_owner_process()["credential_store"] is True
    monkeypatch.setattr(mac, "_bundle_id", lambda pid: "com.apple.TextEdit")
    monkeypatch.setattr(mac, "_pasteboard_types", lambda: ["public.utf8-plain-text", "org.nspasteboard.ConcealedType"])
    assert mac._clipboard_owner_process()["credential_store"] is True


def test_a_short_value_is_typed_not_pasted_over_an_image(monkeypatch: pytest.MonkeyPatch) -> None:
    typed: list[str] = []
    monkeypatch.setattr(mac, "_send_unicode", typed.append)
    monkeypatch.setattr(mac, "_pasteboard_types", lambda: ["public.png"])
    monkeypatch.setattr(mac, "_write_clipboard_text", lambda value: pytest.fail("the image must stay on the clipboard"))
    hold = mac._ClipboardHold()
    assert mac._deliver_text("short", prefer_paste=True, clipboard=hold) == "keystrokes"
    assert typed == ["short"] and not hold.pending
    # A secret never touches the clipboard at all.
    assert mac._deliver_text("x" * 500, allow_paste=False, clipboard=hold) == "keystrokes"


def test_apps_that_open_a_shell_or_admin_tool_are_not_launchable() -> None:
    for name in ("Terminal", "iTerm", "Script Editor", "Keychain Access", "Python 3.12", "Uninstall Foo"):
        assert not mac._app_name_allowed(name), name
    for name in ("TextEdit", "Safari", "Google Chrome", "Calculator"):
        assert mac._app_name_allowed(name), name


def test_system_settings_is_not_launchable_openable_listed_or_raised(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    for name in ("System Settings", "System Preferences", "system settings", "System Settings Helper"):
        assert not mac._app_name_allowed(name), name
    assert mac._app_name_allowed("Settings Sync Tool") and mac._app_name_allowed("Calculator")
    # URL schemes and panes never open, whatever the caller.
    for target in ("x-apple.systempreferences:com.apple.preference.security", "x-apple.systemsettings:", "prefs:root=Privacy",
                   "/System/Library/PreferencePanes/Security.prefPane", "/System/Applications/System Settings.app"):
        assert mac._settings_target(target), target
        if not target.startswith(("x-", "prefs")):
            with pytest.raises(RuntimeError):
                mac._open_path(Path(target))
    assert not mac._settings_target("/Users/me/Downloads/report.pdf")
    ran = []
    monkeypatch.setattr(mac.subprocess, "run", lambda *a, **k: ran.append(a))
    # A renamed copy is still refused by its bundle id, at launch and at enumeration.
    import plistlib

    bundle = tmp_path / "Prefs.app"
    (bundle / "Contents").mkdir(parents=True)
    (bundle / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.apple.systempreferences", "CFBundleName": "Prefs"}))
    assert not mac._launch_inventory_target({"kind": "bundle", "target": str(bundle)})
    monkeypatch.setattr(mac, "_APPLICATION_ROOTS", (tmp_path,))
    assert mac._app_candidates() == []
    assert ran == []
    # Its windows are not in the inventory (so focus_window finds them stale) and are never raised.
    infos = [
        {"id": 1, "pid": 10, "owner": "System Settings", "title": "Privacy & Security", "bounds": (0.0, 0.0, 100.0, 100.0)},
        {"id": 2, "pid": 11, "owner": "Renamed", "title": "Hidden by id", "bounds": (0.0, 0.0, 100.0, 100.0)},
        {"id": 3, "pid": 12, "owner": "TextEdit", "title": "Untitled", "bounds": (0.0, 0.0, 100.0, 100.0)},
    ]
    monkeypatch.setattr(mac, "_window_infos", lambda: infos)
    monkeypatch.setattr(mac, "_foreground_window_id", lambda: 3)
    monkeypatch.setattr(mac, "_bundle_id", lambda pid: "com.apple.systempreferences" if pid == 11 else "")
    assert [item["title"] for item in mac._window_inventory()] == ["Untitled - TextEdit"]
    monkeypatch.setattr(mac, "_window_pid", lambda handle: 11)
    touched = []
    monkeypatch.setattr(mac, "_ax_window_for", lambda handle: touched.append(handle))
    mac._set_foreground_window(2)
    assert touched == []


def test_status_names_missing_permissions_and_refuses_with_them(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mac, "_macos_available", lambda: True)
    monkeypatch.setattr(mac, "_configured", lambda: True)
    monkeypatch.setitem(sys.modules, "AppKit", type(sys)("AppKit"))
    monkeypatch.setitem(sys.modules, "ApplicationServices", type(sys)("ApplicationServices"))
    monkeypatch.setitem(sys.modules, "Quartz", type(sys)("Quartz"))
    monkeypatch.setattr(mac, "_permissions", lambda: {"accessibility": False, "screen_recording": True, "post_events": True})
    status = mac.desktop_control_status()
    assert status["available"] is False and status["permissions_missing"] == ["accessibility"]
    assert "Privacy_Accessibility" in status["permission_settings"]["accessibility"]
    assert status["safety"]["shell_execution"] == "unavailable"
    with pytest.raises(RuntimeError, match="needs permission: accessibility"):
        mac._require_enabled()
    monkeypatch.setattr(mac, "_permissions", lambda: {"accessibility": True, "screen_recording": True, "post_events": True})
    assert mac.desktop_control_status()["available"] is True


def test_pointer_feedback_goes_out_in_global_points(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict] = []
    monkeypatch.setattr(mac, "publish_pointer_event", lambda kind, **kwargs: seen.append({"kind": kind, **kwargs}))
    mac._publish_pointer("click", rel_x=10, rel_y=20)
    assert seen == [{"kind": "click", "x": 105, "y": 60, "dy": None, "label": ""}]


def test_back_and_forward_are_the_command_bracket_chords(monkeypatch: pytest.MonkeyPatch) -> None:
    pressed: list[str] = []

    async def fake_key(self, key, **kwargs):
        pressed.append(key)
        return {"ok": True}

    monkeypatch.setattr(desktop_body.DesktopBody, "vision_key", fake_key)
    body = _body()
    asyncio.run(mac.DesktopBody.navigate_back.__wrapped__(body))
    asyncio.run(mac.DesktopBody.navigate_forward.__wrapped__(body))
    assert pressed == ["CMD+[", "CMD+]"]


def test_the_platform_facade_serves_the_os_body_unless_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    desktop_platform.pin(None)
    monkeypatch.setattr(desktop_platform.sys, "platform", "darwin")
    assert desktop_platform.module() is mac
    assert desktop_platform.DesktopBody is mac.DesktopBody
    monkeypatch.setattr(desktop_platform.sys, "platform", "win32")
    assert desktop_platform.module() is desktop_body
    desktop_platform.pin(mac)
    assert desktop_platform.module() is mac


def test_windows_wording_is_said_for_a_mac_and_left_alone_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import lumen_model

    router = "Use execution_surface=desktop only when the task starts in native Windows applications, Settings, or Explorer with no starting web page; target_url must then be empty"
    assert desktop_platform.os_text(router) == router  # pinned to Windows by conftest
    desktop_platform.pin(mac)
    assert "native Mac applications or Finder with no starting web page" in desktop_platform.os_text(router)
    assert "Settings" not in desktop_platform.os_text(router)
    assert desktop_platform.os_text("I created a live Windows computer task") == "I created a live Mac computer task"
    assert desktop_platform.os_text("in your browser and in Windows apps.") == "in your browser and in Mac apps."
    # Nothing Windows-worded is left in the router's own sentence about where desktop tasks start.
    assert "Windows" not in desktop_platform.os_text(router)
    assert lumen_model.desktop_platform is desktop_platform


def test_a_missing_permission_says_where_to_grant_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mac, "_permissions", lambda: {"accessibility": False, "screen_recording": True, "post_events": False})
    message = mac.permission_message()
    assert "Accessibility permission" in message and "Privacy & Security > Accessibility" in message
    monkeypatch.setattr(mac, "_permissions", lambda: {"accessibility": False, "screen_recording": False, "post_events": False})
    assert "Accessibility and Screen Recording permission" in mac.permission_message()


def test_input_is_refused_while_system_settings_is_frontmost(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = []

    async def core_click(self, x, y, **kwargs):
        sent.append((x, y))
        return {"ok": True}

    monkeypatch.setattr(desktop_body.DesktopBody, "vision_click", core_click)
    monkeypatch.setattr(mac, "_frontmost_pid", lambda: 7)
    monkeypatch.setattr(mac, "_bundle_id", lambda pid: "com.apple.systempreferences")
    body = _body()
    receipt = asyncio.run(body.vision_click(5, 6))
    assert receipt["ok"] is False and receipt["failure_reason"] == "desktop_prohibited_app_frontmost" and sent == []
    refused = asyncio.run(body.vision_key("A"))
    assert refused["failure_reason"] == "desktop_prohibited_app_frontmost"
    assert "focus_window" in refused["error"] and "focus_window" in refused["message"]
    monkeypatch.setattr(mac, "_bundle_id", lambda pid: "com.apple.TextEdit")
    assert asyncio.run(body.vision_click(5, 6)) == {"ok": True} and sent == [(5, 6)]


def test_spotlight_and_input_source_chords_are_blocked() -> None:
    for chord in ("CMD+SPACE", "CMD+ALT+SPACE"):
        with pytest.raises(ValueError):
            mac._check_key(chord)
    for chord in ("SHIFT+SPACE", "CTRL+SPACE", "CTRL+CMD+SPACE", "CTRL+SHIFT+SPACE"):
        mac._check_key(chord)


def test_guarded_input_methods_keep_the_wrapped_signature() -> None:
    # The loop reads capabilities from signatures (_method_accepts: at_caret, sensitive, expected_target_identity,
    # allow_final_submit); a (*args, **kwargs) guard made the Mac refuse every caret type.
    import inspect

    from app import desktop_body as windows
    from app import desktop_body_macos as macos

    for name in (
        "vision_click", "vision_click_control", "vision_type", "vision_type_control", "vision_key", "vision_wheel",
        "vision_drag", "vision_select_text", "vision_hover", "vision_upload", "vision_upload_control",
    ):
        assert list(inspect.signature(getattr(macos.DesktopBody, name)).parameters) == list(
            inspect.signature(getattr(windows.DesktopBody, name)).parameters
        ), name
    assert "at_caret" in inspect.signature(macos.DesktopBody.vision_type_control).parameters


def test_a_covered_control_is_raised_first_and_refused_if_still_covered(monkeypatch) -> None:
    # The approved final click once landed on Lumen's own approval window, still closing over Safari.
    import sys
    import types

    from app import desktop_body_macos as macos

    fake_ax = types.SimpleNamespace(AXUIElementGetPid=lambda element, _: (0, 4242))
    monkeypatch.setitem(sys.modules, "ApplicationServices", fake_ax)
    monkeypatch.setitem(sys.modules, "AppKit", types.SimpleNamespace(NSRunningApplication=None, NSApplicationActivateIgnoringOtherApps=1))
    monkeypatch.setattr(macos, "_bundle_id", lambda pid: "com.apple.Safari")
    monkeypatch.setattr(macos, "_to_points", lambda x, y: (x / 2, y / 2))
    monkeypatch.setattr(macos, "_ax_attr", lambda *a, **k: None)
    monkeypatch.setattr(macos, "_ax_set", lambda *a, **k: True)
    monkeypatch.setattr(macos, "_ax_app", lambda pid: object())
    monkeypatch.setattr(macos.time, "sleep", lambda s: None)
    wrapper = types.SimpleNamespace(element=object())

    owners = {"front": 4242, "at_point": 4242}
    monkeypatch.setattr(macos, "_frontmost_pid", lambda: owners["front"])
    monkeypatch.setattr(macos, "_window_pid_at", lambda x, y: owners["at_point"])
    assert macos._ensure_control_clickable(wrapper, (1094, 511)) == ""

    owners["at_point"] = 9999  # another app's window stays over the control
    clock = iter(range(0, 100))
    monkeypatch.setattr(macos.time, "monotonic", lambda: next(clock) * 0.5)
    assert macos._ensure_control_clickable(wrapper, (1094, 511)) == "desktop_target_covered"
