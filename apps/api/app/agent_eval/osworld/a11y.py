"""OSWorld's AT-SPI accessibility XML -> the desktop body's control inventory and window list.

Enumeration only, the same contract as the Windows body's UI Automation inventory: every showing,
on-screen node that carries a name, text, or value, with its role and geometry. What a control means
is the model's call.
"""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from typing import Any

STATE = "https://accessibility.ubuntu.example.org/ns/state"
COMPONENT = "https://accessibility.ubuntu.example.org/ns/component"
VALUE = "https://accessibility.ubuntu.example.org/ns/value"

MAX_CONTROLS = 160
_PAIR = re.compile(r"\(\s*(-?\d+)\s*,\s*(-?\d+)\s*\)")
# Roles that take typed text: the only role knowledge here, and it is about the input mechanism
# (whether keystrokes edit a value), not what the control means.
TEXT_ROLES = frozenset({"entry", "text", "password-text", "spin-button", "terminal", "paragraph", "document-text"})
# AT-SPI's structural containers: their names are window or region titles, not click targets. The
# loop binds a point to the smallest listed box around it, so when the tree lacks the real target (a
# GTK2 menu item, a canvas widget) a listed container took the click binding: a named one then failed
# the click-identity check, a nameless one had its centre clicked. Windows are in the window list.
CONTAINER_ROLES = frozenset({
    "frame", "window", "dialog", "file-chooser", "document-web", "document-frame", "panel", "filler",
    "scroll-pane", "viewport", "layered-pane", "split-pane", "root-pane", "glass-pane", "internal-frame",
    "desktop-frame", "page-tab-list", "tool-bar", "menu-bar", "status-bar", "table", "tree-table",
})
# Roles where a press acts on the item (activates, toggles, drags, or selects it as a whole), so a click
# and shift-click across its label never selects characters: input mechanics, as TEXT_ROLES.
NOT_TEXT_ROLES = frozenset({
    "push-button", "toggle-button", "radio-button", "check-box", "combo-box", "menu", "menu-item",
    "check-menu-item", "radio-menu-item", "menu-bar", "tool-bar", "page-tab", "list-item", "tree-item",
    "table-cell", "table-column-header", "table-row-header", "column-header", "row-header", "icon",
    "link", "slider", "scroll-bar", "title-bar",
})


def _state(node: ET.Element, name: str) -> bool:
    return node.get(f"{{{STATE}}}{name}", "false") == "true"


def _pair(node: ET.Element, name: str) -> tuple[int, int] | None:
    match = _PAIR.fullmatch(str(node.get(f"{{{COMPONENT}}}{name}") or "").strip())
    return (int(match.group(1)), int(match.group(2))) if match else None


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def parse_tree(xml_text: str, *, screen_width: int, screen_height: int) -> dict[str, Any]:
    """Controls (active window first, then the shell's panels, then other windows), the window
    list, the focused control, and the active window's screen box and showing text (every showing
    node's name, text, and value, password fields left out: what a wait for text reads)."""
    try:
        root = ET.fromstring(xml_text or "<desktop-frame/>")
    except ET.ParseError:
        return {"controls": [], "windows": [], "focused": None, "active_box": None, "active_text": ""}
    windows: list[dict[str, Any]] = []
    buckets: dict[str, list[dict[str, Any]]] = {"active": [], "shell": [], "other": []}
    focused: dict[str, Any] | None = None
    active_box: list[int] | None = None
    active_text: list[str] = []

    def visit(node: ET.Element, path: str, app: str, window: dict[str, Any] | None) -> None:
        nonlocal focused, active_box
        role = node.tag.split("}")[-1]
        at = _pair(node, "screencoord")
        size = _pair(node, "size")
        if role == "frame" or role == "dialog" or role == "window":
            if _state(node, "showing"):
                window = {
                    "window_id": f"win:{path}",
                    "title": _clean(node.get("name"), 200) or app,
                    "app": app,
                    "active": _state(node, "active"),
                }
                windows.append(window)
                if window["active"] and at and size and size[0] > 0 and size[1] > 0:
                    active_box = [at[0], at[1], size[0], size[1]]
        name = _clean(node.get("name"), 260)
        text = _clean(node.text, 400)
        value = _clean(node.get(f"{{{VALUE}}}value"), 400)
        if window and window.get("active") and _state(node, "showing") and role != "password-text":
            active_text.extend(item for item in (name, " ".join(str(node.text or "").split()), value) if item)
        on_screen = bool(
            at and size and size[0] > 0 and size[1] > 0
            and at[0] < screen_width and at[1] < screen_height
            and at[0] + size[0] > 0 and at[1] + size[1] > 0
        )
        # A nameless node is still listed when it takes input (a radio button labelled only by the
        # text beside it): the Windows inventory lists unnamed controls too, and without it a click
        # on the radio binds to the dialog around it.
        takes_input = any(_state(node, flag) for flag in ("editable", "checkable", "selectable", "expandable"))
        container = role in CONTAINER_ROLES and not _state(node, "editable")
        if on_screen and not container and _state(node, "showing") and _state(node, "visible") and (name or text or value or takes_input):
            x = max(0, at[0])
            y = max(0, at[1])
            width = min(size[0], screen_width - x)
            height = min(size[1], screen_height - y)
            password = role == "password-text"
            editable = _state(node, "editable") or role in TEXT_ROLES
            record = {
                "selector": f"atspi:{path}",
                "tag": role,
                "role": role,
                "type": role,
                # An editable field's node text is its current contents, which change as it is used and
                # never name it: its identity text is its accessible name only (the contents are "value").
                "text": name if editable else (name or text),
                "name": name,
                "accessible_name": name,
                "value": "" if password else (value or (text if text != name else "")),
                "value_redacted": password,
                "box": [x, y, width, height],
                "x": x,
                "y": y,
                "width": width,
                "height": height,
                "visible": True,
                "enabled": _state(node, "enabled") or _state(node, "sensitive"),
                "disabled": not (_state(node, "enabled") or _state(node, "sensitive")),
                "focused": _state(node, "focused"),
                "password": password,
                "editable": editable,
                "multi_line": _state(node, "multi-line"),
                "checked": _state(node, "checked"),
                "selected": _state(node, "selected"),
                "expanded": _state(node, "expanded"),
                "app": app,
                "window_title": (window or {}).get("title", ""),
                "window_id": (window or {}).get("window_id", ""),
            }
            if record["focused"] and (focused is None or record["editable"]):
                focused = record
            if window and window.get("active"):
                buckets["active"].append(record)
            elif app == "gnome-shell":
                buckets["shell"].append(record)
            else:
                buckets["other"].append(record)
        for index, child in enumerate(list(node)):
            visit(child, f"{path}.{index}", app, window)

    for app_index, app_node in enumerate(list(root)):
        app_name = _clean(app_node.get("name"), 80)
        visit(app_node, str(app_index), app_name, None)
    controls = (buckets["active"] + buckets["shell"] + buckets["other"])[:MAX_CONTROLS]
    controls.sort(key=lambda item: (item["box"][1], item["box"][0]))
    for position, control in enumerate(controls, start=1):
        control["index"] = position
    return {
        "controls": controls,
        "windows": windows,
        "focused": focused,
        "active_box": active_box,
        "active_text": " ".join(active_text),
    }


def find_control(xml_text: str, selector: str, *, screen_width: int, screen_height: int) -> dict[str, Any] | None:
    """The control at this exact tree path now, or None when the tree changed under it."""
    for control in parse_tree(xml_text, screen_width=screen_width, screen_height=screen_height)["controls"]:
        if control["selector"] == selector:
            return control
    return None


def control_at(controls: list[dict[str, Any]], x: int, y: int) -> dict[str, Any] | None:
    """The smallest listed control whose box holds the point, or None."""
    holding = [
        item for item in controls
        if item["box"][0] <= x <= item["box"][0] + item["box"][2] and item["box"][1] <= y <= item["box"][1] + item["box"][3]
    ]
    return min(holding, key=lambda item: item["box"][2] * item["box"][3]) if holding else None


def tree_signature(controls: list[dict[str, Any]], windows: list[dict[str, Any]]) -> str:
    active = next((item for item in windows if item.get("active")), {})
    payload = f"{active.get('window_id', '')}|{active.get('title', '')}|" + "|".join(
        f"{item['selector']}:{item['name']}" for item in controls
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:40]
