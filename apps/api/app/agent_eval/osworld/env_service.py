"""OSWorld environment service (runs in WSL with OSWorld's own venv, cwd = the OSWorld checkout).

Holds one ``DesktopEnv`` (Docker provider) and exposes it over HTTP so Lumen's runner on Windows can
reset a task, drive the VM through its in-VM server, and get OSWorld's own score:

    cd ~/osworld && .venv/bin/python /path/to/env_service.py --port 7010

POST /reset  {"domain", "example_id"}  -> {"server_url", "instruction", ...}
POST /finish {"final": "DONE"|"FAIL"}  -> {"score"}
POST /close
GET  /health
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import traceback

sys.path.insert(0, os.getcwd())

from flask import Flask, jsonify, request  # noqa: E402

from desktop_env.desktop_env import DesktopEnv  # noqa: E402

app = Flask(__name__)
LOCK = threading.Lock()
STATE: dict = {"env": None, "task": None}
logging.basicConfig(level=logging.INFO)


def _env() -> DesktopEnv:
    if STATE["env"] is None:
        STATE["env"] = DesktopEnv(
            provider_name="docker",
            os_type="Ubuntu",
            action_space="pyautogui",
            headless=True,
            require_a11y_tree=False,
            screen_size=(1920, 1080),
        )
    return STATE["env"]


@app.get("/health")
def health():
    env = STATE["env"]
    return jsonify({"ok": True, "env_started": env is not None, "server_port": getattr(env, "server_port", None), "task": STATE["task"]})


@app.post("/reset")
def reset():
    body = request.get_json(force=True) or {}
    domain, example_id = str(body.get("domain") or ""), str(body.get("example_id") or "")
    path = os.path.join("evaluation_examples", "examples", domain, f"{example_id}.json")
    if not os.path.isfile(path):
        return jsonify({"ok": False, "error": f"no task {path}"}), 404
    with open(path, encoding="utf-8") as handle:
        task = json.load(handle)
    with LOCK:
        try:
            env = _env()
            env.reset(task_config=task)
        except Exception as exc:  # noqa: BLE001 - reported to the runner, which records the task as errored
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()[-2000:]}), 500
        STATE["task"] = {"domain": domain, "example_id": example_id}
        return jsonify({
            "ok": True,
            "server_url": f"http://localhost:{env.server_port}",
            "vnc_url": f"http://localhost:{env.vnc_port}",
            "instruction": task.get("instruction", ""),
            "evaluator_func": (task.get("evaluator") or {}).get("func"),
            "related_apps": task.get("related_apps", []),
        })


@app.post("/finish")
def finish():
    body = request.get_json(force=True) or {}
    final = "FAIL" if str(body.get("final") or "").upper() == "FAIL" else "DONE"
    with LOCK:
        env = STATE["env"]
        if env is None:
            return jsonify({"ok": False, "error": "no environment"}), 409
        try:
            env.step(final, pause=1)
            score = float(env.evaluate())
        except Exception as exc:  # noqa: BLE001
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()[-2000:]}), 500
        return jsonify({"ok": True, "score": score, "final": final, "task": STATE["task"]})


@app.post("/close")
def close():
    with LOCK:
        if STATE["env"] is not None:
            STATE["env"].close()
            STATE["env"] = None
    return jsonify({"ok": True})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=7010)
    args = parser.parse_args()
    app.run(host="0.0.0.0", port=args.port, threaded=True)
