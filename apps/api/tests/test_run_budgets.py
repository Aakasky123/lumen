"""Run budgets: an overall step ceiling across answered asks, a wall-clock budget that excludes time
parked on the user, and token / cost caps. Each ends the run BLOCKED with a distinct reason and says so."""

from __future__ import annotations

import asyncio
import threading
import time

from app import extension_run, model_call_ledger, models
from app.agent_settings import get_agent_settings, update_agent_settings
from app.db import SessionLocal, init_db
from app.extension_run import run_vision_loop
from tests.test_ask_user_park import (
    _ParkBody,
    _drive,
    _install,
    _make_run,
    _pending_question_id,
    _state,
)
from tests.test_extension_run import _DummyContextEngine, _VisionBody

_NO_LIMITS = {"seconds": None, "tokens": None, "cost_usd": None}


def _waits():
    counter = {"n": 0}

    def next_wait() -> dict:
        counter["n"] += 1
        return {"action": "wait", "wait_ms": 100 + counter["n"], "reason": f"Look again ({counter['n']})."}

    return next_wait


def _run_plain(monkeypatch, *, limits: dict, max_steps: int, on_decide=None):
    """A loop on the plain fake body whose every decision is a wait; `on_decide` runs per decision."""
    init_db()
    next_wait = _waits()
    decided: list[int] = []
    events: list[tuple[str, dict]] = []
    stops: list[dict] = []

    def fake_decide(user_id, image, view, context):
        decided.append(1)
        if on_decide:
            on_decide()
        return next_wait()

    async def narrate(kind: str, data: dict) -> None:
        if kind == "stop":
            stops.append(data)

    monkeypatch.setattr("app.candidate_context.CandidateContextEngine", _DummyContextEngine)
    monkeypatch.setattr(extension_run, "_db_event", lambda run_id, kind, payload=None: events.append((kind, dict(payload or {}))))
    monkeypatch.setattr(extension_run, "_record_context_usage_for_run", lambda *a, **k: None)
    monkeypatch.setattr(extension_run, "_decide_vision_action", fake_decide)
    monkeypatch.setattr(extension_run, "_run_budget_limits", lambda user_id: dict(limits))
    summary = asyncio.run(
        run_vision_loop(_VisionBody(), url=None, narrate=narrate, run_id=0, user_id=0, skip_navigate=True, max_steps=max_steps)
    )
    return summary, len(decided), events, stops


def _usage(prompt: int, completion: int, cost) -> dict:
    return {"ok": True, "metadata": {"model": "m", "token_usage": {"prompt_tokens": prompt, "completion_tokens": completion, "cost": cost}}}


# --- defaults and settings -----------------------------------------------------------------------


def test_defaults_are_generous_and_cost_is_off(monkeypatch) -> None:
    monkeypatch.setattr(extension_run, "get_agent_settings", lambda db, user_id: {})
    limits = extension_run._run_budget_limits(0)
    assert limits["seconds"] == extension_run._VISION_DEFAULT_MAX_RUN_SECONDS >= 3 * 3600
    assert limits["tokens"] == extension_run._VISION_DEFAULT_MAX_RUN_TOKENS >= 30_000_000
    assert limits["cost_usd"] is None
    assert extension_run._VISION_TOTAL_STEPS_MULTIPLIER >= 3


def test_settings_override_defaults_and_zero_means_unlimited(monkeypatch) -> None:
    monkeypatch.setattr(
        extension_run, "get_agent_settings", lambda db, user_id: {"max_run_seconds": 90, "max_run_tokens": 0, "max_run_cost_usd": 1.5}
    )
    assert extension_run._run_budget_limits(0) == {"seconds": 90.0, "tokens": None, "cost_usd": 1.5}


def test_agent_settings_store_valid_budgets_and_clear_on_null() -> None:
    init_db()
    with SessionLocal() as db:
        user = models.User(email=f"budget-{time.time_ns()}@example.com", name="B")
        db.add(user)
        db.flush()
        assert get_agent_settings(db, user.id)["max_run_seconds"] is None
        merged = update_agent_settings(db, user.id, {"max_run_seconds": 600, "max_run_tokens": 5000, "max_run_cost_usd": 2.5})
        assert (merged["max_run_seconds"], merged["max_run_tokens"], merged["max_run_cost_usd"]) == (600, 5000, 2.5)
        merged = update_agent_settings(db, user.id, {"max_run_seconds": -5, "max_run_tokens": True})
        assert (merged["max_run_seconds"], merged["max_run_tokens"]) == (600, 5000), "invalid values are ignored"
        merged = update_agent_settings(db, user.id, {"max_run_seconds": None})
        assert merged["max_run_seconds"] is None
        db.rollback()


# --- tokens, cost, wall-clock --------------------------------------------------------------------


def test_token_cap_ends_the_run_blocked_and_says_the_budget_ran_out(monkeypatch) -> None:
    summary, decisions, events, stops = _run_plain(
        monkeypatch,
        limits={**_NO_LIMITS, "tokens": 1000.0},
        max_steps=50,
        on_decide=lambda: model_call_ledger.record("vision", _usage(300, 100, 0.01)),
    )
    assert summary["state"] == "BLOCKED" and summary["reason"] == "max_run_tokens"
    assert decisions == 3, "400 tokens per decision: the cap is reached after the third and checked before the fourth"
    assert summary["budget"]["tokens"] == 1200 and summary["budget"]["limit"] == 1000.0
    assert "model-token limit" in summary["note"] and "not known to be finished" in summary["note"]
    assert stops[-1]["reason"] == "max_run_tokens"
    assert [payload["reason"] for kind, payload in events if kind == "RUN_BUDGET_EXHAUSTED"] == ["max_run_tokens"]


def test_cost_cap_ends_the_run_and_ignores_a_cost_that_is_not_known(monkeypatch) -> None:
    summary, decisions, _events, _stops = _run_plain(
        monkeypatch,
        limits={**_NO_LIMITS, "cost_usd": 0.05},
        max_steps=50,
        on_decide=lambda: model_call_ledger.record("vision", _usage(10, 10, 0.02)),
    )
    assert summary["reason"] == "max_run_cost_usd" and decisions == 3
    assert "cost limit" in summary["note"]

    # A provider that reports no cost leaves the total unknown: the cost cap never fires on a guess.
    summary, decisions, _events, _stops = _run_plain(
        monkeypatch,
        limits={**_NO_LIMITS, "cost_usd": 0.05},
        max_steps=3,
        on_decide=lambda: model_call_ledger.record("vision", _usage(10, 10, None)),
    )
    assert summary["reason"] == "max_steps" and decisions == 3


def test_wall_clock_budget_ends_the_run(monkeypatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(extension_run, "_vision_clock", lambda: now[0])
    summary, decisions, _events, _stops = _run_plain(
        monkeypatch,
        limits={**_NO_LIMITS, "seconds": 100.0},
        max_steps=50,
        on_decide=lambda: now.__setitem__(0, now[0] + 40.0),
    )
    assert summary["state"] == "BLOCKED" and summary["reason"] == "max_run_seconds"
    assert decisions == 3, "120s of work: over the 100s budget, caught before the fourth step"
    assert summary["budget"]["elapsed_seconds"] == 120.0
    assert "time limit" in summary["note"] and "waiting for you" in summary["note"]


def test_no_limits_means_the_old_max_steps_ending(monkeypatch) -> None:
    summary, decisions, _events, _stops = _run_plain(monkeypatch, limits=_NO_LIMITS, max_steps=5)
    assert summary["reason"] == "max_steps" and decisions == 5


# --- answered asks: the overall step ceiling, and parked time ------------------------------------


def _answer_every_ask(run_id: int, advance_clock=None) -> threading.Thread:
    """Answers each question the loop parks on, until the run is over."""
    from app.cognition import OperatorChatService

    stop = threading.Event()

    def worker() -> None:
        answered: set[int] = set()
        deadline = time.monotonic() + 60
        while not stop.is_set() and time.monotonic() < deadline:
            question_id = _pending_question_id(run_id)
            if (
                question_id
                and question_id not in answered
                and run_id in extension_run._PARKED_ASKS
                and _state(run_id) == "WAITING_FOR_USER"
            ):
                if advance_clock:
                    advance_clock()
                with SessionLocal() as db:
                    OperatorChatService(db).answer_question(question_id=question_id, answer="Blue")
                answered.add(question_id)
            time.sleep(0.02)

    thread = threading.Thread(target=worker, daemon=True)
    thread.stop = stop.set  # type: ignore[attr-defined]
    thread.start()
    return thread


def _question_decisions():
    counter = {"n": 0}

    def decide(user_id, image, view, context):
        counter["n"] += 1
        n = counter["n"]
        if n % 2 == 0:
            return {"action": "ask_user", "question": f"Which colour for banner number {n}?", "options": ["Red", "Blue"], "reason": f"Colour {n} is the user's call."}
        return {"action": "wait", "wait_ms": 100 + n, "reason": f"Look again ({n})."}

    return decide, counter


def test_answered_asks_refresh_the_step_budget_but_not_past_the_overall_ceiling(monkeypatch) -> None:
    run_id, thread_id = _make_run()
    decide, counter = _question_decisions()
    events = _install(monkeypatch, decide, window=30.0)
    monkeypatch.setattr(extension_run, "_VISION_TOTAL_STEPS_MULTIPLIER", 2)  # max_steps=4 (in _drive) -> 8 overall
    monkeypatch.setattr(extension_run, "_run_budget_limits", lambda user_id: dict(_NO_LIMITS))
    answerer = _answer_every_ask(run_id)
    try:
        summary = _drive(run_id, thread_id, _ParkBody(), max_loops=[])
    finally:
        answerer.stop()  # type: ignore[attr-defined]
    assert summary["state"] == "BLOCKED" and summary["reason"] == "max_total_steps"
    assert counter["n"] == 8, "two refreshes, then the overall ceiling of 2 x max_steps stops the run"
    assert summary["budget"]["total_step_limit"] == 8
    assert "overall limit of 8 steps" in summary["note"]
    assert any(kind == "RUN_BUDGET_EXHAUSTED" for kind, _payload in events)


def test_time_parked_on_the_user_does_not_count_against_the_wall_clock(monkeypatch) -> None:
    run_id, thread_id = _make_run()
    now = [0.0]
    monkeypatch.setattr(extension_run, "_vision_clock", lambda: now[0])
    decide, counter = _question_decisions()

    def timed_decide(user_id, image, view, context):
        now[0] += 30.0
        return decide(user_id, image, view, context)

    _install(monkeypatch, timed_decide, window=30.0)
    monkeypatch.setattr(extension_run, "_run_budget_limits", lambda user_id: {**_NO_LIMITS, "seconds": 100.0})
    # While the loop is parked the user "takes" ten hours.
    answerer = _answer_every_ask(run_id, advance_clock=lambda: now.__setitem__(0, now[0] + 36000.0))
    try:
        summary = _drive(run_id, thread_id, _ParkBody(), max_loops=[])
    finally:
        answerer.stop()  # type: ignore[attr-defined]
    assert summary["reason"] == "max_run_seconds"
    assert 100.0 <= summary["budget"]["elapsed_seconds"] < 200.0, "the ten hours of waiting were not counted"
    assert counter["n"] >= 4, "the run kept going past the first answered ask"


# --- spend carried across loops of one run --------------------------------------------------------


def _resumed_journal(monkeypatch, *, steps_before: int, budget_spent: dict) -> None:
    journal = extension_run._load_run_journal_for_run(None)
    journal.update(steps_before=steps_before, budget_spent=extension_run._budget_spent_from(budget_spent))
    monkeypatch.setattr(extension_run, "_load_run_journal_for_run", lambda run_id: dict(journal))


def test_a_resumed_loop_counts_earlier_loops_tokens(monkeypatch) -> None:
    _resumed_journal(monkeypatch, steps_before=4, budget_spent={"seconds": 10, "tokens": 900, "cost_usd": 0.0})
    summary, decisions, _events, _stops = _run_plain(
        monkeypatch,
        limits={**_NO_LIMITS, "tokens": 1000.0},
        max_steps=50,
        on_decide=lambda: model_call_ledger.record("vision", _usage(300, 100, 0.01)),
    )
    assert summary["reason"] == "max_run_tokens" and decisions == 1
    assert summary["budget"]["tokens"] == 1300


def test_a_resumed_loop_gets_a_fresh_step_budget_but_not_a_fresh_ceiling(monkeypatch) -> None:
    # max_steps 5 -> ceiling 15; earlier loops took 14, so this loop may take one step.
    _resumed_journal(monkeypatch, steps_before=14, budget_spent={})
    summary, decisions, _events, _stops = _run_plain(monkeypatch, limits=_NO_LIMITS, max_steps=5)
    assert summary["reason"] == "max_total_steps" and decisions == 1


def test_an_unknown_earlier_cost_keeps_the_run_cost_unknown_but_the_known_part_still_counts(monkeypatch) -> None:
    # The old journal shape (cost_usd None) means some call had no figure: the cap acts on what is known.
    _resumed_journal(monkeypatch, steps_before=0, budget_spent={"cost_usd": None})
    summary, decisions, _events, _stops = _run_plain(
        monkeypatch,
        limits={**_NO_LIMITS, "cost_usd": 0.03},
        max_steps=10,
        on_decide=lambda: model_call_ledger.record("vision", _usage(10, 10, 0.02)),
    )
    assert summary["reason"] == "max_run_cost_usd" and decisions == 2
    assert summary["budget"]["cost_usd"] is None and summary["budget"]["cost_known_usd"] == 0.04
    assert "at least $0.04" in summary["note"]


def test_budget_spent_round_trips_through_the_journal() -> None:
    assert extension_run._budget_spent_from({"seconds": "12.5", "tokens": 7, "cost_usd": None}) == {
        "seconds": 12.5, "tokens": 7, "cost_usd": None, "cost_known_usd": 0.0, "cost_unknown_calls": 1,
    }
    assert extension_run._budget_spent_from({"cost_usd": 0.5}) == {
        "seconds": 0.0, "tokens": 0, "cost_usd": 0.5, "cost_known_usd": 0.5, "cost_unknown_calls": 0,
    }
    full = {"seconds": 1.0, "tokens": 2, "cost_usd": None, "cost_known_usd": 0.25, "cost_unknown_calls": 3}
    assert extension_run._budget_spent_from(full) == full
    assert extension_run._budget_spent_from("junk") == {
        "seconds": 0.0, "tokens": 0, "cost_usd": 0.0, "cost_known_usd": 0.0, "cost_unknown_calls": 0,
    }


# --- a cost that is partly unknown ------------------------------------------------------------------


def test_cost_cap_acts_on_the_known_part_when_some_calls_report_no_cost(monkeypatch) -> None:
    calls = {"n": 0}

    def record() -> None:
        calls["n"] += 1
        # Every other call has no cost figure.
        model_call_ledger.record("vision", _usage(10, 10, None if calls["n"] % 2 else 0.02))

    summary, decisions, events, _stops = _run_plain(monkeypatch, limits={**_NO_LIMITS, "cost_usd": 0.03}, max_steps=50, on_decide=record)
    assert summary["reason"] == "max_run_cost_usd", "the known part alone passed the cap"
    assert decisions == 4
    assert summary["budget"]["cost_usd"] is None and summary["budget"]["cost_unknown_calls"] == 2
    assert summary["budget"]["cost_known_usd"] == 0.04


def test_the_first_unknown_cost_is_announced_once_and_the_summary_says_the_cap_was_partial(monkeypatch) -> None:
    summary, decisions, events, _stops = _run_plain(
        monkeypatch,
        limits={**_NO_LIMITS, "cost_usd": 5.0},
        max_steps=4,
        on_decide=lambda: model_call_ledger.record("vision", _usage(10, 10, None)),
    )
    assert summary["reason"] == "max_steps" and decisions == 4
    unknown_events = [payload for kind, payload in events if kind == "RUN_BUDGET_COST_UNKNOWN"]
    assert len(unknown_events) == 1 and unknown_events[0]["cost_unknown_calls"] >= 1
    assert "could not be fully enforced" in summary["cost_cap_note"]


def test_no_cost_cap_means_no_unknown_cost_notice(monkeypatch) -> None:
    summary, _decisions, events, _stops = _run_plain(
        monkeypatch,
        limits=_NO_LIMITS,
        max_steps=3,
        on_decide=lambda: model_call_ledger.record("vision", _usage(10, 10, None)),
    )
    assert not [1 for kind, _p in events if kind == "RUN_BUDGET_COST_UNKNOWN"]
    assert "cost_cap_note" not in summary


def test_ledger_totals_keep_the_known_cost_and_the_unknown_count() -> None:
    token = model_call_ledger.bind(777)
    try:
        model_call_ledger.record("a", _usage(1, 1, 0.01))
        model_call_ledger.record("b", _usage(1, 1, None))
        model_call_ledger.record("a", _usage(1, 1, 0.02))
        totals = model_call_ledger.totals(777)
    finally:
        model_call_ledger.release(token, 777)
    assert totals["cost_usd"] is None and totals["cost_known_usd"] == 0.03 and totals["cost_unknown_calls"] == 1


# --- spend survives an abnormal loop end --------------------------------------------------------------


def _journal_budget(run_id: int) -> dict:
    return extension_run._load_run_journal_for_run(run_id)["budget_spent"]


def _run_user_id(run_id: int) -> int:
    with SessionLocal() as db:
        return int(db.get(models.OperatorRun, run_id).user_id)


class _LoopDied(BaseException):
    """Passes every `except Exception` in the loop, like an error nothing handles."""


def _loop_ending_by(monkeypatch, exc: BaseException) -> int:
    """Runs a real-run loop whose third model decision raises `exc`; returns the run id."""
    run_id, _thread_id = _make_run()
    decided = {"n": 0}

    def decide(user_id, image, view, context):
        decided["n"] += 1
        model_call_ledger.record("vision", _usage(300, 100, 0.01))
        return {"action": "wait", "wait_ms": 100 + decided["n"], "reason": "Look again."}

    real_await = extension_run._await_with_progress

    async def await_with_progress(*args, **kwargs):
        # The third decision's wait is where the loop dies (after its model call was tallied).
        result = await real_await(*args, **kwargs)
        if decided["n"] == 3:
            raise exc
        return result

    monkeypatch.setattr(extension_run, "_await_with_progress", await_with_progress)

    async def narrate(kind: str, data: dict) -> None:
        return None

    monkeypatch.setattr("app.candidate_context.CandidateContextEngine", _DummyContextEngine)
    monkeypatch.setattr(extension_run, "_record_context_usage_for_run", lambda *a, **k: None)
    monkeypatch.setattr(extension_run, "_decide_vision_action", decide)
    monkeypatch.setattr(extension_run, "_run_budget_limits", lambda user_id: dict(_NO_LIMITS))
    try:
        asyncio.run(run_vision_loop(_VisionBody(), url=None, narrate=narrate, run_id=run_id, user_id=_run_user_id(run_id), skip_navigate=True, max_steps=10))
    except BaseException as raised:  # noqa: BLE001
        assert raised is exc or isinstance(raised, type(exc)), "the original error is not masked"
    else:  # pragma: no cover
        raise AssertionError("the loop should not have ended normally")
    return run_id


def test_spend_is_persisted_when_the_loop_ends_by_an_uncaught_error(monkeypatch) -> None:
    run_id = _loop_ending_by(monkeypatch, _LoopDied("loop exploded"))
    spent = _journal_budget(run_id)
    assert spent["tokens"] == 1200 and spent["cost_known_usd"] == 0.03 and spent["cost_usd"] == 0.03
    assert extension_run._load_run_journal_for_run(run_id)["steps_before"] >= 3


def test_spend_is_persisted_when_the_loop_task_is_cancelled(monkeypatch) -> None:
    # What the stall watchdog does to a stuck loop: a cancel lands in the loop's task.
    run_id = _loop_ending_by(monkeypatch, asyncio.CancelledError())
    assert _journal_budget(run_id)["tokens"] == 1200


# --- the reply carries the budget notes ---------------------------------------------------------------


def test_budget_stop_reply_uses_the_honest_note_and_the_cost_cap_note_is_appended() -> None:
    usage = {"elapsed_seconds": 61.0}
    summary = {
        "state": "BLOCKED", "reason": "max_run_seconds", "budget": usage, "actions": 7,
        "note": extension_run._budget_stop_note("max_run_seconds", usage),
    }
    message = extension_run._budget_final_message(summary)
    assert message.startswith("I stopped after 7 step(s). Stopped because the run used its time limit")
    assert "max_run_seconds" not in message and "not known to be finished" in message
    assert extension_run._budget_final_message({"reason": "max_steps", "actions": 3}) == ""
    with_cap = extension_run._with_cost_cap_note(message, {"cost_cap_note": "The cost cap of $1.00 could not be fully enforced."})
    assert with_cap.endswith("could not be fully enforced.")
    assert extension_run._with_cost_cap_note(message, {}) == message
