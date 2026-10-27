"""Metrics across runs: what each recent run cost, where its time went, why it stopped, and how every
Interaction Gauntlet page has fared over time.

Read-only. Per-run facts come from the events the loop already writes (RUN_TIMING_SUMMARY, RUN_MODEL_CALLS,
STEP_TIMING and the refusal/failure events); gauntlet history comes from the `scorecard.json` files that
`interaction_bench.live_run` writes under `eval_runs/<dir>/`, and OSWorld history from the
`summary.json` files under `eval_runs/osworld-*`. Every database read is bounded by a run window and goes
through the `run_id` index, so the report stays cheap on a large database.

Used by `python -m app.agent_eval.metrics` and `GET /operator/metrics`.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import bindparam, text

DEFAULT_RUN_LIMIT = 25
MAX_RUN_LIMIT = 500
DEFAULT_TREND_ATTEMPTS = 5
GAUNTLET_BENCHMARK = "lumen-interaction-gauntlet"
# The macOS desktop bench writes the same scorecard shape under its own benchmark name; it is read and
# reported on its own so its pass counts never mix into the gauntlet pages.
MAC_BENCHMARK = "Lumen macOS desktop bench"

# The phases RUN_TIMING_SUMMARY (and each STEP_TIMING) splits a loop's time into.
PHASES = ("observe", "decide", "judge", "act", "verify")

# STEP_TIMING outcomes of a step that did what it set out to do. Every other outcome is a step that
# was refused, invalid, or made no progress, and is counted by name.
_SUCCESS_STEP_OUTCOMES = frozenset(
    {
        "executed",
        "done",
        "final_action_verified_continue",
        "final_action_reconciled_continue",
        "final_submit_verified",
        "final_submit_reconciled",
    }
)

_FINISHED_STATES = frozenset({"READY_FOR_REVIEW", "COMPLETED"})

_EVENT_TYPES = (
    "STEP_TIMING",
    "RUN_TIMING_SUMMARY",
    "RUN_MODEL_CALLS",
    "RUN_STATE_CHANGED",
    "EVAL_CANDIDATE_RECORDED",
    "VISION_GROUNDING_CITATION_REJECTED",
    "VISION_SEQUENCE_FOLLOWUP_DROPPED",
    "VISION_ACTION_PRECONDITION_BLOCKED",
    "VISION_TARGET_BINDING_REJECTED",
    "VISION_ASK_TARGET_BINDING_REJECTED",
    "FINAL_ACTION_IDENTITY_REJECTED",
    "VISION_MODEL_SCHEMA_INVALID",
    "VISION_MODEL_EMPTY_COMPLETION",
    "VISION_MODEL_TIMEOUT",
    "VISION_MODEL_PROVIDER_FAILURE",
    "VISION_PROVIDER_FAILOVER",
    "VISION_BRAIN_ESCALATED",
    "VISION_BRAIN_DEESCALATED",
    "VISION_MODEL_REPLY_CUT_OFF",
)

# Event type -> (category, payload key naming the reason). Refusals are the runtime declining a
# proposed action; model failures are decision calls that produced no usable action.
_REFUSAL_EVENTS = {
    "VISION_GROUNDING_CITATION_REJECTED": ("grounding_rejected", "reason"),
    "VISION_SEQUENCE_FOLLOWUP_DROPPED": ("followup_dropped", "reason"),
    "VISION_ACTION_PRECONDITION_BLOCKED": ("precondition_blocked", "progress_class"),
    "VISION_TARGET_BINDING_REJECTED": ("target_binding_rejected", "reason"),
    # Its reason is the judge's free text, so only the category is counted.
    "VISION_ASK_TARGET_BINDING_REJECTED": ("ask_target_binding_rejected", None),
    "FINAL_ACTION_IDENTITY_REJECTED": ("final_identity_rejected", None),
}
_MODEL_FAILURE_EVENTS = {
    "VISION_MODEL_SCHEMA_INVALID": ("schema_invalid", "invalid_reason"),
    "VISION_MODEL_EMPTY_COMPLETION": ("empty_completion", "failure_reason"),
    "VISION_MODEL_TIMEOUT": ("model_timeout", "failure_reason"),
    "VISION_MODEL_PROVIDER_FAILURE": ("provider_failure", "failure_reason"),
    "VISION_PROVIDER_FAILOVER": ("provider_failover", "reason"),
}

_BENCH_PAGE_IN_URL = re.compile(r"/eval/bench/([A-Za-z0-9_-]+)")


# ---------------------------------------------------------------------------------------------
# Small helpers


def _payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes)):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def parse_since(value: str | None) -> datetime | None:
    """A `--since` / `?since=` value: an ISO date or datetime (UTC when it names no zone)."""

    if not value:
        return None
    parsed = _as_datetime(value.strip())
    if parsed is None:
        raise ValueError(f"since must be an ISO date or datetime, got {value!r}")
    return parsed


def parse_pages(value: str | list[str] | None) -> list[str] | None:
    if not value:
        return None
    items = value if isinstance(value, list) else value.split(",")
    pages = [item.strip() for item in items if item and item.strip()]
    return pages or None


def _reason_key(category: str, reason: Any) -> str:
    reason = str(reason or "").strip()[:80]
    return f"{category}:{reason}" if reason else category


def _round(value: float | None, digits: int = 6) -> float | None:
    return round(value, digits) if isinstance(value, (int, float)) else None


# ---------------------------------------------------------------------------------------------
# Per-run metrics from the database


def _window_runs(
    conn: Any,
    *,
    limit: int,
    since: datetime | None,
    run_ids: list[int] | None,
) -> list[dict[str, Any]]:
    """The newest runs that ran the vision loop (they have STEP_TIMING), newest first."""

    sqlite = conn.dialect.name == "sqlite"
    # `+event_type` keeps SQLite on the run_id index: the event_type index would walk every
    # STEP_TIMING row in the database for each candidate run.
    type_column = "+e.event_type" if sqlite else "e.event_type"
    clauses = [f"EXISTS (SELECT 1 FROM cognitive_events e WHERE e.run_id = r.id AND {type_column} = 'STEP_TIMING')"]
    params: dict[str, Any] = {"limit": int(limit)}
    if since is not None:
        clauses.append("r.created_at >= :since")
        # SQLite keeps naive UTC text ("YYYY-MM-DD HH:MM:SS.ffffff"); compare in that form.
        utc = since.astimezone(timezone.utc).replace(tzinfo=None)
        params["since"] = utc.strftime("%Y-%m-%d %H:%M:%S.%f") if sqlite else utc
    statement_text = (
        "SELECT r.id, r.current_state, r.goal, r.target_url, r.status_message, r.created_at, r.updated_at "
        "FROM operator_runs r WHERE " + " AND ".join(clauses)
    )
    statement = None
    if run_ids is not None:
        statement_text += " AND r.id IN :run_ids"
        params["run_ids"] = [int(item) for item in run_ids] or [-1]
        statement = text(statement_text + " ORDER BY r.id DESC LIMIT :limit").bindparams(
            bindparam("run_ids", expanding=True)
        )
    else:
        statement = text(statement_text + " ORDER BY r.id DESC LIMIT :limit")
    rows = conn.execute(statement, params).mappings().all()
    return [dict(row) for row in rows]


def _run_events(conn: Any, run_ids: list[int], event_types: tuple[str, ...]) -> list[dict[str, Any]]:
    if not run_ids:
        return []
    type_column = "+event_type" if conn.dialect.name == "sqlite" else "event_type"
    statement = text(
        "SELECT id, run_id, event_type, event_payload, timestamp FROM cognitive_events "
        f"WHERE run_id IN :run_ids AND {type_column} IN :types ORDER BY run_id, id"
    ).bindparams(bindparam("run_ids", expanding=True), bindparam("types", expanding=True))
    rows = conn.execute(statement, {"run_ids": run_ids, "types": list(event_types)}).mappings().all()
    return [dict(row) for row in rows]


def _first_event_times(conn: Any, run_ids: list[int]) -> dict[int, datetime]:
    if not run_ids:
        return {}
    statement = text(
        "SELECT run_id, MIN(timestamp) AS first_at FROM cognitive_events WHERE run_id IN :run_ids GROUP BY run_id"
    ).bindparams(bindparam("run_ids", expanding=True))
    found: dict[int, datetime] = {}
    for row in conn.execute(statement, {"run_ids": run_ids}).mappings().all():
        parsed = _as_datetime(row["first_at"])
        if parsed is not None:
            found[int(row["run_id"])] = parsed
    return found


def _cache_rate(cached: Any, prompt: Any) -> float | None:
    """Share of input tokens served from the provider's cache; None when no input was counted."""
    return round(float(cached or 0) / float(prompt), 4) if prompt and float(prompt) > 0 else None


def _empty_run_metrics(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": int(run["id"]),
        "state": run.get("current_state"),
        "reason": None,
        "state_change_reason": None,
        "loop_end_outcome": None,
        "page": None,
        "page_source": None,
        "bench": None,
        "goal": str(run.get("goal") or "")[:160],
        "target_url": run.get("target_url"),
        "created_at": _iso(_as_datetime(run.get("created_at"))),
        "steps": 0,
        "executed_actions": 0,
        "loops": 0,
        "wall_seconds": None,
        "time_seconds": {phase: 0.0 for phase in PHASES},
        "time_source": None,
        "waste_seconds": {},
        "decision_latency_seconds": None,
        "cost_usd": {"brain": None, "judges": None, "total": None},
        "cost_source": None,
        "model_calls": {},
        "cache_hit_rate": None,
        "brain_cache_hit_rate": None,
        "decisions_by_model": {},
        "counts": {
            "refusals": 0,
            "sequence_invalid": 0,
            "invalid_outputs": 0,
            "length_capped": 0,
            # Brain replies cut off at the token cap, and how many the in-step retry recovered.
            "cut_off_replies": 0,
            "cut_off_recovered": 0,
            "no_progress": 0,
            "escalations": 0,
            "deescalations": 0,
        },
        "step_outcomes": {},
        "refusal_reasons": {},
        "model_failures": {},
        "escalation_reasons": {},
    }


def _fold_events(metrics: dict[str, Any], events: list[dict[str, Any]]) -> None:
    step_outcomes: Counter[str] = Counter()
    refusals: Counter[str] = Counter()
    model_failures: Counter[str] = Counter()
    escalations: Counter[str] = Counter()
    counts = metrics["counts"]
    summaries = 0
    summary_times = {phase: 0.0 for phase in PHASES}
    step_times = {phase: 0.0 for phase in PHASES}
    waste: Counter[str] = Counter()
    summary_steps = summary_actions = 0
    step_count = step_actions = 0
    by_purpose: dict[str, dict[str, Any]] = {}
    end_at: datetime | None = None
    last_outcome = None
    for event in events:
        kind = event["event_type"]
        payload = _payload(event.get("event_payload"))
        at = _as_datetime(event.get("timestamp"))
        if kind == "STEP_TIMING":
            step_count += 1
            step_actions += int(payload.get("executed_subactions") or 0)
            for phase in PHASES:
                step_times[phase] += float(payload.get(f"{phase}_ms") or 0) / 1000.0
            outcome = str(payload.get("outcome") or "unknown")
            last_outcome = outcome
            if outcome == "sequence_invalid":
                counts["sequence_invalid"] += 1
                counts["refusals"] += 1
                step_outcomes[_reason_key("sequence_invalid", payload.get("rejection_reason"))] += 1
            elif outcome not in _SUCCESS_STEP_OUTCOMES:
                step_outcomes[outcome] += 1
                if outcome == "executed_without_durable_progress":
                    counts["no_progress"] += 1
                elif outcome == "empty_length":
                    counts["length_capped"] += 1
            if end_at is None or (at and at > end_at):
                end_at = at
        elif kind == "RUN_TIMING_SUMMARY":
            # One summary per loop of the run (a run resumed after a question has several).
            summaries += 1
            totals = payload.get("totals_ms") or {}
            for phase in PHASES:
                summary_times[phase] += float(totals.get(f"{phase}_ms") or 0) / 1000.0
            for key, value in (payload.get("waste_seconds") or {}).items():
                if isinstance(value, (int, float)):
                    waste[key] += float(value)
            summary_steps += int(payload.get("steps") or 0)
            summary_actions += int(payload.get("executed_actions") or 0)
            if isinstance(payload.get("decision_latency_seconds"), dict):
                metrics["decision_latency_seconds"] = payload["decision_latency_seconds"]
            if at and (end_at is None or at > end_at):
                end_at = at
        elif kind == "RUN_MODEL_CALLS":
            for purpose, tally in (payload.get("by_purpose") or {}).items():
                if not isinstance(tally, dict):
                    continue
                merged = by_purpose.setdefault(
                    purpose,
                    {
                        "calls": 0, "failed": 0, "latency_ms": 0, "prompt_tokens": 0, "completion_tokens": 0,
                        "cached_prompt_tokens": 0, "cache_write_tokens": 0, "reasoning_tokens": 0, "cost_usd": 0.0,
                    },
                )
                for key in ("calls", "failed", "latency_ms", "prompt_tokens", "completion_tokens", "cached_prompt_tokens", "cache_write_tokens", "reasoning_tokens"):
                    merged[key] += int(tally.get(key) or 0)
                cost = tally.get("cost_usd")
                merged["cost_usd"] = (
                    merged["cost_usd"] + float(cost)
                    if merged["cost_usd"] is not None and isinstance(cost, (int, float))
                    else None
                )
        elif kind == "RUN_STATE_CHANGED":
            metrics["state_change_reason"] = payload.get("reason")
        elif kind == "EVAL_CANDIDATE_RECORDED":
            metrics["reason"] = payload.get("reason") or metrics["reason"]
        elif kind in _REFUSAL_EVENTS:
            category, key = _REFUSAL_EVENTS[kind]
            refusals[_reason_key(category, payload.get(key) if key else None)] += 1
            counts["refusals"] += 1
        elif kind in _MODEL_FAILURE_EVENTS:
            category, key = _MODEL_FAILURE_EVENTS[kind]
            reason = payload.get(key) if key else None
            record = payload.get("attempt_record") if isinstance(payload.get("attempt_record"), dict) else {}
            if key and not reason:
                reason = record.get(key)
            model_failures[_reason_key(category, reason)] += 1
            if category in {"schema_invalid", "empty_completion"}:
                counts["invalid_outputs"] += 1
                if record.get("finish_reason") == "length":
                    counts["length_capped"] += 1
        elif kind == "VISION_MODEL_REPLY_CUT_OFF":
            counts["cut_off_replies"] += 1
            retry_outcome = str(payload.get("retry_outcome") or "none")
            if retry_outcome == "recovered":
                counts["cut_off_recovered"] += 1
            model_failures[_reason_key("reply_cut_off", retry_outcome)] += 1
        elif kind == "VISION_BRAIN_ESCALATED":
            counts["escalations"] += 1
            escalations[str(payload.get("reason") or "unknown")] += 1
        elif kind == "VISION_BRAIN_DEESCALATED":
            counts["deescalations"] += 1
    metrics["loops"] = summaries
    if summaries:
        metrics["time_seconds"] = {phase: round(value, 3) for phase, value in summary_times.items()}
        metrics["time_source"] = "run_timing_summary"
        metrics["steps"] = summary_steps
        metrics["executed_actions"] = summary_actions
    elif step_count:
        # A loop still running, or one that died before its summary: the steps it recorded.
        metrics["time_seconds"] = {phase: round(value, 3) for phase, value in step_times.items()}
        metrics["time_source"] = "step_timing"
        metrics["steps"] = step_count
        metrics["executed_actions"] = step_actions
    metrics["waste_seconds"] = {key: round(value, 3) for key, value in waste.items()}
    metrics["loop_end_outcome"] = last_outcome
    if not metrics["reason"]:
        # The loop's last step outcome names why it ended, unless that step succeeded and the run
        # still did not finish: then it was stopped from outside (the state change says by what).
        stopped_outside = last_outcome in _SUCCESS_STEP_OUTCOMES and metrics["state"] not in _FINISHED_STATES
        metrics["reason"] = (metrics["state_change_reason"] or last_outcome) if stopped_outside else last_outcome
    metrics["step_outcomes"] = dict(step_outcomes.most_common())
    metrics["refusal_reasons"] = dict(refusals.most_common())
    metrics["model_failures"] = dict(model_failures.most_common())
    metrics["escalation_reasons"] = dict(escalations.most_common())
    metrics["_end_at"] = end_at
    if by_purpose:
        for tally in by_purpose.values():
            tally["cost_usd"] = _round(tally["cost_usd"])
        for tally in by_purpose.values():
            tally["cache_hit_rate"] = _cache_rate(tally["cached_prompt_tokens"], tally["prompt_tokens"])
        metrics["model_calls"] = by_purpose
        brain_calls = [t for purpose, t in by_purpose.items() if purpose.startswith("vision_agent_step")]
        metrics["cache_hit_rate"] = _cache_rate(
            sum(t["cached_prompt_tokens"] for t in by_purpose.values()), sum(t["prompt_tokens"] for t in by_purpose.values())
        )
        metrics["brain_cache_hit_rate"] = _cache_rate(
            sum(t["cached_prompt_tokens"] for t in brain_calls), sum(t["prompt_tokens"] for t in brain_calls)
        )
        brain = [tally["cost_usd"] for purpose, tally in by_purpose.items() if purpose.startswith("vision_agent_step")]
        judges = [tally["cost_usd"] for purpose, tally in by_purpose.items() if not purpose.startswith("vision_agent_step")]
        brain_cost = sum(brain) if brain and all(value is not None for value in brain) else (0.0 if not brain else None)
        judge_cost = sum(judges) if all(value is not None for value in judges) else None
        metrics["cost_usd"] = {
            "brain": _round(brain_cost),
            "judges": _round(judge_cost),
            "total": _round(brain_cost + judge_cost) if brain_cost is not None and judge_cost is not None else None,
        }
        metrics["cost_source"] = "run_model_calls"


def _fold_transport_receipts(conn: Any, metrics_by_run: dict[int, dict[str, Any]]) -> None:
    """Decisions per model for every run, and the brain's cost for runs without RUN_MODEL_CALLS
    (older runs, or a loop that has not ended): one receipt per decision call."""

    run_ids = list(metrics_by_run)
    if not run_ids:
        return
    sqlite = conn.dialect.name == "sqlite"
    if sqlite:
        # Receipts carry large input references; read only the two fields used here.
        statement = text(
            "SELECT run_id, json_extract(event_payload, '$.model') AS model, "
            "json_extract(event_payload, '$.cost_usd') AS cost FROM cognitive_events "
            "WHERE run_id IN :run_ids AND +event_type = 'VISION_MODEL_TRANSPORT_RECEIPT'"
        ).bindparams(bindparam("run_ids", expanding=True))
        rows = [(row["run_id"], row["model"], row["cost"]) for row in conn.execute(statement, {"run_ids": run_ids}).mappings()]
    else:
        statement = text(
            "SELECT run_id, event_payload FROM cognitive_events "
            "WHERE run_id IN :run_ids AND event_type = 'VISION_MODEL_TRANSPORT_RECEIPT'"
        ).bindparams(bindparam("run_ids", expanding=True))
        rows = []
        for row in conn.execute(statement, {"run_ids": run_ids}).mappings():
            payload = _payload(row["event_payload"])
            rows.append((row["run_id"], payload.get("model"), payload.get("cost_usd")))
    costs: dict[int, list[Any]] = {}
    for run_id, model, cost in rows:
        metrics = metrics_by_run[int(run_id)]
        name = str(model or "unknown")
        metrics["decisions_by_model"][name] = metrics["decisions_by_model"].get(name, 0) + 1
        costs.setdefault(int(run_id), []).append(cost)
    for run_id, values in costs.items():
        metrics = metrics_by_run[run_id]
        if metrics["cost_source"] is None:
            known = all(isinstance(value, (int, float)) for value in values)
            brain = _round(sum(float(value) for value in values)) if known else None
            metrics["cost_usd"] = {"brain": brain, "judges": None, "total": None}
            metrics["cost_source"] = "transport_receipts"


def _run_page(run: dict[str, Any], page_index: dict[int, dict[str, Any]]) -> tuple[str | None, str | None]:
    entry = page_index.get(int(run["id"]))
    if entry:
        return entry["page"], "gauntlet_scorecard"
    match = _BENCH_PAGE_IN_URL.search(str(run.get("target_url") or ""))
    if match:
        return match.group(1), "target_url"
    return None, None


def collect_run_metrics(
    conn: Any,
    *,
    limit: int = DEFAULT_RUN_LIMIT,
    since: datetime | None = None,
    run_ids: list[int] | None = None,
    page_index: dict[int, dict[str, Any]] | None = None,
    pages: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Per-run metrics for the newest `limit` runs that ran the vision loop, newest first.

    `conn` is a SQLAlchemy Connection or Session; only SELECTs are issued. `page_index` maps a run id
    to its gauntlet scorecard attempt (see `gauntlet_attempts`); with `pages`, only runs of those pages
    count toward the window (run headers are scanned up to MAX_RUN_LIMIT to find them)."""

    limit = max(1, min(int(limit or DEFAULT_RUN_LIMIT), MAX_RUN_LIMIT))
    page_index = page_index or {}
    runs = _window_runs(conn, limit=MAX_RUN_LIMIT if pages else limit, since=since, run_ids=run_ids)
    located = [(run, *_run_page(run, page_index)) for run in runs]
    if pages:
        located = [item for item in located if item[1] in pages][:limit]
    if not located:
        return []
    metrics_by_run = {int(run["id"]): _empty_run_metrics(run) for run, _page, _source in located}
    ids = list(metrics_by_run)
    events_by_run: dict[int, list[dict[str, Any]]] = {}
    for event in _run_events(conn, ids, _EVENT_TYPES):
        events_by_run.setdefault(int(event["run_id"]), []).append(event)
    for run_id, events in events_by_run.items():
        _fold_events(metrics_by_run[run_id], events)
    _fold_transport_receipts(conn, metrics_by_run)
    first_at = _first_event_times(conn, ids)
    for run, page, source in located:
        metrics = metrics_by_run[int(run["id"])]
        end_at = metrics.pop("_end_at", None)
        start_at = first_at.get(metrics["run_id"])
        if start_at and end_at and end_at >= start_at:
            metrics["wall_seconds"] = round((end_at - start_at).total_seconds(), 1)
        metrics["page"], metrics["page_source"] = page, source
        entry = page_index.get(metrics["run_id"]) if source == "gauntlet_scorecard" else None
        if entry:
            metrics["bench"] = {
                "passed": entry.get("passed"),
                "completed": entry.get("completed"),
                "total": entry.get("total"),
                "scorecard": entry.get("dir"),
                "commit": entry.get("commit"),
            }
    return [metrics_by_run[int(run["id"])] for run, _page, _source in located]


# ---------------------------------------------------------------------------------------------
# Gauntlet and OSWorld history from eval_runs


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def gauntlet_attempts(eval_runs_dir: Path, benchmark: str = GAUNTLET_BENCHMARK) -> list[dict[str, Any]]:
    """Every page attempt recorded in a scorecard of `benchmark` (the gauntlet by default), oldest first. `live_run --out` names the
    directory freely, so a scorecard is recognized by its `benchmark` field, not its directory name."""

    attempts: list[dict[str, Any]] = []
    if not eval_runs_dir.is_dir():
        return attempts
    for scorecard_path in eval_runs_dir.glob("*/scorecard.json"):
        scorecard = _read_json(scorecard_path)
        if not scorecard or scorecard.get("benchmark") != benchmark:
            continue
        provenance = scorecard.get("provenance") if isinstance(scorecard.get("provenance"), dict) else {}
        recorded = _as_datetime(scorecard.get("recorded_at"))
        if recorded is None:
            try:
                recorded = datetime.fromtimestamp(scorecard_path.stat().st_mtime, tz=timezone.utc)
            except OSError:
                recorded = None
        roles = provenance.get("model_roles") if isinstance(provenance.get("model_roles"), dict) else {}
        # The Brain seat; scorecards written before the three seats named it "vision".
        vision = roles.get("brain") or roles.get("vision") or {}
        for page, entry in (scorecard.get("by_page") or {}).items():
            if not isinstance(entry, dict):
                continue
            usage = entry.get("model_usage") if isinstance(entry.get("model_usage"), dict) else {}
            cost_parts = [usage.get("decision_cost_usd"), usage.get("hedge_extra_cost_usd"), usage.get("judge_cost_usd")]
            cost = (
                _round(sum(float(part) for part in cost_parts if part is not None))
                if usage and usage.get("decision_cost_usd") is not None and usage.get("judge_cost_usd") is not None
                else None
            )
            attempts.append(
                {
                    "benchmark": benchmark,
                    "page": page.split("@", 1)[0],  # live_run --repeat tags runs page@rN: one page's attempts
                    "run_tag": page.split("@", 1)[1] if "@" in page else None,
                    "dir": scorecard_path.parent.name,
                    "recorded_at": _iso(recorded),
                    "commit": provenance.get("commit"),
                    "uncommitted_changes": provenance.get("uncommitted_changes"),
                    "brain_model": vision.get("model_name"),
                    "passed": bool(entry.get("passed")),
                    "completed": entry.get("completed"),
                    "total": entry.get("total"),
                    "final_state": entry.get("final_state"),
                    "fail_reason": entry.get("fail_reason"),
                    "run_id": entry.get("run_id"),
                    "elapsed_s": entry.get("elapsed_s"),
                    "cost_usd": cost,
                }
            )
    attempts.sort(key=lambda item: (item["recorded_at"] or "", item["dir"]))
    return attempts


def _attempt_ratio(attempt: dict[str, Any]) -> float:
    total = attempt.get("total") or 0
    return float(attempt.get("completed") or 0) / float(total) if total else 0.0


def _attempt_brief(attempt: dict[str, Any] | None) -> dict[str, Any] | None:
    if not attempt:
        return None
    keys = ("passed", "completed", "total", "recorded_at", "commit", "run_id", "dir", "final_state", "fail_reason", "cost_usd")
    return {key: attempt.get(key) for key in keys}


def gauntlet_history(
    attempts: list[dict[str, Any]],
    *,
    pages: list[str] | None = None,
    since: datetime | None = None,
    trend_attempts: int = DEFAULT_TREND_ATTEMPTS,
) -> dict[str, Any]:
    """Per page: the recent pass/fail trend, best and latest attempt, the newest commit that passed,
    and pages whose latest attempt fails although an earlier attempt passed."""

    by_page: dict[str, list[dict[str, Any]]] = {}
    for attempt in attempts:
        if pages and attempt["page"] not in pages:
            continue
        if since and (_as_datetime(attempt["recorded_at"]) or since) < since:
            continue
        by_page.setdefault(attempt["page"], []).append(attempt)
    trend_attempts = max(1, int(trend_attempts or DEFAULT_TREND_ATTEMPTS))
    result_pages: dict[str, Any] = {}
    regressions: list[dict[str, Any]] = []
    for page in sorted(by_page):
        history = by_page[page]
        recent = history[-trend_attempts:]
        latest = history[-1]
        best = max(enumerate(history), key=lambda pair: (pair[1]["passed"], _attempt_ratio(pair[1]), pair[0]))[1]
        passes = [attempt for attempt in history if attempt["passed"]]
        last_pass = passes[-1] if passes else None
        last_pass_commit = next((attempt["commit"] for attempt in reversed(passes) if attempt.get("commit")), None)
        result_pages[page] = {
            "attempts": len(history),
            "passes": len(passes),
            "trend": "".join("P" if attempt["passed"] else "F" for attempt in recent),
            "recent_passes": sum(1 for attempt in recent if attempt["passed"]),
            "recent_attempts": len(recent),
            "best": _attempt_brief(best),
            "latest": _attempt_brief(latest),
            "last_pass": _attempt_brief(last_pass),
            "last_pass_commit": last_pass_commit,
        }
        if not latest["passed"] and last_pass is not None:
            same_commit = bool(latest.get("commit")) and latest.get("commit") == last_pass.get("commit")
            regressions.append(
                {
                    "page": page,
                    # Passed at this very commit before: the page is flaky rather than broken by a change.
                    "kind": "flaky" if same_commit else "regression",
                    "last_pass": _attempt_brief(last_pass),
                    "latest": _attempt_brief(latest),
                }
            )
    return {"pages": result_pages, "regressions": regressions}


def osworld_history(eval_runs_dir: Path, *, since: datetime | None = None) -> list[dict[str, Any]]:
    """One row per OSWorld results directory with a summary.json, oldest first."""

    rows: list[dict[str, Any]] = []
    if not eval_runs_dir.is_dir():
        return rows
    for summary_path in eval_runs_dir.glob("osworld-*/summary.json"):
        summary = _read_json(summary_path)
        if not summary:
            continue
        try:
            written = datetime.fromtimestamp(summary_path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if since and written < since:
            continue
        reasons = summary.get("failure_reasons") if isinstance(summary.get("failure_reasons"), dict) else {}
        rows.append(
            {
                "name": summary_path.parent.name,
                "written_at": written.isoformat(),
                "tasks": summary.get("tasks"),
                "score_sum": summary.get("score_sum"),
                "success_rate": summary.get("success_rate"),
                "env_errors": summary.get("env_errors"),
                "top_failure_reasons": dict(Counter(reasons).most_common(5)),
            }
        )
    rows.sort(key=lambda row: row["written_at"])
    return rows


# ---------------------------------------------------------------------------------------------
# The report


def _window_totals(runs: list[dict[str, Any]]) -> dict[str, Any]:
    phases: Counter[str] = Counter()
    waste: Counter[str] = Counter()
    purposes: dict[str, dict[str, Any]] = {}
    step_outcomes: Counter[str] = Counter()
    refusals: Counter[str] = Counter()
    model_failures: Counter[str] = Counter()
    escalations: Counter[str] = Counter()
    states: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    cost_total = 0.0
    cost_runs = 0
    cached_tokens = 0
    prompt_tokens = 0
    for run in runs:
        states[str(run["state"])] += 1
        if run.get("reason"):
            reasons[f"{run['state']}:{run['reason']}"] += 1
        for phase, seconds in run["time_seconds"].items():
            phases[phase] += float(seconds or 0)
        for key, seconds in run["waste_seconds"].items():
            waste[key] += float(seconds or 0)
        for purpose, tally in run["model_calls"].items():
            merged = purposes.setdefault(purpose, {"calls": 0, "latency_seconds": 0.0, "cost_usd": 0.0})
            merged["calls"] += int(tally.get("calls") or 0)
            merged["latency_seconds"] += float(tally.get("latency_ms") or 0) / 1000.0
            merged["cost_usd"] += float(tally.get("cost_usd") or 0)
            cached_tokens += int(tally.get("cached_prompt_tokens") or 0)
            prompt_tokens += int(tally.get("prompt_tokens") or 0)
        step_outcomes.update(run["step_outcomes"])
        refusals.update(run["refusal_reasons"])
        model_failures.update(run["model_failures"])
        escalations.update(run["escalation_reasons"])
        total = run["cost_usd"].get("total")
        if total is None:
            total = run["cost_usd"].get("brain")
        if isinstance(total, (int, float)):
            cost_total += float(total)
            cost_runs += 1
    phase_total = sum(phases.values()) or 0.0
    return {
        "runs": len(runs),
        "states": dict(states.most_common()),
        "end_reasons": dict(reasons.most_common(15)),
        "cost_usd": round(cost_total, 6),
        "runs_with_cost": cost_runs,
        "cache_hit_rate": _cache_rate(cached_tokens, prompt_tokens),
        "time_sinks": [
            {
                "phase": phase,
                "seconds": round(seconds, 1),
                "share": round(seconds / phase_total, 3) if phase_total else 0.0,
            }
            for phase, seconds in phases.most_common()
        ],
        "waste_seconds": {key: round(value, 1) for key, value in waste.most_common()},
        "model_calls_by_latency": [
            {
                "purpose": purpose,
                "calls": tally["calls"],
                "latency_seconds": round(tally["latency_seconds"], 1),
                "cost_usd": round(tally["cost_usd"], 6),
            }
            for purpose, tally in sorted(purposes.items(), key=lambda pair: -pair[1]["latency_seconds"])
        ][:12],
        "step_outcomes": dict(step_outcomes.most_common(15)),
        "refusal_reasons": dict(refusals.most_common(15)),
        "model_failures": dict(model_failures.most_common(10)),
        "escalation_reasons": dict(escalations.most_common()),
    }


def build_report(
    conn: Any,
    eval_runs_dir: Path,
    *,
    limit: int = DEFAULT_RUN_LIMIT,
    since: datetime | None = None,
    pages: list[str] | None = None,
    trend_attempts: int = DEFAULT_TREND_ATTEMPTS,
    run_ids: list[int] | None = None,
) -> dict[str, Any]:
    """The whole metrics report as plain JSON-ready data. Read-only."""

    attempts = gauntlet_attempts(eval_runs_dir)
    mac_attempts = gauntlet_attempts(eval_runs_dir, MAC_BENCHMARK)
    page_index: dict[int, dict[str, Any]] = {}
    for attempt in [*attempts, *mac_attempts]:
        if isinstance(attempt.get("run_id"), int):
            page_index[attempt["run_id"]] = attempt
    runs = collect_run_metrics(conn, limit=limit, since=since, run_ids=run_ids, page_index=page_index, pages=pages)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": {
            "limit": limit,
            "since": _iso(since),
            "pages": pages,
            "trend_attempts": trend_attempts,
        },
        "runs": runs,
        "totals": _window_totals(runs),
        "gauntlet": gauntlet_history(attempts, pages=pages, since=since, trend_attempts=trend_attempts),
        "mac_bench": gauntlet_history(mac_attempts, pages=pages, since=since, trend_attempts=trend_attempts),
        "osworld": [] if pages else osworld_history(eval_runs_dir, since=since),
    }
