"""Shared pieces of the generality-bench runners (miniwob_run, webgames_run).

Both runners serve a benchmark's pages from a local static server, start one chat-run per task against a
Lumen API (``--api``, never assumed to be on any model provider: an external-brain mailbox API works as well),
grade the task from the benchmark's own signal, and write a scorecard. Lanes are headless bench Chromiums
(interaction_bench.live_run.BenchLane); every task gets a lane with a new profile root, because
BenchLane.stop's rmtree can fail on Windows and a half-removed root would carry the last task's state.

Stdlib only. ``live_run`` is imported lazily, after LUMEN_API is set from ``--api``.
"""

from __future__ import annotations

import json
import mimetypes
import os
import subprocess
import sys
import threading
import time
import urllib.parse
from collections import defaultdict, deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

REPO = Path(__file__).resolve().parents[5]
FORBIDDEN_PORTS = {8001, 8011, 8041}  # the owner's and other sessions' running APIs

mimetypes.add_type("application/wasm", ".wasm")
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")

_TERMINAL_OK = {"COMPLETED"}


# ---------------------------------------------------------------- sources

def source_dir(name: str, env_var: str) -> Path:
    """A pinned source clone: ``$ENV``, else storage/open_bench/src/<name> in this checkout, else the same in the
    main checkout (a git worktree has no storage/)."""
    candidates: list[Path] = []
    if os.environ.get(env_var):
        candidates.append(Path(os.environ[env_var]))
    candidates.append(REPO / "storage" / "open_bench" / "src" / name)
    try:
        common = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=REPO, capture_output=True,
                                text=True, timeout=20).stdout.strip()
        if common:
            candidates.append((REPO / common).resolve().parent / "storage" / "open_bench" / "src" / name)
    except Exception:  # noqa: BLE001 - best effort
        pass
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise SystemExit(f"{name} source not found (looked in {[str(c) for c in candidates]}): run "
                     f"`python -m app.agent_eval.open_bench.sources fetch` or set {env_var}")


# ---------------------------------------------------------------- static server

class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address) -> None:  # a browser dropping a connection is not news
        return


class BenchServer:
    """A ThreadingHTTPServer on a random loopback port, run on a daemon thread."""

    def __init__(self, handler_class: type[BaseHTTPRequestHandler], **attrs) -> None:
        handler = type("Handler", (handler_class,), attrs)
        self.httpd = _QuietServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="bench-server", daemon=True)

    @property
    def port(self) -> int:
        return int(self.httpd.server_address[1])

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "BenchServer":
        self.thread.start()
        return self

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def safe_join(root: Path, relative: str) -> Path | None:
    """``root / relative`` when it stays inside root, else None (path traversal)."""
    relative = urllib.parse.unquote(relative).lstrip("/")
    if "\x00" in relative:
        return None
    target = (root / relative).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return None
    return target


def content_type(path: Path) -> str:
    kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if kind.startswith("text/") or kind in ("application/javascript", "application/json"):
        kind += "; charset=utf-8"
    return kind


class QuietHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:  # noqa: D401 - silence per-request logging
        return

    def send_bytes(self, status: int, body: bytes, kind: str = "application/octet-stream") -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, payload, status: int = 200) -> None:
        self.send_bytes(status, json.dumps(payload).encode("utf-8"), "application/json")

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------- API + lanes

def check_api(api: str, default: str) -> str:
    api = api.rstrip("/")
    port = urllib.parse.urlparse(api).port
    if port in FORBIDDEN_PORTS or port is None:
        raise SystemExit(f"refusing to run against {api}: use a bench API of your own (e.g. {default})")
    return api


def load_live_run(api: str):
    """interaction_bench.live_run reads LUMEN_API at import, so set it first."""
    os.environ["LUMEN_API"] = api
    from app.agent_eval.interaction_bench import live_run as lr
    if lr.API != api:
        raise SystemExit(f"live_run was already imported for {lr.API}; start a new process for {api}")
    return lr


class LaneFactory:
    """Hands out one headless bench lane per task, each with a fresh profile root and a fresh device id."""

    def __init__(self, lr, chromium: Path, out: Path | None = None) -> None:
        self.lr = lr
        self.chromium = chromium
        self._lock = threading.Lock()
        self._last = int(time.time() * 1000) % 1_000_000_000
        self.out = out

    def _next_index(self) -> int:
        with self._lock:
            self._last = max(self._last + 1, int(time.time() * 1000) % 1_000_000_000)
            return self._last

    def fresh(self, timeout_s: float = 90.0):
        index = self._next_index()
        lane = self.lr.BenchLane(index, self.chromium)
        lane.root = self.lr.LANE_ROOT / f"open-bench-{index}"  # never reuse a root: stop()'s rmtree can fail
        lane.start()
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                status = self.lr._request("GET", "/operator/extension/status")
            except Exception:  # noqa: BLE001 - API hiccup; keep waiting
                status = {}
            if any(str(c.get("device_id")) == lane.device_id and c.get("live_actions_ready")
                   for c in (status.get("connections") or [])):
                return lane
            time.sleep(1)
        lane.stop()
        raise RuntimeError(f"bench lane {lane.device_id} did not connect within {timeout_s:.0f} s")


# ---------------------------------------------------------------- one run

def execute_run(lr, goal: str, lane, *, title: str, timeout_s: float, poll: float, auto_resume: int, label: str) -> dict:
    """Start one chat-run for ``goal`` on ``lane`` (a BenchLane, or None for the user's own Chrome), watch it
    to a terminal state or the timeout, collect what graders need. Never answers a question the run asks."""
    device = lane.device_id if lane else None
    started = time.time()
    lr._clear_active_runs(device, lanes_running=bool(device), label=label)
    lr._close_bench_tabs(device, label=label)
    before = {int(r["id"]) for r in lr._request("GET", "/operator/runs")}
    thread = lr._request("POST", "/operator/chat/threads", {"title": title})
    if device:
        lr._request("POST", "/operator/extension/bench-lanes/pin",
                    {"thread_id": thread["id"], "device_id": device}, headers=lr.LOCAL_ADMIN_HEADERS)
    chat = lr._stream_chat(thread["id"], goal, label=label)
    run_id = chat["run_id"]
    if run_id is None and not device:
        latest = lr._latest_run_after(before)
        run_id = int(latest["id"]) if latest else None
    info: dict = {"goal": goal, "thread_id": thread["id"], "run_id": run_id, "lane": device or "user",
                  "assistant_text": chat["assistant_text"][:1200]}
    if run_id is None:
        info.update({"final_state": "NO_RUN_STARTED", "status_message": "", "messages": [chat["assistant_text"]]})
    else:
        watched = lr._watch(int(run_id), timeout_s, poll, auto_resume, label=label)
        run = watched["run"]
        info.update({"final_state": watched["state"], "status_message": str(run.get("status_message") or ""),
                     "timed_out": watched["timed_out"], "resumes": watched["resumes"],
                     "transitions": watched["transitions"]})
        info["messages"] = collect_messages(lr, thread["id"], chat["assistant_text"])
        try:
            events = lr._request("GET", f"/operator/runs/{run_id}/events", timeout=60)
            info["event_count"] = len(events)
            info["model_usage"] = lr._model_usage(events)
        except Exception:  # noqa: BLE001 - grade what we have
            pass
    info["elapsed_s"] = round(time.time() - started, 1)
    return info


def collect_messages(lr, thread_id: int, first: str = "") -> list[str]:
    """Every assistant message of the thread (the final answer is posted there), plus ``first``."""
    messages = [first] if first else []
    try:
        for message in lr._request("GET", f"/operator/chat/threads/{thread_id}/messages", timeout=60):
            if str(message.get("role")) == "assistant" and message.get("content"):
                messages.append(str(message["content"]))
    except Exception:  # noqa: BLE001
        pass
    return messages


def finish_run(lr, run_id) -> None:
    """Graded: end a run still parked or running now, so it never holds the lane or a takeover."""
    if run_id is None:
        return
    try:
        if str(lr._request("GET", f"/operator/runs/{run_id}").get("current_state") or "") in lr._BUSY_STATES:
            lr._request("POST", f"/operator/runs/{run_id}/cancel")
    except Exception:  # noqa: BLE001
        pass


def observe_text(lr, lane) -> tuple[str, str]:
    """(url, visible text) of the lane's current tab; ('', '') if it cannot be read."""
    try:
        obs = lr._request("GET", "/operator/extension/observe" + lr._device_query(lane.device_id if lane else None), timeout=60)
        return str(obs.get("url") or ""), str(obs.get("text") or "")
    except Exception:  # noqa: BLE001
        return "", ""


# ---------------------------------------------------------------- pool, console, scorecard

def run_pool(tasks: list[dict], run_task: Callable[[dict, object, str], dict], *, factory: LaneFactory | None,
             lanes: int, out: Path, label_prefix: str, say: Callable[[str, str], None]) -> list[dict]:
    """Work through ``tasks`` on ``lanes`` workers. ``run_task(task, lane, label)`` returns a result dict with
    at least ``task``, ``family``, ``passed``, ``final_state``, ``elapsed_s``. Each task gets a fresh lane
    (``factory.fresh()``); with no factory (lanes == 0) the user's own Chrome is used, one task at a time."""
    queue = deque(tasks)
    results: list[dict] = []
    lock = threading.Lock()
    counter = [0]
    started = time.time()

    def work(worker_label: str) -> None:
        while True:
            with lock:
                if not queue:
                    return
                task = queue.popleft()
                counter[0] += 1
                position = counter[0]
            say(worker_label, f"[{position}/{len(tasks)}] {task['name']} ({task['family']})")
            lane = None
            try:
                if factory:
                    lane = factory.fresh()
                result = run_task(task, lane, worker_label)
            except Exception as exc:  # noqa: BLE001 - one task's crash must not kill the suite
                result = {"task": task["name"], "family": task["family"], "passed": False,
                          "final_state": "DRIVER_ERROR", "error": f"{type(exc).__name__}: {exc}", "elapsed_s": 0}
            finally:
                if lane:
                    lane.stop()
            result.setdefault("task", task["name"])
            result.setdefault("family", task["family"])
            with lock:
                results.append(result)
                (out / f"{safe_name(task['name'])}.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
                write_scorecard(out, results, wall_s=time.time() - started, planned=len(tasks))
            say(worker_label, result_line(result))

    count = max(1, lanes)
    threads = [threading.Thread(target=work, args=(f"{label_prefix}-{n}" if lanes else "user",), daemon=True)
               for n in range(1, count + 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def safe_name(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name)


def result_line(result: dict) -> str:
    extra = ""
    if "password_on_page" in result:
        extra = f"  password_on_page={'yes' if result['password_on_page'] else 'no'}"
    if result.get("error"):
        extra += f"  error={result['error'][:120]}"
    return (f"    -> {result['task']} {'PASS' if result.get('passed') else 'FAIL'}  run={result.get('run_id')}"
            f"  state={result.get('final_state')}{extra}  {result.get('elapsed_s', 0)}s")


def summarize(results: list[dict]) -> dict:
    """Overall and per-family pass counts."""
    families: dict[str, dict] = defaultdict(lambda: {"passed": 0, "total": 0})
    for result in results:
        entry = families[str(result.get("family") or "other")]
        entry["total"] += 1
        entry["passed"] += int(bool(result.get("passed")))
    for entry in families.values():
        entry["rate"] = round(entry["passed"] / entry["total"], 3) if entry["total"] else None
    passed = sum(int(bool(r.get("passed"))) for r in results)
    return {"passed": passed, "total": len(results), "rate": round(passed / len(results), 3) if results else None,
            "by_family": dict(sorted(families.items())),
            "timed_out": sum(int(bool(r.get("timed_out"))) for r in results),
            "driver_errors": sum(int(r.get("final_state") == "DRIVER_ERROR") for r in results)}


def summary_text(summary: dict, title: str) -> str:
    lines = [f"{title}: {summary['passed']}/{summary['total']} passed"
             + (f" ({summary['rate'] * 100:.0f}%)" if summary["rate"] is not None else "")]
    for family, entry in summary["by_family"].items():
        lines.append(f"  {family:22} {entry['passed']}/{entry['total']}")
    return "\n".join(lines)


def write_scorecard(out: Path, results: list[dict], *, wall_s: float, planned: int, extra: dict | None = None) -> None:
    ordered = sorted(results, key=lambda r: str(r.get("task")))
    scorecard = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "planned": planned,
        "wall_s": round(wall_s, 1), "summary": summarize(results),
        "results": [{k: r.get(k) for k in ("task", "family", "passed", "run_id", "final_state", "elapsed_s",
                                           "password_on_page", "raw_reward", "reason", "error", "lane")
                     if k in r} for r in ordered],
        **(extra or {}),
    }
    (out / "scorecard.json").write_text(json.dumps(scorecard, indent=2, default=str), encoding="utf-8")


def utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def default_out(prefix: str) -> Path:
    return REPO / "storage" / "artifacts" / "eval_runs" / f"{prefix}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
