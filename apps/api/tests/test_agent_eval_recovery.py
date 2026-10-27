"""Measures the vision RECOVERY ladder on the eval harness (CI, ScriptedFixtureBody): a task whose next
control is missing from the structured view and only a screenshot escalation reveals it. This proves the
loop recovers — verified_success — AND that the recovery is REQUIRED (a text-only agent fails the same
task). Per CLAUDE.md, generality/recovery is MEASURED, not asserted. The scripted deciders here exercise
the harness mechanics; competence is measured live with the real brain + a vision model."""

import asyncio

from app.agent_eval.contract import BrowserAction
from app.agent_eval.fixture_body import ScriptedFixtureBody
from app.agent_eval.runner import run_eval
from app.agent_eval.tasks import RECOVER_VIA_SCREENSHOT


def _refs(obs) -> set:
    return {str(e.get("ref")) for e in (obs.elements or [])}


def _escalating_decider(task, obs, receipts) -> BrowserAction:
    """An LLM-shaped agent: act on the control when visible; when the structured view is insufficient,
    escalate to a screenshot (the vision rung) and re-decide."""
    if "continue" in _refs(obs):
        return BrowserAction(kind="click", target={"ref": "continue"}, reason="continue is now visible")
    if not getattr(obs, "image", None):
        return BrowserAction(kind="screenshot", reason="cannot find the next control in the structured view")
    return BrowserAction(kind="blocked", reason="still cannot find the control")


def _text_only_decider(task, obs, receipts) -> BrowserAction:
    """A non-escalating agent — never asks to SEE the page. It cannot find the hidden control."""
    if "continue" in _refs(obs):
        return BrowserAction(kind="click", target={"ref": "continue"}, reason="continue visible")
    return BrowserAction(kind="blocked", reason="no continue control in the element list")


def _always_screenshot_decider(task, obs, receipts) -> BrowserAction:
    return BrowserAction(kind="screenshot", reason="always escalate")


def test_screenshot_escalation_reaches_review() -> None:
    result = asyncio.run(run_eval(RECOVER_VIA_SCREENSHOT, ScriptedFixtureBody(), _escalating_decider))
    assert result.verified_success is True
    assert result.recovery_attempts >= 1  # the screenshot escalation was counted as a recovery
    assert not result.forbidden_side_effects  # reached review, never submitted


def test_recovery_is_required_text_only_agent_fails() -> None:
    # The control is hidden from the structured view, so an agent that never escalates cannot succeed.
    result = asyncio.run(run_eval(RECOVER_VIA_SCREENSHOT, ScriptedFixtureBody(), _text_only_decider))
    assert result.verified_success is False
    assert result.task_success is False


def test_repeated_screenshot_without_acting_is_bounded() -> None:
    # An agent that only ever screenshots must not spin: once an image is attached, a re-request stops.
    result = asyncio.run(run_eval(RECOVER_VIA_SCREENSHOT, ScriptedFixtureBody(), _always_screenshot_decider))
    assert result.final_state == "blocked"
    assert result.verified_success is False
