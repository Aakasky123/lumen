"""Local Windows computer-use body for Lumen's existing vision loop.

The body exposes raw Windows evidence and deterministic primitives only:

* full virtual-desktop screenshots with native password controls masked;
* visible top-level windows and active-window UI Automation elements;
* exact UIA target rebinding before click/type;
* coordinate mouse input for canvas and other non-UIA surfaces;
* keyboard, wheel, window focus, and exact installed-application launch.

The model remains responsible for deciding what the interface means and what action advances the
goal.  This module never classifies application workflows or chooses a strategy.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import io
import math
import os
import platform
import re
import sys
import threading
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageGrab, ImageOps

from app.config import get_settings
from app import desktop_settle
from app.control_ids import ControlIdRegistry


_CONTROL_LOCK = threading.RLock()
_RUNTIME_ENABLED = True
# run_id -> execution surface ("desktop" runs move the real cursor; "browser" runs are
# the screen overlay's mirror of an extension-driven task).
_ACTIVE_CONTROL_RUN_IDS: dict[int, str] = {}
_POINTER_EVENTS: deque[dict[str, Any]] = deque(maxlen=64)
# The live one-line description of what the active run is doing right now, mirrored onto
# the host's floating pill. Text only, replaced wholesale each step, cleared with the run.
_CONTROL_STATUS_MESSAGE = ""
_POINTER_SEQ = 0
_APP_CACHE: tuple[float, list[dict[str, str]], dict[str, dict[str, str]]] | None = None
# The window this body was last told to act on (focus_window, launch_app, or a click).
# Typing checks the live foreground against it so a value can never be delivered to a
# window the model never targeted.
_LAST_TARGET_WINDOW: int = 0
# Absolute screen point of the last click, so typing can re-assert the caret there.
_LAST_CLICK_POINT: tuple[int, int] | None = None
_MAX_CONTROLS = 140
_MAX_WINDOWS = 40
_PROHIBITED_APP_NAMES = {
    "command prompt",
    "git bash",
    "git cmd",
    "node.js command prompt",
    "powershell",
    "powershell ise",
    "registry editor",
    "run",
    "sql shell (psql)",
    "terminal",
    "windows defender firewall with advanced security",
    "windows powershell",
    "windows powershell (x86)",
    "windows powershell ise",
    "windows powershell ise (x86)",
    "windows terminal",
    "wsl",
}


def _windows_available() -> bool:
    return platform.system().lower() == "windows"


def _configured() -> bool:
    return bool(get_settings().desktop_control_enabled and _windows_available())


def set_runtime_enabled(enabled: bool) -> bool:
    global _RUNTIME_ENABLED, _CONTROL_STATUS_MESSAGE
    with _CONTROL_LOCK:
        _RUNTIME_ENABLED = bool(enabled)
        if not _RUNTIME_ENABLED:
            _ACTIVE_CONTROL_RUN_IDS.clear()
            _POINTER_EVENTS.clear()
            _CONTROL_STATUS_MESSAGE = ""
        return _RUNTIME_ENABLED


def runtime_enabled() -> bool:
    with _CONTROL_LOCK:
        return bool(_RUNTIME_ENABLED and _configured())


def desktop_control_status() -> dict[str, Any]:
    dependency_ready = False
    dependency_error = ""
    if _windows_available():
        try:
            import pywinauto  # noqa: F401
            import win32api  # noqa: F401
            import win32gui  # noqa: F401

            dependency_ready = True
        except Exception as exc:  # noqa: BLE001 - status must remain readable
            dependency_error = str(exc) or exc.__class__.__name__
    configured = _configured()
    with _CONTROL_LOCK:
        control_active = bool(_ACTIVE_CONTROL_RUN_IDS)
        control_run_ids = sorted(_ACTIVE_CONTROL_RUN_IDS)
        control_status_message = _CONTROL_STATUS_MESSAGE
        desktop_surface_active = any(
            surface == "desktop" for surface in _ACTIVE_CONTROL_RUN_IDS.values()
        )
    return {
        "available": bool(configured and dependency_ready),
        "configured": configured,
        "enabled": bool(configured and dependency_ready and runtime_enabled()),
        "control_indicator": {
            "active": bool(
                configured
                and dependency_ready
                and runtime_enabled()
                and control_active
            ),
            "message": control_status_message or "Lumen is using your computer",
            "run_ids": control_run_ids,
            # True only while a run is driving the REAL desktop (mouse/keyboard). The
            # host minimizes Lumen's own window then — the agent must never have to
            # fight the chat window for the screen it is working on.
            "desktop_surface_active": bool(
                configured and dependency_ready and runtime_enabled() and desktop_surface_active
            ),
        },
        "platform": platform.system(),
        "dependency_ready": dependency_ready,
        "dependency_error": dependency_error or None,
        "device_id": DesktopBody.device_id,
        "capabilities": [
            "virtual_desktop_screenshot",
            "password_control_masking",
            "visible_window_inventory",
            "active_window_uia_inventory",
            "exact_uia_target_rebinding",
            "coordinate_click",
            "right_and_middle_click",
            "double_and_triple_click",
            "drag_and_drop",
            "hover",
            "focused_text_entry",
            "long_text_clipboard_paste_with_restore",
            "clipboard_read_of_run_copies",
            "document_text_paste_at_caret_with_restore",
            "full_keyboard_including_editing_and_navigation_keys",
            "keyboard_shortcuts",
            "key_repeat",
            "mouse_wheel",
            "horizontal_scroll",
            "native_resolution_region_zoom",
            "window_focus",
            "installed_application_launch",
            "post_action_visual_verification",
            "emergency_stop",
        ],
        "safety": {
            "consequential_actions": "human_approval_required",
            "dangerous_system_shortcuts": "blocked",
            # Shell commands run only in the WSL sandbox (run_command; app.command_sandbox), never on Windows.
            "shell_execution": "sandboxed_run_command",
            # read_clipboard returns only text this run's own input actions copied, never the user's
            # clipboard, a password control's copy, or a copy its app marked private.
            "raw_clipboard_read": "run_copies_only",
        },
    }


def publish_pointer_event(
    kind: str,
    *,
    x: int | None = None,
    y: int | None = None,
    dy: int | None = None,
    label: str = "",
) -> dict[str, Any]:
    """Record a pointer gesture for the desktop host's on-screen pointer overlay.

    Coordinates are absolute physical virtual-screen pixels. Events carry gesture kind,
    geometry, and a short non-sensitive label only — never typed text or control values.
    """
    global _POINTER_SEQ
    event: dict[str, Any] = {"kind": _safe_text(kind, 24)}
    if x is not None and y is not None:
        event["x"] = int(x)
        event["y"] = int(y)
    if dy is not None:
        event["dy"] = int(dy)
    if label:
        event["label"] = _safe_text(label, 48)
    event["ts"] = time.time()
    with _CONTROL_LOCK:
        _POINTER_SEQ += 1
        event["seq"] = _POINTER_SEQ
        _POINTER_EVENTS.append(event)
    return event


def pointer_events(since: int = 0) -> dict[str, Any]:
    """Return pointer gestures newer than ``since`` for the desktop host to render."""
    try:
        floor = int(since or 0)
    except (TypeError, ValueError):
        floor = 0
    with _CONTROL_LOCK:
        return {
            "last_seq": _POINTER_SEQ,
            "events": [dict(item) for item in _POINTER_EVENTS if int(item.get("seq") or 0) > floor],
        }


def _publish_pointer(
    kind: str,
    *,
    rel_x: int | None = None,
    rel_y: int | None = None,
    abs_x: int | None = None,
    abs_y: int | None = None,
    dy: int | None = None,
    label: str = "",
) -> None:
    """Best-effort pointer feedback: a failure here never affects the action itself."""
    try:
        if rel_x is not None and rel_y is not None:
            left, top, _, _ = _virtual_screen()
            abs_x, abs_y = left + int(rel_x), top + int(rel_y)
        publish_pointer_event(kind, x=abs_x, y=abs_y, dy=dy, label=label)
    except Exception:
        pass


def _require_enabled() -> None:
    status = desktop_control_status()
    if not status["available"]:
        raise RuntimeError("Windows computer use is unavailable in this runtime.")
    if not status["enabled"]:
        raise RuntimeError("Windows computer use is paused.")


_DPI_AWARENESS_SET = False


def _ensure_dpi_awareness() -> None:
    """Make this process per-monitor DPI aware so every coordinate (screen grab, UIA rectangles, cursor,
    SendInput) is in physical pixels. Once per process; the first call wins, and a process that is
    already aware (pywinauto sets it on import) just reports access denied, which is fine."""
    global _DPI_AWARENESS_SET
    if _DPI_AWARENESS_SET or sys.platform != "win32":
        return
    _DPI_AWARENESS_SET = True
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        except Exception:  # noqa: BLE001 - before Windows 8.1, or already set
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001 - awareness is best effort; already-aware processes refuse a second call
        pass


def _desktop() -> Any:
    from pywinauto import Desktop

    return Desktop(backend="uia")


def _active_wrapper() -> Any | None:
    try:
        import win32gui

        handle = int(win32gui.GetForegroundWindow() or 0)
        if not handle:
            return None
        if not win32gui.IsWindowVisible(handle):
            # A hidden window can hold the foreground (a withdrawn app root that owns the visible
            # window). Its controls are not on screen, so none of them may enter the inventory: a
            # point the model aims at a visible window must not bind to a phantom title bar.
            return None
        return _desktop().window(handle=handle).wrapper_object()
    except Exception:
        return None


# The part of the desktop the run looks at and acts in. With several monitors the capture is one monitor,
# not the union of all of them (three side by side shrank every window to a fifth of its size before the
# brain saw it): the monitor the run pinned with focus_window monitor:N, else the one holding the
# foreground window. Chosen when a screenshot is taken and fixed until the next one, so every action's
# coordinates mean the pixels of the last capture. rect None = the whole desktop (one monitor).
_VIEW: dict[str, Any] = {"pinned": None, "rect": None}


def _desktop_union() -> tuple[int, int, int, int]:
    """The whole virtual desktop (every monitor) in physical pixels: left, top, width, height."""
    import win32api
    import win32con

    left = int(win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN))
    top = int(win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN))
    width = int(win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN))
    height = int(win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN))
    return left, top, max(1, width), max(1, height)


def _virtual_screen() -> tuple[int, int, int, int]:
    """The capture region in physical pixels: the view's monitor, or the whole desktop. Capture pixel
    (x, y) is desktop point (left + x, top + y)."""
    rect = _VIEW.get("rect")
    return tuple(rect) if rect else _desktop_union()  # type: ignore[return-value]


def _monitors() -> list[dict[str, Any]]:
    """The connected monitors, numbered 1.. from left to right (then top to bottom), physical pixels."""
    try:
        import win32api

        found = []
        for handle, _dc, _rect in win32api.EnumDisplayMonitors(None, None):
            info = win32api.GetMonitorInfo(handle)
            left, top, right, bottom = (int(value) for value in info["Monitor"])
            if right > left and bottom > top:
                found.append({"rect": (left, top, right - left, bottom - top), "primary": bool(int(info.get("Flags") or 0) & 1)})
        found.sort(key=lambda item: (item["rect"][0], item["rect"][1]))
        return [{"monitor": index, **item} for index, item in enumerate(found, start=1)]
    except Exception:  # noqa: BLE001 - unknown: the whole desktop is the view
        return []


def _monitor_of_box(monitors: list[dict[str, Any]], box: Any) -> int:
    """The monitor showing the largest part of a screen box [left, top, width, height]; 0 when none."""
    try:
        left, top, width, height = (int(value) for value in list(box)[:4])
    except (TypeError, ValueError):
        return 0
    best, best_area = 0, 0
    for item in monitors:
        m_left, m_top, m_width, m_height = item["rect"]
        overlap_w = min(left + width, m_left + m_width) - max(left, m_left)
        overlap_h = min(top + height, m_top + m_height) - max(top, m_top)
        if overlap_w > 0 and overlap_h > 0 and overlap_w * overlap_h > best_area:
            best, best_area = int(item["monitor"]), overlap_w * overlap_h
    return best


def _select_view(monitors: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Choose the view for the next capture (see _VIEW) and return the monitors it was chosen from."""
    monitors = _monitors() if monitors is None else monitors
    if len(monitors) <= 1:
        _VIEW["rect"] = None
        return monitors
    by_index = {int(item["monitor"]): item for item in monitors}
    chosen = by_index.get(int(_VIEW.get("pinned") or 0))
    if chosen is None:
        _VIEW["pinned"] = None
        try:
            import win32gui

            foreground = int(win32gui.GetForegroundWindow() or 0)
            if foreground:
                left, top, right, bottom = win32gui.GetWindowRect(foreground)
                chosen = by_index.get(_monitor_of_box(monitors, [left, top, right - left, bottom - top]))
        except Exception:  # noqa: BLE001 - no foreground window: the primary monitor
            chosen = None
    if chosen is None:
        chosen = next((item for item in monitors if item.get("primary")), monitors[0])
    _VIEW["rect"] = tuple(chosen["rect"])
    return monitors


def _view_crop(union: tuple[int, int, int, int], view: tuple[int, int, int, int] | None) -> tuple[int, int, int, int] | None:
    """The view's box inside a whole-desktop grab whose top-left is the union's origin; None when the
    view is the whole desktop."""
    if not view or tuple(view) == tuple(union):
        return None
    left, top = int(view[0]) - int(union[0]), int(view[1]) - int(union[1])
    return (left, top, left + int(view[2]), top + int(view[3]))


def _safe_text(value: Any, limit: int = 240) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _runtime_id(value: Any) -> tuple[int, ...]:
    try:
        return tuple(int(item) for item in (value or ()))
    except (TypeError, ValueError):
        return ()


def _selector(handle: int, runtime_id: tuple[int, ...]) -> str:
    rid = ".".join(str(item) for item in runtime_id)
    return f"uia:hwnd={int(handle)};rid={rid}"


def _parse_selector(selector: str) -> tuple[int, tuple[int, ...]] | None:
    match = re.fullmatch(r"uia:hwnd=(\d+);rid=([0-9.]+)", str(selector or "").strip())
    if not match:
        return None
    return int(match.group(1)), tuple(int(item) for item in match.group(2).split(".") if item)


def _window_inventory() -> list[dict[str, Any]]:
    import win32gui
    import win32process

    active = int(win32gui.GetForegroundWindow() or 0)
    windows: list[dict[str, Any]] = []

    def visit(hwnd: int, _extra: Any) -> bool:
        if len(windows) >= _MAX_WINDOWS:
            return False
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = _safe_text(win32gui.GetWindowText(hwnd), 180)
            if not title:
                return True
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            if right <= left or bottom <= top:
                return True
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            windows.append(
                {
                    "window_id": f"hwnd:{int(hwnd)}",
                    "title": title,
                    "pid": int(pid or 0),
                    "active": int(hwnd) == active,
                    "box": [int(left), int(top), int(right - left), int(bottom - top)],
                }
            )
        except Exception:
            pass
        return True

    win32gui.EnumWindows(visit, None)
    windows.sort(key=lambda item: (not item["active"], item["title"].casefold()))
    return windows[:_MAX_WINDOWS]


def window_ids_snapshot() -> frozenset[str]:
    """The top-level windows open right now. Taken when a run starts, it tells the brain which windows
    were already open before its task (a stale copy of a page or a dialog from earlier work looks just
    like the one this task opens). Empty where windows cannot be listed."""
    try:
        return frozenset(str(item.get("window_id") or "") for item in _window_inventory() if item.get("window_id"))
    except Exception:
        return frozenset()


# Browser documents report these address schemes, web pages and the browser's own pages alike.
_DOCUMENT_URL_SCHEMES = ("http://", "https://", "file://", "chrome://", "chrome-extension://", "edge://", "about:")
# The shell's own surfaces: the desktop (Program Manager / WorkerW) and the taskbar. Their rectangle is
# the whole screen or its edge, so they never describe one window's content.
_SHELL_WINDOW_CLASSES = frozenset({"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"})
# The window class whose front window reports its folder (folder_view), and how captures name this desktop.
_FOLDER_WINDOW_CLASS = "CabinetWClass"
_SURFACE_URL_PREFIX = "desktop://windows/"
_DESKTOP_TITLE = "Windows desktop"
_WINDOWS_HEADING = "Visible Windows:"


def _document_url(controls: list[dict[str, Any]]) -> str:
    """The address a browser window's document reports through UI Automation (its Document control's
    value), when the active window is a browser showing a page (its own pages included)."""
    for control in controls:
        if str(control.get("tag") or "") != "document":
            continue
        value = str(control.get("value") or "").strip()
        if value.lower().startswith(_DOCUMENT_URL_SCHEMES):
            return value[:400]
    return ""


def _window_class(window_id: Any) -> str:
    try:
        import win32gui

        return str(win32gui.GetClassName(int(str(window_id or "").split(":", 1)[1])) or "")
    except Exception:  # noqa: BLE001 - unknown class: no claim
        return ""


# A File Explorer window's folder (folder_view): at most this many item names reach the model.
_FOLDER_VIEW_MAX_ITEMS = 60


def _explorer_folder_view(window_id: Any, title: str) -> dict[str, Any]:
    """The folder a File Explorer window shows and its items' real file names, read from the Windows
    Shell. The window itself may hide known extensions (run 1663 renamed a scan to
    "passport-6EBW.png.png", which the window showed as "passport-6EBW.png"), so its displayed names
    are not the files' names. A window with several tabs reports the tab its title names; a virtual
    folder (Home, This PC) or a shell that will not say reports nothing."""
    try:
        import pythoncom
        import win32com.client

        hwnd = int(str(window_id or "").split(":", 1)[1])
        pythoncom.CoInitialize()
        candidates = []
        for window in win32com.client.Dispatch("Shell.Application").Windows():
            try:
                if int(window.HWND) == hwnd:
                    candidates.append(window)
            except Exception:  # noqa: BLE001 - a closing window
                continue
        if len(candidates) > 1:
            shown = str(title or "").rsplit(" - File Explorer", 1)[0].strip()
            candidates = [
                window
                for window in candidates
                if shown and shown in {str(window.LocationName or ""), str(window.Document.Folder.Self.Path or "")}
            ]
        if len(candidates) != 1:
            return {}
        folder = candidates[0].Document.Folder
        path = str(folder.Self.Path or "")
        if not os.path.isdir(path):
            return {}
        items = folder.Items()
        count = int(items.Count)
        names: list[str] = []
        for index in range(min(count, _FOLDER_VIEW_MAX_ITEMS)):
            item = items.Item(index)
            item_path = str(item.Path or "")
            names.append(os.path.basename(item_path) if os.path.isabs(item_path) else str(item.Name or ""))
        return {"path": path, "items": names, "item_count": count}
    except Exception:  # noqa: BLE001 - no folder view is the safe answer
        return {}


# The capture cannot show what is drawn over a window; this marks a window whose stacking is unknown.
_STACKING_UNKNOWN = [[-1, -1, 0, 0]]


def _window_drawn_in_capture(hwnd: int) -> bool:
    """Whether a visible window actually contributes pixels to a screen capture: DWM does not draw a
    cloaked window (a hidden UWP frame, another desktop's window), and a window excluded from capture
    (Lumen's own overlays) never appears in it. When either check fails the window counts (fail closed)."""
    import ctypes
    from ctypes import wintypes

    try:
        cloaked = wintypes.DWORD(0)
        result = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            ctypes.c_void_p(hwnd), 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked)
        )
        if result == 0 and cloaked.value:
            return False
    except Exception:  # noqa: BLE001 - unknown: the window counts
        pass
    try:
        affinity = wintypes.DWORD(0)
        if ctypes.windll.user32.GetWindowDisplayAffinity(ctypes.c_void_p(hwnd), ctypes.byref(affinity)) and affinity.value == 0x11:
            return False
    except Exception:  # noqa: BLE001 - unknown: the window counts
        pass
    return True


def _windows_drawn_over(window_id: Any, box: list[int], origin: tuple[int, int]) -> list[list[int]]:
    """Image rectangles of the windows stacked above this one that overlap it and are drawn in a capture
    (a notification, an always-on-top tool): whatever they draw inside its rectangle is not its own
    content. Unknown or too-deep stacking returns _STACKING_UNKNOWN, so the window is never read."""
    try:
        import win32con
        import win32gui

        hwnd = int(str(window_id or "").split(":", 1)[1])
        left, top, width, height = (int(item) for item in box)
        right, bottom = left + width, top + height
        covering: list[list[int]] = []
        above = win32gui.GetWindow(hwnd, win32con.GW_HWNDPREV)
        for _ in range(1024):
            if not above:
                break
            if win32gui.IsWindowVisible(above) and _window_drawn_in_capture(int(above)):
                a_left, a_top, a_right, a_bottom = win32gui.GetWindowRect(above)
                if a_right > left and a_left < right and a_bottom > top and a_top < bottom and a_right > a_left and a_bottom > a_top:
                    covering.append([a_left - origin[0], a_top - origin[1], a_right - a_left, a_bottom - a_top])
            above = win32gui.GetWindow(above, win32con.GW_HWNDPREV)
        if above or len(covering) > 32:
            return list(_STACKING_UNKNOWN)
        return covering
    except Exception:  # noqa: BLE001 - unknown stacking: callers treat the window as unreadable
        return list(_STACKING_UNKNOWN)


def _foreground_stacking() -> tuple[str, list[int], list[list[int]]]:
    """The window in front, its screen rectangle, and what is drawn over it, read now."""
    try:
        import win32gui

        hwnd = int(win32gui.GetForegroundWindow() or 0)
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        box = [int(left), int(top), int(right - left), int(bottom - top)]
        origin_left, origin_top, _, _ = _virtual_screen()
        return f"hwnd:{hwnd}", box, _windows_drawn_over(f"hwnd:{hwnd}", box, (origin_left, origin_top))
    except Exception:  # noqa: BLE001 - unknown stacking
        return "", [], list(_STACKING_UNKNOWN)


def _control_affordances(wrapper: Any) -> dict[str, Any]:
    """Raw UIA pattern evidence for one control: which native actions it supports and
    its current toggle/selection/expand state.

    Pure enumeration - the model decides what any of it means. Before this, the brain
    could not see that a checkbox was already ticked (toggle state was only probed AFTER
    a click) or that a control accepts text at all, so it typed into windows with no
    caret and re-clicked already-correct checkboxes.
    """
    facts: dict[str, Any] = {}
    affordances: list[str] = []
    try:
        toggle_state = int(wrapper.iface_toggle.CurrentToggleState)
        affordances.append("toggle")
        if toggle_state in (0, 1):
            facts["checked"] = bool(toggle_state)
    except Exception:
        pass
    try:
        facts["selected"] = bool(wrapper.iface_selection_item.CurrentIsSelected)
        affordances.append("select")
    except Exception:
        pass
    try:
        expand_state = int(wrapper.iface_expand_collapse.CurrentExpandCollapseState)
        # 0 collapsed, 1 expanded, 2 partially expanded, 3 leaf (pattern present but inert).
        if expand_state in (0, 1, 2):
            affordances.append("expand_collapse")
            if expand_state in (0, 1):
                facts["expanded"] = expand_state == 1
    except Exception:
        pass
    try:
        if not bool(wrapper.iface_value.CurrentIsReadOnly):
            affordances.append("set_value")
            facts["accepts_text"] = True
    except Exception:
        pass
    try:
        wrapper.iface_invoke  # noqa: B018 - raises when the pattern is unsupported
        affordances.append("invoke")
    except Exception:
        pass
    try:
        wrapper.iface_scroll  # noqa: B018 - raises when the pattern is unsupported
        affordances.append("scroll")
    except Exception:
        pass
    if affordances:
        facts["affordances"] = affordances
    return facts


def _clickable_point(wrapper: Any) -> tuple[int, int] | None:
    """The element's own UIA-declared clickable screen point, else its rect center.

    GetClickablePoint accounts for occlusion and irregular shapes; a rect center can
    land on an overlapping sibling or the label half of a composite row. Absolute
    virtual-screen pixels."""
    rectangle = None
    try:
        rectangle = wrapper.element_info.rectangle
    except Exception:
        rectangle = None
    try:
        point, got = wrapper.element_info.element.GetClickablePoint()
        if got:
            x, y = int(point.x), int(point.y)
            if rectangle is None or (
                rectangle.left <= x <= rectangle.right and rectangle.top <= y <= rectangle.bottom
            ):
                return x, y
    except Exception:
        pass
    if rectangle is None:
        return None
    return (
        int((rectangle.left + rectangle.right) / 2),
        int((rectangle.top + rectangle.bottom) / 2),
    )


def _smallest_affording_control(root: Any, abs_x: int, abs_y: int) -> Any | None:
    """The smallest visible, enabled control containing the point that supports a real
    UIA action pattern (invoke/toggle/select/expand/set_value/scroll).

    Geometry plus raw pattern evidence only - no meaning is assigned to any control."""
    best = None
    best_area = None
    try:
        candidates = [root, *root.descendants()]
    except Exception:
        return None
    for wrapper in candidates:
        try:
            info = wrapper.element_info
            if not bool(info.visible) or not bool(info.enabled):
                continue
            rectangle = info.rectangle
            if not (rectangle.left <= abs_x <= rectangle.right and rectangle.top <= abs_y <= rectangle.bottom):
                continue
            area = max(1, (rectangle.right - rectangle.left) * (rectangle.bottom - rectangle.top))
            if best_area is not None and area >= best_area:
                continue
            if not _control_affordances(wrapper).get("affordances"):
                continue
            best, best_area = wrapper, area
        except Exception:
            continue
    return best


def _exact_click_target(rel_x: int, rel_y: int) -> dict[str, Any]:
    """Resolve a model-chosen point to the exact clickable point of the control under it.

    Vision-model coordinates are approximate: a point inside a checkbox row can miss the
    glyph, and a point inside a button can land on its border. Snapping stays WITHIN the
    control the model aimed at (the snapped point is inside the same element), so this is
    deterministic click binding, not target reinterpretation. Falls back to the raw point
    when nothing affording contains it."""
    left, top, _, _ = _virtual_screen()
    abs_x, abs_y = left + int(rel_x), top + int(rel_y)
    resolution: dict[str, Any] = {"x": int(rel_x), "y": int(rel_y), "snap_applied": False}
    try:
        import win32con
        import win32gui

        child = win32gui.WindowFromPoint((abs_x, abs_y))
        if not child:
            return resolution
        top_level = win32gui.GetAncestor(child, win32con.GA_ROOT)
        if not top_level:
            return resolution
        resolution["top_level_handle"] = int(top_level)
        root = _desktop().window(handle=int(top_level)).wrapper_object()
        target = _smallest_affording_control(root, abs_x, abs_y)
        if target is None:
            return resolution
        point = _clickable_point(target)
        if point is None:
            return resolution
        info = target.element_info
        resolution.update(
            {
                "x": point[0] - left,
                "y": point[1] - top,
                "snap_applied": (point[0] - left, point[1] - top) != (int(rel_x), int(rel_y)),
                "clicked_control_type": _safe_text(info.control_type, 60),
                "clicked_control_name": _safe_text(info.name, 120),
                "control_id": _runtime_id(getattr(info, "runtime_id", None)),
            }
        )
        return resolution
    except Exception:
        return resolution


def _control_value(wrapper: Any, *, password: bool) -> str:
    if password:
        return ""
    for accessor in (
        lambda: wrapper.iface_value.CurrentValue,
        lambda: wrapper.get_value(),
        # Rich-text documents (Notepad's RichEditD2DPT, WordPad, most editors) expose
        # their content only through TextPattern; ValuePattern reads back empty, which
        # blinded type verification into inconclusive retype loops.
        lambda: wrapper.iface_text.DocumentRange.GetText(4096),
        lambda: wrapper.window_text(),
    ):
        try:
            value = _safe_text(accessor(), 240)
            if value:
                return value
        except Exception:
            continue
    return ""


def _uia_hit_test(x: int, y: int) -> Any | None:
    try:
        from pywinauto.uia_element_info import UIAElementInfo

        return UIAElementInfo.from_point(int(x), int(y))
    except Exception:
        return None


def _point_hits_element(x: int, y: int, wrapper: Any, hit_test: Any = _uia_hit_test) -> bool:
    """Whether the OS hit-test at a screen point lands on this element (or inside it)."""
    try:
        target = _runtime_id(wrapper.element_info.runtime_id)
    except Exception:
        return False
    try:
        node, hops = hit_test(x, y), 0
        while node is not None and hops < 80 and target:
            if _runtime_id(getattr(node, "runtime_id", None)) == target:
                return True
            node, hops = getattr(node, "parent", None), hops + 1
    except Exception:
        # A UIA walk can fail mid-way (an element destroyed under the probe): no proof of a hit.
        return False
    return False


def _element_in_window(wrapper: Any, window_handle: int) -> bool:
    """Whether an element sits inside this top-level window (its UIA ancestors reach it)."""
    try:
        node, hops = wrapper.element_info, 0
        while node is not None and hops < 64:
            if int(getattr(node, "handle", 0) or 0) == int(window_handle):
                return True
            node, hops = node.parent, hops + 1
    except Exception:
        return False
    return False


def _hidden_document_ids(window: Any, hit_test: Any = _uia_hit_test) -> set[tuple[int, ...]]:
    """Documents a window exposes but does not show: Chrome's UIA tree keeps a background tab's page
    as a second "visible" Document over the same screen area. From the brain seat on v3_paperwork, an
    x,y click on the live page bound to "Mirror buffer", a field of another tab's page, and the
    consequence gate judged that control. The OS hit-test at a document's on-screen centre lands in
    the document actually shown. Fails open: unless at least one document is confirmed shown, none
    is reported hidden (an overlay or dialog over the probe point proves nothing)."""
    try:
        documents = list(window.descendants(control_type="Document"))
        bounds = window.element_info.rectangle
    except Exception:
        return set()
    if len(documents) < 2:
        return set()
    shown: set[tuple[int, ...]] = set()
    hidden: set[tuple[int, ...]] = set()
    for document in documents:
        try:
            info = document.element_info
            document_id = _runtime_id(info.runtime_id)
            rect = info.rectangle
            left, top = max(rect.left, bounds.left), max(rect.top, bounds.top)
            right, bottom = min(rect.right, bounds.right), min(rect.bottom, bounds.bottom)
            if not document_id:
                continue
            if right <= left or bottom <= top:
                hidden.add(document_id)
                continue
            node, hops = hit_test((left + right) // 2, (top + bottom) // 2), 0
            inside = False
            while node is not None and hops < 80:
                if _runtime_id(getattr(node, "runtime_id", None)) == document_id:
                    inside = True
                    break
                node, hops = getattr(node, "parent", None), hops + 1
            (shown if inside else hidden).add(document_id)
        except Exception:
            continue
    return hidden if shown else set()


_MAX_POPUP_WINDOWS = 3
# Always-on-top shell windows of explorer.exe (the taskbars, the desktop), stacked above File Explorer.
_SHELL_WINDOW_CLASSES = frozenset({"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW"})


def _popup_windows_above(active_handle: int) -> list[int]:
    """Visible popup windows of the front window's process that sit above it, topmost first.

    An open dropdown list (a Tk combobox's "popdown"), a context menu, or a menu is its own top-level
    window, so the front window's tree never holds its items: v5_memory run 1723 opened the Route list,
    had no control for "North", aimed by eye 18 px too high, and closed the list on the combobox.
    """
    try:
        import win32con
        import win32gui
        import win32process

        _, active_pid = win32process.GetWindowThreadProcessId(active_handle)
        found: list[int] = []
        hwnd = win32gui.GetTopWindow(None)
        while hwnd and hwnd != active_handle:
            try:
                if win32gui.IsWindowVisible(hwnd):
                    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                    # Any window style: a Tk list is an override-redirect top-level Windows flags WS_CHILD.
                    # The shell's own bars share explorer.exe with File Explorer: never a popup of it.
                    shell = win32gui.GetClassName(hwnd) in _SHELL_WINDOW_CLASSES
                    if pid == active_pid and not shell and right > left and bottom > top and len(found) < _MAX_POPUP_WINDOWS:
                        found.append(int(hwnd))
            except Exception:
                pass
            hwnd = win32gui.GetWindow(hwnd, win32con.GW_HWNDNEXT)
        # Reached the end without passing the front window: stacking unknown, list nothing.
        return found if hwnd == active_handle else []
    except Exception:
        return []


def _active_controls() -> tuple[list[dict[str, Any]], list[tuple[int, int, int, int]]]:
    active = _active_wrapper()
    if active is None:
        return [], []
    try:
        window_handle = int(active.handle)
        elements = [active, *active.descendants()]
    except Exception:
        return [], []
    hidden_documents = _hidden_document_ids(active)
    excluded: set[tuple[int, ...]] = set()
    if hidden_documents:
        for wrapper in elements:
            try:
                if _runtime_id(wrapper.element_info.runtime_id) in hidden_documents:
                    excluded.add(_runtime_id(wrapper.element_info.runtime_id))
                    excluded.update(_runtime_id(child.element_info.runtime_id) for child in wrapper.descendants())
            except Exception:
                continue
        elements = [
            wrapper for wrapper in elements if _runtime_id(getattr(wrapper.element_info, "runtime_id", None)) not in excluded
        ]

    left, top, _, _ = _virtual_screen()
    controls: list[dict[str, Any]] = []
    password_boxes: list[tuple[int, int, int, int]] = []
    # The control Windows reports as focused: some editors (Notepad's page) never set their own
    # keyboard-focus flag, so the flag alone left the inventory without a focused control.
    os_focused = _os_focused_wrapper()
    try:
        os_focused_id = _runtime_id(os_focused.element_info.runtime_id) if os_focused is not None else ()
    except Exception:
        os_focused_id = ()
    collected: set[tuple[int, ...]] = set()

    def _collect(wrapper: Any, handle: int = window_handle) -> None:
        try:
            info = wrapper.element_info
            rectangle = info.rectangle
            width = int(rectangle.right - rectangle.left)
            height = int(rectangle.bottom - rectangle.top)
            if width <= 0 or height <= 0 or not bool(info.visible):
                return
            runtime_id = _runtime_id(info.runtime_id)
            if not runtime_id:
                return
            password = bool(getattr(info, "is_password", False))
            box = [
                int(rectangle.left - left),
                int(rectangle.top - top),
                width,
                height,
            ]
            name = _safe_text(info.name, 260)
            control_type = _safe_text(info.control_type, 80)
            class_name = _safe_text(info.class_name, 120)
            controls.append(
                {
                    "selector": _selector(handle, runtime_id),
                    "tag": control_type.casefold(),
                    "role": control_type,
                    "type": class_name,
                    "text": name,
                    "name": name,
                    "accessible_name": name,
                    "value": _control_value(wrapper, password=password),
                    "box": box,
                    # Flattened geometry keys are the body contract the shared control
                    # inventory/index machinery consumes; box remains for masking.
                    "x": box[0],
                    "y": box[1],
                    "width": box[2],
                    "height": box[3],
                    "visible": True,
                    "enabled": bool(info.enabled),
                    "disabled": not bool(info.enabled),
                    "focused": bool(getattr(info, "has_keyboard_focus", False))
                    or (bool(os_focused_id) and runtime_id == os_focused_id),
                    "password": password,
                    "automation_id": _safe_text(info.automation_id, 160),
                    "framework_id": _safe_text(info.framework_id, 80),
                    **_control_affordances(wrapper),
                }
            )
            collected.add(runtime_id)
            if password:
                password_boxes.append((box[0], box[1], box[0] + box[2], box[1] + box[3]))
        except Exception:
            return

    for wrapper in elements:
        if len(controls) >= _MAX_CONTROLS:
            break
        _collect(wrapper)
    if (
        os_focused is not None
        and os_focused_id
        and os_focused_id not in collected
        and os_focused_id not in excluded
        and _element_in_window(os_focused, window_handle)
    ):
        # The control holding keyboard focus is where typing goes: it stays listed when the cap cut
        # the walk short (File Explorer's Home view filled every slot before its focused address bar,
        # so a type bound to an empty pane over it). It is added past the cap, never in place of a
        # listed control (whose sensitive mask would go with it).
        _collect(os_focused)
    for popup_handle in _popup_windows_above(window_handle):
        # An open list or menu over the front window: its items are clickable controls too.
        try:
            popup = _desktop().window(handle=popup_handle).wrapper_object()
            popup_elements = [popup, *popup.descendants()]
        except Exception:
            continue
        for wrapper in popup_elements:
            if len(controls) >= _MAX_CONTROLS + 40:
                break
            _collect(wrapper, popup_handle)
    controls.sort(key=lambda item: ((item.get("box") or [0, 0])[1], (item.get("box") or [0, 0])[0]))
    for position, control in enumerate(controls, start=1):
        control["index"] = position
    return controls, password_boxes


def _restore_caret_at_last_click() -> bool:
    """Put keyboard focus back on the text control the model last clicked.

    Seconds pass between the model's click and its type while it thinks, and windows that
    appear in between (including this app's own always-on-top overlays) can move focus off
    the caret. Refocusing the exact element under the last click is side-effect free -
    unlike clicking again, which would re-trigger a button.
    """
    with _CONTROL_LOCK:
        point = _LAST_CLICK_POINT
        target = int(_LAST_TARGET_WINDOW)
    if not point or not target:
        return False
    try:
        # Resolve inside the target window rather than by screen point: this app's own
        # always-on-top overlays sit above the click point, so a point lookup would
        # return the overlay instead of the text control underneath it.
        root = _desktop().window(handle=target).wrapper_object()
        x, y = point
        best = None
        best_area = None
        for wrapper in [root, *root.descendants()]:
            try:
                info = wrapper.element_info
                if not bool(info.visible) or not bool(info.enabled):
                    continue
                if _safe_text(info.control_type, 40).casefold() not in {
                    "edit",
                    "document",
                    "combobox",
                }:
                    continue
                rectangle = info.rectangle
                if not (rectangle.left <= x <= rectangle.right and rectangle.top <= y <= rectangle.bottom):
                    continue
                area = (rectangle.right - rectangle.left) * (rectangle.bottom - rectangle.top)
                if area > 0 and (best_area is None or area < best_area):
                    best, best_area = wrapper, area
            except Exception:
                continue
        if best is None:
            return False
        best.set_focus()
        return True
    except Exception:
        return False


_LAST_FOCUS_REPAIR: dict[str, Any] = {"result": ""}


def _focus_text_control_within_focus() -> bool:
    """Move keyboard focus into the one text control inside the focused element.

    An editor can hold focus on its container rather than on its text area (Notepad's NotepadTextBox
    pane around the RichEdit page): keys like Select All still reach the text through the app's
    accelerators, but typed characters are dropped. Focusing the single text control inside puts
    the caret back where typing lands, keeping its selection. When the focused element holds none,
    the foreground window's single text control is used. The outcome is recorded for diagnosis.
    """

    def text_controls(root: Any) -> list[Any]:
        found: list[Any] = []
        for wrapper in root.descendants():
            try:
                info = wrapper.element_info
                if _safe_text(info.control_type, 40).casefold() in {"document", "edit"} and bool(info.enabled):
                    found.append(wrapper)
            except Exception:
                continue
        return found

    focused = _os_focused_wrapper()
    if focused is None:
        _LAST_FOCUS_REPAIR["result"] = "no_focused_element"
        return False
    try:
        inner = text_controls(focused)
        source = "inside_focus"
        if not inner:
            active = _active_wrapper()
            inner = text_controls(active) if active is not None else []
            source = "foreground_window"
        if len(inner) != 1:
            _LAST_FOCUS_REPAIR["result"] = f"{source}:{len(inner)}_text_controls"
            return False
        inner[0].set_focus()
        for _ in range(8):
            time.sleep(0.08)
            if _focused_state().get("typable"):
                _LAST_FOCUS_REPAIR["result"] = f"{source}:focused"
                return True
        _LAST_FOCUS_REPAIR["result"] = f"{source}:focus_did_not_move"
        return False
    except Exception as exc:  # noqa: BLE001 - diagnosis only
        _LAST_FOCUS_REPAIR["result"] = f"error:{exc.__class__.__name__}"
        return False


_TYPABLE_TAGS = {"edit", "document", "combobox"}
# Controls whose focus never takes typed text: keys there press, toggle, select, or navigate. Containers
# that hold no text of their own (Pane, Custom, Window, Group) are not listed: a Tk entry or a painted
# editor reports its focus as one of those, so they are unknown, not "not typable".
_NON_TEXT_CONTROL_TYPES = frozenset(
    {
        "button",
        "splitbutton",
        "checkbox",
        "radiobutton",
        "hyperlink",
        "menu",
        "menubar",
        "menuitem",
        "tab",
        "tabitem",
        "list",
        "listitem",
        "tree",
        "treeitem",
        "header",
        "headeritem",
        "scrollbar",
        "slider",
        "thumb",
        "titlebar",
        "toolbar",
        "statusbar",
        "progressbar",
        "separator",
        "tooltip",
        "image",
    }
)


def _foreground_caret_present() -> bool | None:
    """Whether the foreground window's GUI thread shows a Win32 caret (GetGUIThreadInfo's hwndCaret).

    Apps whose text widgets UI Automation sees only as a pane (Tk, painted editors) still create the
    system caret while one of them holds focus. None when Windows will not say.
    """
    try:
        import ctypes
        from ctypes import wintypes

        class GUITHREADINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("hwndActive", wintypes.HWND),
                ("hwndFocus", wintypes.HWND),
                ("hwndCapture", wintypes.HWND),
                ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND),
                ("hwndCaret", wintypes.HWND),
                ("rcCaret", wintypes.RECT),
            ]

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(GUITHREADINFO)]
        user32.GetGUIThreadInfo.restype = wintypes.BOOL
        foreground = user32.GetForegroundWindow()
        if not foreground:
            return None
        thread_id = int(user32.GetWindowThreadProcessId(foreground, None))
        if not thread_id:
            return None
        info = GUITHREADINFO()
        info.cbSize = ctypes.sizeof(GUITHREADINFO)
        if not user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
            return None
        return bool(info.hwndCaret)
    except Exception:
        return None


def _typability(control_type: str) -> bool | None:
    """Whether typed text would land in the focused element: True for a text control or wherever a
    caret blinks, False for a control that never takes text, None (unknown) otherwise."""
    kind = str(control_type or "").casefold()
    if kind in _TYPABLE_TAGS:
        return True
    if kind in _NON_TEXT_CONTROL_TYPES:
        return False
    return True if _foreground_caret_present() else None


def _typable_receipt(state: dict[str, Any] | None) -> bool | None:
    """The focused element's typability for an action receipt: True, False, or None when unknown
    (including when focus could not be read at all)."""
    value = state.get("typability") if isinstance(state, dict) else None
    return value if isinstance(value, bool) else None


def _readable_active_values() -> list[str]:
    """Non-secret text currently visible in the active window's controls.

    Typed values are verified by comparing this before and after the keystrokes: the
    focused element's own value is unreliable (rich editors report nothing) and a screen
    diff proves nothing (a blinking caret repaints the screen on its own).
    """
    try:
        controls, _ = _active_controls()
    except Exception:
        return []
    # Only text controls count: a title bar, menu, or pane that exposes some value says nothing about
    # whether the field that had focus took the keystrokes.
    return [
        str(item.get("value") or "")
        for item in controls
        if not item.get("password")
        and str(item.get("tag") or "").casefold() in _TYPABLE_TAGS
        and str(item.get("value") or "")
    ]


def _element_box(element_info: Any) -> list[int] | None:
    """An element's rectangle [left, top, right, bottom] in capture pixels."""
    try:
        rectangle = element_info.rectangle
        left, top, _, _ = _virtual_screen()
        if rectangle.right <= rectangle.left or rectangle.bottom <= rectangle.top:
            return None
        return [int(rectangle.left - left), int(rectangle.top - top), int(rectangle.right - left), int(rectangle.bottom - top)]
    except Exception:
        return None


# How far outside the focused control the point a type names may lie (capture pixels): its border.
_TYPE_POINT_SLACK = 12
# How near the last click must be to the point a type names when focus cannot be read (capture pixels).
_TYPE_CLICK_REACH = 40


def _element_center(element_info: Any) -> list[int] | None:
    """An element's centre in capture pixels (relative to the virtual screen's top-left)."""
    try:
        rectangle = element_info.rectangle
        left, top, _, _ = _virtual_screen()
        if rectangle.right <= rectangle.left or rectangle.bottom <= rectangle.top:
            return None
        return [int((rectangle.left + rectangle.right) / 2 - left), int((rectangle.top + rectangle.bottom) / 2 - top)]
    except Exception:
        return None


def _focus_summary(state: dict[str, Any] | None) -> str:
    """Value-free: the focused element's control type and class (\"Document/RichEditD2DPT\")."""
    state = state if isinstance(state, dict) else {}
    if not state:
        return "none"
    return f"{state.get('control_type') or '?'}/{state.get('class_name') or '?'}"


def _os_focused_wrapper() -> Any | None:
    """The element Windows reports as holding keyboard focus (IUIAutomation.GetFocusedElement)."""
    for attempt in range(2):
        try:
            from pywinauto.controls.uiawrapper import UIAWrapper
            from pywinauto.uia_defines import IUIA
            from pywinauto.uia_element_info import UIAElementInfo

            element = IUIA().iuia.GetFocusedElement()
            return UIAWrapper(UIAElementInfo(element)) if element is not None else None
        except Exception:
            if attempt:
                return None
            # A thread that has not joined COM cannot call UI Automation: join it and retry once.
            try:
                import pythoncom

                pythoncom.CoInitialize()
            except Exception:
                return None
    return None


def _focus_identity() -> tuple[Any, ...] | None:
    """UI Automation runtime id of the element holding keyboard focus; None when Windows will not
    say. Click receipts compare it before and after instead of asserting that focus moved."""
    try:
        focused = _os_focused_wrapper()
        runtime_id = getattr(getattr(focused, "element_info", None), "runtime_id", None) if focused is not None else None
        return tuple(runtime_id) if runtime_id else None
    except Exception:
        return None


def _focus_change(before: tuple[Any, ...] | None, after: tuple[Any, ...] | None) -> bool | None:
    if before is None and after is None:
        return None
    return before != after


def _focused_state() -> dict[str, Any]:
    try:
        # Ask Windows which element has focus. Searching the active window's descendants for a
        # keyboard-focus flag misses editors that do not report it (Notepad's page), which left
        # focus, caret, and document checks blind there.
        focused = _os_focused_wrapper()
        if focused is None:
            from pywinauto.findwindows import find_element

            info = find_element(active_only=True, backend="uia")
            active = _desktop().window(handle=info.handle).wrapper_object()
            focused = next(
                (
                    wrapper
                    for wrapper in [active, *active.descendants()]
                    if bool(getattr(wrapper.element_info, "has_keyboard_focus", False))
                ),
                None,
            )
        if focused is None:
            return {}
        element_info = focused.element_info
        control_type = _safe_text(element_info.control_type, 80)
        password = bool(getattr(element_info, "is_password", False))
        return {
            "control_type": control_type,
            "class_name": _safe_text(getattr(element_info, "class_name", ""), 80),
            "name": _safe_text(element_info.name, 180),
            "value": _control_value(focused, password=password),
            "password": password,
            # A text control by UI Automation type: the body's own focus repairs key off this.
            "typable": control_type.casefold() in _TYPABLE_TAGS,
            # What action receipts report: True, False, or None (unknown), see _typability. Never used to skip a
            # focus repair: a caret can linger while focus sits on an editor's container (Notepad's pane).
            "typability": _typability(control_type),
            # A multi-line editor's page (Notepad, WordPad): typing there goes in at the caret.
            "document": control_type.casefold() == "document",
            "runtime_id": _runtime_id(element_info.runtime_id),
            "center": _element_center(element_info),
            "box": _element_box(element_info),
        }
    except Exception:
        return {}


def _caret_state() -> dict[str, Any]:
    """Where the caret is in the focused text control: its line's text, column, and selection size.

    Read through UI Automation's TextPattern (Notepad, rich editors, most edit controls). A caret
    click barely changes the screen, so this is how the model sees that the caret landed and where.
    Empty when the focused element exposes no text pattern or is a password field.
    """
    try:
        from pywinauto.uia_defines import IUIA

        uia = IUIA()
        dll = uia.UIA_dll
        element = uia.iuia.GetFocusedElement()
        if element is None or bool(element.CurrentIsPassword):
            return {}
        pattern = element.GetCurrentPattern(dll.UIA_TextPatternId)
        if not pattern:
            return {}
        text_pattern = pattern.QueryInterface(dll.IUIAutomationTextPattern)
        selection = text_pattern.GetSelection()
        if selection is None or selection.Length < 1:
            return {}
        selected = selection.GetElement(0)
        selected_text = str(selected.GetText(4096) or "")
        line = selected.Clone()
        line.ExpandToEnclosingUnit(dll.TextUnit_Line)
        line_text = str(line.GetText(400) or "")
        prefix = line.Clone()
        prefix.MoveEndpointByRange(
            dll.TextPatternRangeEndpoint_End, selected, dll.TextPatternRangeEndpoint_Start
        )
        return {
            "line_text": _safe_text(line_text.rstrip("\r\n"), 200),
            "column": len(str(prefix.GetText(400) or "")),
            "selection_length": len(selected_text),
            # Exactly what is selected, spaces included (not whitespace-collapsed): whether a leading
            # space or the label's colon is part of what a type at the caret would replace.
            "selected_text": selected_text[:200],
            "document": int(element.CurrentControlType) == int(dll.UIA_DocumentControlTypeId),
        }
    except Exception:
        return {}


# The focused multi-line text control's text as the model reads it (focused_text_lines): at most this
# many lines and characters, each line cut past _TEXT_LINE_CHARS; the whole text is read up to the cap.
_TEXT_LINES_MAX_LINES = 40
_TEXT_LINES_MAX_CHARS = 3000
_TEXT_LINE_CHARS = 400
_FOCUSED_TEXT_READ_CAP = 200_000


def text_line_window(
    text: Any,
    *,
    caret_line: int | None = None,
    control: Any = None,
    max_lines: int = _TEXT_LINES_MAX_LINES,
    max_chars: int = _TEXT_LINES_MAX_CHARS,
    line_chars: int = _TEXT_LINE_CHARS,
) -> dict[str, Any]:
    """A multi-line text as numbered ``N|text`` lines: all of them when they fit, else the lines around
    ``caret_line`` (1-based; the first lines when it is unknown). Raw text: spaces and underscores are
    kept, ``\\r\\n``/``\\r`` count as line breaks, and only a line past ``line_chars`` is cut. Empty for
    a text without a line break (the control's value already shows it whole)."""
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if "\n" not in normalized:
        return {}
    lines = normalized.split("\n")
    shown = [
        line if len(line) <= line_chars else f"{line[:line_chars]}...[+{len(line) - line_chars} chars]"
        for line in lines
    ]
    count = len(shown)
    try:
        caret = int(caret_line) if caret_line is not None else 0
    except (TypeError, ValueError):
        caret = 0
    anchor = min(max(caret, 1), count) - 1 if caret > 0 else 0
    start, end = anchor, anchor + 1
    used = len(shown[anchor]) + 1
    while end - start < max_lines:
        grew = False
        if end < count and used + len(shown[end]) + 1 <= max_chars:
            used += len(shown[end]) + 1
            end += 1
            grew = True
        if end - start < max_lines and start > 0 and used + len(shown[start - 1]) + 1 <= max_chars:
            start -= 1
            used += len(shown[start]) + 1
            grew = True
        if not grew:
            break
    view: dict[str, Any] = {
        "line_count": count,
        "lines": [f"{number + 1}|{shown[number]}" for number in range(start, end)],
    }
    if caret > 0:
        view["caret_line"] = anchor + 1
    if control is not None:
        view = {"control": control, **view}
    return view


# A Document control whose value is one of these is a browser's web content (its value is the address).
_WEB_DOCUMENT_PREFIXES = ("http://", "https://", "file://")
_WEB_DOCUMENT_ANCESTOR_LIMIT = 64


def _inside_web_document(uia: Any, dll: Any, element: Any) -> bool:
    """Whether a UI Automation element is a browser's web content: it or one of its ancestors is a
    Document whose value is a web address. There the page's own sensitivity marks (a recovery phrase,
    a pasted key in a textarea) are not visible to the desktop body, so its text is never read out
    here. When the tree cannot be walked, the element counts as web content."""
    document_type = int(getattr(dll, "UIA_DocumentControlTypeId", 50030))
    value_pattern_id = getattr(dll, "UIA_ValuePatternId", 10002)
    try:
        walker = uia.iuia.ControlViewWalker
        current = element
        for _ in range(_WEB_DOCUMENT_ANCESTOR_LIMIT):
            if not current:
                return False
            if int(current.CurrentControlType) == document_type:
                value = ""
                try:
                    pattern = current.GetCurrentPattern(value_pattern_id)
                    if pattern:
                        value = str(pattern.QueryInterface(dll.IUIAutomationValuePattern).CurrentValue or "")
                except Exception:
                    value = ""
                if value.strip().lower().startswith(_WEB_DOCUMENT_PREFIXES):
                    return True
            current = walker.GetParentElement(current)
        return True
    except Exception:
        return True


def _focused_text_reading() -> dict[str, Any]:
    """The focused text control's whole text and the caret's line, read through UI Automation.

    Only an Edit or Document that takes typing: neither its text range (TextPattern's IsReadOnly
    attribute) nor its ValuePattern says it is read-only. A web page's document is read-only text and
    is never read here, nor is any control inside a browser's web content (_inside_web_document).
    Empty for a password field or anything else.
    """
    try:
        from pywinauto.uia_defines import IUIA
        from pywinauto.uia_element_info import UIAElementInfo

        uia = IUIA()
        dll = uia.UIA_dll
        element = uia.iuia.GetFocusedElement()
        if element is None or bool(element.CurrentIsPassword):
            return {}
        control_type = int(element.CurrentControlType)
        if control_type not in {
            int(getattr(dll, "UIA_DocumentControlTypeId", 50030)),
            int(getattr(dll, "UIA_EditControlTypeId", 50004)),
        }:
            return {}
        if _inside_web_document(uia, dll, element):
            return {}
        runtime_id = _runtime_id(UIAElementInfo(element).runtime_id)
        text = ""
        caret_line: int | None = None
        read_only = False
        try:
            pattern = element.GetCurrentPattern(dll.UIA_TextPatternId)
            if pattern:
                text_pattern = pattern.QueryInterface(dll.IUIAutomationTextPattern)
                document = text_pattern.DocumentRange
                try:
                    # A bool when the provider says; a mixed or not-supported marker object otherwise.
                    attribute = document.GetAttributeValue(getattr(dll, "UIA_IsReadOnlyAttributeId", 40015))
                    read_only = attribute is True
                except Exception:
                    pass
                text = str(document.GetText(_FOCUSED_TEXT_READ_CAP) or "")
                selection = text_pattern.GetSelection()
                if selection is not None and selection.Length >= 1:
                    prefix = document.Clone()
                    prefix.MoveEndpointByRange(
                        dll.TextPatternRangeEndpoint_End,
                        selection.GetElement(0),
                        dll.TextPatternRangeEndpoint_Start,
                    )
                    before = str(prefix.GetText(_FOCUSED_TEXT_READ_CAP) or "")
                    if len(before) < _FOCUSED_TEXT_READ_CAP:
                        caret_line = before.replace("\r\n", "\n").replace("\r", "\n").count("\n") + 1
        except Exception:
            pass
        try:
            value_pattern = element.GetCurrentPattern(getattr(dll, "UIA_ValuePatternId", 10002))
            if value_pattern:
                value = value_pattern.QueryInterface(dll.IUIAutomationValuePattern)
                read_only = read_only or bool(value.CurrentIsReadOnly)
                if not text:
                    text = str(value.CurrentValue or "")
        except Exception:
            pass
        if read_only or not text or not runtime_id:
            return {}
        return {"runtime_id": runtime_id, "text": text, "caret_line": caret_line}
    except Exception:
        return {}


def _focused_text_lines(controls: list[dict[str, Any]], sensitive_runtime_ids: set[tuple[int, ...]]) -> dict[str, Any]:
    """focused_text_lines for the inventory's focused multi-line Edit/Document: its text line by line
    (text_line_window), tagged with its inventory index. Never a password control, a control marked
    sensitive, or one whose value the inventory redacted; empty unless the control UI Automation
    reports focused is that same inventory control."""
    candidates = [
        control
        for control in controls
        if control.get("focused")
        and str(control.get("tag") or "") in {"document", "edit"}
        and not control.get("password")
        and not control.get("value_redacted")
    ]
    if not candidates:
        return {}
    reading = _focused_text_reading()
    runtime_id = tuple(reading.get("runtime_id") or ()) if reading else ()
    if not runtime_id or runtime_id in sensitive_runtime_ids:
        return {}
    control = next(
        (
            item
            for item in candidates
            if (_parse_selector(str(item.get("selector") or "")) or (0, ()))[1] == runtime_id
        ),
        None,
    )
    if control is None:
        return {}
    return text_line_window(reading.get("text"), caret_line=reading.get("caret_line"), control=control.get("index"))


# How much of the caret's paragraph a type's verification reads; a longer paragraph is not judged by it.
_EDIT_UNIT_CAP = 2000


def _caret_paragraph() -> dict[str, Any]:
    """The paragraph holding the caret in the focused text control, raw (spaces kept): its text without
    the line break, the column where the selection starts, and the selected text.

    A type at the caret is verified against it: the paragraph afterwards must be this one with the
    selection replaced by the typed text. A paragraph (not a visual line) so word wrap cannot reflow
    it. Empty when the focused element exposes no text pattern, is a password field, or the paragraph
    is longer than the body reads.
    """
    try:
        from pywinauto.uia_defines import IUIA

        uia = IUIA()
        dll = uia.UIA_dll
        element = uia.iuia.GetFocusedElement()
        if element is None or bool(element.CurrentIsPassword):
            return {}
        pattern = element.GetCurrentPattern(dll.UIA_TextPatternId)
        if not pattern:
            return {}
        text_pattern = pattern.QueryInterface(dll.IUIAutomationTextPattern)
        selection = text_pattern.GetSelection()
        if selection is None or selection.Length < 1:
            return {}
        selected = selection.GetElement(0)
        selected_text = str(selected.GetText(_EDIT_UNIT_CAP) or "")
        paragraph = selected.Clone()
        paragraph.ExpandToEnclosingUnit(getattr(dll, "TextUnit_Paragraph", 4))
        text = str(paragraph.GetText(_EDIT_UNIT_CAP) or "")
        prefix = paragraph.Clone()
        prefix.MoveEndpointByRange(
            dll.TextPatternRangeEndpoint_End, selected, dll.TextPatternRangeEndpoint_Start
        )
        before_selection = str(prefix.GetText(_EDIT_UNIT_CAP) or "")
        if len(text) >= _EDIT_UNIT_CAP or len(selected_text) >= _EDIT_UNIT_CAP:
            return {}
        return {"text": text.rstrip("\r\n"), "column": len(before_selection), "selected": selected_text}
    except Exception:
        return {}


# Controls where a press acts on the item rather than placing a caret in text: selecting text there
# by pointer would open, drag, or toggle it.
_NOT_TEXT_CONTROL_TYPES = frozenset(
    {
        "ListItem", "TreeItem", "DataItem", "HeaderItem", "TitleBar", "Button", "SplitButton", "MenuItem",
        "Menu", "MenuBar", "TabItem", "Hyperlink", "Thumb", "ScrollBar", "CheckBox", "RadioButton",
        "ComboBox", "Slider", "Spinner", "ToolBar",
    }
)


def _control_type_at(rel_x: int, rel_y: int) -> str:
    """The UI Automation control type of the element under a capture point ("" when unknown)."""
    try:
        from ctypes.wintypes import tagPOINT

        from pywinauto.uia_defines import IUIA
        from pywinauto.uia_element_info import UIAElementInfo

        left, top, _, _ = _virtual_screen()
        element = IUIA().iuia.ElementFromPoint(tagPOINT(left + int(rel_x), top + int(rel_y)))
        return str(UIAElementInfo(element).control_type or "") if element else ""
    except Exception:
        return ""


def _double_click_seconds() -> float:
    try:
        import ctypes

        return max(0.2, min(1.0, ctypes.windll.user32.GetDoubleClickTime() / 1000))
    except Exception:
        return 0.5


def _changed_outside(before: Any, after: Any, box: tuple[int, int, int, int]) -> bool:
    """Whether pixels changed anywhere but inside the box (a window moved, a list reordered)."""
    try:
        difference = ImageChops.difference(before, after).convert("L")
        ImageDraw.Draw(difference).rectangle(box, fill=0)
        return difference.getbbox() is not None
    except Exception:
        return False


def _ocr_locate(image: Any, text: str, x: int, y: int, *, reach: tuple[int, int]) -> dict[str, Any]:
    """Where `text` is drawn in a capture, the occurrence nearest (x, y) within reach (half the band's
    width and height, capture pixels): screen_ocr.locate_phrase over the band read as is, then
    enlarged 2x (small text), then with selection highlights recolored (OCR does not read white on
    blue). Boxes are in capture pixels."""
    from app import screen_ocr

    width, height = image.size
    band = (max(0, x - reach[0]), max(0, y - reach[1]), min(width, x + reach[0]), min(height, y + reach[1]))
    if band[2] <= band[0] or band[3] <= band[1]:
        return {"found": False, "occurrences": 0}
    crop = image.crop(band)
    readings = [(crop, 1), (crop.resize((crop.width * 2, crop.height * 2)), 2)]
    located: dict[str, Any] = {"found": False, "occurrences": 0}
    for picture, scale in readings:
        if picture is None:
            break
        buffer = io.BytesIO()
        picture.save(buffer, format="PNG")
        lines = [
            [
                {
                    "t": word["t"],
                    "x": band[0] + float(word["x"]) / scale,
                    "y": band[1] + float(word["y"]) / scale,
                    "w": float(word["w"]) / scale,
                    "h": float(word["h"]) / scale,
                }
                for word in line
            ]
            for line in screen_ocr.read_words(buffer.getvalue())
        ]
        located = screen_ocr.locate_phrase(text, lines, (float(x), float(y)))
        if located.get("found"):
            break
        if len(readings) == 2 and scale == 2:
            readings.append((_highlights_inverted(crop), 1))
    return located


# How far from the model's point a click that names its option may move to that option's drawn
# label (capture pixels): a neighbouring option, never another part of the window.
_LABEL_CLICK_REACH = 200


# How far (screen pixels) the occurrence UI Automation selects may be from the model's point.
_UIA_SELECT_REACH = 400


def _select_text_by_uia(text: str, rel_x: int, rel_y: int) -> dict[str, Any] | None:
    """Select exactly `text` in the text control under a capture point through UI Automation's
    TextPattern (no pointer: a press inside an earlier selection of an editable control would drag
    that text away instead of selecting), the occurrence nearest the point, and read it back.

    None when no element at or above the point exposes a selectable TextPattern, or it does not hold
    the text: the caller finds the text on screen instead. Never a password control."""
    wanted = " ".join(str(text or "").split())
    if not wanted:
        return None
    try:
        from ctypes.wintypes import tagPOINT

        from pywinauto.uia_defines import IUIA

        uia = IUIA()
        dll = uia.UIA_dll
        left, top, _, _ = _virtual_screen()
        abs_x, abs_y = left + int(rel_x), top + int(rel_y)
        element = uia.iuia.ElementFromPoint(tagPOINT(abs_x, abs_y))
        walker = uia.iuia.ControlViewWalker
        text_pattern = None
        for _ in range(4):
            if not element:
                return None
            if bool(element.CurrentIsPassword):
                return None
            pattern = element.GetCurrentPattern(dll.UIA_TextPatternId)
            if pattern:
                text_pattern = pattern.QueryInterface(dll.IUIAutomationTextPattern)
                break
            element = walker.GetParentElement(element)
        if text_pattern is None or int(text_pattern.SupportedTextSelection) == 0:
            return None
        document = text_pattern.DocumentRange
        best = None
        best_distance = float("inf")
        search = document.Clone()
        for _ in range(50):
            found = search.FindText(wanted, False, False)
            if not found:  # comtypes hands back a NULL pointer, not None, when nothing more matches
                break
            rects = list(found.GetBoundingRectangles() or ())
            distance = float("inf")
            for index in range(0, len(rects) - 3, 4):
                r_left, r_top, r_width, r_height = rects[index : index + 4]
                near_x = min(max(abs_x, r_left), r_left + r_width)
                near_y = min(max(abs_y, r_top), r_top + r_height)
                distance = min(distance, ((abs_x - near_x) ** 2 + (abs_y - near_y) ** 2) ** 0.5)
            if best is None or distance < best_distance:
                best, best_distance = found, distance
            search.MoveEndpointByRange(
                dll.TextPatternRangeEndpoint_Start, found, dll.TextPatternRangeEndpoint_End
            )
        if best is None or best_distance > _UIA_SELECT_REACH:
            # Only an occurrence shown near the point: an off-screen one has no rectangles, and a far
            # one is not the text the model pointed at. The screen is read instead.
            return None
        best.Select()
        selection = text_pattern.GetSelection()
        selected = (
            str(selection.GetElement(0).GetText(4096) or "")
            if selection and selection.Length >= 1
            else ""
        )
        matches = " ".join(selected.split()) == wanted
        return {
            "ok": matches,
            "action": "select_text",
            "mode": "text_pattern",
            "selected_text": selected[:300],
            **({} if matches else {"failure_reason": "select_text_mismatch"}),
            "desktop_surface": True,
        }
    except Exception:  # noqa: BLE001 - no pattern selection: the caller finds the text on screen
        return None


def _paragraph_edit(before: dict[str, Any], after: dict[str, Any], text: str) -> dict[str, Any]:
    """Judge a type at the caret by the caret's paragraph before and after it.

    Returns the paragraph before and after, the paragraph expected (the one before with the selection
    replaced by ``text``), and whether the paragraph after is exactly that. Empty when the edit cannot
    be judged this way: no reading on either side, or the text or selection spans a line break.
    """
    if not before or not after or "\n" in text or "\r" in text:
        return {}
    line_before = str(before.get("text") or "")
    selected = str(before.get("selected") or "")
    try:
        column = int(before.get("column") or 0)
    except (TypeError, ValueError):
        return {}
    if (
        "\n" in selected
        or "\r" in selected
        or column < 0
        or column + len(selected) > len(line_before)
        or line_before[column : column + len(selected)] != selected
    ):
        return {}
    expected = line_before[:column] + text + line_before[column + len(selected) :]
    line_after = str(after.get("text") or "")
    return {"before": line_before, "after": line_after, "expected": expected, "matches": line_after == expected}


def _edit_excerpts(before: str, after: str, *, limit: int = 160) -> tuple[str, str]:
    """Before/after text for a receipt: whole when short, else the same window around the first change."""
    before, after = str(before or ""), str(after or "")
    if len(before) <= limit and len(after) <= limit:
        return before, after
    first = next((index for index, (a, b) in enumerate(zip(before, after)) if a != b), min(len(before), len(after)))
    start = max(0, first - limit // 3)

    def cut(value: str) -> str:
        return ("..." if start else "") + value[start : start + limit] + ("..." if start + limit < len(value) else "")

    return cut(before), cut(after)


def _focus_within(wrapper: Any) -> bool | None:
    """Whether keyboard focus is on this control or inside it; None when focus cannot be read."""
    try:
        focused_id = _focused_state().get("runtime_id")
        if not focused_id:
            return None
        if _runtime_id(wrapper.element_info.runtime_id) == focused_id:
            return True
        return any(_runtime_id(child.element_info.runtime_id) == focused_id for child in wrapper.descendants())
    except Exception:
        return None


def _point_within_control(wrapper: Any, x: int, y: int) -> tuple[int, int] | None:
    """The model's point (capture pixels) as an absolute screen point, when it lies inside the control.

    Where in a control a click lands can matter: in a text area it is where the caret goes. A point
    inside the bound control is the model's chosen spot; one outside it (stale, misaligned) is not.
    """
    if not (x or y):
        return None
    try:
        left, top, _, _ = _virtual_screen()
        abs_x, abs_y = int(x) + left, int(y) + top
        rectangle = wrapper.element_info.rectangle
        if rectangle.left <= abs_x < rectangle.right and rectangle.top <= abs_y < rectangle.bottom:
            return (abs_x, abs_y)
    except Exception:
        return None
    return None


def _resolve_control(selector: str) -> Any | None:
    parsed = _parse_selector(selector)
    if parsed is None:
        return None
    handle, expected_runtime_id = parsed
    try:
        root = _desktop().window(handle=handle).wrapper_object()
        for wrapper in [root, *root.descendants()]:
            if _runtime_id(wrapper.element_info.runtime_id) == expected_runtime_id:
                return wrapper
    except Exception:
        return None
    return None


def _capture_image() -> Any:
    image = ImageGrab.grab(all_screens=True)
    crop = _view_crop(_desktop_union(), _VIEW.get("rect"))
    if crop is not None:
        image = image.crop(crop)
    return image.convert("RGB")


def _typed_text_read_near_point(after: Any, point: Any, text: str, *, radius_x: int = 320, radius_y: int = 40) -> bool:
    """Whether the exact typed text (whitespace collapsed) now reads in the band around the caret point,
    from the window's own pixels: the read-back of a field that exposes no value (a Tk entry). One line
    of OCR text must contain it whole; 3+ characters, so a stray digit elsewhere in the band proves
    nothing (v5_memory Sonnet brain, 2026-10-06: every unread type broke the chain after it)."""
    try:
        wanted = " ".join(str(text or "").split())
        if len(wanted) < 3:
            return False
        import io

        from app import screen_ocr

        x, y = int(point[0]), int(point[1])
        width, height = after.size
        box = (max(0, x - radius_x), max(0, y - radius_y), min(width, x + radius_x), min(height, y + radius_y))
        if box[2] - box[0] < 16 or box[3] - box[1] < 16:
            return False
        buffer = io.BytesIO()
        after.crop(box).convert("RGB").save(buffer, format="PNG")
        lines = (screen_ocr.read_lines([buffer.getvalue()]) or [[]])[0]
        return any(wanted in " ".join(str(line).split()) for line in lines)
    except Exception:  # noqa: BLE001 - no reading proves nothing
        return False


def _changed_near_point(before: Any, after: Any, point: Any, *, radius_x: int = 240, radius_y: int = 70) -> bool:
    """Whether pixels changed in the band around a screen point (the field the caret was put in)."""
    try:
        x, y = int(point[0]), int(point[1])
        width, height = before.size
        box = (max(0, x - radius_x), max(0, y - radius_y), min(width, x + radius_x), min(height, y + radius_y))
        if box[2] <= box[0] or box[3] <= box[1]:
            return False
        return ImageChops.difference(before.crop(box), after.crop(box)).getbbox() is not None
    except Exception:
        return False


def _window_capture_box(handle: int) -> tuple[int, int, int, int] | None:
    """A window's rectangle in capture pixels (relative to the virtual screen's top-left)."""
    try:
        import win32gui

        left, top, right, bottom = win32gui.GetWindowRect(int(handle))
        origin_x, origin_y, _, _ = _virtual_screen()
        if right <= left or bottom <= top:
            return None
        return (left - origin_x, top - origin_y, right - origin_x, bottom - origin_y)
    except Exception:
        return None


def _changed_within_box(before: Any, after: Any, box: Any) -> bool:
    """Whether pixels changed inside a capture-pixel box."""
    try:
        width, height = before.size
        clipped = (max(0, int(box[0])), max(0, int(box[1])), min(width, int(box[2])), min(height, int(box[3])))
        if clipped[2] <= clipped[0] or clipped[3] <= clipped[1]:
            return False
        return ImageChops.difference(before.crop(clipped), after.crop(clipped)).getbbox() is not None
    except Exception:
        return False


def _spread(mask: Any, dx: int, dy: int, steps: tuple[int, ...]) -> Any:
    """The mask OR-ed with itself shifted along (dx, dy) by each step in turn: with steps 1, 2, 4 a
    pixel is set when the mask is set up to 7 pixels behind it in that direction."""
    spread = mask
    for step in steps:
        shifted = Image.new("L", mask.size, 0)
        shifted.paste(spread, (dx * step, dy * step))
        spread = ImageChops.lighter(spread, shifted)
    return spread


def _highlights_inverted(image: Any) -> Any:
    """The image in grey with its selection highlights as dark text on white, or None when it has none.

    A selection highlight shows its text light on a saturated color (white on blue), which OCR does
    not read. A highlight is dense color (thin colored strokes such as a link's letters are not), and
    its inside is where that color lies within a few pixels on all four sides: a white stroke of a
    highlighted letter, even one touching the highlight's edge, but never the page just outside it.
    Inside, color becomes white and the light strokes black.
    """
    try:
        _, saturation, value = image.convert("HSV").split()
        colored = ImageChops.multiply(
            Image.eval(saturation, lambda level: 255 if level > 110 else 0),
            Image.eval(value, lambda level: 255 if level > 70 else 0),
        )
        dense = Image.eval(colored.filter(ImageFilter.BoxBlur(4)), lambda level: 255 if level > 110 else 0)
        if dense.getbbox() is None:
            return None
        across = (1, 2, 4)
        tall = (1, 2, 4, 8)
        inside = dense
        for dx, dy, steps in ((1, 0, across), (-1, 0, across), (0, 1, tall), (0, -1, tall)):
            inside = ImageChops.darker(inside, _spread(colored, dx, dy, steps))
        if inside.getbbox() is None:
            return None
        grey = ImageOps.grayscale(image)
        text = Image.eval(grey, lambda level: 0 if level > 160 else 255)
        recolored = Image.composite(Image.new("L", grey.size, 255), text, colored)
        return Image.composite(recolored, grey, inside)
    except Exception:  # noqa: BLE001 - no second reading
        return None


def _image_changed(before: Any, after: Any) -> bool:
    try:
        return ImageChops.difference(before, after).getbbox() is not None
    except Exception:
        return False


_KEYSTROKE_INTERVAL_SECONDS = 0.018


def _send_unicode(text: str) -> None:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    input_keyboard = 1
    keyeventf_keyup = 0x0002
    keyeventf_unicode = 0x0004

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
        ]

    class MOUSEINPUT(ctypes.Structure):
        # Never sent, but the union MUST include the largest INPUT member: SendInput
        # validates cbSize against the full winuser.h INPUT layout and rejects the
        # whole batch with ERROR_INVALID_PARAMETER (87) when the union is truncated
        # to KEYBDINPUT alone on x64.
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
        ]

    class INPUT_UNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("union",)
        _fields_ = [("type", wintypes.DWORD), ("union", INPUT_UNION)]

    encoded = text.encode("utf-16-le")
    # One code unit at a time at human typing speed. A single batched burst outruns how
    # fast applications sample the keyboard, which silently drops and duplicates
    # characters (observed: "harness check ABCDEF" arriving as "harness hheck FFFFFF").
    # Measured on Windows 11 Notepad replacing a selected line: 6 ms between characters
    # garbled 6 of 6 tries ("Seal uumber:----LXZK..."); 12, 18, and 25 ms typed it exactly.
    for index in range(0, len(encoded), 2):
        code_unit = int.from_bytes(encoded[index : index + 2], "little")
        events = (INPUT * 2)(
            INPUT(type=input_keyboard, ki=KEYBDINPUT(0, code_unit, keyeventf_unicode, 0, None)),
            INPUT(
                type=input_keyboard,
                ki=KEYBDINPUT(0, code_unit, keyeventf_unicode | keyeventf_keyup, 0, None),
            ),
        )
        sent = int(user32.SendInput(2, events, ctypes.sizeof(INPUT)))
        if sent != 2:
            raise OSError(ctypes.get_last_error(), "SendInput did not inject the complete text.")
        if index + 2 < len(encoded):
            time.sleep(_KEYSTROKE_INTERVAL_SECONDS)


# Above this length a human reaches for paste, and so does the body: per-character
# SendInput pacing makes essay-length values slow and gives focus stealers a wider
# window to corrupt them. Short values keep real keystrokes (some inputs filter paste).
_PASTE_THRESHOLD_CHARS = 120
# Registered clipboard formats that keep a value out of clipboard history (Win+V), cloud clipboard sync, and
# clipboard monitors. Every text this body writes carries them: the run's transient value, and the user's own
# text put back (a password manager marks its copies this way; a plain write would push one into history).
_CLIPBOARD_EXCLUSION_FORMATS = (
    "ExcludeClipboardContentFromMonitorProcessing",
    "CanIncludeInClipboardHistory",
    "CanUploadToCloudClipboard",
)
# Standard clipboard formats that hold text or describe it: CF_TEXT, CF_OEMTEXT, CF_UNICODETEXT, CF_LOCALE.
_TEXT_CLIPBOARD_FORMATS = frozenset({1, 7, 13, 16})
# Formats from here up are registered by name (HTML, rich text, an app's own markers beside its content).
_FIRST_REGISTERED_CLIPBOARD_FORMAT = 0xC000


def _open_clipboard(win32clipboard: Any) -> bool:
    for _ in range(3):
        try:
            win32clipboard.OpenClipboard(0)
            return True
        except Exception:  # noqa: BLE001 - another process briefly owns the clipboard
            time.sleep(0.05)
    return False


def _write_clipboard_text(win32clipboard: Any, win32con: Any, value: str) -> None:
    """Write ``value`` to the open clipboard as text kept out of clipboard history, cloud sync, and monitors."""
    win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, value)
    for name in _CLIPBOARD_EXCLUSION_FORMATS:
        try:
            win32clipboard.SetClipboardData(win32clipboard.RegisterClipboardFormat(name), b"\x00\x00\x00\x00")
        except Exception:  # noqa: BLE001 - an older Windows without the format keeps no history either
            pass


def _clipboard_holds_text_or_nothing(win32clipboard: Any) -> bool:
    """Whether the open clipboard is empty or holds text: a text format and no standard format of other
    content (a bitmap, files, a metafile, sound). Formats registered by name beside the text (HTML, rich
    text, an app's markers) are not other content. False when the formats cannot be listed."""
    formats: list[int] = []
    try:
        current = 0
        for _ in range(512):
            current = int(win32clipboard.EnumClipboardFormats(current) or 0)
            if not current:
                break
            formats.append(current)
    except Exception:  # noqa: BLE001 - unknown content is never wiped for a short value
        return False
    if not formats:
        return True
    return any(fmt in _TEXT_CLIPBOARD_FORMATS for fmt in formats) and all(
        fmt in _TEXT_CLIPBOARD_FORMATS or fmt >= _FIRST_REGISTERED_CLIPBOARD_FORMAT for fmt in formats
    )


class _ClipboardHold:
    """The user's clipboard text while text this body pasted may still be read by the target app.

    Held in memory only: never logged, returned, or shown to the model (read_clipboard never returns it:
    the paste and the restore are no input action of the run, and both carry the private markers). The type that pasted settles it after its
    read-back: the user's text goes back only when the read-back saw the pasted text land. Otherwise the
    clipboard is left empty, so an app still handling the paste gets nothing rather than the user's value
    (which may be a password), and no later read-back can carry that value to the model.
    """

    def __init__(self) -> None:
        self.pending = False
        self._previous: str | None = None

    def hold(self, previous: str | None) -> None:
        self.pending, self._previous = True, previous

    def settle(self, *, landed: bool) -> None:
        if not self.pending:
            return
        previous = self._previous if landed else None
        self.pending, self._previous = False, None
        try:
            import win32clipboard
            import win32con

            if not _open_clipboard(win32clipboard):
                return
            try:
                win32clipboard.EmptyClipboard()
                if previous:
                    _write_clipboard_text(win32clipboard, win32con, previous)
            finally:
                win32clipboard.CloseClipboard()
        except Exception:  # noqa: BLE001 - best-effort; the run's own (never secret) text may stay behind
            pass


def _deliver_text_via_clipboard(text: str, hold: _ClipboardHold, *, text_clipboard_only: bool = False) -> bool:
    """Paste ``text`` through the clipboard; the user's clipboard text waits in ``hold`` for the caller to
    settle after its read-back (see _ClipboardHold).

    ``text_clipboard_only`` leaves a clipboard holding anything but text (an image, files) or text that
    cannot be read untouched and returns False, so a short value is typed instead. Without it, non-text
    content cannot be put back and is the accepted cost of a long paste. Returns False when nothing was
    pasted and the clipboard is as it was. Raises when Ctrl+V itself failed with ``hold`` pending: the
    paste may have landed.
    """
    import win32clipboard
    import win32con

    if not _open_clipboard(win32clipboard):
        return False
    previous: str | None = None
    try:
        if text_clipboard_only and not _clipboard_holds_text_or_nothing(win32clipboard):
            return False
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                previous = str(win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT))
        except Exception:  # noqa: BLE001 - unreadable text is wiped only by a long paste
            if text_clipboard_only:
                return False
            previous = None
        win32clipboard.EmptyClipboard()
        try:
            # The transient value is the run's, not the user's: it stays out of history, sync, and monitors.
            _write_clipboard_text(win32clipboard, win32con, str(text))
        except Exception:
            # Nothing will be pasted: the user's text goes straight back before the error is reported.
            try:
                win32clipboard.EmptyClipboard()
                if previous:
                    _write_clipboard_text(win32clipboard, win32con, previous)
            except Exception:  # noqa: BLE001 - best-effort
                pass
            raise
    finally:
        win32clipboard.CloseClipboard()
    # From here a paste may be outstanding: the caller decides what goes back, after its read-back.
    hold.hold(previous)
    _send_key("CTRL+V")
    # The target app reads the clipboard while handling the keystroke; give slow apps a beat before the
    # caller reads the result back.
    time.sleep(0.35)
    return True


# The most clipboard text read_clipboard returns to the model; the receipt says when it cut more.
CLIPBOARD_READ_LIMIT_CHARS = 4000
# How many of this run's own copies the body remembers by clipboard sequence number.
_CLIPBOARD_COPY_LEDGER_LIMIT = 32


def _clipboard_sequence() -> int | None:
    """Windows' clipboard sequence number: it changes every time any process changes the clipboard.
    Reading it does not open the clipboard. None when it cannot be read."""
    try:
        import win32clipboard

        return int(win32clipboard.GetClipboardSequenceNumber())
    except Exception:  # noqa: BLE001 - unknown sequence means no copy is attributed to the run
        return None


def _clipboard_marked_private(win32clipboard: Any) -> bool:
    """Whether the open clipboard carries a marker an app sets to keep its copy out of clipboard history,
    cloud sync, or monitors: what a password manager does when it copies a secret."""
    for name in _CLIPBOARD_EXCLUSION_FORMATS:
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32clipboard.RegisterClipboardFormat(name)):
                return True
        except Exception:  # noqa: BLE001 - a marker that cannot be checked counts as present
            return True
    return False


def _read_clipboard_text() -> dict[str, Any]:
    """The clipboard's text, read without changing it: ``{"text", "private", "sequence"}``, or
    ``{"failure_reason"}`` when the clipboard cannot be opened or holds no text."""
    import win32clipboard
    import win32con

    if not _open_clipboard(win32clipboard):
        return {"failure_reason": "clipboard_busy"}
    try:
        sequence = _clipboard_sequence()
        private = _clipboard_marked_private(win32clipboard)
        if not win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            return {"failure_reason": "clipboard_holds_no_text", "private": private, "sequence": sequence}
        text = str(win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT) or "")
        return {"text": text, "private": private, "sequence": sequence}
    finally:
        win32clipboard.CloseClipboard()


# Password managers: whatever they put on the clipboard is a secret, however their window looks.
_CREDENTIAL_STORE_PROCESSES = frozenset(
    {"bitwarden.exe", "1password.exe", "keepass.exe", "keepassxc.exe", "lastpass.exe", "dashlane.exe",
     "enpass.exe", "nordpass.exe", "roboform.exe", "keeper.exe", "proton pass.exe", "protonpass.exe"}
)


def _clipboard_owner_process() -> dict[str, Any] | None:
    """The process that owns the clipboard now ({pid, exe, credential_store}), or None when unknown."""
    try:
        import win32clipboard
        import win32process

        owner = int(win32clipboard.GetClipboardOwner() or 0)
        if not owner:
            return None
        _, pid = win32process.GetWindowThreadProcessId(owner)
        exe = ""
        try:
            import win32api
            import win32con

            handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            try:
                exe = os.path.basename(str(win32process.GetModuleFileNameEx(handle, 0) or "")).casefold()
            finally:
                win32api.CloseHandle(handle)
        except Exception:  # noqa: BLE001 - a name we cannot read is no credential store we know
            exe = ""
        return {"pid": int(pid or 0), "exe": exe, "credential_store": exe in _CREDENTIAL_STORE_PROCESSES}
    except Exception:  # noqa: BLE001 - unknown owner: not the run's copy
        return None


def _foreground_process_id() -> int:
    try:
        import win32gui
        import win32process

        _, pid = win32process.GetWindowThreadProcessId(int(win32gui.GetForegroundWindow() or 0))
        return int(pid or 0)
    except Exception:  # noqa: BLE001
        return 0


def _records_clipboard_copies(method: Any) -> Any:
    """Note on the body when an input action of the run changed the clipboard (a Ctrl+C, a Copy button or
    menu entry): only such a copy is ever readable by read_clipboard."""

    @functools.wraps(method)
    async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        native = getattr(self, "_native", None) or sys.modules[__name__]
        before = native._clipboard_sequence()
        # A password control in the front window as the run last saw it (or holding focus now): a copy
        # made by this action may be its value, even when a Copy button beside it did the copying.
        password_shown = bool(getattr(self, "_password_control_shown", False))
        copied = False
        try:
            result = await method(self, *args, **kwargs)
        finally:
            after = native._clipboard_sequence()
            owner = native._clipboard_owner_process() if before is not None and after is not None and after != before else None
            # Only a copy the front window's own app made counts: a cloud-clipboard push or a background
            # app's copy landing during the action is not the run's.
            if owner is not None and owner.get("pid") and owner.get("pid") == native._foreground_process_id():
                copied = True
                sensitive = password_shown or owner.get("credential_store", False)
                if not sensitive:
                    focus = native._focused_state()
                    sensitive = bool(focus.get("password"))
                self._note_clipboard_copy(after, sensitive=sensitive)
        if copied and isinstance(result, dict):
            # A copy draws nothing: the clipboard changing is the action's observed effect (value-free).
            result = {**result, "clipboard_changed": True}
        return result

    return wrapper


def _deliver_text(
    text: str,
    *,
    prefer_paste: bool = False,
    allow_paste: bool = True,
    clipboard: _ClipboardHold | None = None,
) -> str:
    """Put ``text`` into the focused control the way a human would.

    Short values are typed key by key; long values, and any value the caller prefers pasted (text going into
    a document at the caret, where paced keystrokes have arrived garbled), are pasted. A paste needs
    ``clipboard``, which the caller settles after reading the text back. A short value is pasted only over a
    clipboard holding text or nothing, never over an image or files. A clipboard that is unavailable (or not
    to be wiped) means keystrokes. ``allow_paste=False`` (a secret, a password control) never touches the
    clipboard. Returns the method used so read-back evidence can name it: "paste_unconfirmed" when Ctrl+V
    failed partway. That paste may have landed, so nothing is typed after it and the read-back decides.
    """
    value = str(text)
    long_value = len(value) >= _PASTE_THRESHOLD_CHARS
    # Never an empty paste: pasting nothing would delete a selection that typing nothing leaves alone.
    if allow_paste and clipboard is not None and value and (prefer_paste or long_value):
        try:
            if _deliver_text_via_clipboard(value, clipboard, text_clipboard_only=not long_value):
                return "paste"
        except Exception:  # noqa: BLE001 - clipboard trouble degrades to typing when nothing was pasted
            if clipboard.pending:
                # Typing as well could put the text in twice.
                return "paste_unconfirmed"
    _send_unicode(value)
    return "keystrokes"


# The chord that selects all of a field's text before a replacing type.
_SELECT_ALL_KEY = "CTRL+A"

# A human's full keyboard. Every key a person can press on a standard Windows layout is
# available to the brain; only the narrow set of system shortcuts below stays blocked.
_VK = {
    "BACKSPACE": 0x08,
    "TAB": 0x09,
    "CLEAR": 0x0C,
    "ENTER": 0x0D,
    "PAUSE": 0x13,
    "CAPSLOCK": 0x14,
    "ESC": 0x1B,
    "ESCAPE": 0x1B,
    "SPACE": 0x20,
    "PAGEUP": 0x21,
    "PAGEDOWN": 0x22,
    "END": 0x23,
    "HOME": 0x24,
    "ARROWLEFT": 0x25,
    "ARROWUP": 0x26,
    "ARROWRIGHT": 0x27,
    "ARROWDOWN": 0x28,
    "PRINTSCREEN": 0x2C,
    "INSERT": 0x2D,
    "DELETE": 0x2E,
    "NUMPAD0": 0x60,
    "NUMPAD1": 0x61,
    "NUMPAD2": 0x62,
    "NUMPAD3": 0x63,
    "NUMPAD4": 0x64,
    "NUMPAD5": 0x65,
    "NUMPAD6": 0x66,
    "NUMPAD7": 0x67,
    "NUMPAD8": 0x68,
    "NUMPAD9": 0x69,
    "MULTIPLY": 0x6A,
    "ADD": 0x6B,
    "SUBTRACT": 0x6D,
    "DECIMAL": 0x6E,
    "DIVIDE": 0x6F,
    "NUMLOCK": 0x90,
    "SCROLLLOCK": 0x91,
    "VOLUMEMUTE": 0xAD,
    "VOLUMEDOWN": 0xAE,
    "VOLUMEUP": 0xAF,
    "MEDIANEXT": 0xB0,
    "MEDIAPREV": 0xB1,
    "MEDIASTOP": 0xB2,
    "MEDIAPLAYPAUSE": 0xB3,
    ";": 0xBA,
    ":": 0xBA,
    "=": 0xBB,
    "+": 0xBB,
    ",": 0xBC,
    "<": 0xBC,
    "-": 0xBD,
    "_": 0xBD,
    ".": 0xBE,
    ">": 0xBE,
    "/": 0xBF,
    "?": 0xBF,
    "`": 0xC0,
    "~": 0xC0,
    "[": 0xDB,
    "{": 0xDB,
    "\\": 0xDC,
    "|": 0xDC,
    "]": 0xDD,
    "}": 0xDD,
    "'": 0xDE,
    '"': 0xDE,
}
# Extended-scan-code keys need KEYEVENTF_EXTENDEDKEY or apps mistake them for numpad keys.
_EXTENDED_KEYS = {
    0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,
    0x2D, 0x2E, 0x2C, 0x90, 0x6F, 0x0D,
}
_MODIFIERS = {
    "CTRL": 0x11,
    "CONTROL": 0x11,
    "SHIFT": 0x10,
    "ALT": 0x12,
    "WIN": 0x5B,
    "WINDOWS": 0x5B,
    "META": 0x5B,
}
# Only shortcuts that end the session, kill the host app, or reach the secure desktop stay
# blocked. Ordinary editing and application shortcuts (CTRL+A/C/V/X/Z/Y/S/P/W/F, ALT+TAB,
# WIN, function keys) are part of normal human computer use and the loop's own approval
# gates govern consequential actions.
_BLOCKED_SHORTCUTS = {
    ("ALT", "F4"),
    ("CTRL", "ALT", "DELETE"),
    ("WIN", "L"),
    ("WIN", "R"),
}


def _normalize_key_parts(value: str) -> list[str]:
    aliases = {
        "RETURN": "ENTER",
        "NEWLINE": "ENTER",
        "UP": "ARROWUP",
        "DOWN": "ARROWDOWN",
        "LEFT": "ARROWLEFT",
        "RIGHT": "ARROWRIGHT",
        "CMD": "WIN",
        "SUPER": "WIN",
        "BKSP": "BACKSPACE",
        "BACK": "BACKSPACE",
        "DEL": "DELETE",
        "INS": "INSERT",
        "PGUP": "PAGEUP",
        "PGDN": "PAGEDOWN",
        "PAGE_UP": "PAGEUP",
        "PAGE_DOWN": "PAGEDOWN",
        "SPACEBAR": "SPACE",
        "PRTSC": "PRINTSCREEN",
        "PRINTSCRN": "PRINTSCREEN",
    }
    parts: list[str] = []
    for part in re.split(r"\s*\+\s*", str(value or "")):
        token = part.strip()
        if not token:
            continue
        # Single punctuation keys are meaningful as-is; only names are uppercased, and a name is
        # read without its spaces, underscores, or hyphens ("Page Up", v5_reconcile run 1703).
        if len(token) == 1:
            parts.append(token.upper() if token.isalpha() else token)
            continue
        normalized = token.upper()
        squashed = re.sub(r"[\s_-]+", "", normalized)
        parts.append(aliases.get(normalized.replace("-", "_"), aliases.get(squashed, squashed)))
    return parts


def _key_code(key_name: str) -> int:
    if key_name in _VK:
        return _VK[key_name]
    if re.fullmatch(r"F(?:[1-9]|1[0-9]|2[0-4])", key_name):
        return 0x70 + int(key_name[1:]) - 1
    if len(key_name) == 1:
        if key_name.isalnum():
            return ord(key_name.upper())
        if key_name in _VK:
            return _VK[key_name]
    raise ValueError(f"Unsupported keyboard key: {key_name[:24]}")


def _check_key(value: str) -> tuple[list[str], int]:
    """The modifiers and key code a key or chord presses; ValueError (nothing pressed) when it names
    no key, a modifier that is not one, or a blocked system shortcut."""
    parts = _normalize_key_parts(value)
    if not parts:
        raise ValueError("A key action must name a key.")
    held = {_MODIFIERS[part.upper()] for part in parts[:-1] if part.upper() in _MODIFIERS}
    for blocked in _BLOCKED_SHORTCUTS:
        # Meta+R is Win+R, Control+Alt+Delete is Ctrl+Alt+Delete, and Shift+Alt+F4 still closes the app.
        if parts[-1].upper() == blocked[-1] and {_MODIFIERS[name] for name in blocked[:-1]} <= held:
            raise ValueError("That system shortcut is not available to autonomous computer use.")
    modifiers = parts[:-1]
    if any(item not in _MODIFIERS for item in modifiers):
        raise ValueError("Unsupported keyboard modifier.")
    return modifiers, _key_code(parts[-1])


def _key_steps(value: str, *, repeat: int = 1, hold_ms: int = 0) -> Iterator[float]:
    """Press one key or modifier combo, optionally repeated (for example BACKSPACE x30) or held
    down for hold_ms before release (a held arrow in a game, push-to-talk).

    Uses SendInput with correct extended-key flags so navigation and editing keys reach
    applications exactly as a human keyboard would deliver them. A held key is one press: an
    injected key-down gets no typematic repeat, so holding never multiplies the keystroke.
    """
    import ctypes
    from ctypes import wintypes

    modifiers, key_code = _check_key(value)
    presses = max(1, min(200, int(repeat or 1)))
    hold_seconds = max(0, min(5000, int(hold_ms or 0))) / 1000

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    input_keyboard = 1
    keyeventf_extendedkey = 0x0001
    keyeventf_keyup = 0x0002

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
        ]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.POINTER(wintypes.ULONG)),
        ]

    class INPUT_UNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("union",)
        _fields_ = [("type", wintypes.DWORD), ("union", INPUT_UNION)]

    def key_event(code: int, *, up: bool) -> INPUT:
        flags = keyeventf_keyup if up else 0
        if code in _EXTENDED_KEYS:
            flags |= keyeventf_extendedkey
        return INPUT(type=input_keyboard, ki=KEYBDINPUT(code, 0, flags, 0, None))

    def dispatch(events: list[INPUT]) -> None:
        if not events:
            return
        array = (INPUT * len(events))(*events)
        sent = int(user32.SendInput(len(events), array, ctypes.sizeof(INPUT)))
        if sent != len(events):
            raise OSError(
                ctypes.get_last_error(), "SendInput did not deliver the complete key sequence."
            )

    dispatch([key_event(_MODIFIERS[modifier], up=False) for modifier in modifiers])
    try:
        for index in range(presses):
            if index:
                # Repeated presses go one at a time at human speed. A single batched
                # SendInput burst is delivered faster than applications sample the
                # keyboard, so they coalesce it into one keystroke.
                yield 0.03
            if not hold_seconds:
                dispatch([key_event(key_code, up=False), key_event(key_code, up=True)])
                continue
            dispatch([key_event(key_code, up=False)])
            try:
                yield hold_seconds
            finally:
                dispatch([key_event(key_code, up=True)])
    finally:
        dispatch([key_event(_MODIFIERS[modifier], up=True) for modifier in reversed(modifiers)])


def _send_key(value: str, *, repeat: int = 1, hold_ms: int = 0) -> None:
    for pause in _key_steps(value, repeat=repeat, hold_ms=hold_ms):
        time.sleep(pause)


async def _send_key_async(value: str, *, repeat: int = 1, hold_ms: int = 0) -> None:
    """The same key sequence with awaited pauses: 200 held presses used to block the server's
    event loop (cancellation, the extension hub, health checks) for minutes. A cancel lands at an
    await, and closing the steps still releases the held key and every modifier."""
    steps = _key_steps(value, repeat=repeat, hold_ms=hold_ms)
    try:
        for pause in steps:
            await asyncio.sleep(pause)
    finally:
        steps.close()

def _set_foreground_window(handle: int) -> None:
    """Best-effort foreground switch. Windows refuses SetForegroundWindow from a
    background process unless the caller recently produced input; a transient ALT
    press satisfies that foreground-lock heuristic. The caller decides success by
    re-reading GetForegroundWindow, so every attempt here is allowed to fail."""
    import win32api
    import win32con
    import win32gui

    if win32gui.IsIconic(handle):
        win32gui.ShowWindow(handle, win32con.SW_RESTORE)
    try:
        win32gui.SetForegroundWindow(handle)
        return
    except Exception:
        pass
    win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
    try:
        win32gui.SetForegroundWindow(handle)
    except Exception:
        try:
            _desktop().window(handle=handle).wrapper_object().set_focus()
        except Exception:
            pass
    finally:
        win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)


_MOUSE_BUTTONS = {"left", "right", "middle"}


def _absolute_point(x: int, y: int) -> tuple[int, int]:
    left, top, width, height = _virtual_screen()
    return (
        max(left, min(left + width - 1, left + int(x))),
        max(top, min(top + height - 1, top + int(y))),
    )


def _remember_target_window(handle: Any = None) -> int:
    """Record the window this body was told to act on; defaults to the live foreground."""
    global _LAST_TARGET_WINDOW
    try:
        if handle is None:
            import win32gui

            handle = win32gui.GetForegroundWindow()
        with _CONTROL_LOCK:
            _LAST_TARGET_WINDOW = int(handle or 0)
            return _LAST_TARGET_WINDOW
    except Exception:
        return 0


def _same_app_window(handle: int, targeted: int) -> bool:
    """Whether a window belongs to the app this body targeted: that window itself, or another window
    of the same process (a Save dialog or message box the app opened)."""
    if not handle or not targeted:
        return False
    if int(handle) == int(targeted):
        return True
    try:
        import win32process

        thread, process = win32process.GetWindowThreadProcessId(int(handle))
        targeted_thread, targeted_process = win32process.GetWindowThreadProcessId(int(targeted))
    except Exception:
        return False
    # A closed or unknown window reports thread 0 (and an undefined process): no evidence of the app.
    return bool(thread and targeted_thread and process) and process == targeted_process


def _foreground_window() -> tuple[int, str]:
    try:
        import win32gui

        handle = int(win32gui.GetForegroundWindow() or 0)
        return handle, _safe_text(win32gui.GetWindowText(handle), 180)
    except Exception:
        return 0, ""


def _mouse_move(x: int, y: int) -> None:
    """Move the physical pointer without pressing anything (hover)."""
    import win32api

    win32api.SetCursorPos(_absolute_point(x, y))


# The model names gesture modifiers the way the browser body does; each is one physical key.
_GESTURE_MODIFIER_KEYS = {"shift": "SHIFT", "ctrl": "CTRL", "alt": "ALT", "meta": "WIN"}


def _gesture_modifier_codes(modifiers: Any) -> list[int]:
    codes: list[int] = []
    for item in modifiers or ():
        name = _GESTURE_MODIFIER_KEYS.get(str(item or "").strip().lower())
        if name is None:
            raise ValueError("Unsupported gesture modifier.")
        code = _MODIFIERS[name]
        if code not in codes:
            codes.append(code)
    return codes


@contextmanager
def _held_modifiers(modifiers: Any) -> Iterator[None]:
    """Hold modifier keys down for exactly one pointer gesture (shift-click, ctrl-drag,
    ctrl+wheel), and release every key this gesture pressed even when the gesture fails."""
    codes = _gesture_modifier_codes(modifiers)
    if not codes:
        yield
        return
    import win32api
    import win32con

    pressed: list[int] = []
    try:
        for code in codes:
            win32api.keybd_event(code, 0, 0, 0)
            pressed.append(code)
        # Let the application register the held keys before the pointer event reads them.
        time.sleep(0.03)
        yield
    finally:
        for code in reversed(pressed):
            try:
                win32api.keybd_event(code, 0, win32con.KEYEVENTF_KEYUP, 0)
            except Exception:
                pass


def _mouse_button_flags(button: str) -> tuple[int, int]:
    import win32con

    normalized = str(button or "left").strip().lower()
    if normalized not in _MOUSE_BUTTONS:
        raise ValueError("Unsupported mouse button.")
    return {
        "left": (win32con.MOUSEEVENTF_LEFTDOWN, win32con.MOUSEEVENTF_LEFTUP),
        "right": (win32con.MOUSEEVENTF_RIGHTDOWN, win32con.MOUSEEVENTF_RIGHTUP),
        "middle": (win32con.MOUSEEVENTF_MIDDLEDOWN, win32con.MOUSEEVENTF_MIDDLEUP),
    }[normalized]


def _gesture_label(button: str, clicks: int, hold_ms: int = 0, modifiers: Any = ()) -> str:
    """The overlay's short name for a non-plain gesture ("ctrl+shift left x1 hold"); empty for a plain click."""
    try:
        count = max(1, int(clicks or 1))
    except (TypeError, ValueError):
        count = 1
    held = "+".join(str(item) for item in (modifiers or ()))
    if str(button or "left") == "left" and count == 1 and not held and not int(hold_ms or 0):
        return ""
    return " ".join(
        part for part in (held, f"{button or 'left'} x{count}", "hold" if int(hold_ms or 0) else "") if part
    )


def _mouse_click(
    x: int,
    y: int,
    *,
    button: str = "left",
    clicks: int = 1,
    hold_ms: int = 0,
    modifiers: Any = (),
) -> None:
    """Click at a point with any mouse button, single/double/triple, like a human would; hold_ms
    keeps the button down (press-and-hold) and modifiers are held for the whole gesture."""
    import win32api

    down, up = _mouse_button_flags(button)
    hold_seconds = max(0, min(5000, int(hold_ms or 0))) / 1000
    global _LAST_CLICK_POINT
    point = _absolute_point(x, y)
    with _CONTROL_LOCK:
        _LAST_CLICK_POINT = point
    win32api.SetCursorPos(point)
    with _held_modifiers(modifiers):
        for index in range(max(1, min(3, int(clicks or 1)))):
            if index:
                # Stay inside the system double-click interval so apps see one gesture.
                time.sleep(0.05)
            win32api.mouse_event(down, 0, 0, 0, 0)
            try:
                if hold_seconds:
                    time.sleep(hold_seconds)
            finally:
                win32api.mouse_event(up, 0, 0, 0, 0)


def _mouse_drag(
    x: int,
    y: int,
    to_x: int,
    to_y: int,
    *,
    button: str = "left",
    path: Any = (),
    hold_ms: int = 0,
    modifiers: Any = (),
) -> None:
    """Press at one point, move in visible steps (through any waypoints), and release at another
    (text selection, sliders, dials, drag-and-drop). Intermediate moves matter: apps ignore
    instant jumps. hold_ms waits with the button down before moving (a list that starts a drag
    only after a long press); modifiers are held for the whole gesture (ctrl-drag copies)."""
    import win32api

    down, up = _mouse_button_flags(button)
    route = [_absolute_point(x, y)]
    for point in list(path or ())[:40]:
        route.append(_absolute_point(int(point[0]), int(point[1])))
    route.append(_absolute_point(to_x, to_y))
    hold_seconds = max(0, min(5000, int(hold_ms or 0))) / 1000
    win32api.SetCursorPos(route[0])
    time.sleep(0.05)
    with _held_modifiers(modifiers):
        win32api.mouse_event(down, 0, 0, 0, 0)
        try:
            if hold_seconds:
                time.sleep(hold_seconds)
            for leg in range(1, len(route)):
                (start_x, start_y), (end_x, end_y) = route[leg - 1], route[leg]
                # A straight drag keeps its 24 steps; a routed one steps each leg by its length.
                steps = (
                    24
                    if len(route) == 2
                    else max(4, min(40, round(math.hypot(end_x - start_x, end_y - start_y) / 12)))
                )
                for step in range(1, steps + 1):
                    win32api.SetCursorPos(
                        (
                            int(start_x + (end_x - start_x) * step / steps),
                            int(start_y + (end_y - start_y) * step / steps),
                        )
                    )
                    time.sleep(0.012)
        finally:
            # Never leave a button held: a failed drag still releases (and so drops) where it is.
            win32api.mouse_event(up, 0, 0, 0, 0)


def _app_name_allowed(name: str) -> bool:
    normalized = _safe_text(name, 100).casefold()
    if (
        not normalized
        or normalized in _PROHIBITED_APP_NAMES
        or normalized.startswith("uninstall")
    ):
        return False
    return not any(
        pattern.search(normalized)
        for pattern in (
            re.compile(r"^(?:idle|python)(?:\s|$|\d)"),
            re.compile(r"(?:command prompt|registry editor|script host)"),
            re.compile(r"^(?:git bash|git cmd|run|wsl)(?:\s|$)"),
            re.compile(r"(?:^|\s)(?:powershell|terminal)(?:\s|$|\()"),
        )
    )


def _start_menu_app_candidates() -> list[tuple[str, str, str]]:
    roots = [
        Path(os.environ.get("APPDATA") or "") / "Microsoft/Windows/Start Menu/Programs",
        Path(os.environ.get("PROGRAMDATA") or "") / "Microsoft/Windows/Start Menu/Programs",
    ]
    candidates: list[tuple[str, str, str]] = []
    for root in roots:
        if not root.is_dir():
            continue
        for shortcut in sorted(root.rglob("*.lnk"), key=lambda item: item.stem.casefold()):
            name = _safe_text(shortcut.stem, 100)
            if _app_name_allowed(name):
                candidates.append((name, "shortcut", str(shortcut)))
    return candidates


def _shell_app_candidates() -> list[tuple[str, str, str]]:
    if not _windows_available():
        return []
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        shell = win32com.client.Dispatch("Shell.Application")
        folder = shell.Namespace("shell:AppsFolder")
        if folder is None:
            return []
        items = folder.Items()
        candidates: list[tuple[str, str, str]] = []
        for index in range(int(items.Count)):
            item = items.Item(index)
            name = _safe_text(getattr(item, "Name", ""), 100)
            target = _safe_text(getattr(item, "Path", ""), 500)
            if name and target and _app_name_allowed(name):
                candidates.append((name, "shell_app", target))
        return candidates
    finally:
        pythoncom.CoUninitialize()


def _application_inventory() -> tuple[list[dict[str, str]], dict[str, dict[str, str]]]:
    global _APP_CACHE
    now = time.monotonic()
    if _APP_CACHE is not None and now - _APP_CACHE[0] < 300:
        return _APP_CACHE[1], _APP_CACHE[2]
    candidates = [*_start_menu_app_candidates(), *_shell_app_candidates()]
    candidates.sort(key=lambda item: (item[0].casefold(), item[1], item[2].casefold()))
    rows: list[dict[str, str]] = []
    index: dict[str, dict[str, str]] = {}
    seen_names: set[str] = set()
    for name, kind, target in candidates:
        normalized_name = name.casefold()
        if normalized_name in seen_names or not _app_name_allowed(name):
            continue
        app_id = "app:" + hashlib.sha256(f"{kind}:{target}".encode("utf-8")).hexdigest()[:16]
        seen_names.add(normalized_name)
        index[app_id] = {"kind": kind, "target": target, "name": name}
        rows.append({"app_id": app_id, "name": name})
        if len(rows) >= 160:
            break
    _APP_CACHE = (now, rows, index)
    return rows, index


def chrome_profiles() -> list[dict[str, str]]:
    """Enumerate installed Chrome profiles from the browser's own Local State catalog.

    Pure eyes: directory id + display name, in Chrome's own order. This never chooses a
    profile — which profile to use is the user's call, asked through the question flow."""
    import json as _json

    try:
        local_state = (
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "Google"
            / "Chrome"
            / "User Data"
            / "Local State"
        )
        if not local_state.is_file():
            return []
        data = _json.loads(local_state.read_text(encoding="utf-8"))
        info_cache = (data.get("profile") or {}).get("info_cache") or {}
        profiles = [
            {
                "directory": str(directory),
                "name": _safe_text(meta.get("name"), 80) or str(directory),
            }
            for directory, meta in info_cache.items()
            if isinstance(meta, dict)
        ]
        profiles.sort(key=lambda profile: profile["directory"])
        return profiles
    except Exception:  # noqa: BLE001 - unreadable catalog means no profile knowledge, never a crash
        return []


def launch_application_named(name_fragment: str) -> bool:
    """Launch an installed application by case-insensitive name from the exact enumerated
    inventory. Exact-name matches win over substring matches so "google chrome" can never
    accidentally start "Chrome Remote Desktop". Used by the runtime to open its own
    browser body; never driven by page content."""
    fragment = str(name_fragment or "").strip().casefold()
    if not fragment or not runtime_enabled():
        return False
    try:
        _, index = _application_inventory()
    except Exception:  # noqa: BLE001 - a failed inventory scan is a launch failure, not a crash
        return False
    exact = [t for t in index.values() if str(t.get("name") or "").casefold() == fragment]
    partial = [t for t in index.values() if fragment in str(t.get("name") or "").casefold()]
    for target in exact + [t for t in partial if t not in exact]:
        try:
            if _launch_inventory_target(target):
                return True
        except Exception:  # noqa: BLE001 - try the next enumerated candidate
            continue
    return False


def _launch_inventory_target(target: dict[str, str]) -> bool:
    kind = str(target.get("kind") or "")
    exact = str(target.get("target") or "")
    if kind == "shortcut":
        shortcut = Path(exact)
        if not shortcut.is_file():
            return False
        os.startfile(str(shortcut))  # noqa: S606 - exact enumerated .lnk path, never a shell string
        return True
    if kind != "shell_app" or not exact:
        return False
    import pythoncom
    import win32com.client

    pythoncom.CoInitialize()
    try:
        shell = win32com.client.Dispatch("Shell.Application")
        folder = shell.Namespace("shell:AppsFolder")
        if folder is None:
            return False
        items = folder.Items()
        for index in range(int(items.Count)):
            item = items.Item(index)
            if _safe_text(getattr(item, "Path", ""), 500) == exact:
                item.InvokeVerb("open")
                return True
        return False
    finally:
        pythoncom.CoUninitialize()


def _set_cursor(abs_x: int, abs_y: int) -> None:
    import win32api

    win32api.SetCursorPos((int(abs_x), int(abs_y)))


def _wheel(dx: int, dy: int) -> None:
    """One wheel gesture at the pointer: horizontal for wide tables, timelines, and canvases; vertical
    otherwise (a zero gesture is one vertical notch of nothing)."""
    import win32api
    import win32con

    if int(dx or 0):
        win32api.mouse_event(win32con.MOUSEEVENTF_HWHEEL, 0, 0, int(max(-2400, min(2400, int(dx)))), 0)
    if int(dy or 0) or not int(dx or 0):
        wheel_delta = int(max(-2400, min(2400, -int(dy))))
        win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, wheel_delta, 0)


def _open_path(target: Path) -> None:
    os.startfile(str(target))  # noqa: S606 - a document type from the allowlist, never an executable


def _parse_window_id(window_id: Any) -> int | None:
    match = re.fullmatch(r"hwnd:(\d+)", str(window_id or ""))
    return int(match.group(1)) if match else None


def _foreground_handle() -> int:
    import win32gui

    return int(win32gui.GetForegroundWindow() or 0)


class _NativeLoop:
    """One dedicated thread, with its own event loop, for every native desktop operation.

    Screen capture, UI Automation reads, and paced SendInput are blocking calls; run on the server's
    event loop they froze Stop, the extension hub, and phone requests for the length of each action
    (seconds for a drag hold). Here they run one at a time on this thread instead, and a cancelled
    caller still cancels the operation at its next await.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    def loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None or self._loop.is_closed():
                ready = threading.Event()
                holder: dict[str, asyncio.AbstractEventLoop] = {}

                def run() -> None:
                    _join_com_apartment()
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    holder["loop"] = loop
                    ready.set()
                    loop.run_forever()

                threading.Thread(target=run, name="desktop-native", daemon=True).start()
                ready.wait()
                self._loop = holder["loop"]
            return self._loop


def _join_com_apartment() -> None:
    """UI Automation is COM: the native thread joins the apartment pywinauto chose (MTA by default)."""
    if not _windows_available():
        return
    try:
        import pythoncom

        pythoncom.CoInitializeEx(int(getattr(sys, "coinit_flags", 0)))
    except Exception:  # noqa: BLE001 - per-call COM retries still apply
        pass


_NATIVE = _NativeLoop()


def _on_native_thread(cls: type) -> type:
    """Run each public coroutine method of ``cls`` on the native desktop thread."""

    def wrap(method: Any) -> Any:
        @functools.wraps(method)
        async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
            native = _NATIVE.loop()
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is native:
                return await method(self, *args, **kwargs)
            future = asyncio.run_coroutine_threadsafe(method(self, *args, **kwargs), native)
            return await asyncio.wrap_future(future)

        return wrapper

    for name, value in list(vars(cls).items()):
        if not name.startswith("_") and asyncio.iscoroutinefunction(value):
            setattr(cls, name, wrap(value))
    return cls


@_on_native_thread
@dataclass
class DesktopBody:
    """Windows implementation of the screenshot/action interface used by ``run_vision_loop``."""

    device_id: str = "windows-desktop"
    execution_surface: str = "desktop"
    _sensitive_selectors: set[str] = field(default_factory=set, init=False, repr=False)
    # Window ids already open when this run started (window_ids_snapshot); None when unknown.
    windows_before_task: frozenset[str] | None = field(default=None, repr=False)
    # The clipboard sequence number when this body started: what the clipboard held then is the user's.
    _clipboard_start_sequence: int | None = field(default=None, init=False, repr=False)
    # Clipboard sequence numbers this run's own input actions produced, each with whether the copy may
    # hold a password control's value (see _records_clipboard_copies). Only these are readable.
    _clipboard_copies: dict[int, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)
    # Whether the front window the run last captured showed a password (or run-marked sensitive) control.
    _password_control_shown: bool = field(default=False, init=False, repr=False)
    # Control ids stay with their element across this run's captures (control_ids.py).
    _control_ids: ControlIdRegistry = field(default_factory=ControlIdRegistry, init=False, repr=False)

    def __post_init__(self) -> None:
        _ensure_dpi_awareness()
        self._clipboard_start_sequence = self._native._clipboard_sequence()
        # A new run looks where the foreground window is until it pins a monitor itself.
        self._native._VIEW["pinned"] = None

    def _note_clipboard_copy(self, sequence: int, *, sensitive: bool) -> None:
        self._clipboard_copies[int(sequence)] = {"sensitive": bool(sensitive)}
        while len(self._clipboard_copies) > _CLIPBOARD_COPY_LEDGER_LIMIT:
            self._clipboard_copies.pop(next(iter(self._clipboard_copies)))

    async def set_control_indicator(
        self,
        active: bool,
        *,
        run_id: int | None = None,
        message: str = "Lumen is using your computer",
        timeout: float = 8.0,
        surface: str = "desktop",
    ) -> dict[str, Any]:
        """Publish exact body activity for the packaged host's screen overlay.

        ``surface`` distinguishes a run driving the REAL desktop (the host clears its
        own window out of the way) from a browser run's overlay mirror."""
        del timeout
        global _CONTROL_STATUS_MESSAGE
        key = int(run_id) if run_id is not None else 0
        clean_surface = "browser" if str(surface).strip().lower() == "browser" else "desktop"
        with _CONTROL_LOCK:
            if active:
                _ACTIVE_CONTROL_RUN_IDS[key] = clean_surface
                _CONTROL_STATUS_MESSAGE = " ".join(str(message or "").split())[:140]
            else:
                _ACTIVE_CONTROL_RUN_IDS.pop(key, None)
                if not _ACTIVE_CONTROL_RUN_IDS:
                    _CONTROL_STATUS_MESSAGE = ""
            visible = bool(_ACTIVE_CONTROL_RUN_IDS and self._native.runtime_enabled())
        return {
            "ok": True,
            "active": bool(active),
            "visible": visible,
            "surface": "desktop",
        }

    def tool_context(self) -> dict[str, Any]:
        applications, _ = self._native._application_inventory()
        return {
            "execution_surface": "desktop",
            "windows": self._native._window_inventory(),
            "applications": applications,
            "key_format": "KEY or MODIFIER+KEY, for example TAB, CTRL+A, ALT+TAB, or WIN",
            "typing_requires_caret": (
                "Bringing a window to the front does not put the caret in its text area. "
                "Before typing, click the middle of the exact text area - not its edge, "
                "title bar, tab strip, or toolbar - so the keystrokes land in it."
            ),
            "tool_targets": {
                "focus_window": (
                    "Use an exact window_id from windows. With several monitors, monitor:N (N from page "
                    "evidence monitors) shows that whole monitor until you focus a window or launch an app; "
                    "otherwise the screenshot is the monitor holding the active window."
                ),
                "launch_app": "Use an exact app_id from applications.",
            },
        }

    async def navigate(self, url: str, *, timeout: float = 45.0) -> dict[str, Any]:
        del timeout
        self._native._require_enabled()
        # The real screen has no address bar this body drives: reporting success would be a false receipt.
        return {
            "ok": False,
            "action": "navigate",
            "desktop_surface": True,
            "failure_reason": "navigate_unavailable_on_desktop",
            # The URL is not echoed: its query or fragment can carry a token, and errors reach logs and the model.
            "error": "The desktop body cannot open a URL; open it from a browser window on the screen.",
        }

    async def observe(self, *, timeout: float = 30.0) -> dict[str, Any]:
        del timeout
        shot = await self.capture_screenshot()
        return {
            "ok": bool(shot.get("ok")),
            "url": shot.get("url"),
            "title": shot.get("title"),
            "page_evidence": shot.get("page_evidence") or {},
        }

    async def capture_screenshot(self, *, full_page: bool = False, timeout: float = 24.0) -> dict[str, Any]:
        del full_page, timeout
        self._native._require_enabled()
        # What is drawn over the window in front, read right before and right after the grab: a
        # notification that comes or goes during it is still blacked out of that window's text.
        monitors = self._native._select_view()
        stacking_before = self._native._foreground_stacking()
        image = self._native._capture_image()
        stacking_after = self._native._foreground_stacking()
        controls, password_boxes = self._native._active_controls()
        self._control_ids.relabel(controls)
        sensitive_runtime_ids: set[tuple[int, ...]] = set()
        for control in controls:
            if str(control.get("selector") or "") in self._sensitive_selectors:
                box = control.get("box") if isinstance(control.get("box"), list) else []
                if len(box) >= 4:
                    password_boxes.append(
                        (int(box[0]), int(box[1]), int(box[0] + box[2]), int(box[1] + box[3]))
                    )
                # Masking the pixels is not enough: the inventory carried the same value as text.
                control["value"] = ""
                control["value_redacted"] = True
                parsed = self._native._parse_selector(str(control.get("selector") or ""))
                if parsed:
                    sensitive_runtime_ids.add(parsed[1])
        caret = self._native._caret_state()
        if caret and sensitive_runtime_ids:
            focused = self._native._focus_identity()
            if focused is None or focused in sensitive_runtime_ids:
                # The caret's line and selection are the sensitive control's text.
                caret = {key: value for key, value in caret.items() if key not in {"line_text", "selected_text"}}
                caret["text_redacted"] = True
        self._password_control_shown = bool(password_boxes)
        if password_boxes:
            draw = ImageDraw.Draw(image)
            for box in password_boxes:
                draw.rectangle(box, fill=(24, 24, 27))
        windows = self._native._window_inventory()
        active = next((item for item in windows if item.get("active")), None)
        view_monitor = 0
        if len(monitors) > 1:
            view_monitor = _monitor_of_box(monitors, list(self._native._virtual_screen()))
            for item in windows:
                item["monitor"] = _monitor_of_box(monitors, item.get("box") or [0, 0, 0, 0])
        before_task = self.windows_before_task
        if before_task is not None:
            for item in windows:
                if item.get("window_id") in before_task:
                    item["opened_before_task"] = True
        # A browser window's own address, which its tab strip and toolbar controls otherwise crowd
        # out of the inventory: the web page in the active window says where it is.
        document_url = _document_url(controls) if active else ""
        if active and document_url:
            active["document_url"] = document_url
        if active and self._native._window_class(active.get("window_id")) not in self._native._SHELL_WINDOW_CLASSES:
            # Where the active window sits in this capture (window rects are screen coordinates;
            # the capture starts at the virtual screen's origin), and what is drawn over it there.
            origin_left, origin_top, _, _ = self._native._virtual_screen()
            box = active.get("box") or [0, 0, 0, 0]
            active["image_box"] = [int(box[0]) - origin_left, int(box[1]) - origin_top, int(box[2]), int(box[3])]
            steady = (
                stacking_before[0] == stacking_after[0] == active.get("window_id")
                and stacking_before[1] == stacking_after[1] == [int(item) for item in box]
            )
            drawn_over = [*stacking_before[2], *[rect for rect in stacking_after[2] if rect not in stacking_before[2]]]
            active["drawn_over"] = drawn_over if steady and _STACKING_UNKNOWN[0] not in drawn_over else list(_STACKING_UNKNOWN)
        # The focused multi-line text control's lines; never one marked sensitive, listed here or not,
        # and never in a browser window showing a web page: the page's own sensitivity marks (a pasted
        # key or recovery phrase in a textarea) live in the browser, not here.
        marked_runtime_ids = set(sensitive_runtime_ids)
        for marked in self._sensitive_selectors:
            parsed_marked = self._native._parse_selector(marked)
            if parsed_marked:
                marked_runtime_ids.add(parsed_marked[1])
        focused_lines = {} if document_url else self._native._focused_text_lines(controls, marked_runtime_ids)
        folder_view = (
            self._native._explorer_folder_view(active.get("window_id"), str(active.get("title") or ""))
            if active and self._native._window_class(active.get("window_id")) == self._native._FOLDER_WINDOW_CLASS
            else {}
        )
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=78, optimize=True)
        width, height = image.size
        window_lines = [
            f"[{item['window_id']}] {item['title']}"
            f"{' (active)' if item.get('active') else ''}"
            f"{' on monitor ' + str(item['monitor']) if item.get('monitor') else ''}"
            f"{' (open before this task)' if item.get('opened_before_task') else ''}"
            f"{' at ' + item['document_url'] if item.get('document_url') else ''}"
            for item in windows[:16]
        ]
        # Deterministic surface identity for mask binding and same-surface checks: the
        # active window plus the exact UIA control set. The runtime's sensitive-mask flow
        # requires it; changing window, title, or control tree changes the signature.
        active_window_id = (active or {}).get("window_id", "none")
        active_title = (active or {}).get("title") or ""
        control_selectors = "|".join(str(item.get("selector") or "") for item in controls)
        surface_signature = hashlib.sha256(
            f"{active_window_id}|{active_title}|{control_selectors}".encode("utf-8")
        ).hexdigest()[:40]
        stable_surface_signature = hashlib.sha256(
            f"{active_title}|{len(controls)}".encode("utf-8")
        ).hexdigest()[:40]
        return {
            "ok": True,
            "image": base64.b64encode(output.getvalue()).decode("ascii"),
            "width": width,
            "height": height,
            "dpr": 1,
            "url": f"{self._native._SURFACE_URL_PREFIX}{(active or {}).get('window_id', 'none')}",
            "title": (active or {}).get("title") or self._native._DESKTOP_TITLE,
            "surface_signature": surface_signature,
            "stable_surface_signature": stable_surface_signature,
            "scroll_y": 0,
            "scroll_height": height,
            "page_evidence": {
                "url": f"{self._native._SURFACE_URL_PREFIX}{(active or {}).get('window_id', 'none')}",
                "title": (active or {}).get("title") or self._native._DESKTOP_TITLE,
                "viewport_text": self._native._WINDOWS_HEADING + "\n" + "\n".join(window_lines),
                "width": width,
                "height": height,
                "scroll_y": 0,
                "scroll_height": height,
                "controls": controls,
                "windows": windows,
                # Where the caret is in the focused text control (line, column, selection).
                "caret": caret,
                # The focused multi-line text control's text, numbered lines around the caret.
                **({"focused_text_lines": focused_lines} if focused_lines else {}),
                # The front File Explorer window's folder and its items' real file names.
                **({"folder_view": folder_view} if folder_view else {}),
                "password_controls_masked": len(password_boxes),
                "execution_surface": "desktop",
                **({"document_url": document_url} if document_url else {}),
                # Several monitors: the screenshot is one of them (in_view); windows say where they are.
                **(
                    {
                        "monitors": [
                            {
                                "monitor": item["monitor"],
                                "primary": bool(item.get("primary")),
                                "size": [int(item["rect"][2]), int(item["rect"][3])],
                                "in_view": int(item["monitor"]) == view_monitor,
                                "pinned": int(item["monitor"]) == int(self._native._VIEW.get("pinned") or 0),
                            }
                            for item in monitors
                        ]
                    }
                    if view_monitor
                    else {}
                ),
            },
        }

    def _mask_signature(self, selectors: list[str]) -> str:
        payload = "|".join(selectors)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]

    async def mark_sensitive_screenshot_control(
        self,
        selector: str,
        *,
        expected_page_context: dict[str, Any],
        timeout: float = 12.0,
    ) -> dict[str, Any]:
        del timeout
        self._native._require_enabled()
        exact = str(selector or "").strip()
        wrapper = self._native._resolve_control(exact)
        current_url = str((await self.capture_screenshot()).get("url") or "")
        expected_url = str((expected_page_context or {}).get("url") or "")
        if wrapper is None or (expected_url and current_url != expected_url):
            return {
                "ok": False,
                "selector_bound": False,
                "page_context_bound": False,
                "failure_reason": "desktop_sensitive_target_or_surface_changed",
            }
        self._sensitive_selectors.add(exact)
        return {
            "ok": True,
            "selector_bound": True,
            "page_context_bound": True,
            "bound_selectors": [exact],
            "mask_surface_signature": self._mask_signature([exact]),
            "mask_mode": "desktop_uia",
        }

    async def ensure_sensitive_screenshot_control(
        self,
        selector: str,
        *,
        expected_page_context: dict[str, Any],
        timeout: float = 12.0,
    ) -> dict[str, Any]:
        return await self.ensure_sensitive_screenshot_controls(
            [selector],
            expected_page_context=expected_page_context,
            timeout=timeout,
        )

    async def ensure_sensitive_screenshot_controls(
        self,
        selectors: list[str],
        *,
        expected_page_context: dict[str, Any],
        timeout: float = 12.0,
    ) -> dict[str, Any]:
        del timeout
        self._native._require_enabled()
        exact = [str(item or "").strip() for item in selectors if str(item or "").strip()]
        if not exact or any(self._native._resolve_control(item) is None for item in exact):
            return {
                "ok": False,
                "selector_bound": False,
                "page_context_bound": False,
                "failure_reason": "desktop_sensitive_target_changed",
            }
        current_signature = self._mask_signature(exact)
        expected_signature = str((expected_page_context or {}).get("mask_surface_signature") or "")
        if expected_signature and current_signature != expected_signature:
            return {
                "ok": False,
                "selector_bound": False,
                "page_context_bound": False,
                "failure_reason": "desktop_sensitive_surface_changed",
                "mask_surface_signature": current_signature,
            }
        self._sensitive_selectors.update(exact)
        return {
            "ok": True,
            "selector_bound": True,
            "page_context_bound": True,
            "bound_selectors": exact,
            "mask_surface_signature": current_signature,
            "mask_mode": "desktop_uia",
            "binding_rebound": True,
        }

    @_records_clipboard_copies
    async def vision_click(
        self,
        x: int,
        y: int,
        *,
        button: str = "left",
        clicks: int = 1,
        hold_ms: int = 0,
        modifiers: list[str] | None = None,
        allow_final_submit: bool = False,
        allow_guarded_intermediate: bool = False,
        expected_target_identity: dict[str, Any] | None = None,
        expected_page_context: dict[str, Any] | None = None,
        label_text: str = "",
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        del allow_guarded_intermediate, expected_target_identity, expected_page_context, timeout
        self._native._require_enabled()
        if allow_final_submit:
            # Final authority is one plain left click, exactly as on the browser surface.
            button, clicks, hold_ms, modifiers = "left", 1, 0, None
        before = self._native._capture_image()
        focus_before = self._native._focus_identity()
        before_windows = self._native._window_inventory()
        before_title = next((item["title"] for item in before_windows if item.get("active")), "")
        # A click that names the option it chooses ("Approve") is checked against that option's label
        # drawn near the point (v5_memory run 1704: an "Approve" click aimed at the Deny radio beside it
        # dispatched a denial). The click may move onto the label only inside the control the model
        # aimed at - the control the click preflight judged; a label on another control is reported
        # back with where it is and nothing is clicked. Only one exact reading in reach counts. A name
        # with no letter is a value to read back (a slider's 172), not a label: the point stands.
        label_bound: dict[str, Any] = {}
        wanted_label = " ".join(str(label_text or "").split())
        if wanted_label and any(character.isalpha() for character in wanted_label) and not allow_final_submit:
            found = _ocr_locate(before, wanted_label, int(x), int(y), reach=(_LABEL_CLICK_REACH, _LABEL_CLICK_REACH))
            single = (
                found.get("found")
                and not found.get("approximate")
                and int(found.get("occurrences") or 0) == 1
                and float(found.get("distance") or 0) <= _LABEL_CLICK_REACH
            )
            if single:
                left_edge, top_edge, right_edge, bottom_edge = found["box"]
                label_x, label_y = (left_edge + right_edge) // 2, (top_edge + bottom_edge) // 2
                on_label = left_edge <= int(x) <= right_edge and top_edge <= int(y) <= bottom_edge
                aimed_id = self._native._exact_click_target(int(x), int(y)).get("control_id") if not on_label else ()
                label_id = self._native._exact_click_target(label_x, label_y).get("control_id") if not on_label else ()
                if on_label or (aimed_id and aimed_id == label_id):
                    x, y = label_x, label_y
                    label_bound = {"label_bound": True}
                else:
                    return {
                        "ok": False,
                        "action": "click",
                        "failure_reason": "label_elsewhere",
                        "label_text": wanted_label[:120],
                        "label_point": [int(label_x), int(label_y)],
                        "activation_attempted": False,
                        "activation_dispatched": False,
                        "desktop_surface": True,
                    }
            else:
                label_bound = {
                    "label_bound": False,
                    **({"label_ambiguous": True} if found.get("found") else {}),
                }
        # Exact pointer control: resolve the approximate model point to the precise
        # clickable point of the control it lands inside.
        resolution = self._native._exact_click_target(int(x), int(y))
        click_x, click_y = int(resolution["x"]), int(resolution["y"])
        label = _gesture_label(button, clicks, hold_ms, modifiers)
        self._native._publish_pointer("click", rel_x=click_x, rel_y=click_y, label=label)
        self._native._mouse_click(click_x, click_y, button=button, clicks=clicks, hold_ms=hold_ms, modifiers=modifiers or ())
        desktop_settle.settle_after_action(self._native._capture_image)
        # Clicking into a window is how a human targets it for typing.
        self._native._remember_target_window()
        after = self._native._capture_image()
        after_windows = self._native._window_inventory()
        after_title = next((item["title"] for item in after_windows if item.get("active")), "")
        focused = self._native._focused_state()
        return {
            "ok": True,
            "action": "click",
            "activation_attempted": True,
            "activation_dispatched": True,
            "activation_count": 1,
            "surface_changed": _image_changed(before, after),
            "title_changed": before_title != after_title,
            "active_typable": _typable_receipt(focused),
            "active_tag": focused.get("control_type"),
            "focus_changed": _focus_change(focus_before, self._native._focus_identity()),
            "caret": self._native._caret_state(),
            "click_snap_applied": bool(resolution.get("snap_applied")),
            "clicked_control_type": resolution.get("clicked_control_type") or "",
            "clicked_control_name": resolution.get("clicked_control_name") or "",
            **label_bound,
            **({"label_text": wanted_label[:120]} if label_bound else {}),
            "desktop_surface": True,
        }

    @_records_clipboard_copies
    async def vision_click_control(
        self,
        selector: str,
        fallback_x: int,
        fallback_y: int,
        *,
        button: str = "left",
        clicks: int = 1,
        hold_ms: int = 0,
        modifiers: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        expected = kwargs.get("expected_target_identity") if isinstance(kwargs.get("expected_target_identity"), dict) else {}
        # The gesture the model asked for (a double-click opens a file-manager item; a right-click
        # opens a context menu; a ctrl-click adds to a selection) applies to a bound control
        # exactly as to a point.
        button = str(button or "left")
        try:
            clicks = max(1, min(3, int(clicks or 1)))
        except (TypeError, ValueError):
            clicks = 1
        if kwargs.get("allow_final_submit"):
            # Final authority is one plain left click, exactly as on the browser surface.
            button, clicks, hold_ms, modifiers = "left", 1, 0, None
        gesture = {"button": button, "clicks": clicks, "hold_ms": hold_ms, "modifiers": list(modifiers or [])}
        guarded_authority = bool(kwargs.get("allow_final_submit") or kwargs.get("allow_guarded_intermediate"))
        del kwargs
        self._native._require_enabled()
        wrapper = self._native._resolve_control(selector)
        if wrapper is None:
            # Proxied UIA elements (Tk widgets, some Win32 menus) get a new runtime id on every
            # enumeration, so the observed element cannot be re-found by id. Re-find it where the
            # model pointed, in the current screen: the control under that point with the same
            # accessible name is the same control, clicked through the ordinary point path.
            expected_name = " ".join(str(expected.get("target_text") or "").split()).casefold()
            expected_kind = str(expected.get("target_role") or expected.get("target_tag") or "").strip().casefold()
            if fallback_x or fallback_y:
                under_point = self._native._exact_click_target(int(fallback_x), int(fallback_y))
                found_name = " ".join(str(under_point.get("clicked_control_name") or "").split()).casefold()
                found_kind = str(under_point.get("clicked_control_type") or "").strip().casefold()
                same_named = bool(expected_name) and found_name == expected_name
                # An unnamed control (a Tk menu entry) is the same one when the point still lands on
                # an unnamed control of the same UIA type.
                same_unnamed = not expected_name and not found_name and bool(expected_kind) and found_kind == expected_kind
                # With final or guarded authority, a same-named control is only the approved one when
                # it sits in the approved control's own window: after that window closed, another
                # window's "Submit" at the same point must not receive the approved click.
                same_window = True
                if guarded_authority:
                    parsed = self._native._parse_selector(selector)
                    same_window = bool(parsed) and int(under_point.get("top_level_handle") or 0) == int(parsed[0])
                if (same_named or same_unnamed) and same_window:
                    result = await self.vision_click(int(fallback_x), int(fallback_y), **gesture)
                    return {**result, "rebound_by_point": True}
            return {
                "ok": False,
                "action": "click",
                "failure_reason": "desktop_uia_target_stale",
                "activation_attempted": False,
                "desktop_surface": True,
            }
        before = self._native._capture_image()
        focus_before = self._native._focus_identity()
        # A click that opens a dialog or another window retitles the active window: report it like a point
        # click does, so a chained step after it is re-decided instead of following focus into the dialog.
        before_title = next((item["title"] for item in self._native._window_inventory() if item.get("active")), "")
        before_value = self._native._control_value(wrapper, password=bool(getattr(wrapper.element_info, "is_password", False)))
        # Exact pointer control: click the element's own UIA-declared clickable point
        # through the body's paced injector. One physical click path for every click -
        # overlay-accurate, caret-recorded, and never a rect center that misses an
        # irregular or partially occluded control.
        point = self._native._point_within_control(wrapper, fallback_x, fallback_y) or self._native._clickable_point(wrapper)
        label = _gesture_label(button, clicks, hold_ms, modifiers)
        press_control = getattr(self._native, "_press_control", None)
        pressed = bool(guarded_authority and not label and callable(press_control) and press_control(wrapper))
        ensure_clickable = None if pressed else getattr(self._native, "_ensure_control_clickable", None)
        covered = ensure_clickable(wrapper, point) if callable(ensure_clickable) else ""
        if covered:
            # The control's own window is not the one under the click point (another window covers it): the
            # click is not sent rather than landing on whatever is in front.
            return {
                "ok": False,
                "action": "click",
                "failure_reason": covered,
                "error": "the control is covered by another window; nothing was clicked",
                "activation_attempted": False,
                "desktop_surface": True,
            }
        if pressed:
            # An approved final (or guarded) click is one plain click on exactly the approved control: where the
            # platform can press the element itself (macOS AXPress), it does, so no window or overlay that came
            # up around the approval can take the click.
            self._native._publish_pointer("click", label=label)
        elif point is not None:
            left, top, _, _ = self._native._virtual_screen()
            self._native._publish_pointer("click", abs_x=point[0], abs_y=point[1], label=label)
            self._native._mouse_click(point[0] - left, point[1] - top, **gesture)
        else:
            self._native._publish_pointer("click", label=label)
            try:
                if label:
                    raise ValueError("gesture needs the paced injector")
                wrapper.click_input()
            except Exception:
                self._native._mouse_click(fallback_x, fallback_y, **gesture)
        desktop_settle.settle_after_action(self._native._capture_image)
        self._native._remember_target_window()
        after = self._native._capture_image()
        live = self._native._resolve_control(selector)
        after_value = (
            self._native._control_value(live, password=bool(getattr(live.element_info, "is_password", False)))
            if live is not None
            else ""
        )
        focused = self._native._focused_state()
        after_title = next((item["title"] for item in self._native._window_inventory() if item.get("active")), "")
        selected = False
        if live is not None:
            for accessor in (
                lambda: bool(live.is_selected()),
                lambda: bool(live.get_toggle_state()),
            ):
                try:
                    selected = accessor()
                    break
                except Exception:
                    continue
        return {
            "ok": True,
            "action": "click",
            "activation_attempted": True,
            "activation_dispatched": True,
            "activation_count": 1,
            "surface_changed": _image_changed(before, after),
            "title_changed": before_title != after_title,
            "active_typable": _typable_receipt(focused),
            "active_tag": focused.get("control_type"),
            "selection_committed": bool(selected or (before_value != after_value and after_value)),
            "selected_value": after_value if before_value != after_value else "",
            "focus_changed": _focus_change(focus_before, self._native._focus_identity()),
            "caret": self._native._caret_state(),
            "desktop_surface": True,
        }

    async def vision_type(
        self,
        text: str,
        *,
        at_caret: bool = False,
        restore_caret: bool = True,
        sensitive: bool = False,
        target_point: Any = None,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        """Type ``text`` where keyboard focus is. ``sensitive`` (the run classified the value as a secret)
        keeps it off the clipboard and out of any edit excerpt. ``target_point`` (capture pixels) is where
        the model said the text goes: when the focused control is known and is not there, nothing is
        typed."""
        del timeout
        self._native._require_enabled()
        focused_before = self._native._focused_state()
        foreground_handle, foreground_title = self._native._foreground_window()
        with _CONTROL_LOCK:
            targeted = int(self._native._LAST_TARGET_WINDOW)
        if not targeted or (foreground_handle and not self._native._same_app_window(foreground_handle, targeted)):
            # Typing goes wherever Windows focus happens to be. If this body was never
            # told to act on the foreground window's app, that window belongs to something
            # else entirely, so refuse rather than deliver the value there.
            return {
                "ok": False,
                "action": "type",
                "failure_reason": "desktop_untargeted_type_window",
                "failure_detail": (
                    f"The foreground window is '{foreground_title or 'unknown'}', which this "
                    "task has not targeted, so typing there was withheld. Use focus_window "
                    "on the exact window and click the text area first, then type."
                ),
                "focus_attempted": True,
                "active_typable": bool(focused_before.get("typable")),
                "active_window_title": foreground_title,
                "input_dispatched": False,
                "desktop_surface": True,
            }
        # Geometry-free typing signal: the event never carries the typed text.
        self._native._publish_pointer("type")
        # At the caret, the caret and selection the model set are the target: refocus only if
        # focus left the field (refocusing can move a caret), and never select everything.
        # Refocus the last clicked field only when focus is not on a text field (an overlay took
        # it): a field the model put the caret in by key (Alt+D, Tab) or click is where typing goes.
        self._native._LAST_FOCUS_REPAIR["result"] = ""
        caret_restored = (
            (self._native._restore_caret_at_last_click() or self._native._focus_text_control_within_focus())
            if restore_caret and not focused_before.get("typable")
            else False
        )
        if caret_restored:
            # Focus moved into the text control: read it again (a document is typed into at the caret).
            focused_before = self._native._focused_state() or focused_before
        point = list(target_point) if isinstance(target_point, (list, tuple)) and len(target_point) == 2 else None
        box = focused_before.get("box") if isinstance(focused_before.get("box"), list) else None
        if point and box and not (
            box[0] - _TYPE_POINT_SLACK <= int(point[0]) <= box[2] + _TYPE_POINT_SLACK
            and box[1] - _TYPE_POINT_SLACK <= int(point[1]) <= box[3] + _TYPE_POINT_SLACK
        ):
            # Keystrokes go to the focused control, and it is not where the model said the text goes
            # (v5_memory run 1709: a Tk radio click left focus in the depot-code entry, and a type aimed
            # at the Route dropdown wrote "North" into the depot code). Nothing is typed.
            return {
                "ok": False,
                "action": "type",
                "failure_reason": "type_focus_elsewhere",
                "focused_box": box,
                "focus_attempted": True,
                "input_dispatched": False,
                "active_typable": bool(focused_before.get("typable")),
                "desktop_surface": True,
            }
        if point and not focused_before.get("typable"):
            # The focused element is no text control: a toolkit like Tk keeps focus on its window and
            # routes keys to whichever field it last focused, so where they go cannot be read. Only a
            # click on that very field just before puts them there (v5_memory run 1713 typed "Harbor",
            # meant for the Route dropdown, into the Override code field the window still focused).
            with _CONTROL_LOCK:
                last_click = self._native._LAST_CLICK_POINT
            aimed = self._native._absolute_point(int(point[0]), int(point[1]))
            if not last_click or max(abs(last_click[0] - aimed[0]), abs(last_click[1] - aimed[1])) > _TYPE_CLICK_REACH:
                return {
                    "ok": False,
                    "action": "type",
                    "failure_reason": "type_focus_unverified",
                    "focus_attempted": True,
                    "input_dispatched": False,
                    "active_typable": False,
                    "desktop_surface": True,
                }
        before_values = self._native._readable_active_values()
        before_image = self._native._capture_image()
        # A field's whole value is set by selecting it all first. A document (a text editor's page)
        # is typed into the way a person does: at the caret, replacing only the selection, so the
        # rest of the document stays; the model selects everything itself to replace it all.
        at_caret = bool(at_caret or focused_before.get("document"))
        # A secret (the run marked the type sensitive, or focus is on a password box) goes in key by key: it
        # never passes through the clipboard and is never read into an edit excerpt (the run redacts the rest).
        secret = bool(sensitive or focused_before.get("password"))
        # Where the caret is and what is selected, read before the text goes in: a type at the caret is
        # verified against exactly this paragraph.
        paragraph_before = self._native._caret_paragraph() if at_caret and not secret else {}
        if not at_caret:
            self._native._send_key(self._native._SELECT_ALL_KEY)
        if not at_caret or caret_restored:
            # Let the select-all (or the focus repair) settle before the first character: typed at once,
            # it races the Ctrl release and arrives as Ctrl+<char> (run 770: "eclaration-CD7-0158.txt").
            time.sleep(0.12)
        # A paste leaves the user's clipboard text held here until the read-back below settles it.
        clipboard = self._native._ClipboardHold()
        landed = False
        try:
            if secret:
                text_delivery = self._native._deliver_text(str(text), allow_paste=False)
            elif focused_before.get("document"):
                # Into a document the text is pasted at the caret, replacing the selection like typing does:
                # paced keystrokes into a rich editor have arrived garbled ("Seal bbber:LLL-826275").
                text_delivery = self._native._deliver_text(str(text), prefer_paste=True, clipboard=clipboard)
            else:
                text_delivery = self._native._deliver_text(str(text), clipboard=clipboard)
            desktop_settle.settle_after_action(self._native._capture_image)
            after_image = self._native._capture_image()
            screen_changed = _image_changed(before_image, after_image)
            focused_after = self._native._focused_state()
            expected = str(text)
            # Read the value back from the active window's own controls. The focused-element
            # value is unreliable (rich editors report nothing), and a screen diff alone is
            # not proof: a blinking caret repaints the screen while the keystrokes went
            # nowhere. Searching the live control values is what actually settles it.
            active_value = str(focused_after.get("value") or "")
            readable_values = self._native._readable_active_values()
            # Only text that appeared because of this keystroke sequence counts: matching any
            # current value would also match text that was already on screen.
            appeared = [item for item in readable_values if item not in before_values]
            # Prefer a control whose whole value IS the typed text: that is the field itself.
            # Containment alone also matches incidental mirrors such as the window title
            # ("*The input harness is complete. - Notepad"), which is proof the text arrived
            # but is not the field's value the runtime compares against.
            exact = next((item for item in appeared if item.strip() == expected.strip()), "")
            observed = exact or next((item for item in appeared if expected in item), "")
            if exact:
                active_value = exact
            elif observed and not active_value:
                active_value = observed
            # At the caret with a known selection: the caret's paragraph afterwards must be the one before with
            # the selection replaced by the text. Containment in the document would also accept leftovers of
            # what was selected ("Seal number: _SL-826275") or text that landed somewhere else.
            # Only within the field the caret was in: after focus moved, another field's paragraph proves nothing.
            field_before = focused_before.get("runtime_id")
            same_field = bool(field_before) and field_before == focused_after.get("runtime_id")
            edit = _paragraph_edit(
                paragraph_before, self._native._caret_paragraph() if paragraph_before and same_field else {}, expected
            )
            landed = bool(observed or (edit and (edit["matches"] or expected in edit["after"])))
        finally:
            # The user's clipboard goes back only once the pasted text is seen to have landed. Otherwise (not
            # seen, no read-back, an error) it is left empty: an app still handling the paste gets nothing.
            clipboard.settle(landed=landed)
        # A paste that did not land as intended may have been joined by something else the app pasted (whatever
        # the clipboard held around it, possibly the user's own value): the changed line is not forwarded then.
        pasted = text_delivery != "keystrokes"
        edit_fields: dict[str, Any] = {}
        if edit:
            line_before, line_after = _edit_excerpts(edit["before"], edit["after"])
            edit_fields = {"edited_line_before": line_before, "edited_line_after": line_after}
        diagnostics = {
            "focus_attempted": True,
            "text_delivery": text_delivery,
            "caret_restored": caret_restored,
            "typed_at_caret": at_caret,
            "focused_before": _focus_summary(focused_before),
            "focus_repair": self._native._LAST_FOCUS_REPAIR.get("result") or "",
            "focused_after": _focus_summary(focused_after),
            "input_dispatched": True,
            "desktop_surface": True,
        }
        typable = bool(focused_after.get("typable") or focused_before.get("typable"))
        corrupted_detail = (
            (
                "The pasted content did not land as intended: the text changed but does not show the typed text "
                "exactly. Read what it now shows in the screenshot and correct it."
            )
            if pasted
            else (
                "The text changed but does not show the typed text exactly (garbled, reformatted, or filtered); "
                "the edited line before and after follows. Correct it from what it now shows."
            )
        )
        if edit and not edit["matches"] and expected not in edit["after"] and edit["after"] != edit["before"]:
            # Something landed at the caret, but not the text: report what did, never "no effect".
            return {
                "ok": False,
                "action": "type",
                "failure_reason": "typed_text_corrupted",
                "failure_detail": corrupted_detail,
                "surface_changed": screen_changed,
                "active_typable": typable,
                "active_value_matches_expected": False,
                "active_window_title": foreground_title,
                **({} if pasted else edit_fields),
                **diagnostics,
            }
        if edit and (edit["matches"] or expected in edit["after"]):
            # Read back from the caret's own paragraph (a long document's head may not show the edit).
            return {
                "ok": True,
                "action": "type",
                "surface_changed": screen_changed,
                "active_typable": typable,
                "active_has_value": True,
                "active_value": active_value or edit["after"],
                "active_value_length": len(active_value or edit["after"]),
                "active_value_matches_expected": bool(edit["matches"]),
                "value_read_back": True,
                # Where the field that took the text is: a following Enter acts there.
                "focus_point": focused_after.get("center"),
                **edit_fields,
                **diagnostics,
            }
        if not observed and appeared and not secret:
            # A text control's value changed because of the keystrokes, but it does not hold the text.
            vanished = [item for item in before_values if item not in readable_values]
            changed_before, changed_after = _edit_excerpts(vanished[0] if vanished else "", appeared[0])
            changed_fields = (
                {} if pasted else {"edited_line_before": changed_before, "edited_line_after": changed_after}
            )
            return {
                "ok": False,
                "action": "type",
                "failure_reason": "typed_text_corrupted",
                "failure_detail": corrupted_detail,
                "surface_changed": screen_changed,
                "active_typable": typable,
                "active_value_matches_expected": False,
                "active_window_title": foreground_title,
                **changed_fields,
                **diagnostics,
            }
        typed_at_caret = bool(
            not observed
            and screen_changed
            and self._native._LAST_CLICK_POINT
            and _changed_near_point(before_image, after_image, self._native._LAST_CLICK_POINT)
        )
        if typed_at_caret and _typed_text_read_near_point(after_image, self._native._LAST_CLICK_POINT, text):
            # The field exposes no value, but its own pixels now show exactly the typed text where the
            # caret was put: that is the read-back.
            return {
                "ok": True,
                "action": "type",
                "focus_attempted": True,
                "text_delivery": text_delivery,
                "surface_changed": True,
                "active_typable": typable,
                "active_has_value": True,
                "active_value": "" if sensitive else text,
                **({"active_value_redacted": True} if sensitive else {}),
                "active_value_length": len(text),
                "active_value_matches_expected": True,
                "value_read_back": True,
                "value_read_by": "screen_text",
                "typed_where_caret_is": True,
                "focused_before": _focus_summary(focused_before),
                "focus_repair": self._native._LAST_FOCUS_REPAIR.get("result") or "",
                "focused_after": _focus_summary(focused_after),
                "focus_point": focused_after.get("center") or focused_before.get("center"),
                "caret_restored": caret_restored,
                "input_dispatched": True,
                "desktop_surface": True,
            }
        if typed_at_caret:
            # A field that exposes no readable value (a Tk entry, a painted control) changed right
            # where the caret was put: the text arrived there, unread. The next screenshot is the
            # read-back.
            return {
                "ok": True,
                "action": "type",
                "focus_attempted": True,
                "text_delivery": text_delivery,
                "surface_changed": True,
                "active_typable": typable,
                "active_has_value": False,
                "active_value": "",
                "active_value_length": 0,
                "active_value_matches_expected": False,
                "value_read_back": False,
                "typed_where_caret_is": True,
                "focused_before": _focus_summary(focused_before),
                "focus_repair": self._native._LAST_FOCUS_REPAIR.get("result") or "",
                "focused_after": _focus_summary(focused_after),
                "focus_point": focused_after.get("center") or focused_before.get("center"),
                "caret_restored": caret_restored,
                "input_dispatched": True,
                "desktop_surface": True,
            }
        target_box = self._native._window_capture_box(targeted) if targeted else None
        window_changed = (
            _changed_within_box(before_image, after_image, target_box) if target_box else screen_changed
        )
        if not observed and (readable_values or not window_changed):
            # No text control's value changed (a changed one was reported above), or nothing changed in
            # the targeted window: in both cases the keystrokes did not reach a field. With no readable
            # text control and a change in the window, the text arrived unread.
            return {
                "ok": False,
                "action": "type",
                "failure_reason": "desktop_type_had_no_effect",
                "failure_detail": (
                    f"The keystrokes reached '{foreground_title or 'the active window'}' but the "
                    "value is not visible in it, so no text area had the caret. Click the middle "
                    "of the exact text area first, then type."
                ),
                "focus_attempted": True,
                "active_typable": bool(focused_after.get("typable")),
                "active_window_title": foreground_title,
                "caret_restored": caret_restored,
                "caret_target_window": targeted,
                "caret_click_point": list(self._native._LAST_CLICK_POINT or ()),
                "focused_before": _focus_summary(focused_before),
                "focus_repair": self._native._LAST_FOCUS_REPAIR.get("result") or "",
                "focused_after": _focus_summary(focused_after),
                "typed_at_caret": at_caret,
                "input_dispatched": True,
                "text_delivery": text_delivery,
                "surface_changed": screen_changed,
                "desktop_surface": True,
            }
        return {
            "ok": True,
            "action": "type",
            "focus_attempted": True,
            "text_delivery": text_delivery,
            "surface_changed": screen_changed,
            "active_typable": typable,
            "active_has_value": bool(active_value),
            "active_value": active_value,
            "active_value_length": len(active_value),
            "active_value_matches_expected": bool(observed) or active_value == expected,
            "value_read_back": bool(observed),
            "caret_restored": caret_restored,
            "typed_at_caret": at_caret,
            "focused_before": _focus_summary(focused_before),
            "focus_repair": self._native._LAST_FOCUS_REPAIR.get("result") or "",
            "focused_after": _focus_summary(focused_after),
            # Where the field that took the text is: a following Enter acts there.
            "focus_point": focused_after.get("center"),
            "input_dispatched": True,
            "desktop_surface": True,
        }

    async def vision_type_control(
        self,
        selector: str,
        text: str,
        *,
        at_caret: bool = False,
        sensitive: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del kwargs
        self._native._require_enabled()
        # A control marked sensitive for screenshot masking holds a secret: it is typed like one.
        sensitive = bool(sensitive or str(selector or "").strip() in self._sensitive_selectors)
        wrapper = self._native._resolve_control(selector)
        if wrapper is None:
            return {
                "ok": False,
                "action": "type",
                "failure_reason": "desktop_uia_target_stale",
                "focus_attempted": False,
                "desktop_surface": True,
            }
        try:
            rectangle = wrapper.element_info.rectangle
            self._native._publish_pointer(
                "type",
                abs_x=int((rectangle.left + rectangle.right) / 2),
                abs_y=int((rectangle.top + rectangle.bottom) / 2),
            )
        except Exception:
            pass
        try:
            wrapper.set_focus()
        except Exception:
            rectangle = wrapper.element_info.rectangle
            center_x = int((rectangle.left + rectangle.right) / 2)
            center_y = int((rectangle.top + rectangle.bottom) / 2)
            if not self._native._point_hits_element(center_x, center_y, wrapper):
                # Something else is drawn at the control's centre now (a dialog a click opened): a click
                # there would land on it, unjudged. Nothing is clicked or typed.
                return {
                    "ok": False,
                    "action": "type",
                    "failure_reason": "desktop_type_target_not_focused",
                    "failure_detail": (
                        "The chosen control would not take focus and is not what is on screen at its position, "
                        "so nothing was clicked or typed. Act on what the fresh screenshot shows."
                    ),
                    "focus_attempted": True,
                    "input_dispatched": False,
                    "desktop_surface": True,
                }
            left, top, _, _ = self._native._virtual_screen()
            self._native._mouse_click(center_x - left, center_y - top)
        time.sleep(0.12)
        # The model named a control in this window: typing there is aimed at it.
        parsed = self._native._parse_selector(selector)
        if parsed:
            self._native._remember_target_window(parsed[0])
        if self._native._focus_within(wrapper) is False:
            # Some controls do not take focus programmatically (a file dialog's breadcrumb bar only
            # turns into its edit box on a click): typing now would land in whatever has focus.
            return {
                "ok": False,
                "action": "type",
                "failure_reason": "desktop_type_target_not_focused",
                "failure_detail": (
                    "Keyboard focus did not move into the chosen control, so nothing was typed. Put the "
                    "caret in it first (click its text area, or its shortcut), then type."
                ),
                "focus_attempted": True,
                "input_dispatched": False,
                "desktop_surface": True,
            }
        # A container bound as the target (an editor's pane around its page) takes focus but drops
        # typed characters: put focus on the one text control inside it.
        self._native._LAST_FOCUS_REPAIR["result"] = ""
        if not self._native._focused_state().get("typable"):
            self._native._focus_text_control_within_focus()
        # Focus is on the target now: typing goes here, not to the field of an earlier click.
        repair = self._native._LAST_FOCUS_REPAIR.get("result") or ""
        result = await self.vision_type(text, at_caret=at_caret, restore_caret=False, sensitive=sensitive)
        if repair:
            result = {**result, "focus_repair": repair}
        if result.get("ok") and self._native._focus_within(wrapper) is False:
            return {
                **result,
                "ok": False,
                "failure_reason": "desktop_type_focus_left_target",
                "active_value_matches_expected": False,
            }
        return result

    async def vision_scroll(
        self,
        dy: int,
        *,
        dx: int = 0,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        del timeout
        self._native._require_enabled()
        # No point named: wheel over the middle of the window this task targets, not wherever the
        # pointer was left (a wheel goes to the window under the pointer).
        with _CONTROL_LOCK:
            targeted = int(self._native._LAST_TARGET_WINDOW)
        if not targeted:
            targeted, _ = self._native._foreground_window()
        box = self._native._window_capture_box(targeted) if targeted else None
        if box:
            point = (int((box[0] + box[2]) / 2), int((box[1] + box[3]) / 2))
        else:
            _, _, width, height = self._native._virtual_screen()
            point = (int(width / 2), int(height / 2))
        return self._wheel_at(point[0], point[1], int(dx or 0), int(dy or 0))

    async def vision_wheel(
        self,
        x: int,
        y: int,
        dx: int,
        dy: int,
        *,
        modifiers: list[str] | None = None,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        """Wheel at a screen point: whatever is under it scrolls (a list, a document, a page).
        Modifiers are held during the wheel (ctrl+wheel zooms a map, image, or document)."""
        del timeout
        self._native._require_enabled()
        return self._wheel_at(int(x), int(y), int(dx or 0), int(dy or 0), modifiers=modifiers)

    def _wheel_at(self, x: int, y: int, dx: int, dy: int, *, modifiers: Any = ()) -> dict[str, Any]:
        abs_x, abs_y = self._native._absolute_point(x, y)
        self._native._set_cursor(abs_x, abs_y)
        self._native._publish_pointer("scroll", abs_x=int(abs_x), abs_y=int(abs_y), dy=int(dy))
        # Let a hover highlight under the new pointer settle before the before-frame.
        time.sleep(0.12)
        before = self._native._capture_image()
        with self._native._held_modifiers(modifiers):
            self._native._wheel(int(dx or 0), int(dy or 0))
        desktop_settle.settle_after_action(self._native._capture_image)
        after = self._native._capture_image()
        # Proof is a change around the wheel point (what scrolled is under it), not anywhere on
        # screen: a caret blink or a clock elsewhere is not a scroll.
        scrolled = _changed_near_point(before, after, (x, y), radius_x=360, radius_y=240)
        return {
            "ok": True,
            "action": "scroll",
            "wheel_point": [int(x), int(y)],
            "before_scroll_y": 0,
            "scroll_y": 1 if scrolled else 0,
            "scroll_changed": scrolled,
            "surface_changed": _image_changed(before, after),
            "desktop_surface": True,
        }

    @_records_clipboard_copies
    async def vision_key(
        self,
        key: str,
        *,
        repeat: int = 1,
        hold_ms: int = 0,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        del timeout
        self._native._require_enabled()
        try:
            self._native._check_key(key)
        except ValueError as exc:
            # A key no keyboard has, or a blocked shortcut: nothing is pressed, and the brain hears
            # why (v5_reconcile run 1703 ended on "Unsupported keyboard key: PAGE UP").
            return {
                "ok": False,
                "action": "key",
                "failure_reason": "unsupported_key",
                "error": _safe_text(str(exc), 160),
                "input_dispatched": False,
                "desktop_surface": True,
            }
        before = self._native._capture_image()
        before_focus = self._native._focused_state()
        before_windows = self._native._window_inventory()
        presses = max(1, min(200, int(repeat or 1)))
        hold = max(0, min(5000, int(hold_ms or 0)))
        # Key events show the pressed combo (for example ENTER or CTRL+A), never typed text.
        self._native._publish_pointer(
            "key",
            label=_safe_text(key, 24) + (f" x{presses}" if presses > 1 else "") + (" hold" if hold else ""),
        )
        await self._native._send_key_async(key, repeat=presses, hold_ms=hold)
        desktop_settle.settle_after_action(self._native._capture_image)
        after = self._native._capture_image()
        after_focus = self._native._focused_state()
        after_windows = self._native._window_inventory()
        return {
            "ok": True,
            "action": "key",
            "surface_changed": _image_changed(before, after),
            "focus_changed": before_focus.get("runtime_id") != after_focus.get("runtime_id"),
            # Which element holds focus now (its UIA runtime id, never its value): a type that follows
            # can be bound to the field this key opened (F2's rename box, Alt+D's address bar).
            "focused_rid": ".".join(str(item) for item in (after_focus.get("runtime_id") or ())),
            "caret": self._native._caret_state(),
            "title_changed": next((item["title"] for item in before_windows if item.get("active")), "")
            != next((item["title"] for item in after_windows if item.get("active")), ""),
            # Which window actually received the keystrokes, so the model can tell that an
            # ambient shortcut went somewhere it did not intend.
            "active_window_title": next(
                (item["title"] for item in after_windows if item.get("active")), ""
            ),
            "active_typable": _typable_receipt(after_focus),
            "presses": presses,
            "hold_ms": hold,
            "desktop_surface": True,
        }

    async def vision_drag(
        self,
        x: int,
        y: int,
        to_x: int,
        to_y: int,
        *,
        button: str = "left",
        path: list[tuple[int, int]] | None = None,
        hold_ms: int = 0,
        modifiers: list[str] | None = None,
        timeout: float = 25.0,
    ) -> dict[str, Any]:
        """Press, move (through any waypoints), and release: text selection, sliders, dials,
        resizing, drag-and-drop. hold_ms waits pressed before moving; modifiers are held throughout."""
        del timeout
        self._native._require_enabled()
        before = self._native._capture_image()
        held = "+".join(str(item) for item in (modifiers or ()))
        self._native._publish_pointer("click", rel_x=int(x), rel_y=int(y), label=f"{held} drag from".strip())
        self._native._mouse_drag(
            int(x),
            int(y),
            int(to_x),
            int(to_y),
            button=button,
            path=list(path or ()),
            hold_ms=hold_ms,
            modifiers=modifiers or (),
        )
        self._native._publish_pointer("click", rel_x=int(to_x), rel_y=int(to_y), label="drop")
        desktop_settle.settle_after_action(self._native._capture_image)
        after = self._native._capture_image()
        focused = self._native._focused_state()
        return {
            "ok": True,
            "action": "drag",
            "activation_attempted": True,
            "activation_dispatched": True,
            "surface_changed": _image_changed(before, after),
            "active_typable": _typable_receipt(focused),
            "active_tag": focused.get("control_type"),
            "desktop_surface": True,
        }

    async def vision_select_text(self, text: str, x: int, y: int, *, timeout: float = 25.0) -> dict[str, Any]:
        """Select exactly `text` on the real screen near the model's point. A control exposing its
        text through UI Automation selects it there, read back exactly. Otherwise (windows drawn by
        toolkits such as Tk) read the words around the point with OCR, take the occurrence nearest
        it, and click just before its first character and shift-click just after its last; that
        selection cannot be read back from pixels, so the result says whether the screen changed
        where the text is (a highlight) and anywhere else."""
        del timeout
        self._native._require_enabled()

        # A control that exposes its text (Notepad, rich editors, most edit controls) selects it
        # exactly without the pointer and reads the selection back; only drawn text is dragged across.
        by_pattern = self._native._select_text_by_uia(text, int(x), int(y))
        if by_pattern is not None:
            return by_pattern
        before = self._native._capture_image()
        width, height = before.size
        located = _ocr_locate(before, text, int(x), int(y), reach=(1000, 400))
        if not located.get("found"):
            return {
                "ok": False,
                "action": "select_text",
                "mode": "screen_text",
                "failure_reason": "select_text_not_found",
                "occurrences": 0,
                "desktop_surface": True,
            }
        (from_x, from_y), (to_x, to_y) = located["from"], located["to"]
        start_type = self._native._control_type_at(int(from_x), int(from_y))
        if start_type in _NOT_TEXT_CONTROL_TYPES:
            # A press there acts on the item (opens or drags a file, a list row, a window): never a
            # text selection.
            return {
                "ok": False,
                "action": "select_text",
                "mode": "screen_text",
                "failure_reason": "select_text_not_text",
                "control_type": start_type,
                "desktop_surface": True,
            }
        # A click before the first character, then a shift-click after the last: never a drag, so a
        # press inside an earlier selection cannot drag that text away. The pause outlasts the system
        # double-click time (a quick second click would select by words).
        self._native._publish_pointer("click", rel_x=int(from_x), rel_y=int(from_y), label="select from")
        self._native._mouse_click(int(from_x), int(from_y))
        time.sleep(min(1.2, self._native._double_click_seconds() + 0.08))
        self._native._publish_pointer("click", rel_x=int(to_x), rel_y=int(to_y), label="shift select to")
        self._native._mouse_click(int(to_x), int(to_y), modifiers=["shift"])
        desktop_settle.settle_after_action(self._native._capture_image)
        after = self._native._capture_image()
        left, top, right, bottom = located["box"]
        pad = 3
        text_box = (max(0, left - pad), max(0, top - pad), min(width, right + pad), min(height, bottom + pad))
        return {
            "ok": True,
            "action": "select_text",
            "mode": "screen_text",
            "selected_text": " ".join(str(text).split())[:300],
            "occurrences": int(located.get("occurrences") or 1),
            **({"read_approximately": True} if located.get("approximate") else {}),
            "highlight_changed": _image_changed(before.crop(text_box), after.crop(text_box)),
            "changed_outside_text": _changed_outside(before, after, text_box),
            "surface_changed": _image_changed(before, after),
            "activation_attempted": True,
            "activation_dispatched": True,
            "desktop_surface": True,
        }

    async def vision_hover(self, x: int, y: int, *, dwell_ms: int = 0, timeout: float = 20.0) -> dict[str, Any]:
        """Move the pointer without clicking, to reveal tooltips, menus, and hover states. dwell_ms
        rests it there longer, for a tooltip or menu that opens only after a delay."""
        del timeout
        self._native._require_enabled()
        before = self._native._capture_image()
        self._native._publish_pointer("hover", rel_x=int(x), rel_y=int(y), label="hover")
        self._native._mouse_move(int(x), int(y))
        # An awaited dwell: a blocking sleep here froze the whole server (the extension hub, cancel
        # checks, heartbeats) for up to five seconds.
        await asyncio.sleep(max(0.45, max(0, min(5000, int(dwell_ms or 0))) / 1000))
        after = self._native._capture_image()
        return {
            "ok": True,
            "action": "hover",
            "surface_changed": _image_changed(before, after),
            "desktop_surface": True,
        }

    async def vision_upload(self, x: int, y: int, document: dict[str, Any], *, timeout: float = 35.0) -> dict[str, Any]:
        del timeout
        self._native._require_enabled()
        path = Path(str(document.get("path") or document.get("file_path") or "")).expanduser().resolve()
        if not path.is_file():
            return {"ok": False, "action": "upload", "failure_reason": "desktop_upload_file_missing"}
        self._native._publish_pointer("click", rel_x=int(x), rel_y=int(y), label="upload")
        self._native._mouse_click(int(x), int(y))
        time.sleep(0.5)
        self._native._send_unicode(str(path))
        self._native._send_key("ENTER")
        time.sleep(0.45)
        return {
            "ok": True,
            "action": "upload",
            "observed_present": False,
            "filename": path.name,
            "desktop_surface": True,
        }

    async def vision_upload_control(
        self,
        selector: str,
        document: dict[str, Any],
        fallback_x: int,
        fallback_y: int,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del kwargs
        wrapper = self._native._resolve_control(selector)
        if wrapper is not None:
            rectangle = wrapper.element_info.rectangle
            left, top, _, _ = self._native._virtual_screen()
            fallback_x = int((rectangle.left + rectangle.right) / 2 - left)
            fallback_y = int((rectangle.top + rectangle.bottom) / 2 - top)
        return await self.vision_upload(fallback_x, fallback_y, document)

    def downloads_snapshot(self) -> dict[str, Any]:
        from app import run_downloads

        return run_downloads.local_snapshot()

    def downloads_listing(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        from app import run_downloads

        return run_downloads.local_listing(snapshot)

    async def vision_open_file(self, path: str, *, timeout: float = 20.0) -> dict[str, Any]:
        """Open a downloaded document in its default app (the loop has already checked it is a listed,
        completed download of this run). Re-checked here: only an allowed document type sitting
        directly in the downloads folder is ever opened; nothing that can run is started."""
        del timeout
        self._native._require_enabled()
        from app import run_downloads

        target = run_downloads.local_openable_path(path)
        if target is None:
            return {"ok": False, "action": "open_file", "failure_reason": "open_file_type_not_allowed", "desktop_surface": True}
        before = {item["window_id"] for item in self._native._window_inventory()}
        self._native._open_path(target)
        deadline = time.monotonic() + 6
        after: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            await asyncio.sleep(0.3)
            after = self._native._window_inventory()
            if any(item["window_id"] not in before for item in after):
                break
        new_window = next((item for item in after if item["window_id"] not in before), None)
        if new_window:
            self._native._remember_target_window()
        return {
            "ok": True,
            "action": "open_file",
            "opened_file": target.name,
            "surface_changed": bool(new_window),
            "title_changed": bool(new_window),
            "new_window_id": (new_window or {}).get("window_id"),
            "new_window_title": (new_window or {}).get("title"),
            "desktop_surface": True,
        }

    async def vision_focus_window(self, window_id: str, *, timeout: float = 20.0) -> dict[str, Any]:
        del timeout
        self._native._require_enabled()

        monitor_match = re.fullmatch(r"monitor:(\d+)", str(window_id or "").strip().lower())
        if monitor_match:
            # Look at a whole monitor: later screenshots show it, and coordinates are its pixels, until
            # focus_window or launch_app moves the view to a window.
            wanted = int(monitor_match.group(1))
            if not any(int(item["monitor"]) == wanted for item in self._native._monitors()):
                return {"ok": False, "action": "focus_window", "failure_reason": "invalid_monitor"}
            changed = int(self._native._VIEW.get("pinned") or 0) != wanted
            self._native._VIEW["pinned"] = wanted
            return {
                "ok": True,
                "action": "focus_window",
                "monitor": wanted,
                "focus_changed": True,
                "view_changed": changed,
                "desktop_surface": True,
            }
        handle = _parse_window_id(window_id)
        if handle is None:
            return {"ok": False, "action": "focus_window", "failure_reason": "invalid_window_id"}
        self._native._VIEW["pinned"] = None
        if not any(item.get("window_id") == window_id for item in self._native._window_inventory()):
            return {"ok": False, "action": "focus_window", "failure_reason": "desktop_window_stale"}
        self._native._set_foreground_window(handle)
        desktop_settle.settle_after_action(self._native._capture_image)
        active = self._native._foreground_handle()
        if active == handle:
            self._native._remember_target_window(handle)
        return {
            "ok": active == handle,
            "action": "focus_window",
            "focus_changed": active == handle,
            "title_changed": active == handle,
            "desktop_surface": True,
        }

    async def vision_launch_app(self, app_id: str, *, timeout: float = 30.0) -> dict[str, Any]:
        del timeout
        self._native._require_enabled()
        _, index = self._native._application_inventory()
        target = index.get(str(app_id or ""))
        if target is None:
            return {"ok": False, "action": "launch_app", "failure_reason": "desktop_app_target_stale"}
        before = {item["window_id"] for item in self._native._window_inventory()}
        self._native._VIEW["pinned"] = None
        if not self._native._launch_inventory_target(target):
            return {"ok": False, "action": "launch_app", "failure_reason": "desktop_app_target_stale"}
        deadline = time.monotonic() + 5
        after: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            time.sleep(0.25)
            after = self._native._window_inventory()
            if any(item["window_id"] not in before for item in after):
                break
        new_window = next((item for item in after if item["window_id"] not in before), None)
        if new_window:
            self._native._remember_target_window()
        return {
            "ok": True,
            "action": "launch_app",
            "surface_changed": bool(new_window),
            "title_changed": bool(new_window),
            "launched_app_id": app_id,
            "new_window_id": (new_window or {}).get("window_id"),
            "new_window_title": (new_window or {}).get("title"),
            "desktop_surface": True,
        }

    async def vision_read_clipboard(self, *, timeout: float = 10.0) -> dict[str, Any]:
        """The clipboard's text, only when one of this run's own input actions put it there.

        What the clipboard held before the run (or got from anywhere but a run action: the user, another
        app, the body's own paste and restore) is the user's and is refused as clipboard_not_from_this_run.
        A copy made while a password or run-marked sensitive control was in the front window or focused, or
        one its source app marked private (kept out of clipboard history, as password managers do), is
        refused as clipboard_sensitive_source. The text is cut to CLIPBOARD_READ_LIMIT_CHARS.
        """
        del timeout
        self._native._require_enabled()
        sequence = self._native._clipboard_sequence()
        if sequence is None:
            return {"ok": False, "action": "read_clipboard", "failure_reason": "clipboard_unavailable", "desktop_surface": True}
        copy = self._clipboard_copies.get(sequence)
        if sequence == self._clipboard_start_sequence or copy is None:
            return {
                "ok": False,
                "action": "read_clipboard",
                "failure_reason": "clipboard_not_from_this_run",
                "desktop_surface": True,
            }
        if copy.get("sensitive"):
            return {"ok": False, "action": "read_clipboard", "failure_reason": "clipboard_sensitive_source", "desktop_surface": True}
        try:
            read = self._native._read_clipboard_text()
        except Exception:  # noqa: BLE001
            read = {"failure_reason": "clipboard_unavailable"}
        if read.get("private"):
            return {"ok": False, "action": "read_clipboard", "failure_reason": "clipboard_sensitive_source", "desktop_surface": True}
        if read.get("sequence") != sequence:
            # Something replaced the run's copy between the check and the read.
            return {"ok": False, "action": "read_clipboard", "failure_reason": "clipboard_not_from_this_run", "desktop_surface": True}
        if read.get("failure_reason"):
            return {"ok": False, "action": "read_clipboard", "failure_reason": read["failure_reason"], "desktop_surface": True}
        text = str(read.get("text") or "")
        return {
            "ok": True,
            "action": "read_clipboard",
            "text": text[:CLIPBOARD_READ_LIMIT_CHARS],
            "chars": len(text),
            "truncated": len(text) > CLIPBOARD_READ_LIMIT_CHARS,
            "copied_by_run_action": True,
            "desktop_surface": True,
        }

    async def vision_wait(self, wait_ms: int, *, timeout: float = 10.0, text: str = "") -> dict[str, Any]:
        del timeout
        self._native._require_enabled()
        duration = max(100, min(30000, int(wait_ms or 750)))
        wanted = " ".join(str(text or "").split()).casefold()
        if wanted:
            return await self._wait_for_window_text(wanted, duration)
        before = self._native._capture_image()
        # Awaited, not time.sleep: a desktop wait of up to 30 s must not freeze the server's event loop.
        await asyncio.sleep(duration / 1000)
        after = self._native._capture_image()
        return {
            "ok": True,
            "action": "wait",
            "wait_ms": duration,
            "surface_changed": _image_changed(before, after),
            "desktop_surface": True,
        }

    async def vision_run_command(self, command: str, *, timeout_seconds: int | None = None, run_id: Any = None) -> dict[str, Any]:
        """Run one shell command in the WSL sandbox (app.command_sandbox): no network, none of the user's
        files, only a per-run scratch folder. Nothing runs on Windows itself."""
        from app import command_sandbox

        self._native._require_enabled()
        result = await asyncio.to_thread(command_sandbox.run, command, run_id=run_id, timeout_seconds=timeout_seconds)
        return {"action": "run_command", **result}

    async def _wait_for_window_text(self, wanted: str, duration: int) -> dict[str, Any]:
        """Wait until the front window shows `wanted` (any letter case) more times than it did when the
        wait began, read from its own pixels: the browser's wait-for-text, for windows that expose no
        text. Copies already shown at the start never satisfy it (present_at_start)."""
        from app import screen_ocr

        def count_now() -> int | None:
            try:
                import io

                handle = self._native._foreground_handle()
                box = self._native._window_capture_box(handle) if handle else None
                image = self._native._capture_image()
                if box is None or image is None:
                    return None
                left, top, right, bottom = box
                buffer = io.BytesIO()
                image.crop((left, top, right, bottom)).save(buffer, format="PNG")
                lines = (screen_ocr.read_lines([buffer.getvalue()]) or [[]])[0]
                return " ".join(" ".join(str(line).split()) for line in lines).casefold().count(wanted)
            except Exception:  # noqa: BLE001 - no reading this round
                return None

        started = time.monotonic()
        lowest = await asyncio.to_thread(count_now)
        present_at_start = lowest or 0
        while (time.monotonic() - started) * 1000 < duration:
            await asyncio.sleep(0.4)
            count = await asyncio.to_thread(count_now)
            if count is None:
                continue
            if lowest is not None and count > lowest:
                return {
                    "ok": True,
                    "action": "wait",
                    "waited_ms": int((time.monotonic() - started) * 1000),
                    "text_matched": True,
                    "present_at_start": present_at_start,
                    "desktop_surface": True,
                }
            lowest = count if lowest is None else min(lowest, count)
        return {
            "ok": False,
            "action": "wait",
            "failure_reason": "wait_text_not_seen",
            "waited_ms": int((time.monotonic() - started) * 1000),
            "text_matched": False,
            "present_at_start": present_at_start,
            "desktop_surface": True,
        }


# The module holding the platform primitives the shared methods above call (self._native.<name>): this
# one on Windows, app.desktop_body_macos on macOS (a subclass). Tests patch primitives on the module.
DesktopBody._native = sys.modules[__name__]
