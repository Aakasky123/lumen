"""A final click the page never lets bind (it re-renders under the target) stops after a bounded
number of withheld attempts instead of renominating until the run times out."""

from __future__ import annotations

import asyncio

from app import extension_run
from app.db import init_db
from app.extension_run import _FINAL_SUBMIT_DRIFT_REFUSAL_LIMIT, run_vision_loop

from tests.test_extension_run import (  # noqa: F401 - the autouse fixtures apply to this module too
    _DummyContextEngine,
    _FinalSubmitBody,
    _complete_final_action_readiness_audit_by_default,
    _final_click_decision,
    _ordinary_click_consequence_by_default,
    _ordinary_secret_semantics_by_default,
)


class _ChurningPageBody(_FinalSubmitBody):
    """The extension withholds every final click: the page state changed between binding and dispatch."""

    async def vision_click_control(self, selector, fallback_x, fallback_y, *, allow_final_submit=False, **binding):
        if allow_final_submit:
            self.control_clicks.append((selector, fallback_x, fallback_y, True))
            return {
                "ok": False,
                "action": "click",
                "mode": "control_selector",
                "failure_reason": "final_submit_page_context_drift",
                "page_context_mismatch_field": "document_state_signature",
                "activation_attempted": False,
                "activation_dispatched": False,
                "activation_count": 0,
            }
        return await super().vision_click_control(selector, fallback_x, fallback_y, allow_final_submit=False, **binding)


def test_a_final_click_withheld_for_page_drift_stops_after_the_limit(monkeypatch) -> None:
    init_db()
    body = _ChurningPageBody()
    events: list[tuple[str, dict]] = []

    async def narrate(kind: str, data: dict) -> None:
        pass

    monkeypatch.setattr("app.candidate_context.CandidateContextEngine", _DummyContextEngine)
    monkeypatch.setattr(extension_run, "_final_action_mode_for_user", lambda user_id: "autonomous")
    monkeypatch.setattr(extension_run, "_vision_unhandled_visible_controls", lambda *args, **kwargs: [])
    monkeypatch.setattr(extension_run, "_vision_forward_progress_available", lambda *args, **kwargs: False)
    monkeypatch.setattr(extension_run, "_judge_final_action_payment", lambda *args, **kwargs: {"payment": False, "summary": "", "metadata": {}})
    monkeypatch.setattr(extension_run, "_final_action_outcome_verdict",
                        lambda *args, **kwargs: {"status": "failed", "confidence": 0.9, "evidence": "Nothing changed."})
    monkeypatch.setattr(extension_run, "_db_event", lambda run_id, event_type, payload: events.append((event_type, payload)))
    monkeypatch.setattr(extension_run, "_record_context_usage_for_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(extension_run, "_decide_vision_action", lambda *args, **kwargs: _final_click_decision("Click Finalize."))

    summary = asyncio.run(run_vision_loop(body, url=None, narrate=narrate, run_id=0, user_id=0, skip_navigate=True, max_steps=40))

    assert summary["state"] == "BLOCKED"
    assert summary["reason"] == "no_progress"
    assert summary["original_reason"] == "final_submit_page_context_drift"
    final_attempts = [c for c in body.control_clicks if c[3]]
    assert len(final_attempts) == _FINAL_SUBMIT_DRIFT_REFUSAL_LIMIT, "bounded, not 69 renominations"
    assert not body.submitted, "nothing was ever dispatched"
