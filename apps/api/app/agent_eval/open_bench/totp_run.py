"""Run Lumen on the single-field TOTP fixture (headless bench lanes); success = the page's password in the answer.

    python -m app.agent_eval.open_bench.totp_run --api http://127.0.0.1:8021 --arm on --repeat 10 --out DIR
    python -m app.agent_eval.open_bench.totp_run --api ... --arm off --variants goal,page
    python -m app.agent_eval.open_bench.totp_run --list

Three variants differ only in where the authenticator key is (totp_server.py): the credential vault, the task
text, or the page. ``--arm on|off`` sets the user's ``totp_capability`` agent setting before the run (the A/B
switch; it is restored afterwards). A fresh server, seed and password per task; a vault item is stored for the
task's origin (variant ``vault``) and deleted after. Grading, like webgames_run: the password occurs in the answer
(final status message or any assistant message of the chat thread); the runner also records whether the page
showed it. Nothing here assumes a model provider. Not part of the default suite.
"""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.parse
from pathlib import Path

from app import totp
from app.agent_eval.open_bench import bench_common as bc
from app.agent_eval.open_bench import totp_server as srv

DEFAULT_API = "http://127.0.0.1:8021"
TIMEOUT_MIN = 8.0
# The owner's stacks: this runner PATCHes agent settings and stores a vault item, so it never touches them.
OWNER_PORTS = {8001, 8002}

GOALS = {
    "vault": (
        "Finish signing in at {url}. The site asks for a verification code from your authenticator app: the "
        "authenticator key for this site is stored in your saved credentials. Once verified the page shows a "
        "password; report that password."
    ),
    "goal": (
        "Finish signing in at {url}. The site asks for a verification code from your authenticator app. The "
        "authenticator key for {url} is {seed}. Once verified the page shows a password; report that password."
    ),
    "page": (
        "Finish signing in at {url}. The site asks for a verification code from an authenticator app: use the "
        "secret key the page itself shows to get the current 6-digit code and enter it. Once verified the page "
        "shows a password; report that password."
    ),
}


def make_goal(variant: str, url: str, seed: str) -> str:
    return GOALS[variant].format(url=url, seed=" ".join(seed[i : i + 4] for i in range(0, len(seed), 4)))


def page_url(base: str, variant: str) -> str:
    return f"{base.rstrip('/')}/totp/{variant}"


def build_jobs(variants: list[str], repeat: int) -> list[dict]:
    for variant in variants:
        if variant not in srv.VARIANTS:
            raise SystemExit(f"unknown variant {variant!r}; known: {list(srv.VARIANTS)}")
    return [
        {"name": f"totp-{variant}" + (f"-{n}" if repeat > 1 else ""), "family": f"totp-{variant}", "variant": variant}
        for variant in variants
        for n in range(1, repeat + 1)
    ]


def grade(password: str, answer_texts: list[str], page_text: str) -> dict:
    in_answer = bool(password) and any(password in (text or "") for text in answer_texts)
    return {"passed": in_answer, "password_on_page": bool(password) and password in (page_text or "")}


def _normal_db(value: str) -> str:
    text = str(value or "").strip()
    if text.startswith("sqlite:///"):
        text = text[len("sqlite:///"):]
    return re.sub(r"/+$", "", text.replace("\\", "/")).lower()


def launch_configs(path: Path | None = None) -> list[dict]:
    path = path or bc.REPO / ".claude" / "launch.json"
    try:
        return list(json.loads(path.read_text(encoding="utf-8")).get("configurations") or [])
    except (OSError, ValueError):
        return []


def bench_stack_problem(api: str, bench_db: str | None, configs: list[dict]) -> str | None:
    """Why this API may not be driven by the runner (None when it is a bench stack).

    The API exposes neither its database nor its data root, so a bench stack is proven from the other side: not
    an owner port, and the operator names the stack's database with --bench-db, which must be the DATABASE_URL
    that stack's launch configuration (.claude/launch.json, matched by port) starts it with, and not the default
    application database."""
    port = urllib.parse.urlparse(api).port
    if port is None or port in OWNER_PORTS or port in bc.FORBIDDEN_PORTS:
        return f"port {port} is an owner/other-session stack"
    if not bench_db:
        return "--bench-db is required: the DATABASE_URL of this bench stack, as in its launch configuration"
    wanted = _normal_db(bench_db)
    if not wanted or wanted.endswith("/lumen.db") or wanted == "lumen.db":
        return "--bench-db names the default application database"
    matching = [c for c in configs if int(c.get("port") or 0) == port]
    if not matching:
        return f"no launch configuration for port {port} in .claude/launch.json to check --bench-db against"
    for config in matching:
        declared = _normal_db(str((config.get("env") or {}).get("DATABASE_URL") or ""))
        if declared and declared == wanted:
            return None
    return f"--bench-db does not match the DATABASE_URL of the launch configuration for port {port}"


def set_arm(lr, on: bool) -> bool:
    """Set totp_capability; returns the previous value so the caller can restore it."""
    previous = bool((lr._request("GET", "/agent-settings").get("settings") or {}).get("totp_capability"))
    lr._request("PATCH", "/agent-settings", {"totp_capability": bool(on)})
    return previous


def run_job(job: dict, lane, label: str, *, lr, timeout_min: float, poll: float, auto_resume: int) -> dict:
    variant = job["variant"]
    server, state = srv.start_server()
    item_id = None
    try:
        entry = state[variant]
        url = page_url(server.url, variant)
        if variant == "vault":
            item = lr._request(
                "POST",
                "/credential-vault",
                {"origin": server.url, "account_label": f"bench-{job['name']}", "fields": {"totp": entry["seed"]}, "auto_use": True},
            )
            item_id = item.get("id")
        goal = make_goal(variant, url, entry["seed"])
        info = bc.execute_run(lr, goal, lane, title=f"TOTP {job['name']}", timeout_s=timeout_min * 60,
                              poll=poll, auto_resume=auto_resume, label=label)
        time.sleep(1.0)
        final_url, page_text = bc.observe_text(lr, lane)
        bc.finish_run(lr, info.get("run_id"))
        answers = [info.get("status_message") or ""] + list(info.get("messages") or [])
        verdict = grade(entry["password"], answers, page_text)
        info.pop("goal", None)
        return {"task": job["name"], "family": job["family"], **verdict, "server_verified": bool(entry.get("passed")),
                "wrong_codes": int(entry.get("wrong_attempts") or 0), "final_url": final_url, **info}
    finally:
        if item_id:
            try:
                lr._request("DELETE", f"/credential-vault/{item_id}")
            except Exception:  # noqa: BLE001 - a leftover bench item is harmless but reported by the vault listing
                pass
        server.close()


def main(argv: list[str] | None = None) -> int:
    bc.utf8_console()
    parser = argparse.ArgumentParser(description="Run Lumen on the single-field TOTP fixture.")
    parser.add_argument("--api", default=DEFAULT_API, help="the Lumen API to drive (never 8001/8011/8041)")
    parser.add_argument("--bench-db", help="the bench stack's DATABASE_URL (must match its launch configuration); required")
    parser.add_argument("--arm", choices=("on", "off"), required=False, help="set totp_capability for the run (the A/B switch)")
    parser.add_argument("--variants", default=",".join(srv.VARIANTS))
    parser.add_argument("--repeat", type=int, default=1, help="runs per variant (the A/B plan uses 10)")
    parser.add_argument("--lanes", type=int, default=3)
    parser.add_argument("--timeout-min", type=float, default=TIMEOUT_MIN)
    parser.add_argument("--poll", type=float, default=6.0)
    parser.add_argument("--auto-resume", type=int, default=3)
    parser.add_argument("--chromium")
    parser.add_argument("--out")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)
    if args.list:
        for variant in srv.VARIANTS:
            print(f"{variant:6} {GOALS[variant]}")
        return 0
    jobs = build_jobs([v.strip() for v in args.variants.split(",") if v.strip()], max(1, args.repeat))
    api = bc.check_api(args.api, DEFAULT_API)
    problem = bench_stack_problem(api, args.bench_db, launch_configs())
    if problem:
        raise SystemExit(f"refusing to run against {api}: {problem}")
    lr = bc.load_live_run(api)
    chromium = lr.find_lane_chromium(args.chromium)
    out = Path(args.out) if args.out else bc.default_out(f"totp-{args.arm or 'asis'}")
    out.mkdir(parents=True, exist_ok=True)
    previous = set_arm(lr, args.arm == "on") if args.arm else None
    print(f"TOTP fixture: {len(jobs)} runs, arm={args.arm or 'as configured'}, api={api}, lanes={args.lanes}, out={out}", flush=True)
    factory = bc.LaneFactory(lr, chromium) if args.lanes > 0 else None
    started = time.time()
    try:
        results = bc.run_pool(
            jobs,
            lambda job, lane, label: run_job(job, lane, label, lr=lr, timeout_min=args.timeout_min, poll=args.poll,
                                             auto_resume=args.auto_resume),
            factory=factory, lanes=args.lanes, out=out, label_prefix="lane", say=lr._say)
    finally:
        if previous is not None:
            try:
                lr._request("PATCH", "/agent-settings", {"totp_capability": previous})
            except Exception:  # noqa: BLE001
                pass
    summary = bc.summarize(results)
    (out / "summary.txt").write_text(bc.summary_text(summary, "TOTP fixture") + "\n", encoding="utf-8")
    print(bc.summary_text(summary, "TOTP fixture"), flush=True)
    print(f"done in {(time.time() - started) / 60:.1f} min; scorecard {out / 'scorecard.json'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
