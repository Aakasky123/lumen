"""Every pointer primitive that can activate an element passes the click gates."""

from __future__ import annotations

import asyncio
import uuid

from app import extension_run, models, vision_agent
from app.db import SessionLocal, init_db
from app.extension_run import run_vision_loop

from tests.test_extension_run import (
    _DummyContextEngine,
    _FinalSubmitBody,
    _GestureBody,
    _approved_final_action,
    _run_gesture_decisions,
)


def _consequential_judge(*_args, **kwargs) -> dict:
    # The send arrow sits at x >= 400; everything else is ordinary.
    if int(kwargs.get("x") or 0) >= 400:
        return {"classification": "irreversible_final", "confidence": 0.95, "evidence": "Sends the email."}
    return {"classification": "ordinary_reversible", "confidence": 0.99, "evidence": "Fixture."}


def _run(monkeypatch, body, decisions) -> list[str]:
    narrated: list[str] = []
    feed = iter(decisions + [{"action": "done", "reason": "Finished."}])

    async def narrate(kind: str, data: dict) -> None:
        narrated.append(f"{kind}: {data}")

    init_db()
    monkeypatch.setattr("app.candidate_context.CandidateContextEngine", _DummyContextEngine)
    monkeypatch.setattr(extension_run, "_db_event", lambda *a, **k: None)
    monkeypatch.setattr(extension_run, "_record_context_usage_for_run", lambda *a, **k: None)
    monkeypatch.setattr(extension_run, "_judge_click_consequence", _consequential_judge)
    monkeypatch.setattr(extension_run, "_decide_vision_action", lambda *a, **k: next(feed))
    asyncio.run(run_vision_loop(body, url=None, narrate=narrate, run_id=0, user_id=0, skip_navigate=True, max_steps=len(decisions) + 2))
    return narrated


def test_a_press_and_release_on_one_spot_is_a_click_for_every_gate() -> None:
    click = vision_agent.normalize_action({"action": "drag", "x": 450, "y": 20, "to_x": 451, "to_y": 20, "button": "right", "hold_ms": 300})
    assert click["action"] == "click" and (click["x"], click["y"]) == (450, 20)
    assert click["button"] == "right" and click["hold_ms"] == 300 and not click["to_x"] and not click["path"]
    slider = vision_agent.normalize_action({"action": "drag", "x": 10, "y": 20, "to_x": 80, "to_y": 20})
    assert slider["action"] == "drag"
    loop = vision_agent.normalize_action({"action": "drag", "x": 10, "y": 20, "to_x": 11, "to_y": 20, "path": [{"x": 60, "y": 20}]})
    assert loop["action"] == "drag", "a route that leaves the press point is a real drag"


def test_a_one_pixel_drag_on_a_final_control_is_refused_like_the_click(monkeypatch) -> None:
    body = _GestureBody()
    _run(monkeypatch, body, [{"action": "drag", "x": 450, "y": 20, "to_x": 451, "to_y": 20, "reason": "Send."}])
    assert body.drags == [] and body.clicks == [], "nothing was dispatched on the commit control"


def test_a_real_drag_released_over_a_commit_control_is_withheld(monkeypatch) -> None:
    body = _GestureBody()
    narrated = _run(monkeypatch, body, [{"action": "drag", "x": 10, "y": 20, "to_x": 450, "to_y": 20, "reason": "Drop it."}])
    assert body.drags == []
    assert any("receipt" in line and "not_attempted" in line for line in narrated)


def test_a_genuine_slider_drag_is_unaffected(monkeypatch) -> None:
    body = _GestureBody()
    _run(monkeypatch, body, [{"action": "drag", "x": 10, "y": 20, "to_x": 200, "to_y": 20, "reason": "Slide."}])
    assert [item["to"] for item in body.drags] == [(200, 20)]


def test_a_drag_naming_only_a_control_starts_at_its_centre_never_the_corner(monkeypatch) -> None:
    class _Body(_GestureBody):
        async def capture_screenshot(self) -> dict:
            shot = await super().capture_screenshot()
            shot["page_evidence"] = {
                "controls": [{"index": 3, "tag": "textarea", "text": "", "selector": "#t", "x": 100, "y": 60, "width": 80, "height": 40}]
            }
            return shot

    body = _Body()
    _run(monkeypatch, body, [{"action": "drag", "control": 3, "to_x": 210, "to_y": 125, "reason": "Resize."}])
    assert body.drags and body.drags[0]["from"] == (140, 80)
    missing = _Body()
    _run(monkeypatch, missing, [{"action": "drag", "control": 9, "to_x": 210, "to_y": 125, "reason": "Resize."}])
    assert missing.drags == []


def test_a_point_outside_the_named_control_is_a_conflict() -> None:
    control = {"x": 100, "y": 60, "width": 80, "height": 40}
    assert extension_run._point_outside_control_box({"x": 140, "y": 80}, control, 1.0) is False
    assert extension_run._point_outside_control_box({"x": 400, "y": 300}, control, 1.0) is True


def test_a_withheld_click_is_reported_as_not_executed() -> None:
    state = extension_run._outcome_state({"timing_outcome": "final_action_identity_unavailable", "executed_action": False})
    assert state.startswith("not executed: needs a listed control")


def test_a_final_action_the_verifier_saw_succeed_is_verified_even_when_the_body_said_not_ok(monkeypatch) -> None:
    init_db()

    class _NotOkBody(_FinalSubmitBody):
        async def vision_click_control(self, selector, fallback_x, fallback_y, *, allow_final_submit=False, **_kw) -> dict:
            self.control_clicks.append((selector, fallback_x, fallback_y, bool(allow_final_submit)))
            self.submitted = True
            return {"ok": False, "action": "click", "activation_attempted": True, "failure_reason": "click_unconfirmed"}

    body = _NotOkBody()
    db_events: list[tuple[str, dict]] = []
    with SessionLocal() as db:
        user = models.User(email=f"final-not-ok-{uuid.uuid4().hex}@example.test", name="Final")
        db.add(user)
        db.flush()
        run = models.OperatorRun(
            user_id=user.id,
            goal="Submit application",
            target_url="https://example.test/apply",
            current_state="WAITING_FOR_USER",
            confidence_state={"pending_final_action": _approved_final_action(question_id=7191)},
        )
        db.add(run)
        db.commit()
        user_id, run_id = user.id, run.id

    async def narrate(kind: str, data: dict) -> None:
        pass

    monkeypatch.setattr("app.candidate_context.CandidateContextEngine", _DummyContextEngine)
    monkeypatch.setattr(extension_run, "_db_event", lambda run_id, event_type, payload: db_events.append((event_type, payload)))
    monkeypatch.setattr(extension_run, "_record_context_usage_for_run", lambda *a, **k: None)
    monkeypatch.setattr(
        extension_run,
        "_final_action_outcome_verdict",
        lambda *a, **k: {"status": "succeeded", "confidence": 0.99, "evidence": "The confirmation page is visible."},
    )

    summary = asyncio.run(run_vision_loop(body, url=None, narrate=narrate, run_id=run_id, user_id=user_id, skip_navigate=True, max_steps=2))

    assert summary["reason"] == "final_action_submitted"
    assert any(event == "FINAL_ACTION_SUBMIT_ATTEMPTED" and payload.get("verified") is True for event, payload in db_events)
    assert extension_run._outcome_state({"timing_outcome": "final_submit_verified"}).startswith("ran, outcome verified")


def test_final_state_unchanged_needs_a_visible_difference_to_verify_a_not_ok_click():
    from app.extension_run import _final_state_unchanged

    before = {"url": "https://x.test/a", "title": "Cart", "page_evidence": {"viewport_text": "Place order"}}
    same = {"url": "https://x.test/a", "title": "Cart", "page_evidence": {"viewport_text": "Place  order"}}
    changed = {"url": "https://x.test/a", "title": "Cart", "page_evidence": {"viewport_text": "Order placed"}}
    assert _final_state_unchanged(before, same) is True
    assert _final_state_unchanged(before, changed) is False
    assert _final_state_unchanged({}, changed) is True
