"""Run a rule-ablation sweep: a baseline arm plus one arm per group switched off, on the same pages.

Each arm starts its OWN API (uvicorn, a non-8001 port, default 8021) on a fresh copy of the source database,
with its own data root, artifact root and bench-lane/workspace folders, and the eval-only switch
(app.eval_ablation) in that process's environment. The runner checks GET /eval/ablation on the started API
before any page runs (the baseline must report inactive, an off arm exactly its groups), then runs
interaction_bench.live_run against it on headless bench lanes, then stops the API. Arms run one after another,
never two at once (lanes use ports and the bench harness its own windows).

    python -m app.agent_eval.ablation.run --groups payment_floor_judge --pages v6_crm --repeat 1 --lanes 1 \\
        --source-db ../../storage/lumen.db
    python -m app.agent_eval.ablation.run --groups all --generation 6 --repeat 3 --lanes 10
    python -m app.agent_eval.ablation.run --groups brain_hedge --reasoning-efforts medium --screenshot-widths 1568 ...
    python -m app.agent_eval.ablation.run --report-only <out dir>
    python -m app.agent_eval.ablation.run --groups all --generation 6 --repeat 3 --dry-run   # the plan + estimate

Writes <out>/report.json and <out>/report.md (see report.py for the verdict rule) and keeps each arm's
scorecard, API log and database copy under <out>/arms/<arm>/, and the source snapshot every arm starts from
(--prune-dbs deletes the copies once read). Rerunning with the same --out resumes: an arm already finished there
(no error, graded runs) is reused, and the kept snapshot is the starting point of the arms still to run.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.agent_eval.ablation import report as report_mod

HERE = Path(__file__).resolve().parent
API_DIR = HERE.parents[2]  # apps/api
REPO = HERE.parents[4]
PRODUCTION_PORT = 8001
DEFAULT_PORT = 8021
MAC_PLAYWRIGHT = Path.home() / "Library" / "Caches" / "ms-playwright"
# The runner's own copy of these is authoritative for the child processes it starts.
_ABLATION_ENV = (
    "LUMEN_EVAL_ABLATION",
    "LUMEN_EVAL_MARKER",
    "LUMEN_EVAL_SCREENSHOT_WIDTH",
    "LUMEN_EVAL_BRAIN_REASONING_EFFORT",
)
# Dev seams that would change what an arm measures; never inherited by an arm's API.
_STRIPPED_ENV = (
    "LUMEN_EXTERNAL_BRAIN_DIR",
    "LUMEN_EXTERNAL_JUDGE_DIR",
    "LUMEN_EXTERNAL_ESCALATION_DIR",
    "LUMEN_BRAIN_MESSAGE_HISTORY",
    "LUMEN_BRAIN_INVENTORY_DELTA",
    "LUMEN_API",
)

# Generation-6 history on this machine (operator_runs, 2026-10): ~$0.12 and ~9 min per page run on average (v6_crm
# $0.10 / 7 min; v6_mail and v6_helpdesk up to $0.31 / 17 min).
EST_COST_PER_RUN_USD = 0.12
EST_MINUTES_PER_RUN = 9.0
EST_API_START_MIN = 0.5


def _say(message: str) -> None:
    print(message, flush=True)


# --- arms ---------------------------------------------------------------------------------------------------------


def plan_arms(groups: list[str], widths: list[int], efforts: list[str]) -> list[dict[str, Any]]:
    """The baseline first, then one arm per group off, per screenshot width and per brain reasoning effort."""
    arms: list[dict[str, Any]] = [{"name": "baseline", "groups": [], "knobs": {}}]
    arms += [{"name": f"off:{group}", "groups": [group], "knobs": {}} for group in groups]
    arms += [{"name": f"screenshot_width={width}", "groups": [], "knobs": {"screenshot_width": int(width)}} for width in widths]
    arms += [{"name": f"brain_reasoning_effort={effort}", "groups": [], "knobs": {"brain_reasoning_effort": effort}} for effort in efforts]
    return arms


def parse_groups(text: str | None) -> list[str]:
    from app import eval_ablation

    names = [name.strip() for name in (text or "").split(",") if name.strip()]
    expanded: list[str] = []
    for name in names:
        if name == "all":
            expanded += list(eval_ablation.GROUPS)
        elif name == "checks":
            expanded += [g.name for g in eval_ablation.CHECK_GROUPS]
        elif name == "prompts":
            expanded += [g.name for g in eval_ablation.PROMPT_GROUPS]
        elif name in eval_ablation.GROUPS:
            expanded.append(name)
        else:
            raise SystemExit(f"--groups: unknown group {name!r} (choose from: all, checks, prompts, {', '.join(eval_ablation.GROUPS)})")
    return list(dict.fromkeys(expanded))


def arm_env(arm: dict[str, Any]) -> dict[str, str]:
    """The eval switch variables for an arm's API (none for the baseline beyond the marker)."""
    from app import eval_ablation

    env = {eval_ablation.ENV_MARKER: eval_ablation.EVAL_MARKER_VALUE}
    if arm.get("groups"):
        env[eval_ablation.ENV_GROUPS] = ",".join(arm["groups"])
    knobs = arm.get("knobs") or {}
    if knobs.get("screenshot_width"):
        env[eval_ablation.ENV_SCREENSHOT_WIDTH] = str(int(knobs["screenshot_width"]))
    if knobs.get("brain_reasoning_effort"):
        env[eval_ablation.ENV_BRAIN_REASONING_EFFORT] = str(knobs["brain_reasoning_effort"])
    return env


def expected_status_ok(arm: dict[str, Any], status: dict[str, Any]) -> tuple[bool, str]:
    """Whether the started API runs exactly this arm's ablation (the baseline: none at all)."""
    knobs = arm.get("knobs") or {}
    wants_active = bool(arm.get("groups") or knobs)
    if not wants_active:
        return (not status.get("active"), "baseline API reports an active ablation" if status.get("active") else "")
    if not status.get("active"):
        return False, f"ablation not active on the arm's API: {status.get('reason')}"
    if sorted(status.get("groups") or []) != sorted(arm.get("groups") or []):
        return False, f"API runs groups {status.get('groups')} instead of {arm.get('groups')}"
    if (status.get("screenshot_width") or None) != (knobs.get("screenshot_width") or None):
        return False, f"API screenshot width {status.get('screenshot_width')} != {knobs.get('screenshot_width')}"
    if (status.get("brain_reasoning_effort") or None) != (knobs.get("brain_reasoning_effort") or None):
        return False, f"API reasoning effort {status.get('brain_reasoning_effort')} != {knobs.get('brain_reasoning_effort')}"
    return True, ""


# --- environment, database, API -------------------------------------------------------------------------------------


def read_env_file(path: Path | None) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path or not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def snapshot_database(source: Path, dest: Path) -> None:
    """A consistent copy of a live SQLite database (WAL included), opened read-only: the source is never written."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        out = sqlite3.connect(str(dest))
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()


def _port_answers(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2):
            return True
    except urllib.error.HTTPError:
        return True
    except Exception:  # noqa: BLE001 - nothing listening
        return False


def _get_json(url: str, timeout: float = 10.0) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


class ArmApi:
    """One uvicorn API process for one arm, on its own port, data root and database copy."""

    def __init__(self, *, port: int, data_root: Path, env: dict[str, str], log_path: Path, python: str) -> None:
        self.port = port
        self.data_root = data_root
        self.env = env
        self.log_path = log_path
        self.python = python
        self.proc: subprocess.Popen | None = None
        self._log = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self, timeout_s: float = 120.0) -> dict[str, Any]:
        if _port_answers(self.port):
            raise RuntimeError(f"port {self.port} already answers: refusing to run an arm against a server this runner did not start")
        self._log = open(self.log_path, "a", encoding="utf-8")
        self.proc = subprocess.Popen(
            [self.python, "-m", "uvicorn", "app.main:app", "--app-dir", str(API_DIR), "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=str(REPO),
            env=self.env,
            stdout=self._log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"the arm API exited with {self.proc.returncode}; see {self.log_path}")
            try:
                return _get_json(f"{self.url}/eval/ablation")
            except Exception:  # noqa: BLE001 - not up yet
                time.sleep(1.0)
        self.stop()
        raise RuntimeError(f"the arm API did not answer within {timeout_s:.0f} s; see {self.log_path}")

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except Exception:  # noqa: BLE001
                self.proc.terminate()
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except Exception:  # noqa: BLE001
                    self.proc.kill()
        if self._log is not None:
            self._log.close()
            self._log = None


def build_api_env(*, base_env: dict[str, str], env_file: dict[str, str], arm: dict[str, Any], data_root: Path) -> dict[str, str]:
    data_root = Path(data_root).resolve()  # the arm API's working directory differs from the runner's
    env = {key: value for key, value in base_env.items() if key not in _ABLATION_ENV and key not in _STRIPPED_ENV}
    for key, value in env_file.items():
        if key not in _ABLATION_ENV and key not in _STRIPPED_ENV:
            env.setdefault(key, value)
    env.update(
        {
            "DATABASE_URL": f"sqlite:///{data_root / 'lumen.db'}",
            "LUMEN_DATA_ROOT": str(data_root),
            "LUMEN_ARTIFACT_ROOT": str(data_root / "artifacts"),
            # The phone listener (default 8002) and phone alerts belong to the production API.
            "LUMEN_REMOTE_PORT": "0",
            "LUMEN_FIREBASE_CREDENTIALS": str(data_root / "no-firebase.json"),
            "PYTHONUNBUFFERED": "1",
        }
    )
    env.update(arm_env(arm))
    return env


def find_chromium(explicit: str | None) -> str | None:
    for candidate in (explicit, os.environ.get("LUMEN_BENCH_CHROMIUM")):
        if candidate:
            return candidate
    builds = sorted(
        MAC_PLAYWRIGHT.glob("chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium"),
        key=lambda path: int(path.parts[-6].removeprefix("chromium-")) if path.parts[-6].removeprefix("chromium-").isdigit() else 0,
    )
    return str(builds[-1]) if builds else None


# --- one arm --------------------------------------------------------------------------------------------------------


def live_run_args(args: argparse.Namespace, out: Path, chromium: str | None) -> list[str]:
    cmd = [sys.executable, "-m", "app.agent_eval.interaction_bench.live_run", "--out", str(out), "--repeat", str(args.repeat)]
    if args.focus:
        cmd += ["--focus", args.focus]
    else:
        if args.generation:
            cmd += ["--generation", args.generation]
        if args.pages:
            cmd += ["--pages", args.pages]
    if args.lanes:
        cmd += ["--lanes", str(args.lanes)]
    if chromium:
        cmd += ["--chromium", chromium]
    if args.timeout_min is not None:
        cmd += ["--timeout-min", str(args.timeout_min)]
    if args.perturb:
        cmd += ["--perturb", args.perturb, "--seed", str(args.seed)]
    return cmd


def run_arm(arm: dict[str, Any], args: argparse.Namespace, *, out: Path, snapshot: Path, env_file: dict[str, str], chromium: str | None) -> dict[str, Any]:
    arm_dir = out / "arms" / arm["name"].replace(":", "_").replace("=", "_")
    data_root = arm_dir / "data"
    data_root.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(snapshot, data_root / "lumen.db")
    memory = Path(args.source_db).expanduser().resolve().parent / "memory"
    if memory.is_dir() and not (data_root / "memory").exists():
        shutil.copytree(memory, data_root / "memory")
    env = build_api_env(base_env=dict(os.environ), env_file=env_file, arm=arm, data_root=data_root)
    api = ArmApi(port=args.port, data_root=data_root, env=env, log_path=arm_dir / "api.log", python=args.python or sys.executable)
    record: dict[str, Any] = {**arm, "dir": str(arm_dir), "port": args.port, "started_at": datetime.now(timezone.utc).isoformat()}
    _say(f"== arm {arm['name']}: starting API on :{args.port} ({data_root})")
    started = time.time()
    try:
        status = api.start()
        record["api_status"] = status
        ok, why = expected_status_ok(arm, status)
        if not ok:
            raise RuntimeError(f"arm {arm['name']}: {why}")
        _say(f"   API ablation status: {status.get('reason')} groups={status.get('groups')} width={status.get('screenshot_width')} effort={status.get('brain_reasoning_effort')}")
        gauntlet = arm_dir / "gauntlet"
        live_env = {
            **{key: value for key, value in os.environ.items() if key not in _ABLATION_ENV},
            "LUMEN_API": api.url,
            "LUMEN_BENCH_LANE_ROOT": str(arm_dir / "lanes"),
            "LUMEN_BENCH_WORKSPACE_ROOT": str(arm_dir / "workspace"),
            "PYTHONUNBUFFERED": "1",
        }
        cmd = live_run_args(args, gauntlet, chromium)
        record["live_run_cmd"] = cmd
        with open(arm_dir / "live_run.log", "w", encoding="utf-8") as log:
            proc = subprocess.Popen(cmd, cwd=str(API_DIR), env=live_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            assert proc.stdout is not None
            for line in proc.stdout:
                log.write(line)
                print(f"   [{arm['name']}] {line}", end="", flush=True)
            record["live_run_exit"] = proc.wait()
    except Exception as exc:  # noqa: BLE001 - one arm's failure is recorded; the sweep continues
        record["error"] = str(exc)
        _say(f"   arm {arm['name']} failed: {exc}")
    finally:
        api.stop()
    record["wall_s"] = round(time.time() - started, 1)
    record.update(collect_arm(arm_dir))
    if args.prune_dbs:
        (data_root / "lumen.db").unlink(missing_ok=True)
    (arm_dir / "arm.json").write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    return record


def finished_arm(out: Path, arm: dict[str, Any]) -> dict[str, Any] | None:
    """The record of an arm already run to completion in this output dir (no error, at least one graded run)."""
    path = out / "arms" / arm["name"].replace(":", "_").replace("=", "_") / "arm.json"
    if not path.is_file():
        return None
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("error") or not record.get("records") or record.get("groups") != arm.get("groups") or (record.get("knobs") or {}) != (arm.get("knobs") or {}):
        return None
    return record


def collect_arm(arm_dir: Path) -> dict[str, Any]:
    """The arm's per-run records: its scorecard joined with its database copy."""
    scorecard_path = arm_dir / "gauntlet" / "scorecard.json"
    db_path = arm_dir / "data" / "lumen.db"
    if not scorecard_path.is_file():
        return {"records": [], "collect_error": "no scorecard"}
    scorecard = json.loads(scorecard_path.read_text(encoding="utf-8"))
    run_ids = [page.get("run_id") for page in (scorecard.get("by_page") or {}).values() if page.get("run_id") is not None]
    metrics = report_mod.read_run_metrics(db_path, run_ids) if db_path.is_file() else {}
    return {
        "records": report_mod.run_records(scorecard, metrics),
        "scorecard_provenance": scorecard.get("provenance"),
        "scorecard_overall": scorecard.get("overall"),
    }


# --- estimate -------------------------------------------------------------------------------------------------------


def estimate(arms: int, runs_per_arm: int, lanes: int, *, cost_per_run: float = EST_COST_PER_RUN_USD, minutes_per_run: float = EST_MINUTES_PER_RUN) -> dict[str, Any]:
    """Cost and wall time of a sweep: lanes share an arm's runs as a queue, arms run one after another."""
    lanes = max(1, min(lanes, runs_per_arm or 1))
    # The queue's work spread over the lanes, plus the slowest run's tail (about twice the mean) and the API start.
    minutes_per_arm = runs_per_arm * minutes_per_run / lanes + (2 * minutes_per_run if runs_per_arm > lanes else minutes_per_run) + EST_API_START_MIN
    return {
        "arms": arms,
        "runs_per_arm": runs_per_arm,
        "runs_total": arms * runs_per_arm,
        "cost_usd": round(arms * runs_per_arm * cost_per_run, 2),
        "wall_hours": round(arms * minutes_per_arm / 60.0, 1),
        "assumptions": f"${cost_per_run:.2f} and {minutes_per_run:.0f} min per page run (generation-6 history), "
        f"{lanes} lanes as a queue, arms one after another",
    }


def _pages_count(args: argparse.Namespace) -> int:
    manifest = json.loads((API_DIR / "app" / "agent_eval" / "interaction_bench" / "manifest.json").read_text(encoding="utf-8"))
    if args.focus:
        return len([s for s in args.focus.split(",") if s.strip()])
    pages = [p for p in manifest["pages"] if not args.generation or args.generation == "all" or p.get("generation") == int(args.generation)]
    if args.pages:
        wanted = {n.strip() for n in args.pages.split(",")}
        pages = [p for p in pages if p["name"] in wanted]
    desktop = [p["name"] for p in pages if p.get("desktop")]
    if desktop and not args.allow_desktop:
        raise SystemExit(
            f"desktop pages {desktop} need the user's own Chrome and screen, which an arm's API cannot use: drop them "
            "or pass --allow-desktop"
        )
    return len(pages)


# --- main -------------------------------------------------------------------------------------------------------------


def _commit() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO, capture_output=True, text=True, timeout=20).stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


def _registry() -> dict[str, Any]:
    from app import eval_ablation

    registry = eval_ablation.registry()
    try:
        registry["prompt_token_sizes"] = eval_ablation.prompt_group_token_sizes()
        registry["schema_section_tokens"] = eval_ablation.schema_section_token_sizes()
    except Exception as exc:  # noqa: BLE001 - sizes are informational
        registry["prompt_token_sizes_error"] = str(exc)
    return registry


def write_report(out: Path, records: list[dict[str, Any]], meta: dict[str, Any]) -> dict[str, Any]:
    report = report_mod.build_report(records, meta=meta, registry=_registry())
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (out / "report.md").write_text(report_mod.render_markdown(report), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rule-ablation sweep over the Interaction Gauntlet (eval only).")
    parser.add_argument("--groups", help="comma-separated groups to switch off, one arm each (or all, checks, prompts)")
    parser.add_argument("--screenshot-widths", default="", help="extra arms with this brain screenshot width (e.g. 1568)")
    parser.add_argument("--reasoning-efforts", default="", help="extra arms with this brain reasoning effort (e.g. medium)")
    parser.add_argument("--generation", default=None, help="passed to live_run (1-6 or all)")
    parser.add_argument("--pages", help="passed to live_run")
    parser.add_argument("--focus", help="passed to live_run (page:challenge,...)")
    parser.add_argument("--perturb", help="passed to live_run")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--lanes", type=int, default=1, help="headless bench lanes per arm (default 1)")
    parser.add_argument("--timeout-min", type=float, default=None)
    parser.add_argument("--chromium", help="lane Chromium (default: LUMEN_BENCH_CHROMIUM or the newest macOS Playwright Chromium)")
    parser.add_argument("--api", default=f"http://127.0.0.1:{DEFAULT_PORT}", help="where each arm's API listens (never :8001)")
    parser.add_argument("--source-db", default=str(REPO / "storage" / "lumen.db"), help="database each arm copies (read only)")
    parser.add_argument("--env-file", default=str(REPO / ".env"), help=".env whose values (encryption key, model keys) each arm's API gets")
    parser.add_argument("--python", help="interpreter for the arm APIs (default: this one)")
    parser.add_argument("--out", help="output dir (default storage/artifacts/eval_runs/ablation-<stamp>)")
    parser.add_argument("--prune-dbs", action="store_true", help="delete each arm's database copy once its records are read")
    parser.add_argument("--allow-desktop", action="store_true", help="allow desktop pages (they need a Chrome connected to the arm API)")
    parser.add_argument("--dry-run", action="store_true", help="print the arms and the estimate; run nothing")
    parser.add_argument("--report-only", help="rebuild report.json/report.md from an existing output dir")
    args = parser.parse_args(argv)

    if args.report_only:
        out = Path(args.report_only)
        records = []
        for arm_json in sorted((out / "arms").glob("*/arm.json")):
            record = json.loads(arm_json.read_text(encoding="utf-8"))
            record.update(collect_arm(arm_json.parent))
            records.append(record)
        records.sort(key=lambda r: (r["name"] != "baseline", r.get("started_at") or ""))
        meta = json.loads((out / "meta.json").read_text(encoding="utf-8")) if (out / "meta.json").is_file() else {}
        write_report(out, records, meta)
        _say(f"report: {out / 'report.md'}")
        return 0

    port = urllib.parse.urlparse(args.api).port or DEFAULT_PORT
    if port == PRODUCTION_PORT:
        raise SystemExit("--api: an arm never runs on the production port 8001")
    args.port = port
    groups = parse_groups(args.groups)
    widths = [int(w) for w in args.screenshot_widths.split(",") if w.strip()]
    efforts = [e.strip().lower() for e in args.reasoning_efforts.split(",") if e.strip()]
    if not (groups or widths or efforts):
        raise SystemExit("name at least one --groups, --screenshot-widths or --reasoning-efforts arm")
    arms = plan_arms(groups, widths, efforts)
    pages = _pages_count(args)
    runs_per_arm = pages * max(1, args.repeat)
    est = estimate(len(arms), runs_per_arm, max(1, args.lanes))
    _say(f"{len(arms)} arms x {runs_per_arm} runs: estimated ${est['cost_usd']}, {est['wall_hours']} h ({est['assumptions']})")
    for arm in arms:
        _say(f"  - {arm['name']}: {json.dumps(arm_env(arm))}")
    if args.dry_run:
        return 0
    source = Path(args.source_db).expanduser().resolve()
    if not source.is_file():
        raise SystemExit(f"--source-db: {source} not found")
    chromium = find_chromium(args.chromium) if args.lanes else None
    if args.lanes and not chromium:
        raise SystemExit("no lane Chromium: pass --chromium")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    # Absolute: each arm's API runs in another working directory, so a relative --out would point its
    # DATABASE_URL and data root at a fresh, empty database outside the arm's folder.
    out = Path(args.out).expanduser().resolve() if args.out else REPO / "storage" / "artifacts" / "eval_runs" / f"ablation-{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    snapshot = out / "source-snapshot.db"
    if snapshot.is_file():
        _say(f"reusing the snapshot {snapshot} (every arm of this output dir starts from it)")
    else:
        _say(f"snapshotting {source} (read only) -> {snapshot}")
        snapshot_database(source, snapshot)
    meta = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "commit": _commit(),
        "pages_args": {k: getattr(args, k) for k in ("generation", "pages", "focus", "perturb")},
        "repeat": args.repeat,
        "lanes": args.lanes,
        "port": port,
        "source_db": str(source),
        "chromium": chromium,
        "arms": [arm["name"] for arm in arms],
        "estimate": est,
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    env_file = read_env_file(Path(args.env_file)) if args.env_file else {}
    records = []
    for arm in arms:
        done = finished_arm(out, arm)
        if done is not None:
            # An arm an earlier invocation finished in this output dir (same snapshot) is not run again.
            _say(f"== arm {arm['name']}: already finished in {out}, reusing it")
            records.append(done)
        else:
            records.append(run_arm(arm, args, out=out, snapshot=snapshot, env_file=env_file, chromium=chromium))
        write_report(out, records, meta)
    if args.prune_dbs:
        snapshot.unlink(missing_ok=True)
    meta["finished_at"] = datetime.now(timezone.utc).isoformat()
    (out / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    write_report(out, records, meta)
    _say(f"report: {out / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
