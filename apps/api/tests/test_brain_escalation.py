"""Per-task brain escalation: a stuck part of a task goes to the smarter model, then back."""

from __future__ import annotations

import asyncio
import base64
import uuid

from app import extension_run, models
from app.agent_settings import get_agent_settings, update_agent_settings
from app.brain_escalation import (
    ESCALATION_FAILURE_THRESHOLD,
    SELF_RECOVERY_LIMIT,
    SELF_RECOVERY_NEVER_REASONS,
    BrainEscalation,
    can_self_recover,
    is_brain_failure_stop,
)
from app.extension_run import _FINAL_APPROVAL_CONTEXT_REFUSAL_LIMIT
from app.db import SessionLocal, init_db
from app.extension_run import run_vision_loop
from app.model_provider import ModelProviderService, ResolvedModelProvider

from tests.test_extension_run import (  # noqa: F401 - the autouse fixtures apply to this module too
    _ChoiceBody,
    _DummyContextEngine,
    _complete_final_action_readiness_audit_by_default,
    _ordinary_click_consequence_by_default,
    _ordinary_secret_semantics_by_default,
    _resolved_vision_provider,
)


def test_failures_escalate_and_progress_hands_the_task_back() -> None:
    state = BrainEscalation(enabled=True)
    for _ in range(ESCALATION_FAILURE_THRESHOLD - 1):
        state.record_outcome("target_binding_rejected")
    state.record_outcome("executed")
    state.record_outcome("provider_error")
    assert not state.should_escalate()
    state.record_outcome("executed_without_durable_progress")
    assert state.should_escalate()

    state.mark_escalated()
    assert not state.should_escalate()
    assert not state.should_deescalate()
    state.record_progress()
    assert state.should_deescalate()
    state.mark_deescalated()
    assert (state.active, state.failures, state.escalations) == (False, 0, 1)


def test_progress_resets_failures_and_disabled_never_escalates() -> None:
    state = BrainEscalation(enabled=True)
    state.record_outcome("invalid")
    state.record_outcome("invalid")
    state.record_progress()
    state.record_outcome("invalid")
    assert not state.should_escalate()

    disabled = BrainEscalation(enabled=False)
    for _ in range(10):
        disabled.record_outcome("invalid")
    assert not disabled.should_escalate()


def test_only_brain_failure_stops_are_escalation_points() -> None:
    assert is_brain_failure_stop({"state": "BLOCKED", "reason": "no_progress"})
    assert is_brain_failure_stop({"state": "BLOCKED", "reason": "agent_confused"})
    assert not is_brain_failure_stop({"state": "BLOCKED", "reason": "no_progress", "needs_user": True})
    assert not is_brain_failure_stop({"state": "BLOCKED", "reason": "vision_model_timeout"})
    assert not is_brain_failure_stop({"state": "READY_FOR_REVIEW", "reason": "no_progress"})
    assert not is_brain_failure_stop(None)


def _provider_row(db, *, provider_type: str, model_name: str, keyed: bool = False) -> int:
    row = models.ModelProvider(
        display_name=f"Escalation test {uuid.uuid4().hex[:8]}",
        provider_type=provider_type,
        model_name=model_name,
        base_url="https://provider.example.test/v1",
        encrypted_api_key="sealed" if keyed else None,
        enabled=True,
        is_active=False,
    )
    db.add(row)
    db.flush()
    return row.id


def _user(db) -> int:
    user = models.User(email=f"escalation-{uuid.uuid4().hex[:8]}@example.com", name="Escalation Test")
    db.add(user)
    db.flush()
    return user.id


def test_escalation_designation_is_cleared_when_it_is_the_primary_brain() -> None:
    init_db()
    with SessionLocal() as db:
        user_id = _user(db)
        brain = _provider_row(db, provider_type="openrouter", model_name="escalation-test/brain", keyed=True)
        smart = _provider_row(db, provider_type="openrouter", model_name="escalation-test/smart")
        settings = update_agent_settings(db, user_id, {"vision_provider_id": brain, "vision_escalation_provider_id": brain})
        assert settings["vision_escalation_provider_id"] is None
        settings = update_agent_settings(db, user_id, {"vision_escalation_provider_id": smart})
        assert settings["vision_escalation_provider_id"] == smart
        assert get_agent_settings(db, user_id)["vision_escalation_provider_id"] == smart
        # The escalation brain decides steps, so it can never also be the Checker (the secret gate's second opinion).
        settings = update_agent_settings(db, user_id, {"judge_provider_id": smart})
        assert settings["judge_provider_id"] == smart
        assert settings["vision_escalation_provider_id"] is None
        db.rollback()


def test_escalation_provider_is_designation_only_and_must_carry_a_screenshot(monkeypatch) -> None:
    init_db()
    with SessionLocal() as db:
        user_id = _user(db)
        service = ModelProviderService(db)
        monkeypatch.setattr(
            service,
            "resolve_provider",
            lambda row: _resolved_vision_provider(
                row.id,
                display_name=row.display_name,
                provider_type=row.provider_type,
                model_name=row.model_name,
            ),
        )
        assert service.escalation_provider(user_id=user_id) is None
        smart = _provider_row(db, provider_type="openrouter", model_name="anthropic/claude-sonnet-5.5")
        update_agent_settings(db, user_id, {"vision_escalation_provider_id": smart})
        chosen = service.escalation_provider(user_id=user_id)
        assert chosen is not None and chosen.id == smart
        text_only = _provider_row(db, provider_type="deepseek", model_name="deepseek-v4-flash")
        update_agent_settings(db, user_id, {"vision_escalation_provider_id": text_only})
        assert service.escalation_provider(user_id=user_id) is None
        # Settings written around the validation (an older row) still never make the Checker the brain.
        profile = db.query(models.UserProfile).filter_by(user_id=user_id).one()
        profile.agent_settings_json = {"vision_escalation_provider_id": smart, "judge_provider_id": smart}
        db.flush()
        assert service.escalation_provider(user_id=user_id) is None
        # A legacy adjudicator seat is the Checker now, so it excludes the same row.
        profile.agent_settings_json = {"vision_escalation_provider_id": smart, "secret_adjudicator_provider_id": smart}
        db.flush()
        assert service.escalation_provider(user_id=user_id) is None
        db.rollback()


def test_refused_final_actions_count_and_empty_answers_do_not() -> None:
    state = BrainEscalation(enabled=True)
    state.record_outcome("no_model_action")
    state.record_outcome("final_action_rebind_required")
    assert state.failures == 0
    for _ in range(ESCALATION_FAILURE_THRESHOLD):
        state.record_outcome("final_approval_context_ungrounded")
    assert state.should_escalate()


class _AdvancingChoiceBody(_ChoiceBody):
    """A click that opens the next page: durable progress."""

    async def capture_screenshot(self) -> dict:
        shot = await super().capture_screenshot()
        if self.clicked:
            shot = {**shot, "url": "https://example.test/apply/review", "title": "Review"}
        return shot

    async def vision_click(self, x: int, y: int, **expected_binding: object) -> dict:
        result = await super().vision_click(x, y, **expected_binding)
        return {**result, "url_changed": True, "title_changed": True}


def _run_with_escalation(
    monkeypatch,
    *,
    primary_actions,
    smart_actions,
    escalation: ResolvedModelProvider | None,
    checker: ResolvedModelProvider | None = None,
    body=None,
    contexts: list[dict] | None = None,
    run_id: int = 0,
    user_id: int = 0,
    max_steps: int = 10,
):
    init_db()
    body = body if body is not None else _AdvancingChoiceBody()
    decided_by: list[str] = []
    db_events: list[tuple[str, dict]] = []
    primary = _resolved_vision_provider(
        501, display_name="DeepSeek V4.1 Flash", provider_type="openrouter", model_name="deepseek-v4.1-flash"
    )
    primary_iter = iter(primary_actions)
    smart_iter = iter(smart_actions)

    async def narrate(kind: str, data: dict) -> None:
        pass

    async def fake_save(profile: dict, *, force: bool = False, run_id: int | None = None) -> bool:
        return True

    def fake_decide(user_id: int, image: dict, view: dict, context: dict) -> dict:
        if contexts is not None:
            contexts.append(context)
        selected = context.get("_vision_provider_override") or primary
        # An exhausted script ends the run at once: StopIteration cannot cross asyncio.to_thread, so a
        # bare next() hung the step until its outer timeout (one test ran 70 s).
        if escalation is not None and selected is escalation:
            decided_by.append("smart")
            return next(smart_iter, {"action": "done", "reason": "Script exhausted."})
        assert selected is primary
        decided_by.append("primary")
        return next(primary_iter, {"action": "done", "reason": "Script exhausted."})

    monkeypatch.setattr("app.candidate_context.CandidateContextEngine", _DummyContextEngine)
    monkeypatch.setattr(extension_run, "_vision_profile_key_for_user", lambda user_id: ("openrouter", primary.model_name))
    monkeypatch.setattr(
        extension_run,
        "_load_vision_decision_profile",
        lambda provider_type, model_name: {
            "provider_type": provider_type,
            "model_name": model_name,
            "ewma_latency_seconds": None,
            "token_budget": None,
            "latency_ring_seconds": [],
        },
    )
    monkeypatch.setattr(extension_run, "_save_vision_decision_profile_async", fake_save)
    monkeypatch.setattr(extension_run, "_vision_provider_for_user", lambda user_id: primary)
    monkeypatch.setattr(extension_run, "_vision_escalation_provider_for_user", lambda user_id: escalation)
    monkeypatch.setattr(extension_run, "_judge_provider_for_user", lambda user_id: checker)
    monkeypatch.setattr(extension_run, "_vision_backup_provider", lambda *args, **kwargs: None)
    monkeypatch.setattr(extension_run, "_db_event", lambda run_id, event_type, payload: db_events.append((event_type, payload)))
    monkeypatch.setattr(extension_run, "_record_context_usage_for_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(extension_run, "_decide_vision_action", fake_decide)

    summary = asyncio.run(
        run_vision_loop(body, url=None, narrate=narrate, run_id=run_id, user_id=user_id, skip_navigate=True, max_steps=max_steps)
    )
    return summary, decided_by, db_events, body


_INVALID = {"_outcome_type": "schema_invalid", "invalid_reason": "unsupported_action", "action": "teleport"}
_CLICK = {"action": "click", "x": 20, "y": 100, "text": "", "reason": "Open the visible choice."}
_DONE = {"action": "done", "x": 0, "y": 0, "artifact_role": "", "reason": "The requested review state is ready."}


def test_stuck_primary_hands_the_step_to_the_smart_model_and_takes_the_rest_back(monkeypatch) -> None:
    smart = _resolved_vision_provider(
        502, display_name="Claude Sonnet 5.5", provider_type="openrouter", model_name="anthropic/claude-sonnet-5.5"
    )
    summary, decided_by, db_events, body = _run_with_escalation(
        monkeypatch,
        primary_actions=[_INVALID, _INVALID, _INVALID, _DONE],
        smart_actions=[_CLICK],
        escalation=smart,
    )

    assert summary["state"] != "BLOCKED", summary
    assert body.clicks == [(20, 100)]
    assert decided_by == ["primary", "primary", "primary", "smart", "primary"]
    events = [event for event, _ in db_events]
    escalated = [payload for event, payload in db_events if event == "VISION_BRAIN_ESCALATED"]
    returned = [payload for event, payload in db_events if event == "VISION_BRAIN_DEESCALATED"]
    assert [(p["from_provider_id"], p["to_provider_id"], p["reason"]) for p in escalated] == [
        (501, 502, "invalid_output")
    ]
    assert [(p["from_provider_id"], p["to_provider_id"]) for p in returned] == [(502, 501)]
    assert events.index("VISION_BRAIN_ESCALATED") < events.index("VISION_BRAIN_DEESCALATED")


_TIMEOUT = {
    "_outcome_type": "provider_transport_error",
    "failure_reason": "provider_timeout",
    "error": "inner provider timeout",
    "_model_metadata": {"provider": "openrouter", "model": "anthropic/claude-sonnet-5.5"},
}


def test_a_failing_escalation_model_hands_the_task_back_to_the_primary(monkeypatch) -> None:
    monkeypatch.setattr(extension_run, "_PROVIDER_TRANSPORT_RETRY_LIMIT", 2)
    monkeypatch.setattr(extension_run, "_PROVIDER_TRANSPORT_BACKOFF_CAP_SECONDS", 0)
    smart = _resolved_vision_provider(
        502, display_name="Claude Sonnet 5.5", provider_type="openrouter", model_name="anthropic/claude-sonnet-5.5"
    )
    summary, decided_by, db_events, body = _run_with_escalation(
        monkeypatch,
        primary_actions=[_INVALID, _INVALID, _INVALID, _CLICK, _DONE],
        smart_actions=[_TIMEOUT, _TIMEOUT, _TIMEOUT],
        escalation=smart,
    )

    assert summary["state"] != "BLOCKED", summary
    assert body.clicks == [(20, 100)]
    returned = [payload for event, payload in db_events if event == "VISION_BRAIN_DEESCALATED"]
    assert [(p["from_provider_id"], p["to_provider_id"]) for p in returned] == [(502, 501)]
    assert returned[0]["reason"].startswith("escalation_provider_failed:")
    assert decided_by[:3] == ["primary", "primary", "primary"]
    assert decided_by[-2:] == ["primary", "primary"]
    assert "smart" in decided_by
    assert not [event for event, _ in db_events if event == "VISION_PROVIDER_FAILOVER"]


def test_an_escalation_model_that_is_the_checkers_model_never_takes_a_step(monkeypatch) -> None:
    smart = _resolved_vision_provider(
        502, display_name="Claude Sonnet 5.5", provider_type="openrouter", model_name="anthropic/claude-sonnet-5.5"
    )
    # Another row serving the same model is the same model: the Checker must stay a different model from
    # whichever brain decides the step, so this escalation is off for the run.
    checker = _resolved_vision_provider(
        503, display_name="Claude Sonnet 5.5 (checker)", provider_type="openrouter", model_name="anthropic/claude-sonnet-5.5"
    )
    summary, decided_by, db_events, body = _run_with_escalation(
        monkeypatch,
        primary_actions=[_INVALID, _INVALID, _INVALID, _DONE],
        smart_actions=[_CLICK],
        escalation=smart,
        checker=checker,
    )

    assert "smart" not in decided_by
    assert not [event for event, _ in db_events if event.startswith("VISION_BRAIN_")]


def test_without_an_escalation_model_an_engineering_stop_recovers_twice_then_blocks(monkeypatch) -> None:
    contexts: list[dict] = []
    summary, decided_by, db_events, body = _run_with_escalation(
        monkeypatch,
        primary_actions=[_INVALID] * 9,
        smart_actions=[],
        escalation=None,
        contexts=contexts,
    )

    # Three invalid replies end a budget; the stop recovers in-loop twice, and the third budget ends the run.
    assert summary["state"] == "BLOCKED"
    assert summary["reason"] == "agent_confused"
    assert decided_by == ["primary"] * 9
    recoveries = [payload for event, payload in db_events if event == "VISION_SELF_RECOVERY"]
    assert [(p["reason"], p["count"], p["escalated"]) for p in recoveries] == [
        ("agent_confused", 1, False),
        ("agent_confused", 2, False),
    ]
    # The brain is told what was stuck, and the next decision after the first recovery sees it.
    assert any(
        item.startswith("self_recovery 1 of 2: ") and "stop agent_confused" in item and "unsupported_action" in item
        for context in contexts[3:]
        for item in context.get("recent_actions", [])
    )
    assert not [event for event, _ in db_events if event.startswith("VISION_BRAIN_")]


def test_a_recovery_does_not_escalate_again_while_the_escalation_brain_holds_the_task(monkeypatch) -> None:
    smart = _resolved_vision_provider(
        502, display_name="Claude Sonnet 5.5", provider_type="openrouter", model_name="anthropic/claude-sonnet-5.5"
    )
    summary, decided_by, db_events, body = _run_with_escalation(
        monkeypatch,
        primary_actions=[_INVALID] * 3,
        smart_actions=[_INVALID] * 9,
        escalation=smart,
        max_steps=14,
    )

    # The primary's stuck part goes to the escalation brain once; its own stops then recover in-loop.
    assert summary["state"] == "BLOCKED"
    assert summary["reason"] == "agent_confused"
    assert decided_by == ["primary"] * 3 + ["smart"] * 9
    escalated = [payload for event, payload in db_events if event == "VISION_BRAIN_ESCALATED"]
    assert [p["reason"] for p in escalated] == ["invalid_output"]
    recoveries = [payload for event, payload in db_events if event == "VISION_SELF_RECOVERY"]
    assert [(p["count"], p["escalated"]) for p in recoveries] == [(1, False), (2, False)]


def test_the_recovery_count_survives_a_loop_restart(monkeypatch) -> None:
    """A resumed or restarted loop of the same run keeps the recoveries already spent (run journal)."""
    stored: dict[str, dict] = {}

    def load_journal(run_id):
        empty = {
            "notes": [], "plan": "", "page_memory": [], "control_memory": [], "verified_receipts": [],
            "steps_before": 0, "delivered_answer": "", "pending_answer": "", "self_recoveries": 0,
        }
        return {**empty, **stored.get("journal", {})}

    def persist_journal(run_id, journal):
        stored["journal"] = dict(journal)

    monkeypatch.setattr(extension_run, "_load_run_journal_for_run", load_journal)
    monkeypatch.setattr(extension_run, "_persist_run_journal_for_run", persist_journal)

    # First loop: two stops recover in-loop, then the step cap ends it.
    first, first_decided, _, _ = _run_with_escalation(
        monkeypatch, primary_actions=[_INVALID] * 6, smart_actions=[], escalation=None, max_steps=6
    )
    assert first["reason"] == "max_steps"
    assert first_decided == ["primary"] * 6
    assert stored["journal"]["self_recoveries"] == SELF_RECOVERY_LIMIT

    # Restarted loop: the budget is already spent, so the next stop ends the run at once.
    second, second_decided, second_events, _ = _run_with_escalation(
        monkeypatch, primary_actions=[_INVALID] * 3, smart_actions=[], escalation=None, max_steps=10
    )
    assert second["state"] == "BLOCKED"
    assert second["reason"] == "agent_confused"
    assert second_decided == ["primary"] * 3
    assert not [event for event, _ in second_events if event == "VISION_SELF_RECOVERY"]


def test_self_recovery_covers_only_engineering_stops_within_the_limit() -> None:
    assert can_self_recover("no_progress", 0)
    assert can_self_recover("agent_confused", 0)
    assert can_self_recover("scroll_execution_outcome_unknown", SELF_RECOVERY_LIMIT - 1)
    assert not can_self_recover("no_progress", SELF_RECOVERY_LIMIT)
    # Stops that are not engineering stops, and the excluded ones, block at once.
    for reason in (
        "vision_model_timeout",
        "payment_floor_confirmation",
        "needs_answer",
        "sensitive_browser_takeover",
        "sensitive_screenshot_mask_unavailable",
        "grounding_recovery_exhausted",
        "max_steps",
        "external_workflow_rejected",
        "runtime_stall",
        "user_cancelled",
    ):
        assert not can_self_recover(reason, 0), reason
    for reason in SELF_RECOVERY_NEVER_REASONS:
        assert not can_self_recover("no_progress", 0, original_reason=reason), reason


def test_a_refusal_code_in_the_never_set_blocks_recovery_of_a_sequence_stop() -> None:
    # A sequence-invalid stop carries the refusal code it stopped on; the stop reason itself is recoverable.
    assert can_self_recover("agent_confused", 0, codes=["invalid_output"])
    assert can_self_recover("agent_confused", 0, codes=["sequence_invalid"])
    for code in SELF_RECOVERY_NEVER_REASONS:
        assert not can_self_recover("agent_confused", 0, codes=[code]), code
        assert not can_self_recover("agent_confused", 0, codes=["invalid_output", code]), code


class _CommitOrderBody:
    """Each page has a consequential Commit order button and an ordinary Next page button."""

    def __init__(self) -> None:
        self.page = 1
        self.clicks: list[str] = []

    async def capture_screenshot(self) -> dict:
        return {
            "ok": True,
            "image": base64.b64encode(f"commit-order-page-{self.page}".encode()).decode(),
            "width": 800,
            "height": 600,
            "url": f"https://example.test/order/{self.page}",
            "title": f"Order step {self.page}",
            "scroll_y": 0,
            "scroll_height": 600,
            "page_evidence": {
                "viewport_text": f"Order step {self.page}. Commit order places the order for 2 items.",
                "controls": [
                    {
                        "index": 1,
                        "tag": "button",
                        "type": "button",
                        "role": "button",
                        "text": "Commit order",
                        "context_text": "Order review",
                        "selector": "#commit-order",
                        "x": 250,
                        "y": 180,
                        "width": 300,
                        "height": 48,
                    },
                    {
                        "index": 2,
                        "tag": "button",
                        "type": "button",
                        "role": "button",
                        "text": "Next page",
                        "context_text": "Order review",
                        "selector": "#next-page",
                        "x": 250,
                        "y": 300,
                        "width": 300,
                        "height": 48,
                    },
                ],
            },
        }

    async def vision_click(self, x: int, y: int, **_expected_binding: object) -> dict:
        self.clicks.append(f"{x},{y}")
        self.page += 1
        return {"ok": True, "action": "click", "surface_changed": True, "url_changed": True, "title_changed": True}

    async def vision_click_control(self, selector: str, fallback_x: int, fallback_y: int, **_expected_binding: object) -> dict:
        self.clicks.append(selector)
        self.page += 1
        return {
            "ok": True,
            "action": "click",
            "mode": "control_selector",
            "control_selector_resolved": True,
            "surface_changed": True,
            "url_changed": True,
            "title_changed": True,
        }


# Run 1576 shape: a consequential click, autonomous authority, a payment verdict that stayed unknown, and a
# brain that never fills the approval context. The grounding requirement holds; only the loop is bounded.
_COMMIT_UNGROUNDED = {"action": "click", "control": 1, "x": 400, "y": 204, "text": "", "reason": "Commit the order."}
_NEXT_PAGE = {"action": "click", "control": 2, "x": 400, "y": 324, "text": "", "reason": "Open the next order step."}


def _commit_is_consequential(monkeypatch) -> None:
    def judge(*args, **kwargs):
        consequential = kwargs.get("selector") == "#commit-order"
        return {
            "classification": "irreversible_final" if consequential else "intermediate",
            "confidence": 0.95,
            "evidence": "This button places the order." if consequential else "This button opens the next step.",
            "metadata": {"provider": "fixture", "model": "fixture-click-risk", "attempt_count": 1},
        }

    monkeypatch.setattr(extension_run, "_judge_click_consequence", judge)
    monkeypatch.setattr(extension_run, "_final_action_mode_for_run", lambda run_id, user_id: "autonomous")
    monkeypatch.setattr(
        extension_run,
        "_payment_floor_verdict",
        lambda *args, **kwargs: {
            "payment": True,
            "payment_unknown": True,
            "deterministic_hit": False,
            "judge_payment": True,
            "judge_reason": "fixture_judge_unavailable",
            "fail_closed": True,
            "summary": "",
        },
    )


def _refusals(db_events) -> list[tuple[int, bool]]:
    return [
        (payload["refusal"], payload["exhausted"])
        for event, payload in db_events
        if event == "FINAL_APPROVAL_CONTEXT_REFUSED"
    ]


def test_ungrounded_final_nominations_stop_as_a_brain_failure(monkeypatch) -> None:
    _commit_is_consequential(monkeypatch)
    contexts: list[dict] = []
    summary, decided_by, db_events, body = _run_with_escalation(
        monkeypatch,
        primary_actions=[_COMMIT_UNGROUNDED] * 10,
        smart_actions=[],
        escalation=None,
        body=_CommitOrderBody(),
        contexts=contexts,
    )

    limit = _FINAL_APPROVAL_CONTEXT_REFUSAL_LIMIT
    assert summary["state"] == "BLOCKED"
    assert summary["reason"] == "no_progress"
    assert summary["model_failure_reason"] == "final_approval_context_ungrounded"
    assert summary["final_action_label"] == "Commit order"
    assert is_brain_failure_stop(summary)
    assert decided_by == ["primary"] * limit
    assert body.clicks == []
    assert not [event for event, _ in db_events if event == "FINAL_ACTION_INTERCEPTED"]
    assert _refusals(db_events) == [(n, n == limit) for n in range(1, limit + 1)]
    feedback = contexts[1]["recent_actions"][-1]
    assert feedback.startswith(f"final_approval_context_ungrounded (refusal 1 of {limit})")
    assert '"Commit order" (control 1)' in feedback
    assert "approval_context_summary was empty; approval_context_evidence was empty" in feedback


def test_ungrounded_final_nominations_escalate_before_the_run_stops(monkeypatch) -> None:
    _commit_is_consequential(monkeypatch)
    smart = _resolved_vision_provider(
        502, display_name="Claude Sonnet 5.5", provider_type="openrouter", model_name="anthropic/claude-sonnet-5.5"
    )
    summary, decided_by, db_events, body = _run_with_escalation(
        monkeypatch,
        primary_actions=[_COMMIT_UNGROUNDED] * 10,
        smart_actions=[_COMMIT_UNGROUNDED] * 10,
        escalation=smart,
        body=_CommitOrderBody(),
    )

    limit = _FINAL_APPROVAL_CONTEXT_REFUSAL_LIMIT
    assert summary["state"] == "BLOCKED"
    assert summary["model_failure_reason"] == "final_approval_context_ungrounded"
    assert decided_by == ["primary"] * limit + ["smart"] * limit
    assert body.clicks == []
    escalated = [payload for event, payload in db_events if event == "VISION_BRAIN_ESCALATED"]
    assert len(escalated) == 1
    # The escalated brain gets the whole refusal budget again.
    assert [refusal for refusal, _ in _refusals(db_events)] == list(range(1, limit + 1)) * 2


def test_durable_progress_restarts_the_refusal_budget(monkeypatch) -> None:
    _commit_is_consequential(monkeypatch)
    below_limit = [_COMMIT_UNGROUNDED] * (_FINAL_APPROVAL_CONTEXT_REFUSAL_LIMIT - 1)
    summary, decided_by, db_events, body = _run_with_escalation(
        monkeypatch,
        # done is rejected a few times while the page still shows controls; that governor is not under test.
        primary_actions=below_limit + [_NEXT_PAGE] + below_limit + [_DONE] * 5,
        smart_actions=[],
        escalation=None,
        body=_CommitOrderBody(),
    )

    assert summary["state"] != "BLOCKED", summary
    assert body.clicks == ["#next-page"]
    assert [refusal for refusal, _ in _refusals(db_events)] == list(range(1, _FINAL_APPROVAL_CONTEXT_REFUSAL_LIMIT)) * 2


_CAPPED_EMPTY = {
    "_outcome_type": "empty_completion",
    "failure_reason": "provider_empty_output",
    "_model_metadata": {
        "provider": "openrouter",
        "model": "deepseek-v4.1-flash",
        "parse_status": "empty",
        "raw_output_empty": True,
        "raw_output_chars": 0,
        "finish_reason": "length",
        "max_tokens": 8192,
    },
}


def test_a_brain_that_keeps_reasoning_past_its_budget_hands_the_part_to_the_escalation_model(monkeypatch) -> None:
    # v2_pointer_hell: DeepSeek reasoned into its output cap again and again on a geometric part; the
    # capped replies went to the backup provider, never to the escalation brain.
    smart = _resolved_vision_provider(
        502, display_name="GLM 5.3 Flash", provider_type="openrouter", model_name="z-ai/glm-5.3-flash"
    )
    summary, decided_by, db_events, body = _run_with_escalation(
        monkeypatch,
        # Each step's capped reply gets its in-step retry, which caps again: three unrecovered steps.
        primary_actions=[_CAPPED_EMPTY] * 6 + [_DONE],
        smart_actions=[_CLICK],
        escalation=smart,
    )

    assert decided_by[:7] == ["primary"] * 6 + ["smart"]
    assert body.clicks == [(20, 100)]
    escalated = [payload for event, payload in db_events if event == "VISION_BRAIN_ESCALATED"]
    assert escalated and escalated[0]["to_provider_id"] == 502
    assert not [event for event, _ in db_events if event == "VISION_PROVIDER_FAILOVER"]


def test_capped_empties_and_inflight_timeouts_count_as_brain_failures() -> None:
    state = BrainEscalation(enabled=True)
    state.record_outcome("empty")
    state.record_outcome("provider_error")
    assert state.failures == 0
    state.record_outcome("empty_length")
    state.record_outcome("timeout_inflight")
    state.record_outcome("empty_length")
    assert state.should_escalate()


def test_a_no_progress_step_that_reached_a_new_screen_is_not_a_failure() -> None:
    state = BrainEscalation(enabled=True)
    state.record_outcome("executed_without_durable_progress")
    state.settle_no_progress(reached_new_state=True)
    assert state.failures == 0
    # A return to a screen already seen still counts, and only the step just recorded is settled.
    state.record_outcome("executed_without_durable_progress")
    state.settle_no_progress(reached_new_state=False)
    state.settle_no_progress(reached_new_state=True)
    assert state.failures == 1
    state.record_outcome("target_binding_rejected")
    state.settle_no_progress(reached_new_state=True)
    assert state.failures == 2


def test_paging_a_viewer_record_by_record_is_not_a_reason_to_escalate(monkeypatch) -> None:
    # v5_reconcile run 1719: each Next click showed the next invoice, yet every repeat counted as a brain
    # failure and the task went to the escalation model twice while reading invoices in order.
    from tests.test_extension_run import _EngageBody

    smart = _resolved_vision_provider(
        502, display_name="Claude Sonnet 5.5", provider_type="openrouter", model_name="anthropic/claude-sonnet-5.5"
    )
    monkeypatch.setattr(
        extension_run,
        "_judge_click_consequence",
        lambda *args, **kwargs: {"classification": "ordinary_reversible", "confidence": 0.99, "evidence": "Fixture."},
    )
    press = {"action": "click", "control": 1, "x": 940, "y": 135, "reason": "Show the next record."}
    summary, decided_by, db_events, body = _run_with_escalation(
        monkeypatch,
        # Only presses: the run ends on its step budget, every press reaching a record not seen before.
        primary_actions=[press] * 12,
        smart_actions=[press] * 12,
        escalation=smart,
        body=_EngageBody(toggle=False),
    )

    outcomes = [payload.get("outcome") for event, payload in db_events if event == "STEP_TIMING"]
    # Each press is made from a record it was never pressed on, so none counts as a repeated no-op.
    assert "executed_without_durable_progress" not in outcomes, outcomes
    assert len(body.clicks) >= 6
    assert not [event for event, _ in db_events if event == "VISION_BRAIN_ESCALATED"], (outcomes, decided_by)
    assert "smart" not in decided_by


def test_a_streak_of_harness_refusals_is_not_a_brain_failure_stop() -> None:
    from app.brain_escalation import BRAIN_FAILURE_OUTCOMES, HARNESS_REFUSAL_REASONS

    assert "click_target_drift" in HARNESS_REFUSAL_REASONS
    assert "repeated_harness_refusal" not in BRAIN_FAILURE_OUTCOMES
    assert not is_brain_failure_stop({"state": "BLOCKED", "reason": "no_progress", "harness_refusal": True})
    assert is_brain_failure_stop({"state": "BLOCKED", "reason": "no_progress"})
    state = BrainEscalation(enabled=True)
    for _ in range(5):
        state.record_outcome("repeated_harness_refusal")
        state.record_outcome("pre_action_revalidation_failed")
    assert not state.should_escalate()


_BLIND_CLICK = {"action": "click", "x": 0, "y": 0, "text": "", "reason": "Open the visible choice."}


def test_a_brain_that_never_names_a_click_target_hands_the_part_to_the_escalation_model(monkeypatch) -> None:
    smart = _resolved_vision_provider(
        502, display_name="Claude Sonnet 5.5", provider_type="openrouter", model_name="anthropic/claude-sonnet-5.5"
    )
    summary, decided_by, db_events, body = _run_with_escalation(
        monkeypatch,
        primary_actions=[_BLIND_CLICK, _BLIND_CLICK, _BLIND_CLICK, _DONE],
        smart_actions=[_CLICK],
        escalation=smart,
    )

    assert summary.get("reason") != "vision_target_unresolved", summary
    assert body.clicks == [(20, 100)]
    assert decided_by[:4] == ["primary", "primary", "primary", "smart"]
    escalated = [payload for event, payload in db_events if event == "VISION_BRAIN_ESCALATED"]
    assert [p["reason"] for p in escalated] == ["target_unresolved"]


def test_a_blind_click_stop_is_unchanged_without_an_escalation_model(monkeypatch) -> None:
    summary, decided_by, db_events, body = _run_with_escalation(
        monkeypatch,
        primary_actions=[_BLIND_CLICK, _BLIND_CLICK, _BLIND_CLICK],
        smart_actions=[],
        escalation=None,
    )

    assert summary["state"] == "BLOCKED"
    assert summary["reason"] == "vision_target_unresolved"
    assert not [event for event, _ in db_events if event.startswith("VISION_BRAIN_")]


def test_stuck_episode_reset_covers_every_stop_counter() -> None:
    import inspect

    source = inspect.getsource(extension_run.run_vision_loop)
    source = source if "def _reset_stuck_episode_budgets" in source else inspect.getsource(extension_run._run_vision_loop_impl)
    start = source.index("def _reset_stuck_episode_budgets")
    helper = source[start : source.index("async def _deescalate_brain", start)]
    for counter in ("blind = 0", "grounding_recovery_replans.clear()", "final_submit_drift_refusals = 0"):
        assert counter in helper


def test_saved_escalation_state_round_trips_and_falls_back_without_an_escalation_brain() -> None:
    state = BrainEscalation(enabled=True)
    for _ in range(ESCALATION_FAILURE_THRESHOLD):
        state.record_outcome("invalid")
    state.mark_escalated()
    state.record_progress()
    saved = state.to_state()

    resumed = BrainEscalation(enabled=True)
    assert resumed.restore(saved) is True
    assert (resumed.active, resumed.progressed_while_active, resumed.escalations) == (True, True, 1)
    assert resumed.should_deescalate()

    # No escalation brain to hand the task to (row missing or disabled): the loop starts on the primary.
    no_brain = BrainEscalation(enabled=False)
    assert no_brain.restore(saved) is False
    assert (no_brain.active, no_brain.failures, no_brain.escalations) == (False, 0, 1)

    # A primary streak that never escalated carries its failure count into the resumed loop.
    streak = BrainEscalation(enabled=True)
    assert streak.restore({"active": False, "failures": 2, "escalations": 0}) is False
    streak.record_outcome("invalid")
    assert streak.should_escalate()

    assert BrainEscalation(enabled=True).restore("not a journal entry") is False


def _resumable_run() -> tuple[int, int]:
    init_db()
    with SessionLocal() as db:
        user_id = _user(db)
        run = models.OperatorRun(
            user_id=user_id, goal="Finish the stuck form", target_url="https://example.test/", current_state="RUNNING"
        )
        db.add(run)
        db.commit()
        return run.id, user_id


def _saved_escalation(run_id: int) -> dict:
    with SessionLocal() as db:
        journal = (db.get(models.OperatorRun, run_id).confidence_state or {}).get("vision_run_journal") or {}
        return dict(journal.get("escalation") or {})


def _stuck_first_loop(monkeypatch, *, run_id: int, user_id: int, smart) -> None:
    """Loop one: the primary fails three times on one part of the task, the escalation brain takes it,
    and the loop ends (max_steps) while that brain still holds it."""
    _, decided_by, _events, _body = _run_with_escalation(
        monkeypatch,
        primary_actions=[_INVALID, _INVALID, _INVALID],
        smart_actions=[_INVALID, _INVALID],
        escalation=smart,
        run_id=run_id,
        user_id=user_id,
        max_steps=4,
    )
    assert decided_by[:3] == ["primary", "primary", "primary"] and "smart" in decided_by, decided_by
    assert _saved_escalation(run_id)["active"] is True


def test_an_escalated_part_of_the_task_survives_a_loop_restart(monkeypatch) -> None:
    smart = _resolved_vision_provider(
        502, display_name="Claude Sonnet 5.5", provider_type="openrouter", model_name="anthropic/claude-sonnet-5.5"
    )
    run_id, user_id = _resumable_run()
    _stuck_first_loop(monkeypatch, run_id=run_id, user_id=user_id, smart=smart)

    # The resumed loop starts on the escalation brain: the primary never decides before it makes progress.
    _summary, decided_by, db_events, _body = _run_with_escalation(
        monkeypatch,
        primary_actions=[_DONE],
        smart_actions=[_INVALID, _INVALID],
        escalation=smart,
        run_id=run_id,
        user_id=user_id,
        max_steps=2,
    )
    assert decided_by == ["smart", "smart"], decided_by
    restored = [payload for event, payload in db_events if event == "VISION_BRAIN_ESCALATION_RESTORED"]
    assert [(p["from_provider_id"], p["to_provider_id"], p["escalation_index"]) for p in restored] == [(501, 502, 1)]
    assert not [event for event, _ in db_events if event == "VISION_BRAIN_ESCALATED"], "not escalated again"
    assert _saved_escalation(run_id)["active"] is True, "still held by the escalation brain when that loop ends"


def test_a_restored_escalation_hands_back_on_its_first_durable_progress(monkeypatch) -> None:
    smart = _resolved_vision_provider(
        502, display_name="Claude Sonnet 5.5", provider_type="openrouter", model_name="anthropic/claude-sonnet-5.5"
    )
    run_id, user_id = _resumable_run()
    _stuck_first_loop(monkeypatch, run_id=run_id, user_id=user_id, smart=smart)

    summary, decided_by, db_events, body = _run_with_escalation(
        monkeypatch,
        primary_actions=[_DONE],
        smart_actions=[_CLICK],
        escalation=smart,
        run_id=run_id,
        user_id=user_id,
    )
    assert summary["state"] != "BLOCKED", summary
    assert body.clicks == [(20, 100)]
    assert decided_by == ["smart", "primary"], decided_by
    returned = [payload for event, payload in db_events if event == "VISION_BRAIN_DEESCALATED"]
    assert [(p["from_provider_id"], p["to_provider_id"], p["reason"]) for p in returned] == [(502, 501, "durable_progress")]
    assert not [event for event, _ in db_events if event == "VISION_BRAIN_ESCALATED"]
    assert _saved_escalation(run_id)["active"] is False


def test_a_missing_escalation_row_starts_the_resumed_loop_on_the_primary(monkeypatch) -> None:
    smart = _resolved_vision_provider(
        502, display_name="Claude Sonnet 5.5", provider_type="openrouter", model_name="anthropic/claude-sonnet-5.5"
    )
    run_id, user_id = _resumable_run()
    _stuck_first_loop(monkeypatch, run_id=run_id, user_id=user_id, smart=smart)

    # The designation was removed or its row disabled: the provider resolver returns no escalation brain.
    _summary, decided_by, db_events, _body = _run_with_escalation(
        monkeypatch,
        primary_actions=[_DONE],
        smart_actions=[],
        escalation=None,
        run_id=run_id,
        user_id=user_id,
        # The run-wide ceiling is 3 x max_steps across loops and the first loop took 4 steps.
        max_steps=2,
    )
    assert decided_by == ["primary"], decided_by
    assert not [event for event, _ in db_events if event.startswith("VISION_BRAIN_")], db_events


def test_a_resumed_loop_that_ends_before_deciding_keeps_the_escalation(monkeypatch) -> None:
    # The loop-end journal write must carry the restored escalation, not a fresh state that would erase it.
    smart = _resolved_vision_provider(
        502, display_name="Claude Sonnet 5.5", provider_type="openrouter", model_name="anthropic/claude-sonnet-5.5"
    )
    run_id, user_id = _resumable_run()
    _stuck_first_loop(monkeypatch, run_id=run_id, user_id=user_id, smart=smart)
    before = _saved_escalation(run_id)

    _summary, decided_by, _events, _body = _run_with_escalation(
        monkeypatch,
        primary_actions=[_DONE],
        smart_actions=[_DONE],
        escalation=smart,
        run_id=run_id,
        user_id=user_id,
        max_steps=0,
    )
    assert decided_by == []
    assert _saved_escalation(run_id) == before and before["active"] is True
