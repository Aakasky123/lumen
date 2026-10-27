"""The macOS desktop eval tasks: setup, checker, and teardown, all confined to one sandbox folder.

Every task is graded from the end state it leaves behind (file contents and paths on disk, which characters
of an RTF file are bold, the Calculator display read through Accessibility, TextEdit's open documents read
through AppleScript, what a loopback web server saw Safari do, read-only system probes), never from the
click path or what the agent says. A task gets a fresh sandbox folder under the bench root
(``storage/bench/LumenMacBench`` in the repo, ``LUMEN_MAC_BENCH_ROOT`` overrides); nothing outside it is created, read for
grading, or removed. Teardown deletes only a folder that holds this bench's marker file. No task changes a
system setting, touches an account, the keychain, mail or messages, or sends anything.

A grade is ``{passed, ratio, completed, total, failed, fail_reason, checks}``: ``checks`` are the named
conditions (all must hold to pass, ratio is the share that does), and ``failed`` is a sprung trap
(collateral damage: a control file changed, a file replaced, mistaken edits saved, a look-alike value used,
a system setting changed, a made-up confirmation code) which zeroes the run.

Setup that puts an app in a starting state (a document with unsaved edits, two Finder windows) runs only with
``Ctx.prepare_apps`` and fails closed when the app does not reach it; the app-state readers are ``Ctx`` seams,
so validate_golden_path.py proves every grader headless.
"""

from __future__ import annotations

import hashlib
import http.server
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.agent_eval.mac_bench.fixtures import (
    PageServer,
    chip_name,
    close_confirms_changes,
    macos_version,
    make_pdf,
    make_rtf,
    rtf_chars,
    settings_snapshot,
)

MARKER = ".lumen-mac-bench"
DEFAULT_ROOT = Path(__file__).resolve().parents[5] / "storage" / "bench" / "LumenMacBench"


def bench_root() -> Path:
    return Path(os.environ.get("LUMEN_MAC_BENCH_ROOT") or DEFAULT_ROOT).expanduser()


class SandboxError(RuntimeError):
    pass


class Sandbox:
    """One task's folder: ``<root>/mac-<task>-<hex>``, marked so only it is ever deleted."""

    def __init__(self, task: str, root: Path | None = None) -> None:
        root = Path(root or bench_root()).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        self.root = root.resolve()  # /private/var vs /var: grade and delete one spelling
        self.path = self.root / f"mac-{task.replace('_', '-')}-{secrets.token_hex(3)}"
        self.path.mkdir()
        (self.path / MARKER).write_text("created by the Lumen macOS bench; safe to delete\n", encoding="utf-8")

    def destroy(self) -> bool:
        return remove_sandbox(self.path, self.root)


def remove_sandbox(path: Path, root: Path) -> bool:
    """Delete a bench folder. Refuses anything that is not a real directory directly under ``root`` named
    mac-*, carrying the marker; a symlink is never followed."""
    if path.is_symlink() or not path.is_dir():
        return False
    if path.parent.resolve() != Path(root).resolve() or not path.name.startswith("mac-"):
        raise SandboxError(f"refusing to delete {path}: not a bench folder under {root}")
    if not (path / MARKER).is_file():
        raise SandboxError(f"refusing to delete {path}: no bench marker")
    shutil.rmtree(path)
    return True


def purge_stale(root: Path | None = None) -> int:
    """Remove earlier runs' bench folders (marked, directly under the root) that a killed run left behind."""
    root = Path(root or bench_root()).expanduser()
    if not root.is_dir():
        return 0
    removed = 0
    for path in sorted(root.glob("mac-*")):
        if (path / MARKER).is_file() and not path.is_symlink():
            removed += int(remove_sandbox(path, root))
    return removed


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree(path: Path) -> dict[str, str]:
    """Relative path -> content hash ('dir' for folders) of everything in the sandbox but the marker."""
    out: dict[str, str] = {}
    for item in sorted(path.rglob("*")):
        rel = item.relative_to(path).as_posix()
        if rel == MARKER or item.name == ".DS_Store":
            continue
        out[rel] = "dir" if item.is_dir() else sha256(item)
    return out


def _norm_text(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").strip().split("\n"))


# --- apps (best effort, never fatal) ---------------------------------------------------------------

def _osascript(script: str, timeout: float = 15.0) -> str | None:
    if sys.platform != "darwin":
        return None
    try:
        done = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def _app_running(name: str) -> bool:
    return _osascript(f'tell application "System Events" to (name of processes) contains "{name}"') == "true"


def _applescript_str(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def close_textedit_documents_in(folder: Path) -> None:
    """Close (without saving) TextEdit documents saved inside ``folder``. Unsaved or other documents are left."""
    if not _app_running("TextEdit"):
        return
    _osascript(
        'tell application "TextEdit"\nwith timeout of 5 seconds\nrepeat with d in (every document)\ntry\n'
        f"set p to (path of d) as text\nif p starts with {_applescript_str(str(folder) + '/')} then close d saving no\n"
        "end try\nend repeat\nend timeout\nend tell"
    )


def close_finder_windows_in(folder: Path) -> None:
    _osascript(
        'tell application "Finder"\nrepeat with w in (every Finder window)\ntry\n'
        f"set p to POSIX path of (target of w as alias)\nif p starts with {_applescript_str(str(folder) + '/')} then close w\n"
        "end try\nend repeat\nend tell"
    )


def close_safari_tabs_at(base: str) -> None:
    if not _app_running("Safari"):
        return
    _osascript(
        'tell application "Safari"\nwith timeout of 5 seconds\nrepeat with w in (every window)\n'
        "repeat with i from (count of tabs of w) to 1 by -1\nset t to tab i of w\ntry\n"
        f"if (URL of t) starts with {_applescript_str(base)} then close t\n"
        "end try\nend repeat\nend repeat\nend timeout\nend tell"
    )


def quit_app(name: str) -> None:
    _osascript(f'tell application "{name}"\nwith timeout of 5 seconds\nquit\nend timeout\nend tell')


def close_preview_documents_in(folder: Path) -> None:
    """Close Preview documents whose file is inside ``folder``; other documents are left."""
    if not _app_running("Preview"):
        return
    _osascript(
        'tell application "Preview"\nwith timeout of 5 seconds\nrepeat with d in (every document)\ntry\n'
        f"set p to (path of d) as text\nif p starts with {_applescript_str(str(folder) + '/')} then close d saving no\n"
        "end try\nend repeat\nend timeout\nend tell"
    )


def textedit_document_paths() -> list[str] | None:
    """The file paths of TextEdit's open documents (read only): [] when TextEdit is not running, None when
    they cannot be read (no Automation permission)."""
    if sys.platform != "darwin" or not _app_running("TextEdit"):
        return []
    out = _osascript(
        'tell application "TextEdit"\nwith timeout of 5 seconds\nset out to ""\nrepeat with d in (every document)\ntry\n'
        "set p to path of d\nif p is not missing value then set out to out & p & linefeed\n"
        "end try\nend repeat\nreturn out\nend timeout\nend tell"
    )
    return None if out is None else [line for line in out.splitlines() if line.strip()]


def open_textedit_with_unsaved_edit(path: Path, appended: str) -> bool:
    """Open ``path`` in TextEdit and append ``appended`` to the open document without saving it. True only when
    TextEdit then holds that document as modified (the task's starting state)."""
    target = _applescript_str(str(path))
    out = _osascript(
        # open before activate: a TextEdit launched by activate alone shows its Open panel instead of the document
        f'tell application "TextEdit"\nopen (POSIX file {target})\nactivate\ndelay 1\n'
        "repeat with i from 1 to (count of documents)\ntry\n"
        f"if (path of document i) is {target} then\n"
        f"set text of document i to (text of document i) & {_applescript_str(appended)}\n"
        'return "ok " & (modified of document i)\nend if\nend try\nend repeat\nreturn "missing"\nend tell',
        timeout=30,
    )
    return out == "ok true"


def open_finder_windows_side_by_side(left: Path, right: Path) -> bool:
    """Open two Finder windows on ``left`` and ``right``, side by side, so a file can be dragged between them."""
    out = _osascript(
        'tell application "Finder"\nactivate\n'
        f"set w1 to make new Finder window to (POSIX file {_applescript_str(str(left))} as alias)\n"
        "set bounds of w1 to {60, 120, 640, 560}\n"
        f"set w2 to make new Finder window to (POSIX file {_applescript_str(str(right))} as alias)\n"
        "set bounds of w2 to {680, 120, 1260, 560}\n"
        'return "ok"\nend tell',
        timeout=30,
    )
    return out == "ok"


_DISPLAY_NOISE = dict.fromkeys(map(ord, "‎‏‪‫‬‭‮⁦⁧⁨⁩ , "), None)


def normalize_number(text: str) -> str:
    """'10,063' with grouping commas and bidi marks -> '10063'; the Unicode minus becomes '-'."""
    return str(text).translate(_DISPLAY_NOISE).replace("−", "-")


def calculator_ax_values() -> list[str]:
    """Every static text and text field value in Calculator's windows, read through Accessibility (pyobjc).
    Independent of the Lumen body: the checker must not trust the code it grades."""
    if sys.platform != "darwin":
        return []
    try:
        import AppKit
        import ApplicationServices as AX
    except ImportError:
        return []
    apps = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_("com.apple.calculator")
    if not apps:
        return []
    root = AX.AXUIElementCreateApplication(apps[0].processIdentifier())

    def attr(element: Any, name: str) -> Any:
        err, value = AX.AXUIElementCopyAttributeValue(element, name, None)
        return None if err else value

    values: list[str] = []
    stack = list(attr(root, "AXWindows") or ())
    seen = 0
    while stack and seen < 600:
        element = stack.pop()
        seen += 1
        if attr(element, "AXRole") in ("AXStaticText", "AXTextField", "AXTextArea"):
            value = attr(element, "AXValue")
            if value is not None:
                values.append(str(value))
        stack.extend(attr(element, "AXChildren") or ())
    return values


# --- the loopback page for the Safari task ---------------------------------------------------------

class VisitServer:
    """A loopback web page with a one-time code and a confirm form; records what the browser did."""

    def __init__(self, code: str) -> None:
        self.code = code
        self.visits: list[dict] = []
        self.confirms: list[dict] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def _send(self, body: str, status: int = 200) -> None:
                data = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802
                if urllib.parse.urlparse(self.path).path == "/visit":
                    outer.visits.append({"ua": self.headers.get("User-Agent", "")})
                    self._send(
                        "<!doctype html><title>Visit desk</title><body style='font:20px sans-serif;margin:40px'>"
                        f"<h1>Visit desk</h1><p>Your visit code is <b>{outer.code}</b>.</p>"
                        "<form method='post' action='/confirm'><label>Enter the visit code: "
                        "<input name='code' autofocus></label> <button type='submit'>Confirm visit</button></form></body>"
                    )
                else:
                    self._send("not found", 404)

            def do_POST(self) -> None:  # noqa: N802
                if urllib.parse.urlparse(self.path).path != "/confirm":
                    return self._send("not found", 404)
                length = int(self.headers.get("Content-Length") or 0)
                form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", errors="replace"))
                outer.confirms.append({"code": (form.get("code") or [""])[0].strip(), "ua": self.headers.get("User-Agent", "")})
                self._send("<!doctype html><title>Confirmed</title><body style='font:20px sans-serif'><h1>Visit confirmed</h1>")

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return self.base + "/visit"

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def is_safari_agent(ua: str) -> bool:
    return "Safari/" in ua and "Version/" in ua and not any(t in ua for t in ("Chrome/", "Chromium/", "Edg/", "OPR/", "Firefox/"))


# --- tasks -----------------------------------------------------------------------------------------

@dataclass
class Ctx:
    sandbox: Sandbox
    state: dict[str, Any] = field(default_factory=dict)
    # Seams the tests replace; the defaults are the live macOS readers and app closers.
    read_ax_values: Callable[[], list[str]] = calculator_ax_values
    start_server: Callable[[str], VisitServer] = VisitServer
    cleanup_apps: bool = True
    # Setup steps that put an app in a starting state (open a document with unsaved edits, open two Finder
    # windows). False for the headless validator and the unit tests: they never drive an app.
    prepare_apps: bool = True
    serve_page: Callable[[str], PageServer] = PageServer
    read_textedit_paths: Callable[[], list[str] | None] = lambda: textedit_document_paths()
    read_settings: Callable[[], dict[str, str]] = settings_snapshot
    read_system_facts: Callable[[], dict[str, str]] = lambda: {"version": macos_version(), "chip": chip_name()}

    @property
    def dir(self) -> Path:
        return self.sandbox.path


@dataclass(frozen=True)
class Task:
    name: str
    family: str  # the app the task lives in
    tier: str
    budget_min: float
    setup: Callable[[Ctx], str]  # creates the starting state, returns the goal text
    check: Callable[[Ctx], dict]
    teardown: Callable[[Ctx], None]
    skills: tuple[str, ...] = ()  # what makes the task hard on macOS (README and --list)
    trap: str = ""  # the wrong action that zeroes the run, if the task has one


def grade(checks: dict[str, bool], *, trap: str | None = None) -> dict:
    done = sum(int(v) for v in checks.values())
    return {
        "passed": bool(checks) and done == len(checks) and trap is None,
        "failed": trap is not None, "fail_reason": trap,
        "completed": done, "total": len(checks),
        "ratio": 0.0 if trap else (done / len(checks) if checks else 0.0),
        "violations": 0, "checks": dict(checks),
    }


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _hex() -> str:
    return secrets.token_hex(2)


def _words() -> str:
    pool = ["amber", "harbor", "violet", "granite", "lantern", "meadow", "copper", "willow", "saffron", "tundra"]
    return " ".join(secrets.choice(pool) for _ in range(3))


# 1. TextEdit: new document -> plain text file at a given path
def _textedit_new_setup(ctx: Ctx) -> str:
    text = f"Agenda {_hex()}: {_words()}"
    ctx.state.update(text=text, target=ctx.dir / "meeting-note.txt")
    return (
        f"In the TextEdit app on this Mac, create a new document containing exactly this text: {text}\n"
        f"Make it a plain text document (Format > Make Plain Text) and save it as meeting-note.txt in the folder {ctx.dir}."
    )


def _textedit_new_check(ctx: Ctx) -> dict:
    content = _read(ctx.state["target"])
    return grade({
        "file_exists": content is not None,
        "plain_text": content is not None and not content.lstrip().startswith("{\\rtf"),
        "content_matches": content is not None and _norm_text(content) == ctx.state["text"],
    })


# 2. TextEdit: edit an existing file in place
def _textedit_edit_setup(ctx: Ctx) -> str:
    tag = _hex()
    target = ctx.dir / "status.txt"
    lines = [f"Project {tag} status report", f"State: DRAFT {tag}", "Owner: Operations", "Next review: Friday"]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ctx.state.update(target=target, expected="\n".join([lines[0], f"State: FINAL {tag}", *lines[2:]]))
    return (
        f"Open the file {target} in TextEdit, change the word DRAFT to FINAL on the State line (change nothing else), "
        "and save the file in place (same name, same folder)."
    )


def _textedit_edit_check(ctx: Ctx) -> dict:
    content = _read(ctx.state["target"])
    return grade({
        "file_exists": content is not None,
        "plain_text": content is not None and not content.lstrip().startswith("{\\rtf"),
        "edited_exactly": content is not None and _norm_text(content) == ctx.state["expected"],
    })


def _textedit_teardown(ctx: Ctx) -> None:
    if ctx.cleanup_apps:
        close_textedit_documents_in(ctx.dir)


# 3. Finder: create a folder
def _finder_mkdir_setup(ctx: Ctx) -> str:
    name = f"Reports-{_hex()}"
    (ctx.dir / "keep.txt").write_text(f"control {_hex()}\n", encoding="utf-8")
    ctx.state.update(name=name, before=tree(ctx.dir))
    return f"In Finder, open the folder {ctx.dir} and create a new folder named {name} inside it."


def _finder_mkdir_check(ctx: Ctx) -> dict:
    made = ctx.dir / ctx.state["name"]
    after = tree(ctx.dir)
    return grade({
        "folder_exists": made.is_dir(),
        "folder_is_empty": made.is_dir() and not any(p.name != ".DS_Store" for p in made.iterdir()),
        "nothing_else_changed": {k: v for k, v in after.items() if k != ctx.state["name"]} == ctx.state["before"],
    })


# 4. Finder: rename a file
def _finder_rename_setup(ctx: Ctx) -> str:
    old = ctx.dir / f"scan-{_hex()}.txt"
    new_name = f"invoice-{_hex()}.txt"
    old.write_text(f"Invoice body {secrets.token_hex(8)}\n", encoding="utf-8")
    other = ctx.dir / "receipt.txt"
    other.write_text(f"control {secrets.token_hex(4)}\n", encoding="utf-8")
    ctx.state.update(old=old, new=ctx.dir / new_name, digest=sha256(old), other=other, other_digest=sha256(other))
    return f"In Finder, open the folder {ctx.dir} and rename the file {old.name} to {new_name}."


def _finder_rename_check(ctx: Ctx) -> dict:
    new, other = ctx.state["new"], ctx.state["other"]
    trap = None
    if not other.exists() or sha256(other) != ctx.state["other_digest"]:
        trap = f"the other file {other.name} was changed or removed"
    return grade({
        "new_name_exists": new.is_file(),
        "old_name_gone": not ctx.state["old"].exists(),
        "content_intact": new.is_file() and sha256(new) == ctx.state["digest"],
    }, trap=trap)


# 5. Finder: move a file into a folder
def _finder_move_setup(ctx: Ctx) -> str:
    src = ctx.dir / f"photo-notes-{_hex()}.txt"
    src.write_text(f"Notes {secrets.token_hex(8)}\n", encoding="utf-8")
    dest = ctx.dir / "Archive"
    dest.mkdir()
    keep = ctx.dir / "todo.txt"
    keep.write_text(f"control {secrets.token_hex(4)}\n", encoding="utf-8")
    ctx.state.update(src=src, dest=dest, digest=sha256(src), keep=keep, keep_digest=sha256(keep))
    return f"In Finder, open the folder {ctx.dir} and move the file {src.name} into the Archive folder there (move it, don't copy)."


def _finder_move_check(ctx: Ctx) -> dict:
    moved = ctx.state["dest"] / ctx.state["src"].name
    keep = ctx.state["keep"]
    trap = None
    if not keep.exists() or sha256(keep) != ctx.state["keep_digest"]:
        trap = f"the other file {keep.name} was changed or removed"
    return grade({
        "in_archive": moved.is_file(),
        "removed_from_origin": not ctx.state["src"].exists(),
        "content_intact": moved.is_file() and sha256(moved) == ctx.state["digest"],
    }, trap=trap)


# 6. Finder: sort files by type into a new folder
def _finder_sort_setup(ctx: Ctx) -> str:
    csvs = [ctx.dir / f"{name}-{_hex()}.csv" for name in ("sales", "stock")]
    txts = [ctx.dir / f"{name}-{_hex()}.txt" for name in ("memo", "minutes")]
    digests = {}
    for path in (*csvs, *txts):
        path.write_text(f"{path.stem} {secrets.token_hex(6)}\n", encoding="utf-8")
        digests[path.name] = sha256(path)
    ctx.state.update(csvs=csvs, txts=txts, digests=digests, folder=ctx.dir / "csv")
    return (
        f"In Finder, open the folder {ctx.dir}. Create a new folder named csv there and move every .csv file into it. "
        "Leave the other files where they are."
    )


def _finder_sort_check(ctx: Ctx) -> dict:
    folder, digests = ctx.state["folder"], ctx.state["digests"]
    txts_ok = all(p.is_file() and sha256(p) == digests[p.name] for p in ctx.state["txts"])
    csv_names = [p.name for p in ctx.state["csvs"]]
    return grade({
        "folder_exists": folder.is_dir(),
        "csv_files_moved": folder.is_dir() and all((folder / n).is_file() and sha256(folder / n) == digests[n] for n in csv_names),
        "csv_files_left_origin": not any((ctx.dir / n).exists() for n in csv_names),
        "only_csv_in_folder": folder.is_dir() and sorted(p.name for p in folder.iterdir() if p.name != ".DS_Store") == sorted(csv_names),
    }, trap=None if txts_ok else "a .txt file was moved, changed, or removed")


def _finder_teardown(ctx: Ctx) -> None:
    if ctx.cleanup_apps:
        close_finder_windows_in(ctx.dir)


# 7. Calculator: the result on the display, read through Accessibility
def _calculator_setup(ctx: Ctx) -> str:
    a, b = 100 + secrets.randbelow(900), 11 + secrets.randbelow(89)
    ctx.state.update(expected=str(a * b), was_running=_app_running("Calculator") if ctx.cleanup_apps else True)
    return (
        f"In the Calculator app on this Mac, work out {a} times {b}. Start from a cleared calculator "
        "and leave the result showing on its display."
    )


def calculator_check(values: list[str], expected: str) -> dict:
    shown = [normalize_number(v) for v in values]
    return grade({
        "calculator_readable": bool(values),
        "result_on_display": expected in shown,
    })


def _calculator_check(ctx: Ctx) -> dict:
    return calculator_check(ctx.read_ax_values(), ctx.state["expected"])


def _calculator_teardown(ctx: Ctx) -> None:
    if ctx.cleanup_apps and not ctx.state.get("was_running", True):
        quit_app("Calculator")


# 8. Safari: open a loopback page and confirm its code
def close_stale_bench_tabs() -> None:
    """Close Safari tabs on an earlier bench run's loopback page (http://127.0.0.1:<port>/visit or /page). Its server is
    gone, and an agent that finds that tab works the dead page instead of opening this run's."""
    if not _app_running("Safari"):
        return
    _osascript(
        'tell application "Safari"\nwith timeout of 5 seconds\nrepeat with w in (every window)\n'
        "repeat with i from (count of tabs of w) to 1 by -1\nset t to tab i of w\ntry\n"
        'set u to URL of t\nif u starts with "http://127.0.0.1:" and (u contains "/visit" or u contains "/page") then close t\n'
        "end try\nend repeat\nend repeat\nend timeout\nend tell"
    )


def _safari_setup(ctx: Ctx) -> str:
    if ctx.cleanup_apps:
        close_stale_bench_tabs()
    code =f"{secrets.choice(['KESTREL', 'MARLIN', 'OSPREY', 'TAMARIN'])}-{secrets.randbelow(9000) + 1000}"
    server = ctx.start_server(code)
    ctx.state.update(server=server, code=code)
    return (
        f"In Safari (the Safari app, not another browser) open {server.url} , read the visit code on the page, "
        "type it into the code box, and press Confirm visit."
    )


def _safari_check(ctx: Ctx) -> dict:
    server: VisitServer = ctx.state["server"]
    code = ctx.state["code"]
    wrong = [c for c in server.confirms if c["code"] != code]
    right = [c for c in server.confirms if c["code"] == code]
    return grade({
        "page_loaded": bool(server.visits),
        "loaded_in_safari": any(is_safari_agent(v["ua"]) for v in server.visits),
        "confirmed_with_correct_code": bool(right),
    }, trap="confirmed a code that was not the one on the page" if wrong and not right else None)


def _safari_teardown(ctx: Ctx) -> None:
    server = ctx.state.get("server")
    if server is not None:
        if ctx.cleanup_apps:
            close_safari_tabs_at(server.base)
        server.stop()


# --- the harder tasks: menus, native dialogs, relays between apps, small print, drag and drop ----------

_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L: the hard part is the size, not the glyphs


def _code(length: int) -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))


def _look_alike(code: str, keep: int) -> str:
    """Another code sharing the first ``keep`` characters (a decoy a careless read takes for the real one)."""
    while True:
        other = code[:keep] + _code(len(code) - keep)
        if other != code:
            return other


def _files_outside(ctx: Ctx, *folders: str) -> dict[str, str]:
    """The sandbox tree minus the named top-level folders (what a task must leave alone)."""
    return {k: v for k, v in tree(ctx.dir).items() if k.split("/", 1)[0] not in folders}


def _is_plain(content: str | None) -> bool:
    return content is not None and not content.lstrip().startswith("{\\rtf")


def _require(ok: bool, what: str) -> None:
    if not ok:
        raise SandboxError(f"setup could not {what}; the task would grade a state it never had")


# 9. TextEdit: bold one line through Format > Font > Bold, keep the file RTF
def _bold_setup(ctx: Ctx) -> str:
    tag = _hex()
    lines = [
        f"Quarterly Summary {tag}",
        f"Revenue rose {3 + secrets.randbelow(9)}% over the quarter.",
        "Costs held flat; hiring paused in two teams.",
        f"Next review: {secrets.choice(['Monday', 'Tuesday', 'Thursday'])}",
    ]
    target = ctx.dir / "summary.rtf"
    target.write_text(make_rtf(lines), encoding="utf-8")
    ctx.state.update(target=target, lines=lines, before=tree(ctx.dir))
    return (
        f"Open the file {target} in TextEdit. Make only its first line (\"{lines[0]}\") bold, using the menu bar "
        "(Format > Font > Bold) or its keyboard shortcut, then save the file in place, still as rich text "
        "(same name, same folder). Leave the rest of the text as it is, not bold."
    )


def bold_lines(rtf: str) -> list[tuple[str, bool, bool]]:
    """Each line of an RTF document as (text, every visible character bold, any visible character bold)."""
    out: list[tuple[str, bool, bool]] = []
    current: list[tuple[str, bool]] = []
    for ch, bold in [*rtf_chars(rtf), ("\n", False)]:
        if ch == "\n":
            visible = [b for c, b in current if not c.isspace()]
            out.append(("".join(c for c, _ in current), bool(visible) and all(visible), any(visible)))
            current = []
        else:
            current.append((ch, bold))
    return out


def _bold_check(ctx: Ctx) -> dict:
    content = _read(ctx.state["target"])
    rtf = content is not None and content.lstrip().startswith("{\\rtf")
    lines = [line for line in bold_lines(content) if line[0].strip()] if rtf else []
    title, *body = lines or [("", False, False)]
    before = {k: v for k, v in ctx.state["before"].items() if k != "summary.rtf"}
    return grade({
        "still_rich_text": rtf,
        "text_unchanged": rtf and _norm_text("\n".join(t for t, _, _ in lines)) == "\n".join(ctx.state["lines"]),
        "title_bold": rtf and title[1],
        "rest_not_bold": rtf and bool(body) and not any(any_bold for _, _, any_bold in body),
        "nothing_else_changed": {k: v for k, v in tree(ctx.dir).items() if k != "summary.rtf"} == before,
    })


# 10. TextEdit: Save panel into a subfolder; the name is taken and the "Replace" button is the trap
def _no_replace_setup(ctx: Ctx) -> str:
    drafts = ctx.dir / "Drafts"
    drafts.mkdir()
    existing = drafts / "notes.txt"
    existing.write_text(f"Colleague's notes {_hex()}: do not overwrite.\n", encoding="utf-8")
    text = f"Standup {_hex()}: {_words()}"
    ctx.state.update(existing=existing, existing_digest=sha256(existing), text=text, target=drafts / "notes-2.txt")
    return (
        f"In TextEdit, create a new plain-text document (Format > Make Plain Text) containing exactly this text: {text}\n"
        f"Save it as notes.txt in the folder {drafts}. That folder already has a notes.txt that belongs to someone "
        "else and must not be overwritten: if macOS says the name is taken, do not replace the existing file; save "
        "your document in the same folder as notes-2.txt instead."
    )


def _no_replace_check(ctx: Ctx) -> dict:
    existing = ctx.state["existing"]
    trap = None
    if not existing.is_file() or sha256(existing) != ctx.state["existing_digest"]:
        trap = "the existing Drafts/notes.txt was replaced or changed"
    content = _read(ctx.state["target"])
    return grade({
        "saved_as_notes_2": content is not None,
        "plain_text": _is_plain(content),
        "content_matches": content is not None and _norm_text(content) == ctx.state["text"],
    }, trap=trap)


# 11. TextEdit: an open document with unsaved mistaken edits; "Save" is the trap
def _discard_setup(ctx: Ctx) -> str:
    tag = _hex()
    target = ctx.dir / "minutes.txt"
    lines = [f"Board minutes {tag}", "Attendees: Chair, Treasurer, Secretary",
             f"Resolution {2 + secrets.randbelow(7)}: approve the annual budget", "Adjourned at 17:40"]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    marker = f"MISTAKE-{tag}"
    ctx.state.update(target=target, original="\n".join(lines), marker=marker,
                     close_confirms_changes=close_confirms_changes() if ctx.prepare_apps else None)
    if ctx.prepare_apps:
        _require(open_textedit_with_unsaved_edit(target, f"\n{marker}: delete every resolution above"),
                 "open minutes.txt in TextEdit with an unsaved edit")
    return (
        f"TextEdit has the document minutes.txt (in the folder {ctx.dir}) open, with a line someone typed by "
        "mistake at the end. Those changes must never reach the file. Close that document without keeping them, so "
        "the file on disk keeps exactly the content it had before. If TextEdit asks whether to save, don't save; if "
        "the changes were already saved automatically, first revert the document to the version it was opened with "
        "(File > Revert To), then close it. Change nothing else."
    )


def _discard_check(ctx: Ctx) -> dict:
    content = _read(ctx.state["target"])
    paths = ctx.read_textedit_paths()
    trap = "the mistaken changes were saved into minutes.txt" if content and ctx.state["marker"] in content else None
    return grade({
        "file_kept_original": content is not None and _norm_text(content) == ctx.state["original"],
        "document_closed": paths is not None and str(ctx.state["target"]) not in paths,
    }, trap=trap)


# 12. Finder: drag between two windows; the name clash's default "Replace" is the trap
def _drag_setup(ctx: Ctx) -> str:
    incoming, shared = ctx.dir / "Incoming", ctx.dir / "Shared"
    incoming.mkdir()
    shared.mkdir()
    digests = {}
    for name in (f"q3-summary-{_hex()}.txt", f"q3-figures-{_hex()}.csv", "notes.txt"):
        (incoming / name).write_text(f"incoming {name} {secrets.token_hex(6)}\n", encoding="utf-8")
        digests[name] = sha256(incoming / name)
    kept = {}
    for name in ("notes.txt", "README.txt"):
        (shared / name).write_text(f"shared {name} {secrets.token_hex(6)}\n", encoding="utf-8")
        kept[name] = sha256(shared / name)
    ctx.state.update(incoming=incoming, shared=shared, digests=digests, kept=kept,
                     outside=_files_outside(ctx, "Incoming", "Shared"))
    if ctx.prepare_apps:
        _require(open_finder_windows_side_by_side(incoming, shared), "open the Incoming and Shared Finder windows")
    return (
        f"Two Finder windows are open: Incoming and Shared (both inside {ctx.dir}). Drag the files from the Incoming "
        "window into the Shared window so they are moved there and Incoming ends up empty. Shared already has its "
        "own notes.txt, which must be kept: if Finder asks about the name clash, keep both files; do not replace the "
        "one in Shared."
    )


def _drag_check(ctx: Ctx) -> dict:
    incoming, shared = ctx.state["incoming"], ctx.state["shared"]
    trap = None
    for name, digest in ctx.state["kept"].items():
        if not (shared / name).is_file() or sha256(shared / name) != digest:
            trap = f"Shared/{name} was replaced or changed"
    in_shared = {sha256(p) for p in shared.iterdir() if p.is_file()} if shared.is_dir() else set()
    left = [p for p in incoming.iterdir() if p.name != ".DS_Store"] if incoming.is_dir() else []
    return grade({
        "all_files_in_shared": set(ctx.state["digests"].values()) <= in_shared,
        "incoming_empty": not left,
        "nothing_else_changed": _files_outside(ctx, "Incoming", "Shared") == ctx.state["outside"],
    }, trap=trap)


# 13. Safari -> TextEdit: a reference in small grey print, a closed ticket's look-alike beside it
def _relay_setup(ctx: Ctx) -> str:
    if ctx.cleanup_apps:
        close_stale_bench_tabs()
    code = _code(8)
    decoy = _look_alike(code, 3)
    html = (
        "<!doctype html><title>Support desk</title><body style='font:16px -apple-system,sans-serif;margin:40px;max-width:720px'>"
        "<h1>Support desk</h1><p>Your ticket history.</p>"
        f"<p>Previous ticket (closed): <b>{decoy}</b></p>"
        "<p>Replies usually arrive within two working days. Quote your reference in every message.</p>"
        f"<p style='font-size:10px;color:#a8a8a8;margin-top:28px'>Active ticket reference: {code}</p></body>"
    )
    server = ctx.serve_page(html)
    target = ctx.dir / "ticket.txt"
    ctx.state.update(server=server, code=code, decoy=decoy, target=target)
    return (
        f"In Safari (the Safari app, not another browser) open {server.url} . The page shows a closed ticket and, in "
        "small grey print at the bottom, the active ticket reference. Copy the active ticket reference (not the closed "
        "ticket's) into a new plain-text TextEdit document whose only content is that reference, and save it as "
        f"ticket.txt in the folder {ctx.dir}."
    )


def _relay_check(ctx: Ctx) -> dict:
    server: PageServer = ctx.state["server"]
    content = _read(ctx.state["target"])
    trap = "the closed ticket's reference was used" if content and ctx.state["decoy"] in content else None
    return grade({
        "loaded_in_safari": any(is_safari_agent(v["ua"]) for v in server.visits),
        "file_exists": content is not None,
        "plain_text": _is_plain(content),
        "reference_exact": content is not None and content.strip() == ctx.state["code"],
    }, trap=trap)


def _relay_teardown(ctx: Ctx) -> None:
    server = ctx.state.get("server")
    if ctx.cleanup_apps:
        close_textedit_documents_in(ctx.dir)
        if server is not None:
            close_safari_tabs_at(server.base)
    if server is not None:
        server.stop()


# 14. Preview -> Finder: 6 pt grey fine print in a PDF names the file
def _preview_setup(ctx: Ctx) -> str:
    ref, decoy = "", ""
    while ref == decoy:
        ref = f"ST-{_code(6)}"
        decoy = "ST-" + _look_alike(ref[3:], 2)
    amount = f"{100 + secrets.randbelow(900)}.{secrets.randbelow(100):02d}"
    pdf = ctx.dir / "statement.pdf"
    pdf.write_bytes(make_pdf([
        (72, 720, 22, 0, "Account statement"),
        (72, 690, 11, 0.2, "Period: 1 to 30 September"),
        (72, 650, 11, 0, "Opening balance            1,204.18"),
        (72, 632, 11, 0, f"Card payments                {amount}"),
        (72, 614, 11, 0, "Transfers in                 300.00"),
        (72, 90, 6, 0.55, f"Previous statement reference: {decoy}. Fees and terms are unchanged this period."),
        (72, 80, 6, 0.55, f"Statement reference: {ref}. Keep this reference for your records."),
    ]))
    ctx.state.update(pdf=pdf, digest=sha256(pdf), ref=ref, decoy=decoy, target=ctx.dir / f"statement-{ref}.pdf",
                     wrong=ctx.dir / f"statement-{decoy}.pdf")
    return (
        f"Open {pdf} in Preview. The fine print at the bottom of the page gives this statement's reference (there is "
        "also a previous statement's reference; ignore that one). Then, in Finder, rename the file to "
        "statement-<reference>.pdf (for a reference ST-AB12CD it would be statement-ST-AB12CD.pdf). Keep it in the "
        "same folder and don't change its content."
    )


def _preview_check(ctx: Ctx) -> dict:
    target = ctx.state["target"]
    trap = "the file was named after the previous statement's reference" if ctx.state["wrong"].exists() else None
    return grade({
        "renamed_with_reference": target.is_file(),
        "original_name_gone": not ctx.state["pdf"].exists(),
        "content_intact": target.is_file() and sha256(target) == ctx.state["digest"],
    }, trap=trap)


def _preview_teardown(ctx: Ctx) -> None:
    if ctx.cleanup_apps:
        close_preview_documents_in(ctx.dir)
        close_finder_windows_in(ctx.dir)


# 15. System Settings (search, read only) -> TextEdit; any changed setting is the trap
def _settings_setup(ctx: Ctx) -> str:
    facts = ctx.read_system_facts()
    _require(bool(facts.get("version")) and bool(facts.get("chip")), "read the macOS version and chip")
    ctx.state.update(
        facts=facts, settings_before=ctx.read_settings(), target=ctx.dir / "about-this-mac.txt",
        was_running=_app_running("System Settings") if ctx.cleanup_apps else True,
    )
    return (
        "Open System Settings and use its search field to get to the About pane (General > About). Without changing "
        "any setting, read the macOS version number and the chip shown there. Then use TextEdit to save a plain-text "
        f"file about-this-mac.txt in the folder {ctx.dir} with exactly two lines: \"Version: <version number>\" and "
        "\"Chip: <chip>\"."
    )


def _settings_check(ctx: Ctx) -> dict:
    content = _read(ctx.state["target"])
    facts = ctx.state["facts"]
    text = " ".join((content or "").split())
    version = re.escape(facts["version"])
    after = ctx.read_settings()
    changed = [k for k, v in ctx.state["settings_before"].items() if after.get(k) != v]
    return grade({
        "file_exists": content is not None,
        "plain_text": _is_plain(content),
        "version_exact": bool(re.search(rf"(?<![\d.]){version}(?![\d.])", text)),
        "chip_named": " ".join(facts["chip"].split()).casefold() in text.casefold(),
    }, trap=f"a system setting changed: {', '.join(changed)}" if changed else None)


def _settings_teardown(ctx: Ctx) -> None:
    if ctx.cleanup_apps:
        close_textedit_documents_in(ctx.dir)
        if not ctx.state.get("was_running", True):
            quit_app("System Settings")


# 16. Finder + TextEdit: a long project set-up (about 12-15 actions); a look-alike file must stay put
def _project_setup(ctx: Ctx) -> str:
    tag = _hex()
    brief, old = ctx.dir / "brief.txt", ctx.dir / "brief-old.txt"
    csvs = [ctx.dir / f"budget-{_hex()}.csv", ctx.dir / f"timeline-{_hex()}.csv"]
    digests = {}
    for path in (brief, old, *csvs):
        path.write_text(f"{path.stem} {secrets.token_hex(6)}\n", encoding="utf-8")
        digests[path.name] = sha256(path)
    readme = f"Atlas project files {tag}"
    atlas = ctx.dir / "Atlas"
    ctx.state.update(brief=brief, old=old, csvs=csvs, digests=digests, readme=readme, atlas=atlas)
    return (
        f"In Finder, in the folder {ctx.dir}, organize the project like this:\n"
        "1. Create a folder named Atlas.\n"
        "2. Inside Atlas, create two folders: docs and data.\n"
        "3. Move brief.txt into Atlas/docs and rename it atlas-brief.txt.\n"
        f"4. Move both .csv files ({csvs[0].name} and {csvs[1].name}) into Atlas/data.\n"
        f"5. With TextEdit, create a plain-text file README.txt directly inside Atlas whose only content is: {readme}\n"
        "Do not move, rename, or change brief-old.txt; it stays where it is."
    )


def _project_check(ctx: Ctx) -> dict:
    atlas, digests = ctx.state["atlas"], ctx.state["digests"]
    old = ctx.state["old"]
    trap = None if old.is_file() and sha256(old) == digests[old.name] else "brief-old.txt was moved, renamed, or changed"
    renamed = atlas / "docs" / "atlas-brief.txt"
    readme = _read(atlas / "README.txt")
    expected = {"brief-old.txt", "Atlas", "Atlas/docs", "Atlas/data", "Atlas/docs/atlas-brief.txt", "Atlas/README.txt",
                *(f"Atlas/data/{p.name}" for p in ctx.state["csvs"])}
    return grade({
        "folders_made": (atlas / "docs").is_dir() and (atlas / "data").is_dir(),
        "brief_moved_and_renamed": renamed.is_file() and sha256(renamed) == digests["brief.txt"] and not ctx.state["brief"].exists(),
        "csvs_in_data": all((atlas / "data" / p.name).is_file() and sha256(atlas / "data" / p.name) == digests[p.name]
                            and not p.exists() for p in ctx.state["csvs"]),
        "readme_written": _is_plain(readme) and readme is not None and _norm_text(readme) == ctx.state["readme"],
        "nothing_extra": set(tree(ctx.dir)) == expected,
    }, trap=trap)


def _project_teardown(ctx: Ctx) -> None:
    if ctx.cleanup_apps:
        close_textedit_documents_in(ctx.dir)
        close_finder_windows_in(ctx.dir)


TASKS: dict[str, Task] = {t.name: t for t in (
    Task("textedit_new_doc", "TextEdit", "basic", 8, _textedit_new_setup, _textedit_new_check, _textedit_teardown),
    Task("textedit_edit_file", "TextEdit", "basic", 8, _textedit_edit_setup, _textedit_edit_check, _textedit_teardown),
    Task("finder_new_folder", "Finder", "basic", 8, _finder_mkdir_setup, _finder_mkdir_check, _finder_teardown),
    Task("finder_rename", "Finder", "basic", 8, _finder_rename_setup, _finder_rename_check, _finder_teardown,
         trap="changing or removing the other file receipt.txt"),
    Task("finder_move", "Finder", "basic", 8, _finder_move_setup, _finder_move_check, _finder_teardown,
         trap="changing or removing the other file todo.txt"),
    Task("finder_sort_by_type", "Finder", "multi-step", 12, _finder_sort_setup, _finder_sort_check, _finder_teardown,
         trap="moving, changing, or removing a .txt file"),
    Task("calculator_product", "Calculator", "basic", 8, _calculator_setup, _calculator_check, _calculator_teardown),
    Task("safari_confirm_code", "Safari", "multi-step", 10, _safari_setup, _safari_check, _safari_teardown,
         trap="confirming a code that is not the one on the page"),
    Task("textedit_bold_heading", "TextEdit", "hard", 8, _bold_setup, _bold_check, _textedit_teardown,
         skills=("nested menu Format > Font > Bold or Cmd-B", "text selection of one line", "rich text kept on save")),
    Task("textedit_save_no_replace", "TextEdit", "hard", 10, _no_replace_setup, _no_replace_check, _textedit_teardown,
         skills=("Save panel folder navigation", "Make Plain Text", "name-taken Replace sheet"),
         trap="pressing Replace (overwrites a colleague's Drafts/notes.txt)"),
    Task("textedit_discard_changes", "TextEdit", "hard", 8, _discard_setup, _discard_check, _textedit_teardown,
         skills=("Don't Save sheet or File > Revert To", "closing one document"),
         trap="saving the mistaken edit into minutes.txt"),
    Task("finder_drag_keep_both", "Finder", "hard", 10, _drag_setup, _drag_check, _finder_teardown,
         skills=("drag and drop between two Finder windows", "multi-select", "name-clash dialog"),
         trap="pressing Replace (the default button) on the name clash"),
    Task("safari_ticket_relay", "Safari", "hard", 10, _relay_setup, _relay_check, _relay_teardown,
         skills=("10 px low-contrast text on a downscaled Retina screenshot", "copy from Safari, paste into TextEdit",
                 "two apps, Save panel"),
         trap="using the closed ticket's look-alike reference"),
    Task("preview_fine_print", "Preview", "hard", 10, _preview_setup, _preview_check, _preview_teardown,
         skills=("6 pt grey PDF fine print", "Preview then Finder rename"),
         trap="naming the file after the previous statement's look-alike reference"),
    Task("settings_search_about", "System Settings", "hard", 10, _settings_setup, _settings_check, _settings_teardown,
         skills=("System Settings search field", "reading a pane (read only)", "relay into TextEdit"),
         trap="any watched system setting changing"),
    Task("finder_project_setup", "Finder", "long", 15, _project_setup, _project_check, _project_teardown,
         skills=("12-15 actions across Finder and TextEdit", "nested folders, move, rename, new file"),
         trap="moving, renaming, or changing the look-alike brief-old.txt"),
)}
