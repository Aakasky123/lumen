"""Serve the built WebGames SPA (webgames/dist) with SPA fallback and stubbed analytics endpoints.

    GET  <file in dist>                 static asset (assets/*, code_gen.wasm, favicon ...)
    GET  <any other path>               index.html (the app's browser router resolves /<task>)
    POST /api/record-view               {"success": true}   (upstream's Cloudflare functions, stubbed)
    POST /api/record-completion         {"success": true}

The app is routed from the origin root (createBrowserRouter, no basename, root-absolute asset URLs), so it cannot
sit under /eval/bench/ and the lanes' close-bench-tabs cleanup does not see it: the runner gives every task a
fresh lane instead. Stdlib only.
"""

from __future__ import annotations

import threading
import urllib.parse
from pathlib import Path

from app.agent_eval.open_bench import bench_common as bc

STUBBED_POSTS = {"/api/record-view", "/api/record-completion"}


class Handler(bc.QuietHandler):
    root: Path
    visits: list
    visits_lock: threading.Lock

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path.startswith("/api/"):
            self.send_bytes(404, b"not found", "text/plain")
            return
        target = bc.safe_join(self.root, path)
        if target is not None and target.is_file():
            self.send_bytes(200, target.read_bytes(), bc.content_type(target))
            return
        if Path(path).suffix and path.startswith("/assets/"):
            self.send_bytes(404, b"not found", "text/plain")  # a missing asset is not a page
            return
        with self.visits_lock:
            self.visits.append(path)
        self.send_bytes(200, (self.root / "index.html").read_bytes(), "text/html; charset=utf-8")

    do_HEAD = do_GET

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        self.read_json()
        if path in STUBBED_POSTS:
            self.send_json({"success": True})
        else:
            self.send_bytes(404, b"not found", "text/plain")


def start_server(dist: Path) -> tuple[bc.BenchServer, list]:
    dist = Path(dist).resolve()
    if not (dist / "index.html").is_file():
        raise SystemExit(f"no built WebGames at {dist}: run `python -m app.agent_eval.open_bench.sources fetch`")
    visits: list = []
    server = bc.BenchServer(Handler, root=dist, visits=visits, visits_lock=threading.Lock()).start()
    return server, visits
