"""A sequence's leading click is judged alongside its follow-up clicks, and each step's timing names
the phases that used to be invisible (settle pause, preparation, side-by-side checks)."""

from __future__ import annotations

import asyncio
import threading
import time

from app import extension_run
from app.db import init_db
from app.extension_run import run_vision_loop

from tests.test_extension_run import (  # noqa: F401 - the autouse fixtures apply to this module too
    _DummyContextEngine,
    _TimedReleaseBody,
    _complete_final_action_readiness_audit_by_default,
    _ordinary_click_consequence_by_default,
    _ordinary_secret_semantics_by_default,
)


def test_the_leading_click_is_judged_alongside_the_follow_up_and_the_phases_are_timed(monkeypatch) -> None:
    init_db()
    body = _TimedReleaseBody(ready_text_appears=True)
    decisions = iter([
        {
            "action": "click", "x": 100, "y": 200, "reason": "Arm; fire when the window opens.",
            "then": [
                {"action": "wait", "wait_ms": 9000, "text": "Ready - fire now"},
                {"action": "click", "x": 300, "y": 200, "text": ""},
            ],
        },
        {"action": "done", "reason": "Fired."},
    ])
    spans: list[tuple[float, float, int]] = []
    lock = threading.Lock()

    def judge(*args, **kwargs):
        started = time.monotonic()
        time.sleep(0.3)
        with lock:
            spans.append((started, time.monotonic(), int(kwargs.get("x") or 0)))
        return {"classification": "intermediate", "confidence": 0.99, "evidence": "Fixture."}

    timings: list[dict] = []

    async def narrate(kind: str, data: dict) -> None:
        pass

    monkeypatch.setattr("app.candidate_context.CandidateContextEngine", _DummyContextEngine)
    monkeypatch.setattr(extension_run, "_VISION_STEP_SETTLE_SECONDS", 0.05)
    monkeypatch.setattr(
        extension_run,
        "_db_event",
        lambda run_id, event_type, payload: timings.append(payload) if event_type == "STEP_TIMING" else None,
    )
    monkeypatch.setattr(extension_run, "_record_context_usage_for_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(extension_run, "_judge_click_consequence", judge)
    monkeypatch.setattr(extension_run, "_decide_vision_action", lambda *args, **kwargs: next(decisions))

    asyncio.run(run_vision_loop(body, url=None, narrate=narrate, run_id=0, user_id=0, skip_navigate=True, max_steps=3))

    assert body.clicks == [(100, 200), (300, 200)]
    assert sorted(x for _, _, x in spans) == [100, 300], "each click judged exactly once"
    (a_start, a_end, _), (b_start, b_end, _) = sorted(spans)
    assert b_start < a_end, "the leading click's check ran alongside the follow-up's, not after it"
    sequence_step = timings[0]
    assert sequence_step["settle_ms"] == 50
    assert sequence_step["prepare_ms"] >= 0
    assert 250 <= sequence_step["prejudge_ms"] < 550, "one round of side-by-side checks, not two in a row"


def test_a_bare_wait_counts_from_the_previous_dispatch_never_negative() -> None:
    timing: dict = {}
    # No dispatch recorded: the full request remains.
    assert extension_run._bare_wait_plan(timing, 1000, now=10.0) == (1000, 0)
    # The press time is the browser's own clock when it falls inside the body call.
    extension_run._record_dispatch_mark(
        timing, "click", {"ok": True, "pressed_at_ms": 1_000_000 - 800},
        act_started=9.0, returned_at=10.0, wall_now_ms=1_000_000,
    )
    assert abs(timing["_dispatch_ref"] - 9.2) < 1e-9
    assert extension_run._bare_wait_plan(timing, 1000, now=10.05) == (150, 850)
    # Already elapsed: nothing remains, and never a negative number.
    assert extension_run._bare_wait_plan(timing, 1000, now=12.0) == (0, 1000)
    # A press time the clocks disagree about is ignored: dispatch is the body's return.
    other: dict = {}
    extension_run._record_dispatch_mark(
        other, "click", {"ok": True, "pressed_at_ms": 1_000_000 - 9000},
        act_started=9.0, returned_at=10.0, wall_now_ms=1_000_000,
    )
    assert other["_dispatch_ref"] == 10.0
    # A step that did not activate leaves nothing to count from.
    extension_run._record_dispatch_mark(other, "click", {"ok": False}, act_started=10.0, returned_at=10.1)
    assert extension_run._bare_wait_plan(other, 1000, now=10.5) == (1000, 0)
    assert [row["kind"] for row in timing["dispatch_log"]] == ["click"]


class _DelayedButtonBody:
    """Click ONE (slow to return, like a read-back), wait, click TWO by its label; the page can change."""

    def __init__(self, *, page_changes_during_wait: bool = False, one_verified: bool = False) -> None:
        self.one_verified = one_verified
        self.page_changes_during_wait = page_changes_during_wait
        self.captures = 0
        self.waits: list[int] = []
        self.clicks: list[tuple[int, int]] = []
        self.label_clicks: list[str] = []
        self.wait_ended: float | None = None
        self.wait_started: float | None = None

    async def capture_screenshot(self, **_kwargs: object) -> dict:
        self.captures += 1
        changed = bool(self.page_changes_during_wait and self.wait_started is not None and time.monotonic() - self.wait_started > 0.2)
        return {
            "ok": True, "image": "jpeg", "width": 800, "height": 600,
            "url": "https://example.test/next" if changed else "https://example.test/delay",
            "title": "Delay", "scroll_y": 0, "scroll_height": 600,
            "page_evidence": {"viewport_text": "ONE TWO", "controls": [
                {"index": 1, "tag": "button", "text": "ONE", "selector": "#one"},
                {"index": 2, "tag": "button", "text": "TWO", "selector": "#two"},
            ]},
        }

    async def vision_click(self, x: int, y: int, *, target_text: str = "", **_binding: object) -> dict:
        if target_text:
            self.label_clicks.append(target_text)
            return {"ok": True, "action": "click", "surface_changed": True, "target_text_resolved": True}
        pressed = time.time() * 1000.0
        self.clicks.append((x, y))
        await asyncio.sleep(0.6)
        return {"ok": True, "action": "click", "pressed_at_ms": int(pressed), **({"target_state_changed": True} if self.one_verified else {})}

    async def vision_wait(self, wait_ms: int, *, text: str = "") -> dict:
        self.waits.append(wait_ms)
        self.wait_started = time.monotonic()
        await asyncio.sleep(wait_ms / 1000.0)
        self.wait_ended = time.monotonic()
        return {"ok": True, "action": "wait", "waited_ms": wait_ms, "text_matched": False}


def _run_delayed_button(monkeypatch, body: _DelayedButtonBody, *, upfront_ordinary: bool = False, after_run=None):
    init_db()
    decisions = iter([
        {"action": "click", "x": 100, "y": 200, "reason": "Press ONE.",
         "then": [{"action": "wait", "wait_ms": 1000},
                  {"action": "click", "target_text": "TWO", "reason": "Press TWO."}]},
        {"action": "done", "reason": "Pressed both."},
    ])
    timings: list[dict] = []
    events: list[tuple[str, dict]] = []
    judge_spans: list[tuple[float, float]] = []
    two_judgements = {"n": 0}

    def judge(*args, **kwargs):
        started = time.monotonic()
        time.sleep(0.2)
        judge_spans.append((started, time.monotonic()))
        if (kwargs.get("action") or {}).get("target_text") == "TWO":
            two_judgements["n"] += 1
            if two_judgements["n"] == 1 and not upfront_ordinary:  # the opening observation cannot vouch for it: re-judged at dispatch
                return {"classification": "uncertain", "confidence": 0.3, "evidence": "Fixture."}
        return {"classification": "intermediate", "confidence": 0.99, "evidence": "Fixture."}

    def record(run_id, event_type, payload):
        events.append((event_type, payload))
        if event_type == "STEP_TIMING":
            timings.append(payload)

    async def narrate(kind: str, data: dict) -> None:
        pass

    monkeypatch.setattr("app.candidate_context.CandidateContextEngine", _DummyContextEngine)
    monkeypatch.setattr(extension_run, "_VISION_STEP_SETTLE_SECONDS", 0.0)
    monkeypatch.setattr(extension_run, "_db_event", record)
    monkeypatch.setattr(extension_run, "_record_context_usage_for_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(extension_run, "_judge_click_consequence", judge)
    monkeypatch.setattr(extension_run, "_decide_vision_action", lambda *args, **kwargs: next(decisions))
    async def main() -> None:
        await run_vision_loop(body, url=None, narrate=narrate, run_id=0, user_id=0, skip_navigate=True, max_steps=3)
        if after_run is not None:
            await after_run()

    asyncio.run(main())
    return timings, events, judge_spans


def test_a_chained_wait_is_measured_from_dispatch_and_keeps_its_gates(monkeypatch) -> None:
    body = _DelayedButtonBody()
    timings, events, judge_spans = _run_delayed_button(monkeypatch, body, upfront_ordinary=True)

    assert body.clicks == [(100, 200)] and body.label_clicks == ["TWO"]
    # Click ONE took 0.6 s to return: only the rest of the 1000 ms remains to wait.
    assert len(body.waits) == 1 and 100 <= body.waits[0] <= 450, body.waits
    log = timings[0]["dispatch_log"]
    wait_row = next(row for row in log if row["kind"] == "wait")
    assert wait_row["requested_ms"] == 1000 and wait_row["credited_ms"] >= 550
    # Both clicks were still consequence-judged (side by side, before the wait).
    assert len(judge_spans) == 2


def test_the_fresh_view_judgement_of_a_label_click_runs_during_the_wait(monkeypatch) -> None:
    body = _DelayedButtonBody(one_verified=True)
    timings, events, judge_spans = _run_delayed_button(monkeypatch, body)

    assert body.label_clicks == ["TWO"]
    assert len(body.waits) == 1 and 100 <= body.waits[0] <= 450, body.waits
    # The gate still ran on a fresh view of the page, and it was computed during the wait.
    rejudged = [payload for kind, payload in events if kind == "VISION_FOLLOWUP_CLICK_REJUDGED"]
    assert len(rejudged) == 1 and rejudged[0]["early"] is True and rejudged[0]["classification"] == "intermediate"
    assert judge_spans[-1][0] < body.wait_ended, judge_spans


def test_a_page_change_during_the_wait_falls_back_to_judging_after_it(monkeypatch) -> None:
    body = _DelayedButtonBody(page_changes_during_wait=True, one_verified=True)
    timings, events, judge_spans = _run_delayed_button(monkeypatch, body)

    assert body.label_clicks == ["TWO"], "the click is still gated and then runs"
    rejudged = [payload for kind, payload in events if kind == "VISION_FOLLOWUP_CLICK_REJUDGED"]
    # The verdict made on the earlier view is discarded: the click is judged again on the changed page.
    assert [payload["early"] for payload in rejudged] == [True, False]


def test_an_early_judge_is_cancelled_when_the_chain_exits_before_its_click(monkeypatch) -> None:
    body = _DelayedButtonBody(one_verified=True)

    async def failing_wait(wait_ms: int, *, text: str = "") -> dict:
        body.waits.append(wait_ms)
        return {"ok": False, "action": "wait", "failure_reason": "wait_failed"}

    body.vision_wait = failing_wait  # type: ignore[method-assign]
    seen: dict = {}

    async def after_run() -> None:
        seen["captures_at_exit"] = body.captures
        await asyncio.sleep(0.8)  # long enough for an uncancelled task to take its second capture
        seen["captures_later"] = body.captures
        seen["leaked"] = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]

    _run_delayed_button(monkeypatch, body, after_run=after_run)

    assert body.label_clicks == [], "the chain ended before the click"
    assert seen["captures_later"] == seen["captures_at_exit"], "the cancelled task did no further work"
    assert seen["leaked"] == []
