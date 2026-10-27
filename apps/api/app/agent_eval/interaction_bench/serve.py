"""Standalone static server for the Interaction Gauntlet pages — stdlib only.

Usage:  python serve.py [--port 8020]

Serves the pages/ directory next to this file. Extensionless names resolve to <name>.html and any
path prefix is ignored, so both /page_dynamics and /eval/bench/page_dynamics work — point the agent
under test at  http://127.0.0.1:<port>/eval/bench/<name> .
"""

from __future__ import annotations

import argparse
import http.server
import re
from pathlib import Path

PAGES_DIR = Path(__file__).parent / "pages"


class GauntletHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PAGES_DIR), **kwargs)

    def translate_path(self, path):
        name = path.split("?")[0].split("#")[0].rstrip("/").rsplit("/", 1)[-1]
        name = re.sub(r"[^a-zA-Z0-9_.-]", "", name)
        if name and "." not in name:
            name += ".html"
        return str(PAGES_DIR / name) if name else str(PAGES_DIR)

    def log_message(self, fmt, *args):
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the Interaction Gauntlet pages.")
    parser.add_argument("--port", type=int, default=8020)
    args = parser.parse_args()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), GauntletHandler)
    print(f"Interaction Gauntlet at http://127.0.0.1:{args.port}/eval/bench/<page>")
    print("Pages:", ", ".join(sorted(p.stem for p in PAGES_DIR.glob("*.html"))))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
