"""Run the macOS desktop eval against PRODUCTION Lumen: each task is a chat-started run on this Mac's
desktop body, graded from the end state it leaves in the task's sandbox folder (tasks.py).

It reuses the Interaction Gauntlet's run machinery (live_run: chat start, watch with auto-resume,
leftover-run cleanup, provenance, model usage, scorecard), but needs no Chrome extension and no harness:
the apps are the real ones (TextEdit, Finder, Calculator, Safari, Preview, System Settings) and the grader is
the file system, the Accessibility tree, read-only system probes, and a loopback web page.

    python -m app.agent_eval.mac_bench.run --list
    python -m app.agent_eval.mac_bench.run
    python -m app.agent_eval.mac_bench.run --tasks finder_rename,calculator_product
    python -m app.agent_eval.mac_bench.run --focus textedit_new_doc --repeat 5
    python -m app.agent_eval.mac_bench.run --tier hard,long --repeat 3 --expect-vision-provider 14

--focus names the tasks to run alone (like the Gauntlet's --focus, a task has no inner challenges);
--repeat N runs each selected task N times as tagged runs (``task@rN``) that never replace one another,
in rounds (every task once, then every task again), and the scorecard's ``pass_rate_by_page`` gives each
task's pass rate with its spread. Tasks run one at a time: they share the screen.
validate_golden_path.py proves every task's setup, grader, traps and teardown without the agent.
Needs the API (LUMEN_API, default http://127.0.0.1:8001) with desktop control enabled and the macOS
privacy permissions granted. The run takes over the screen and keyboard while it works.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from app.agent_eval.interaction_bench import live_run as lr
from app.agent_eval.mac_bench.tasks import TASKS, Ctx, Sandbox, Task, bench_root, grade, purge_stale

REPO = lr.REPO
MANIFEST = {"benchmark": "Lumen macOS desktop bench", "version": 2}  # 2: sixteen tasks (eight harder ones added)


def plan_jobs(task_names: list[str] | None, repeat: int, tiers: list[str] | None = None) -> list[dict]:
    """The runs to make: each task once, or ``repeat`` times as ``task@rN`` jobs in rounds (every task's r1,
    then every task's r2), so a drift in the API or the machine spreads over all tasks rather than one."""
    names = list(dict.fromkeys(task_names)) if task_names else list(TASKS)
    unknown = [n for n in names if n not in TASKS]
    if unknown:
        raise SystemExit(f"unknown task {unknown[0]!r} (choose from: {', '.join(TASKS)})")
    if tiers:
        names = [n for n in names if TASKS[n].tier in tiers]
        if not names:
            raise SystemExit(f"no task in tier {', '.join(tiers)} (tiers: {', '.join(sorted({t.tier for t in TASKS.values()}))})")
    jobs = []
    for n in range(1, max(1, repeat) + 1):
        for name in names:
            tag = f"r{n}" if repeat > 1 else ""
            jobs.append({"name": name, "job": name + (f"@{tag}" if tag else ""), "tag": tag, "task": TASKS[name]})
    return jobs


def _surface(run: dict) -> str | None:
    task = (run.get("confidence_state") or {}).get("browser_task")
    return str(task.get("execution_surface")) if isinstance(task, dict) and task.get("execution_surface") else None


def run_job(job: dict, *, root: Path, timeout_s: float, poll_s: float, auto_resume: int) -> dict:
    """Set up, drive one run to a terminal state, grade the end state, and always tear down."""
    task: Task = job["task"]
    key = job["job"]
    started = time.time()
    ctx = Ctx(Sandbox(task.name, root))
    result: dict = {
        "page": key, "base_page": task.name, "focus": None, "tag": job["tag"], "perturb": [], "seed": None,
        "family": task.family, "generation": None, "tier": task.tier, "lane": "user",
    }
    try:
        goal = task.setup(ctx)
        result["goal"] = goal
        # The task's own random values (expected text, codes and decoys, the autosave setting) for reading a failure.
        result["setup_state"] = {k: v for k, v in ctx.state.items() if v is None or isinstance(v, (str, int, float, bool))}
        lr._clear_active_runs(None, label="")
        before = {int(r["id"]) for r in lr._request("GET", "/operator/runs")}
        thread = lr._request("POST", "/operator/chat/threads", {"title": f"Mac bench {key}"})
        chat = lr._stream_chat(thread["id"], goal)
        run_id = chat["run_id"]
        if run_id is None:
            latest = lr._latest_run_after(before)
            run_id = int(latest["id"]) if latest else None
        result.update(thread_id=thread["id"], run_id=run_id, assistant_text=chat["assistant_text"][:1200], chat_meta=chat["meta"])
        if run_id is None:
            result["final_state"] = "NO_RUN_STARTED"
        else:
            deadline = time.time() + timeout_s
            watched = lr._watch(int(run_id), timeout_s, poll_s, auto_resume)
            approvals = 0
            # The bench stands in for the user: a final action on its own sandbox page is the task's goal, so in
            # Approval mode it approves it through the same structured control the UI uses (never a payment).
            while (
                watched["state"] == "WAITING_FOR_USER"
                and approvals < MAX_FINAL_APPROVALS
                and time.time() < deadline
                and _approve_pending_final(int(run_id))
            ):
                approvals += 1
                # The run reads WAITING_FOR_USER for a moment after the approval while it resumes: wait for it to
                # leave that state before watching again, or the watch would end on the stale state.
                left_by = time.time() + 60
                while time.time() < left_by:
                    if lr._request("GET", f"/operator/runs/{run_id}").get("current_state") != "WAITING_FOR_USER":
                        break
                    time.sleep(poll_s)
                watched = lr._watch(int(run_id), max(30.0, deadline - time.time()), poll_s, auto_resume)
            result["final_action_approvals"] = approvals
            run = watched["run"]
            try:
                events = lr._request("GET", f"/operator/runs/{run_id}/events", timeout=60)
            except Exception:  # noqa: BLE001
                events = []
            result.update(
                final_state=watched["state"], status_message=run.get("status_message"), timed_out=watched["timed_out"],
                resumes=watched["resumes"], transitions=watched["transitions"], event_count=len(events),
                surface=_surface(run), model_usage=lr._model_usage(events),
            )
        time.sleep(2)  # let the last save / keystroke land before reading the end state
        result["grade"] = task.check(ctx)
        result["end_state"] = _describe_end_state(ctx)
    except Exception as exc:  # noqa: BLE001 - one task's crash must not kill the suite
        result.update(final_state=result.get("final_state") or "DRIVER_ERROR", error=f"{exc.__class__.__name__}: {exc}")
        result["grade"] = grade({"driver_error": False})
    finally:
        try:
            task.teardown(ctx)
        except Exception as exc:  # noqa: BLE001
            result["teardown_error"] = str(exc)
        try:
            ctx.sandbox.destroy()
        except Exception as exc:  # noqa: BLE001
            result["teardown_error"] = str(exc)
    if result.get("surface") not in (None, "desktop"):
        result["grade"]["surface_warning"] = f"the run used the {result['surface']} surface, not the desktop body"
    result["elapsed_s"] = round(time.time() - started, 1)
    return result


MAX_FINAL_APPROVALS = 2


def _approve_pending_final(run_id: int) -> bool:
    """Approve the run's pending final-action question with its rendered token, as the UI's approve button does.
    A payment approval, or a question without a token, is left alone (the run then grades as it stands)."""
    questions = lr._request("GET", f"/operator/runs/{run_id}/questions") or []
    for question in reversed(questions):
        if question.get("question_type") != "final_action_approval" or question.get("status") != "pending":
            continue
        final = (question.get("expected_answer_schema") or {}).get("final_action") or {}
        token = str(final.get("approval_token") or "")
        if not token or final.get("payment_floor"):
            return False
        choices = final.get("choices") or []
        label = next((c.get("label") for c in choices if c.get("decision") == "approve"), None) or "Approve"
        lr._request(
            "POST",
            f"/operator/questions/{question['id']}/answer",
            {"answer": label, "approval_token": token, "final_action_decision": "approve", "auto_resume": True},
        )
        return True
    return False


def _wilson(passed: int, runs: int, z: float = 1.96) -> list[float]:
    """The 95% Wilson interval of a pass rate: with three runs, 3/3 still reads 0.44-1.0."""
    if not runs:
        return [0.0, 1.0]
    p = passed / runs
    centre = (p + z * z / (2 * runs)) / (1 + z * z / runs)
    half = z * math.sqrt(p * (1 - p) / runs + z * z / (4 * runs * runs)) / (1 + z * z / runs)
    return [round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3)]


def _usage_cost(usage: dict | None) -> float | None:
    if not isinstance(usage, dict) or usage.get("decision_cost_usd") is None:
        return None
    return sum(float(usage.get(k) or 0.0) for k in ("decision_cost_usd", "hedge_extra_cost_usd", "judge_cost_usd"))


def _usage_decisions(usage: dict | None) -> int | None:
    by_model = usage.get("decisions_by_model") if isinstance(usage, dict) else None
    return sum(int(v) for v in by_model.values()) if isinstance(by_model, dict) else None


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def pass_rate_spread(results: list[dict]) -> dict:
    """Per task across its ``--repeat`` runs: the Gauntlet's ``runs/passed/pass_rate`` plus how much the runs
    spread (the pass rate's 95% interval, the check ratio's range and deviation, traps, end states, time, cost)."""
    by_task: dict[str, list[dict]] = {}
    for r in results:
        by_task.setdefault(str(r["page"]).split("@", 1)[0], []).append(r)
    out: dict[str, dict] = {}
    for task, runs in by_task.items():
        passed = sum(int(bool(r["grade"].get("passed"))) for r in runs)
        ratios = [float(r["grade"].get("ratio") or 0.0) for r in runs]
        elapsed = [float(r["elapsed_s"]) for r in runs if r.get("elapsed_s") is not None]
        costs = [c for c in (_usage_cost(r.get("model_usage")) for r in runs) if c is not None]
        decisions = [d for d in (_usage_decisions(r.get("model_usage")) for r in runs) if d is not None]
        states: dict[str, int] = {}
        for r in runs:
            states[str(r.get("final_state"))] = states.get(str(r.get("final_state")), 0) + 1
        out[task] = {
            "runs": len(runs), "passed": passed, "pass_rate": passed / len(runs),
            "pass_rate_ci95": _wilson(passed, len(runs)),
            "ratio_mean": _mean(ratios), "ratio_min": min(ratios), "ratio_max": max(ratios),
            "ratio_stdev": round(statistics.pstdev(ratios), 4),
            "traps_sprung": sum(int(bool(r["grade"].get("failed"))) for r in runs),
            "final_states": states,
            "elapsed_s_median": round(statistics.median(elapsed), 1) if elapsed else None,
            "elapsed_s_min": min(elapsed) if elapsed else None, "elapsed_s_max": max(elapsed) if elapsed else None,
            "decisions_mean": _mean([float(d) for d in decisions]),
            "cost_usd_mean": _mean(costs),
        }
    return out


def write_scorecard(out: Path, results: list[dict], provenance: dict, *, wall_s: float | None = None) -> dict:
    """The Gauntlet's scorecard (same fields, so run_metrics and the A/B tooling read it), with
    ``pass_rate_by_page`` widened to the spread across repeats and the overall cost and flaky tasks."""
    lr._write_scorecard(out, results, MANIFEST, provenance, wall_s=wall_s)
    path = out / "scorecard.json"
    scorecard = json.loads(path.read_text(encoding="utf-8"))
    spread = pass_rate_spread(results)
    scorecard["pass_rate_by_page"] = spread
    costs = [c for c in (_usage_cost(r.get("model_usage")) for r in results) if c is not None]
    scorecard["overall"].update(
        tasks=len(spread),
        flaky_tasks=sorted(t for t, s in spread.items() if 0 < s["passed"] < s["runs"]),
        never_passed=sorted(t for t, s in spread.items() if s["passed"] == 0),
        cost_usd=round(sum(costs), 4) if costs else None,
    )
    path.write_text(json.dumps(scorecard, indent=2), encoding="utf-8")
    return scorecard


def _vision_provider_id(provenance: dict) -> int | None:
    roles = (provenance.get("model_roles") or {}) if isinstance(provenance, dict) else {}
    vision = roles.get("brain") or roles.get("vision") or {}
    try:
        return int(vision.get("id")) if vision.get("id") is not None else None
    except (TypeError, ValueError):
        return None


def _describe_end_state(ctx: Ctx) -> dict:
    from app.agent_eval.mac_bench.tasks import tree

    try:
        return {"sandbox_tree": tree(ctx.dir)}
    except OSError:
        return {}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="Run the macOS desktop eval on production Lumen.")
    parser.add_argument("--tasks", help="comma-separated task names (default: all)")
    parser.add_argument("--focus", help="comma-separated task names to run alone (same as --tasks)")
    parser.add_argument("--repeat", type=int, default=1, help="run every selected task this many times")
    parser.add_argument("--timeout-min", type=float, default=None, help="per-task run budget (default: each task's own)")
    parser.add_argument("--poll", type=float, default=5.0)
    parser.add_argument("--auto-resume", type=int, default=3)
    parser.add_argument("--out", help="output dir (default: storage/artifacts/eval_runs/macbench-<stamp>)")
    parser.add_argument("--tier", help="comma-separated tiers to run (basic, multi-step, hard, long; default: all)")
    parser.add_argument("--expect-vision-provider", type=int, default=None,
                        help="refuse to start unless the API's vision role is this model-provider row")
    parser.add_argument("--list", action="store_true", help="list the tasks and exit")
    args = parser.parse_args(argv)
    if args.list:
        for task in TASKS.values():
            print(f"{task.name:26s} {task.family:16s} {task.tier:10s} {task.budget_min:g} min"
                  + (f"  trap: {task.trap}" if task.trap else ""))
        return 0
    if args.tasks and args.focus:
        parser.error("--focus and --tasks both name tasks; use one")
    names = [n.strip() for n in (args.focus or args.tasks or "").split(",") if n.strip()] or None
    tiers = [t.strip() for t in (args.tier or "").split(",") if t.strip()] or None
    jobs = plan_jobs(names, args.repeat, tiers)

    status = lr._request("GET", "/operator/desktop/status")
    if not status.get("available") or not status.get("enabled"):
        print(f"Desktop control is not ready: available={status.get('available')} enabled={status.get('enabled')} "
              f"missing={status.get('permissions_missing')}. Enable it and grant the macOS permissions first.")
        return 2
    provenance = lr._provenance()
    if args.expect_vision_provider is not None and _vision_provider_id(provenance) != args.expect_vision_provider:
        print(f"The API's vision role is provider {_vision_provider_id(provenance)}, not {args.expect_vision_provider}: "
              "set the brain first (or drop --expect-vision-provider).")
        return 2
    root = bench_root()
    purged = purge_stale(root)
    if purged:
        print(f"removed {purged} stale bench folder(s) under {root}")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    out = Path(args.out) if args.out else REPO / "storage" / "artifacts" / "eval_runs" / f"macbench-{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    print(f"macOS desktop bench: {len(jobs)} run(s), sandboxes under {root}, out={out}", flush=True)
    results: list[dict] = []
    started_at = time.time()
    for position, job in enumerate(jobs, 1):
        task: Task = job["task"]
        print(f"[{position}/{len(jobs)}] {job['job']} ({task.family}, {task.tier})", flush=True)
        budget_min = args.timeout_min if args.timeout_min is not None else task.budget_min
        res = run_job(job, root=root, timeout_s=budget_min * 60, poll_s=args.poll, auto_resume=args.auto_resume)
        results.append(res)
        (out / f"{job['job'].replace('@', '__')}.json").write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
        write_scorecard(out, results, provenance, wall_s=time.time() - started_at)
        g = res["grade"]
        print(f"    -> {job['job']} {'PASS' if g.get('passed') else 'FAIL'}  checks {g.get('completed', 0)}/{g.get('total', 0)}"
              f"  trap={'yes: ' + str(g.get('fail_reason')) if g.get('failed') else 'no'}  state={res.get('final_state')}  {res['elapsed_s']}s"
              + (f"  WARNING {g['surface_warning']}" if g.get("surface_warning") else ""), flush=True)
    print(f"done: {sum(int(bool(r['grade'].get('passed'))) for r in results)}/{len(results)} passed in "
          f"{(time.time() - started_at) / 60:.1f} min; scorecard {out / 'scorecard.json'}")
    for name, s in pass_rate_spread(results).items():
        lo, hi = s["pass_rate_ci95"]
        print(f"    {name:26s} {s['passed']}/{s['runs']} (95% {lo:.2f}-{hi:.2f})  ratio {s['ratio_min']:.2f}-{s['ratio_max']:.2f}"
              f"  traps {s['traps_sprung']}  median {s['elapsed_s_median']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
