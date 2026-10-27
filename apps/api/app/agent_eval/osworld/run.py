"""Run Lumen on OSWorld tasks: one real Lumen desktop run per task, scored by OSWorld's own evaluator.

    python -m app.agent_eval.osworld.run --set test_all --claim --out <dir> --env http://localhost:7010

Needs ``env_service.py`` running in WSL (see its docstring). Each task: the service resets the VM and
runs OSWorld's setup; this runner creates a Lumen run (execution_surface=desktop) with the task
instruction as the goal and drives ``run_vision_loop`` with ``OSWorldBody``; then the service scores
the VM state. Lumen declares a task impossible only by ending ``external_workflow_rejected``; that
is sent to OSWorld as FAIL, anything else as DONE.

Parallel: start one env service per VM (ports 7010, 7011, ...) and one runner per service, all with
the same ``--out`` and ``--claim``; each task is claimed by exactly one worker. ``--summarize`` prints
the aggregate of an out dir.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from app.agent_eval.osworld.body import OSWorldBody
from app.config import get_settings
from app.db import SessionLocal, init_db


def _post(env_url: str, path: str, payload: dict[str, Any], timeout: float = 900.0) -> dict[str, Any]:
    response = httpx.post(f"{env_url}{path}", json=payload, timeout=timeout)
    try:
        body = response.json()
    except Exception:
        body = {"ok": False, "error": response.text[:500]}
    body.setdefault("http_status", response.status_code)
    return body


def _brain_cost(run_id: int) -> tuple[float, int]:
    import sqlite3

    db = sqlite3.connect(str(get_settings().database_url).replace("sqlite:///", ""), timeout=30)
    cost, decisions = 0.0, 0
    for (payload,) in db.execute(
        "select event_payload from cognitive_events where run_id=? and event_type='VISION_MODEL_TRANSPORT_RECEIPT'", (run_id,)
    ):
        cost += float(json.loads(payload).get("cost_usd") or 0)
        decisions += 1
    return cost, decisions


def _code_version() -> str:
    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=10).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "."], capture_output=True, text=True, timeout=10).stdout.strip()
        return head + ("+dirty" if dirty else "")
    except Exception:
        return "unknown"


def _claim(out_dir: Path, name: str) -> bool:
    """One worker per task across processes sharing an out dir: an exclusive claim file."""
    try:
        handle = os.open(str(out_dir / f"{name}.claim"), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    os.write(handle, f"{os.getpid()} {time.time():.0f}".encode())
    os.close(handle)
    return True


def summarize(out_dir: Path) -> dict[str, Any]:
    results = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(out_dir.glob("*__*.json"))]
    by_domain: dict[str, list[float]] = {}
    for item in results:
        by_domain.setdefault(item["domain"], []).append(float(item.get("score") or 0.0))
    reasons: dict[str, int] = {}
    for item in results:
        if float(item.get("score") or 0) < 1:
            key = item.get("status") if item.get("status") != "scored" else f"{item.get('lumen_state')}:{item.get('lumen_reason')}"
            reasons[str(key)] = reasons.get(str(key), 0) + 1
    total = sum(float(item.get("score") or 0) for item in results)
    return {
        "out_dir": str(out_dir),
        "tasks": len(results),
        "score_sum": round(total, 3),
        "success_rate": round(total / max(1, len(results)), 4),
        "env_errors": sum(1 for item in results if item.get("status") != "scored"),
        "by_domain": {
            domain: {"n": len(scores), "score": round(sum(scores), 2), "rate": round(sum(scores) / len(scores), 3)}
            for domain, scores in sorted(by_domain.items())
        },
        "failure_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "brain_cost_usd": round(sum(float(item.get("brain_cost_usd") or 0) for item in results), 3),
        "task_seconds": round(sum(float(item.get("elapsed_s") or 0) for item in results), 1),
        "code_versions": sorted({str(item.get("code_version") or "") for item in results}),
    }


async def run_task(env_url: str, domain: str, example_id: str, *, user_id: int, max_steps: int) -> dict[str, Any]:
    from app.extension_run import run_vision_loop
    from app.runtime import OperatorRuntimeEngine

    started = time.monotonic()
    reset = await asyncio.to_thread(_post, env_url, "/reset", {"domain": domain, "example_id": example_id})
    record: dict[str, Any] = {"domain": domain, "example_id": example_id, "instruction": reset.get("instruction", ""),
                              "evaluator_func": reset.get("evaluator_func")}
    if not reset.get("ok"):
        return {**record, "status": "env_reset_error", "error": reset.get("error"), "score": 0.0,
                "elapsed_s": round(time.monotonic() - started, 1)}
    setup_s = round(time.monotonic() - started, 1)
    with SessionLocal() as db:
        run = OperatorRuntimeEngine(db).create_browser_task(
            user_id=user_id, goal=record["instruction"], target_url="desktop://osworld", execution_surface="desktop"
        )
        run_id = int(run.id)
    last_status: list[str] = []

    async def narrate(kind: str, data: dict[str, Any]) -> None:
        summary = str((data or {}).get("summary") or (data or {}).get("reason") or "")[:140]
        if summary and summary != (last_status[-1] if last_status else ""):
            last_status.append(summary)
            print(f"      [{kind}] {summary}", flush=True)

    body = OSWorldBody(reset["server_url"])
    loop_started = time.monotonic()
    try:
        summary = await run_vision_loop(body, url=None, narrate=narrate, run_id=run_id, user_id=user_id,
                                        skip_navigate=True, max_steps=max_steps)
    except Exception as exc:  # noqa: BLE001 - one broken task must not end the benchmark
        summary = {"state": "ERROR", "reason": f"{type(exc).__name__}: {exc}"}
    loop_s = round(time.monotonic() - loop_started, 1)
    state, reason = str(summary.get("state") or ""), str(summary.get("reason") or "")
    final = "FAIL" if state == "BLOCKED" and reason == "external_workflow_rejected" else "DONE"
    scored = await asyncio.to_thread(_post, env_url, "/finish", {"final": final})
    cost, decisions = _brain_cost(run_id)
    return {
        **record,
        "status": "scored" if scored.get("ok") else "eval_error",
        "score": float(scored.get("score") or 0.0),
        "eval_error": scored.get("error"),
        "final_sent": final,
        "lumen_state": state,
        "lumen_reason": reason,
        "run_id": run_id,
        "decisions": decisions,
        "brain_cost_usd": round(cost, 4),
        "setup_s": setup_s,
        "loop_s": loop_s,
        "elapsed_s": round(time.monotonic() - started, 1),
        "last_status": last_status[-3:],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--env", default="http://localhost:7010", help="env_service URL")
    parser.add_argument("--set", default="test_small", help="evaluation_examples/<set>.json in the OSWorld checkout")
    parser.add_argument("--osworld", default=r"\\wsl.localhost\Ubuntu\home\aakas\osworld", help="OSWorld checkout path (for the task list)")
    parser.add_argument("--domains", help="comma-separated domain filter")
    parser.add_argument("--tasks-file", help="JSON list of [domain, example_id] to run instead of --set")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--max-steps", type=int, default=50)
    parser.add_argument("--user-id", type=int, default=2)
    parser.add_argument("--out", help="output dir (default storage/artifacts/eval_runs/osworld-<set>-<stamp>)")
    parser.add_argument("--skip-done", action="store_true", help="skip tasks already scored in --out")
    parser.add_argument("--claim", action="store_true", help="share --out with other workers: claim each task exclusively")
    parser.add_argument("--summarize", action="store_true", help="only print the summary of --out")
    args = parser.parse_args()
    if args.summarize:
        print(json.dumps(summarize(Path(args.out)), indent=2))
        return 0

    init_db()
    if args.tasks_file:
        tasks = [tuple(item) for item in json.loads(Path(args.tasks_file).read_text(encoding="utf-8"))]
    else:
        tasks_by_domain = json.loads((Path(args.osworld) / "evaluation_examples" / f"{args.set}.json").read_text(encoding="utf-8"))
        tasks = [(domain, example_id) for domain, ids in tasks_by_domain.items() for example_id in ids]
    wanted = {item.strip() for item in (args.domains or "").split(",") if item.strip()}
    tasks = [(domain, example_id) for domain, example_id in tasks if not wanted or domain in wanted]
    if args.limit:
        tasks = tasks[: args.limit]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(args.out) if args.out else get_settings().artifact_root / "eval_runs" / f"osworld-{args.set}-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    version = _code_version()
    print(f"OSWorld: {len(tasks)} tasks, max {args.max_steps} steps, env={args.env}, code={version}, out={out_dir}", flush=True)
    for position, (domain, example_id) in enumerate(tasks, start=1):
        name = f"{domain}__{example_id}"
        result_path = out_dir / f"{name}.json"
        if (args.skip_done or args.claim) and result_path.exists():
            continue
        if args.claim and not _claim(out_dir, name):
            continue
        print(f"[{position}/{len(tasks)}] {domain}/{example_id}", flush=True)
        result = asyncio.run(run_task(args.env, domain, example_id, user_id=args.user_id, max_steps=args.max_steps))
        result["code_version"] = version
        result["env"] = args.env
        result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"    -> score {result['score']:.2f}  {result.get('lumen_state')} {result.get('lumen_reason')}  "
              f"{result.get('decisions', 0)} decisions  {result.get('elapsed_s')}s  ${result.get('brain_cost_usd', 0)}", flush=True)
    summary = summarize(out_dir)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
