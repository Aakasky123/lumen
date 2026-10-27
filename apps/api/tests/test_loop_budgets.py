"""Liveness budgets of the vision loop: a failure the model cannot repair must end the run, not
spend every remaining step on the same rejected action."""

import asyncio

from app import extension_run
from app.brain_escalation import SELF_RECOVERY_LIMIT
from app.db import init_db
from app.extension_hub import ExtensionTransportError
from app.extension_run import run_vision_loop

from test_extension_run import _ControlIndexClickBody, _DummyContextEngine, _TransportInterruptedClickBody


def _stub_loop(monkeypatch, decide):
    monkeypatch.setattr("app.candidate_context.CandidateContextEngine", _DummyContextEngine)
    monkeypatch.setattr(extension_run, "_db_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(extension_run, "_record_context_usage_for_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(extension_run, "_decide_vision_action", decide)
    # Same fixture verdict test_extension_run.py applies to every loop test: the click is ordinary.
    monkeypatch.setattr(
        extension_run,
        "_judge_click_consequence",
        lambda *args, **kwargs: {
            "classification": "intermediate",
            "confidence": 0.99,
            "evidence": "Fixture verdict: this selected click is ordinary and reversible.",
            "metadata": {"provider": "fixture", "model": "fixture-click-risk", "attempt_count": 1},
        },
    )


async def _narrate(kind: str, data: dict) -> None:
    pass


class _DroppingClickBody(_TransportInterruptedClickBody):
    """Every click loses its transport; every screenshot succeeds and still shows the button."""

    async def vision_click(self, x, y, *, expected_target_identity=None, expected_page_context=None):
        self.clicks += 1
        raise ExtensionTransportError("Extension reconnected while the click result was in flight.")


def test_a_click_that_always_drops_its_transport_ends_the_run_within_the_recovery_limit(monkeypatch) -> None:
    # A successful screenshot used to reset the shared interruption counter, so an action whose
    # transport dropped every time never reached the limit and looped to max_steps.
    init_db()
    body = _DroppingClickBody()
    _stub_loop(
        monkeypatch,
        lambda user_id, image, view, context: {"action": "click", "x": 110, "y": 120, "reason": "Continue to the review section."},
    )

    summary = asyncio.run(run_vision_loop(body, url=None, narrate=_narrate, run_id=0, user_id=0, skip_navigate=True, max_steps=12))

    assert summary["state"] == "BLOCKED"
    assert summary["reason"] == "extension_transport_unavailable"
    assert body.clicks == extension_run._BODY_TRANSPORT_RECOVERY_LIMIT + 1


def test_a_sequence_the_body_keeps_rejecting_blocks_after_the_retry_budget(monkeypatch) -> None:
    # The sequence-invalid branch shared the schema-invalid counter, which reset on every schema-valid
    # response, so every rejection looked like attempt 1 (355 of 355 in history) and nothing bounded it.
    init_db()
    body = _ControlIndexClickBody()
    decisions: list[dict] = []

    def decide(user_id, image, view, context):
        action = {"action": "click", "control": 99, "reason": "Open the missing control."}
        decisions.append(action)
        return dict(action)

    _stub_loop(monkeypatch, decide)

    summary = asyncio.run(run_vision_loop(body, url=None, narrate=_narrate, run_id=0, user_id=0, skip_navigate=True, max_steps=20))

    assert summary["state"] == "BLOCKED"
    assert summary["reason"] == "agent_confused"
    # Each budget of retries is spent, then the stop recovers in-loop (brain_escalation.SELF_RECOVERY_LIMIT
    # times per run) before the final budget ends the run: one budget per recovery plus the last.
    assert len(decisions) == (SELF_RECOVERY_LIMIT + 1) * (extension_run._SEQUENCE_INVALID_RETRY_LIMIT + 1)
    assert body.clicks == [] and body.control_clicks == []


def test_an_outside_cancel_stops_the_wrapped_body_call(monkeypatch) -> None:
    # The stall watchdog cancels the run task; the body call inside _await_with_progress used to keep
    # running and could finish a click after the run had ended.
    state = {"started": False, "finished": False, "cancelled": False}

    async def slow_click():
        state["started"] = True
        try:
            await asyncio.sleep(5)
            state["finished"] = True
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    async def main():
        waiter = asyncio.ensure_future(
            extension_run._await_with_progress(slow_click(), narrate=_narrate, kind="execute", data={}, timeout_seconds=30)
        )
        await asyncio.sleep(0.05)
        waiter.cancel()
        try:
            await waiter
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0)

    asyncio.run(main())
    assert state["started"] and state["cancelled"] and not state["finished"]


class _UnknownOutcomeClickBody(_TransportInterruptedClickBody):
    """The press reached the page but its acknowledgement timed out."""

    async def vision_click(self, x, y, *, expected_target_identity=None, expected_page_context=None):
        self.clicks += 1
        return {
            "ok": False,
            "action": "click",
            "mode": "cdp_trusted_click",
            "activation_attempted": True,
            "activation_dispatched": True,
            "activation_count": 1,
            "execution_outcome_unknown": True,
            "failure_reason": "click_execution_outcome_unknown",
        }


def test_a_click_with_an_unknown_outcome_is_reported_as_possibly_done_and_reobserved(monkeypatch) -> None:
    init_db()
    body = _UnknownOutcomeClickBody()
    contexts: list[dict] = []

    def decide(user_id, image, view, context):
        contexts.append(context)
        if len(contexts) == 1:
            return {"action": "click", "x": 110, "y": 120, "reason": "Continue to the review section."}
        return {"action": "done", "x": 0, "y": 0, "artifact_role": "", "reason": "Stop here."}

    _stub_loop(monkeypatch, decide)
    captures_before = body.captures
    asyncio.run(run_vision_loop(body, url=None, narrate=_narrate, run_id=0, user_id=0, skip_navigate=True, max_steps=3))

    assert body.clicks == 1
    assert any("click_execution_outcome_unknown" in item for item in contexts[1]["recent_actions"])
    assert body.captures - captures_before >= 2, "the decision after the click sees a fresh screenshot"


def test_a_different_click_gesture_on_the_same_control_is_a_different_action() -> None:
    # The progress guard fingerprinted clicks without their gesture, so after plain clicks on a
    # badge failed it withheld the shift-click the badge asked for as "the same action".
    fingerprint = extension_run._progress_action_fingerprint
    plain = fingerprint("click", {"action": "click", "x": 87, "y": 506}, target_text="Override badge")
    assert plain == fingerprint("click", {"action": "click", "x": 88, "y": 507, "clicks": 1, "button": "left", "hold_ms": 0, "modifiers": []}, target_text="Override badge"), "plain clicks fingerprint as before"
    shift = fingerprint("click", {"action": "click", "x": 87, "y": 506, "modifiers": ["shift"]}, target_text="Override badge")
    triple = fingerprint("click", {"action": "click", "x": 87, "y": 506, "clicks": 3}, target_text="Override badge")
    held = fingerprint("click", {"action": "click", "x": 87, "y": 506, "hold_ms": 1000}, target_text="Override badge")
    held_short = fingerprint("click", {"action": "click", "x": 87, "y": 506, "hold_ms": 500}, target_text="Override badge")
    assert len({plain, shift, triple, held, held_short}) == 5
    assert held == fingerprint("click", {"action": "click", "x": 87, "y": 506, "hold_ms": 1040}, target_text="Override badge"), "hold jitter is the same attempt"


def test_a_run_cancelled_before_its_first_step_finalizes_without_error(monkeypatch) -> None:
    """_finalize reads the unverified-value tally, so the tally must be bound before the first early stop."""
    import asyncio

    from app.db import init_db
    from app.extension_run import run_vision_loop

    from test_extension_run import _ControlIndexClickBody

    init_db()
    _stub_loop(monkeypatch, lambda user_id, image, view, context: {"action": "wait", "wait_ms": 100, "reason": "Unused."})

    summary = asyncio.run(
        run_vision_loop(
            _ControlIndexClickBody(), url=None, narrate=_narrate, run_id=0, user_id=0,
            skip_navigate=True, max_steps=3, should_cancel=lambda: True,
        )
    )

    assert summary["actions"] == 0
    assert summary["reason"]
