"""Run Lumen on MiniWoB++ tasks (headless bench lanes) and score them with the benchmark's own reward.

    python -m app.agent_eval.open_bench.miniwob_run --api http://127.0.0.1:8021 --lanes 3 --out DIR
    python -m app.agent_eval.open_bench.miniwob_run --list                 # families and the default sample
    python -m app.agent_eval.open_bench.miniwob_run --api ... --tasks click-button,enter-text --seeds 1,2
    python -m app.agent_eval.open_bench.miniwob_run --api ... --families click,drag --seeds 1

Protocol (BrowserGym's): a task page is seeded (?seed=N), its first episode starts by itself, and the goal given
to Lumen is the instruction text (the utterance). The utterance is read ahead by loading the page once in a
throwaway headless Chromium (same seed, same text); if that fails the goal points at the instruction shown at the
top of the page. Success is ``raw_reward > 0`` (BrowserGym's validate), reported by the page shim when the
episode ends. text-transform is excluded by the owner's rule. MiniWoB has no CAPTCHA tasks.
See README.md and miniwob_server.py. Nothing here assumes a model provider.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from app.agent_eval.open_bench import bench_common as bc
from app.agent_eval.open_bench import miniwob_server as srv

DEFAULT_API = "http://127.0.0.1:8021"
EXCLUDED = {"text-transform"}
# Tasks whose instruction is a CAPTCHA/anti-bot challenge would be listed here; MiniWoB++ has none.
TIMEOUT_MIN = 6.0

# ~30 diverse tasks: one or two from each family, easy to hard.
DEFAULT_SAMPLE = [
    "click-button", "click-link", "click-dialog", "click-checkboxes", "click-collapsible", "click-menu",
    "click-tab-2", "click-widget", "click-shape", "enter-text", "enter-password", "enter-date", "enter-time",
    "choose-list", "choose-date", "drag-box", "drag-items", "drag-sort-numbers", "use-autocomplete",
    "use-slider", "use-spinner", "focus-text", "form-sequence", "login-user", "search-engine",
    "email-inbox", "social-media", "navigate-tree", "read-table", "simple-arithmetic",
]


def html_root() -> Path:
    return bc.source_dir("miniwob", "MINIWOB_SOURCE") / "miniwob" / "html"


def discover_tasks(root: Path) -> list[str]:
    """Every task page of the pinned MiniWoB++ (miniwob/miniwob/html/miniwob/*.html), minus EXCLUDED."""
    folder = Path(root) / "miniwob"
    return sorted(path.stem for path in folder.glob("*.html") if path.stem not in EXCLUDED)


# Task-name prefixes shared by two or more tasks; every one-off task (terminal, tic-tac-toe, ...) is "misc".
FAMILY_PREFIXES = {"click", "choose", "drag", "enter", "use", "email", "find", "focus", "form", "social", "login",
                   "read", "scroll", "simple", "highlight", "copy", "count", "book", "draw", "multi"}


def family_of(task: str) -> str:
    """click-*, choose-*, drag-*, enter-*, use-*, email-*, form-* ... (FAMILY_PREFIXES), else misc."""
    prefix = task.split("-", 1)[0]
    return prefix if prefix in FAMILY_PREFIXES else "misc"


def families(tasks: list[str]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for task in tasks:
        grouped.setdefault(family_of(task), []).append(task)
    return dict(sorted(grouped.items()))


def select_tasks(available: list[str], *, tasks: str | None = None, family_names: str | None = None,
                 all_tasks: bool = False) -> list[str]:
    """--tasks a,b (validated, excluded names rejected) / --families click,drag / --all / the default sample."""
    if tasks:
        picked = [name.strip() for name in tasks.split(",") if name.strip()]
        for name in picked:
            if name in EXCLUDED:
                raise SystemExit(f"{name} is excluded from this runner")
            if name not in available:
                raise SystemExit(f"unknown MiniWoB task {name!r}")
        return picked
    if family_names:
        wanted = [name.strip() for name in family_names.split(",") if name.strip()]
        by_family = families(available)
        unknown = [name for name in wanted if name not in by_family]
        if unknown:
            raise SystemExit(f"unknown families {unknown}; known: {sorted(by_family)}")
        return [task for name in wanted for task in by_family[name]]
    if all_tasks:
        return list(available)
    return [task for task in DEFAULT_SAMPLE if task in available]


def build_jobs(task_names: list[str], seeds: list[int]) -> list[dict]:
    return [{"name": f"{task}@{seed}" if len(seeds) > 1 or seed != 1 else task, "task": task, "seed": seed,
             "family": family_of(task)} for task in task_names for seed in seeds]


# ---------------------------------------------------------------- goal

GOAL = "Open {url} and do the instruction shown at the top of the page: {utterance}"
GOAL_NO_TEXT = "Open {url} and do the instruction shown at the top of the page."


def make_goal(url: str, utterance: str | None) -> str:
    utterance = " ".join((utterance or "").split())
    return GOAL.format(url=url, utterance=utterance) if utterance else GOAL_NO_TEXT.format(url=url)


def read_utterance(chromium: Path, url: str, store: srv.EpisodeStore, sid: str, *, timeout: float = 45.0) -> str | None:
    """Load the seeded page once in a throwaway headless Chromium; the shim posts the instruction text."""
    with tempfile.TemporaryDirectory(prefix="miniwob-pre-") as profile:
        try:
            subprocess.run(
                [str(chromium), "--headless=new", "--disable-gpu", "--no-first-run", f"--user-data-dir={profile}",
                 "--virtual-time-budget=4000", "--dump-dom", url],
                capture_output=True, timeout=timeout, check=False)
        except (subprocess.TimeoutExpired, OSError):
            pass
    page = store.get(sid)
    return (page or {}).get("utterance") or None


# ---------------------------------------------------------------- grading

def grade(page: dict | None) -> dict:
    """Success = the episode ended with raw_reward > 0 (BrowserGym's MiniWoB validate)."""
    if not page or "raw_reward" not in page:
        return {"passed": False, "raw_reward": None, "reason": "episode never ended"}
    try:
        raw = float(page["raw_reward"])
    except (TypeError, ValueError):
        return {"passed": False, "raw_reward": None, "reason": "unreadable reward"}
    return {"passed": raw > 0, "raw_reward": raw, "reason": page.get("reason")}


# ---------------------------------------------------------------- run

def run_job(job: dict, lane, label: str, *, lr, base: str, store: srv.EpisodeStore, chromium: Path,
            timeout_min: float, poll: float, auto_resume: int, prerender: bool = True) -> dict:
    sid = uuid.uuid4().hex[:12]
    url = srv.task_url(base, job["task"], job["seed"], sid)
    utterance = None
    if prerender:
        utterance = read_utterance(chromium, srv.task_url(base, job["task"], job["seed"], "pre-" + sid), store, "pre-" + sid)
    goal = make_goal(url, utterance)
    info = bc.execute_run(lr, goal, lane, title=f"MiniWoB {job['name']}", timeout_s=timeout_min * 60,
                          poll=poll, auto_resume=auto_resume, label=label)
    time.sleep(1.5)  # the shim's report is a fire-and-forget POST
    page = store.get(sid)
    verdict = grade(page)
    bc.finish_run(lr, info.get("run_id"))
    result = {"task": job["name"], "miniwob_task": job["task"], "seed": job["seed"], "family": job["family"],
              "sid": sid, "url": url, "utterance_prerendered": utterance,
              "utterance_live": (page or {}).get("utterance"), **verdict, **{
                  k: v for k, v in info.items() if k not in ("goal",)}, "goal": goal}
    if utterance and result["utterance_live"] and utterance != result["utterance_live"]:
        result["utterance_mismatch"] = True
    return result


def main(argv: list[str] | None = None) -> int:
    bc.utf8_console()
    parser = argparse.ArgumentParser(description="Run Lumen on MiniWoB++ tasks.")
    parser.add_argument("--api", default=DEFAULT_API, help="the Lumen API to drive (never 8001/8011/8041)")
    parser.add_argument("--lanes", type=int, default=3, help="parallel headless bench lanes (0: your own Chrome, serial)")
    parser.add_argument("--tasks", help="comma-separated task names (default: the sample)")
    parser.add_argument("--families", help="comma-separated families, e.g. click,enter,drag")
    parser.add_argument("--all", action="store_true", help="every non-excluded task")
    parser.add_argument("--seeds", default="1", help="comma-separated seeds (default 1)")
    parser.add_argument("--timeout-min", type=float, default=TIMEOUT_MIN)
    parser.add_argument("--poll", type=float, default=8.0)
    parser.add_argument("--auto-resume", type=int, default=3)
    parser.add_argument("--no-prerender", action="store_true", help="do not read the instruction ahead; the goal points at the page")
    parser.add_argument("--chromium")
    parser.add_argument("--out", help="output dir (default: storage/artifacts/eval_runs/miniwob-<stamp>)")
    parser.add_argument("--list", action="store_true", help="print families and the default sample, then exit")
    args = parser.parse_args(argv)

    root = html_root()
    available = discover_tasks(root)
    if args.list:
        print(f"{len(available)} tasks (excluded: {sorted(EXCLUDED)})")
        for name, members in families(available).items():
            print(f"  {name:12} {len(members):2}  {' '.join(members)}")
        print("default sample:", " ".join(t for t in DEFAULT_SAMPLE if t in available))
        return 0
    seeds = [int(item) for item in args.seeds.split(",") if item.strip()]
    jobs = build_jobs(select_tasks(available, tasks=args.tasks, family_names=args.families, all_tasks=args.all), seeds)
    api = bc.check_api(args.api, DEFAULT_API)
    lr = bc.load_live_run(api)
    chromium = lr.find_lane_chromium(args.chromium)
    out = Path(args.out) if args.out else bc.default_out("miniwob")
    out.mkdir(parents=True, exist_ok=True)
    server, store = srv.start_server(root)
    print(f"MiniWoB: {len(jobs)} runs, api={api}, pages={server.url}{srv.MOUNT}, lanes={args.lanes}, out={out}", flush=True)
    factory = bc.LaneFactory(lr, chromium) if args.lanes > 0 else None
    started = time.time()
    try:
        results = bc.run_pool(
            jobs,
            lambda job, lane, label: run_job(job, lane, label, lr=lr, base=server.url, store=store, chromium=chromium,
                                             timeout_min=args.timeout_min, poll=args.poll,
                                             auto_resume=args.auto_resume, prerender=not args.no_prerender),
            factory=factory, lanes=args.lanes, out=out, label_prefix="lane", say=lr._say)
    finally:
        server.close()
    summary = bc.summarize(results)
    (out / "summary.txt").write_text(bc.summary_text(summary, "MiniWoB++") + "\n", encoding="utf-8")
    print(bc.summary_text(summary, "MiniWoB++"), flush=True)
    print(f"done in {(time.time() - started) / 60:.1f} min; scorecard {out / 'scorecard.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
