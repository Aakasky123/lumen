"""Release-blocking capability ratchet promoted from real Chrome Runs 439 and 442.

The fixture protects the outcome and safety contract for an already-satisfied choice. It exercises
the real vision-loop orchestration without preserving an external site's historical click path.
"""

from __future__ import annotations

import asyncio

from app import extension_run
from app.agent_eval.capability_regressions import ALREADY_SATISFIED_CHOICE_ADVANCES
from app.db import init_db
from app.extension_run import run_vision_loop


CHOICE_QUESTION = "Have you built integrations with language models in a production environment?"
NEXT_QUESTION = "How have you applied AI in your engineering work and what impact did it have?"


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
    monkeypatch.setattr(
        extension_run,
        "_call_secret_request_classifier",
        lambda *args, **kwargs: {
            "verdict": "ordinary",
            "confidence": 0.99,
            "reason": "Fixture verdict: these visible questions request ordinary profile information.",
            "evidence": "Fixture control and page semantics.",
            "metadata": {"provider": "fixture", "model": "fixture-secret-risk"},
        },
    )


class _EmptyCandidateContext:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def query(self, *args, **kwargs):
        return []

    def compact_defaults_with_provenance(self, user_id: int, include_seeded_facts: bool = True) -> dict:
        return {"values": {}}


class _AlreadySatisfiedChoiceBody:
    def __init__(self) -> None:
        self.menu_open = True
        self.choice_clicks: list[str] = []

    def _controls(self) -> list[dict]:
        controls = [
            {
                "index": 1,
                "tag": "div",
                "role": "combobox",
                "text": "Yes",
                "value": "Yes",
                "context_text": CHOICE_QUESTION,
                "aria_expanded": "true" if self.menu_open else "false",
                "aria_controls": "choice-list",
                "selector": "#choice-owner",
                "x": 100,
                "y": 100,
                "width": 420,
                "height": 40,
            },
            {
                "index": 3,
                "tag": "textarea",
                "role": "textbox",
                "text": "",
                "value": "",
                "context_text": NEXT_QUESTION,
                "selector": "#next-answer",
                "x": 100,
                "y": 250,
                "width": 420,
                "height": 100,
            },
        ]
        if self.menu_open:
            controls.insert(
                1,
                {
                    "index": 2,
                    "tag": "div",
                    "role": "option",
                    "text": "Yes",
                    "context_text": CHOICE_QUESTION,
                    "selected": True,
                    "aria_selected": "true",
                    "selector": "#choice-yes",
                    "ancestor_chain": [
                        {"depth": 1, "tag": "div", "role": "listbox", "id": "choice-list"}
                    ],
                    "x": 100,
                    "y": 145,
                    "width": 420,
                    "height": 36,
                },
            )
        return controls

    async def capture_screenshot(self) -> dict:
        menu = " Yes selected." if self.menu_open else ""
        return {
            "ok": True,
            "image": "jpeg-fixture",
            "width": 800,
            "height": 600,
            "url": "https://fixture.test/workflow",
            "title": "Fixture workflow",
            "scroll_y": 0,
            "scroll_height": 600,
            "page_evidence": {
                "viewport_text": f"{CHOICE_QUESTION} Yes.{menu} {NEXT_QUESTION}",
                "controls": self._controls(),
            },
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
        if selector not in {"#choice-owner", "#choice-yes"}:
            raise AssertionError(f"unexpected control click: {selector}")
        self.choice_clicks.append(selector)
        target_role = "radio" if selector == "#choice-yes" else "combobox"
        self.menu_open = selector == "#choice-owner"
        return {
            "ok": True,
            "action": "click",
            "surface_changed": True,
            "target_tag": "div",
            "target_role": target_role,
            "target_text": "Yes",
            "target_option_like": False,
            "target_selected_before": True,
            "target_selected_after": True,
            "option_menu_closed": not self.menu_open,
            "selection_committed": False,
        }


class _NoEffectKeyFollowupBody(_AlreadySatisfiedChoiceBody):
    """A no-op popup dismissal must not authorize the stale coordinate that follows it."""

    def __init__(self) -> None:
        super().__init__()
        self.keys: list[str] = []
        self.neighbor_mutations = 0

    async def vision_key(self, key: str) -> dict:
        self.keys.append(key)
        return {
            "ok": True,
            "action": "key",
            "key": key,
            "trusted_key_ok": True,
            "before_active_selector": "#choice-owner",
            "after_active_selector": "#choice-owner",
            "before_surface_signature": "menu-open",
            "after_surface_signature": "menu-open",
        }

    async def vision_click(
        self,
        x: int,
        y: int,
        *,
        expected_target_identity: dict | None = None,
        expected_page_context: dict | None = None,
    ) -> dict:
        self.neighbor_mutations += 1
        return {
            "ok": True,
            "action": "click",
            "selection_committed": True,
            "target_option_like": True,
            "target_text": "neighboring option",
        }


def test_promoted_capability_advances_past_already_satisfied_choice(monkeypatch) -> None:
    init_db()
    _stub_ordinary_click_consequence(monkeypatch)
    body = _AlreadySatisfiedChoiceBody()
    events: list[tuple[str, dict]] = []
    contexts: list[dict] = []
    decisions = iter(
        [
            {
                "action": "click",
                "control": 2,
                "x": 310,
                "y": 163,
                "text": "Yes",
                "question": CHOICE_QUESTION,
                "grounding_source": "saved_answer",
                "grounding_quote": "Yes",
                "reason": "The desired visible option is selected, so I will click it.",
            },
            {
                "action": "click",
                "control": 1,
                "x": 310,
                "y": 120,
                "text": "",
                "question": CHOICE_QUESTION,
                "grounding_source": "",
                "grounding_quote": "",
                "reason": "Open the owning control even though its current value is already correct.",
            },
            {
                "action": "click",
                "control": 2,
                "x": 310,
                "y": 163,
                "text": "Yes",
                "question": CHOICE_QUESTION,
                "grounding_source": "saved_answer",
                "grounding_quote": "Yes",
                "reason": "Try the selected option again.",
            },
            {
                "action": "click",
                "control": 1,
                "x": 310,
                "y": 120,
                "text": "",
                "question": CHOICE_QUESTION,
                "grounding_source": "",
                "grounding_quote": "",
                "reason": "Toggle the owning control again instead of advancing.",
            },
            {
                "action": "click",
                "control": 2,
                "x": 310,
                "y": 163,
                "text": "Yes",
                "question": CHOICE_QUESTION,
                "grounding_source": "saved_answer",
                "grounding_quote": "Yes",
                "reason": "Try the selected option for a third time.",
            },
            {
                "action": "ask_user",
                "control": 1,
                "x": 310,
                "y": 120,
                "question": CHOICE_QUESTION,
                "options": ["Yes", "No"],
                "proposed_answer": "Yes",
                "grounding_source": "saved_answer",
                "grounding_quote": "Yes",
                "reason": "Ask the user to reconfirm the already-grounded current value.",
            },
            {
                "action": "ask_user",
                "control": 3,
                "x": 310,
                "y": 300,
                "question": NEXT_QUESTION,
                "options": [],
                "proposed_answer": "",
                "reason": "The next visible field needs the user's grounded answer.",
            },
        ]
    )

    async def narrate(kind: str, data: dict) -> None:
        events.append((kind, data))

    def decide(user_id: int, image: dict, view: dict, context: dict) -> dict:
        contexts.append(context)
        return next(decisions)

    monkeypatch.setattr("app.candidate_context.CandidateContextEngine", _EmptyCandidateContext)
    monkeypatch.setattr(extension_run, "_record_context_usage_for_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(extension_run, "_db_event", lambda run_id, event_type, payload: events.append((event_type, payload)))
    monkeypatch.setattr(
        extension_run,
        "_call_target_binding_judge",
        lambda *args, **kwargs: {"matches": True, "reason": "The fixture exposes one owning question and its popup option."},
    )
    monkeypatch.setattr(
        extension_run,
        "_call_application_proposal_repair",
        lambda *args, **kwargs: {
            "decision": "needs_user_input",
            "answer": "",
            "supporting_entry_indexes": [],
            "rationale": "The next answer is intentionally ungrounded in this fixture.",
            "metadata": {},
        },
    )
    monkeypatch.setattr(
        extension_run,
        "_saved_answers_for_user",
        lambda user_id: [{"question": CHOICE_QUESTION, "answer": "Yes", "source": "answer_memory"}],
    )
    monkeypatch.setattr(extension_run, "_decide_vision_action", decide)

    summary = asyncio.run(
        run_vision_loop(
            body,
            url=None,
            narrate=narrate,
            run_id=0,
            user_id=0,
            skip_navigate=True,
            max_steps=10,
        )
    )

    assert summary["state"] == ALREADY_SATISFIED_CHOICE_ADVANCES.required_final_state
    assert summary["ask"]["label"] == NEXT_QUESTION
    assert body.choice_clicks == ["#choice-yes", "#choice-owner", "#choice-yes", "#choice-owner"]
    transient = [
        receipt
        for receipt in summary["receipts"]
        if receipt["type"] == "browser_click" and receipt["outcome"] == "verified_succeeded"
    ]
    assert len(transient) == 4
    assert all(receipt["verification"]["progress_made"] is False for receipt in transient)
    assert all(receipt["verification"]["target_selected_before"] is True for receipt in transient)
    guards = [receipt for receipt in summary["receipts"] if receipt["outcome"] == "precondition_failed"]
    assert len(guards) == 1
    assert guards[0]["attempt"]["reason"] == "repeated_without_progress"
    assert guards[0]["verification"]["progress_guard"] is True
    assert ALREADY_SATISFIED_CHOICE_ADVANCES.missing_receipts(summary["receipts"]) == []
    assert any(event == "VISION_ACTION_PRECONDITION_BLOCKED" for event, _ in events)
    assert any(event == "VISION_PAGE_STATE_CYCLE" for event, _ in events)
    assert any(event == "VISION_REDUNDANT_ASK_REJECTED" for event, _ in events)
    assert any(
        "already selected before the action" in item
        for context in contexts
        for item in context.get("recent_actions", [])
    )
    assert not any(receipt["verification"].get("final_action") for receipt in summary["receipts"])
    assert any(
        item.get("value") == "Yes"
        for context in contexts
        for item in context.get("satisfied_controls", [])
    )
    assert any(item.get("value") == "Yes" for item in contexts[1]["satisfied_controls"])


def test_promoted_capability_drops_followup_after_unverified_popup_dismissal(monkeypatch) -> None:
    init_db()
    _stub_ordinary_click_consequence(monkeypatch)
    body = _NoEffectKeyFollowupBody()
    contexts: list[dict] = []
    decisions = iter(
        [
            {
                "action": "key",
                "key": "Escape",
                "x": 0,
                "y": 0,
                "reason": "Dismiss the open popup before continuing.",
                "then": [
                    {
                        "action": "click",
                        "x": 310,
                        "y": 199,
                        "text": "",
                        "reason": "Continue with the visible workflow.",
                    }
                ],
            },
            {
                "action": "ask_user",
                "control": 3,
                "x": 310,
                "y": 300,
                "question": NEXT_QUESTION,
                "options": [],
                "proposed_answer": "",
                "reason": "The next visible field needs the user's grounded answer.",
            },
        ]
    )

    async def narrate(kind: str, data: dict) -> None:
        pass

    def decide(user_id: int, image: dict, view: dict, context: dict) -> dict:
        contexts.append(context)
        return next(decisions)

    monkeypatch.setattr("app.candidate_context.CandidateContextEngine", _EmptyCandidateContext)
    monkeypatch.setattr(extension_run, "_record_context_usage_for_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(extension_run, "_db_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        extension_run,
        "_call_target_binding_judge",
        lambda *args, **kwargs: {"matches": True, "reason": "The fixture exposes the requested raw control."},
    )
    monkeypatch.setattr(
        extension_run,
        "_call_application_proposal_repair",
        lambda *args, **kwargs: {
            "decision": "needs_user_input",
            "answer": "",
            "supporting_entry_indexes": [],
            "rationale": "The next answer is intentionally ungrounded in this fixture.",
            "metadata": {},
        },
    )
    monkeypatch.setattr(extension_run, "_saved_answers_for_user", lambda user_id: [])
    monkeypatch.setattr(extension_run, "_decide_vision_action", decide)

    summary = asyncio.run(
        run_vision_loop(
            body,
            url=None,
            narrate=narrate,
            run_id=0,
            user_id=0,
            skip_navigate=True,
            max_steps=4,
        )
    )

    assert summary["state"] == ALREADY_SATISFIED_CHOICE_ADVANCES.required_final_state
    assert summary["ask"]["label"] == NEXT_QUESTION
    assert body.keys == ["Escape"]
    assert body.neighbor_mutations == 0
    key_receipts = [receipt for receipt in summary["receipts"] if receipt["type"] == "browser_action"]
    assert len(key_receipts) == 1
    assert key_receipts[0]["outcome"] == "verification_inconclusive"
    assert key_receipts[0]["verification"]["trusted_key_ok"] is True
    assert any(
        "sequence_aborted at step 1: verification_failed" in item
        for context in contexts
        for item in context.get("recent_actions", [])
    )


def test_promoted_already_satisfied_choice_records_complete_ratchet_contract() -> None:
    spec = ALREADY_SATISFIED_CHOICE_ADVANCES
    assert spec.release_blocking is True
    assert spec.source_proof["surface"] == "real_chrome"
    assert spec.source_proof["run_id"] == 439
    assert spec.source_proof["regression_run_id"] == 442
    assert spec.initial_browser_state and spec.minimum_context
    assert spec.success_criteria and spec.forbidden_side_effects and spec.required_receipts
    assert spec.required_final_state == "WAITING_FOR_USER"
    assert set(spec.body_modes) == {"fixture"}
    assert "durable-progress classification" in spec.failure_artifacts
    assert "key dispatch and before-after browser evidence" in spec.failure_artifacts
