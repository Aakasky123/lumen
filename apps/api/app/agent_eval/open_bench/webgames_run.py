"""Run Lumen on WebGames challenges (headless bench lanes); success = the task password in Lumen's answer.

    python -m app.agent_eval.open_bench.webgames_run --api http://127.0.0.1:8021 --lanes 3 --out DIR
    python -m app.agent_eval.open_bench.webgames_run --list
    python -m app.agent_eval.open_bench.webgames_run --api ... --tasks date,patience-easy,maze-easy
    python -m app.agent_eval.open_bench.webgames_run --api ... --categories timed,scroll

The goal is the official prompt (evals/inspect_ai_webgames.py) with the task description and the local URL.
Score, like the official scorer: the task's password occurs in the answer (the run's final status message or any
assistant message of its chat thread). The runner also records whether the password was shown on the final
page (``password_on_page``): a pass with the password never on the page would be a leak, a fail with it on the
page is a reporting failure. Excluded (the owner's rule): i-accept*, robo-check*, verify-challenge*,
illegal-material*. The SPA cannot sit under /eval/bench/, so every task gets a fresh lane (README.md).
Nothing here assumes a model provider.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from app.agent_eval.open_bench import bench_common as bc
from app.agent_eval.open_bench import webgames_server as srv

DEFAULT_API = "http://127.0.0.1:8021"
EXCLUDED_PREFIXES = ("i-accept", "robo-check", "verify-challenge", "illegal-material")
TIMEOUT_MIN = 10.0
TASK_PROMPT = ("Your task is: {description}. You must go to {homepage} and obtain the password for the game. "
               "To prove you have completed the task you must obtain and report the password for the game. "
               "If you do not have the password, you have not managed to complete the task.")

# ~30 tasks across the categories (first tag); the real-time ones: click-cubed-easy, patience-easy,
# bullseye-easy, button-hold, frog-crossing-easy.
DEFAULT_SAMPLE = [
    "date", "dateeasy", "buttons-easy", "click-cubed-easy", "patience-easy", "bullseye-easy", "button-hold",
    "frog-crossing-easy", "slider-symphony-easy", "emoji-remember-easy", "wolf-goat-cabbage-easy",
    "towers-of-hanoi-easy", "color-harmony-easy", "canvas-catch-easy", "text-mirror", "key-combo",
    "scroll-vertical", "scroll-horizontal", "webgl-text-easy", "file-credentials-easy", "menu-navigator",
    "chart-read-easy", "prompt-defender-easy", "shopping-challenge-easy", "maze-easy", "iframe-nest-easy",
    "otp-entry-easy", "right-click", "calendar-comprehension-easy", "shop-admin-easy",
]


def source_root() -> Path:
    return bc.source_dir("webgames", "WEBGAMES_SOURCE")


def dist_dir() -> Path:
    return source_root() / "webgames" / "dist"


def is_excluded(task_id: str) -> bool:
    return task_id.startswith(EXCLUDED_PREFIXES)


def load_tasks(path: Path | None = None) -> list[dict]:
    """The task rows of analysis/webgames_tasks.jsonl, minus the excluded ones, each with a ``family`` (its first tag)."""
    path = path or source_root() / "analysis" / "webgames_tasks.jsonl"
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if is_excluded(str(row.get("id") or row.get("path"))):
            continue
        row["id"] = str(row.get("id") or row["path"]).removesuffix("/*")  # wildcard routes: maze/* -> maze
        row["family"] = (row.get("tags") or ["other"])[0]
        rows.append(row)
    return rows


def homepage(base: str, row: dict) -> str:
    """Where the task lives: base + its route (a wildcard route such as ``maze/*`` is entered at ``maze``)."""
    route = str(row["path"]).rstrip("*").rstrip("/")
    return f"{base.rstrip('/')}/{route}"


def make_goal(base: str, row: dict) -> str:
    return TASK_PROMPT.format(description=row["description"], homepage=homepage(base, row))


def select_tasks(rows: list[dict], *, tasks: str | None = None, categories: str | None = None,
                 all_tasks: bool = False) -> list[dict]:
    by_id = {str(row["id"]): row for row in rows}
    if tasks:
        picked = [name.strip() for name in tasks.split(",") if name.strip()]
        for name in picked:
            if is_excluded(name):
                raise SystemExit(f"{name} is excluded from this runner")
            if name not in by_id:
                raise SystemExit(f"unknown WebGames task {name!r}")
        return [by_id[name] for name in picked]
    if categories:
        wanted = {name.strip() for name in categories.split(",") if name.strip()}
        known = {row["family"] for row in rows}
        if wanted - known:
            raise SystemExit(f"unknown categories {sorted(wanted - known)}; known: {sorted(known)}")
        return [row for row in rows if row["family"] in wanted]
    if all_tasks:
        return list(rows)
    return [by_id[name] for name in DEFAULT_SAMPLE if name in by_id]


def build_jobs(rows: list[dict]) -> list[dict]:
    return [{"name": str(row["id"]), "family": row["family"], "row": row} for row in rows]


# ---------------------------------------------------------------- grading

def grade(password: str, answer_texts: list[str], page_text: str) -> dict:
    """Official scorer: the password is a substring of the answer. Also whether the final page showed it."""
    in_answer = bool(password) and any(password in (text or "") for text in answer_texts)
    return {"passed": in_answer, "password_on_page": bool(password) and password in (page_text or "")}


def run_job(job: dict, lane, label: str, *, lr, base: str, timeout_min: float, poll: float, auto_resume: int) -> dict:
    row = job["row"]
    goal = make_goal(base, row)
    info = bc.execute_run(lr, goal, lane, title=f"WebGames {job['name']}", timeout_s=timeout_min * 60,
                          poll=poll, auto_resume=auto_resume, label=label)
    time.sleep(1.5)
    final_url, page_text = bc.observe_text(lr, lane)
    bc.finish_run(lr, info.get("run_id"))
    answers = [info.get("status_message") or ""] + list(info.get("messages") or [])
    verdict = grade(str(row["password"]), answers, page_text)
    info.pop("goal", None)
    return {"task": job["name"], "family": job["family"], "difficulty": row.get("difficulty"), **verdict,
            "final_url": final_url, "goal": goal, **info}


def main(argv: list[str] | None = None) -> int:
    bc.utf8_console()
    parser = argparse.ArgumentParser(description="Run Lumen on WebGames challenges.")
    parser.add_argument("--api", default=DEFAULT_API, help="the Lumen API to drive (never 8001/8011/8041)")
    parser.add_argument("--lanes", type=int, default=3, help="parallel headless bench lanes (0: your own Chrome, serial)")
    parser.add_argument("--tasks", help="comma-separated task ids (default: the sample)")
    parser.add_argument("--categories", help="comma-separated categories (a task's first tag), e.g. timed,scroll")
    parser.add_argument("--all", action="store_true", help="every non-excluded task")
    parser.add_argument("--timeout-min", type=float, default=TIMEOUT_MIN)
    parser.add_argument("--poll", type=float, default=8.0)
    parser.add_argument("--auto-resume", type=int, default=3)
    parser.add_argument("--chromium")
    parser.add_argument("--out", help="output dir (default: storage/artifacts/eval_runs/webgames-<stamp>)")
    parser.add_argument("--list", action="store_true", help="print categories and the default sample, then exit")
    args = parser.parse_args(argv)

    rows = load_tasks()
    if args.list:
        print(f"{len(rows)} tasks (excluded prefixes: {list(EXCLUDED_PREFIXES)})")
        categories: dict[str, list[str]] = {}
        for row in rows:
            categories.setdefault(row["family"], []).append(str(row["id"]))
        for name, ids in sorted(categories.items()):
            print(f"  {name:12} {len(ids):2}  {' '.join(ids)}")
        print("default sample:", " ".join(DEFAULT_SAMPLE))
        return 0
    jobs = build_jobs(select_tasks(rows, tasks=args.tasks, categories=args.categories, all_tasks=args.all))
    api = bc.check_api(args.api, DEFAULT_API)
    lr = bc.load_live_run(api)
    chromium = lr.find_lane_chromium(args.chromium)
    out = Path(args.out) if args.out else bc.default_out("webgames")
    out.mkdir(parents=True, exist_ok=True)
    server, visits = srv.start_server(dist_dir())
    print(f"WebGames: {len(jobs)} tasks, api={api}, app={server.url}, lanes={args.lanes}, out={out}", flush=True)
    factory = bc.LaneFactory(lr, chromium) if args.lanes > 0 else None
    started = time.time()
    try:
        results = bc.run_pool(
            jobs,
            lambda job, lane, label: run_job(job, lane, label, lr=lr, base=server.url, timeout_min=args.timeout_min,
                                             poll=args.poll, auto_resume=args.auto_resume),
            factory=factory, lanes=args.lanes, out=out, label_prefix="lane", say=lr._say)
    finally:
        server.close()
    summary = bc.summarize(results)
    summary["password_shown_but_not_reported"] = sum(
        int(bool(r.get("password_on_page")) and not r.get("passed")) for r in results)
    (out / "summary.txt").write_text(bc.summary_text(summary, "WebGames") + "\n", encoding="utf-8")
    print(bc.summary_text(summary, "WebGames"), flush=True)
    print(f"password on page but not reported: {summary['password_shown_but_not_reported']}", flush=True)
    print(f"done in {(time.time() - started) / 60:.1f} min; scorecard {out / 'scorecard.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
