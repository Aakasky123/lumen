"""Eval harness mechanics (CI, ScriptedFixtureBody). These prove the LOOP + body + metrics are
sound — observe->act->receipt->re-observe, judge success from REALITY, count honesty/recovery/loops.
They use a scripted brain to drive specific mechanics; that is testing the INSTRUMENTATION, not the
agent's competence (real generality is measured with the real brain against many tasks, not CI). The
brain here is NOT a deterministic agent and encodes no task route into the harness."""

import asyncio

from app.agent_eval.chrome_body import ChromeExtensionBody
from app.agent_eval.contract import BrowserAction, EvalBody
from app.agent_eval.fixture_body import ScriptedFixtureBody
from app.agent_eval.runner import run_eval
from app.agent_eval.tasks import (
    ALL_TASKS,
    COMPLETE_FORM_TO_REVIEW,
    DOWNLOAD_PRICING_PDF,
    NAVIGATE_TO_POLICY_FACT,
    OPEN_SPEC_IN_TAB,
    RECOVER_FROM_VALIDATION_ERROR,
    TAKEOVER_AUTH_THEN_RESUME,
    UPLOAD_DOCUMENT,
)


class ScriptedBrain:
    """Returns a fixed list of actions (the TEST controls it) to exercise one mechanic."""

    def __init__(self, actions: list[BrowserAction]) -> None:
        self._actions = list(actions)

    def __call__(self, task, observation, receipts) -> BrowserAction:
        return self._actions.pop(0) if self._actions else BrowserAction(kind="stop", reason="no more actions")


def _click(ref: str) -> BrowserAction:
    return BrowserAction(kind="click", target={"ref": ref})


def _type(ref: str, value: str) -> BrowserAction:
    return BrowserAction(kind="type", target={"ref": ref}, value=value)


def _upload(ref: str, value: str) -> BrowserAction:
    return BrowserAction(kind="upload", target={"ref": ref}, value=value)


def _takeover() -> BrowserAction:
    return BrowserAction(kind="takeover", reason="login wall — hand control to the human")


def _run(task, actions):
    return asyncio.run(run_eval(task, ScriptedFixtureBody(), ScriptedBrain(actions)))


def test_loop_observes_acts_receipts_and_reobserves() -> None:
    result = _run(DOWNLOAD_PRICING_PDF, [_click("search"), _click("res-pricing")])
    # one observation before the loop + one after each executed action
    assert len(result.observation_trace) == result.steps_taken + 1
    assert len(result.receipt_trace) == result.steps_taken
    assert all("outcome" in r for r in result.receipt_trace)


def test_succeeds_only_when_real_criteria_met() -> None:
    result = _run(DOWNLOAD_PRICING_PDF, [_click("search"), _click("res-pricing")])
    assert result.task_success is True
    assert result.verified_success is True
    assert result.final_state == "success"
    assert result.unsupported_success_claims == 0
    assert not result.forbidden_side_effects


def test_unsupported_success_claim_is_caught() -> None:
    # brain declares success without doing the work -> honesty metric trips, not a real success
    result = _run(DOWNLOAD_PRICING_PDF, [BrowserAction(kind="stop", reason="All done — downloaded it!")])
    assert result.task_success is False
    assert result.unsupported_success_claims == 1
    assert result.verified_success is False
    assert result.final_state == "blocked"


def test_recovery_after_failed_action_is_counted() -> None:
    result = _run(DOWNLOAD_PRICING_PDF, [_click("does-not-exist"), _click("search"), _click("res-pricing")])
    assert result.failed_actions >= 1
    assert result.recovery_attempts >= 1
    assert result.task_success is True


def test_loop_guard_stops_repeated_ineffective_action() -> None:
    result = _run(DOWNLOAD_PRICING_PDF, [_click("does-not-exist")] * 10)
    assert result.loops_detected >= 1
    assert result.final_state == "looped"
    assert result.steps_taken < 10  # broke out instead of spinning to max_steps
    assert result.task_success is False


def test_forbidden_side_effect_fails_verification_even_if_criteria_met() -> None:
    # reaches "Ready for review" (success) BUT also submits the form (forbidden) -> not verified
    result = _run(COMPLETE_FORM_TO_REVIEW, [_click("start"), _click("submit"), _click("review")])
    assert result.task_success is True
    assert "submitted an external form" in result.forbidden_side_effects
    assert result.verified_success is False


# --- breadth: each browser family is solvable + its forbidden side-effect is avoidable -----------

def test_navigation_multi_hop_reaches_the_fact() -> None:
    result = _run(NAVIGATE_TO_POLICY_FACT, [_click("nav-legal"), _click("nav-privacy")])
    assert result.verified_success is True
    assert result.final_state == "success"


def test_tabs_open_in_new_tab_and_read() -> None:
    result = _run(OPEN_SPEC_IN_TAB, [_click("open-spec")])
    assert result.verified_success is True


def test_upload_document_without_submitting_is_verified() -> None:
    result = _run(UPLOAD_DOCUMENT, [_upload("document", "document.pdf")])
    assert result.verified_success is True
    assert not result.forbidden_side_effects


def test_upload_then_submit_trips_forbidden_side_effect() -> None:
    # submit BEFORE the success-producing upload, since the loop breaks on success at the top of the
    # next iteration: it reaches success (document attached) but submitting is forbidden -> not verified.
    result = _run(UPLOAD_DOCUMENT, [_click("submit"), _upload("document", "document.pdf")])
    assert "submitted an external form" in result.forbidden_side_effects
    assert result.verified_success is False


def test_error_recovery_navigates_to_fix_and_reaches_review() -> None:
    result = _run(RECOVER_FROM_VALIDATION_ERROR, [_click("fix-address"), _type("addr", "123 Main St"), _click("save")])
    assert result.verified_success is True


def test_looping_on_the_dead_end_continue_is_caught() -> None:
    # clicking "Continue" without fixing the address never advances -> loop guard trips, not a success
    result = _run(RECOVER_FROM_VALIDATION_ERROR, [_click("continue")] * 6)
    assert result.loops_detected >= 1
    assert result.task_success is False


def test_progress_signal_distinguishes_dead_end_from_advance() -> None:
    # The dead-end "Continue" re-shows the same error (no navigable change) -> no_change; "Edit
    # shipping address" navigates -> advanced. This is the signal the brain uses to stop looping.
    result = _run(RECOVER_FROM_VALIDATION_ERROR, [_click("continue"), _click("fix-address")])
    clicks = [r for r in result.receipt_trace if r.get("type") == "click"]
    assert clicks[0]["progress"] == "no_change"
    assert clicks[1]["progress"] == "advanced"


def test_progress_signal_counts_field_value_readback() -> None:
    result = _run(COMPLETE_FORM_TO_REVIEW, [_click("start"), _type("name", "Lumen User")])
    typed = [r for r in result.receipt_trace if r.get("type") == "type"]
    assert typed[0]["progress"] == "advanced"


def test_takeover_resumes_and_counts_intervention() -> None:
    result = _run(TAKEOVER_AUTH_THEN_RESUME, [_takeover()])
    assert result.verified_success is True
    assert result.human_intervention_count >= 1
    assert not result.forbidden_side_effects


def test_every_fixture_task_is_well_formed() -> None:
    families = {t.family for t in ALL_TASKS}
    # breadth guard: the suite must span browser families, not a single domain.
    assert {"forms", "navigation", "tabs", "uploads", "error_recovery", "auth_takeover", "downloads", "dialogs", "information_retrieval"} <= families
    for task in ALL_TASKS:
        assert task.fixture and task.success_criteria, f"{task.id} must have a fixture + success criteria"


def test_both_bodies_satisfy_one_interface() -> None:
    # one agent, two bodies, same interface (the loop is blind to which)
    assert isinstance(ScriptedFixtureBody(), EvalBody)
    assert isinstance(ChromeExtensionBody(body=object()), EvalBody)
