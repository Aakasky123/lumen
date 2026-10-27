"""Release-blocking capability ratchet promoted from real Chrome Run 439.

This protects the outcome and safety contract for resuming an approved field answer when the
question control and its popup option are distinct elements. It does not preserve a site click path.
"""

from __future__ import annotations

import asyncio

from app import extension_run
from app.agent_eval.capability_regressions import APPROVED_FIELD_CHOICE_BOUND
from app.db import init_db
from app.extension_run import run_vision_loop


def _stub_ordinary_click_consequence(monkeypatch) -> None:
    monkeypatch.setattr(
        extension_run,
        "_judge_click_consequence",
        lambda *args, **kwargs: {
            "classification": "intermediate",
            "confidence": 0.99,
            "evidence": "Fixture verdict: this choice click is ordinary and reversible.",
            "metadata": {"provider": "fixture", "model": "fixture-click-risk", "attempt_count": 1},
        },
    )


class _EmptyCandidateContext:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def query(self, *args, **kwargs):
        return []

    def compact_defaults_with_provenance(self, user_id: int, include_seeded_facts: bool = True) -> dict:
        return {"values": {}}


class _ApprovedPopupChoiceBody:
    def __init__(self) -> None:
        self.selected = ""
        self.control_clicks: list[tuple[str, int, int]] = []

    async def capture_screenshot(self) -> dict:
        if self.selected:
            controls = []
            viewport_text = f"Pronouns {self.selected}. Review is ready."
        else:
            controls = [
                {
                    "index": 6,
                    "tag": "div",
                    "role": "combobox",
                    "id": "field-24",
                    "text": "Search",
                    "context_text": "Pronouns",
                    "aria_controls": "field-24-list",
                    "aria_labelledby": "field-24-label",
                    "accessibility_context": [{"relation": "aria-labelledby", "text": "Pronouns"}],
                    "selector": "#field-24",
                    "x": 100,
                    "y": 100,
                    "width": 300,
                    "height": 40,
                },
                {
                    "index": 7,
                    "tag": "li",
                    "role": "option",
                    "id": "field-24-list-option-0",
                    "text": "She/her/hers",
                    "selector": "#field-24-list-option-0",
                    "ancestor_chain": [
                        {
                            "depth": 1,
                            "tag": "ul",
                            "id": "field-24-list",
                            "role": "listbox",
                            "aria_labelledby": "field-24-label",
                        }
                    ],
                    "x": 100,
                    "y": 150,
                    "width": 300,
                    "height": 36,
                },
                {
                    "index": 8,
                    "tag": "li",
                    "role": "option",
                    "id": "field-24-list-option-1",
                    "text": "He/him/his",
                    "selector": "#field-24-list-option-1",
                    "ancestor_chain": [
                        {
                            "depth": 1,
                            "tag": "ul",
                            "id": "field-24-list",
                            "role": "listbox",
                            "aria_labelledby": "field-24-label",
                        }
                    ],
                    "x": 100,
                    "y": 190,
                    "width": 300,
                    "height": 36,
                },
            ]
            viewport_text = "Pronouns. Search. She/her/hers. He/him/his."
        return {
            "ok": True,
            "image": "jpeg-fixture",
            "width": 800,
            "height": 600,
            "url": "https://fixture.test/form",
            "title": "Fixture form",
            "scroll_y": 0,
            "scroll_height": 600,
            "page_evidence": {"viewport_text": viewport_text, "controls": controls},
        }

    async def vision_click_control(
        self,
        selector: str,
        fallback_x: int,
        fallback_y: int,
        *,
        expected_target_identity: dict | None = None,
        expected_page_context: dict | None = None,
    ) -> dict:
        self.control_clicks.append((selector, fallback_x, fallback_y))
        if selector == "#field-24-list-option-1":
            self.selected = "He/him/his"
            return {
                "ok": True,
                "action": "click",
                "mode": "control_selector",
                "surface_changed": True,
                "target_tag": "li",
                "target_role": "option",
                "target_text": self.selected,
                "selection_committed": True,
                "selection_evidence": "selected_option_readback",
            }
        return {
            "ok": False,
            "action": "click",
            "mode": "control_selector",
            "error": "neighboring option must not be activated",
            "target_role": "option",
            "target_text": "She/her/hers",
        }


class _StaleLiveChoiceBody:
    def __init__(self) -> None:
        self.selected = "Current choice"
        self.control_clicks: list[tuple[str, int, int]] = []

    async def capture_screenshot(self) -> dict:
        if self.selected == "Desired choice":
            controls = []
            viewport_text = "Preference Desired choice. Review is ready."
        else:
            controls = [
                {
                    "index": 5,
                    "tag": "div",
                    "role": "combobox",
                    "id": "preference",
                    "text": "Current choice",
                    "context_text": "Preference",
                    "aria_controls": "preference-options",
                    "accessibility_context": [{"relation": "aria-labelledby", "text": "Preference"}],
                    "selector": "#preference",
                    "x": 80,
                    "y": 80,
                    "width": 300,
                    "height": 40,
                },
                {
                    "index": 6,
                    "tag": "div",
                    "role": "option",
                    "id": "preference-option-0",
                    "text": "Current choice",
                    "checked": True,
                    "selector": "#preference-option-0",
                    "ancestor_chain": [{"depth": 1, "tag": "div", "id": "preference-options", "role": "listbox"}],
                    "x": 80,
                    "y": 130,
                    "width": 300,
                    "height": 36,
                },
                {
                    "index": 7,
                    "tag": "div",
                    "role": "option",
                    "id": "preference-option-1",
                    "text": "Desired choice",
                    "checked": False,
                    "selector": "#preference-option-1",
                    "ancestor_chain": [{"depth": 1, "tag": "div", "id": "preference-options", "role": "listbox"}],
                    "x": 80,
                    "y": 170,
                    "width": 300,
                    "height": 36,
                },
            ]
            viewport_text = "Preference Current choice. Current choice. Desired choice."
        return {
            "ok": True,
            "image": "jpeg-fixture",
            "width": 800,
            "height": 600,
            "url": "https://fixture.test/composite-choice",
            "title": "Composite choice fixture",
            "scroll_y": 0,
            "scroll_height": 600,
            "page_evidence": {"viewport_text": viewport_text, "controls": controls},
        }

    async def vision_click_control(
        self,
        selector: str,
        fallback_x: int,
        fallback_y: int,
        *,
        expected_target_identity: dict | None = None,
        expected_page_context: dict | None = None,
    ) -> dict:
        self.control_clicks.append((selector, fallback_x, fallback_y))
        if selector != "#preference-option-1":
            raise AssertionError("a live option whose own text conflicts with the intended value must not execute")
        self.selected = "Desired choice"
        return {
            "ok": True,
            "action": "click",
            "mode": "control_selector",
            "surface_changed": True,
            "target_tag": "div",
            "target_role": "option",
            "target_text": "Desired choice",
            "selection_committed": True,
            "selected_value": "Desired choice",
            "selection_evidence": "selected_state_changed",
        }


def test_promoted_capability_reconciles_stale_index_and_verifies_related_popup_option(monkeypatch) -> None:
    init_db()
    _stub_ordinary_click_consequence(monkeypatch)
    body = _ApprovedPopupChoiceBody()
    events: list[tuple[str, dict]] = []
    decisions = iter(
        [
            {
                # This reproduces the real provider drift: stale index 7 names the neighboring option,
                # while the current screenshot point and exact action text identify option 8.
                "action": "click",
                "control": 7,
                "x": 200,
                "y": 208,
                "text": "He/him/his",
                "question": "Pronouns",
                "grounding_source": "human_answer",
                "grounding_quote": "He/him/his",
                "reason": "Apply the user's approved answer to the visible choice.",
            },
            {"action": "done", "x": 0, "y": 0, "artifact_role": "", "reason": "Review is ready."},
        ]
    )

    def target_judge(user_id: int, **kwargs) -> dict:
        selected = kwargs["selected_control"]
        expected = kwargs["expected_control"]
        assert selected["selector"] == "#field-24-list-option-1"
        assert selected["ancestor_chain"][0]["id"] == "field-24-list"
        assert expected["selector"] == "#field-24"
        assert expected["aria_controls"] == "field-24-list"
        return {"matches": True, "reason": "The option list is controlled by the expected question field."}

    monkeypatch.setattr("app.candidate_context.CandidateContextEngine", _EmptyCandidateContext)
    monkeypatch.setattr(extension_run, "_record_context_usage_for_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(extension_run, "_db_event", lambda run_id, event_type, payload: events.append((event_type, payload)))
    monkeypatch.setattr(extension_run, "_decide_vision_action", lambda *args, **kwargs: next(decisions))
    monkeypatch.setattr(extension_run, "_call_target_binding_judge", target_judge)
    monkeypatch.setattr(
        extension_run,
        "_human_answers_for_run",
        lambda run_id: [
            {
                "question": "Pronouns",
                "answer": "He/him/his",
                "raw_user_answer": "he him",
                "source": "human_field_approval",
                "target_selector": "#field-24",
            }
        ],
    )

    summary = asyncio.run(
        run_vision_loop(
            body,
            url=None,
            narrate=lambda *args, **kwargs: asyncio.sleep(0),
            run_id=0,
            user_id=0,
            skip_navigate=True,
            max_steps=4,
        )
    )

    assert summary["state"] == APPROVED_FIELD_CHOICE_BOUND.required_final_state
    assert body.control_clicks == [("#field-24-list-option-1", 200, 208)]
    assert body.selected == "He/him/his"
    assert APPROVED_FIELD_CHOICE_BOUND.missing_receipts(summary["receipts"]) == []
    assert any(event == "VISION_CONTROL_REFERENCE_RECONCILED" for event, _ in events)
    verdict = next(payload for event, payload in events if event == "VISION_TARGET_BINDING_VERDICT")
    assert verdict["matches"] is True
    assert verdict["expected_selector"] == "#field-24"


def test_promoted_capability_reobserves_when_live_choice_identity_conflicts(monkeypatch) -> None:
    init_db()
    _stub_ordinary_click_consequence(monkeypatch)
    body = _StaleLiveChoiceBody()
    decisions = iter(
        [
            {
                # Both references point at the current option while the explicit intended value names
                # another option. The body must receive no click from this stale decision.
                "action": "click",
                "control": 6,
                "x": 200,
                "y": 148,
                "text": "Desired choice",
                "question": "Preference",
                "grounding_source": "human_answer",
                "grounding_quote": "Desired choice",
                "reason": "Apply the approved value.",
            },
            {
                "action": "click",
                "control": 7,
                "x": 200,
                "y": 188,
                "text": "Desired choice",
                "question": "Preference",
                "grounding_source": "human_answer",
                "grounding_quote": "Desired choice",
                "reason": "Use the freshly observed exact option.",
            },
            {"action": "done", "x": 0, "y": 0, "artifact_role": "", "reason": "Review is ready."},
        ]
    )

    monkeypatch.setattr("app.candidate_context.CandidateContextEngine", _EmptyCandidateContext)
    monkeypatch.setattr(extension_run, "_record_context_usage_for_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(extension_run, "_db_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(extension_run, "_decide_vision_action", lambda *args, **kwargs: next(decisions))
    monkeypatch.setattr(
        extension_run,
        "_call_target_binding_judge",
        lambda user_id, **kwargs: {"matches": True, "reason": "The current option belongs to the persisted field."},
    )
    monkeypatch.setattr(
        extension_run,
        "_human_answers_for_run",
        lambda run_id: [
            {
                "question": "Preference",
                "answer": "Desired choice",
                "raw_user_answer": "Desired choice",
                "source": "human_field_approval",
                "target_selector": "#preference",
            }
        ],
    )

    summary = asyncio.run(
        run_vision_loop(
            body,
            url=None,
            narrate=lambda *args, **kwargs: asyncio.sleep(0),
            run_id=0,
            user_id=0,
            skip_navigate=True,
            max_steps=5,
        )
    )

    assert summary["state"] == APPROVED_FIELD_CHOICE_BOUND.required_final_state
    assert body.control_clicks == [("#preference-option-1", 200, 188)]
    assert body.selected == "Desired choice"
    # The rejected stale decision must not produce a browser receipt; only the freshly observed,
    # exact live option is allowed to reach the body.
    assert len(summary["receipts"]) == 1


def test_promoted_field_choice_capability_records_complete_ratchet_contract() -> None:
    spec = APPROVED_FIELD_CHOICE_BOUND
    assert spec.release_blocking is True
    assert spec.source_proof["surface"] == "real_chrome"
    assert spec.source_proof["run_id"] == 439
    assert spec.initial_browser_state and spec.minimum_context
    assert spec.success_criteria and spec.forbidden_side_effects and spec.required_receipts
    assert spec.required_final_state == "READY_FOR_REVIEW"
    assert set(spec.body_modes) == {"fixture", "real_chrome"}
    assert "target-binding judge verdict" in spec.failure_artifacts


class _OpensPopupFirstBody(_ApprovedPopupChoiceBody):
    """The popup is opened by a click on the field in an earlier step; no ARIA link ties the listbox to it."""

    def __init__(self) -> None:
        super().__init__()
        self.popup_open = False

    async def capture_screenshot(self) -> dict:
        shot = await super().capture_screenshot()
        controls = shot["page_evidence"]["controls"]
        for control in controls:
            control.pop("aria_controls", None)
            control.pop("accessibility_context", None)
            control.pop("aria_labelledby", None)
            for ancestor in control.get("ancestor_chain") or []:
                ancestor.pop("aria_labelledby", None)
        if not self.popup_open:
            controls[:] = [item for item in controls if item.get("role") != "option"]
        return shot

    async def vision_click_control(self, selector, fallback_x, fallback_y, **kwargs) -> dict:
        if selector == "#field-24":
            self.popup_open = True
            self.control_clicks.append((selector, fallback_x, fallback_y))
            return {
                "ok": True,
                "action": "click",
                "mode": "control_selector",
                "surface_changed": True,
                "target_tag": "div",
                "target_role": "combobox",
                "option_menu_open_after": True,
            }
        return await super().vision_click_control(selector, fallback_x, fallback_y, **kwargs)


class _OpensPopupFirstBody(_ApprovedPopupChoiceBody):
    """The popup is opened by a click on the field in an earlier step; no ARIA link ties the listbox to it."""

    def __init__(self) -> None:
        super().__init__()
        self.popup_open = False

    async def capture_screenshot(self) -> dict:
        shot = await super().capture_screenshot()
        controls = shot["page_evidence"]["controls"]
        for control in controls:
            control.pop("aria_controls", None)
            control.pop("accessibility_context", None)
            control.pop("aria_labelledby", None)
            for ancestor in control.get("ancestor_chain") or []:
                ancestor.pop("aria_labelledby", None)
        if not self.popup_open:
            controls[:] = [item for item in controls if item.get("role") != "option"]
        return shot

    async def vision_click_control(self, selector, fallback_x, fallback_y, **kwargs) -> dict:
        if selector == "#field-24":
            self.popup_open = True
            self.control_clicks.append((selector, fallback_x, fallback_y))
            return {
                "ok": True,
                "action": "click",
                "mode": "control_selector",
                "surface_changed": True,
                "target_tag": "div",
                "target_role": "combobox",
                "option_menu_open_after": True,
            }
        return await super().vision_click_control(selector, fallback_x, fallback_y, **kwargs)


def test_option_of_popup_the_run_opened_binds_to_the_opening_field(monkeypatch) -> None:
    init_db()
    _stub_ordinary_click_consequence(monkeypatch)
    body = _OpensPopupFirstBody()
    events: list[tuple[str, dict]] = []
    decisions = iter(
        [
            {"action": "click", "control": 6, "x": 200, "y": 120, "reason": "Open the Pronouns field."},
            {
                "action": "click",
                "control": 8,
                "x": 200,
                "y": 208,
                "text": "He/him/his",
                "question": "Pronouns",
                "grounding_source": "human_answer",
                "grounding_quote": "He/him/his",
                "reason": "Choose the approved answer.",
            },
            {"action": "done", "x": 0, "y": 0, "artifact_role": "", "reason": "Review is ready."},
        ]
    )
    judged: list[str] = []

    def target_judge(user_id: int, **kwargs) -> dict:
        selector = kwargs["selected_control"]["selector"]
        judged.append(selector)
        return {"matches": selector == "#field-24", "reason": "only the field itself is the Pronouns question"}

    monkeypatch.setattr("app.candidate_context.CandidateContextEngine", _EmptyCandidateContext)
    monkeypatch.setattr(extension_run, "_record_context_usage_for_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(extension_run, "_db_event", lambda run_id, event_type, payload: events.append((event_type, payload)))
    monkeypatch.setattr(extension_run, "_decide_vision_action", lambda *args, **kwargs: next(decisions))
    monkeypatch.setattr(extension_run, "_call_target_binding_judge", target_judge)
    monkeypatch.setattr(
        extension_run,
        "_human_answers_for_run",
        lambda run_id: [{"question": "Pronouns", "answer": "He/him/his", "raw_user_answer": "he him", "source": "human_field_approval"}],
    )

    asyncio.run(
        run_vision_loop(
            body,
            url=None,
            narrate=lambda *args, **kwargs: asyncio.sleep(0),
            run_id=0,
            user_id=0,
            skip_navigate=True,
            max_steps=5,
        )
    )

    assert judged == ["#field-24-list-option-1"]
    assert any(event == "VISION_TARGET_BINDING_POPUP_OWNER" for event, _ in events)
    assert body.selected == "He/him/his"




class _ExpandsPopupOpenerBody(_ApprovedPopupChoiceBody):
    """The popup is opened by a click on the field in an earlier step; no ARIA link ties the listbox to it."""

    def __init__(self) -> None:
        super().__init__()
        self.popup_open = False

    async def capture_screenshot(self) -> dict:
        shot = await super().capture_screenshot()
        controls = shot["page_evidence"]["controls"]
        for control in controls:
            control.pop("aria_controls", None)
            control.pop("accessibility_context", None)
            control.pop("aria_labelledby", None)
            for ancestor in control.get("ancestor_chain") or []:
                ancestor.pop("aria_labelledby", None)
        if not self.popup_open:
            controls[:] = [item for item in controls if item.get("role") != "option"]
        return shot

    async def vision_click_control(self, selector, fallback_x, fallback_y, **kwargs) -> dict:
        if selector == "#field-24":
            self.popup_open = True
            self.control_clicks.append((selector, fallback_x, fallback_y))
            return {
                "ok": True,
                "action": "click",
                "mode": "control_selector",
                "surface_changed": True,
                "target_tag": "div",
                "target_role": "combobox",
                "option_menu_open_after": False,
                "target_disclosure": True,
                "target_expanded_before": "false",
                "target_expanded_after": "true",
                "expansion_changed": True,
            }
        return await super().vision_click_control(selector, fallback_x, fallback_y, **kwargs)


def test_option_of_portal_popup_opened_by_an_expanded_button_binds_to_the_opener(monkeypatch) -> None:
    init_db()
    _stub_ordinary_click_consequence(monkeypatch)
    body = _ExpandsPopupOpenerBody()
    events: list[tuple[str, dict]] = []
    decisions = iter(
        [
            {"action": "click", "control": 6, "x": 200, "y": 120, "reason": "Open the Pronouns field."},
            {
                "action": "click",
                "control": 8,
                "x": 200,
                "y": 208,
                "text": "He/him/his",
                "question": "Pronouns",
                "grounding_source": "human_answer",
                "grounding_quote": "He/him/his",
                "reason": "Choose the approved answer.",
            },
            {"action": "done", "x": 0, "y": 0, "artifact_role": "", "reason": "Review is ready."},
        ]
    )
    judged: list[str] = []

    def target_judge(user_id: int, **kwargs) -> dict:
        selector = kwargs["selected_control"]["selector"]
        judged.append(selector)
        return {"matches": selector == "#field-24", "reason": "only the field itself is the Pronouns question"}

    monkeypatch.setattr("app.candidate_context.CandidateContextEngine", _EmptyCandidateContext)
    monkeypatch.setattr(extension_run, "_record_context_usage_for_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(extension_run, "_db_event", lambda run_id, event_type, payload: events.append((event_type, payload)))
    monkeypatch.setattr(extension_run, "_decide_vision_action", lambda *args, **kwargs: next(decisions))
    monkeypatch.setattr(extension_run, "_call_target_binding_judge", target_judge)
    monkeypatch.setattr(
        extension_run,
        "_human_answers_for_run",
        lambda run_id: [{"question": "Pronouns", "answer": "He/him/his", "raw_user_answer": "he him", "source": "human_field_approval"}],
    )

    asyncio.run(
        run_vision_loop(
            body,
            url=None,
            narrate=lambda *args, **kwargs: asyncio.sleep(0),
            run_id=0,
            user_id=0,
            skip_navigate=True,
            max_steps=5,
        )
    )

    assert judged == ["#field-24-list-option-1"]
    assert any(event == "VISION_TARGET_BINDING_POPUP_OWNER" for event, _ in events)
    assert body.selected == "He/him/his"




def test_click_opened_popup_reads_expansion_receipt() -> None:
    assert extension_run._click_opened_popup({"option_menu_open_after": True})
    assert extension_run._click_opened_popup(
        {"target_disclosure": True, "target_expanded_before": "false", "target_expanded_after": "true"}
    )
    assert not extension_run._click_opened_popup({"target_expanded_before": "true", "target_expanded_after": "true"})
    assert not extension_run._click_opened_popup({"target_expanded_after": "false"})
    assert not extension_run._click_opened_popup({"ok": True})


def test_popup_owner_binds_only_options_inside_its_still_open_popup() -> None:
    owner = {"selector": "#p-assignee", "aria_controls": "assignee-list"}
    seen = {"controls": [{"selector": "#p-assignee", "aria_expanded": "true"}]}
    inside = {"role": "option", "ancestor_chain": [{"id": "assignee-list", "role": "listbox"}, {"tag": "body"}]}
    other_popup = {"role": "option", "selector": "#o2", "ancestor_chain": [{"id": "status-list", "role": "listbox"}]}
    inline = {"role": "option", "selector": "#o3", "ancestor_chain": [{"tag": "div", "id": "card"}, {"tag": "body"}]}
    holds = extension_run._popup_owner_still_holds
    assert holds(owner, inside, "http://x/a", "http://x/a", seen)
    assert not holds(owner, other_popup, "http://x/a", "http://x/a", seen)
    assert not holds(owner, inline, "http://x/a", "http://x/a", seen)
    assert not holds(owner, inside, "http://x/a", "http://x/b", seen)
    closed = {"controls": [{"selector": "#p-assignee", "aria_expanded": "false"}]}
    assert not holds(owner, inside, "http://x/a", "http://x/a", closed)


def test_popup_owner_fallback_needs_a_newly_present_popup_option() -> None:
    bare = {"selector": "#p-assignee"}
    seen = {"controls": [{"selector": "#p-assignee", "aria_expanded": "true"}]}
    holds = extension_run._popup_owner_still_holds
    new_option = {"role": "option", "selector": "#new", "ancestor_chain": [{"role": "listbox"}, {"tag": "body"}]}
    old_option = {"role": "option", "selector": "#old", "ancestor_chain": [{"role": "listbox"}]}
    inline = {"role": "option", "selector": "#inline", "ancestor_chain": [{"tag": "div"}]}
    in_dialog = {"role": "option", "selector": "#d", "ancestor_chain": [{"role": "dialog"}]}
    before = frozenset({"#p-assignee", "#old"})
    assert holds(bare, new_option, "", "", seen, before)
    assert not holds(bare, old_option, "", "", seen, before)  # already on the page when the opener was clicked
    assert not holds(bare, inline, "", "", seen, before)
    assert not holds(bare, in_dialog, "", "", seen, before)  # a dialog is not a choice popup


def test_popup_owner_that_cannot_be_reobserved_is_treated_as_collapsed() -> None:
    owner = {"selector": "#p-assignee", "aria_controls": "assignee-list"}
    inside = {"role": "option", "ancestor_chain": [{"id": "assignee-list", "role": "listbox"}]}
    assert not extension_run._popup_owner_still_holds(owner, inside, "", "", {})
    assert not extension_run._popup_owner_still_holds(owner, inside, "", "", {"controls": [{"selector": "#other"}]})


def test_target_binding_judge_call_is_retained_for_replay(tmp_path) -> None:
    from app import grounding_judge_retention as retention

    path = retention.retain_attempt(
        7,
        judge_call_id="abc",
        attempt=1,
        system="sys",
        prompt={"visible_question": "Assignee"},
        schema={},
        provider=None,
        max_tokens=100,
        timeout_seconds=0,
        image=None,
        image_sent=False,
        entry_count=0,
        result={"ok": True, "data": {"matches": False, "reason": "no link"}, "metadata": {}},
        purpose="target_binding_judge",
        root=tmp_path,
    )
    assert path
    records = retention.load_records(tmp_path, run_ids=[7])
    assert records[0]["purpose"] == "target_binding_judge"
    assert records[0]["outcome"]["verdict"] == {"matches": False, "reason": "no link"}
    assert records[0]["request"]["prompt"]["visible_question"] == "Assignee"


def test_click_with_control_and_matching_target_text_keeps_the_bound_control_for_its_question(monkeypatch) -> None:
    # v6_crm run 1811: a clipped list option named by control AND target_text lost its binding and its
    # question was refused as unbound ("scroll toward it first").
    init_db()
    _stub_ordinary_click_consequence(monkeypatch)
    body = _ApprovedPopupChoiceBody()
    decisions = iter(
        [
            {
                "action": "click",
                "control": 8,
                "x": 200,
                "y": 208,
                "target_text": "He/him/his",
                "text": "He/him/his",
                "question": "Pronouns",
                "grounding_source": "human_answer",
                "grounding_quote": "He/him/his",
                "reason": "Choose the approved answer.",
            },
            {"action": "done", "x": 0, "y": 0, "artifact_role": "", "reason": "Review is ready."},
        ]
    )
    judged: list[str] = []

    def target_judge(user_id: int, **kwargs) -> dict:
        judged.append(kwargs["selected_control"]["selector"])
        return {"matches": True, "reason": "The list belongs to the Pronouns field."}

    monkeypatch.setattr("app.candidate_context.CandidateContextEngine", _EmptyCandidateContext)
    monkeypatch.setattr(extension_run, "_record_context_usage_for_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(extension_run, "_db_event", lambda run_id, event_type, payload: None)
    monkeypatch.setattr(extension_run, "_decide_vision_action", lambda *args, **kwargs: next(decisions))
    monkeypatch.setattr(extension_run, "_call_target_binding_judge", target_judge)
    monkeypatch.setattr(
        extension_run,
        "_human_answers_for_run",
        lambda run_id: [{"question": "Pronouns", "answer": "He/him/his", "raw_user_answer": "he him", "source": "human_field_approval"}],
    )

    asyncio.run(
        run_vision_loop(
            body,
            url=None,
            narrate=lambda *args, **kwargs: asyncio.sleep(0),
            run_id=0,
            user_id=0,
            skip_navigate=True,
            max_steps=3,
        )
    )

    assert judged == ["#field-24-list-option-1"]


def test_retained_target_binding_prompt_is_scrubbed_of_neighbour_pii(monkeypatch, tmp_path) -> None:
    from app import lumen_model, grounding_judge_retention as retention
    from app.lumen_model import LumenModelClient

    monkeypatch.setattr(retention, "retain_attempt", lambda run_id, **kwargs: kept.update(kwargs) or {"path": "x"})
    kept: dict = {}
    client = LumenModelClient.__new__(LumenModelClient)
    client.target_binding_retention_run_id = 9
    prompt = {
        "task": "keep",
        "rules": ["keep a@b.co"],
        "visible_question": "Assignee",
        "expected_question_control": {"text": "Owner jane@example.com"},
        "surrounding_controls": [{"text": "Card 4111111111111111", "context": "ok"}],
    }
    client._retain_binding_attempt(system="s", prompt=prompt, schema={}, image=None, result={"ok": True, "data": {}, "metadata": {}})
    text = str(kept["prompt"])
    assert "jane@example.com" not in text and "4111111111111111" not in text
    assert "[REDACTED_EMAIL]" in text and kept["purpose"] == "target_binding_judge"
