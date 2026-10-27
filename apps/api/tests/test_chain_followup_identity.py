"""Chained follow-ups bound by (stable) control id re-resolve their control at their turn."""

from __future__ import annotations

from app import extension_run
from app.db import init_db

from tests.test_extension_run import _ConflictingControlReferenceBody, _run_scripted


class _ShiftedFollowUpBody(_ConflictingControlReferenceBody):
    async def vision_click_control(self, selector: str, fallback_x: int, fallback_y: int, **_binding: object) -> dict:
        self.control_clicks.append((selector, fallback_x, fallback_y))
        return {
            "ok": True,
            "action": "click",
            "mode": "control_selector",
            "target_role": "combobox",
            "target_text": "Select" if selector == "#experience" else "No",
        }


def test_follow_up_bound_by_control_id_runs_when_its_point_went_stale(monkeypatch) -> None:
    # A shifted layout leaves the follow-up's point on another control; its id still names one control, which the
    # body re-resolves by selector at the follow-up's turn (no model arbitration, no dropped step).
    init_db()
    body = _ShiftedFollowUpBody()
    contexts = _run_scripted(monkeypatch, body, [
        {"action": "click", "control": 9, "x": 250, "y": 240, "reason": "Open experience.",
         "then": [{"action": "click", "control": 7, "x": 250, "y": 240, "reason": "Open sponsorship."}]},
    ], max_steps=2)
    assert [selector for selector, _x, _y in body.control_clicks] == ["#experience", "#sponsorship"]
    assert not any("NOT RUN" in item for context in contexts for item in context.get("recent_actions", []))


def test_primary_action_with_conflicting_id_and_point_still_fails_closed(monkeypatch) -> None:
    init_db()
    body = _ShiftedFollowUpBody()
    monkeypatch.setattr(
        extension_run,
        "_call_control_reference_judge",
        lambda *args, **kwargs: {"choice": "ambiguous", "reason": "both plausible"},
    )
    _run_scripted(monkeypatch, body, [
        {"action": "click", "control": 7, "x": 250, "y": 240, "reason": "Open."},
    ], max_steps=2)
    assert body.control_clicks == []


def test_non_unique_follow_up_identity_is_not_trusted_over_the_conflict() -> None:
    unique = {"selector": "#a"}
    index_map = {1: {"selector": "#a", "control": unique}, 2: {"selector": "#b", "control": {"selector": "#b"}}}
    assert extension_run._followup_index_identity_resolves(unique, index_map) is True
    assert extension_run._followup_index_identity_resolves({"selector": "#a", "disabled": True}, index_map) is False
    assert extension_run._followup_index_identity_resolves({"selector": ""}, index_map) is False
    twin_map = {1: {"selector": "#a"}, 2: {"selector": "#a"}}
    assert extension_run._followup_index_identity_resolves(unique, twin_map) is False


def test_index_bound_step_after_a_scroll_with_an_addressable_control_is_not_refused() -> None:
    refuse = extension_run._index_bound_after_scroll_refusal
    box = [10, 100, 200, 30]
    addressable = {"action": {"action": "click", "control": 1}, "bound_control": {"_inventory_box": box, "selector": "#save"}}
    assert refuse(addressable, {"viewport_height": 800}) is False
    disabled = {
        "action": {"action": "click", "control": 1},
        "bound_control": {"_inventory_box": box, "selector": "#save", "disabled": True},
    }
    assert refuse(disabled, {"viewport_height": 800}) is True
