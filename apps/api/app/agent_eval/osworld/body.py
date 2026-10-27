"""Lumen's desktop body over OSWorld's in-VM HTTP server (screenshot, AT-SPI tree, pyautogui).

Same interface and receipts as ``app.desktop_body.DesktopBody`` so ``run_vision_loop`` drives the
VM exactly as it drives a Windows desktop: the body enumerates and executes, the loop verifies.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import re
import time
from typing import Any

import httpx
from PIL import Image, ImageChops, ImageDraw

from app.agent_eval.osworld import a11y

_KEY_NAMES = {
    "CTRL": "ctrl", "CONTROL": "ctrl", "ALT": "alt", "SHIFT": "shift", "WIN": "win", "SUPER": "win",
    "META": "win", "CMD": "win", "ENTER": "enter", "RETURN": "enter", "ESC": "esc", "ESCAPE": "esc",
    "TAB": "tab", "SPACE": "space", "BACKSPACE": "backspace", "DELETE": "delete", "DEL": "delete",
    "INSERT": "insert", "INS": "insert", "HOME": "home", "END": "end", "PAGEUP": "pageup", "PAGE_UP": "pageup",
    "PGUP": "pageup", "PAGEDOWN": "pagedown", "PAGE_DOWN": "pagedown", "PGDN": "pagedown", "UP": "up",
    "DOWN": "down", "LEFT": "left", "RIGHT": "right", "ARROWUP": "up", "ARROWDOWN": "down",
    "ARROWLEFT": "left", "ARROWRIGHT": "right", "CAPSLOCK": "capslock", "PRINTSCREEN": "printscreen",
    "PLUS": "+", "MINUS": "-", "MENU": "apps", "APPS": "apps",
}
# The most clipboard text read_clipboard returns (same cap as the Windows body).
CLIPBOARD_READ_LIMIT_CHARS = 4000
_MODIFIERS = {"shift": "shift", "ctrl": "ctrl", "control": "ctrl", "alt": "alt", "meta": "win", "win": "win", "super": "win"}
# Run in the VM: the same raw scan run_downloads.scan_folder makes locally (top-level files only).
_DOWNLOADS_SCAN_SCRIPT = (
    "import json, os, time\n"
    "root = os.path.expanduser('~/Downloads')\n"
    "files = []\n"
    "try:\n"
    "    for e in os.scandir(root):\n"
    "        try:\n"
    "            if e.name.startswith('.') or not (e.is_file(follow_symlinks=False) or e.is_symlink()):\n"
    "                continue\n"
    "            s = e.stat(follow_symlinks=False)\n"
    "        except OSError:\n"
    "            continue\n"
    "        files.append({'name': e.name, 'path': os.path.join(root, e.name), 'size': s.st_size, "
    "'mtime': s.st_mtime, 'ctime': s.st_ctime})\n"
    "except OSError:\n"
    "    pass\n"
    "print(json.dumps({'folder': root, 'now': time.time(), 'files': files}))\n"
)
# Run in the VM: open one file in its default app, detached, only a regular file directly in ~/Downloads.
_OPEN_FILE_SCRIPT = (
    "import os, subprocess, sys\n"
    "root = os.path.realpath(os.path.expanduser('~/Downloads'))\n"
    "path = sys.argv[1]\n"
    "real = os.path.realpath(path)\n"
    "allowed = set(sys.argv[2].split(','))\n"
    "ext = os.path.splitext(real)[1].lstrip('.').lower()\n"
    "if os.path.dirname(real) == root and os.path.dirname(os.path.abspath(path)) == root and os.path.isfile(real) and ext in allowed:\n"
    "    subprocess.Popen(['xdg-open', real], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)\n"
    "    print('opened')\n"
)


def pyautogui_keys(combo: str) -> list[str]:
    """'CTRL+SHIFT+T' -> ['ctrl', 'shift', 't']; one character stays itself."""
    raw = str(combo or "").strip()
    if len(raw) == 1:
        return [raw.lower() if raw.isalpha() else raw]
    parts = [part for part in raw.replace(" ", "").split("+") if part] if raw != "+" else ["+"]
    keys: list[str] = []
    for part in parts:
        upper = part.upper()
        if upper in _KEY_NAMES:
            keys.append(_KEY_NAMES[upper])
        elif upper.startswith("F") and upper[1:].isdigit():
            keys.append(upper.lower())
        else:
            keys.append(part.lower() if len(part) == 1 else part.lower())
    return keys


# Lists the VM's launchable applications: every desktop entry (freedesktop.org desktop file) under the
# XDG application directories, earlier directories overriding later ones by desktop file id, with the
# raw keys the body filters on. Runs inside the VM through /execute; prints one JSON array.
_APP_LIST_SCRIPT = r"""
import configparser, json, os, shutil
dirs = [os.path.expanduser("~/.local/share/applications"), "/usr/local/share/applications", "/usr/share/applications",
        "/var/lib/snapd/desktop/applications", "/var/lib/flatpak/exports/share/applications"]
keys = ("Type", "Name", "Exec", "TryExec", "Categories", "NoDisplay", "Hidden", "Terminal", "OnlyShowIn", "NotShowIn")
rows, seen = [], set()
for base in dirs:
    for root, folders, files in os.walk(base):
        folders.sort()
        for name in sorted(files):
            if not name.endswith(".desktop"):
                continue
            path = os.path.join(root, name)
            app_id = os.path.relpath(path, base).replace(os.sep, "-")
            if app_id in seen:
                continue
            seen.add(app_id)
            parser = configparser.RawConfigParser(strict=False, interpolation=None, delimiters=("=",))
            parser.optionxform = str
            try:
                parser.read(path, encoding="utf-8")
                entry = parser["Desktop Entry"]
            except Exception:
                continue
            row = {key: entry.get(key, "") for key in keys}
            row["TryExecFound"] = not row["TryExec"] or bool(shutil.which(row["TryExec"]))
            row.update(id=app_id, path=path)
            rows.append(row)
print(json.dumps(rows))
"""

# Starts one listed desktop entry detached from /execute's pipes (a child holding them would hold the
# request open until the app exits): gtk-launch by desktop file id, else the entry's own Exec line with
# its field codes removed, as an argument list (never a shell string). Prints one JSON object.
_LAUNCH_SCRIPT = r"""
import configparser, json, re, shlex, shutil, subprocess, sys
app_id, path = sys.argv[1], sys.argv[2]
def spawn(argv):
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True, close_fds=True)
if shutil.which("gtk-launch"):
    try:
        code = spawn(["gtk-launch", app_id]).wait(timeout=8)
    except subprocess.TimeoutExpired:
        code = 0
    if code == 0:
        print(json.dumps({"launched": True, "via": "gtk-launch"}))
        sys.exit(0)
parser = configparser.RawConfigParser(strict=False, interpolation=None, delimiters=("=",))
parser.optionxform = str
parser.read(path, encoding="utf-8")
entry = parser["Desktop Entry"]
argv = []
for token in shlex.split(entry.get("Exec", "")):
    if re.fullmatch(r"%[fFuUdDnNvmki]", token):
        continue
    token = re.sub(r"%[fFuUdDnNvmki]", "", token.replace("%c", entry.get("Name", ""))).replace("%%", "%")
    if token:
        argv.append(token)
if not argv:
    print(json.dumps({"launched": False}))
    sys.exit(0)
spawn(argv)
print(json.dumps({"launched": True, "via": "exec"}))
"""

# Never in the application list: shells, terminals, interpreters, privilege escalation, and the
# settings-database editor (the Windows body hides shells, terminals, the Run box, and the registry
# editor). Matched on the entry's program, its categories, and its name.
_PROHIBITED_PROGRAMS = frozenset({
    "sh", "bash", "dash", "zsh", "fish", "ksh", "csh", "tcsh", "sudo", "pkexec", "su", "gksu", "gksudo",
    "x-terminal-emulator", "gnome-terminal", "kgx", "ptyxis", "xterm", "uxterm", "koi8rxterm", "lxterm",
    "konsole", "xfce4-terminal", "mate-terminal", "lxterminal", "tilix", "terminator", "guake", "kitty",
    "alacritty", "terminology", "rxvt", "urxvt", "st", "foot", "wezterm", "byobu", "tmux", "screen",
    "dconf-editor",
})
_PROHIBITED_PROGRAM = re.compile(r"^(?:python|ipython|idle|perl|ruby|irb|node|nodejs|lua|php|tclsh|wish)[\d.]*$")
_PROHIBITED_NAME = re.compile(r"(?:^|\s)(?:terminal|console|xterm|python|idle)(?:\s|$|\d|\()")
_DESKTOP_NAMES = frozenset({"gnome", "ubuntu", "unity"})
_MAX_APPLICATIONS = 160


def _exec_program(exec_line: str) -> str:
    """The program a desktop entry's Exec line starts (past an `env VAR=value` prefix)."""
    tokens = re.sub(r"%[fFuUdDnNvmkic]", "", str(exec_line or "")).split()
    while tokens and (tokens[0].rsplit("/", 1)[-1] == "env" or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0])):
        tokens.pop(0)
    return tokens[0].strip("\"'").rsplit("/", 1)[-1].casefold() if tokens else ""


def launchable_applications(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Desktop entries a person could start from the app grid: applications that are not hidden, not
    run in a terminal, shown on this desktop, with their program installed, minus the prohibited ones.
    One row per name; `app_id` is the desktop file id, `path` stays body-side."""
    listed: list[dict[str, str]] = []
    seen_names: set[str] = set()
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        name = " ".join(str(row.get("Name") or "").split())[:100]

        def flag(key: str) -> bool:
            return str(row.get(key) or "").strip().casefold() == "true"

        def listed_names(key: str) -> set[str]:
            return {item.strip().casefold() for item in str(row.get(key) or "").split(";") if item.strip()}

        shown_in, hidden_in, categories = listed_names("OnlyShowIn"), listed_names("NotShowIn"), listed_names("Categories")
        program = _exec_program(str(row.get("Exec") or ""))
        if (
            str(row.get("Type") or "") != "Application" or not name or not program or not row.get("id")
            or flag("NoDisplay") or flag("Hidden") or flag("Terminal") or row.get("TryExecFound") is False
            or (shown_in and not shown_in & _DESKTOP_NAMES) or hidden_in & _DESKTOP_NAMES
            or "terminalemulator" in categories or program in _PROHIBITED_PROGRAMS
            or _PROHIBITED_PROGRAM.match(program) or _PROHIBITED_NAME.search(name.casefold())
            or name.casefold() in seen_names
        ):
            continue
        seen_names.add(name.casefold())
        listed.append({"app_id": str(row["id"]), "name": name, "path": str(row.get("path") or "")})
    listed.sort(key=lambda item: (item["name"].casefold(), item["app_id"]))
    return listed[:_MAX_APPLICATIONS]


def _text_count(text: str, wanted: str) -> int:
    return " ".join(str(text or "").split()).casefold().count(wanted)


def _changed(before: Image.Image, after: Image.Image, box: tuple[int, int, int, int] | None = None) -> bool:
    if box:
        before, after = before.crop(box), after.crop(box)
    small = (160, 90)
    a = before.convert("L").resize(small)
    b = after.convert("L").resize(small)
    diff = ImageChops.difference(a, b).point(lambda value: 255 if value > 14 else 0)
    return sum(1 for value in diff.getdata() if value) > 4


# AT-SPI roles that never take typed text; focus on one of these refuses a type. Roles where typing is
# legitimately common are left out even when the tree does not flag them editable: table-cell (a
# LibreOffice Calc cell takes typing and reports no editable state), list-item and tree-item (a file
# manager rename, an inline-edit row), label (a canvas app's text object), page-tab and tab (a
# renamable sheet tab).
_NON_TYPABLE_ROLES = frozenset({
    "push-button", "toggle-button", "check-box", "radio-button", "menu-item", "menu", "menu-bar",
    "check-menu-item", "radio-menu-item", "link", "icon", "image", "scroll-bar", "slider", "separator",
})


_NAME_COMPARE_CHARS = 180  # the model-side identity text is cut here; AT-SPI names run to 260
_UNNAMED_POINT_SLACK = 48  # px an unnamed control's box may sit from the model's point


def _label(item: dict[str, Any]) -> str:
    """A control's accessible name, or its text when it is named only by text, as one casefolded line.
    An editable field's text is its current contents, which never name it."""
    text = "" if item.get("editable") else item.get("text")
    return " ".join(str(item.get("name") or text or "").split()).casefold()


def _expected_labels(expected: dict[str, Any]) -> set[str]:
    names = [expected.get("target_text")] + [n for n in (expected.get("target_names") or []) if isinstance(n, str)]
    return {label for label in (" ".join(str(n or "").split()).casefold() for n in names) if label}


def _labels_match(label: str, wanted: set[str]) -> bool:
    """Equal names, or a name the model-side text was cut from (it keeps 180 of AT-SPI's 260 characters)."""
    floor = _NAME_COMPARE_CHARS - 20
    return any(
        label == one or (min(len(label), len(one)) >= floor and (label.startswith(one) or one.startswith(label)))
        for one in wanted
    )


def _expected_kind(expected: dict[str, Any]) -> str:
    return str(expected.get("target_role") or expected.get("target_tag") or "").strip().casefold()


def _near_point(item: dict[str, Any], x: int, y: int, expected: dict[str, Any]) -> bool:
    """An unnamed control is the approved one only when it still sits where the model pointed (or
    where the observed control's box was)."""
    bx, by, bw, bh = item["box"]
    slack = _UNNAMED_POINT_SLACK
    if (x or y) and not (bx - slack <= x <= bx + bw + slack and by - slack <= y <= by + bh + slack):
        return False
    old = expected.get("target_box")
    if isinstance(old, (list, tuple)) and len(old) == 4:
        try:
            ox, oy, ow, oh = (float(v) for v in old)
        except (TypeError, ValueError):
            return True
        return (abs((ox + ow / 2) - (bx + bw / 2)) <= slack + max(ow, bw) / 2
                and abs((oy + oh / 2) - (by + bh / 2)) <= slack + max(oh, bh) / 2)
    return True


def same_target(item: dict[str, Any], expected: dict[str, Any], x: int = 0, y: int = 0) -> bool:
    """Whether this control is the one the model approved. AT-SPI selectors are tree positions, so a
    tree shift can put another control at the same path: the accessible name (or text) must match
    when the approved one had one, the role when one was recorded, and an unnamed control needs its
    role and to still be near the model's point. No recorded identity at all binds nothing."""
    wanted = _expected_labels(expected)
    kind = _expected_kind(expected)
    if not wanted and not kind:
        return True
    role = str(item.get("role") or "").strip().casefold()
    if wanted:
        return _labels_match(_label(item), wanted) and (not kind or not role or kind == role)
    return not _label(item) and kind == role and _near_point(item, int(x or 0), int(y or 0), expected)


class OSWorldBody:
    device_id = "osworld-vm"
    execution_surface = "desktop"

    def __init__(self, server_url: str, *, http_timeout: float = 60.0) -> None:
        self.server_url = server_url.rstrip("/")
        self._http = httpx.Client(timeout=http_timeout)
        self._last_click: tuple[int, int] | None = None
        self._sensitive: set[str] = set()
        self._screen = (1920, 1080)
        self._windows: list[dict[str, Any]] = []
        self._tools: dict[str, bool] = {}
        self._apps: tuple[float, list[dict[str, str]]] | None = None
        # Digest of what the clipboard held at the run's first observation (X11 has no sequence number):
        # read_clipboard refuses while it still holds that. None until the first observation records it.
        self._clipboard_start_digest: str | None = None
        self._password_control_shown = False

    # --- VM access -------------------------------------------------------------------------------

    def _screenshot(self) -> Image.Image:
        response = self._http.get(f"{self.server_url}/screenshot")
        response.raise_for_status()
        image = Image.open(io.BytesIO(response.content)).convert("RGB")
        self._screen = image.size
        return image

    def _tree(self) -> dict[str, Any]:
        try:
            response = self._http.get(f"{self.server_url}/accessibility")
            xml_text = response.json().get("AT") or "" if response.status_code == 200 else ""
        except Exception:
            xml_text = ""
        parsed = a11y.parse_tree(xml_text, screen_width=self._screen[0], screen_height=self._screen[1])
        parsed["xml"] = xml_text
        if parsed["windows"]:
            self._windows = parsed["windows"]
        return parsed

    def _execute(self, command: list[str] | str, *, shell: bool = False) -> dict[str, Any]:
        response = self._http.post(
            f"{self.server_url}/execute",
            headers={"Content-Type": "application/json"},
            content=json.dumps({"command": command, "shell": shell}),
        )
        try:
            return response.json()
        except Exception:
            return {"status": "error", "message": response.text[:300]}

    def _pyautogui(self, code: str) -> dict[str, Any]:
        prefix = "import pyautogui, time; pyautogui.FAILSAFE = False; pyautogui.PAUSE = 0.02\n"
        return self._execute(["python", "-c", prefix + code])

    async def vision_run_command(self, command: str, *, timeout_seconds: int | None = None, **_: Any) -> dict[str, Any]:
        """Run one shell command inside the VM (the task's own machine, its sandbox) as the VM user."""
        from app import command_sandbox

        command = str(command or "")
        if not command.strip():
            return {"ok": False, "action": "run_command", "failure_reason": "command_empty"}
        if len(command) > command_sandbox.COMMAND_CHARS:
            return {"ok": False, "action": "run_command", "failure_reason": "command_too_long"}
        # Within the HTTP client's 60 s, so the command's own timeout always ends it first.
        limit = max(1, min(50, int(timeout_seconds or command_sandbox.DEFAULT_TIMEOUT_SECONDS)))
        started = time.monotonic()
        try:
            result = await asyncio.to_thread(
                self._execute, ["timeout", "-k", "2", str(limit), "bash", "-lc", command]
            )
        except Exception as exc:  # noqa: BLE001 - the VM did not answer
            return {"ok": False, "action": "run_command", "failure_reason": "command_transport_failed", "error": str(exc)[:200]}
        stdout = str(result.get("output") or "").replace(chr(0), "")
        stderr = str(result.get("error") or "").replace(chr(0), "")
        exit_code = int(result.get("returncode") or 0) if str(result.get("returncode") or "0").lstrip("-").isdigit() else -1
        return {
            "ok": result.get("status") == "success" or "returncode" in result,
            "action": "run_command",
            "exit_code": exit_code,
            "timed_out": exit_code in (124, 137),
            "stdout": stdout[: command_sandbox.OUTPUT_CHARS],
            "stderr": stderr[: command_sandbox.OUTPUT_CHARS],
            "stdout_truncated": len(stdout) > command_sandbox.OUTPUT_CHARS,
            "stderr_truncated": len(stderr) > command_sandbox.OUTPUT_CHARS,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "sandbox": "osworld_vm",
            "timeout_seconds": limit,
        }

    def _applications(self) -> list[dict[str, str]]:
        """The VM's launchable applications, read once per five minutes (an unreadable list is not kept)."""
        now = time.monotonic()
        if self._apps is not None and now - self._apps[0] < 300:
            return self._apps[1]
        try:
            result = self._execute(["python", "-c", _APP_LIST_SCRIPT])
            output = str(result.get("output") or "").strip()
            rows = json.loads(output) if result.get("status") == "success" and output else None
        except Exception:
            rows = None
        if not isinstance(rows, list):
            return []
        listed = launchable_applications(rows)
        self._apps = (now, listed)
        return listed

    def _has_tool(self, name: str) -> bool:
        if name not in self._tools:
            result = self._execute(["which", name])
            self._tools[name] = bool(str(result.get("output") or "").strip())
        return self._tools[name]

    def _clipboard_text_sync(self) -> tuple[str | None, str]:
        """(clipboard text, failure_reason): via xclip or xsel in the VM; (None, reason) when unreadable.
        An empty clipboard reads as ""."""
        if self._has_tool("xclip"):
            command = ["xclip", "-selection", "clipboard", "-o"]
        elif self._has_tool("xsel"):
            command = ["xsel", "--clipboard", "--output"]
        else:
            return None, "clipboard_unavailable"
        result = self._execute(command)
        if result.get("status") != "success":
            return None, "clipboard_unavailable"
        if result.get("returncode"):
            # xclip exits non-zero when no app owns the clipboard or it holds no text.
            return "", ""
        return str(result.get("output") or ""), ""

    def _clipboard_digest(self, text: str | None) -> str:
        return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest() if text is not None else ""

    def _read_clipboard_sync(self) -> dict[str, Any]:
        text, failure = self._clipboard_text_sync()
        if text is None:
            return {"ok": False, "action": "read_clipboard", "failure_reason": failure, "desktop_surface": True}
        if self._clipboard_start_digest is None or self._clipboard_digest(text) == self._clipboard_start_digest:
            return {"ok": False, "action": "read_clipboard", "failure_reason": "clipboard_not_from_this_run", "desktop_surface": True}
        if self._password_control_shown:
            return {"ok": False, "action": "read_clipboard", "failure_reason": "clipboard_sensitive_source", "desktop_surface": True}
        if not text:
            return {"ok": False, "action": "read_clipboard", "failure_reason": "clipboard_holds_no_text", "desktop_surface": True}
        return {
            "ok": True,
            "action": "read_clipboard",
            "text": text[:CLIPBOARD_READ_LIMIT_CHARS],
            "chars": len(text),
            "truncated": len(text) > CLIPBOARD_READ_LIMIT_CHARS,
            # X11 keeps no record of who copied: changed since the run began is all this body can say.
            "copied_by_run_action": False,
            "desktop_surface": True,
        }

    async def vision_read_clipboard(self, **_: Any) -> dict[str, Any]:
        return await asyncio.to_thread(self._read_clipboard_sync)

    # --- observation -----------------------------------------------------------------------------

    async def set_control_indicator(self, active: bool, **_: Any) -> dict[str, Any]:
        return {"ok": True, "active": bool(active), "visible": False, "surface": "desktop"}

    def tool_context(self) -> dict[str, Any]:
        return {
            "execution_surface": "desktop",
            "os": "Ubuntu",
            "windows": self._windows,
            "applications": [{"app_id": item["app_id"], "name": item["name"]} for item in self._applications()],
            "key_format": "KEY or MODIFIER+KEY, for example TAB, CTRL+A, ALT+TAB, or SUPER",
            "typing_requires_caret": (
                "Bringing a window to the front does not put the caret in its text area. "
                "Before typing, click the middle of the exact text area - not its edge, "
                "title bar, tab strip, or toolbar - so the keystrokes land in it."
            ),
            "tool_targets": {
                "focus_window": "Use an exact window_id from windows.",
                "launch_app": "Use an exact app_id from applications.",
            },
        }

    async def navigate(self, url: str, *, timeout: float = 45.0) -> dict[str, Any]:
        # The runner passes skip_navigate, so only the model calls this. There is no browser surface
        # here: say so instead of reporting a navigation that never happened.
        del url, timeout
        return {
            "ok": False,
            "action": "navigate",
            "failure_reason": "navigate_unavailable_on_desktop",
            "failure_detail": "This task has only the desktop surface. To open a URL, click the browser window's address bar, type it, and press ENTER.",
            "desktop_surface": True,
        }

    async def observe(self, *, timeout: float = 30.0) -> dict[str, Any]:
        del timeout
        shot = await self.capture_screenshot()
        return {"ok": bool(shot.get("ok")), "url": shot.get("url"), "title": shot.get("title"), "page_evidence": shot.get("page_evidence") or {}}

    async def capture_screenshot(self, *, full_page: bool = False, timeout: float = 24.0) -> dict[str, Any]:
        del full_page, timeout
        if self._clipboard_start_digest is None:
            start_text, _ = await asyncio.to_thread(self._clipboard_text_sync)
            self._clipboard_start_digest = self._clipboard_digest(start_text if start_text is not None else "")
        image = await asyncio.to_thread(self._screenshot)
        tree = await asyncio.to_thread(self._tree)
        controls, windows = tree["controls"], tree["windows"]
        self._password_control_shown = any(
            control.get("password") or control["selector"] in self._sensitive for control in controls
        )
        masked = 0
        if self._sensitive:
            draw = ImageDraw.Draw(image)
            for control in controls:
                if control["selector"] in self._sensitive or control.get("password"):
                    x, y, w, h = control["box"]
                    draw.rectangle((x, y, x + w, y + h), fill=(24, 24, 27))
                    control["value"] = ""
                    control["value_redacted"] = True
                    masked += 1
        active = next((item for item in windows if item.get("active")), None)
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=80, optimize=True)
        width, height = image.size
        window_lines = [
            f"[{item['window_id']}] {item['title']} ({item['app']}){' (active)' if item.get('active') else ''}"
            for item in windows[:16]
        ]
        focused = tree.get("focused")
        caret = None
        if focused and focused.get("editable"):
            caret = {"control": focused.get("index"), "role": focused.get("role"), "value": "" if focused.get("password") else focused.get("value", "")[:300]}
        url = f"desktop://osworld/{(active or {}).get('window_id', 'none')}"
        title = (active or {}).get("title") or "Ubuntu desktop"
        signature = a11y.tree_signature(controls, windows)
        return {
            "ok": True,
            "image": base64.b64encode(output.getvalue()).decode("ascii"),
            "width": width,
            "height": height,
            "dpr": 1,
            "url": url,
            "title": title,
            "surface_signature": signature,
            "stable_surface_signature": signature,
            "scroll_y": 0,
            "scroll_height": height,
            "page_evidence": {
                "url": url,
                "title": title,
                "viewport_text": "Visible windows:\n" + "\n".join(window_lines),
                "width": width,
                "height": height,
                "scroll_y": 0,
                "scroll_height": height,
                "controls": controls,
                "windows": windows,
                "caret": caret,
                "password_controls_masked": masked,
                "execution_surface": "desktop",
            },
        }

    # --- sensitive-control masking ---------------------------------------------------------------

    async def mark_sensitive_screenshot_control(self, selector: str, *, expected_page_context: dict[str, Any], timeout: float = 12.0) -> dict[str, Any]:
        return await self.ensure_sensitive_screenshot_controls([selector], expected_page_context=expected_page_context, timeout=timeout)

    async def ensure_sensitive_screenshot_control(self, selector: str, *, expected_page_context: dict[str, Any], timeout: float = 12.0) -> dict[str, Any]:
        return await self.ensure_sensitive_screenshot_controls([selector], expected_page_context=expected_page_context, timeout=timeout)

    async def ensure_sensitive_screenshot_controls(self, selectors: list[str], *, expected_page_context: dict[str, Any], timeout: float = 12.0) -> dict[str, Any]:
        del expected_page_context, timeout
        exact = [str(item or "").strip() for item in selectors if str(item or "").strip()]
        tree = await asyncio.to_thread(self._tree)
        present = {item["selector"] for item in tree["controls"]}
        if not exact or any(item not in present for item in exact):
            return {"ok": False, "selector_bound": False, "page_context_bound": False, "failure_reason": "desktop_sensitive_target_changed"}
        self._sensitive.update(exact)
        return {"ok": True, "selector_bound": True, "page_context_bound": True, "bound_selectors": exact, "mask_surface_signature": "|".join(sorted(exact))[:40], "mask_mode": "osworld_atspi"}

    # --- actions ---------------------------------------------------------------------------------

    def _click_sync(self, x: int, y: int, button: str, clicks: int, hold_ms: int, modifiers: list[str]) -> dict[str, Any]:
        mods = [_MODIFIERS[m] for m in (str(item).lower() for item in modifiers or []) if m in _MODIFIERS]
        button = button if button in {"left", "right", "middle"} else "left"
        clicks = max(1, min(3, int(clicks or 1)))
        hold = max(0, min(5000, int(hold_ms or 0)))
        before = self._screenshot()
        code = "".join(f"pyautogui.keyDown({m!r}); " for m in mods)
        if hold:
            code += f"pyautogui.moveTo({x}, {y}); pyautogui.mouseDown(button={button!r}); time.sleep({hold / 1000:.3f}); pyautogui.mouseUp(button={button!r}); "
        else:
            code += f"pyautogui.click({x}, {y}, clicks={clicks}, interval=0.08, button={button!r}); "
        code += "".join(f"pyautogui.keyUp({m!r}); " for m in reversed(mods))
        result = self._pyautogui(code)
        self._last_click = (x, y)
        time.sleep(0.35)
        after = self._screenshot()
        tree = self._tree()
        focused = tree.get("focused") or {}
        active = next((item for item in tree["windows"] if item.get("active")), {})
        return {
            "ok": result.get("status") == "success" and not result.get("returncode"),
            "action": "click",
            "activation_attempted": True,
            "activation_dispatched": result.get("status") == "success",
            "activation_count": 1,
            "surface_changed": _changed(before, after),
            "active_typable": bool(focused.get("editable")),
            "active_tag": focused.get("role"),
            "active_window_title": active.get("title", ""),
            "failure_reason": "" if result.get("status") == "success" else "osworld_execute_failed",
            "desktop_surface": True,
        }

    async def vision_click(self, x: int, y: int, *, button: str = "left", clicks: int = 1, hold_ms: int = 0,
                           modifiers: list[str] | None = None, allow_final_submit: bool = False, **_: Any) -> dict[str, Any]:
        if allow_final_submit:
            button, clicks, hold_ms, modifiers = "left", 1, 0, None
        return await asyncio.to_thread(self._click_sync, int(x), int(y), button, clicks, hold_ms, list(modifiers or []))

    async def vision_click_control(self, selector: str, fallback_x: int, fallback_y: int, *, button: str = "left",
                                   clicks: int = 1, hold_ms: int = 0, modifiers: list[str] | None = None, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("allow_final_submit"):
            button, clicks, hold_ms, modifiers = "left", 1, 0, None
        expected = kwargs.get("expected_target_identity") if isinstance(kwargs.get("expected_target_identity"), dict) else {}
        tree = await asyncio.to_thread(self._tree)
        found = next((item for item in tree["controls"] if item["selector"] == selector), None)
        guarded = bool(kwargs.get("allow_final_submit") or kwargs.get("allow_guarded_intermediate"))
        control = found if found and same_target(found, expected, int(fallback_x), int(fallback_y)) else None
        rebound = False
        if control is None:
            # The selector is stale or now names another control. As on Windows, fail closed: the
            # model's point is only trusted when the control under it is the same one (same accessible
            # name, or both unnamed with the same role); with final or guarded authority it must also
            # sit in the approved control's own window.
            under = a11y.control_at(tree["controls"], int(fallback_x), int(fallback_y)) if (fallback_x or fallback_y) else None
            if under is not None and (_expected_labels(expected) or _expected_kind(expected)) and same_target(under, expected, int(fallback_x), int(fallback_y)):
                same_window = True
                if guarded:
                    same_window = bool(found) and bool(found.get("window_id")) and found.get("window_id") == under.get("window_id")
                if same_window:
                    control, rebound = under, True
        if control is None:
            return {"ok": False, "action": "click", "failure_reason": "desktop_uia_target_stale", "activation_attempted": False, "desktop_surface": True}
        # The model's own point when it lies on the control (its exact target inside a row or
        # list), else the control's centre, as the Windows body does.
        x, y, w, h = control["box"]
        inside = x <= int(fallback_x) <= x + w and y <= int(fallback_y) <= y + h
        point = (int(fallback_x), int(fallback_y)) if inside else (x + w // 2, y + h // 2)
        result = await asyncio.to_thread(self._click_sync, point[0], point[1], button, clicks, hold_ms, list(modifiers or []))
        return {**result, "bound_control_resolved": True, "clicked_control_name": control.get("name", ""),
                **({"rebound_by_point": True} if rebound else {})}

    def _type_sync(self, text: str, at_caret: bool, target_point: tuple[int, int] | None = None) -> dict[str, Any]:
        def refuse(detail: str, attempted: bool) -> dict[str, Any]:
            return {
                "ok": False, "action": "type", "failure_reason": "desktop_type_target_not_focused", "failure_detail": detail,
                "focus_attempted": attempted, "input_dispatched": False, "desktop_surface": True,
            }

        not_text = "Click the middle of the exact text area first, then type."
        if target_point is not None and not at_caret:
            # Judge what is under the point before touching it: a click on a button fires the button.
            # (Typing at the caret never clicks: that would move the caret.)
            under = a11y.control_at(self._tree()["controls"], target_point[0], target_point[1])
            if under and not under.get("editable") and under.get("role") in _NON_TYPABLE_ROLES:
                return refuse(f"The control at that point ({under.get('role')}) does not take text, so nothing was clicked or typed. {not_text}", False)
            # Put the caret where the model pointed, as a person clicks the field before typing.
            self._click_sync(target_point[0], target_point[1], "left", 1, 0, [])
        before_tree = self._tree()
        before_values = {item["value"] for item in before_tree["controls"] if item.get("value")}
        focused_before = before_tree.get("focused") or {}
        if focused_before and not focused_before.get("editable") and focused_before.get("role") in _NON_TYPABLE_ROLES:
            # Focus sits on a control that cannot take text (a button, a menu entry, a list row): typing
            # now would fire shortcuts or go nowhere. Nothing is typed.
            return refuse(f"Keyboard focus is on a control that does not take text, so nothing was typed. {not_text}", target_point is not None and not at_caret)
        # A tree that reports no focused control (a canvas app such as LibreOffice) cannot confirm the
        # caret: the text is typed, and the receipt says so.
        focus_unverified = not focused_before
        before = self._screenshot()
        replace_all = bool(
            not at_caret and focused_before.get("editable") and not focused_before.get("multi_line")
            and focused_before.get("role") not in {"terminal", "paragraph", "document-text"}
        )
        if replace_all:
            self._pyautogui("pyautogui.hotkey('ctrl', 'a'); time.sleep(0.1)")
        if text.isascii():
            result = self._pyautogui(f"pyautogui.write({text!r}, interval=0.004)")
            delivery = "pyautogui_write"
        elif self._has_tool("xdotool"):
            result = self._execute(["xdotool", "type", "--delay", "8", "--", text])
            delivery = "xdotool_type"
        else:
            return {"ok": False, "action": "type", "failure_reason": "osworld_non_ascii_text_unsupported", "input_dispatched": False, "desktop_surface": True}
        time.sleep(0.25)
        after = self._screenshot()
        after_tree = self._tree()
        focused_after = after_tree.get("focused") or {}
        values = [item["value"] for item in after_tree["controls"] if item.get("value")]
        appeared = [item for item in values if item not in before_values]
        expected = str(text)
        exact = next((item for item in appeared if item.strip() == expected.strip()), "")
        observed = exact or next((item for item in appeared if expected.strip() and expected.strip()[:60] in item), "")
        active_value = exact or observed or str(focused_after.get("value") or "")
        # The field's value was read when a typed-text match turned up, or when the focused editable
        # field reports a value (even a different one: that is what lets a wrong value be caught).
        value_read = bool(observed) or bool(
            focused_after.get("editable") and not focused_after.get("password") and not focused_after.get("value_redacted")
            and str(focused_after.get("value") or "")
        )
        screen_changed = _changed(before, after)
        dispatched = result.get("status") == "success"
        if not dispatched:
            return {"ok": False, "action": "type", "failure_reason": "osworld_execute_failed", "input_dispatched": False, "desktop_surface": True}
        if not observed and not screen_changed:
            return {
                "ok": False, "action": "type", "failure_reason": "desktop_type_had_no_effect",
                "failure_detail": "Nothing changed on screen and the text appears in no control, so no text area had the caret. Click the middle of the exact text area first, then type.",
                "input_dispatched": True, "text_delivery": delivery, "surface_changed": False, "desktop_surface": True,
            }
        return {
            "ok": True,
            "action": "type",
            "focus_attempted": True,
            "text_delivery": delivery,
            "surface_changed": screen_changed,
            "active_typable": bool(focused_after.get("editable") or focused_before.get("editable")),
            "active_has_value": bool(active_value),
            "active_value": active_value,
            "active_value_length": len(active_value),
            "active_value_matches_expected": bool(observed) or active_value == expected,
            "value_read_back": value_read,
            "typed_where_caret_is": not observed,
            "typed_at_caret": not replace_all,
            "input_dispatched": True,
            **({"focus_unverified": True} if focus_unverified else {}),
            "desktop_surface": True,
        }

    async def vision_type(self, text: str, *, at_caret: bool = False, restore_caret: bool = True,
                          target_point: Any = None, **_: Any) -> dict[str, Any]:
        del restore_caret
        point = None
        if isinstance(target_point, (list, tuple)) and len(target_point) == 2:
            try:
                point = (int(target_point[0]), int(target_point[1]))
            except (TypeError, ValueError):
                point = None
        return await asyncio.to_thread(self._type_sync, str(text), bool(at_caret), point)

    async def vision_type_control(self, selector: str, text: str, *, at_caret: bool = False, **kwargs: Any) -> dict[str, Any]:
        expected = kwargs.get("expected_target_identity") if isinstance(kwargs.get("expected_target_identity"), dict) else {}
        tree = await asyncio.to_thread(self._tree)
        control = next((item for item in tree["controls"] if item["selector"] == selector), None)
        # Selectors are tree positions: the field must still be the one the model approved.
        if control is None or not same_target(control, expected):
            return {"ok": False, "action": "type", "failure_reason": "desktop_uia_target_stale", "focus_attempted": False, "desktop_surface": True}
        if not control.get("focused"):
            # Focus it the way a person does; a field that already has focus keeps its caret.
            x, y, w, h = control["box"]
            await asyncio.to_thread(self._click_sync, x + w // 2, y + h // 2, "left", 1, 0, [])
        return await self.vision_type(text, at_caret=at_caret)

    def _wheel_sync(self, x: int, y: int, dx: int, dy: int, modifiers: list[str]) -> dict[str, Any]:
        mods = [_MODIFIERS[m] for m in (str(item).lower() for item in modifiers or []) if m in _MODIFIERS]
        before = self._screenshot()
        code = f"pyautogui.moveTo({x}, {y}); " + "".join(f"pyautogui.keyDown({m!r}); " for m in mods)
        if dy or not dx:
            clicks = max(1, round(abs(dy or 300) / 100)) * (-1 if (dy or 300) > 0 else 1)
            code += f"pyautogui.scroll({clicks}); "
        if dx:
            clicks = max(1, round(abs(dx) / 100)) * (1 if dx > 0 else -1)
            code += f"pyautogui.hscroll({clicks}); "
        code += "".join(f"pyautogui.keyUp({m!r}); " for m in reversed(mods))
        self._pyautogui(code)
        time.sleep(0.3)
        after = self._screenshot()
        scrolled = _changed(before, after, (max(0, x - 360), max(0, y - 240), x + 360, y + 240))
        return {"ok": True, "action": "scroll", "wheel_point": [x, y], "before_scroll_y": 0, "scroll_y": 1 if scrolled else 0,
                "scroll_changed": scrolled, "surface_changed": _changed(before, after), "desktop_surface": True}

    async def vision_scroll(self, dy: int, *, dx: int = 0, **_: Any) -> dict[str, Any]:
        x, y = self._last_click or (self._screen[0] // 2, self._screen[1] // 2)
        return await asyncio.to_thread(self._wheel_sync, x, y, int(dx or 0), int(dy or 0), [])

    async def vision_wheel(self, x: int, y: int, dx: int, dy: int, *, modifiers: list[str] | None = None, **_: Any) -> dict[str, Any]:
        return await asyncio.to_thread(self._wheel_sync, int(x), int(y), int(dx or 0), int(dy or 0), list(modifiers or []))

    def _key_sync(self, key: str, repeat: int, hold_ms: int) -> dict[str, Any]:
        keys = pyautogui_keys(key)
        if not keys:
            return {"ok": False, "action": "key", "failure_reason": "invalid_key", "desktop_surface": True}
        before = self._screenshot()
        presses = max(1, min(200, int(repeat or 1)))
        hold = max(0, min(5000, int(hold_ms or 0)))
        if hold:
            code = (
                f"for _ in range({presses}):\n"
                + "".join(f"    pyautogui.keyDown({k!r})\n" for k in keys)
                + f"    time.sleep({hold / 1000:.3f})\n"
                + "".join(f"    pyautogui.keyUp({k!r})\n" for k in reversed(keys))
            )
        else:
            code = f"for _ in range({presses}): pyautogui.hotkey(*{keys!r}); time.sleep(0.03)"
        result = self._pyautogui(code)
        time.sleep(0.3)
        after = self._screenshot()
        tree = self._tree()
        focused = tree.get("focused") or {}
        active = next((item for item in tree["windows"] if item.get("active")), {})
        return {"ok": result.get("status") == "success", "action": "key", "surface_changed": _changed(before, after),
                "focused_rid": focused.get("selector", ""), "active_window_title": active.get("title", ""),
                "active_typable": bool(focused.get("editable")), "presses": presses, "hold_ms": hold, "desktop_surface": True}

    async def vision_key(self, key: str, *, repeat: int = 1, hold_ms: int = 0, **_: Any) -> dict[str, Any]:
        return await asyncio.to_thread(self._key_sync, str(key), repeat, hold_ms)

    def _drag_sync(self, x: int, y: int, to_x: int, to_y: int, button: str, path: list[Any], hold_ms: int, modifiers: list[str]) -> dict[str, Any]:
        mods = [_MODIFIERS[m] for m in (str(item).lower() for item in modifiers or []) if m in _MODIFIERS]
        button = button if button in {"left", "right", "middle"} else "left"
        before = self._screenshot()
        points = [(int(p[0]), int(p[1])) if isinstance(p, (list, tuple)) else (int(p.get("x", 0)), int(p.get("y", 0))) for p in path or []]
        code = f"pyautogui.moveTo({x}, {y}); " + "".join(f"pyautogui.keyDown({m!r}); " for m in mods)
        code += f"pyautogui.mouseDown(button={button!r}); time.sleep({max(0.05, min(5.0, (hold_ms or 0) / 1000)):.3f}); "
        code += "".join(f"pyautogui.moveTo({px}, {py}, duration=0.15); " for px, py in points)
        code += f"pyautogui.moveTo({to_x}, {to_y}, duration=0.25); pyautogui.mouseUp(button={button!r}); "
        code += "".join(f"pyautogui.keyUp({m!r}); " for m in reversed(mods))
        result = self._pyautogui(code)
        time.sleep(0.3)
        after = self._screenshot()
        return {"ok": result.get("status") == "success", "action": "drag", "activation_attempted": True,
                "activation_dispatched": result.get("status") == "success", "surface_changed": _changed(before, after), "desktop_surface": True}

    async def vision_drag(self, x: int, y: int, to_x: int, to_y: int, *, button: str = "left", path: list[Any] | None = None,
                          hold_ms: int = 0, modifiers: list[str] | None = None, **_: Any) -> dict[str, Any]:
        return await asyncio.to_thread(self._drag_sync, int(x), int(y), int(to_x), int(to_y), button, list(path or []), hold_ms, list(modifiers or []))

    async def vision_hover(self, x: int, y: int, *, dwell_ms: int = 0, **_: Any) -> dict[str, Any]:
        before = await asyncio.to_thread(self._screenshot)
        await asyncio.to_thread(self._pyautogui, f"pyautogui.moveTo({int(x)}, {int(y)}, duration=0.1)")
        await asyncio.sleep(max(0.45, max(0, min(5000, int(dwell_ms or 0))) / 1000))
        after = await asyncio.to_thread(self._screenshot)
        return {"ok": True, "action": "hover", "surface_changed": _changed(before, after), "desktop_surface": True}

    def _select_text_sync(self, text: str, x: int, y: int) -> dict[str, Any]:
        # The Windows body's screen-text route, on the VM's pixels: OCR finds the occurrence of `text`
        # nearest the model's point, then a click just before its first character and a shift-click
        # just after its last select it (never a drag, which could move an earlier selection).
        from app.desktop_body import _changed_outside, _ocr_locate

        before = self._screenshot()
        width, height = before.size
        located = _ocr_locate(before, text, int(x), int(y), reach=(1000, 400))
        if not located.get("found"):
            return {"ok": False, "action": "select_text", "mode": "screen_text", "failure_reason": "select_text_not_found",
                    "occurrences": 0, "desktop_surface": True}
        (from_x, from_y), (to_x, to_y) = located["from"], located["to"]
        start = a11y.control_at(self._tree()["controls"], int(from_x), int(from_y))
        if start and start.get("role") in a11y.NOT_TEXT_ROLES:
            # A press there acts on the item (a button, a list row, a spreadsheet cell): never a text selection.
            return {"ok": False, "action": "select_text", "mode": "screen_text", "failure_reason": "select_text_not_text",
                    "control_type": start.get("role"), "desktop_surface": True}
        # The pause between the clicks outlasts GNOME's double-click time (a quick second click selects words).
        code = (
            f"pyautogui.click({int(from_x)}, {int(from_y)}); time.sleep(0.6); "
            f"pyautogui.keyDown('shift'); pyautogui.click({int(to_x)}, {int(to_y)}); pyautogui.keyUp('shift')"
        )
        result = self._pyautogui(code)
        self._last_click = (int(to_x), int(to_y))
        dispatched = result.get("status") == "success" and not result.get("returncode")
        time.sleep(0.25)
        after = self._screenshot()
        left, top, right, bottom = located["box"]
        pad = 3
        text_box = (max(0, int(left) - pad), max(0, int(top) - pad), min(width, int(right) + pad), min(height, int(bottom) + pad))
        return {
            "ok": dispatched,
            "action": "select_text",
            "mode": "screen_text",
            "selected_text": " ".join(str(text).split())[:300],
            "occurrences": int(located.get("occurrences") or 1),
            **({"read_approximately": True} if located.get("approximate") else {}),
            **({} if dispatched else {"failure_reason": "osworld_execute_failed"}),
            "highlight_changed": _changed(before, after, text_box),
            "changed_outside_text": _changed_outside(before, after, text_box),
            "surface_changed": _changed(before, after),
            "activation_attempted": True,
            "activation_dispatched": dispatched,
            "desktop_surface": True,
        }

    async def vision_select_text(self, text: str, x: int, y: int, **_: Any) -> dict[str, Any]:
        return await asyncio.to_thread(self._select_text_sync, str(text), int(x), int(y))

    def _focus_sync(self, window_id: str) -> dict[str, Any]:
        window = next((item for item in self._windows if item.get("window_id") == window_id), None)
        if window is None:
            return {"ok": False, "action": "focus_window", "failure_reason": "desktop_window_stale", "desktop_surface": True}
        if self._has_tool("wmctrl"):
            self._execute(["wmctrl", "-a", window["title"]])
        elif self._has_tool("xdotool"):
            self._execute(["xdotool", "search", "--name", window["title"], "windowactivate"])
        else:
            return {"ok": False, "action": "focus_window", "failure_reason": "osworld_window_focus_unavailable", "desktop_surface": True}
        time.sleep(0.3)
        tree = self._tree()
        active = next((item for item in tree["windows"] if item.get("active")), {})
        focused = active.get("title") == window["title"]
        return {"ok": focused, "action": "focus_window", "focus_changed": focused, "title_changed": focused, "desktop_surface": True}

    async def vision_focus_window(self, window_id: str, **_: Any) -> dict[str, Any]:
        return await asyncio.to_thread(self._focus_sync, str(window_id))

    def _launch_sync(self, app_id: str) -> dict[str, Any]:
        target = next((item for item in self._applications() if item["app_id"] == app_id), None)
        if target is None:
            return {"ok": False, "action": "launch_app", "failure_reason": "desktop_app_target_stale", "desktop_surface": True}
        before = {(item.get("app"), item.get("title")) for item in self._tree()["windows"]}
        result = self._execute(["python", "-c", _LAUNCH_SCRIPT, target["app_id"], target["path"]])
        try:
            lines = str(result.get("output") or "").strip().splitlines()
            launched = json.loads(lines[-1]) if result.get("status") == "success" and lines else {}
        except Exception:
            launched = {}
        if not isinstance(launched, dict) or launched.get("launched") is not True:
            return {"ok": False, "action": "launch_app", "failure_reason": "osworld_launch_failed", "launched_app_id": app_id, "desktop_surface": True}
        # Window identities are tree paths, which shift as applications start; a new window is a
        # (application, title) pair the desktop did not show before.
        deadline = time.monotonic() + 8
        new_window: dict[str, Any] | None = None
        while new_window is None and time.monotonic() < deadline:
            time.sleep(0.5)
            windows = self._tree()["windows"]
            new_window = next((item for item in windows if (item.get("app"), item.get("title")) not in before), None)
        return {
            "ok": True,
            "action": "launch_app",
            "surface_changed": bool(new_window),
            "title_changed": bool(new_window),
            "launched_app_id": app_id,
            "launched_via": str(launched.get("via") or ""),
            "new_window_id": (new_window or {}).get("window_id"),
            "new_window_title": (new_window or {}).get("title"),
            "desktop_surface": True,
        }

    async def vision_launch_app(self, app_id: str, **_: Any) -> dict[str, Any]:
        return await asyncio.to_thread(self._launch_sync, str(app_id or ""))

    def _window_text_counts(self, wanted: str) -> tuple[int | None, int | None]:
        """How many times the front window shows `wanted` (any letter case): in its accessibility text,
        and in its pixels read by OCR (None when that reading failed or this host has no OCR)."""
        from app import screen_ocr

        try:
            image = self._screenshot()
            tree = self._tree()
        except Exception:
            return None, None
        tree_count = _text_count(tree.get("active_text") or "", wanted) if tree.get("xml") else None
        ocr_count = None
        if screen_ocr.available():
            try:
                box = tree.get("active_box")
                if box:
                    x, y, w, h = box
                    left, top = max(0, x), max(0, y)
                    right, bottom = min(image.width, x + w), min(image.height, y + h)
                    if right > left and bottom > top:
                        image = image.crop((left, top, right, bottom))
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
                lines = (screen_ocr.read_lines([buffer.getvalue()]) or [[]])[0]
                ocr_count = _text_count(" ".join(str(line) for line in lines), wanted)
            except Exception:
                ocr_count = None
        return tree_count, ocr_count

    async def _wait_for_window_text(self, wanted: str, duration: int) -> dict[str, Any]:
        """Wait until the front window shows `wanted` more times than it did when the wait began, in its
        accessibility text or its OCR-read pixels (each reading compared only with its own earlier
        readings). Copies already shown at the start never satisfy it (present_at_start)."""
        started = time.monotonic()
        lowest = list(await asyncio.to_thread(self._window_text_counts, wanted))
        present_at_start = max(count or 0 for count in lowest)
        while (time.monotonic() - started) * 1000 < duration:
            await asyncio.sleep(0.4)
            counts = await asyncio.to_thread(self._window_text_counts, wanted)
            if any(count is not None and low is not None and count > low for count, low in zip(counts, lowest)):
                return {
                    "ok": True,
                    "action": "wait",
                    "waited_ms": int((time.monotonic() - started) * 1000),
                    "text_matched": True,
                    "present_at_start": present_at_start,
                    "desktop_surface": True,
                }
            lowest = [
                count if low is None else low if count is None else min(low, count)
                for count, low in zip(counts, lowest)
            ]
        return {
            "ok": False,
            "action": "wait",
            "failure_reason": "wait_text_not_seen",
            "waited_ms": int((time.monotonic() - started) * 1000),
            "text_matched": False,
            "present_at_start": present_at_start,
            "desktop_surface": True,
        }

    async def vision_wait(self, wait_ms: int, *, text: str = "", **_: Any) -> dict[str, Any]:
        duration = max(100, min(30000, int(wait_ms or 750)))
        wanted = " ".join(str(text or "").split()).casefold()
        if wanted:
            return await self._wait_for_window_text(wanted, duration)
        before = await asyncio.to_thread(self._screenshot)
        await asyncio.sleep(duration / 1000)
        after = await asyncio.to_thread(self._screenshot)
        return {"ok": True, "action": "wait", "wait_ms": duration, "surface_changed": _changed(before, after), "desktop_surface": True}

    # --- downloads (the VM's ~/Downloads, read in the VM) --------------------------------------------

    def _downloads_scan(self) -> dict[str, Any]:
        result = self._execute(["python3", "-c", _DOWNLOADS_SCAN_SCRIPT])
        try:
            scan = json.loads(str(result.get("output") or "").strip().splitlines()[-1])
        except Exception:  # noqa: BLE001 - an unreadable scan lists nothing
            return {}
        return scan if isinstance(scan, dict) else {}

    def downloads_snapshot(self) -> dict[str, Any]:
        from app import run_downloads

        scan = self._downloads_scan()
        return run_downloads.make_snapshot(scan) if scan.get("folder") else {}

    def downloads_listing(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        from app import run_downloads

        # The VM's files carry no Windows Mark of the Web; its creation-time check still applies.
        return run_downloads.list_downloads(snapshot, self._downloads_scan(), require_mark_of_the_web=False)

    def _open_file_sync(self, path: str) -> dict[str, Any]:
        from app import run_downloads

        if run_downloads.file_extension(path) not in run_downloads.OPENABLE_EXTENSIONS:
            return {"ok": False, "action": "open_file", "failure_reason": "open_file_type_not_allowed", "desktop_surface": True}
        before = {item.get("window_id") for item in self._tree()["windows"]}
        result = self._execute(
            ["python3", "-c", _OPEN_FILE_SCRIPT, str(path), ",".join(sorted(run_downloads.OPENABLE_EXTENSIONS))]
        )
        if "opened" not in str(result.get("output") or ""):
            return {"ok": False, "action": "open_file", "failure_reason": "open_file_type_not_allowed", "desktop_surface": True}
        new_window: dict[str, Any] = {}
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline and not new_window:
            time.sleep(0.5)
            new_window = next((item for item in self._tree()["windows"] if item.get("window_id") not in before), {})
        return {
            "ok": True,
            "action": "open_file",
            "opened_file": path.rsplit("/", 1)[-1],
            "surface_changed": bool(new_window),
            "title_changed": bool(new_window),
            "new_window_id": new_window.get("window_id"),
            "new_window_title": new_window.get("title"),
            "desktop_surface": True,
        }

    async def vision_open_file(self, path: str, **_: Any) -> dict[str, Any]:
        return await asyncio.to_thread(self._open_file_sync, str(path))

    async def vision_upload(self, x: int, y: int, document: dict[str, Any], **_: Any) -> dict[str, Any]:
        return {"ok": False, "action": "upload", "failure_reason": "osworld_upload_unsupported", "desktop_surface": True}

    async def vision_upload_control(self, selector: str, document: dict[str, Any], fallback_x: int, fallback_y: int, **_: Any) -> dict[str, Any]:
        return {"ok": False, "action": "upload", "failure_reason": "osworld_upload_unsupported", "desktop_surface": True}
