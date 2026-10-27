"""Serve MiniWoB++ for Lumen: the benchmark's own HTML plus a shim that makes each page one scored episode.

The server mounts MiniWoB's ``html`` directory under ``/eval/bench/miniwob/`` (the prefix the bench lanes'
close-bench-tabs cleanup matches). A task is ``/eval/bench/miniwob/miniwob/<task>.html?seed=<n>&sid=<id>``.
Every task page gets the shim (SHIM_JS) injected right after ``core.js``. Mirroring BrowserGym's MiniWoB env,
the shim seeds ``Math.seedrandom(seed)``, sets a very long episode time, starts the first episode itself (no
START click) and, when the episode ends, POSTs the raw reward to ``/__bench/report`` and covers the page with
a neutral "task ended" screen (no retry, no reward display).

    GET  /eval/bench/miniwob/...        MiniWoB files; task pages carry the shim
    POST /__bench/start                 {sid, task, seed, utterance}   (the shim, once the episode started)
    POST /__bench/report                {sid, task, seed, raw_reward, reward, reason}   (the shim, at episode end)
    GET  /__bench/state?sid=<id>        what the server holds for that page load
Stdlib only.
"""

from __future__ import annotations

import re
import threading
import urllib.parse
from pathlib import Path

from app.agent_eval.open_bench import bench_common as bc

MOUNT = "/eval/bench/miniwob/"
SHIM_PATH = MOUNT + "core/__lumen_shim.js"
SHIM_TAG = '<script src="../core/__lumen_shim.js"></script>'
_CORE_TAG = re.compile(r"""<script[^>]*\bsrc\s*=\s*["']\.\./core/core\.js["'][^>]*>\s*</script>""", re.I)

SHIM_JS = r"""(function () {
  if (window.__lumenMiniwobShim || typeof core === 'undefined') { return; }
  window.__lumenMiniwobShim = true;
  var qs = new URLSearchParams(location.search);
  var seed = qs.get('seed') || '1';
  var sid = qs.get('sid') || '';
  var task = (location.pathname.split('/').pop() || '').replace(/\.html$/, '');
  function post(path, body) {
    body.sid = sid; body.task = task; body.seed = seed;
    try {
      fetch('/__bench/' + path, {method: 'POST', headers: {'Content-Type': 'application/json'},
                                 body: JSON.stringify(body), keepalive: true});
    } catch (e) { /* the page still works without the server */ }
  }
  var style = document.createElement('style');
  style.textContent = '#reward-display, #sync-task-cover { display: none !important; }';
  document.head.appendChild(style);

  // No time limit: the runner's budget is the limit. Many task scripts assign core.EPISODE_MAX_TIME (20000
  // ...) after this shim runs, so the property ignores writes instead of being set once.
  Object.defineProperty(core, 'EPISODE_MAX_TIME', {
    configurable: true, enumerable: true,
    get: function () { return 1000000000; },
    set: function () { /* a task's own duration is ignored */ }
  });
  var originalStart = core.startEpisode;
  var originalEnd = core.endEpisode;
  var started = false, ending = false, done = false;

  core.startEpisode = function () {
    if (started || ending || done) { return; }  // the page's own restarts after an episode end are ignored
    started = true;
    Math.seedrandom(seed);
    originalStart.call(core);       // builds the display and the sync cover this version of core.js expects
    core.startEpisodeReal();        // BrowserGym: core.startEpisodeReal() instead of a START click
    post('start', {utterance: core.getUtterance()});
    setTimeout(function () { if (!done) { post('start', {utterance: core.getUtterance()}); } }, 600);
  };

  core.endEpisode = function () {
    if (done || ending || core.EP_TIMER === null) { return; }
    ending = true;
    try { originalEnd.apply(core, arguments); } finally { ending = false; }
    done = true;
    post('report', {raw_reward: WOB_RAW_REWARD_GLOBAL, reward: WOB_REWARD_GLOBAL, reason: WOB_REWARD_REASON});
    var cover = document.createElement('div');
    cover.id = 'lumen-task-ended';
    cover.setAttribute('style', 'position:fixed;left:0;top:0;width:100%;height:100%;z-index:2147483647;' +
      'background:#f2f2f2;color:#444;font:20px sans-serif;display:flex;align-items:center;justify-content:center;');
    cover.textContent = 'The task has ended.';
    document.body.appendChild(cover);
  };
})();
"""


def inject_shim(html: str) -> tuple[str, bool]:
    """``html`` with the shim script tag right after the core.js tag; (html, whether core.js was found)."""
    match = _CORE_TAG.search(html)
    if not match:
        return html, False
    return html[:match.end()] + "\n" + SHIM_TAG + html[match.end():], True


class EpisodeStore:
    """What the shim reported, by page-load id (sid). The first report of a sid is its result."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pages: dict[str, dict] = {}

    def start(self, sid: str, task: str, seed: str, utterance: str) -> None:
        with self._lock:
            page = self._pages.setdefault(sid, {"sid": sid, "task": task, "seed": seed, "starts": 0})
            page["starts"] += 1
            if utterance:
                page["utterance"] = utterance

    def report(self, sid: str, task: str, seed: str, raw_reward, reward, reason) -> None:
        with self._lock:
            page = self._pages.setdefault(sid, {"sid": sid, "task": task, "seed": seed, "starts": 0})
            if "raw_reward" not in page:  # first end wins: no retry
                page.update({"raw_reward": raw_reward, "reward": reward, "reason": reason})

    def get(self, sid: str) -> dict | None:
        with self._lock:
            page = self._pages.get(sid)
            return dict(page) if page else None


class Handler(bc.QuietHandler):
    root: Path
    store: EpisodeStore

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/__bench/state":
            sid = (urllib.parse.parse_qs(parsed.query).get("sid") or [""])[0]
            self.send_json(self.store.get(sid) or {})
            return
        if parsed.path == SHIM_PATH:
            self.send_bytes(200, SHIM_JS.encode("utf-8"), "text/javascript; charset=utf-8")
            return
        if not parsed.path.startswith(MOUNT):
            self.send_bytes(404, b"not found", "text/plain")
            return
        target = bc.safe_join(self.root, parsed.path[len(MOUNT):])
        if target is None or not target.is_file():
            self.send_bytes(404, b"not found", "text/plain")
            return
        body = target.read_bytes()
        if target.suffix == ".html" and target.parent.name == "miniwob":
            text, _ = inject_shim(body.decode("utf-8", errors="replace"))
            body = text.encode("utf-8")
        self.send_bytes(200, body, bc.content_type(target))

    do_HEAD = do_GET

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        data = self.read_json()
        sid, task, seed = str(data.get("sid") or ""), str(data.get("task") or ""), str(data.get("seed") or "")
        if path == "/__bench/start":
            self.store.start(sid, task, seed, str(data.get("utterance") or ""))
        elif path == "/__bench/report":
            self.store.report(sid, task, seed, data.get("raw_reward"), data.get("reward"), data.get("reason"))
        else:
            self.send_bytes(404, b"not found", "text/plain")
            return
        self.send_json({})


def start_server(html_root: Path) -> tuple[bc.BenchServer, EpisodeStore]:
    store = EpisodeStore()
    server = bc.BenchServer(Handler, root=Path(html_root).resolve(), store=store).start()
    return server, store


def task_url(base: str, task: str, seed: int, sid: str) -> str:
    return f"{base.rstrip('/')}{MOUNT}miniwob/{task}.html?seed={seed}&sid={sid}"
