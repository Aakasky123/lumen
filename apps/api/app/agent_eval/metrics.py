"""Metrics across runs: recent runs, gauntlet page trends, regressions, time sinks, refusal reasons.

Run from apps/api:

    python -m app.agent_eval.metrics [--limit 25] [--since 2026-10-01] [--pages v4_text,v4_keys] [--json]
    python -m app.agent_eval.metrics --db C:/path/storage/lumen.db --eval-runs C:/path/storage/artifacts/eval_runs

Read-only: a SQLite database is opened in read-only mode (`mode=ro`), so the report can run beside a live
API. Defaults are the configured database and `<artifact_root>/eval_runs`. The data comes from
`app.run_metrics`, which also backs `GET /operator/metrics`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from app import run_metrics


def _read_only_engine(database_url: str | None, db_path: str | None):
    if db_path:
        path = Path(db_path).expanduser().resolve()
        if not path.is_file():
            raise SystemExit(f"database not found: {path}")
        return create_engine(f"sqlite:///file:{path.as_posix()}?mode=ro&uri=true", future=True)
    from app.config import get_settings

    url = make_url(database_url or get_settings().database_url)
    if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
        path = Path(url.database).expanduser().resolve()
        if not path.is_file():
            raise SystemExit(f"database not found: {path}")
        return create_engine(f"sqlite:///file:{path.as_posix()}?mode=ro&uri=true", future=True)
    return create_engine(url, future=True)


def _pct(value: Any) -> str:
    return f"{float(value) * 100:.0f}%" if isinstance(value, (int, float)) else "-"


def _money(value: Any) -> str:
    return f"${value:.4f}" if isinstance(value, (int, float)) else "-"


def _secs(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "-"
    return f"{value / 60:.1f}m" if value >= 600 else f"{value:.0f}s"


def _cut(value: Any, width: int) -> str:
    value = str(value if value is not None else "-")
    return value if len(value) <= width else value[: width - 1] + "~"


def _top(counts: dict[str, Any], count: int = 8) -> str:
    items = list(counts.items())[:count]
    return ", ".join(f"{key} {value}" for key, value in items) if items else "-"


def _history_section(data: dict[str, Any], title: str, label: str, reg_title: str, window: dict[str, Any]) -> list[str]:
    """The per-name trend table and regression list shared by the gauntlet and the mac bench."""
    lines: list[str] = []
    pages = data["pages"]
    lines.append(f"{title} ({len(pages)}; trend oldest->newest over the last {window['trend_attempts']} attempts)")
    lines.append(f"  {label:<20} {'trend':<7} {'pass':>7} {'best':>6} {'latest':>7} {'latest at':<17} {'last pass commit':<16}")
    for page, info in pages.items():
        best = info["best"] or {}
        latest = info["latest"] or {}
        lines.append(
            f"  {_cut(page, 20):<20} {info['trend']:<7} {info['passes']:>3}/{info['attempts']:<3} "
            f"{best.get('completed')}/{best.get('total'):<4} {latest.get('completed')}/{latest.get('total'):<5} "
            f"{_cut((latest.get('recorded_at') or '')[:16], 17):<17} {_cut((info['last_pass_commit'] or '-')[:12], 16):<16}"
        )
    regressions = data["regressions"]
    lines.append("")
    lines.append(f"{reg_title} ({sum(1 for item in regressions if item['kind'] == 'regression')} regression, "
                 f"{sum(1 for item in regressions if item['kind'] == 'flaky')} flaky)")
    for item in regressions:
        last_pass = item["last_pass"] or {}
        latest = item["latest"] or {}
        lines.append(
            f"  {item['kind']:<10} {item['page']:<20} passed {(last_pass.get('commit') or '?')[:9]} "
            f"({(last_pass.get('recorded_at') or '')[:10]}, {last_pass.get('dir')}); latest "
            f"{latest.get('completed')}/{latest.get('total')} at {(latest.get('commit') or '?')[:9]} "
            f"({(latest.get('recorded_at') or '')[:10]}, {latest.get('dir')})"
        )
    if not regressions:
        lines.append("  none")
    return lines


def render_text(report: dict[str, Any]) -> str:
    lines: list[str] = []
    window = report["window"]
    scope = [f"last {window['limit']} runs"]
    if window.get("since"):
        scope.append(f"since {window['since']}")
    if window.get("pages"):
        scope.append("pages " + ",".join(window["pages"]))
    lines.append("Lumen metrics (" + "; ".join(scope) + ")")
    lines.append("")

    runs = report["runs"]
    lines.append(f"RUNS ({len(runs)})")
    header = (
        f"{'run':>5} {'state':<17} {'reason':<30} {'page':<17} {'bench':>5} {'steps':>5} {'acts':>4} "
        f"{'wall':>6} {'decide':>6} {'judge':>6} {'cost':>8} {'ref':>3} {'inv':>3} {'np':>3} {'esc':>3} {'cache':>6}"
    )
    lines.append(header)
    for run in runs:
        bench = run.get("bench") or {}
        bench_text = f"{bench.get('completed')}/{bench.get('total')}" if bench else "-"
        counts = run["counts"]
        cost = run["cost_usd"].get("total")
        if cost is None:
            cost = run["cost_usd"].get("brain")
        lines.append(
            f"{run['run_id']:>5} {_cut(run['state'], 17):<17} {_cut(run.get('reason'), 30):<30} "
            f"{_cut(run.get('page'), 17):<17} {bench_text:>5} {run['steps']:>5} {run['executed_actions']:>4} "
            f"{_secs(run.get('wall_seconds')):>6} {_secs(run['time_seconds'].get('decide')):>6} "
            f"{_secs(run['time_seconds'].get('judge')):>6} {_money(cost):>8} {counts['refusals']:>3} "
            f"{counts['invalid_outputs']:>3} {counts['no_progress']:>3} {counts['escalations']:>3} "
            f"{_pct(run.get('cache_hit_rate')):>6}"
        )
    lines.append("  ref = refusals (incl. sequence_invalid), inv = invalid/empty model outputs, np = no-progress steps, esc = brain escalations, cache = cached share of input tokens")
    lines.append("")

    totals = report["totals"]
    lines.append(
        f"WINDOW TOTALS: {totals['runs']} runs, cost {_money(totals['cost_usd'])} "
        f"({totals['runs_with_cost']} with cost), prompt cache hit {_pct(totals.get('cache_hit_rate'))}; states: {_top(totals['states'])}"
    )
    lines.append("  end reasons: " + _top(totals["end_reasons"], 8))
    lines.append("")
    lines.append("TIME SINKS")
    lines.append(
        "  phases: "
        + ", ".join(f"{item['phase']} {_secs(item['seconds'])} ({item['share'] * 100:.0f}%)" for item in totals["time_sinks"])
    )
    if totals["waste_seconds"]:
        lines.append("  waste: " + ", ".join(f"{key} {_secs(value)}" for key, value in totals["waste_seconds"].items()))
    for item in totals["model_calls_by_latency"][:8]:
        lines.append(
            f"  {_cut(item['purpose'], 40):<40} {item['calls']:>5} calls {_secs(item['latency_seconds']):>7} {_money(item['cost_usd']):>9}"
        )
    lines.append("")
    lines.append("REFUSALS AND FAILURES")
    lines.append("  refusals: " + _top(totals["refusal_reasons"], 10))
    lines.append("  step outcomes: " + _top(totals["step_outcomes"], 10))
    lines.append("  model failures: " + _top(totals["model_failures"], 8))
    lines.append("  escalations: " + _top(totals["escalation_reasons"], 6))
    lines.append("")

    lines.extend(_history_section(report["gauntlet"], "GAUNTLET PAGES", "page", "REGRESSIONS", window))
    mac_bench = report.get("mac_bench")
    if mac_bench and mac_bench.get("pages"):
        lines.append("")
        lines.extend(_history_section(mac_bench, "MAC BENCH TASKS", "task", "MAC BENCH REGRESSIONS", window))

    osworld = report.get("osworld") or []
    if osworld:
        lines.append("")
        lines.append(f"OSWORLD (latest {min(5, len(osworld))} of {len(osworld)} result dirs)")
        for row in osworld[-5:]:
            rate = row.get("success_rate")
            rate_text = f"{rate * 100:.1f}%" if isinstance(rate, (int, float)) else "-"
            lines.append(
                f"  {_cut(row['name'], 30):<30} {row['written_at'][:10]} {row.get('tasks')!s:>4} tasks {rate_text:>6}  "
                + _top(row.get("top_failure_reasons") or {}, 3)
            )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lumen metrics across runs (read-only)")
    parser.add_argument("--limit", type=int, default=run_metrics.DEFAULT_RUN_LIMIT, help="runs in the window (max 500)")
    parser.add_argument("--since", help="only runs and attempts at or after this ISO date/datetime (UTC)")
    parser.add_argument("--pages", help="comma-separated gauntlet page names")
    parser.add_argument("--attempts", type=int, default=run_metrics.DEFAULT_TREND_ATTEMPTS, help="attempts per page trend")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--db", help="SQLite database file (default: the configured database)")
    parser.add_argument("--eval-runs", help="eval_runs directory (default: <artifact_root>/eval_runs)")
    args = parser.parse_args(argv)
    try:
        since = run_metrics.parse_since(args.since)
    except ValueError as exc:
        parser.error(str(exc))
    if args.eval_runs:
        eval_runs = Path(args.eval_runs)
    else:
        from app.config import get_settings

        eval_runs = Path(get_settings().artifact_root) / "eval_runs"
    engine = _read_only_engine(None, args.db)
    try:
        with engine.connect() as conn:
            report = run_metrics.build_report(
                conn,
                eval_runs,
                limit=args.limit,
                since=since,
                pages=run_metrics.parse_pages(args.pages),
                trend_attempts=args.attempts,
            )
    finally:
        engine.dispose()
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(render_text(report))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
