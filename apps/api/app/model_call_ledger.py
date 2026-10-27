"""Per-run tally of every structured model call, by purpose: count, failures, latency, tokens, cost.

The run loop binds its run id to a context variable; worker threads started with asyncio.to_thread
inherit it, so the decision call and every judge it triggers land in the same run's tally. Nothing
here changes a call or its result. A cost stays None for a purpose when any of its calls came back
without a cost figure, rather than undercounting; the known part of the cost and the count of calls
without a figure are kept beside it, so a cost cap can still act on a lower bound.
"""

from __future__ import annotations

import contextvars
import logging
import threading
from typing import Any

from app.model_usage import normalize_usage

_log = logging.getLogger(__name__)
_current_run: contextvars.ContextVar[int | None] = contextvars.ContextVar("lumen_model_call_run", default=None)
_lock = threading.Lock()
_ledgers: dict[int, dict[str, dict[str, Any]]] = {}
# Spend not yet added to the run's row (operator_runs.cost_estimate), flushed in small batches so a
# run's cost is visible while it runs and survives a restart that never reaches release().
_unflushed: dict[int, float] = {}
_last_flush: dict[int, float] = {}
FLUSH_MIN_USD = 0.002
FLUSH_MAX_SECONDS = 15.0


def bind(run_id: int) -> contextvars.Token:
    """One loop per run at a time: drive_in_browser_application holds a per-run guard until the previous
    drive (this loop's release included) has returned, so the ledger is never shared by two loops. A second
    bind while one is live would mix counts and let the first release delete the second's tally: log it."""
    with _lock:
        if int(run_id) in _ledgers:
            _log.warning("model-call ledger for run %s bound while a previous loop's ledger is still live", run_id)
        _ledgers.setdefault(int(run_id), {})
    return _current_run.set(int(run_id))


def release(token: contextvars.Token, run_id: int) -> dict[str, dict[str, Any]]:
    """Unbind and return the run's tally (this loop session's calls)."""
    _current_run.reset(token)
    _flush_cost(int(run_id), force=True)
    with _lock:
        if not _unflushed.get(int(run_id)):
            _unflushed.pop(int(run_id), None)
            _last_flush.pop(int(run_id), None)
        return _ledgers.pop(int(run_id), {})


def current_run() -> int | None:
    return _current_run.get()


def record(purpose: str, result: Any) -> None:
    record_for(_current_run.get(), purpose, result)


def record_for(run_id: int | None, purpose: str, result: Any) -> None:
    """Tally a call on an explicit run (a worker outside the run's context, e.g. a hedge's loser)."""
    if run_id is None:
        return
    result = result if isinstance(result, dict) else {}
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    usage = metadata.get("token_usage") if isinstance(metadata.get("token_usage"), dict) else {}
    usage = normalize_usage(None, usage, metadata.get("model"))  # a no-op on a normalized block
    cost = usage.get("cost")
    with _lock:
        ledger = _ledgers.get(run_id)
        if ledger is None:
            return  # the run's loop already ended and reported its tally
        entry = ledger.setdefault(
            str(purpose or "unknown"),
            {"calls": 0, "failed": 0, "latency_ms": 0, "prompt_tokens": 0, "completion_tokens": 0, "cached_prompt_tokens": 0, "cache_write_tokens": 0, "reasoning_tokens": 0, "cost_usd": 0.0, "cost_known_usd": 0.0, "cost_unknown_calls": 0, "models": []},
        )
        entry["calls"] += 1
        entry["failed"] += int(not result.get("ok"))
        entry["latency_ms"] += int(metadata.get("latency_ms") or 0)
        entry["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        entry["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        entry["cached_prompt_tokens"] += int(usage.get("cached_prompt_tokens") or 0)
        entry["cache_write_tokens"] += int(usage.get("cache_write_tokens") or 0)
        entry["reasoning_tokens"] += int(usage.get("reasoning_tokens") or 0)
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            entry["cost_known_usd"] = round(entry["cost_known_usd"] + float(cost), 8)
            if entry["cost_usd"] is not None:
                entry["cost_usd"] = round(entry["cost_usd"] + float(cost), 8)
        else:
            entry["cost_unknown_calls"] += 1
            entry["cost_usd"] = None
        model = str(metadata.get("model") or "")
        if model and model not in entry["models"]:
            entry["models"].append(model)
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) and cost > 0:
            _unflushed[run_id] = _unflushed.get(run_id, 0.0) + float(cost)
    _flush_cost(run_id)


def _flush_cost(run_id: int, *, force: bool = False) -> None:
    """Add the run's unflushed spend to its row. Never raises: cost bookkeeping must not fail a call."""
    import time

    now = time.monotonic()
    with _lock:
        pending = _unflushed.get(run_id, 0.0)
        due = pending > 0 and (
            force or pending >= FLUSH_MIN_USD or now - _last_flush.get(run_id, 0.0) >= FLUSH_MAX_SECONDS
        )
        if not due:
            return
        _unflushed[run_id] = 0.0
        _last_flush[run_id] = now
    try:
        from sqlalchemy import update

        from app import models
        from app.db import SessionLocal

        with SessionLocal() as db:
            db.execute(
                update(models.OperatorRun)
                .where(models.OperatorRun.id == run_id)
                .values(cost_estimate=_coalesce_add(models.OperatorRun.cost_estimate, round(pending, 8)))
            )
            db.commit()
    except Exception:  # noqa: BLE001 - keep the spend for the next flush instead of losing it
        with _lock:
            _unflushed[run_id] = _unflushed.get(run_id, 0.0) + pending


def _coalesce_add(column: Any, amount: float) -> Any:
    from sqlalchemy import func

    return func.coalesce(column, 0.0) + amount


def totals(run_id: int | None) -> dict[str, Any]:
    """The run's tokens and cost so far, across every purpose. `cost_usd` is None when any call lacked a
    cost figure (a total that undercounts is no total); `cost_known_usd` is the sum of the figures there
    are and `cost_unknown_calls` counts the calls without one, so the real cost is at least the known part."""
    with _lock:
        ledger = _ledgers.get(int(run_id)) if run_id is not None else None
        entries = [dict(e) for e in (ledger or {}).values()]
    known = round(sum(float(e["cost_known_usd"]) for e in entries), 8)
    unknown = sum(int(e["cost_unknown_calls"]) for e in entries)
    return {
        "calls": sum(int(e["calls"]) for e in entries),
        "tokens": sum(int(e["prompt_tokens"]) + int(e["completion_tokens"]) for e in entries),
        "prompt_tokens": sum(int(e["prompt_tokens"]) for e in entries),
        "completion_tokens": sum(int(e["completion_tokens"]) for e in entries),
        "cached_prompt_tokens": sum(int(e["cached_prompt_tokens"]) for e in entries),
        "cache_write_tokens": sum(int(e["cache_write_tokens"]) for e in entries),
        "reasoning_tokens": sum(int(e["reasoning_tokens"]) for e in entries),
        "cost_usd": None if unknown else known,
        "cost_known_usd": known,
        "cost_unknown_calls": unknown,
    }
