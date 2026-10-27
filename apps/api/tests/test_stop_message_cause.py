"""The stop message names the cause the run's recorded facts show: a provider failure only when a provider
failure ended the run, a harness refusal when the harness withheld the last actions, and a page that did
not change when nothing advanced. It never blames the provider for a run the provider did not fail."""

from __future__ import annotations

import asyncio
import types
import uuid

from app import extension_run, models
from app.brain_escalation import PAGE_UNCHANGED_OUTCOMES, RUNTIME_REFUSAL_OUTCOMES
from app.db import SessionLocal, init_db
from app.extension_run import drive_in_browser_application, run_vision_loop

from tests.test_extension_run import (  # noqa: F401 - the autouse fixtures apply to this module too
    _complete_final_action_readiness_audit_by_default,
    _DummyContextEngine,
    _ordinary_click_consequence_by_default,
    _ordinary_secret_semantics_by_default,
    _PreActionRevalidationBody,
)
from tests.test_loop_progress_signals import _ZOOM_REGIONS, _run_reads_only, _StaticImageBody, _zoom

_KIND_RULE = "same_exact_control_action_kind_is_withheld_until_durable_progress"
_PROVIDER_TAIL = "internal/provider failure"
_ENGINEERING_TAIL = "This is an engineering block, not a missing answer from you."


def _blocked(reason: str, *, trail: list[dict], actions: int = 4, **extra) -> dict:
    return {
        "state": "BLOCKED",
        "reason": reason,
        "actions": actions,
        "recent_step_outcomes": trail,
        "receipts": [],
        **extra,
    }


def _step(outcome: str, code: str = "") -> dict:
    return {"outcome": outcome, "code": code}


def _cycle_trail_with_refusals() -> list[dict]:
    # 1795's shape: a progress-guard refusal, a stale-target refusal, then the page stays the same.
    return [
        _step("executed"),
        _step("precondition_blocked", _KIND_RULE),
        _step("pre_action_revalidation_failed", "control_target_drift"),
        _step("executed"),
        _step("executed_without_durable_progress"),
        _step("page_state_cycle_exhausted"),
    ]


def _drive_with_summary(monkeypatch, summary: dict) -> tuple[str, str]:
    """Run the real in-browser drive with the vision loop replaced by a fixed stop summary, and return the
    terminal state and the status message the run stores."""
    init_db()
    with SessionLocal() as db:
        user = models.User(email=f"stop-cause-{uuid.uuid4().hex}@example.com", name="Stop")
        db.add(user)
        db.flush()
        run = models.OperatorRun(
            user_id=user.id,
            goal="Apply for the job",
            target_url="https://example.test",
            current_state="RUNNING",
            confidence_state={"in_browser": {"device_id": f"stop-{uuid.uuid4().hex}", "execution_surface": "browser"}},
        )
        db.add(run)
        db.flush()
        thread = models.ChatThread(user_id=user.id, run_id=run.id, title="stop", mode="run")
        db.add(thread)
        db.commit()
        run_id, thread_id = int(run.id), int(thread.id)

    async def fixed_loop(*args, **kwargs) -> dict:
        return dict(summary)

    monkeypatch.setattr(extension_run, "run_vision_loop", fixed_loop)
    result = asyncio.run(
        drive_in_browser_application(
            body=types.SimpleNamespace(device_id=None),
            run_id=run_id,
            thread_id=thread_id,
            url=None,
            skip_navigate=True,
        )
    )
    with SessionLocal() as db:
        stored = db.get(models.OperatorRun, run_id)
        return str(result.get("state")), str(stored.status_message)


# --- what counts as a provider failure ---


def test_only_recorded_provider_failures_count_as_provider_failures() -> None:
    assert extension_run._is_provider_failure({"reason": "vision_model_timeout"})
    assert extension_run._is_provider_failure({"reason": "vision_model_unavailable", "model_failure_reason": "no_model_action"})
    assert extension_run._is_provider_failure({"reason": "no_progress", "model_failure_reason": "provider_empty_output"})
    assert extension_run._is_provider_failure({"reason": "agent_confused", "model_failure_reason": "internal_model_error"})

    # A page that cycled, a refused action, or a body fault is not a provider failure.
    assert not extension_run._is_provider_failure({"reason": "no_progress", "model_failure_reason": "page_state_cycle_exhausted"})
    assert not extension_run._is_provider_failure({"reason": "no_progress", "model_failure_reason": "recovery_constraint_ignored"})
    assert not extension_run._is_provider_failure({"reason": "agent_confused", "model_failure_reason": "schema_invalid"})
    assert not extension_run._is_provider_failure({"reason": "evidence_retention", "model_failure_reason": "evidence_retention"})


def test_the_refusal_and_unchanged_outcome_sets_never_include_provider_outcomes() -> None:
    provider_outcomes = {"provider_error", "timeout_inflight", "empty", "empty_length"}
    assert not (RUNTIME_REFUSAL_OUTCOMES & provider_outcomes)
    assert not (PAGE_UNCHANGED_OUTCOMES & provider_outcomes)


# --- the sentence each recorded cause gets ---


def test_a_cycle_after_harness_refusals_names_the_page_and_the_refusals() -> None:
    summary = _blocked("no_progress", trail=_cycle_trail_with_refusals(), original_reason="page_state_cycle_exhausted")

    sentence = extension_run._stop_cause_sentence(summary)

    assert sentence.startswith("I stopped after 4 action(s) without progress: the page did not change and my last actions")
    assert "refused by a harness check" in sentence
    assert "this kind of action on this control was repeated with no verified change" in sentence
    assert "the control no longer matched the one I had bound" in sentence


def test_refusals_alone_name_the_refusal_without_claiming_the_page_stayed_put() -> None:
    summary = _blocked(
        "no_progress",
        trail=[_step("executed"), _step("pre_action_revalidation_failed", "click_target_drift")],
        actions=1,
    )

    assert extension_run._stop_cause_sentence(summary) == (
        "I stopped after 1 action(s) without progress: my last actions were refused by a harness check "
        "(the click point no longer matched the control I had bound)."
    )


def test_an_unchanged_page_without_refusals_says_the_page_did_not_change() -> None:
    summary = _blocked("no_progress", trail=[_step("executed"), _step("page_state_cycle_exhausted")], actions=2)

    assert extension_run._stop_cause_sentence(summary) == "I stopped after 2 action(s) without progress: the page did not change."


def test_an_action_without_verified_progress_is_not_named_an_unchanged_page() -> None:
    summary = _blocked("no_progress", trail=[_step("executed"), _step("executed_without_durable_progress")], actions=2)

    sentence = extension_run._stop_cause_sentence(summary)

    assert sentence == "I stopped after 2 action(s) without progress: there was no verified progress."
    assert "page did not change" not in sentence


def test_an_unlisted_refusal_code_is_shown_in_plain_words() -> None:
    summary = _blocked("no_progress", trail=[_step("sequence_invalid", "scroll_distance_unverified")], actions=0)

    assert extension_run._stop_cause_sentence(summary) == (
        "I stopped without progress: my last actions were refused by a harness check (scroll distance unverified)."
    )


def test_a_stop_with_no_recorded_cause_gets_no_cause_sentence() -> None:
    assert extension_run._stop_cause_sentence(_blocked("no_progress", trail=[], actions=3)) == ""
    assert extension_run._stop_cause_sentence(_blocked("evidence_retention", trail=_cycle_trail_with_refusals())) == ""


# --- the stored message, through the real drive with the loop's stop summary ---


def test_a_provider_timeout_still_names_the_provider(monkeypatch) -> None:
    summary = _blocked(
        "vision_model_timeout",
        trail=[_step("executed"), _step("provider_error")],
        block_kind="awaiting_engineering",
        system_block=True,
        model_failure_reason="provider_timeout",
    )

    state, message = _drive_with_summary(monkeypatch, summary)

    assert state == "BLOCKED"
    assert message == (
        "I stopped because the browser agent hit an internal/provider failure (vision_model_timeout). "
        f"{_ENGINEERING_TAIL} The page is left for review."
    )


def test_a_cycle_after_refusals_is_not_reported_as_a_provider_failure(monkeypatch) -> None:
    summary = _blocked(
        "no_progress",
        trail=_cycle_trail_with_refusals(),
        original_reason="page_state_cycle_exhausted",
        block_kind="awaiting_engineering",
        system_block=True,
        model_failure_reason="page_state_cycle_exhausted",
    )

    state, message = _drive_with_summary(monkeypatch, summary)

    assert state == "BLOCKED"
    assert _PROVIDER_TAIL not in message
    assert _ENGINEERING_TAIL not in message
    assert message.startswith("I stopped after 4 action(s) without progress: the page did not change and my last actions")
    assert message.endswith(" The page is left for review.")


def test_a_plain_no_progress_stop_says_the_page_did_not_change(monkeypatch) -> None:
    summary = _blocked("no_progress", trail=[_step("executed"), _step("page_state_cycle_exhausted")], actions=2)

    state, message = _drive_with_summary(monkeypatch, summary)

    assert state == "BLOCKED"
    assert message == "I stopped after 2 action(s) without progress: the page did not change. The page is left for your review."


def test_a_system_stop_with_no_recorded_cause_is_an_internal_error_not_a_provider_failure(monkeypatch) -> None:
    summary = _blocked(
        "evidence_retention",
        trail=[],
        actions=1,
        block_kind="awaiting_engineering",
        system_block=True,
        model_failure_reason="evidence_retention",
    )

    _state, message = _drive_with_summary(monkeypatch, summary)

    assert message == (
        "I stopped because the browser agent hit an internal error (evidence_retention). "
        f"{_ENGINEERING_TAIL} The page is left for review."
    )


def test_an_unchanged_page_stop_from_the_real_loop_carries_its_recorded_outcomes(monkeypatch) -> None:
    # Zooms on a page that never changes: the loop's own cycle detector ends the run, and the summary it
    # hands the drive carries the recorded outcomes the message is built from.
    summary, _decisions, _events = _run_reads_only(
        monkeypatch,
        _StaticImageBody(),
        lambda n: _zoom(*_ZOOM_REGIONS[n % len(_ZOOM_REGIONS)]),
    )

    trail = summary["recent_step_outcomes"]
    assert trail and trail[-1]["outcome"] == "page_state_cycle_exhausted"
    assert len(trail) <= 6
    assert not extension_run._is_provider_failure(summary)
    assert "the page did not change" in extension_run._stop_cause_sentence(summary)


class _AlwaysRefusedBody(_PreActionRevalidationBody):
    """The body withholds every click before it reaches the page, with the harness's own reason code."""

    async def vision_click_control(self, selector: str, fallback_x: int, fallback_y: int, **_binding: object) -> dict:
        self.control_clicks.append((selector, fallback_x, fallback_y))
        return {
            "ok": False,
            "action": "click",
            "mode": "control_selector",
            "failure_reason": "click_target_drift",
            "activation_attempted": False,
            "activation_dispatched": False,
            "activation_count": 0,
        }


def test_a_run_the_body_keeps_refusing_stops_with_the_refusal_code_in_its_trail(monkeypatch) -> None:
    init_db()
    body = _AlwaysRefusedBody()
    click = {"action": "click", "control": 1, "x": 220, "y": 140, "text": "", "reason": "Open the delivery options."}
    monkeypatch.setattr("app.candidate_context.CandidateContextEngine", _DummyContextEngine)
    monkeypatch.setattr(extension_run, "_db_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(extension_run, "_record_context_usage_for_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(extension_run, "_decide_vision_action", lambda *args, **kwargs: dict(click))

    async def narrate(kind: str, data: dict) -> None:
        pass

    summary = asyncio.run(
        run_vision_loop(body, url=None, narrate=narrate, run_id=0, user_id=0, skip_navigate=True, max_steps=40)
    )

    # The page never changes, so the cycle detector ends the run after the refused clicks.
    assert summary["state"] == "BLOCKED"
    assert summary["reason"] == "no_progress"
    assert summary["original_reason"] == "page_state_cycle_exhausted"
    trail = summary["recent_step_outcomes"]
    assert trail[-1] == {"outcome": "page_state_cycle_exhausted", "code": ""}
    refusals = [entry for entry in trail if entry["outcome"] == "pre_action_revalidation_failed"]
    assert len(refusals) >= 3
    assert all(entry["code"] == "click_target_drift" for entry in refusals)
    assert not extension_run._is_provider_failure(summary)
    assert extension_run._stop_cause_sentence(summary) == (
        f"I stopped after {summary['actions']} action(s) without progress: the page did not change and my last "
        "actions were refused by a harness check (the click point no longer matched the control I had bound)."
    )
