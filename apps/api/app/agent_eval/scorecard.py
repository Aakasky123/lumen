"""Eval scorecard: one command that turns the eval harness into tracked numbers.

Tiers:
  regressions   — deterministic and free: the capability-regression + golden-run pytest
                  suites (recorded fixtures + spec validators, no browser, no model).
  answer_quality — live model: the labeled ask-vs-confirm-vs-auto corpus
                  (app.agent_eval.answer_quality). Runs only with --live so the
                  scorecard never spends tokens implicitly.

Every run appends one JSONL entry (timestamp, git commit, tier scores) to
<artifact_root>/eval_scores/history.jsonl, so prompt/loop changes become deltas between
entries instead of impressions. Schedule it (Windows Task Scheduler / cron) as:

    python -m app.agent_eval.scorecard run --live --trials 3
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import get_settings

_API_ROOT = Path(__file__).resolve().parents[2]

# Deterministic, model-free suites that replay recorded runs against spec validators.
REGRESSION_TEST_PATTERNS = (
    "tests/test_capability_regression_*.py",
    "tests/test_golden_completion_run.py",
    "tests/test_answer_quality_corpus.py",
    "tests/test_agent_eval_brain.py",
)

_SUMMARY_PATTERN = re.compile(
    r"(?:(?P<failed>\d+)\s+failed)?(?:,\s*)?(?P<passed>\d+)\s+passed"
)


def history_path() -> Path:
    directory = get_settings().artifact_root / "eval_scores"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "history.jsonl"


def parse_pytest_summary(output: str) -> dict[str, int]:
    """Extract pass/fail counts from pytest's terminal summary (last matching line)."""
    passed = 0
    failed = 0
    for line in output.splitlines():
        match = _SUMMARY_PATTERN.search(line)
        if match:
            passed = int(match.group("passed") or 0)
            failed = int(match.group("failed") or 0)
    return {"passed": passed, "failed": failed}


def regression_targets() -> list[str]:
    targets: list[str] = []
    for pattern in REGRESSION_TEST_PATTERNS:
        targets.extend(
            sorted(str(path.relative_to(_API_ROOT)) for path in _API_ROOT.glob(pattern))
        )
    return targets


def run_regression_tier() -> dict[str, Any]:
    targets = regression_targets()
    if not targets:
        return {"skipped": "no_regression_targets"}
    started = time.monotonic()
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", *targets, "-q", "--tb=no", "-p", "no:cacheprovider"],
        cwd=str(_API_ROOT),
        capture_output=True,
        text=True,
        timeout=1800,
    )
    counts = parse_pytest_summary(completed.stdout or "")
    return {
        **counts,
        "exit_code": completed.returncode,
        "duration_seconds": round(time.monotonic() - started, 1),
        "target_count": len(targets),
    }


def run_answer_quality_tier(*, trials: int = 3, model: Any = None) -> dict[str, Any]:
    """Live tier: labeled decision corpus against the configured brain. Skips cleanly
    when no live provider is configured, so scheduling is safe on any machine."""
    from app.agent_eval.answer_quality import run_corpus

    if model is None:
        from app.lumen_model import LumenModelClient
        from app.db import SessionLocal, init_db

        init_db()
        with SessionLocal() as db:
            client = LumenModelClient(db)
            if not client.live_enabled:
                return {"skipped": "no_live_provider"}
            report = run_corpus(client, trials=trials)
    else:
        report = run_corpus(model, trials=trials)
    return {
        "trials_per_case": trials,
        "case_count": len(report.get("cases") or []),
        "decision_accuracy": round(float(report.get("decision_accuracy") or 0.0), 4),
        "fabrication_rate": round(float(report.get("fabrication_rate") or 0.0), 4),
        "stability_rate": round(float(report.get("stability_rate") or 0.0), 4),
        "unstable_ids": list(report.get("unstable_ids") or []),
    }


def _git_commit() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(_API_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        return completed.stdout.strip() if completed.returncode == 0 else ""
    except Exception:  # noqa: BLE001 - scorecard works outside a git checkout too
        return ""


def benchmark_run_context(*, benchmark: str, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
    """Provenance block for one benchmark result: what ran, on which commit, with which
    models. Recorded results without this are not comparable to anything."""
    context: dict[str, Any] = {
        "benchmark": benchmark,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git_commit(),
        "parameters": dict(parameters or {}),
    }
    try:
        from app.db import SessionLocal
        from app.model_provider import ModelProviderService

        with SessionLocal() as db:
            service = ModelProviderService(db)
            active = service.active_provider()
            context["model"] = {
                "provider_type": active.provider_type,
                "model": active.model_name,
                "reasoning_effort": active.reasoning_effort,
            }
            vision = service.vision_provider(user_id=None)
            if vision is not None:
                context["vision_model"] = {
                    "provider_type": vision.provider_type,
                    "model": vision.model_name,
                    "reasoning_effort": vision.reasoning_effort,
                }
            # The Checker seat is a per-user designation: read the owner's (the first user's).
            from sqlalchemy import select

            from app import models

            owner_id = db.scalars(select(models.User.id).order_by(models.User.id)).first()
            checker = service.judge_provider(user_id=owner_id)
            if checker is not None:
                context["judge_model"] = {
                    "provider_type": checker.provider_type,
                    "model": checker.model_name,
                    "reasoning_effort": checker.reasoning_effort,
                }
    except Exception:  # noqa: BLE001 - provenance must not block a benchmark result
        context.setdefault("model", {})
    return context


def record_scorecard(*, live: bool = False, trials: int = 3) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git_commit(),
        "tiers": {"regressions": run_regression_tier()},
    }
    if live:
        entry["tiers"]["answer_quality"] = run_answer_quality_tier(trials=trials)
    path = history_path()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    return entry


def load_history(limit: int = 20) -> list[dict[str, Any]]:
    path = history_path()
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries[-max(1, int(limit)):]


def _metric_deltas(current: dict[str, Any], previous: dict[str, Any] | None) -> list[str]:
    if not previous:
        return []
    lines: list[str] = []
    current_aq = (current.get("tiers") or {}).get("answer_quality") or {}
    previous_aq = (previous.get("tiers") or {}).get("answer_quality") or {}
    for metric in ("decision_accuracy", "fabrication_rate", "stability_rate"):
        if metric in current_aq and metric in previous_aq:
            delta = float(current_aq[metric]) - float(previous_aq[metric])
            lines.append(f"  {metric}: {current_aq[metric]} ({delta:+.4f} vs previous)")
    current_reg = (current.get("tiers") or {}).get("regressions") or {}
    previous_reg = (previous.get("tiers") or {}).get("regressions") or {}
    if "failed" in current_reg and "failed" in previous_reg:
        delta_failed = int(current_reg["failed"]) - int(previous_reg["failed"])
        lines.append(
            f"  regressions failed: {current_reg['failed']} ({delta_failed:+d} vs previous)"
        )
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description="Run and track eval scores over time.")
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run", help="Run eval tiers and append a history entry.")
    run_parser.add_argument(
        "--live",
        action="store_true",
        help="Also run the live answer-quality corpus (spends model tokens).",
    )
    run_parser.add_argument("--trials", type=int, default=3)
    history_parser = sub.add_parser("history", help="Show recent scorecard entries.")
    history_parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()

    if args.command == "run":
        previous_entries = load_history(limit=1)
        entry = record_scorecard(live=args.live, trials=args.trials)
        print(json.dumps(entry, ensure_ascii=False, indent=2, default=str))
        for line in _metric_deltas(entry, previous_entries[-1] if previous_entries else None):
            print(line)
        regressions = entry["tiers"]["regressions"]
        return 1 if int(regressions.get("failed") or 0) else 0

    if args.command == "history":
        entries = load_history(limit=args.limit)
        if not entries:
            print(f"No scorecard history at {history_path()}")
            return 0
        for index, entry in enumerate(entries):
            print(json.dumps(entry, ensure_ascii=False, default=str))
            if index == len(entries) - 1 and len(entries) > 1:
                for line in _metric_deltas(entry, entries[-2]):
                    print(line)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
