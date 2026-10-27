"""Serve FormFactory on a chosen port, writing submissions under a chosen directory.

Run with the FormFactory venv's Python (Flask lives there, not in the API's environment):

    storage/open_bench/venvs/formfactory/Scripts/python.exe apps/api/app/agent_eval/open_bench/formfactory_server.py \
        --port 5055 --run-dir storage/open_bench/formfactory-run

Upstream ``python app.py`` binds :5000 with the reloader and writes ``./submission/<template>.json`` (one list
per form, appended per POST, plus ``submission_time``). This launcher loads the same app unmodified, changes
directory to ``--run-dir`` first so that ``submission/`` lands there, and serves without the reloader.
Stdlib plus Flask only; it imports nothing from the Lumen API.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path


def load_app(source: Path):
    spec = importlib.util.spec_from_file_location("formfactory_app", source / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["formfactory_app"] = module  # Flask resolves templates/ from the module's file
    spec.loader.exec_module(module)
    return module.app


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=5055)
    parser.add_argument("--run-dir", required=True, help="working directory; submissions go to <run-dir>/submission/")
    parser.add_argument("--source", default=os.environ.get("FORMFACTORY_SOURCE") or str(
        Path(__file__).resolve().parents[5] / "storage" / "open_bench" / "src" / "formfactory"))
    args = parser.parse_args()
    run_dir = Path(args.run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    flask_app = load_app(Path(args.source).resolve())
    os.chdir(run_dir)
    flask_app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
