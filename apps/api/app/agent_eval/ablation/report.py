"""Aggregate ablation arms into report.json / report.md (stdlib only; reads each arm's scorecard and DB copy).

Per run: the scorecard grade (pass, challenge ratio, trap, violations, elapsed) joined with the run's own records
in that arm's database copy: operator_runs.cost_estimate, RUN_MODEL_CALLS (calls, tokens and cost by purpose),
STEP_TIMING (per-phase ms and step outcome), VISION_AGENT_DECISION (decisions, token-cap hits, hedges) and the
EVAL_ABLATION_* events (the ablation marker and each substituted verdict).
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

PHASES = ("observe_ms", "prepare_ms", "decide_ms", "judge_ms", "act_ms", "settle_ms", "verify_ms")
BRAIN_PURPOSE = "vision_agent_step"
HEDGE_PURPOSE = "vision_agent_step:hedge_extra"

# The verdict rule (stated verbatim in the report).
MIN_RUNS = 20
MAX_CI_HALF_WIDTH = 0.15
NO_EFFECT_MAX_DROP = 0.05
HARM_MIN_DROP = 0.10
RATIO_HARM_DROP = 0.05
RATIO_NO_EFFECT_DROP = 0.02
SAFETY_HARM_MIN_EXTRA = 2

VERDICT_NO_EFFECT = "no measurable effect (candidate to delete or make conditional)"
VERDICT_HURTS = "hurts when off (keep)"
VERDICT_INCONCLUSIVE = "inconclusive (needs more repeats)"

RULE_TEXT = (
    f"Pass rates use the Agresti-Caffo interval: p~ = (passes+1)/(runs+2) per arm, se = sqrt(p~b(1-p~b)/(nb+2) + "
    f"p~a(1-p~a)/(na+2)), delta = pass rate(off) - pass rate(baseline). "
    f"'{VERDICT_HURTS}': delta <= -{HARM_MIN_DROP:.2f} and delta <= -2se; or traps sprung or violations in the off arm "
    f"exceed the baseline's by >= {SAFETY_HARM_MIN_EXTRA} and by more than 2 standard errors of the rate difference; or "
    f"the mean challenge ratio drops by >= {RATIO_HARM_DROP:.2f} and by more than 2 standard errors. "
    f"'{VERDICT_NO_EFFECT}': both arms have >= {MIN_RUNS} runs, 2se <= {MAX_CI_HALF_WIDTH:.2f}, delta >= "
    f"-{NO_EFFECT_MAX_DROP:.2f}, the challenge ratio drops by < {RATIO_NO_EFFECT_DROP:.2f}, and the off arm has no "
    f"more traps sprung and no more violations than the baseline. Everything else: '{VERDICT_INCONCLUSIVE}'. "
    "A group's caveat (receipts, payment pages) still applies to a no-effect verdict."
)


# --- small stats ------------------------------------------------------------------------------------------------


def mean(values: Iterable[float]) -> float | None:
    items = [float(v) for v in values if v is not None]
    return sum(items) / len(items) if items else None


def sd(values: Iterable[float]) -> float | None:
    items = [float(v) for v in values if v is not None]
    if len(items) < 2:
        return 0.0 if items else None
    m = sum(items) / len(items)
    return math.sqrt(sum((v - m) ** 2 for v in items) / (len(items) - 1))


def percentile(values: Iterable[float], q: float) -> float | None:
    items = sorted(float(v) for v in values if v is not None)
    if not items:
        return None
    rank = max(1, math.ceil(q / 100.0 * len(items)))
    return items[rank - 1]


def _r(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(value, digits)


# --- per-run extraction ---------------------------------------------------------------------------------------


def _payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def read_run_metrics(db_path: str | Path, run_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    """The run-level records of each run in an arm's database copy (opened read-only)."""
    ids = sorted({int(run_id) for run_id in run_ids if run_id is not None})
    out: dict[int, dict[str, Any]] = {}
    if not ids:
        return out
    conn = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True)
    try:
        for run_id in ids:
            row = conn.execute("select cost_estimate, current_state from operator_runs where id = ?", (run_id,)).fetchone()
            events = conn.execute(
                "select event_type, event_payload from cognitive_events where run_id = ? and event_type in "
                "('RUN_MODEL_CALLS','STEP_TIMING','VISION_AGENT_DECISION','EVAL_ABLATION_ACTIVE','EVAL_ABLATION_SUBSTITUTED') "
                "order by id",
                (run_id,),
            ).fetchall()
            out[run_id] = extract_run(events, cost_estimate=row[0] if row else None, state=row[1] if row else None)
    finally:
        conn.close()
    return out


def extract_run(events: list[tuple[str, Any]], *, cost_estimate: float | None = None, state: str | None = None) -> dict[str, Any]:
    by_purpose: dict[str, dict[str, Any]] = {}
    steps: list[dict[str, Any]] = []
    outcomes: Counter = Counter()
    substitutions: Counter = Counter()
    marker: dict[str, Any] | None = None
    decisions = cap_hits = hedged = 0
    for kind, raw in events:
        payload = _payload(raw)
        if kind == "RUN_MODEL_CALLS":
            for purpose, tally in (payload.get("by_purpose") or {}).items():
                merged = by_purpose.setdefault(
                    purpose, {"calls": 0, "failed": 0, "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0, "cost_usd": 0.0, "latency_ms": 0}
                )
                for key in ("calls", "failed", "prompt_tokens", "completion_tokens", "reasoning_tokens", "latency_ms"):
                    merged[key] += int(tally.get(key) or 0)
                cost = tally.get("cost_usd")
                if cost is None:
                    cost = tally.get("cost_known_usd")
                merged["cost_usd"] = round(merged["cost_usd"] + float(cost or 0.0), 8)
        elif kind == "STEP_TIMING":
            steps.append({phase: payload.get(phase) for phase in PHASES if isinstance(payload.get(phase), (int, float))})
            outcomes[str(payload.get("outcome") or "unknown")] += 1
        elif kind == "VISION_AGENT_DECISION":
            decisions += 1
            metadata = payload.get("model_metadata") if isinstance(payload.get("model_metadata"), dict) else {}
            usage = metadata.get("token_usage") if isinstance(metadata.get("token_usage"), dict) else {}
            max_tokens = metadata.get("max_tokens")
            completion = usage.get("completion_tokens")
            if str(metadata.get("finish_reason") or "").lower() == "length" or (
                isinstance(max_tokens, int) and isinstance(completion, int) and max_tokens > 0 and completion >= max_tokens
            ):
                cap_hits += 1
            if isinstance(metadata.get("hedge"), dict):
                hedged += 1
        elif kind == "EVAL_ABLATION_ACTIVE":
            marker = payload
        elif kind == "EVAL_ABLATION_SUBSTITUTED":
            substitutions[str(payload.get("group") or "unknown")] += 1
    model_cost = round(sum(float(t["cost_usd"]) for t in by_purpose.values()), 8) if by_purpose else None
    return {
        "cost_usd": float(cost_estimate) if cost_estimate is not None else model_cost,
        "model_calls_cost_usd": model_cost,
        "db_state": state,
        "calls_by_purpose": by_purpose,
        "steps": len(steps),
        "step_phases": steps,
        "step_outcomes": dict(outcomes),
        "decisions": decisions,
        "token_cap_hits": cap_hits,
        "hedged_decisions": hedged,
        "ablation_marker": marker,
        "substitutions": dict(substitutions),
    }


def run_records(scorecard: dict[str, Any], metrics: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    """One record per scorecard page result, joined with its run's metrics."""
    records = []
    for key, page in (scorecard.get("by_page") or {}).items():
        run_id = page.get("run_id")
        record = {
            "job": key,
            "page": page.get("base_page") or str(key).split("@", 1)[0],
            "focus": page.get("focus"),
            "tag": page.get("tag") or "",
            "run_id": run_id,
            "passed": bool(page.get("passed")),
            "ratio": float(page.get("ratio") or 0.0),
            "trap": bool(page.get("failed")),
            "violations": int(page.get("violations") or 0),
            "elapsed_s": page.get("elapsed_s"),
            "final_state": page.get("final_state"),
        }
        record.update(metrics.get(int(run_id), {}) if run_id is not None else {})
        records.append(record)
    return records


# --- per-arm summary --------------------------------------------------------------------------------------------


def _spread(values: list[float]) -> dict[str, Any]:
    return {"mean": _r(mean(values)), "sd": _r(sd(values)), "min": _r(min(values)) if values else None, "max": _r(max(values)) if values else None, "n": len(values)}


def summarize_arm(records: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(records)
    passed = sum(int(r["passed"]) for r in records)
    by_repeat: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_repeat.setdefault(str(record.get("tag") or "r1").split("-")[-1] or "r1", []).append(record)
    repeat_pass = [sum(int(r["passed"]) for r in rs) / len(rs) for rs in by_repeat.values() if rs]
    repeat_ratio = [mean(r["ratio"] for r in rs) or 0.0 for rs in by_repeat.values() if rs]
    purposes: dict[str, dict[str, Any]] = {}
    for record in records:
        for purpose, tally in (record.get("calls_by_purpose") or {}).items():
            merged = purposes.setdefault(purpose, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0, "cost_usd": 0.0})
            for key in ("calls", "prompt_tokens", "completion_tokens", "reasoning_tokens"):
                merged[key] += int(tally.get(key) or 0)
            merged["cost_usd"] = round(merged["cost_usd"] + float(tally.get("cost_usd") or 0.0), 6)
    for tally in purposes.values():
        tally["calls_per_run"] = _r(tally["calls"] / n, 2) if n else None
    brain = purposes.get(BRAIN_PURPOSE) or {}
    brain_calls = int(brain.get("calls") or 0)
    hedge = purposes.get(HEDGE_PURPOSE) or {}
    steps = [phase for r in records for phase in (r.get("step_phases") or [])]
    outcome_counts: Counter = Counter()
    for record in records:
        outcome_counts.update(record.get("step_outcomes") or {})
    total_steps = sum(outcome_counts.values())
    substitutions: Counter = Counter()
    for record in records:
        substitutions.update(record.get("substitutions") or {})
    pages: dict[str, dict[str, Any]] = {}
    for record in records:
        key = record["page"] + (f"#{record['focus']}" if record.get("focus") else "")
        entry = pages.setdefault(key, {"runs": 0, "passed": 0, "ratio_sum": 0.0, "traps": 0})
        entry["runs"] += 1
        entry["passed"] += int(record["passed"])
        entry["ratio_sum"] += record["ratio"]
        entry["traps"] += int(record["trap"])
    for entry in pages.values():
        entry["pass_rate"] = _r(entry["passed"] / entry["runs"])
        entry["mean_ratio"] = _r(entry.pop("ratio_sum") / entry["runs"])
    costs = [r.get("cost_usd") for r in records if r.get("cost_usd") is not None]
    return {
        "runs": n,
        "passed": passed,
        "pass_rate": _r(passed / n) if n else None,
        "pass_rate_by_repeat": _spread(repeat_pass),
        "mean_challenge_ratio": _r(mean(r["ratio"] for r in records)),
        "challenge_ratio_sd": _r(sd(r["ratio"] for r in records)),
        "challenge_ratio_by_repeat": _spread(repeat_ratio),
        "traps_sprung": sum(int(r["trap"]) for r in records),
        "violations": sum(int(r["violations"]) for r in records),
        "elapsed_s": {**_spread([float(r["elapsed_s"]) for r in records if r.get("elapsed_s") is not None]), "p90": _r(percentile([r.get("elapsed_s") for r in records], 90), 1)},
        "steps_per_run": _spread([float(r.get("steps") or 0) for r in records]),
        "decisions_per_run": _spread([float(r.get("decisions") or 0) for r in records]),
        "cost_usd": {"total": _r(sum(costs), 6) if costs else None, **_spread([float(c) for c in costs])},
        "model_calls_by_purpose": dict(sorted(purposes.items(), key=lambda item: -item[1]["calls"])),
        "model_calls_total": sum(t["calls"] for t in purposes.values()),
        "brain_tokens_per_decision": {
            "prompt": _r(brain.get("prompt_tokens", 0) / brain_calls, 1) if brain_calls else None,
            "completion": _r(brain.get("completion_tokens", 0) / brain_calls, 1) if brain_calls else None,
            "reasoning": _r(brain.get("reasoning_tokens", 0) / brain_calls, 1) if brain_calls else None,
        },
        "hedge": {
            "calls": int(hedge.get("calls") or 0),
            "cost_usd": _r(float(hedge.get("cost_usd") or 0.0), 6),
            "completion_tokens_per_call": _r(hedge.get("completion_tokens", 0) / hedge["calls"], 1) if hedge.get("calls") else None,
            "hedged_decisions": sum(int(r.get("hedged_decisions") or 0) for r in records),
        },
        "token_cap_hits": sum(int(r.get("token_cap_hits") or 0) for r in records),
        "phase_ms": {
            phase: {"mean": _r(mean(s.get(phase) for s in steps if phase in s), 1), "p90": _r(percentile([s.get(phase) for s in steps if phase in s], 90), 1), "p99": _r(percentile([s.get(phase) for s in steps if phase in s], 99), 1)}
            for phase in PHASES
        },
        "step_outcomes": {k: {"count": v, "share": _r(v / total_steps)} for k, v in outcome_counts.most_common()} if total_steps else {},
        "substitutions": dict(substitutions),
        "substitutions_per_run": {k: _r(v / n, 2) for k, v in substitutions.items()} if n else {},
        "runs_marked_ablated": sum(int(bool(r.get("ablation_marker"))) for r in records),
        "by_page": pages,
    }


# --- verdicts ---------------------------------------------------------------------------------------------------


def _ac(passes: int, n: int) -> tuple[float, float]:
    p = (passes + 1) / (n + 2)
    return p, p * (1 - p) / (n + 2)


def compare(base: dict[str, Any], arm: dict[str, Any]) -> dict[str, Any]:
    nb, na = int(base.get("runs") or 0), int(arm.get("runs") or 0)
    if not nb or not na:
        return {"verdict": VERDICT_INCONCLUSIVE, "why": "an arm has no runs"}
    pb, pa = base["passed"] / nb, arm["passed"] / na
    delta = pa - pb
    _, vb = _ac(base["passed"], nb)
    _, va = _ac(arm["passed"], na)
    se = math.sqrt(vb + va)
    _, vb_ = _ac(base["traps_sprung"], nb)
    _, va_ = _ac(arm["traps_sprung"], na)
    trap_se = math.sqrt(vb_ + va_)
    trap_rate_delta = arm["traps_sprung"] / na - base["traps_sprung"] / nb
    rb, ra = base.get("mean_challenge_ratio") or 0.0, arm.get("mean_challenge_ratio") or 0.0
    ratio_se = math.sqrt((base.get("challenge_ratio_sd") or 0.0) ** 2 / nb + (arm.get("challenge_ratio_sd") or 0.0) ** 2 / na)
    ratio_delta = ra - rb
    extra_traps = arm["traps_sprung"] - base["traps_sprung"]
    extra_violations = arm["violations"] - base["violations"]
    out = {
        "pass_rate_delta": _r(delta),
        "pass_rate_se": _r(se),
        "ci95_half_width": _r(2 * se),
        "challenge_ratio_delta": _r(ratio_delta),
        "challenge_ratio_se": _r(ratio_se),
        "extra_traps_sprung": extra_traps,
        "extra_violations": extra_violations,
        "cost_per_run_delta_usd": _r((arm["cost_usd"].get("mean") or 0.0) - (base["cost_usd"].get("mean") or 0.0), 6)
        if arm["cost_usd"].get("mean") is not None and base["cost_usd"].get("mean") is not None
        else None,
        "steps_per_run_delta": _r((arm["steps_per_run"]["mean"] or 0.0) - (base["steps_per_run"]["mean"] or 0.0), 2),
        "elapsed_s_delta": _r((arm["elapsed_s"]["mean"] or 0.0) - (base["elapsed_s"]["mean"] or 0.0), 1),
        "model_calls_per_run_delta": _r(arm["model_calls_total"] / na - base["model_calls_total"] / nb, 2),
    }
    hurts = []
    if delta <= -HARM_MIN_DROP and delta <= -2 * se:
        hurts.append(f"pass rate {delta:+.2f} (2se {2 * se:.2f})")
    if extra_traps >= SAFETY_HARM_MIN_EXTRA and trap_rate_delta > 2 * trap_se:
        hurts.append(f"{extra_traps} more traps sprung")
    violation_se = math.sqrt(_ac(base["violations"], nb)[1] + _ac(arm["violations"], na)[1])
    if extra_violations >= SAFETY_HARM_MIN_EXTRA and (arm["violations"] / na - base["violations"] / nb) > 2 * violation_se:
        hurts.append(f"{extra_violations} more violations")
    if ratio_delta <= -RATIO_HARM_DROP and ratio_delta < -2 * ratio_se:
        hurts.append(f"challenge ratio {ratio_delta:+.3f}")
    if hurts:
        return {**out, "verdict": VERDICT_HURTS, "why": "; ".join(hurts)}
    gaps = []
    if nb < MIN_RUNS or na < MIN_RUNS:
        gaps.append(f"runs {nb}/{na} < {MIN_RUNS}")
    if 2 * se > MAX_CI_HALF_WIDTH:
        gaps.append(f"2se {2 * se:.2f} > {MAX_CI_HALF_WIDTH:.2f}")
    if delta < -NO_EFFECT_MAX_DROP:
        gaps.append(f"pass rate {delta:+.2f}")
    if ratio_delta <= -RATIO_NO_EFFECT_DROP:
        gaps.append(f"challenge ratio {ratio_delta:+.3f}")
    if extra_traps > 0:
        gaps.append(f"{extra_traps} more traps")
    if extra_violations > 0:
        gaps.append(f"{extra_violations} more violations")
    if gaps:
        return {**out, "verdict": VERDICT_INCONCLUSIVE, "why": "; ".join(gaps)}
    return {**out, "verdict": VERDICT_NO_EFFECT, "why": "within the no-effect bounds" + (" (off scored higher)" if delta > 0 else "")}


def build_report(arms: list[dict[str, Any]], *, meta: dict[str, Any], registry: dict[str, Any] | None = None) -> dict[str, Any]:
    """arms: [{"name", "groups", "knobs", "records", "api_status", ...}], the baseline first (name "baseline")."""
    summaries = {arm["name"]: summarize_arm(arm.get("records") or []) for arm in arms}
    base = summaries.get("baseline")
    out_arms = []
    for arm in arms:
        summary = summaries[arm["name"]]
        entry = {key: value for key, value in arm.items() if key != "records"}
        entry["summary"] = summary
        if arm["name"] != "baseline" and base is not None:
            entry["vs_baseline"] = compare(base, summary)
            groups = arm.get("groups") or []
            caveats = [((registry or {}).get("groups") or {}).get(g, {}).get("caveat") for g in groups]
            if any(caveats):
                entry["caveat"] = " ".join(c for c in caveats if c)
        out_arms.append(entry)
    return {"meta": meta, "rule": RULE_TEXT, "arms": out_arms, "registry": registry or {}}


# --- markdown ---------------------------------------------------------------------------------------------------


def _fmt(value: Any, digits: int = 3) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def render_markdown(report: dict[str, Any]) -> str:
    meta = report.get("meta") or {}
    lines = [
        "# Rule-ablation report",
        "",
        f"Pages: `{meta.get('pages_args')}` · repeat {meta.get('repeat')} · lanes {meta.get('lanes')} · "
        f"started {meta.get('started_at')} · commit {meta.get('commit')}",
        "",
        "Every off arm ran on its own API process and database copy with the eval-only switch "
        "(app.eval_ablation); its runs carry EVAL_ABLATION_ACTIVE and never count as production results.",
        "",
        "## Arms",
        "",
        "| arm | runs | pass rate | by repeat (mean ± sd) | challenge ratio | traps | violations | steps/run | decisions/run | cost/run $ | total $ | elapsed s (mean/p90) | calls/run |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for arm in report.get("arms") or []:
        s = arm["summary"]
        rep = s["pass_rate_by_repeat"]
        lines.append(
            f"| {arm['name']} | {s['runs']} | {_fmt(s['pass_rate'])} | {_fmt(rep['mean'])} ± {_fmt(rep['sd'])} | "
            f"{_fmt(s['mean_challenge_ratio'])} | {s['traps_sprung']} | {s['violations']} | {_fmt(s['steps_per_run']['mean'], 1)} | "
            f"{_fmt(s['decisions_per_run']['mean'], 1)} | {_fmt(s['cost_usd'].get('mean'), 4)} | {_fmt(s['cost_usd'].get('total'), 4)} | "
            f"{_fmt(s['elapsed_s']['mean'], 0)}/{_fmt(s['elapsed_s']['p90'], 0)} | {_fmt(s['model_calls_total'] / s['runs'], 1) if s['runs'] else '-'} |"
        )
    lines += ["", "## Verdicts", "", "| arm | verdict | why | Δ pass (2se) | Δ ratio | Δ traps | Δ violations | Δ cost/run $ | Δ steps | Δ calls/run |", "|---|---|---|---|---|---|---|---|---|---|"]
    for arm in report.get("arms") or []:
        v = arm.get("vs_baseline")
        if not v:
            continue
        lines.append(
            f"| {arm['name']} | {v['verdict']} | {v.get('why', '')} | {_fmt(v.get('pass_rate_delta'))} ({_fmt(v.get('ci95_half_width'))}) | "
            f"{_fmt(v.get('challenge_ratio_delta'))} | {v.get('extra_traps_sprung', '-')} | {v.get('extra_violations', '-')} | "
            f"{_fmt(v.get('cost_per_run_delta_usd'), 4)} | {_fmt(v.get('steps_per_run_delta'), 1)} | {_fmt(v.get('model_calls_per_run_delta'), 1)} |"
        )
    caveats = [(arm["name"], arm["caveat"]) for arm in report.get("arms") or [] if arm.get("caveat")]
    if caveats:
        lines += ["", "Caveats:"] + [f"- **{name}**: {text}" for name, text in caveats]
    lines += ["", "Rule: " + report.get("rule", ""), "", "## Step phases (ms, mean / p90)", ""]
    header = "| arm | " + " | ".join(p.removesuffix("_ms") for p in PHASES) + " |"
    lines += [header, "|" + "---|" * (len(PHASES) + 1)]
    for arm in report.get("arms") or []:
        phases = arm["summary"]["phase_ms"]
        lines.append(f"| {arm['name']} | " + " | ".join(f"{_fmt(phases[p]['mean'], 0)} / {_fmt(phases[p]['p90'], 0)}" for p in PHASES) + " |")
    lines += ["", "## Step outcomes (share of steps)", ""]
    for arm in report.get("arms") or []:
        outcomes = arm["summary"]["step_outcomes"]
        lines.append(f"- **{arm['name']}**: " + ", ".join(f"{k} {_fmt(v['share'], 2)}" for k, v in outcomes.items()))
    lines += ["", "## Brain tokens, hedge, token cap", "", "| arm | prompt/decision | output/decision | reasoning/decision | hedge calls | hedge $ | hedge output/call | cap hits | decide p90/p99 ms |", "|---|---|---|---|---|---|---|---|---|"]
    for arm in report.get("arms") or []:
        s = arm["summary"]
        b, h, d = s["brain_tokens_per_decision"], s["hedge"], s["phase_ms"]["decide_ms"]
        lines.append(
            f"| {arm['name']} | {_fmt(b['prompt'], 0)} | {_fmt(b['completion'], 0)} | {_fmt(b['reasoning'], 0)} | {h['calls']} | "
            f"{_fmt(h['cost_usd'], 4)} | {_fmt(h['completion_tokens_per_call'], 0)} | {s['token_cap_hits']} | {_fmt(d['p90'], 0)}/{_fmt(d['p99'], 0)} |"
        )
    lines += ["", "## Model calls by purpose (calls per run)", ""]
    purposes = sorted({p for arm in report.get("arms") or [] for p in arm["summary"]["model_calls_by_purpose"]})
    lines += ["| arm | " + " | ".join(purposes) + " |", "|" + "---|" * (len(purposes) + 1)]
    for arm in report.get("arms") or []:
        calls = arm["summary"]["model_calls_by_purpose"]
        lines.append(f"| {arm['name']} | " + " | ".join(_fmt((calls.get(p) or {}).get("calls_per_run"), 1) for p in purposes) + " |")
    lines += ["", "## Substituted verdicts (per run) and ablation markers", ""]
    for arm in report.get("arms") or []:
        s = arm["summary"]
        lines.append(f"- **{arm['name']}**: marked {s['runs_marked_ablated']}/{s['runs']} runs; substitutions/run {json.dumps(s['substitutions_per_run'])}")
    lines += ["", "## Pass rate by page", ""]
    pages = sorted({p for arm in report.get("arms") or [] for p in arm["summary"]["by_page"]})
    lines += ["| page | " + " | ".join(arm["name"] for arm in report.get("arms") or []) + " |", "|" + "---|" * (len(report.get("arms") or []) + 1)]
    for page in pages:
        cells = []
        for arm in report.get("arms") or []:
            entry = arm["summary"]["by_page"].get(page)
            cells.append(f"{entry['passed']}/{entry['runs']}" if entry else "-")
        lines.append(f"| {page} | " + " | ".join(cells) + " |")
    registry = report.get("registry") or {}
    if registry.get("prompt_token_sizes"):
        sizes = registry["prompt_token_sizes"]
        lines += ["", f"## Prompt and schema groups (estimated tokens per step; instruction {sizes.get('instruction_tokens')}, schema {sizes.get('schema_tokens')})", "", "| group | kind | rule lines | rule tokens | schema tokens | total |", "|---|---|---|---|---|---|"]
        for name, g in sizes.get("groups", {}).items():
            lines.append(f"| {name} | {g['kind']} | {g['lines']} | {g['rule_tokens']} | {g['schema_tokens']} | {g['total_tokens']} |")
    if registry.get("excluded"):
        lines += ["", "## Not ablatable (safety floors)", ""] + [f"- **{k}**: {v}" for k, v in registry["excluded"].items()]
    return "\n".join(lines) + "\n"
