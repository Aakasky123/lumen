"""macOS desktop body: the platform primitives behind app.desktop_body.DesktopBody's shared methods.

The shared methods (screenshot assembly, click and type receipts, typed read-back, clipboard ledger) live in
``app.desktop_body`` and call every platform primitive as ``self._native.<name>``. This module provides those
names for macOS and a ``DesktopBody`` subclass whose ``_native`` is this module:

- capture: Quartz (one display per capture, Retina backing pixels; windows that ask to be left out of
  screen sharing, Lumen's own overlays among them, are not drawn);
- windows: CGWindowListCopyWindowInfo, layer 0, ids ``cgw:<CGWindowID>``;
- controls, focus, caret, and text: the Accessibility API (AXUIElement), with roles named in UI Automation's
  control-type vocabulary so the shared receipts and the loop read them unchanged;
- input: CGEvent (mouse, keyboard, Unicode typing, scroll wheel);
- clipboard: NSPasteboard (its change count is the sequence number);
- apps and files: .app bundles from the Applications folders, opened with /usr/bin/open.

Coordinates: capture pixel (x, y) of the view display is absolute point (left + x, top + y), where absolute
coordinates are global display points times the view display's backing scale. Events are posted in points.

Every pyobjc import is lazy (inside a function), so this module imports on any OS and tests fake its
primitives the way test_desktop_body fakes the Windows ones.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import os
import plistlib
import re
import subprocess
import sys
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from app import desktop_body as _core
from app.config import get_settings
from app.desktop_body import (  # noqa: F401 - shared helpers the shared methods and tests reach through here
    _CONTROL_LOCK,
    _KEYSTROKE_INTERVAL_SECONDS,
    _NON_TEXT_CONTROL_TYPES,
    _NOT_TEXT_CONTROL_TYPES,
    _PASTE_THRESHOLD_CHARS,
    _STACKING_UNKNOWN,
    _TYPABLE_TAGS,
    _monitor_of_box,
    _normalize_key_parts,
    _on_native_thread,
    _safe_text,
    pointer_events,
    publish_pointer_event,
    set_runtime_enabled,
    text_line_window,
)

DEVICE_ID = "macos-desktop"
_SURFACE_URL_PREFIX = "desktop://macos/"
_DESKTOP_TITLE = "macOS desktop"
_WINDOWS_HEADING = "Visible windows:"
# Finder reports its folder (folder_view). Window "classes" here are owning apps' bundle ids.
_FOLDER_WINDOW_CLASS = "com.apple.finder"
# The Dock and window-manager surfaces never describe one window's content.
_SHELL_WINDOW_CLASSES = frozenset({"com.apple.dock", "com.apple.WindowManager", "com.apple.systemuiserver"})
_SELECT_ALL_KEY = "CMD+A"
_PASTE_KEY = "CMD+V"

_MAX_CONTROLS = 140
_MAX_WINDOWS = 40
# The accessibility walk of one window stops after this many elements or this long, whichever comes first
# (a web page's tree can hold tens of thousands).
_AX_WALK_NODE_CAP = 2500
_AX_WALK_SECONDS = 1.5
_AX_MESSAGING_TIMEOUT = 0.5

_VIEW: dict[str, Any] = {"pinned": None, "rect": None, "display": None, "scale": 1.0, "origin_pt": (0.0, 0.0)}
_LAST_TARGET_WINDOW: int = 0
_LAST_CLICK_POINT: tuple[int, int] | None = None
_LAST_FOCUS_REPAIR: dict[str, Any] = {"result": ""}
_APP_CACHE: tuple[float, list[dict[str, str]], dict[str, dict[str, str]]] | None = None


# --- configuration and status -------------------------------------------------------------------------


def _macos_available() -> bool:
    return sys.platform == "darwin"


def _configured() -> bool:
    return bool(get_settings().desktop_control_enabled and _macos_available())


def runtime_enabled() -> bool:
    with _CONTROL_LOCK:
        return bool(_core._RUNTIME_ENABLED and _configured())


def _permissions() -> dict[str, bool]:
    """The macOS privacy permissions this body needs, as the system reports them for this process (in
    development macOS charges them to the app that launched the backend, Terminal or the editor)."""
    found = {"accessibility": False, "screen_recording": False, "post_events": False}
    try:
        import ApplicationServices

        found["accessibility"] = bool(ApplicationServices.AXIsProcessTrusted())
    except Exception:  # noqa: BLE001 - unknown is missing
        pass
    try:
        import Quartz

        found["screen_recording"] = bool(Quartz.CGPreflightScreenCaptureAccess())
        found["post_events"] = bool(Quartz.CGPreflightPostEventAccess())
    except Exception:  # noqa: BLE001
        pass
    return found


def request_permissions() -> dict[str, bool]:
    """Ask macOS to show its permission prompts (Accessibility, Screen Recording). Neither grants anything
    by itself: the user decides in System Settings, and Screen Recording takes effect after a relaunch."""
    try:
        import ApplicationServices

        ApplicationServices.AXIsProcessTrustedWithOptions({"AXTrustedCheckOptionPrompt": True})
    except Exception:  # noqa: BLE001
        pass
    try:
        import Quartz

        Quartz.CGRequestScreenCaptureAccess()
    except Exception:  # noqa: BLE001
        pass
    return _permissions()


PERMISSION_SETTINGS_URLS = {
    "accessibility": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    "screen_recording": "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
    "post_events": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
}


def desktop_control_status() -> dict[str, Any]:
    dependency_ready = False
    dependency_error = ""
    if _macos_available():
        try:
            import AppKit  # noqa: F401
            import ApplicationServices  # noqa: F401
            import Quartz  # noqa: F401

            dependency_ready = True
        except Exception as exc:  # noqa: BLE001 - status must remain readable
            dependency_error = str(exc) or exc.__class__.__name__
    permissions = _permissions() if dependency_ready else {"accessibility": False, "screen_recording": False, "post_events": False}
    missing = [name for name, granted in permissions.items() if not granted]
    configured = _configured()
    ready = bool(configured and dependency_ready and not missing)
    enabled = bool(ready and runtime_enabled())
    with _CONTROL_LOCK:
        control_active = bool(_core._ACTIVE_CONTROL_RUN_IDS)
        control_run_ids = sorted(_core._ACTIVE_CONTROL_RUN_IDS)
        control_status_message = _core._CONTROL_STATUS_MESSAGE
        desktop_surface_active = any(surface == "desktop" for surface in _core._ACTIVE_CONTROL_RUN_IDS.values())
    return {
        "available": ready,
        "configured": configured,
        "enabled": enabled,
        "control_indicator": {
            "active": bool(enabled and control_active),
            "message": control_status_message or "Lumen is using your computer",
            "run_ids": control_run_ids,
            "desktop_surface_active": bool(enabled and desktop_surface_active),
        },
        "platform": "Darwin",
        "dependency_ready": dependency_ready,
        "dependency_error": dependency_error or None,
        "permissions": permissions,
        "permissions_missing": missing,
        "permission_settings": {name: PERMISSION_SETTINGS_URLS[name] for name in missing},
        "device_id": DEVICE_ID,
        "capabilities": [
            "display_screenshot",
            "password_control_masking",
            "visible_window_inventory",
            "active_window_ax_inventory",
            "exact_ax_target_rebinding",
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
            # No shell sandbox on macOS: run_command is not offered.
            "shell_execution": "unavailable",
            "raw_clipboard_read": "run_copies_only",
        },
    }


_PERMISSION_NAMES = {"accessibility": "Accessibility", "screen_recording": "Screen Recording", "post_events": "Accessibility"}


def permission_message() -> str:
    """The run-start refusal when macOS has not granted what the body needs, naming where to grant it."""
    missing = sorted({_PERMISSION_NAMES[name] for name in _permissions() if not _permissions()[name]}) or ["Accessibility"]
    names = " and ".join(missing)
    return (
        f"I cannot start this Mac computer task yet: macOS has not given Lumen {names} permission. "
        f"Turn Lumen on under System Settings > Privacy & Security > {missing[0]}"
        + (f" (and {missing[1]})" if len(missing) > 1 else "")
        + ", reopen Lumen, then try again."
    )


# Windows wording in the router and chat prompts and in user-facing lines, said for a Mac
# (desktop_platform.os_text). Longest phrases first so a shorter one never splits a longer one.
OS_TEXT_REPLACEMENTS = (
    # System Settings is not offered on a Mac (see _PROHIBITED_BUNDLE_IDS), so the sentence names only Finder.
    # The browser surface is the Chrome extension, so a goal that names another browser app (Safari) starts on the
    # desktop even when it carries a web address.
    ("native Windows applications, Settings, or Explorer with no starting web page; target_url must then be empty",
     "native Mac applications or Finder with no starting web page, or when the goal names a browser app other than "
     "Chrome (Safari, Firefox, Brave, Microsoft Edge, Opera), even if it also gives a web address; "
     "target_url must then be empty"),
    ("Windows desktop tasks", "Mac desktop tasks"),
    ("Windows desktop task", "Mac desktop task"),
    ("Computer task: Windows", "Computer task: Mac"),
    ("Windows interface", "Mac interface"),
    ("packaged Windows desktop app", "packaged Mac desktop app"),
    ("the Windows desktop", "the Mac desktop"),
    ("Windows computer", "Mac computer"),
    ("Windows application", "Mac application"),
    ("Windows apps", "Mac apps"),
    ("Windows app", "Mac app"),
)


# Browser apps the browser surface (the Chrome extension) cannot drive; a goal that names one starts on the desktop.
OTHER_BROWSER_APPS = (
    r"safari(?: technology preview)?", r"firefox", r"brave", r"microsoft edge", r"opera(?: gx)?", r"vivaldi",
)


def _require_enabled() -> None:
    status = desktop_control_status()
    if not status["available"]:
        missing = status.get("permissions_missing") or []
        if status.get("configured") and status.get("dependency_ready") and missing:
            names = ", ".join(name.replace("_", " ") for name in missing)
            raise RuntimeError(f"macOS computer use needs permission: {names} (System Settings > Privacy & Security).")
        raise RuntimeError("macOS computer use is unavailable in this runtime.")
    if not status["enabled"]:
        raise RuntimeError("macOS computer use is paused.")


# --- displays, the view, and coordinates ----------------------------------------------------------------


def _displays() -> list[dict[str, Any]]:
    """Active displays: id, bounds in global points, backing scale, main flag."""
    import Quartz

    err, ids, count = Quartz.CGGetActiveDisplayList(16, None, None)
    if err:
        return []
    main = int(Quartz.CGMainDisplayID())
    found = []
    for display in list(ids or ())[: int(count or 0)]:
        bounds = Quartz.CGDisplayBounds(display)
        width_pt = float(bounds.size.width)
        if width_pt <= 0 or bounds.size.height <= 0:
            continue
        scale = 1.0
        try:
            mode = Quartz.CGDisplayCopyDisplayMode(display)
            pixel_width = float(Quartz.CGDisplayModeGetPixelWidth(mode)) if mode is not None else 0.0
            scale = pixel_width / width_pt if pixel_width else 1.0
        except Exception:  # noqa: BLE001 - an unreadable mode is 1x
            scale = 1.0
        found.append(
            {
                "id": int(display),
                "bounds": (float(bounds.origin.x), float(bounds.origin.y), width_pt, float(bounds.size.height)),
                "scale": max(1.0, round(scale * 4) / 4),
                "primary": int(display) == main,
            }
        )
    return found


def _monitors() -> list[dict[str, Any]]:
    """Connected displays numbered 1.. left to right (then top to bottom). ``rect`` is in absolute
    coordinates at the current view's scale (so it compares with window boxes)."""
    try:
        displays = _displays()
    except Exception:  # noqa: BLE001 - unknown: one display
        return []
    scale = float(_VIEW.get("scale") or 1.0)
    displays.sort(key=lambda item: (item["bounds"][0], item["bounds"][1]))
    monitors = []
    for index, item in enumerate(displays, start=1):
        x, y, w, h = item["bounds"]
        monitors.append(
            {
                "monitor": index,
                "rect": (round(x * scale), round(y * scale), round(w * scale), round(h * scale)),
                "primary": item["primary"],
                "display": item["id"],
                "display_scale": item["scale"],
            }
        )
    return monitors


def _set_view(display: dict[str, Any] | None, bounds: tuple[float, float, float, float], scale: float) -> None:
    _VIEW["display"] = display
    _VIEW["scale"] = float(scale)
    _VIEW["origin_pt"] = (float(bounds[0]), float(bounds[1]))
    _VIEW["rect"] = (
        round(bounds[0] * scale),
        round(bounds[1] * scale),
        round(bounds[2] * scale),
        round(bounds[3] * scale),
    )


def _select_view(monitors: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Choose the display the next capture shows: the pinned one (focus_window monitor:N), else the one
    holding most of the front window, else the main display. Every capture is one display on macOS (one
    backing scale per image), so the view is always set. Returns the monitors when there are several."""
    del monitors
    try:
        displays = _displays()
    except Exception:  # noqa: BLE001
        displays = []
    if not displays:
        _set_view(None, (0.0, 0.0, 1.0, 1.0), 1.0)
        return []
    ordered = sorted(displays, key=lambda item: (item["bounds"][0], item["bounds"][1]))
    chosen_index = int(_VIEW.get("pinned") or 0)
    if not 1 <= chosen_index <= len(ordered):
        _VIEW["pinned"] = None
        chosen_index = 0
        front = _front_window_bounds_pt() if len(ordered) > 1 else None
        if front is not None:
            best_area = 0.0
            for index, item in enumerate(ordered, start=1):
                x, y, w, h = item["bounds"]
                overlap_w = min(front[0] + front[2], x + w) - max(front[0], x)
                overlap_h = min(front[1] + front[3], y + h) - max(front[1], y)
                if overlap_w > 0 and overlap_h > 0 and overlap_w * overlap_h > best_area:
                    chosen_index, best_area = index, overlap_w * overlap_h
        if not chosen_index:
            chosen_index = next((index for index, item in enumerate(ordered, start=1) if item["primary"]), 1)
    chosen = ordered[chosen_index - 1]
    _set_view(chosen, chosen["bounds"], chosen["scale"])
    monitors = _monitors()
    return monitors if len(monitors) > 1 else []


def _scale() -> float:
    return float(_VIEW.get("scale") or 1.0)


def _virtual_screen() -> tuple[int, int, int, int]:
    """The capture region in absolute coordinates: capture pixel (x, y) is absolute (left + x, top + y)."""
    if not _VIEW.get("rect"):
        _select_view()
    rect = _VIEW.get("rect") or (0, 0, 1, 1)
    return tuple(int(value) for value in rect)  # type: ignore[return-value]


def _to_absolute(x_pt: float, y_pt: float) -> tuple[int, int]:
    scale = _scale()
    return round(float(x_pt) * scale), round(float(y_pt) * scale)


def _to_points(abs_x: float, abs_y: float) -> tuple[float, float]:
    scale = _scale()
    return float(abs_x) / scale, float(abs_y) / scale


def _absolute_point(x: int, y: int) -> tuple[int, int]:
    left, top, width, height = _virtual_screen()
    return (
        max(left, min(left + width - 1, left + int(x))),
        max(top, min(top + height - 1, top + int(y))),
    )


def _box_from_pt(x: float, y: float, w: float, h: float) -> list[int]:
    """A global-points rectangle as an absolute [left, top, width, height]."""
    left, top = _to_absolute(x, y)
    right, bottom = _to_absolute(x + w, y + h)
    return [left, top, right - left, bottom - top]


# --- capture -------------------------------------------------------------------------------------------


def _excluded_from_capture(info: dict[str, Any]) -> bool:
    """A window that asked to be left out of screen sharing (NSWindowSharingNone: Electron's content
    protection, which Lumen's overlays set) is not drawn into a capture, as on Windows."""
    try:
        return int(info.get("kCGWindowSharingState", 1)) == 0
    except (TypeError, ValueError):
        return False


def _cgimage_to_pil(cgimage: Any) -> Any:
    import Quartz

    width = int(Quartz.CGImageGetWidth(cgimage))
    height = int(Quartz.CGImageGetHeight(cgimage))
    bytes_per_row = int(Quartz.CGImageGetBytesPerRow(cgimage))
    data = Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(cgimage))
    image = Image.frombuffer("RGBA", (width, height), bytes(data), "raw", "BGRA", bytes_per_row, 1)
    return image.convert("RGB")


def _capture_image() -> Any:
    """The view display as an image in its backing pixels, without windows excluded from capture.

    Raises when Screen Recording is not granted: macOS then returns the wallpaper alone, and a run must
    never act on a picture of an empty desktop."""
    import Quartz

    if not Quartz.CGPreflightScreenCaptureAccess():
        raise RuntimeError("Screen Recording permission is required to see the screen.")
    _, _, width, height = _virtual_screen()
    x_pt, y_pt = _VIEW.get("origin_pt") or (0.0, 0.0)
    scale = _scale()
    region = Quartz.CGRectMake(x_pt, y_pt, width / scale, height / scale)
    infos = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []
    options = Quartz.kCGWindowImageBestResolution
    if any(_excluded_from_capture(info) for info in infos):
        keep = [int(info["kCGWindowNumber"]) for info in infos if not _excluded_from_capture(info)]
        cgimage = Quartz.CGWindowListCreateImageFromArray(region, keep, options)
    else:
        cgimage = Quartz.CGWindowListCreateImage(
            region, Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID, options
        )
    if cgimage is None:
        raise RuntimeError("The screen could not be captured.")
    image = _cgimage_to_pil(cgimage)
    if image.size != (width, height):
        image = image.resize((width, height))
    return image


# --- windows ---------------------------------------------------------------------------------------------


def _window_infos() -> list[dict[str, Any]]:
    """On-screen windows front to back (CGWindowList order), layer 0, drawn in captures."""
    import Quartz

    infos = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID
    ) or []
    found = []
    for info in infos:
        try:
            if int(info.get("kCGWindowLayer", 0)) != 0 or _excluded_from_capture(info):
                continue
            bounds = info.get("kCGWindowBounds") or {}
            w, h = float(bounds.get("Width", 0)), float(bounds.get("Height", 0))
            if w < 2 or h < 2 or float(info.get("kCGWindowAlpha", 1.0)) <= 0:
                continue
            found.append(
                {
                    "id": int(info["kCGWindowNumber"]),
                    "pid": int(info.get("kCGWindowOwnerPID", 0)),
                    "owner": str(info.get("kCGWindowOwnerName") or ""),
                    "title": str(info.get("kCGWindowName") or ""),
                    "bounds": (float(bounds.get("X", 0)), float(bounds.get("Y", 0)), w, h),
                }
            )
        except Exception:  # noqa: BLE001 - a closing window
            continue
    return found


def _frontmost_pid() -> int:
    """The app that owns keyboard focus. Read through Accessibility (NSWorkspace's frontmost app goes stale
    in a process without a main run loop); else the owner of the frontmost normal window."""
    try:
        import ApplicationServices as AX

        err, app = AX.AXUIElementCopyAttributeValue(AX.AXUIElementCreateSystemWide(), "AXFocusedApplication", None)
        if not err and app is not None:
            err, pid = AX.AXUIElementGetPid(app, None)
            if not err and pid:
                return int(pid)
    except Exception:  # noqa: BLE001
        pass
    try:
        infos = _window_infos()
        return int(infos[0]["pid"]) if infos else 0
    except Exception:  # noqa: BLE001
        return 0


def _foreground_window_id() -> int:
    pid = _frontmost_pid()
    if not pid:
        return 0
    app = _ax_app(pid)
    window = _ax_attr(app, "AXFocusedWindow") or _ax_attr(app, "AXMainWindow")
    window_id = _ax_window_id(window) if window is not None else 0
    if window_id:
        return window_id
    try:
        # No accessibility answer: the app's frontmost on-screen window.
        return next((item["id"] for item in _window_infos() if item["pid"] == pid), 0)
    except Exception:  # noqa: BLE001
        return 0


def _foreground_handle() -> int:
    return _foreground_window_id()


def _front_window_bounds_pt() -> tuple[float, float, float, float] | None:
    handle = _foreground_window_id()
    if not handle:
        return None
    return next((item["bounds"] for item in _window_infos() if item["id"] == handle), None)


def _bundle_id(pid: int) -> str:
    try:
        import AppKit

        app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid))
        return str(app.bundleIdentifier() or "") if app is not None else ""
    except Exception:  # noqa: BLE001
        return ""


def _window_title(item: dict[str, Any]) -> str:
    """A window's title with its app's name, the way Windows titles read ("Notes - TextEdit"); the app
    name alone for an untitled window."""
    title, owner = _safe_text(item.get("title"), 150), _safe_text(item.get("owner"), 28)
    if title and owner and owner.casefold() not in title.casefold():
        return f"{title} - {owner}"
    return title or owner


def _settings_window(item: dict[str, Any]) -> bool:
    """A System Settings (System Preferences) window: never listed, so never focusable by id."""
    owner = str(item.get("owner") or "").casefold()
    return owner in {"system settings", "system preferences"} or _bundle_id_prohibited(_bundle_id(int(item.get("pid") or 0)))


def _window_inventory() -> list[dict[str, Any]]:
    active = _foreground_window_id()
    windows: list[dict[str, Any]] = []
    for item in _window_infos():
        title = _window_title(item)
        if not title or _settings_window(item):
            continue
        windows.append(
            {
                "window_id": f"cgw:{item['id']}",
                "title": title,
                "pid": item["pid"],
                "active": item["id"] == active,
                "box": _box_from_pt(*item["bounds"]),
            }
        )
        if len(windows) >= _MAX_WINDOWS:
            break
    windows.sort(key=lambda entry: (not entry["active"], entry["title"].casefold()))
    return windows


def window_ids_snapshot() -> frozenset[str]:
    try:
        return frozenset(str(item.get("window_id") or "") for item in _window_inventory() if item.get("window_id"))
    except Exception:  # noqa: BLE001
        return frozenset()


def _parse_window_id(window_id: Any) -> int | None:
    match = re.fullmatch(r"cgw:(\d+)", str(window_id or "").strip())
    return int(match.group(1)) if match else None


def _window_pid(handle: int) -> int:
    try:
        return next((item["pid"] for item in _window_infos() if item["id"] == int(handle)), 0)
    except Exception:  # noqa: BLE001
        return 0


def _window_class(window_id: Any) -> str:
    """The owning app's bundle id, the closest thing a macOS window has to a window class."""
    handle = _parse_window_id(window_id)
    pid = _window_pid(handle) if handle is not None else 0
    return _bundle_id(pid) if pid else ""


def _window_capture_box(handle: int) -> tuple[int, int, int, int] | None:
    try:
        bounds = next((item["bounds"] for item in _window_infos() if item["id"] == int(handle)), None)
    except Exception:  # noqa: BLE001
        return None
    if bounds is None:
        return None
    left, top, width, height = _box_from_pt(*bounds)
    origin_x, origin_y, _, _ = _virtual_screen()
    if width <= 0 or height <= 0:
        return None
    return (left - origin_x, top - origin_y, left + width - origin_x, top + height - origin_y)


def _foreground_stacking() -> tuple[str, list[int], list[list[int]]]:
    """The window in front, its absolute rectangle, and what is drawn over it (capture pixels)."""
    try:
        import Quartz

        handle = _foreground_window_id()
        if not handle:
            return "", [], list(_STACKING_UNKNOWN)
        bounds = next((item["bounds"] for item in _window_infos() if item["id"] == handle), None)
        if bounds is None:
            return "", [], list(_STACKING_UNKNOWN)
        box = _box_from_pt(*bounds)
        origin_x, origin_y, _, _ = _virtual_screen()
        above = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenAboveWindow, handle) or []
        covering: list[list[int]] = []
        right, bottom = box[0] + box[2], box[1] + box[3]
        for info in above:
            if _excluded_from_capture(info) or float(info.get("kCGWindowAlpha", 1.0)) <= 0:
                continue
            raw = info.get("kCGWindowBounds") or {}
            a_left, a_top, a_width, a_height = _box_from_pt(
                float(raw.get("X", 0)), float(raw.get("Y", 0)), float(raw.get("Width", 0)), float(raw.get("Height", 0))
            )
            if a_width <= 0 or a_height <= 0:
                continue
            if a_left + a_width > box[0] and a_left < right and a_top + a_height > box[1] and a_top < bottom:
                covering.append([a_left - origin_x, a_top - origin_y, a_width, a_height])
        if len(covering) > 32:
            return f"cgw:{handle}", box, list(_STACKING_UNKNOWN)
        return f"cgw:{handle}", box, covering
    except Exception:  # noqa: BLE001 - unknown stacking
        return "", [], list(_STACKING_UNKNOWN)


def _foreground_window() -> tuple[int, str]:
    handle = _foreground_window_id()
    if not handle:
        return 0, ""
    try:
        item = next((entry for entry in _window_infos() if entry["id"] == handle), None)
        return handle, _window_title(item) if item else ""
    except Exception:  # noqa: BLE001
        return handle, ""


def _remember_target_window(handle: Any = None) -> int:
    global _LAST_TARGET_WINDOW
    try:
        if handle is None:
            handle = _foreground_window_id()
        with _CONTROL_LOCK:
            _LAST_TARGET_WINDOW = int(handle or 0)
            return _LAST_TARGET_WINDOW
    except Exception:  # noqa: BLE001
        return 0


def _same_app_window(handle: int, targeted: int) -> bool:
    """That window itself, or another window of the same app (a Save sheet or alert it opened)."""
    if not handle or not targeted:
        return False
    if int(handle) == int(targeted):
        return True
    pid = _window_pid(handle)
    return bool(pid) and pid == _window_pid(targeted)


def _foreground_process_id() -> int:
    return _frontmost_pid()


def _set_foreground_window(handle: int) -> None:
    """Best effort: unminimize and raise the window and make its app frontmost. The caller decides success
    by reading the foreground window again."""
    pid = _window_pid(handle)
    if pid and _bundle_id_prohibited(_bundle_id(pid)):
        return  # System Settings is never raised; the caller reads the foreground and reports no focus
    window = _ax_window_for(handle)
    if window is not None:
        _ax_set(window, "AXMinimized", False)
        _ax_action(window, "AXRaise")
        _ax_set(window, "AXMain", True)
    if pid:
        _ax_set(_ax_app(pid), "AXFrontmost", True)
        try:
            import AppKit

            app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid))
            if app is not None:
                app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
        except Exception:  # noqa: BLE001
            pass


# --- accessibility ---------------------------------------------------------------------------------------


def _ax_app(pid: int) -> Any:
    import ApplicationServices as AX

    element = AX.AXUIElementCreateApplication(int(pid))
    try:
        AX.AXUIElementSetMessagingTimeout(element, _AX_MESSAGING_TIMEOUT)
    except Exception:  # noqa: BLE001
        pass
    return element


def _ax_system() -> Any:
    import ApplicationServices as AX

    return AX.AXUIElementCreateSystemWide()


def _ax_attr(element: Any, name: str) -> Any:
    if element is None:
        return None
    try:
        import ApplicationServices as AX

        err, value = AX.AXUIElementCopyAttributeValue(element, name, None)
        return None if err else value
    except Exception:  # noqa: BLE001
        return None


def _ax_attrs(element: Any, names: list[str]) -> dict[str, Any]:
    """Several attributes in one round trip; a missing one reads as None."""
    try:
        import ApplicationServices as AX

        err, values = AX.AXUIElementCopyMultipleAttributeValues(element, names, 0, None)
        if err or values is None:
            raise ValueError
        result = {}
        for name, value in zip(names, list(values)):
            # A per-attribute failure comes back as an AXValue of the error type.
            if value is not None and type(value).__name__ == "AXValueRef":
                try:
                    if AX.AXValueGetType(value) == AX.kAXValueAXErrorType:
                        value = None
                except Exception:  # noqa: BLE001
                    pass
            result[name] = value
        return result
    except Exception:  # noqa: BLE001 - one at a time
        return {name: _ax_attr(element, name) for name in names}


def _ax_set(element: Any, name: str, value: Any) -> bool:
    if element is None:
        return False
    try:
        import ApplicationServices as AX

        return AX.AXUIElementSetAttributeValue(element, name, value) == 0
    except Exception:  # noqa: BLE001
        return False


def _ax_action(element: Any, name: str) -> bool:
    if element is None:
        return False
    try:
        import ApplicationServices as AX

        return AX.AXUIElementPerformAction(element, name) == 0
    except Exception:  # noqa: BLE001
        return False


def _ax_actions(element: Any) -> list[str]:
    try:
        import ApplicationServices as AX

        err, names = AX.AXUIElementCopyActionNames(element, None)
        return [str(name) for name in names or ()] if not err else []
    except Exception:  # noqa: BLE001
        return []


def _ax_settable(element: Any, name: str) -> bool:
    try:
        import ApplicationServices as AX

        err, settable = AX.AXUIElementIsAttributeSettable(element, name, None)
        return bool(settable) if not err else False
    except Exception:  # noqa: BLE001
        return False


def _ax_point(value: Any) -> tuple[float, float] | None:
    try:
        import ApplicationServices as AX

        ok, point = AX.AXValueGetValue(value, AX.kAXValueCGPointType, None)
        return (float(point.x), float(point.y)) if ok else None
    except Exception:  # noqa: BLE001
        return None


def _ax_size(value: Any) -> tuple[float, float] | None:
    try:
        import ApplicationServices as AX

        ok, size = AX.AXValueGetValue(value, AX.kAXValueCGSizeType, None)
        return (float(size.width), float(size.height)) if ok else None
    except Exception:  # noqa: BLE001
        return None


def _ax_range(value: Any) -> tuple[int, int] | None:
    if value is None:
        return None
    try:
        import ApplicationServices as AX

        ok, cf_range = AX.AXValueGetValue(value, AX.kAXValueCFRangeType, None)
        return (int(cf_range.location), int(cf_range.length)) if ok else None
    except Exception:  # noqa: BLE001
        return None


def _ax_make_range(location: int, length: int) -> Any:
    import ApplicationServices as AX

    return AX.AXValueCreate(AX.kAXValueCFRangeType, (int(location), int(length)))


_AX_LIBRARY: Any = None


def _ax_library() -> Any:
    global _AX_LIBRARY
    if _AX_LIBRARY is None:
        import ctypes

        library = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        library._AXUIElementGetWindow.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        library._AXUIElementGetWindow.restype = ctypes.c_int32
        _AX_LIBRARY = library
    return _AX_LIBRARY


def _ax_window_id(window: Any) -> int:
    """The CGWindowID behind an accessibility window (the private _AXUIElementGetWindow every window
    manager uses); 0 when unknown."""
    if window is None:
        return 0
    try:
        import ctypes

        out = ctypes.c_uint32(0)
        if _ax_library()._AXUIElementGetWindow(window.__c_void_p__(), ctypes.byref(out)) == 0:
            return int(out.value)
    except Exception:  # noqa: BLE001
        pass
    return 0


def _ax_window_for(handle: int) -> Any:
    """The accessibility window of a CGWindowID."""
    pid = _window_pid(handle)
    if not pid:
        return None
    for window in list(_ax_attr(_ax_app(pid), "AXWindows") or ()):
        if _ax_window_id(window) == int(handle):
            return window
    return None


class _ElementIds:
    """Stable small ids for accessibility elements across captures (selectors carry them as rid). Element
    references compare with CFEqual and hash with CFHash, so the same element keeps its id while it lives;
    a dead element's id stops resolving."""

    def __init__(self, limit: int = 20000) -> None:
        self._by_element: OrderedDict[Any, int] = OrderedDict()
        self._by_id: dict[int, Any] = {}
        self._next = 1
        self._limit = limit
        self._lock = threading.Lock()

    def id_of(self, element: Any) -> int:
        with self._lock:
            found = self._by_element.get(element)
            if found is not None:
                self._by_element.move_to_end(element)
                return found
            new_id = self._next
            self._next += 1
            self._by_element[element] = new_id
            self._by_id[new_id] = element
            while len(self._by_element) > self._limit:
                _, old_id = self._by_element.popitem(last=False)
                self._by_id.pop(old_id, None)
            return new_id

    def element(self, element_id: int) -> Any:
        with self._lock:
            return self._by_id.get(int(element_id))


_IDS = _ElementIds()

# Accessibility roles named in UI Automation's control-type vocabulary: the shared receipts, the typing
# binding, and the prompt key off these names (Edit, Document, ComboBox ...). The raw role goes in `type`.
_AX_ROLE_TO_TYPE = {
    "AXTextField": "Edit",
    "AXSearchField": "Edit",
    "AXSecureTextField": "Edit",
    "AXDateField": "Edit",
    "AXTextArea": "Document",
    "AXWebArea": "Document",
    "AXComboBox": "ComboBox",
    "AXPopUpButton": "ComboBox",
    "AXCheckBox": "CheckBox",
    "AXRadioButton": "RadioButton",
    "AXButton": "Button",
    "AXMenuButton": "SplitButton",
    "AXDisclosureTriangle": "Button",
    "AXColorWell": "Button",
    "AXLink": "Hyperlink",
    "AXMenuItem": "MenuItem",
    "AXMenuBarItem": "MenuItem",
    "AXMenu": "Menu",
    "AXMenuBar": "MenuBar",
    "AXRow": "ListItem",
    "AXCell": "DataItem",
    "AXOutlineRow": "TreeItem",
    "AXList": "List",
    "AXTable": "Table",
    "AXOutline": "Tree",
    "AXTabGroup": "Tab",
    "AXRadioGroup": "Group",
    "AXStaticText": "Text",
    "AXHeading": "Text",
    "AXImage": "Image",
    "AXSlider": "Slider",
    "AXIncrementor": "Spinner",
    "AXScrollBar": "ScrollBar",
    "AXToolbar": "ToolBar",
    "AXProgressIndicator": "ProgressBar",
    "AXSplitter": "Thumb",
    "AXWindow": "Window",
    "AXSheet": "Window",
    "AXDialog": "Window",
    "AXGroup": "Group",
    "AXScrollArea": "Pane",
    "AXSplitGroup": "Pane",
}
# Elements that never become controls themselves (their children may).
_AX_STRUCTURAL_ROLES = frozenset(
    {"AXGroup", "AXScrollArea", "AXSplitGroup", "AXLayoutArea", "AXLayoutItem", "AXUnknown", "AXMatte", "AXGrowArea",
     "AXWindow", "AXApplication", "AXSheet"}
)
_TEXT_ROLES = frozenset({"AXTextField", "AXSearchField", "AXSecureTextField", "AXTextArea", "AXComboBox", "AXDateField"})
_AX_ELEMENT_ATTRS = [
    "AXRole",
    "AXSubrole",
    "AXTitle",
    "AXDescription",
    "AXValue",
    "AXPosition",
    "AXSize",
    "AXEnabled",
    "AXFocused",
    "AXSelected",
    "AXExpanded",
    "AXHelp",
    "AXPlaceholderValue",
    "AXURL",
]


class _Rect:
    __slots__ = ("left", "top", "right", "bottom")

    def __init__(self, left: int, top: int, right: int, bottom: int) -> None:
        self.left, self.top, self.right, self.bottom = int(left), int(top), int(right), int(bottom)


class _AXInfo:
    """The element_info view the shared methods read (rectangle in absolute coordinates, is_password ...)."""

    def __init__(self, element: Any, attrs: dict[str, Any] | None = None) -> None:
        self.element = element
        self._attrs = attrs if attrs is not None else (_ax_attrs(element, _AX_ELEMENT_ATTRS) if element is not None else {})

    @property
    def role(self) -> str:
        return str(self._attrs.get("AXRole") or "")

    @property
    def subrole(self) -> str:
        return str(self._attrs.get("AXSubrole") or "")

    @property
    def control_type(self) -> str:
        if self.role == "AXStaticText" and self._attrs.get("AXURL"):
            return "Hyperlink"
        return _AX_ROLE_TO_TYPE.get(self.role, (self.role[2:] if self.role.startswith("AX") else self.role) or "Custom")

    @property
    def class_name(self) -> str:
        return f"{self.role}/{self.subrole}" if self.subrole else self.role

    @property
    def name(self) -> str:
        for key in ("AXTitle", "AXDescription", "AXPlaceholderValue", "AXHelp"):
            value = self._attrs.get(key)
            if isinstance(value, str) and value.strip():
                return value
        if self.role in {"AXStaticText", "AXHeading"}:
            value = self._attrs.get("AXValue")
            return value if isinstance(value, str) else ""
        return ""

    @property
    def is_password(self) -> bool:
        return self.role == "AXSecureTextField" or self.subrole == "AXSecureTextField"

    @property
    def enabled(self) -> bool:
        value = self._attrs.get("AXEnabled")
        return True if value is None else bool(value)

    @property
    def visible(self) -> bool:
        rect = self.rect_pt
        return rect is not None and rect[2] > 0 and rect[3] > 0

    @property
    def has_keyboard_focus(self) -> bool:
        return bool(self._attrs.get("AXFocused"))

    @property
    def rect_pt(self) -> tuple[float, float, float, float] | None:
        position = _ax_point(self._attrs.get("AXPosition")) if self._attrs.get("AXPosition") is not None else None
        size = _ax_size(self._attrs.get("AXSize")) if self._attrs.get("AXSize") is not None else None
        if position is None or size is None:
            return None
        return (position[0], position[1], size[0], size[1])

    @property
    def rectangle(self) -> _Rect:
        rect = self.rect_pt
        if rect is None:
            raise ValueError("element has no frame")
        left, top, width, height = _box_from_pt(*rect)
        return _Rect(left, top, left + width, top + height)

    @property
    def runtime_id(self) -> tuple[int, ...]:
        return (_IDS.id_of(self.element),)

    @property
    def value(self) -> Any:
        return self._attrs.get("AXValue")


class _AXWrapper:
    """The pywinauto-like wrapper the shared methods hold for a resolved control."""

    def __init__(self, element: Any, attrs: dict[str, Any] | None = None) -> None:
        self.element = element
        self.element_info = _AXInfo(element, attrs)

    def set_focus(self) -> None:
        if not _ax_set(self.element, "AXFocused", True):
            raise RuntimeError("focus was not taken")

    def click_input(self) -> None:
        if "AXPress" in _ax_actions(self.element) and _ax_action(self.element, "AXPress"):
            return
        point = _clickable_point(self)
        if point is None:
            raise RuntimeError("the control has no clickable point")
        origin_x, origin_y, _, _ = _virtual_screen()
        _mouse_click(point[0] - origin_x, point[1] - origin_y)

    def is_selected(self) -> bool:
        value = _ax_attr(self.element, "AXSelected")
        if value is None:
            raise ValueError("no selection state")
        return bool(value)

    def get_toggle_state(self) -> int:
        if self.element_info.role not in {"AXCheckBox", "AXRadioButton"}:
            raise ValueError("not a toggle")
        return int(_ax_attr(self.element, "AXValue") or 0)

    def descendants(self) -> list[_AXWrapper]:
        return [_AXWrapper(element, attrs) for element, attrs in _ax_walk(self.element, cap=_AX_WALK_NODE_CAP)]


def _ax_children(element: Any, role: str = "") -> list[Any]:
    # Lists and tables expose only their visible rows as AXVisibleChildren; walking every row of a long
    # list would spend the budget on rows nobody can see.
    if role in {"AXList", "AXTable", "AXOutline", "AXBrowser"}:
        visible = _ax_attr(element, "AXVisibleChildren") or _ax_attr(element, "AXVisibleRows")
        if visible is not None:
            return list(visible)
    return list(_ax_attr(element, "AXChildren") or ())


def _ax_walk(root: Any, *, cap: int, deadline: float | None = None) -> Iterator[tuple[Any, dict[str, Any]]]:
    """Breadth-first walk of an element's descendants with their attributes, bounded by count and time."""
    deadline = deadline if deadline is not None else time.monotonic() + _AX_WALK_SECONDS
    queue: deque[Any] = deque(_ax_children(root, str(_ax_attr(root, "AXRole") or "")))
    seen = 0
    while queue and seen < cap and time.monotonic() < deadline:
        element = queue.popleft()
        attrs = _ax_attrs(element, _AX_ELEMENT_ATTRS)
        seen += 1
        yield element, attrs
        queue.extend(_ax_children(element, str(attrs.get("AXRole") or "")))


def _enable_web_accessibility(pid: int) -> None:
    """Chromium and Electron apps build their web content's accessibility tree only when asked.
    (AXEnhancedUserInterface would too, but it breaks window animations in some apps.)"""
    _ax_set(_ax_app(pid), "AXManualAccessibility", True)


def _control_affordances(wrapper: _AXWrapper) -> dict[str, Any]:
    info = wrapper.element_info
    facts: dict[str, Any] = {}
    affordances: list[str] = []
    if info.role in {"AXCheckBox", "AXRadioButton"}:
        affordances.append("toggle")
        try:
            facts["checked"] = bool(int(info.value or 0))
        except (TypeError, ValueError):
            pass
    selected = info._attrs.get("AXSelected")
    if selected is not None and info.role in {"AXRow", "AXCell", "AXOutlineRow", "AXMenuItem", "AXTab"}:
        facts["selected"] = bool(selected)
        affordances.append("select")
    expanded = info._attrs.get("AXExpanded")
    if expanded is not None:
        facts["expanded"] = bool(expanded)
        affordances.append("expand_collapse")
    if info.role in _TEXT_ROLES:
        affordances.append("set_value")
        facts["accepts_text"] = True
    actions = _ax_actions(wrapper.element)
    if "AXPress" in actions or "AXPick" in actions or "AXConfirm" in actions:
        affordances.append("invoke")
    if info.role in {"AXScrollArea", "AXScrollBar"}:
        affordances.append("scroll")
    if affordances:
        facts["affordances"] = affordances
    return facts


def _control_value(wrapper: Any, *, password: bool) -> str:
    if password:
        return ""
    try:
        info = wrapper.element_info
        value = info.value
        if info.role == "AXWebArea":
            # A browser's page reports its address as the document's value, as UI Automation does.
            url = info._attrs.get("AXURL")
            value = str(url) if url is not None else ""
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = str(value)
        if isinstance(value, str):
            return _safe_text(value, 240)
    except Exception:  # noqa: BLE001
        pass
    return ""


def _selector(window_id: int, element_id: int) -> str:
    return f"ax:win={int(window_id)};rid={int(element_id)}"


def _parse_selector(selector: str) -> tuple[int, tuple[int, ...]] | None:
    match = re.fullmatch(r"ax:win=(\d+);rid=(\d+)", str(selector or "").strip())
    if not match:
        return None
    return int(match.group(1)), (int(match.group(2)),)


def _resolve_control(selector: str) -> _AXWrapper | None:
    parsed = _parse_selector(selector)
    if parsed is None:
        return None
    element = _IDS.element(parsed[1][0])
    if element is None:
        return None
    attrs = _ax_attrs(element, _AX_ELEMENT_ATTRS)
    if attrs.get("AXRole") is None:
        return None  # the element is gone (kAXErrorInvalidUIElement)
    return _AXWrapper(element, attrs)


def _focused_element() -> Any:
    return _ax_attr(_ax_system(), "AXFocusedUIElement")


def _active_window_element() -> tuple[Any, int, int]:
    """The front app's focused window element, its CGWindowID, and the app's pid."""
    pid = _frontmost_pid()
    if not pid:
        return None, 0, 0
    app = _ax_app(pid)
    window = _ax_attr(app, "AXFocusedWindow") or _ax_attr(app, "AXMainWindow")
    return window, _ax_window_id(window), pid


def _active_controls() -> tuple[list[dict[str, Any]], list[tuple[int, int, int, int]]]:
    window, window_id, pid = _active_window_element()
    if window is None or not window_id:
        return [], []
    _enable_web_accessibility(pid)
    left, top, view_width, view_height = _virtual_screen()
    focused = _focused_element()
    controls: list[dict[str, Any]] = []
    password_boxes: list[tuple[int, int, int, int]] = []
    collected: set[int] = set()
    window_rect = _AXInfo(window).rect_pt

    def collect(element: Any, attrs: dict[str, Any], handle: int) -> None:
        info = _AXInfo(element, attrs)
        role = info.role
        if not role or role in _AX_STRUCTURAL_ROLES:
            return
        rect = info.rect_pt
        if rect is None or rect[2] < 1 or rect[3] < 1:
            return
        if window_rect is not None and handle == window_id:
            # Outside the window (a row scrolled out of its list): not on screen.
            if rect[0] >= window_rect[0] + window_rect[2] or rect[1] >= window_rect[1] + window_rect[3]:
                return
            if rect[0] + rect[2] <= window_rect[0] or rect[1] + rect[3] <= window_rect[1]:
                return
        if role in {"AXStaticText", "AXImage"} and not info.name:
            return
        element_id = _IDS.id_of(element)
        if element_id in collected:
            return
        abs_left, abs_top, width, height = _box_from_pt(*rect)
        box = [abs_left - left, abs_top - top, width, height]
        if box[0] >= view_width or box[1] >= view_height or box[0] + box[2] <= 0 or box[1] + box[3] <= 0:
            return
        password = info.is_password
        name = _safe_text(info.name, 260)
        control_type = info.control_type
        wrapper = _AXWrapper(element, attrs)
        controls.append(
            {
                "selector": _selector(handle, element_id),
                "tag": control_type.casefold(),
                "role": control_type,
                "type": _safe_text(info.class_name, 120),
                "text": name,
                "name": name,
                "accessible_name": name,
                "value": _control_value(wrapper, password=password),
                "box": box,
                "x": box[0],
                "y": box[1],
                "width": box[2],
                "height": box[3],
                "visible": True,
                "enabled": info.enabled,
                "disabled": not info.enabled,
                "focused": bool(info.has_keyboard_focus) or (focused is not None and element == focused),
                "password": password,
                "automation_id": "",
                "framework_id": "AX",
                **_control_affordances(wrapper),
            }
        )
        collected.add(element_id)
        if password:
            password_boxes.append((box[0], box[1], box[0] + box[2], box[1] + box[3]))

    deadline = time.monotonic() + _AX_WALK_SECONDS
    for element, attrs in _ax_walk(window, cap=_AX_WALK_NODE_CAP, deadline=deadline):
        if len(controls) >= _MAX_CONTROLS:
            break
        collect(element, attrs, window_id)
    if focused is not None and _IDS.id_of(focused) not in collected and _ax_attr(focused, "AXWindow") == window:
        # The control holding keyboard focus is where typing goes: listed past the cap when the walk was cut.
        collect(focused, _ax_attrs(focused, _AX_ELEMENT_ATTRS), window_id)
    for popup, popup_id in _popup_windows_above(window_id, pid):
        for element, attrs in _ax_walk(popup, cap=400, deadline=time.monotonic() + 0.5):
            if len(controls) >= _MAX_CONTROLS + 40:
                break
            collect(element, attrs, popup_id)
    controls.sort(key=lambda item: ((item.get("box") or [0, 0])[1], (item.get("box") or [0, 0])[0]))
    for position, control in enumerate(controls, start=1):
        control["index"] = position
    return controls, password_boxes


def _popup_windows_above(window_id: int, pid: int) -> list[tuple[Any, int]]:
    """Open menus and other windows of the front app stacked above its focused window (an open popup list
    is its own window), topmost first, at most three."""
    found: list[tuple[Any, int]] = []
    try:
        above_ids = []
        for item in _window_infos():
            if item["id"] == window_id:
                break
            if item["pid"] == pid:
                above_ids.append(item["id"])
        if above_ids:
            for candidate in list(_ax_attr(_ax_app(pid), "AXWindows") or ()):
                candidate_id = _ax_window_id(candidate)
                if candidate_id in above_ids and len(found) < 3:
                    found.append((candidate, candidate_id))
        # An open menu (a pop-up button's list, a context menu) is no window: it hangs off the app.
        for menu in list(_ax_attr(_ax_app(pid), "AXChildren") or ()):
            if str(_ax_attr(menu, "AXRole") or "") == "AXMenu" and len(found) < 3:
                found.append((menu, window_id))
    except Exception:  # noqa: BLE001
        return found
    return found


def _clickable_point(wrapper: Any) -> tuple[int, int] | None:
    """The element's activation point when it declares one inside its frame, else its frame centre
    (absolute coordinates)."""
    try:
        rectangle = wrapper.element_info.rectangle
    except Exception:  # noqa: BLE001
        return None
    try:
        point = _ax_point(_ax_attr(wrapper.element, "AXActivationPoint"))
        if point is not None:
            x, y = _to_absolute(*point)
            if rectangle.left <= x <= rectangle.right and rectangle.top <= y <= rectangle.bottom:
                return x, y
    except Exception:  # noqa: BLE001
        pass
    return int((rectangle.left + rectangle.right) / 2), int((rectangle.top + rectangle.bottom) / 2)


def _press_control(wrapper: Any) -> bool:
    """Press a bound control through Accessibility (AXPress) when it offers that action. True when pressed."""
    try:
        return "AXPress" in _ax_actions(wrapper.element) and bool(_ax_action(wrapper.element, "AXPress"))
    except Exception:  # noqa: BLE001 - the caller falls back to the physical click
        return False


def _ensure_control_clickable(wrapper: Any, point: tuple[int, int] | None) -> str:
    """Before a physical click on a bound control: bring the control's app to the front and confirm the window
    under the click point is that app's. Another window (Lumen's own approval window still closing, a dialog) can
    cover the control, and the click would land on it. Returns "" when the click may go, else a failure reason."""
    try:
        import ApplicationServices as AX

        err, pid = AX.AXUIElementGetPid(wrapper.element, None)
        pid = int(pid or 0) if not err else 0
    except Exception:  # noqa: BLE001 - unreadable owner: the click path stays as it was
        return ""
    if not pid or point is None:
        return ""
    if _bundle_id_prohibited(_bundle_id(pid)):
        return "desktop_prohibited_app_frontmost"
    x_pt, y_pt = _to_points(*point)

    def clear() -> bool:
        return _frontmost_pid() == pid and _window_pid_at(x_pt, y_pt) == pid

    if clear():
        return ""
    window = _ax_attr(wrapper.element, "AXWindow")
    if window is not None:
        _ax_set(window, "AXMinimized", False)
        _ax_action(window, "AXRaise")
        _ax_set(window, "AXMain", True)
    _ax_set(_ax_app(pid), "AXFrontmost", True)
    try:
        import AppKit

        app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app is not None:
            app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps)
    except Exception:  # noqa: BLE001
        pass
    # A window minimizing or closing over the control animates for a few hundred ms.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if clear():
            time.sleep(0.15)
            return ""
        time.sleep(0.1)
    return "desktop_target_covered"


def _window_pid_at(x_pt: float, y_pt: float) -> int:
    for item in _window_infos():
        x, y, w, h = item["bounds"]
        if x <= x_pt < x + w and y <= y_pt < y + h:
            return int(item["pid"])
    return 0


def _element_at(abs_x: int, abs_y: int, pid: int = 0) -> Any:
    """The accessibility element under an absolute point, asked of the app that owns the window there (so
    Lumen's own click-through overlays above it never answer)."""
    try:
        import ApplicationServices as AX

        x_pt, y_pt = _to_points(abs_x, abs_y)
        if not pid:
            pid = _window_pid_at(x_pt, y_pt)
        root = _ax_app(pid) if pid else _ax_system()
        err, element = AX.AXUIElementCopyElementAtPosition(root, float(x_pt), float(y_pt), None)
        return None if err else element
    except Exception:  # noqa: BLE001
        return None


def _ax_parent_chain(element: Any, limit: int = 64) -> Iterator[Any]:
    current, hops = element, 0
    while current is not None and hops < limit:
        yield current
        current, hops = _ax_attr(current, "AXParent"), hops + 1


def _exact_click_target(rel_x: int, rel_y: int) -> dict[str, Any]:
    """Snap a model-chosen point to the clickable point of the nearest actionable element under it, staying
    inside that element (deterministic binding, never reinterpretation). The raw point otherwise."""
    left, top, _, _ = _virtual_screen()
    abs_x, abs_y = left + int(rel_x), top + int(rel_y)
    resolution: dict[str, Any] = {"x": int(rel_x), "y": int(rel_y), "snap_applied": False}
    try:
        hit = _element_at(abs_x, abs_y)
        if hit is None:
            return resolution
        for element in _ax_parent_chain(hit, limit=6):
            wrapper = _AXWrapper(element)
            info = wrapper.element_info
            if info.role in {"AXWindow", "AXApplication", "AXSheet"}:
                break
            if not info.enabled or not _control_affordances(wrapper).get("affordances"):
                continue
            point = _clickable_point(wrapper)
            if point is None:
                break
            resolution.update(
                {
                    "x": point[0] - left,
                    "y": point[1] - top,
                    "snap_applied": (point[0] - left, point[1] - top) != (int(rel_x), int(rel_y)),
                    "clicked_control_type": _safe_text(info.control_type, 60),
                    "clicked_control_name": _safe_text(info.name, 120),
                    "control_id": info.runtime_id,
                }
            )
            window_id = _ax_window_id(_ax_attr(element, "AXWindow"))
            if window_id:
                resolution["top_level_handle"] = window_id
            break
        return resolution
    except Exception:  # noqa: BLE001
        return resolution


def _point_hits_element(x: int, y: int, wrapper: Any, hit_test: Any = None) -> bool:
    """Whether the hit-test at an absolute point lands on this element (or inside it)."""
    try:
        target = wrapper.element
        hit = hit_test(x, y) if hit_test is not None else _element_at(int(x), int(y))
        return any(node == target for node in _ax_parent_chain(hit, limit=80))
    except Exception:  # noqa: BLE001
        return False


def _point_within_control(wrapper: Any, x: int, y: int) -> tuple[int, int] | None:
    if not (x or y):
        return None
    try:
        left, top, _, _ = _virtual_screen()
        abs_x, abs_y = int(x) + left, int(y) + top
        rectangle = wrapper.element_info.rectangle
        if rectangle.left <= abs_x < rectangle.right and rectangle.top <= abs_y < rectangle.bottom:
            return (abs_x, abs_y)
    except Exception:  # noqa: BLE001
        return None
    return None


def _focus_within(wrapper: Any) -> bool | None:
    focused = _focused_element()
    if focused is None:
        return None
    try:
        return any(node == wrapper.element for node in _ax_parent_chain(focused, limit=80))
    except Exception:  # noqa: BLE001
        return None


def _control_type_at(rel_x: int, rel_y: int) -> str:
    left, top, _, _ = _virtual_screen()
    element = _element_at(left + int(rel_x), top + int(rel_y))
    return _AXInfo(element).control_type if element is not None else ""


# --- focus, caret, and text ------------------------------------------------------------------------------


def _typability(info: _AXInfo) -> bool | None:
    kind = info.control_type.casefold()
    if kind in _TYPABLE_TAGS:
        return True
    if kind in _NON_TEXT_CONTROL_TYPES:
        return False
    # A text range means a caret: a custom editor that is neither field nor area still takes typing.
    return True if _ax_attr(info.element, "AXSelectedTextRange") is not None else None


def _element_box(info: _AXInfo) -> list[int] | None:
    rect = info.rect_pt
    if rect is None or rect[2] <= 0 or rect[3] <= 0:
        return None
    left, top, _, _ = _virtual_screen()
    abs_left, abs_top, width, height = _box_from_pt(*rect)
    return [abs_left - left, abs_top - top, abs_left - left + width, abs_top - top + height]


def _focused_state() -> dict[str, Any]:
    try:
        focused = _focused_element()
        if focused is None:
            return {}
        info = _AXInfo(focused)
        control_type = info.control_type
        password = info.is_password
        box = _element_box(info)
        return {
            "control_type": control_type,
            "class_name": _safe_text(info.class_name, 80),
            "name": _safe_text(info.name, 180),
            "value": _control_value(_AXWrapper(focused, info._attrs), password=password),
            "password": password,
            "typable": control_type.casefold() in _TYPABLE_TAGS,
            "typability": _typability(info),
            "document": control_type.casefold() == "document",
            "runtime_id": info.runtime_id,
            "center": [int((box[0] + box[2]) / 2), int((box[1] + box[3]) / 2)] if box else None,
            "box": box,
        }
    except Exception:  # noqa: BLE001
        return {}


def _focus_identity() -> tuple[Any, ...] | None:
    focused = _focused_element()
    return (_IDS.id_of(focused),) if focused is not None else None


def _utf16_to_index(text: str, units: int) -> int:
    """A UTF-16 offset (what Accessibility ranges count) as an index into a Python string."""
    if units <= 0:
        return 0
    count = 0
    for index, char in enumerate(text):
        if count >= units:
            return index
        count += 2 if ord(char) > 0xFFFF else 1
    return len(text)


def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _focused_text_and_selection() -> tuple[Any, str, int, int] | None:
    """The focused text element, its whole value, and the selection as Python string indices; None for a
    password field, an element without a text value, or no selection."""
    focused = _focused_element()
    if focused is None:
        return None
    info = _AXInfo(focused)
    if info.is_password:
        return None
    value = info.value
    if not isinstance(value, str):
        return None
    selection = _ax_range(_ax_attr(focused, "AXSelectedTextRange"))
    if selection is None:
        return None
    start = _utf16_to_index(value, selection[0])
    end = _utf16_to_index(value, selection[0] + selection[1])
    return focused, value, start, end


_LINE_BREAKS = re.compile(r"\r\n|\r|\n| | ")


def _line_bounds(text: str, index: int) -> tuple[int, int]:
    """The [start, end) of the line holding index, without its line break."""
    start = 0
    for match in _LINE_BREAKS.finditer(text):
        if match.end() <= index:
            start = match.end()
        elif match.start() >= index:
            return start, match.start()
    return start, len(text)


def _caret_state() -> dict[str, Any]:
    try:
        found = _focused_text_and_selection()
        if found is None:
            return {}
        focused, value, start, end = found
        line_start, line_end = _line_bounds(value, start)
        selected = value[start:end]
        return {
            "line_text": _safe_text(value[line_start:line_end], 200),
            "column": start - line_start,
            "selection_length": len(selected),
            "selected_text": selected[:200],
            "document": _AXInfo(focused).control_type == "Document",
        }
    except Exception:  # noqa: BLE001
        return {}


_EDIT_UNIT_CAP = 2000


def _caret_paragraph() -> dict[str, Any]:
    try:
        found = _focused_text_and_selection()
        if found is None:
            return {}
        _, value, start, end = found
        line_start, line_end = _line_bounds(value, start)
        text = value[line_start:line_end]
        selected = value[start:end]
        if len(text) >= _EDIT_UNIT_CAP or len(selected) >= _EDIT_UNIT_CAP:
            return {}
        return {"text": text, "column": start - line_start, "selected": selected}
    except Exception:  # noqa: BLE001
        return {}


def _inside_web_area(element: Any) -> bool:
    """Whether an element is web content (it or an ancestor is an AXWebArea): the page's own sensitivity
    marks live in the browser, so its text is never read out here. Unknown counts as web content."""
    try:
        return any(str(_ax_attr(node, "AXRole") or "") == "AXWebArea" for node in _ax_parent_chain(element, limit=64))
    except Exception:  # noqa: BLE001
        return True


_FOCUSED_TEXT_READ_CAP = 200_000


def _focused_text_reading() -> dict[str, Any]:
    """The focused editable text control's whole text and the caret's line. Never a password field, web
    content, or a read-only text."""
    try:
        focused = _focused_element()
        if focused is None:
            return {}
        info = _AXInfo(focused)
        if info.is_password or info.control_type not in {"Edit", "Document"}:
            return {}
        if _inside_web_area(focused) or not _ax_settable(focused, "AXValue"):
            return {}
        value = info.value
        if not isinstance(value, str) or not value:
            return {}
        value = value[:_FOCUSED_TEXT_READ_CAP]
        caret_line = None
        selection = _ax_range(_ax_attr(focused, "AXSelectedTextRange"))
        if selection is not None:
            before = value[: _utf16_to_index(value, selection[0])]
            caret_line = len(_LINE_BREAKS.findall(before)) + 1
        return {"runtime_id": info.runtime_id, "text": value, "caret_line": caret_line}
    except Exception:  # noqa: BLE001
        return {}


def _focused_text_lines(controls: list[dict[str, Any]], sensitive_runtime_ids: set[tuple[int, ...]]) -> dict[str, Any]:
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
        (item for item in candidates if (_parse_selector(str(item.get("selector") or "")) or (0, ()))[1] == runtime_id),
        None,
    )
    if control is None:
        return {}
    text = _LINE_BREAKS.sub("\n", str(reading.get("text") or ""))
    return text_line_window(text, caret_line=reading.get("caret_line"), control=control.get("index"))


def _readable_active_values() -> list[str]:
    try:
        controls, _ = _active_controls()
    except Exception:  # noqa: BLE001
        return []
    return [
        str(item.get("value") or "")
        for item in controls
        if not item.get("password") and str(item.get("tag") or "").casefold() in _TYPABLE_TAGS and str(item.get("value") or "")
    ]


def _restore_caret_at_last_click() -> bool:
    """Focus the text control under the last click again (asked of the targeted window's app, so Lumen's own
    overlays above the point never answer). Side-effect free, unlike a second click."""
    with _CONTROL_LOCK:
        point = _LAST_CLICK_POINT
        target = int(_LAST_TARGET_WINDOW)
    if not point or not target:
        return False
    element = _element_at(point[0], point[1], _window_pid(target))
    for node in _ax_parent_chain(element, limit=6):
        info = _AXInfo(node)
        if info.control_type.casefold() in {"edit", "document", "combobox"} and info.enabled:
            return _ax_set(node, "AXFocused", True)
    return False


def _focus_text_control_within_focus() -> bool:
    """Move keyboard focus into the one text control inside the focused element (else the front window's
    single text control). The outcome is recorded for diagnosis."""

    def text_controls(root: Any) -> list[Any]:
        return [
            element
            for element, attrs in _ax_walk(root, cap=600, deadline=time.monotonic() + 0.6)
            if _AX_ROLE_TO_TYPE.get(str(attrs.get("AXRole") or "")) in {"Edit", "Document"}
            and attrs.get("AXEnabled") is not False
        ]

    focused = _focused_element()
    if focused is None:
        _LAST_FOCUS_REPAIR["result"] = "no_focused_element"
        return False
    try:
        inner = text_controls(focused)
        source = "inside_focus"
        if not inner:
            window, _, _ = _active_window_element()
            inner = text_controls(window) if window is not None else []
            source = "foreground_window"
        if len(inner) != 1:
            _LAST_FOCUS_REPAIR["result"] = f"{source}:{len(inner)}_text_controls"
            return False
        _ax_set(inner[0], "AXFocused", True)
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


# How far (capture pixels) the occurrence Accessibility selects may be from the model's point.
_AX_SELECT_REACH = 400


def _ax_bounds_for_range(element: Any, location: int, length: int) -> tuple[float, float, float, float] | None:
    try:
        import ApplicationServices as AX

        err, value = AX.AXUIElementCopyParameterizedAttributeValue(
            element, "AXBoundsForRange", _ax_make_range(location, length), None
        )
        if err or value is None:
            return None
        ok, rect = AX.AXValueGetValue(value, AX.kAXValueCGRectType, None)
        if not ok or rect.size.width <= 0 or rect.size.height <= 0:
            return None
        return (float(rect.origin.x), float(rect.origin.y), float(rect.size.width), float(rect.size.height))
    except Exception:  # noqa: BLE001
        return None


def _select_text_by_uia(text: str, rel_x: int, rel_y: int) -> dict[str, Any] | None:
    """Select exactly `text` in the editable text element under a capture point through Accessibility (the
    occurrence nearest the point), then read the selection back. None when no such element there holds it:
    the caller finds the text on screen instead. Never a password field."""
    wanted = " ".join(str(text or "").split())
    if not wanted:
        return None
    try:
        left, top, _, _ = _virtual_screen()
        abs_x, abs_y = left + int(rel_x), top + int(rel_y)
        target = None
        for node in _ax_parent_chain(_element_at(abs_x, abs_y), limit=4):
            info = _AXInfo(node)
            if info.is_password:
                return None
            if isinstance(info.value, str) and _ax_settable(node, "AXSelectedTextRange"):
                target = node
                break
        if target is None:
            return None
        value = str(_ax_attr(target, "AXValue") or "")
        best: tuple[float, int] | None = None
        for match in re.finditer(re.escape(wanted), value):
            bounds = _ax_bounds_for_range(target, _utf16_length(value[: match.start()]), _utf16_length(match.group(0)))
            if bounds is None:
                continue
            b_left, b_top, b_width, b_height = _box_from_pt(*bounds)
            near_x = min(max(abs_x, b_left), b_left + b_width)
            near_y = min(max(abs_y, b_top), b_top + b_height)
            distance = ((abs_x - near_x) ** 2 + (abs_y - near_y) ** 2) ** 0.5
            if best is None or distance < best[0]:
                best = (distance, match.start())
        if best is None or best[0] > _AX_SELECT_REACH:
            return None
        _ax_set(target, "AXFocused", True)
        _ax_set(target, "AXSelectedTextRange", _ax_make_range(_utf16_length(value[: best[1]]), _utf16_length(wanted)))
        selected = str(_ax_attr(target, "AXSelectedText") or "")
        matches = " ".join(selected.split()) == wanted
        return {
            "ok": matches,
            "action": "select_text",
            "mode": "text_pattern",
            "selected_text": selected[:300],
            **({} if matches else {"failure_reason": "select_text_mismatch"}),
            "desktop_surface": True,
        }
    except Exception:  # noqa: BLE001
        return None


def _double_click_seconds() -> float:
    try:
        import AppKit

        return max(0.2, min(1.0, float(AppKit.NSEvent.doubleClickInterval())))
    except Exception:  # noqa: BLE001
        return 0.5


def _explorer_folder_view(window_id: Any, title: str) -> dict[str, Any]:
    """The folder a Finder window shows (its AXDocument URL) and its items' real file names."""
    del title
    try:
        from urllib.parse import urlparse
        from urllib.request import url2pathname

        handle = _parse_window_id(window_id)
        window = _ax_window_for(handle) if handle else None
        document = str(_ax_attr(window, "AXDocument") or "")
        parsed = urlparse(document)
        if parsed.scheme != "file":
            return {}
        path = os.path.normpath(url2pathname(parsed.path))
        if not os.path.isdir(path):
            return {}
        names = sorted((name for name in os.listdir(path) if not name.startswith(".")), key=str.casefold)
        return {"path": path, "items": names[:60], "item_count": len(names)}
    except Exception:  # noqa: BLE001
        return {}


# --- input -----------------------------------------------------------------------------------------------

# macOS virtual key codes (ANSI layout positions, Events.h).
_KEYCODES = {
    "A": 0, "S": 1, "D": 2, "F": 3, "H": 4, "G": 5, "Z": 6, "X": 7, "C": 8, "V": 9, "B": 11, "Q": 12,
    "W": 13, "E": 14, "R": 15, "Y": 16, "T": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23,
    "=": 24, "+": 24, "9": 25, "7": 26, "-": 27, "_": 27, "8": 28, "0": 29, "]": 30, "}": 30, "O": 31,
    "U": 32, "[": 33, "{": 33, "I": 34, "P": 35, "ENTER": 36, "L": 37, "J": 38, "'": 39, '"': 39, "K": 40,
    ";": 41, ":": 41, "\\": 42, "|": 42, ",": 43, "<": 43, "/": 44, "?": 44, "N": 45, "M": 46, ".": 47,
    ">": 47, "TAB": 48, "SPACE": 49, "`": 50, "~": 50, "BACKSPACE": 51, "ESC": 53,
    "CAPSLOCK": 57, "VOLUMEUP": 72, "VOLUMEDOWN": 73, "VOLUMEMUTE": 74, "HELP": 114, "INSERT": 114,
    "HOME": 115, "PAGEUP": 116, "DELETE": 117, "END": 119, "PAGEDOWN": 121, "ARROWLEFT": 123,
    "ARROWRIGHT": 124, "ARROWDOWN": 125, "ARROWUP": 126, "CLEAR": 71,
    "DECIMAL": 65, "MULTIPLY": 67, "ADD": 69, "DIVIDE": 75, "SUBTRACT": 78,
    "NUMPAD0": 82, "NUMPAD1": 83, "NUMPAD2": 84, "NUMPAD3": 85, "NUMPAD4": 86, "NUMPAD5": 87,
    "NUMPAD6": 88, "NUMPAD7": 89, "NUMPAD8": 91, "NUMPAD9": 92,
}
_FUNCTION_KEYCODES = {
    1: 122, 2: 120, 3: 99, 4: 118, 5: 96, 6: 97, 7: 98, 8: 100, 9: 101, 10: 109, 11: 103, 12: 111,
    13: 105, 14: 107, 15: 113, 16: 106, 17: 64, 18: 79, 19: 80, 20: 90,
}
# Modifier name -> (key code, CGEvent flag). WIN/META/SUPER/CMD are Command (the Windows key's place;
# _normalize_key_parts already reads CMD and SUPER as WIN).
_MODIFIERS = {
    "CMD": (55, 0x100000),
    "COMMAND": (55, 0x100000),
    "WIN": (55, 0x100000),
    "WINDOWS": (55, 0x100000),
    "META": (55, 0x100000),
    "SHIFT": (56, 0x20000),
    "ALT": (58, 0x80000),
    "OPTION": (58, 0x80000),
    "OPT": (58, 0x80000),
    "CTRL": (59, 0x40000),
    "CONTROL": (59, 0x40000),
    "FN": (63, 0x800000),
}
_CANONICAL_MODIFIER = {55: "CMD", 56: "SHIFT", 58: "ALT", 59: "CTRL", 63: "FN"}
# Shortcuts that quit the app, end the session, lock the screen, force-quit, or open the launcher stay
# blocked (the Windows body's Alt+F4, Ctrl+Alt+Delete, Win+L, Win+R). A chord is blocked only when its
# modifier set EQUALS the listed one, so CMD+CTRL+SPACE (emoji picker) and CTRL+SPACE (autocomplete, column
# select; input-source switching is off by default for most users) are allowed.
_BLOCKED_SHORTCUTS = (
    ({"CMD"}, "Q"),
    ({"CMD", "ALT"}, "ESC"),
    ({"CMD", "CTRL"}, "Q"),
    ({"CMD", "SHIFT"}, "Q"),
    ({"CMD"}, "SPACE"),
    ({"CMD", "ALT"}, "SPACE"),
)
_MOUSE_BUTTONS = {"left", "right", "middle"}
# The model names gesture modifiers the way the browser body does; meta is Command.
_GESTURE_MODIFIER_KEYS = {"shift": "SHIFT", "ctrl": "CTRL", "alt": "ALT", "meta": "CMD"}


def _key_code(key_name: str) -> int:
    name = "ESC" if key_name.upper() == "ESCAPE" else key_name
    if name in _KEYCODES:
        return _KEYCODES[name]
    match = re.fullmatch(r"F([1-9]|1[0-9]|20)", name)
    if match:
        return _FUNCTION_KEYCODES[int(match.group(1))]
    if len(name) == 1 and name.upper() in _KEYCODES:
        return _KEYCODES[name.upper()]
    raise ValueError(f"Unsupported keyboard key: {key_name[:24]}")


def _check_key(value: str) -> tuple[list[str], int]:
    """The modifiers and key code a key or chord presses; ValueError (nothing pressed) when it names no
    key, a modifier that is not one, or a blocked system shortcut."""
    parts = _normalize_key_parts(value)
    if not parts:
        raise ValueError("A key action must name a key.")
    modifiers = [part.upper() for part in parts[:-1]]
    if any(item not in _MODIFIERS for item in modifiers):
        raise ValueError("Unsupported keyboard modifier.")
    held = [_CANONICAL_MODIFIER[_MODIFIERS[item][0]] for item in modifiers]
    key = parts[-1]
    key_upper = "ESC" if key.upper() in {"ESC", "ESCAPE"} else key.upper()
    for blocked_modifiers, blocked_key in _BLOCKED_SHORTCUTS:
        if key_upper == blocked_key and blocked_modifiers == set(held):
            raise ValueError("That system shortcut is not available to autonomous computer use.")
    return list(dict.fromkeys(held)), _key_code(key)


def _post(event: Any) -> None:
    import Quartz

    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def _key_event(code: int, down: bool, flags: int) -> Any:
    import Quartz

    event = Quartz.CGEventCreateKeyboardEvent(None, int(code), bool(down))
    Quartz.CGEventSetFlags(event, int(flags))
    return event


def _key_steps(value: str, *, repeat: int = 1, hold_ms: int = 0) -> Iterator[float]:
    """Press one key or chord, optionally repeated or held; modifiers go down first and always come up."""
    modifiers, key_code = _check_key(value)
    presses = max(1, min(200, int(repeat or 1)))
    hold_seconds = max(0, min(5000, int(hold_ms or 0))) / 1000
    flags = 0
    pressed: list[str] = []
    try:
        for name in modifiers:
            code, flag = _MODIFIERS[name]
            flags |= flag
            _post(_key_event(code, True, flags))
            pressed.append(name)
        for index in range(presses):
            if index:
                yield 0.03
            _post(_key_event(key_code, True, flags))
            try:
                if hold_seconds:
                    yield hold_seconds
            finally:
                _post(_key_event(key_code, False, flags))
    finally:
        for name in reversed(pressed):
            code, flag = _MODIFIERS[name]
            flags &= ~flag
            try:
                _post(_key_event(code, False, flags))
            except Exception:  # noqa: BLE001
                pass


def _send_key(value: str, *, repeat: int = 1, hold_ms: int = 0) -> None:
    for pause in _key_steps(value, repeat=repeat, hold_ms=hold_ms):
        time.sleep(pause)


async def _send_key_async(value: str, *, repeat: int = 1, hold_ms: int = 0) -> None:
    steps = _key_steps(value, repeat=repeat, hold_ms=hold_ms)
    try:
        for pause in steps:
            await asyncio.sleep(pause)
    finally:
        steps.close()


def _send_unicode(text: str) -> None:
    """Type text one character at a time at human speed (a surrogate pair travels in one event), with no
    modifier flags so a held key can never turn a letter into a shortcut."""
    import Quartz

    characters = list(str(text))
    for index, char in enumerate(characters):
        for down in (True, False):
            event = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
            Quartz.CGEventSetFlags(event, 0)
            Quartz.CGEventKeyboardSetUnicodeString(event, _utf16_length(char), char)
            _post(event)
        if index + 1 < len(characters):
            time.sleep(_KEYSTROKE_INTERVAL_SECONDS)


def _gesture_modifier_names(modifiers: Any) -> list[str]:
    names: list[str] = []
    for item in modifiers or ():
        name = _GESTURE_MODIFIER_KEYS.get(str(item or "").strip().lower())
        if name is None:
            raise ValueError("Unsupported gesture modifier.")
        if name not in names:
            names.append(name)
    return names


# Modifier flags held by the current pointer gesture: posted mouse and wheel events carry them, as a
# hardware event would while the keys are down.
_HELD_FLAGS: dict[str, int] = {"flags": 0}


@contextmanager
def _held_modifiers(modifiers: Any) -> Iterator[None]:
    """Hold modifier keys for exactly one pointer gesture; every key pressed here comes back up."""
    names = _gesture_modifier_names(modifiers)
    if not names:
        yield
        return
    pressed: list[str] = []
    flags = 0
    try:
        for name in names:
            code, flag = _MODIFIERS[name]
            flags |= flag
            _post(_key_event(code, True, flags))
            pressed.append(name)
        _HELD_FLAGS["flags"] = flags
        time.sleep(0.03)
        yield
    finally:
        _HELD_FLAGS["flags"] = 0
        for name in reversed(pressed):
            code, flag = _MODIFIERS[name]
            flags &= ~flag
            try:
                _post(_key_event(code, False, flags))
            except Exception:  # noqa: BLE001
                pass


def _mouse_event(kind: str, button: str, abs_x: int, abs_y: int, *, clicks: int = 1) -> Any:
    import Quartz

    types = {
        ("down", "left"): Quartz.kCGEventLeftMouseDown,
        ("up", "left"): Quartz.kCGEventLeftMouseUp,
        ("drag", "left"): Quartz.kCGEventLeftMouseDragged,
        ("down", "right"): Quartz.kCGEventRightMouseDown,
        ("up", "right"): Quartz.kCGEventRightMouseUp,
        ("drag", "right"): Quartz.kCGEventRightMouseDragged,
        ("down", "middle"): Quartz.kCGEventOtherMouseDown,
        ("up", "middle"): Quartz.kCGEventOtherMouseUp,
        ("drag", "middle"): Quartz.kCGEventOtherMouseDragged,
        ("move", "left"): Quartz.kCGEventMouseMoved,
    }
    buttons = {"left": Quartz.kCGMouseButtonLeft, "right": Quartz.kCGMouseButtonRight, "middle": Quartz.kCGMouseButtonCenter}
    x_pt, y_pt = _to_points(abs_x, abs_y)
    event = Quartz.CGEventCreateMouseEvent(None, types[(kind, button)], Quartz.CGPointMake(x_pt, y_pt), buttons[button])
    if kind in {"down", "up"}:
        Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState, int(clicks))
    if _HELD_FLAGS["flags"]:
        Quartz.CGEventSetFlags(event, _HELD_FLAGS["flags"])
    return event


def _button_name(button: str) -> str:
    normalized = str(button or "left").strip().lower()
    if normalized not in _MOUSE_BUTTONS:
        raise ValueError("Unsupported mouse button.")
    return normalized


def _set_cursor(abs_x: int, abs_y: int) -> None:
    _post(_mouse_event("move", "left", int(abs_x), int(abs_y)))


def _mouse_move(x: int, y: int) -> None:
    """Move the pointer without pressing anything (hover): a real mouse-moved event, so hover effects fire."""
    _set_cursor(*_absolute_point(x, y))


def _mouse_click(
    x: int,
    y: int,
    *,
    button: str = "left",
    clicks: int = 1,
    hold_ms: int = 0,
    modifiers: Any = (),
) -> None:
    """Click at a point with any button, single/double/triple; hold_ms keeps the button down and modifiers
    are held for the whole gesture."""
    global _LAST_CLICK_POINT
    name = _button_name(button)
    hold_seconds = max(0, min(5000, int(hold_ms or 0))) / 1000
    point = _absolute_point(x, y)
    with _CONTROL_LOCK:
        _LAST_CLICK_POINT = point
    _set_cursor(*point)
    time.sleep(0.02)
    with _held_modifiers(modifiers):
        for index in range(max(1, min(3, int(clicks or 1)))):
            if index:
                time.sleep(0.05)
            # The click state counts up within one gesture, so apps see one double or triple click.
            _post(_mouse_event("down", name, *point, clicks=index + 1))
            try:
                if hold_seconds:
                    time.sleep(hold_seconds)
            finally:
                _post(_mouse_event("up", name, *point, clicks=index + 1))


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
    """Press, move in visible dragged steps through any waypoints, and release (apps ignore instant jumps).
    The button always comes back up, where the pointer is."""
    import math

    name = _button_name(button)
    route = [_absolute_point(x, y)]
    for point in list(path or ())[:40]:
        route.append(_absolute_point(int(point[0]), int(point[1])))
    route.append(_absolute_point(to_x, to_y))
    hold_seconds = max(0, min(5000, int(hold_ms or 0))) / 1000
    _set_cursor(*route[0])
    time.sleep(0.05)
    current = route[0]
    with _held_modifiers(modifiers):
        _post(_mouse_event("down", name, *route[0]))
        try:
            if hold_seconds:
                time.sleep(hold_seconds)
            for leg in range(1, len(route)):
                (start_x, start_y), (end_x, end_y) = route[leg - 1], route[leg]
                steps = 24 if len(route) == 2 else max(4, min(40, round(math.hypot(end_x - start_x, end_y - start_y) / 12)))
                for step in range(1, steps + 1):
                    current = (int(start_x + (end_x - start_x) * step / steps), int(start_y + (end_y - start_y) * step / steps))
                    _post(_mouse_event("drag", name, *current))
                    time.sleep(0.012)
        finally:
            _post(_mouse_event("up", name, *current))


def _wheel_lines(dx: int, dy: int) -> tuple[int, int]:
    """Windows wheel deltas (120 a notch, positive dy scrolls down, positive dx right) as macOS scroll lines
    (vertical, horizontal; positive scrolls up or left). A notch is three lines here as there."""

    def lines(delta: int) -> int:
        value = round(int(max(-2400, min(2400, int(delta)))) / 40)
        return int(value) or (1 if delta > 0 else -1 if delta < 0 else 0)

    return -lines(int(dy or 0)), -lines(int(dx or 0))


def _wheel(dx: int, dy: int) -> None:
    """One wheel gesture at the pointer."""
    import Quartz

    vertical, horizontal = _wheel_lines(dx, dy)
    event = Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 2, vertical, horizontal)
    if _HELD_FLAGS["flags"]:
        Quartz.CGEventSetFlags(event, _HELD_FLAGS["flags"])
    _post(event)


# --- clipboard -------------------------------------------------------------------------------------------

# Pasteboard markers (nspasteboard.org) apps set to keep a copy out of clipboard managers and history; a
# password manager's copy carries the concealed one. Every text this body writes carries them too.
_CONCEALED_TYPE = "org.nspasteboard.ConcealedType"
_TRANSIENT_TYPE = "org.nspasteboard.TransientType"
_AUTO_GENERATED_TYPE = "org.nspasteboard.AutoGeneratedType"
_PRIVATE_TYPES = (_CONCEALED_TYPE, _TRANSIENT_TYPE)
_PLAIN_TEXT_TYPE = "public.utf8-plain-text"
_TEXT_TYPES = frozenset({"public.utf8-plain-text", "public.utf16-plain-text", "NSStringPboardType", "public.plain-text"})
# Pasteboard content that is not text (an image, files): never wiped for a short value.
_NON_TEXT_TYPE_PREFIXES = ("public.png", "public.tiff", "public.jpeg", "public.heic", "public.file-url", "NSFilenamesPboardType", "com.adobe.pdf", "public.image")
_CREDENTIAL_STORE_BUNDLES = frozenset(
    {
        "com.1password.1password",
        "com.agilebits.onepassword7",
        "com.bitwarden.desktop",
        "org.keepassxc.keepassxc",
        "com.apple.Passwords",
        "com.apple.keychainaccess",
        "com.lastpass.LastPass",
        "com.nordsec.nordpass",
        "me.proton.pass.electron",
        "com.enpass.Enpass-Desktop",
    }
)


def _pasteboard() -> Any:
    import AppKit

    return AppKit.NSPasteboard.generalPasteboard()


def _pasteboard_types() -> list[str]:
    return [str(item) for item in (_pasteboard().types() or ())]


def _clipboard_sequence() -> int | None:
    """The pasteboard's change count: it moves every time any app writes the clipboard."""
    try:
        return int(_pasteboard().changeCount())
    except Exception:  # noqa: BLE001
        return None


def _read_clipboard_text() -> dict[str, Any]:
    try:
        board = _pasteboard()
        sequence = int(board.changeCount())
        types = [str(item) for item in (board.types() or ())]
        private = any(marker in types for marker in _PRIVATE_TYPES)
        text = board.stringForType_(_PLAIN_TEXT_TYPE)
        if text is None:
            return {"failure_reason": "clipboard_holds_no_text", "private": private, "sequence": sequence}
        return {"text": str(text), "private": private, "sequence": sequence}
    except Exception:  # noqa: BLE001
        return {"failure_reason": "clipboard_unavailable"}


def _secure_input_enabled() -> bool:
    """Whether a password field anywhere holds secure keyboard input right now."""
    try:
        import ctypes

        carbon = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Carbon.framework/Carbon")
        carbon.IsSecureEventInputEnabled.restype = ctypes.c_bool
        return bool(carbon.IsSecureEventInputEnabled())
    except Exception:  # noqa: BLE001
        return False


def _clipboard_owner_process() -> dict[str, Any] | None:
    """macOS does not record which app wrote the pasteboard: the front app is taken as the writer (the
    shared wrapper counts only a change made while it was in front). A concealed marker, a password
    manager in front, or secure input on makes the copy a credential store's."""
    pid = _frontmost_pid()
    if not pid:
        return None
    bundle = _bundle_id(pid)
    try:
        types = _pasteboard_types()
    except Exception:  # noqa: BLE001
        types = []
    credential = bundle in _CREDENTIAL_STORE_BUNDLES or _CONCEALED_TYPE in types or _secure_input_enabled()
    return {"pid": pid, "exe": bundle, "credential_store": credential}


def _write_clipboard_text(value: str) -> None:
    """Write text kept to this Mac (no Universal Clipboard) and out of clipboard managers and history."""
    import AppKit

    board = _pasteboard()
    try:
        board.prepareForNewContentsWithOptions_(AppKit.NSPasteboardContentsCurrentHostOnly)
    except Exception:  # noqa: BLE001 - older systems: clearContents alone
        pass
    board.clearContents()
    board.declareTypes_owner_([_PLAIN_TEXT_TYPE, _CONCEALED_TYPE, _TRANSIENT_TYPE, _AUTO_GENERATED_TYPE], None)
    if not board.setString_forType_(str(value), _PLAIN_TEXT_TYPE):
        raise OSError("The clipboard did not take the text.")
    for marker in (_CONCEALED_TYPE, _TRANSIENT_TYPE, _AUTO_GENERATED_TYPE):
        board.setData_forType_(AppKit.NSData.data(), marker)


def _clipboard_holds_text_or_nothing() -> bool:
    try:
        types = _pasteboard_types()
    except Exception:  # noqa: BLE001
        return False
    if not types:
        return True
    if any(item.startswith(_NON_TEXT_TYPE_PREFIXES) for item in types):
        return False
    return any(item in _TEXT_TYPES for item in types)


class _ClipboardHold:
    """The user's clipboard text while text this body pasted may still be read by the target app (the
    Windows body's _ClipboardHold): it goes back only when the read-back saw the paste land; otherwise the
    clipboard is left empty."""

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
            if previous:
                _write_clipboard_text(previous)
            else:
                _pasteboard().clearContents()
        except Exception:  # noqa: BLE001 - best effort
            pass


def _deliver_text_via_clipboard(text: str, hold: _ClipboardHold, *, text_clipboard_only: bool = False) -> bool:
    if text_clipboard_only and not _clipboard_holds_text_or_nothing():
        return False
    previous: str | None = None
    try:
        current = _pasteboard().stringForType_(_PLAIN_TEXT_TYPE)
        previous = str(current) if current is not None else None
    except Exception:  # noqa: BLE001
        if text_clipboard_only:
            return False
    try:
        _write_clipboard_text(str(text))
    except Exception:
        try:
            if previous:
                _write_clipboard_text(previous)
        except Exception:  # noqa: BLE001
            pass
        raise
    hold.hold(previous)
    _send_key(_PASTE_KEY)
    time.sleep(0.35)
    return True


def _deliver_text(
    text: str,
    *,
    prefer_paste: bool = False,
    allow_paste: bool = True,
    clipboard: _ClipboardHold | None = None,
) -> str:
    """Put text into the focused control as a human would (the Windows body's _deliver_text policy)."""
    value = str(text)
    long_value = len(value) >= _PASTE_THRESHOLD_CHARS
    if allow_paste and clipboard is not None and value and (prefer_paste or long_value):
        try:
            if _deliver_text_via_clipboard(value, clipboard, text_clipboard_only=not long_value):
                return "paste"
        except Exception:  # noqa: BLE001
            if clipboard.pending:
                return "paste_unconfirmed"
    _send_unicode(value)
    return "keystrokes"


# --- applications and files ------------------------------------------------------------------------------

_APPLICATION_ROOTS = (
    Path("/Applications"),
    Path("/Applications/Utilities"),
    Path("/System/Applications"),
    Path("/System/Applications/Utilities"),
    Path.home() / "Applications",
)
# Shells, script runners, and system administration tools stay out of the launcher (the Windows body's
# terminals, PowerShell, and registry editor).
_PROHIBITED_APP_NAMES = frozenset(
    {
        "terminal", "iterm", "iterm2", "warp", "kitty", "alacritty", "wezterm", "ghostty", "hyper",
        "script editor", "automator", "shortcuts", "keychain access", "passwords", "disk utility",
        "migration assistant", "directory utility", "boot camp assistant", "system information",
        "console", "activity monitor", "python launcher", "idle",
        # System and security settings are the user's to change (the Privacy & Security grants Lumen itself
        # needs included): neither name is launchable, and System Settings windows are hidden and never raised.
        "system settings", "system preferences",
    }
)
# The same apps by bundle id (the rename from System Preferences kept com.apple.systempreferences), so a
# renamed or localized copy is still refused. Also what a window's owning app is checked against.
_PROHIBITED_BUNDLE_IDS = frozenset({"com.apple.systempreferences", "com.apple.settings"})
# URL schemes and bundle suffixes that open a settings pane without naming the app.
_SETTINGS_URL_PREFIXES = ("x-apple.systempreferences:", "x-apple.systemsettings:", "prefs:")
_SETTINGS_PANE_SUFFIXES = (".prefpane", ".systempreferences")


def _bundle_id_prohibited(bundle_id: str) -> bool:
    return str(bundle_id or "").casefold() in _PROHIBITED_BUNDLE_IDS


def _settings_target(target: Any) -> bool:
    """Whether an open target (path or URL text) is a settings pane or the Settings app."""
    text = str(target or "").strip().casefold()
    if text.startswith(_SETTINGS_URL_PREFIXES) or text.rstrip("/").endswith(_SETTINGS_PANE_SUFFIXES):
        return True
    return text.rstrip("/").rsplit("/", 1)[-1] in {"system settings.app", "system preferences.app"}


def _app_name_allowed(name: str) -> bool:
    normalized = _safe_text(name, 100).casefold()
    if not normalized or normalized in _PROHIBITED_APP_NAMES or normalized.startswith("uninstall"):
        return False
    return not re.search(
        r"^(?:idle|python)(?:\s|$|\d)|(?:^|\s)terminal(?:\s|$)|^system (?:settings|preferences)(?:\s|$)", normalized
    )


def _app_candidates() -> list[tuple[str, str, str]]:
    candidates: list[tuple[str, str, str]] = []
    for root in _APPLICATION_ROOTS:
        if not root.is_dir():
            continue
        for bundle in sorted(root.glob("*.app"), key=lambda item: item.stem.casefold()):
            name = bundle.stem
            try:
                with open(bundle / "Contents" / "Info.plist", "rb") as handle:
                    info = plistlib.load(handle)
                if info.get("LSUIElement") in (True, "1", 1) or info.get("LSBackgroundOnly") in (True, "1", 1):
                    continue
                if _bundle_id_prohibited(str(info.get("CFBundleIdentifier") or "")):
                    continue
                name = str(info.get("CFBundleDisplayName") or info.get("CFBundleName") or bundle.stem)
            except Exception:  # noqa: BLE001 - the bundle's own name
                pass
            name = _safe_text(name, 100)
            if _app_name_allowed(name) and _app_name_allowed(bundle.stem):
                candidates.append((name, "bundle", str(bundle)))
    return candidates


def _application_inventory() -> tuple[list[dict[str, str]], dict[str, dict[str, str]]]:
    global _APP_CACHE
    now = time.monotonic()
    if _APP_CACHE is not None and now - _APP_CACHE[0] < 300:
        return _APP_CACHE[1], _APP_CACHE[2]
    candidates = _app_candidates()
    candidates.sort(key=lambda item: (item[0].casefold(), item[2].casefold()))
    rows: list[dict[str, str]] = []
    index: dict[str, dict[str, str]] = {}
    seen: set[str] = set()
    for name, kind, target in candidates:
        if name.casefold() in seen:
            continue
        app_id = "app:" + hashlib.sha256(f"{kind}:{target}".encode("utf-8")).hexdigest()[:16]
        seen.add(name.casefold())
        index[app_id] = {"kind": kind, "target": target, "name": name}
        rows.append({"app_id": app_id, "name": name})
        if len(rows) >= 160:
            break
    _APP_CACHE = (now, rows, index)
    return rows, index


def _launch_inventory_target(target: dict[str, str]) -> bool:
    if str(target.get("kind") or "") != "bundle":
        return False
    bundle = Path(str(target.get("target") or ""))
    if bundle.suffix != ".app" or not bundle.is_dir() or _settings_target(bundle):
        return False
    try:
        with open(bundle / "Contents" / "Info.plist", "rb") as handle:
            if _bundle_id_prohibited(str(plistlib.load(handle).get("CFBundleIdentifier") or "")):
                return False
    except Exception:  # noqa: BLE001 - no readable plist: the name check ran at enumeration
        pass
    # The exact enumerated bundle path as one argument: never a shell string.
    result = subprocess.run(["/usr/bin/open", "-a", str(bundle)], capture_output=True, timeout=20, check=False)  # noqa: S603
    return result.returncode == 0


def screen_locked() -> bool:
    """The login session's own lock flag: while it is set no app window can be seen or captured."""
    try:
        import Quartz

        session = Quartz.CGSessionCopyCurrentDictionary() or {}
        return bool(session.get("CGSSessionScreenIsLocked"))
    except Exception:  # noqa: BLE001 - unknown means not known to be locked
        return False


_CHROME_BUNDLE_ID = "com.google.Chrome"


def bring_browser_to_front() -> bool:
    """Make the running Google Chrome the frontmost app, so the extension body can capture its tab. macOS does not
    let Chrome take the front back itself (the extension's window focus request is refused while another app is
    active); this process holds Accessibility. Only Chrome, only while computer use is on, never page-directed."""
    if not runtime_enabled():
        return False
    try:
        import AppKit

        apps = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(_CHROME_BUNDLE_ID)
        app = apps[0] if apps and len(apps) else None
        if app is None:
            return False
        if app.isHidden():
            app.unhide()
        _ax_set(_ax_app(int(app.processIdentifier())), "AXFrontmost", True)
        return bool(app.activateWithOptions_(AppKit.NSApplicationActivateIgnoringOtherApps))
    except Exception:  # noqa: BLE001 - best effort; the capture that follows decides
        return False


def launch_application_named(name_fragment: str) -> bool:
    """Launch an installed app by name from the enumerated inventory (exact names before partial ones).
    Used by the runtime to open its own browser body; never driven by page content."""
    fragment = str(name_fragment or "").strip().casefold()
    if not fragment or not runtime_enabled():
        return False
    try:
        _, index = _application_inventory()
    except Exception:  # noqa: BLE001
        return False
    exact = [item for item in index.values() if str(item.get("name") or "").casefold() == fragment]
    partial = [item for item in index.values() if fragment in str(item.get("name") or "").casefold()]
    for target in exact + [item for item in partial if item not in exact]:
        try:
            if _launch_inventory_target(target):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def chrome_profiles() -> list[dict[str, str]]:
    """Installed Chrome profiles from the browser's own Local State catalog (directory id + name)."""
    import json as _json

    try:
        local_state = Path.home() / "Library" / "Application Support" / "Google" / "Chrome" / "Local State"
        if not local_state.is_file():
            return []
        data = _json.loads(local_state.read_text(encoding="utf-8"))
        info_cache = (data.get("profile") or {}).get("info_cache") or {}
        profiles = [
            {"directory": str(directory), "name": _safe_text(meta.get("name"), 80) or str(directory)}
            for directory, meta in info_cache.items()
            if isinstance(meta, dict)
        ]
        profiles.sort(key=lambda profile: profile["directory"])
        return profiles
    except Exception:  # noqa: BLE001
        return []


def _open_path(target: Path) -> None:
    # A document type from the allowlist sitting in the downloads folder, opened in its default app.
    if _settings_target(target):
        raise RuntimeError("System Settings is not available to autonomous computer use.")
    subprocess.run(["/usr/bin/open", str(target)], capture_output=True, timeout=20, check=True)  # noqa: S603


# --- overlay feedback ------------------------------------------------------------------------------------


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
    """Pointer feedback for the host's overlay in global points (what Electron's screen API uses on
    macOS). Best effort: a failure never affects the action."""
    try:
        if rel_x is not None and rel_y is not None:
            left, top, _, _ = _virtual_screen()
            abs_x, abs_y = left + int(rel_x), top + int(rel_y)
        x = y = None
        if abs_x is not None and abs_y is not None:
            x_pt, y_pt = _to_points(abs_x, abs_y)
            x, y = round(x_pt), round(y_pt)
        publish_pointer_event(kind, x=x, y=y, dy=dy, label=label)
    except Exception:  # noqa: BLE001
        pass


# --- the body --------------------------------------------------------------------------------------------


def _frontmost_prohibited() -> bool:
    """Whether the app holding keyboard focus is System Settings / System Preferences (one bundle-id read)."""
    try:
        pid = _frontmost_pid()
        return bool(pid) and _bundle_id_prohibited(_bundle_id(pid))
    except Exception:  # noqa: BLE001 - an unreadable frontmost app never blocks the task
        return False


def _guarded(name: str) -> Any:
    async def method(self: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if _frontmost_prohibited():
            return {
                "ok": False,
                "action": name,
                "failure_reason": "desktop_prohibited_app_frontmost",
                "error": "System Settings in front, off limits: use focus_window or launch_app to switch",
                "message": (
                    "System Settings is in front and is off limits; nothing was sent. Use focus_window or "
                    "launch_app to switch to the task's app (those are not blocked), then retry."
                ),
                "desktop_surface": True,
            }
        return await getattr(super(DesktopBody, self), f"vision_{name}")(*args, **kwargs)

    # The loop reads a body's capabilities from its method signatures (at_caret, sensitive,
    # expected_target_identity, allow_final_submit, ...): the guard must show the wrapped method's.
    return functools.wraps(getattr(_core.DesktopBody, f"vision_{name}"))(method)


@_on_native_thread
@dataclass
class DesktopBody(_core.DesktopBody):
    """macOS implementation of the screenshot/action interface used by ``run_vision_loop``."""

    device_id: str = DEVICE_ID

    # No shell sandbox on macOS: with no callable run_command the loop lists it as unavailable.
    vision_run_command = None  # type: ignore[assignment]

    def tool_context(self) -> dict[str, Any]:
        context = super().tool_context()
        context["os"] = "macOS"
        context["key_format"] = (
            "KEY or MODIFIER+KEY with CMD, CTRL, ALT (Option), and SHIFT, for example TAB, CMD+A, CMD+C, "
            "CMD+V, CMD+TAB, or CMD+SHIFT+G"
        )
        return context

    # Every input action first checks the frontmost app: System Settings in front refuses it.
    vision_click = _guarded("click")
    vision_click_control = _guarded("click_control")
    vision_type = _guarded("type")
    vision_type_control = _guarded("type_control")
    vision_key = _guarded("key")
    vision_wheel = _guarded("wheel")
    vision_scroll = _guarded("scroll")
    vision_drag = _guarded("drag")
    vision_select_text = _guarded("select_text")
    vision_hover = _guarded("hover")
    vision_upload = _guarded("upload")
    vision_upload_control = _guarded("upload_control")

    async def navigate_back(self, *, timeout: float = 20.0) -> dict[str, Any]:
        """Back in the front app's history (CMD+[ as a person presses it)."""
        return await self.vision_key("CMD+[", timeout=timeout)

    async def navigate_forward(self, *, timeout: float = 20.0) -> dict[str, Any]:
        return await self.vision_key("CMD+]", timeout=timeout)


DesktopBody._native = sys.modules[__name__]
