"""OSWorld body over a fake VM transport: the application list and launch_app, wait-for-text, and
select-text."""

from __future__ import annotations

import asyncio
import inspect
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from PIL import Image

from app import screen_ocr
from app.agent_eval.osworld import a11y
from app.agent_eval.osworld import body as body_module
from app.agent_eval.osworld.body import OSWorldBody, launchable_applications


def _entry(app_id: str, name: str, exec_line: str, **keys: str) -> dict[str, Any]:
    row = {"id": app_id, "path": f"/usr/share/applications/{app_id}", "Type": "Application", "Name": name,
           "Exec": exec_line, "TryExecFound": True}
    row.update(keys)
    return row


ROWS = [
    _entry("org.gnome.TextEditor.desktop", "Text Editor", "gnome-text-editor %U"),
    _entry("google-chrome.desktop", "Google Chrome", "/usr/bin/google-chrome-stable %U"),
    _entry("libreoffice-calc.desktop", "LibreOffice Calc", "env GTK_THEME=Adwaita libreoffice --calc %U"),
    _entry("org.gnome.Terminal.desktop", "Terminal", "gnome-terminal", Categories="GNOME;GTK;System;TerminalEmulator;"),
    _entry("xterm.desktop", "XTerm", "xterm"),
    _entry("debian-uxterm.desktop", "UXTerm", "uxterm"),
    _entry("python3.12.desktop", "Python (v3.12)", "python3.12"),
    _entry("htop.desktop", "Htop", "htop", Terminal="true"),
    _entry("wrapper.desktop", "Wrapped", "sh -c 'echo hi'"),
    _entry("root.desktop", "Root Files", "pkexec nautilus"),
    _entry("hidden.desktop", "Hidden App", "hidden-app", NoDisplay="true"),
    _entry("kde-only.desktop", "KDE Thing", "kthing", OnlyShowIn="KDE;"),
    _entry("not-gnome.desktop", "Not GNOME", "ngthing", NotShowIn="GNOME;"),
    _entry("missing.desktop", "Missing", "missing-app", TryExecFound=False),
    _entry("dup.desktop", "Text Editor", "other-editor"),
    {"id": "link.desktop", "Type": "Link", "Name": "A link", "Exec": ""},
]


def test_the_application_list_is_the_vms_launchers_minus_shells_and_terminals() -> None:
    listed = launchable_applications(ROWS)
    assert [item["name"] for item in listed] == ["Google Chrome", "LibreOffice Calc", "Text Editor"]
    assert listed[2]["app_id"] == "org.gnome.TextEditor.desktop"  # the first of two same-named entries


class FakeVM(OSWorldBody):
    """OSWorldBody with its VM transport replaced: /execute is recorded and answered by `answer`."""

    def __init__(self, *, trees: list[dict[str, Any]] | None = None, answer: Any = None) -> None:
        super().__init__("http://vm.invalid")
        self.commands: list[Any] = []
        self.trees = list(trees or [])
        self.answer = answer or (lambda command: {"status": "success", "output": "", "returncode": 0})

    def _execute(self, command: Any, *, shell: bool = False) -> dict[str, Any]:
        self.commands.append(command)
        return self.answer(command)

    def _screenshot(self) -> Image.Image:
        return Image.new("RGB", (1920, 1080), (240, 240, 240))

    def _tree(self) -> dict[str, Any]:
        tree = self.trees.pop(0) if len(self.trees) > 1 else (self.trees[0] if self.trees else {})
        return {"controls": [], "windows": [], "focused": None, "active_box": None, "active_text": "", "xml": "<x/>", **tree}


def _vm_answer(launched: bool = True):
    def answer(command: Any) -> dict[str, Any]:
        if command[:2] == ["python", "-c"] and command[2] == body_module._APP_LIST_SCRIPT:
            return {"status": "success", "output": json.dumps(ROWS), "returncode": 0}
        if command[:2] == ["python", "-c"] and command[2] == body_module._LAUNCH_SCRIPT:
            return {"status": "success", "output": json.dumps({"launched": launched, "via": "gtk-launch"}), "returncode": 0}
        return {"status": "success", "output": "", "returncode": 0}

    return answer


def test_launch_app_starts_a_listed_entry_and_reports_its_new_window(monkeypatch) -> None:
    monkeypatch.setattr(body_module.time, "sleep", lambda _seconds: None)
    before = {"windows": [{"window_id": "win:1.0", "title": "Files", "app": "nautilus", "active": True}]}
    after = {"windows": [*before["windows"], {"window_id": "win:2.0", "title": "Untitled 1 - LibreOffice Calc", "app": "soffice", "active": True}]}
    vm = FakeVM(trees=[before, after], answer=_vm_answer())
    context = vm.tool_context()
    assert {"app_id": "libreoffice-calc.desktop", "name": "LibreOffice Calc"} in context["applications"]
    assert all("Terminal" not in item["name"] and "path" not in item for item in context["applications"])
    assert "launch_app" in context["tool_targets"]

    result = asyncio.run(vm.vision_launch_app("libreoffice-calc.desktop"))
    assert result["ok"] and result["surface_changed"] and result["new_window_id"] == "win:2.0"
    assert result["new_window_title"] == "Untitled 1 - LibreOffice Calc"
    # The exact enumerated id and desktop file go to the VM as arguments, never as a shell string.
    launch = vm.commands[-1]
    assert launch == ["python", "-c", body_module._LAUNCH_SCRIPT, "libreoffice-calc.desktop", "/usr/share/applications/libreoffice-calc.desktop"]
    # The list was read once and cached.
    assert sum(1 for command in vm.commands if command[2] == body_module._APP_LIST_SCRIPT) == 1


def test_launch_app_refuses_an_unlisted_id_and_reports_a_failed_start(monkeypatch) -> None:
    monkeypatch.setattr(body_module.time, "sleep", lambda _seconds: None)
    vm = FakeVM(answer=_vm_answer())
    for blocked in ("org.gnome.Terminal.desktop", "xterm.desktop", "gnome-terminal", "../evil.desktop"):
        result = asyncio.run(vm.vision_launch_app(blocked))
        assert result["ok"] is False and result["failure_reason"] == "desktop_app_target_stale"
    assert not any(command[2] == body_module._LAUNCH_SCRIPT for command in vm.commands)
    failing = FakeVM(answer=_vm_answer(launched=False))
    result = asyncio.run(failing.vision_launch_app("google-chrome.desktop"))
    assert result["ok"] is False and result["failure_reason"] == "osworld_launch_failed"


def test_the_vm_scripts_list_and_start_desktop_entries(tmp_path: Path, monkeypatch) -> None:
    # The in-VM scripts run as written here: the list reads a user application directory, and the
    # launcher (no gtk-launch on this host) starts the entry's Exec line with field codes removed.
    home = tmp_path / "home"
    apps = home / ".local" / "share" / "applications"
    apps.mkdir(parents=True)
    marker = tmp_path / "started.txt"
    python = Path(sys.executable).as_posix()
    code = f"open({marker.as_posix()!r}, 'w').write('started')"
    (apps / "probe.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=Probe: test\nExec=\"" + python + "\" -c \"" + code + "\" %U\n"
        "Categories=Utility;\nNoDisplay=false\n",
        encoding="utf-8",
    )
    (apps / "broken.desktop").write_text("not a desktop file", encoding="utf-8")
    env = {**__import__("os").environ, "HOME": str(home), "USERPROFILE": str(home), "PATH": ""}
    listed = subprocess.run([sys.executable, "-c", body_module._APP_LIST_SCRIPT], capture_output=True, text=True, env=env, timeout=60)
    rows = [row for row in json.loads(listed.stdout) if row["id"] in {"probe.desktop", "broken.desktop"}]
    assert [row["id"] for row in rows] == ["probe.desktop"]
    assert rows[0]["Name"] == "Probe: test" and rows[0]["TryExecFound"] is True
    started = subprocess.run(
        [sys.executable, "-c", body_module._LAUNCH_SCRIPT, "probe.desktop", rows[0]["path"]],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert json.loads(started.stdout.strip().splitlines()[-1]) == {"launched": True, "via": "exec"}
    for _ in range(100):
        if marker.exists():
            break
        __import__("time").sleep(0.1)
    assert marker.read_text() == "started"


def test_wait_for_text_reads_the_front_windows_text_and_ignores_copies_already_shown(monkeypatch) -> None:
    assert "text" in inspect.signature(OSWorldBody.vision_wait).parameters
    monkeypatch.setattr(screen_ocr, "available", lambda: False)
    rising = FakeVM(trees=[{"active_text": "Saving..."}, {"active_text": "Saving..."}, {"active_text": "Saved report.pdf"}])
    result = asyncio.run(rising.vision_wait(5000, text="  SAVED  "))
    assert result["ok"] and result["text_matched"] is True and result["present_at_start"] == 0

    static = FakeVM(trees=[{"active_text": "Saved draft"}])
    result = asyncio.run(static.vision_wait(900, text="saved"))
    assert result["ok"] is False and result["failure_reason"] == "wait_text_not_seen"
    assert result["text_matched"] is False and result["present_at_start"] == 1


def test_wait_for_text_also_reads_the_front_windows_pixels(monkeypatch) -> None:
    # A canvas or a window with no accessible text: the OCR reading rises while the tree stays empty.
    readings = iter([[["Rendering"]], [["Rendering"]], [["Done", "Rendering"]]])
    crops: list[tuple[int, int]] = []

    def read_lines(images: list[bytes], **_: Any) -> list[list[str]]:
        import io

        crops.append(Image.open(io.BytesIO(images[0])).size)
        return next(readings, [["Done"]])

    monkeypatch.setattr(screen_ocr, "available", lambda: True)
    monkeypatch.setattr(screen_ocr, "read_lines", read_lines)
    vm = FakeVM(trees=[{"active_box": [100, 50, 800, 600]}])
    result = asyncio.run(vm.vision_wait(5000, text="done"))
    assert result["text_matched"] is True
    assert crops[0] == (800, 600)  # only the front window is read


def test_plain_wait_still_pauses(monkeypatch) -> None:
    vm = FakeVM()
    result = asyncio.run(vm.vision_wait(100))
    assert result["ok"] and result["wait_ms"] == 100 and "text_matched" not in result


def test_select_text_clicks_before_and_shift_clicks_after_the_occurrence(monkeypatch) -> None:
    from app import desktop_body

    monkeypatch.setattr(body_module.time, "sleep", lambda _seconds: None)
    located = {"found": True, "from": (329, 312), "to": (408, 312), "box": (330, 304, 407, 321), "occurrences": 2}
    monkeypatch.setattr(desktop_body, "_ocr_locate", lambda *args, **kwargs: dict(located))
    paragraph = {"controls": [{"selector": "atspi:1", "role": "paragraph", "box": [100, 290, 900, 40]}]}
    vm = FakeVM(trees=[paragraph])
    result = asyncio.run(vm.vision_select_text("report.pdf", 300, 310))
    assert result["ok"] and result["mode"] == "screen_text" and result["selected_text"] == "report.pdf"
    assert result["occurrences"] == 2 and result["activation_dispatched"] is True
    code = vm.commands[-1][2]
    assert "pyautogui.click(329, 312)" in code and "keyDown('shift'); pyautogui.click(408, 312)" in code

    button = {"controls": [{"selector": "atspi:2", "role": "push-button", "box": [300, 300, 200, 30]}]}
    vm = FakeVM(trees=[button])
    result = asyncio.run(vm.vision_select_text("report.pdf", 300, 310))
    assert result["ok"] is False and result["failure_reason"] == "select_text_not_text" and not vm.commands

    monkeypatch.setattr(desktop_body, "_ocr_locate", lambda *args, **kwargs: {"found": False, "occurrences": 0})
    result = asyncio.run(FakeVM().vision_select_text("nowhere", 10, 10))
    assert result["ok"] is False and result["failure_reason"] == "select_text_not_found"


def test_the_tree_reports_the_front_windows_box_and_text_without_passwords() -> None:
    from tests.test_osworld_adapter import TREE

    parsed = a11y.parse_tree(TREE, screen_width=1920, screen_height=1080)
    assert parsed["active_box"] == [70, 30, 1850, 1050]
    assert "hello world" in parsed["active_text"] and "Save" in parsed["active_text"]
    assert "s3cret" not in parsed["active_text"] and "Token" not in parsed["active_text"]
    assert "Activities" not in parsed["active_text"]  # another application's panel
    assert a11y.control_at(parsed["controls"], 1710, 50)["name"] == "Save"


def _ctl(selector: str, name: str, role: str, box: list[int], **extra: Any) -> dict[str, Any]:
    return {"selector": selector, "name": name, "text": name, "role": role, "box": box, "value": "",
            "editable": False, "focused": False, "window_id": "w1", **extra}


def test_a_stale_selector_clicks_the_models_point_only_when_the_same_control_is_there(monkeypatch) -> None:
    monkeypatch.setattr(body_module.time, "sleep", lambda _seconds: None)
    identity = {"expected_target_identity": {"target_text": "Save"}}
    same = {"controls": [_ctl("atspi:9", "Save", "push-button", [100, 100, 60, 30])]}
    vm = FakeVM(trees=[same])
    result = asyncio.run(vm.vision_click_control("atspi:1", 120, 110, **identity))
    assert result["ok"] and result["rebound_by_point"] is True and any("pyautogui.click(120, 110" in str(c) for c in vm.commands)

    other = {"controls": [_ctl("atspi:9", "Delete", "push-button", [100, 100, 60, 30])]}
    for tree in (other, {}):
        vm = FakeVM(trees=[tree])
        result = asyncio.run(vm.vision_click_control("atspi:1", 120, 110, **identity))
        assert result["ok"] is False and result["failure_reason"] == "desktop_uia_target_stale"
        assert result["activation_attempted"] is False and not vm.commands

    # A found selector whose name no longer matches is stale too, even under final authority.
    renamed = {"controls": [_ctl("atspi:1", "Delete", "push-button", [100, 100, 60, 30])]}
    vm = FakeVM(trees=[renamed])
    result = asyncio.run(vm.vision_click_control("atspi:1", 120, 110, allow_final_submit=True, **identity))
    assert result["ok"] is False and result["failure_reason"] == "desktop_uia_target_stale" and not vm.commands

    # Final authority never moves to another window's same-named control.
    moved = {"controls": [_ctl("atspi:9", "Save", "push-button", [100, 100, 60, 30], window_id="w2")]}
    vm = FakeVM(trees=[moved])
    result = asyncio.run(vm.vision_click_control("atspi:1", 120, 110, allow_final_submit=True, **identity))
    assert result["ok"] is False and not vm.commands

    # A bound control still clicks, and a plain coordinate click needs no selector.
    vm = FakeVM(trees=[{"controls": [_ctl("atspi:1", "Save", "push-button", [100, 100, 60, 30])]}])
    result = asyncio.run(vm.vision_click_control("atspi:1", 5, 5, **identity))
    assert result["ok"] and "rebound_by_point" not in result and "pyautogui.click(130, 115" in vm.commands[-1][2]
    vm = FakeVM()
    assert asyncio.run(vm.vision_click(40, 50))["ok"] and "pyautogui.click(40, 50" in vm.commands[-1][2]


def test_type_clicks_the_target_point_and_refuses_when_focus_is_on_a_non_text_control(monkeypatch) -> None:
    monkeypatch.setattr(body_module.time, "sleep", lambda _seconds: None)
    field = _ctl("atspi:3", "Name", "text", [10, 10, 200, 30], editable=True, focused=True)
    vm = FakeVM(trees=[{"controls": [field], "focused": field}])
    result = asyncio.run(vm.vision_type("hello", target_point=[50, 20]))
    assert "pyautogui.click(50, 20" in vm.commands[0][2]
    assert any("pyautogui.write('hello'" in str(c) for c in vm.commands) and result["input_dispatched"] is True

    button = _ctl("atspi:4", "OK", "push-button", [10, 10, 60, 30], focused=True)
    vm = FakeVM(trees=[{"controls": [button], "focused": button}])
    result = asyncio.run(vm.vision_type("hello", target_point=[50, 20]))
    assert result["ok"] is False and result["failure_reason"] == "desktop_type_target_not_focused"
    assert result["input_dispatched"] is False and not any("pyautogui.write" in str(c) for c in vm.commands)
    result = asyncio.run(FakeVM(trees=[{"controls": [button], "focused": button}]).vision_type("hello"))
    assert result["failure_reason"] == "desktop_type_target_not_focused" and result["focus_attempted"] is False


def _typing_vm(trees: list[dict[str, Any]]) -> FakeVM:
    """A FakeVM whose screen changes once text has been typed."""
    vm = FakeVM(trees=trees)
    vm._screenshot = lambda: Image.new("RGB", (1920, 1080), (0, 0, 0) if any("write" in str(c) for c in vm.commands) else (240, 240, 240))  # type: ignore[method-assign]
    return vm


def test_an_unnamed_target_needs_its_role_and_to_still_be_near_the_models_point() -> None:
    identity = {"expected_target_identity": {"target_role": "push-button"}}
    box = [100, 100, 40, 30]
    same = {"controls": [_ctl("atspi:1", "", "push-button", box)]}
    result = asyncio.run(FakeVM(trees=[same]).vision_click_control("atspi:1", 110, 110, **identity))
    assert result["ok"] is True
    # The path now holds another role, or a same-role control far from the point: stale.
    for control in (_ctl("atspi:1", "", "check-box", box), _ctl("atspi:1", "", "push-button", [900, 700, 40, 30]), _ctl("atspi:1", "Save", "push-button", box)):
        vm = FakeVM(trees=[{"controls": [control]}])
        result = asyncio.run(vm.vision_click_control("atspi:1", 110, 110, **identity))
        assert result["ok"] is False and result["failure_reason"] == "desktop_uia_target_stale" and not vm.commands


def test_long_names_compare_on_a_common_prefix_and_text_only_controls_match() -> None:
    long_name = "Quarterly report " * 20
    vm = FakeVM(trees=[{"controls": [_ctl("atspi:1", long_name[:260], "push-button", [0, 0, 80, 30])]}])
    identity = {"expected_target_identity": {"target_text": long_name[:180]}}
    assert asyncio.run(vm.vision_click_control("atspi:1", 5, 5, **identity))["ok"] is True
    textual = _ctl("atspi:1", "", "push-button", [0, 0, 80, 30], text="Save")
    vm = FakeVM(trees=[{"controls": [textual]}])
    result = asyncio.run(vm.vision_click_control("atspi:7", 5, 5, expected_target_identity={"target_text": "Save"}))
    assert result["ok"] is True and result["rebound_by_point"] is True


def test_type_control_fails_closed_when_the_path_holds_another_field(monkeypatch) -> None:
    monkeypatch.setattr(body_module.time, "sleep", lambda _seconds: None)
    other = _ctl("atspi:3", "Search", "text", [10, 10, 200, 30], editable=True, focused=True)
    identity = {"expected_target_identity": {"target_text": "Name", "target_role": "text"}}
    vm = FakeVM(trees=[{"controls": [other], "focused": other}])
    result = asyncio.run(vm.vision_type_control("atspi:3", "hello", **identity))
    assert result["ok"] is False and result["failure_reason"] == "desktop_uia_target_stale"
    assert result["focus_attempted"] is False and not vm.commands
    name = _ctl("atspi:3", "Name", "text", [10, 10, 200, 30], editable=True, focused=True)
    vm = _typing_vm([{"controls": [name], "focused": name}])
    assert asyncio.run(vm.vision_type_control("atspi:3", "hello", **identity))["ok"] is True


def test_type_refuses_a_button_under_the_point_before_clicking_and_skips_the_click_at_the_caret(monkeypatch) -> None:
    monkeypatch.setattr(body_module.time, "sleep", lambda _seconds: None)
    button = _ctl("atspi:4", "OK", "push-button", [10, 10, 60, 30])
    vm = FakeVM(trees=[{"controls": [button]}])
    result = asyncio.run(vm.vision_type("hello", target_point=[50, 20]))
    assert result["failure_reason"] == "desktop_type_target_not_focused" and result["input_dispatched"] is False
    assert not vm.commands  # no click was fired
    field = _ctl("atspi:3", "Name", "text", [10, 10, 200, 30], editable=True, focused=True)
    vm = FakeVM(trees=[{"controls": [field], "focused": field}])
    asyncio.run(vm.vision_type("hello", at_caret=True, target_point=[50, 20]))
    assert not any("pyautogui.click" in str(c) for c in vm.commands) and any("pyautogui.write" in str(c) for c in vm.commands)


def test_type_on_an_empty_tree_still_types_but_says_focus_was_unverified(monkeypatch) -> None:
    monkeypatch.setattr(body_module.time, "sleep", lambda _seconds: None)
    vm = _typing_vm([{}])
    result = asyncio.run(vm.vision_type("hello", target_point=[50, 20]))
    assert result["ok"] is True and result["focus_unverified"] is True and result["value_read_back"] is False


def test_a_wrong_value_in_the_focused_field_still_counts_as_read_back(monkeypatch) -> None:
    monkeypatch.setattr(body_module.time, "sleep", lambda _seconds: None)
    before = _ctl("atspi:3", "Name", "text", [10, 10, 200, 30], editable=True, focused=True)
    after = {**before, "value": "someone else"}
    vm = _typing_vm([{"controls": [before], "focused": before}, {"controls": [after], "focused": after}])
    result = asyncio.run(vm.vision_type("hello", at_caret=True))
    assert result["value_read_back"] is True and result["active_value"] == "someone else"
    assert result["active_value_matches_expected"] is False
    from app.extension_run import _type_read_back_contradicts
    assert _type_read_back_contradicts({"text": "hello"}, result) is True


def test_an_unnamed_editable_still_matches_after_its_contents_change() -> None:
    before = {"role": "text", "editable": True, "name": "", "text": "", "value": "", "box": [10, 10, 200, 30]}
    after = {**before, "value": "hello world"}
    expected = {"target_text": "", "target_role": "text", "target_box": [10, 10, 200, 30]}
    assert body_module.same_target(after, expected)
    # A text copy of the contents (an older record shape) never names an editable field either.
    assert body_module.same_target({**after, "text": "hello world"}, expected)


def test_the_tree_never_names_an_editable_field_by_its_contents() -> None:
    from tests.test_osworld_adapter import TREE

    parsed = a11y.parse_tree(TREE, screen_width=1920, screen_height=1080)
    field = parsed["focused"]
    assert field["value"] == "hello world" and field["text"] == "" and field["name"] == ""


def test_the_observed_box_is_a_real_position_check_without_a_point() -> None:
    expected = {"target_role": "push-button", "target_box": [100, 100, 40, 30]}
    near = _ctl("atspi:1", "", "push-button", [110, 105, 40, 30])
    far = _ctl("atspi:1", "", "push-button", [900, 700, 40, 30])
    assert body_module.same_target(near, expected) is True
    assert body_module.same_target(far, expected) is False
    assert body_module.same_target(far, {"target_role": "push-button"}) is True


def test_the_observed_identity_records_the_controls_box() -> None:
    from app.extension_run import _observed_control_target_identity

    control = {"tag": "text", "role": "text", "x": 10, "y": 20, "width": 200, "height": 30, "selector": "atspi:3"}
    assert _observed_control_target_identity(control, selector="atspi:3")["target_box"] == [10, 20, 200, 30]
    assert "target_box" not in _observed_control_target_identity({"tag": "div", "selector": "#a"}, selector="#a")


def test_a_calc_like_table_cell_takes_a_type(monkeypatch) -> None:
    monkeypatch.setattr(body_module.time, "sleep", lambda _seconds: None)
    cell = _ctl("atspi:5", "A1", "table-cell", [10, 10, 80, 20], focused=True)
    vm = _typing_vm([{"controls": [cell], "focused": cell}])
    result = asyncio.run(vm.vision_type("42", target_point=[50, 20]))
    assert result["ok"] is True, result


def test_a_type_with_unverified_focus_tells_the_model_to_check_the_screen() -> None:
    from app.extension_run import _vision_result_hint

    assert "check on screen" in _vision_result_hint({"ok": True, "action": "type", "focus_unverified": True})
