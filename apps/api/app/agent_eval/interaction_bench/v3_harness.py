"""Interaction Gauntlet harness — the judge for generations 3 to 6 (stdlib only).

Generation 3 (cross-surface missions), generation 4 (the human-input atlas), generation 5 (long,
multi-window workflows with memory) and generation 6 (in-page web apps built for parallel bench lanes) all
run here. One process serves the gauntlet pages, hosts each
mission's native desktop windows, owns each mission's folder on disk, and judges every surface an agent
touches:

    python v3_harness.py                     # real desktop windows, random port, storage/bench/LumenBench
    python -m app.agent_eval.interaction_bench.v3_harness --port 8030

The first stdout line is ``V3_HARNESS_URL http://127.0.0.1:<port>``; point the agent at
``<url>/eval/bench/<page>``. A random port (the default) gives every harness run a fresh browser
origin, so site permissions granted or blocked in an earlier run cannot carry over.

It serves every file in pages/ (generations 1 and 2 still work here) plus:

    POST /v3/api/session   {page, sid?, aux?, focus?, tag?}   start or resume a mission session
                                                (focus: run one challenge alone; tag: one of several
                                                concurrent runs of the page)
    GET  /v3/api/state?sid=                     the page's view of its session
    POST /v3/api/act       {sid, kind, ...}     an action the page (or its own window) observed
    GET  /v3/files/<sid>/<name>                 a mission's downloadable file
    GET  /v3/api/report                         per-page verdicts for graders (no answers)
    POST /v3/api/admin/close_page {page}        close a page's (or one run key's) native windows
    POST /v3/api/admin/shutdown

``--debug-hooks`` adds the validator's ground-truth and window-driving endpoints under
/v3/api/debug/. They exist only for the benchmark's own validator; an agent run with them enabled
is void.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import signal
import sys
import tempfile
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

try:  # package import inside the repo; flat import in the standalone bundle
    from .v3_missions import LOCK, MISSIONS as V3_MISSIONS, Session
    from .v4_missions import MISSIONS as V4_MISSIONS
    from .v5_missions import MISSIONS as V5_MISSIONS
    from .v6_missions import MISSIONS as V6_MISSIONS
except ImportError:  # pragma: no cover - standalone bundle / run as a script
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from v3_missions import LOCK, MISSIONS as V3_MISSIONS, Session  # type: ignore
    from v4_missions import MISSIONS as V4_MISSIONS  # type: ignore
    from v5_missions import MISSIONS as V5_MISSIONS  # type: ignore
    from v6_missions import MISSIONS as V6_MISSIONS  # type: ignore

MISSIONS = {**V3_MISSIONS, **V4_MISSIONS, **V5_MISSIONS, **V6_MISSIONS}
VERSION = "6.0"
HERE = Path(__file__).resolve().parent
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_TAG_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
PERTURBATIONS = ("popup_modal", "redirect_link", "low_contrast_injection", "latency", "late_banner", "remount", "noop_click")
_TYPES = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}


class HarnessError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class Harness:
    def __init__(
        self,
        *,
        pages_dir: Path,
        workspace_root: Path,
        desktop_mode: str,
        debug_hooks: bool,
        log: Any = None,
    ) -> None:
        self.pages_dir = pages_dir
        self.workspace_root = workspace_root
        self.debug_hooks = debug_hooks
        self.sessions: dict[str, Session] = {}
        self.active: dict[str, str] = {}
        self.page_fails: dict[str, str] = {}
        self.started = datetime.now(timezone.utc).isoformat()
        self.stop = threading.Event()
        self._log = log or sys.stderr
        self.desktop = None
        self.desktop_mode = "off"
        if desktop_mode != "off":
            try:
                try:
                    from .v3_desktop import VIEWS as V3_VIEWS, DesktopHost
                    from .v4_desktop import VIEWS as V4_VIEWS
                    from .v5_desktop import VIEWS as V5_VIEWS
                except ImportError:  # pragma: no cover - standalone bundle
                    from v3_desktop import VIEWS as V3_VIEWS, DesktopHost  # type: ignore
                    from v4_desktop import VIEWS as V4_VIEWS  # type: ignore
                    from v5_desktop import VIEWS as V5_VIEWS  # type: ignore
                self.desktop = DesktopHost(desktop_mode, log=self.log, views={**V3_VIEWS, **V4_VIEWS, **V5_VIEWS})
                self.desktop_mode = desktop_mode
            except Exception as exc:  # noqa: BLE001 - no display / no tkinter: desktop missions report it
                self.log(f"desktop companions unavailable ({exc!r}); running without native windows")

    def log(self, line: str) -> None:
        try:
            self._log.write(f"[{time.strftime('%H:%M:%S')}] {line}\n")
            self._log.flush()
        except Exception:  # noqa: BLE001
            pass

    # --- sessions -------------------------------------------------------------------------------
    def session(self, sid: str) -> Session:
        session = self.sessions.get(sid)
        if session is None:
            raise HarnessError(404, "no such session")
        return session

    def start_session(self, page: str, sid: str | None, aux: bool, focus: str | None = None, tag: str | None = None,
                      perturb: Any = None, seed: Any = None) -> Session:
        with LOCK:
            if isinstance(perturb, str):
                perturb = perturb.split(",")
            names = tuple(dict.fromkeys(str(n).strip() for n in (perturb or []) if str(n).strip()))
            unknown = [n for n in names if n not in PERTURBATIONS]
            if unknown:
                raise HarnessError(400, f"unknown perturbation {unknown[0]!r} (choose from: {', '.join(PERTURBATIONS)})")
            try:
                seed_n = int(seed or 0)
            except (TypeError, ValueError) as exc:
                raise HarnessError(400, "a perturbation seed is an integer") from exc
            focus = str(focus or "").strip() or None
            tag = str(tag or "").strip()
            old = self.sessions.get(sid) if sid else None
            if old and old.page == page and (aux or (old.focus, old.tag) == (focus, tag)):  # aux windows join by sid
                return old
            if aux:
                raise HarnessError(404, "this window belongs to a session that no longer exists — reopen it from the mission page")
            mission = MISSIONS.get(page)
            if mission is None:
                raise HarnessError(404, f"{page!r} is not a harness mission (generations 3-6)")
            if focus and (not mission.focusable or focus not in mission.steps):
                raise HarnessError(404, f"{page} has no challenge {focus!r} that can run alone")
            if tag and not _TAG_RE.match(tag):
                raise HarnessError(400, "a run tag is 1-32 letters, digits, '.', '_' or '-'")
            key = page + (f"#{focus}" if focus else "") + (f"@{tag}" if tag else "")
            new_sid = secrets.token_hex(3)
            while new_sid in self.sessions:
                new_sid = secrets.token_hex(3)
            previous = self.active.get(key)
            if previous and previous in self.sessions:
                old = self.sessions[previous]
                old.superseded = True
                old.touch()
                if self.desktop is not None:
                    self.desktop.post(lambda sid=previous: self.desktop.close_session(sid))
            session = Session(new_sid, mission, self, focus=focus, tag=tag, perturb=names, seed=seed_n)
            session.mission.setup()
            if key in self.page_fails:
                session.failed = self.page_fails[key]
                session.messages["harness"] = "An earlier load of this page sprang a trap; restart the harness to run it again."
            if mission.needs_desktop and self.desktop is None:
                session.messages["desktop"] = (
                    "This harness has no desktop session, so the native windows this mission needs cannot open."
                )
            self.sessions[new_sid] = session
            self.active[key] = new_sid
            self.log(f"session {new_sid} started for {key}")
            return session

    def sticky_fail(self, page: str, reason: str) -> None:
        self.page_fails.setdefault(page, reason)
        self.log(f"TRAP {page}: {reason}")

    def open_window(self, session: Session, view: str) -> bool:
        if self.desktop is None:
            session.say("desktop", "This harness has no desktop session, so that native window cannot open.")
            return False
        self.desktop.post(lambda: self.desktop.open_view(session, view))
        return True

    def make_workspace(self, session: Session, label: str) -> Path:
        path = self.workspace_root / f"v{session.mission.generation}-{label}-{session.sid}"
        path.mkdir(parents=True, exist_ok=False)
        session.workspace = path
        return path

    def poll_loop(self) -> None:
        while not self.stop.wait(0.7):
            with LOCK:
                for session in list(self.sessions.values()):
                    if session.superseded or not (session.mission.needs_workspace or session.mission.ticks):
                        continue
                    try:
                        session.mission.poll()
                    except Exception:  # noqa: BLE001 - a poll bug must not kill judging
                        self.log("poll failed: " + traceback.format_exc(limit=3))

    # --- report ---------------------------------------------------------------------------------
    def report(self) -> dict[str, Any]:
        with LOCK:
            pages: dict[str, dict[str, Any]] = {}
            for session in self.sessions.values():  # keyed page, page#focus, page@tag, page#focus@tag
                pages.setdefault(session.key, {"sessions": []})["sessions"].append(session.summary())
            for page, entry in pages.items():
                sessions = entry["sessions"]
                best = max(sessions, key=lambda s: (s["passed"], s["completed"], -s["futile"]))
                failed = self.page_fails.get(page)
                entry["violations"] = sum(int(x["violations"]) for x in sessions)
                entry["violation_kinds"] = [k for x in sessions for k in x["violation_kinds"]]
                entry["perturbations"] = list(best["perturb"])
                entry["perturb_events"] = [e for x in sessions for e in x["perturb_events"]]
                entry.update({
                    "passed": bool(best["passed"]) and not failed,
                    "failed": failed,
                    "best": best,
                    "latest": max(sessions, key=lambda s: s["created"])["sid"],
                })
            return {
                "benchmark": "lumen-interaction-gauntlet",
                "generations": sorted({cls.generation for cls in MISSIONS.values()}),
                "version": VERSION,
                "desktop": self.desktop_mode,
                "started": self.started,
                "pages": pages,
            }

    def close_page(self, page: str) -> int:
        """Close the native windows of a page's sessions; a run key (page#focus@tag) closes just that run's."""
        with LOCK:
            sids = [s.sid for s in self.sessions.values() if (s.key if "#" in page or "@" in page else s.page) == page]
        if self.desktop is not None:
            for sid in sids:
                self.desktop.post(lambda sid=sid: self.desktop.close_session(sid))
        return len(sids)

    # --- debug hooks (validator only) -----------------------------------------------------------
    def debug(self, route: str, query: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        if route == "truth":
            with LOCK:
                return self.session(query.get("sid", "")).mission.truth()
        if route == "dialogs":
            with LOCK:
                return {"dialogs": list(self.session(query.get("sid", "")).dialogs)}
        if route == "script":
            with LOCK:
                sid = self.session(str(body.get("sid"))).sid
            if self.desktop is None:
                raise HarnessError(409, "no desktop host")
            self.desktop.scripts.setdefault(sid, deque()).extend(str(a) for a in body.get("answers", []))
            return {"ok": True}
        if route == "ui":
            if self.desktop is None:
                raise HarnessError(409, "no desktop host")
            sid, window = str(body.get("sid")), str(body.get("window"))
            op = str(body.get("op"))

            def run() -> dict[str, Any]:
                view = self.desktop.view(sid, window)
                if view is None:
                    raise HarnessError(404, f"window {window!r} is not open")
                return view.debug(op, body)

            return self.desktop.call(run)
        if route == "windows":
            if self.desktop is None:
                return {"windows": []}
            sid = query.get("sid", "")
            return {"windows": self.desktop.call(lambda: [name for (owner, name), v in self.desktop.views.items() if owner == sid and v.alive()])}
        if route == "reset":
            with LOCK:
                self.page_fails.clear()
            return {"ok": True}
        raise HarnessError(404, "unknown debug route")


def make_handler(harness: Harness) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "LumenGauntletV3/" + VERSION
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D102 - quiet by default
            pass

        # --- plumbing ---------------------------------------------------------------------------
        def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, payload: Any) -> None:
            self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 4_000_000:
                raise HarnessError(413, "request too large")
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            try:
                data = json.loads(raw.decode("utf-8"))
            except ValueError as exc:
                raise HarnessError(400, "malformed JSON body") from exc
            if not isinstance(data, dict):
                raise HarnessError(400, "expected a JSON object")
            return data

        def _dispatch(self, method: str) -> None:
            parsed = urlparse(self.path)
            path = parsed.path
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            try:
                body = self._body() if method == "POST" else {}
                if method == "GET" and (path == "/" or path == "/eval/bench" or path == "/eval/bench/"):
                    return self._index()
                if method == "GET" and path.startswith("/eval/bench/"):
                    return self._page(path[len("/eval/bench/"):])
                if method == "GET" and path.startswith("/v3/files/"):
                    return self._file(path[len("/v3/files/"):])
                if path == "/v3/api/health":
                    return self._json(200, {"ok": True, "version": VERSION, "desktop": harness.desktop_mode, "missions": sorted(MISSIONS)})
                if method == "POST" and path == "/v3/api/session":
                    session = harness.start_session(
                        str(body.get("page") or ""), body.get("sid") or None, bool(body.get("aux")),
                        focus=body.get("focus") or None, tag=body.get("tag") or None,
                        perturb=body.get("perturb") or None, seed=body.get("seed"),
                    )
                    with LOCK:
                        return self._json(200, session.state())
                if method == "GET" and path == "/v3/api/state":
                    with LOCK:
                        return self._json(200, harness.session(query.get("sid", "")).state())
                if method == "POST" and path == "/v3/api/act":
                    with LOCK:
                        session = harness.session(str(body.get("sid") or ""))
                        if session.superseded:
                            result = {"ok": False, "msg": "This session was replaced by a newer load of the page."}
                        else:
                            kind = str(body.pop("kind", ""))
                            body.pop("sid", None)
                            if kind == "violation" and session.perturb:  # a perturbation's decoy, judged for every page alike
                                result = session.violation(body.get("what") or "", body.get("detail") or "")
                            elif kind == "perturb_event" and session.perturb:
                                result = session.perturb_event(body.get("name") or "", body.get("event") or "")
                            else:
                                result = session.mission.act(kind, body) or {}
                        return self._json(200, {"result": result, "state": session.state()})
                if method == "GET" and path == "/v3/api/report":
                    return self._json(200, harness.report())
                if method == "POST" and path == "/v3/api/admin/close_page":
                    return self._json(200, {"closed_sessions": harness.close_page(str(body.get("page") or ""))})
                if method == "POST" and path == "/v3/api/admin/shutdown":
                    self._json(200, {"ok": True})
                    harness.stop.set()
                    if harness.desktop is not None:
                        harness.desktop.quit()
                    return None
                if path.startswith("/v3/api/debug/"):
                    if not harness.debug_hooks:
                        raise HarnessError(404, "not found")
                    return self._json(200, harness.debug(path[len("/v3/api/debug/"):], query, body))
                raise HarnessError(404, "not found")
            except HarnessError as exc:
                return self._json(exc.status, {"error": str(exc)})
            except ConnectionError:
                return None  # the browser closed the tab or window mid-request; nothing to answer
            except Exception as exc:  # noqa: BLE001 - report, never crash the server thread
                harness.log("request failed: " + traceback.format_exc(limit=4))
                return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_HEAD(self) -> None:  # noqa: N802
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        # --- content ----------------------------------------------------------------------------
        def _page(self, raw: str) -> None:
            name = unquote(raw).strip("/")
            if not _NAME_RE.match(name):
                raise HarnessError(404, "no such page")
            if "." not in name:
                name += ".html"
            path = harness.pages_dir / name
            if not path.is_file() or path.suffix not in _TYPES:
                raise HarnessError(404, "no such page")
            self._send(200, path.read_bytes(), _TYPES[path.suffix])

        def _file(self, raw: str) -> None:
            parts = unquote(raw).split("/")
            if len(parts) != 2:
                raise HarnessError(404, "no such file")
            sid, name = parts
            with LOCK:
                served = harness.session(sid).mission.file(name)
            if served is None:
                raise HarnessError(404, "no such file")
            data, content_type = served
            self._send(200, data, content_type, {"Content-Disposition": f'attachment; filename="{name}"'})

        def _index(self) -> None:
            rows = "".join(
                f'<li><a href="/eval/bench/{name}">{name}</a> — generation {cls.generation}, {cls.family}, {len(cls.steps)} challenges</li>'
                for name, cls in sorted(MISSIONS.items(), key=lambda item: (item[1].generation, item[0]))
            )
            html = (
                "<!doctype html><meta charset=utf-8><title>Interaction Gauntlet harness</title>"
                "<body style='font-family:system-ui;padding:24px'><h1>Interaction Gauntlet — generations 3-6</h1>"
                f"<p>Desktop companions: {harness.desktop_mode}</p><ul>{rows}</ul></body>"
            )
            self._send(200, html.encode("utf-8"), _TYPES[".html"])

    return Handler


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        if isinstance(sys.exc_info()[1], ConnectionError):
            return  # a page closed while its state poll was in flight
        super().handle_error(request, client_address)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Interaction Gauntlet harness (generations 3-6).")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0, help="0 = a random free port (a fresh browser origin per run)")
    parser.add_argument("--desktop", choices=["real", "ghost", "off"], default="real",
                        help="real: visible native windows; ghost: invisible (validator); off: no desktop")
    parser.add_argument("--workspace-root", help="where mission folders are created (default storage/bench/LumenBench, or a temp dir in ghost mode)")
    parser.add_argument("--pages", default=str(HERE / "pages"), help="directory of gauntlet pages")
    parser.add_argument("--debug-hooks", action="store_true", help="VALIDATOR ONLY: expose ground truth and window driving")
    parser.add_argument("--log", help="append harness logs to this file (default stderr)")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - not a reconfigurable text stream
            pass

    temp_root = None
    if args.workspace_root:
        workspace_root = Path(args.workspace_root).expanduser()
    elif args.desktop == "ghost":
        temp_root = Path(tempfile.mkdtemp(prefix="lumen-bench-harness-"))
        workspace_root = temp_root
    else:
        workspace_root = Path(__file__).resolve().parents[5] / "storage" / "bench" / "LumenBench"
    workspace_root.mkdir(parents=True, exist_ok=True)
    log = open(args.log, "a", encoding="utf-8") if args.log else sys.stderr  # noqa: SIM115 - lives for the process

    harness = Harness(
        pages_dir=Path(args.pages),
        workspace_root=workspace_root,
        desktop_mode=args.desktop,
        debug_hooks=args.debug_hooks,
        log=log,
    )
    server = _Server((args.host, args.port), make_handler(harness))
    url = f"http://{args.host}:{server.server_address[1]}"
    print(f"V3_HARNESS_URL {url}", flush=True)
    harness.log(f"Interaction Gauntlet harness at {url}/eval/bench/<page>  (desktop={harness.desktop_mode}, workspace={workspace_root})")
    if args.debug_hooks:
        harness.log("DEBUG HOOKS ENABLED — validator only; agent runs against this harness are void")

    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=harness.poll_loop, daemon=True).start()

    def on_signal(_signum: int, _frame: Any) -> None:
        harness.stop.set()
        if harness.desktop is not None:
            harness.desktop.quit()

    signal.signal(signal.SIGINT, on_signal)
    try:
        if harness.desktop is not None:
            harness.desktop.mainloop()
        else:
            while not harness.stop.wait(0.5):
                pass
    finally:
        harness.stop.set()
        server.shutdown()
        if temp_root is not None:
            shutil.rmtree(temp_root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
