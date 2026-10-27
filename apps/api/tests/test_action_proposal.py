"""ActionProposal v1 is a lossless typed wrapper of vision_agent.normalize_action's decision."""

import pytest

from app import vision_agent
from app.action_proposal import (
    TARGET_CARET,
    TARGET_CONTROL,
    TARGET_CONTROL_AND_POINT,
    TARGET_LABEL,
    TARGET_NONE,
    TARGET_POINT,
    TARGET_TOOL,
    ActionProposal,
    ProposalRejection,
    classify_target,
    decision_summary,
)

RAW_DECISIONS = [
    {"action": "click", "control": 3, "x": 120, "y": 44, "reason": "Open the form."},
    {"action": "click", "x": 87, "y": 506, "modifiers": ["shift"], "reason": "Shift-click the badge."},
    {"action": "click", "control": 7, "clicks": 3, "reason": "Triple-click the plate."},
    {"action": "click", "target_text": "Commit", "target_wait_ms": 2000, "wait_ms": 1500, "reason": "Press it when it appears."},
    {"action": "right_click", "x": 10, "y": 20, "reason": "Context menu."},
    {"action": "click", "x": 50, "y": 60, "hold_ms": 1000, "follow": True, "reason": "Hold the seal."},
    {"action": "type", "control": 2, "x": 200, "y": 150, "text": "Jordan Rivera", "question": "Full name",
     "grounding_source": "profile", "grounding_quote": "Jordan Rivera", "reason": "Fill the name."},
    {"action": "type", "text": "hello", "at_caret": True, "reason": "Type at the caret."},
    {"action": "type", "text": "no target", "reason": "Relies on focus."},
    {"action": "key", "key": "Enter", "reason": "Submit the search."},
    {"action": "scroll", "direction": "down", "amount": 600, "reason": "See more."},
    {"action": "scroll", "text": "Education", "reason": "Find the section."},
    {"action": "drag", "x": 10, "y": 10, "to_x": 200, "to_y": 10, "path": [{"x": 100, "y": 40}], "reason": "Slide."},
    {"action": "zoom", "x": 10, "y": 10, "to_x": 200, "to_y": 120, "reason": "Read small print."},
    {"action": "wait", "wait_ms": 1200, "reason": "Let it load."},
    {"action": "watch", "control": 4, "wait_ms": 3000, "reason": "Watch the counter."},
    {"action": "switch_tab", "target": "12", "reason": "Back to the form."},
    {"action": "switch_surface", "target": "desktop", "reason": "Use the save dialog."},
    {"action": "navigate", "text": "https://example.com/jobs", "reason": "Open the listing."},
    {"action": "upload", "x": 300, "y": 400, "artifact_role": "resume", "reason": "Attach the resume."},
    {"action": "use_credential", "control": 5, "credential_slot": "password", "credential_item_id": 1, "reason": "Sign in."},
    {"action": "ask_user", "question": "Which start date should I use?", "options": ["Now", "Next month"], "reason": "Not in sources."},
    {"action": "query_candidate", "query": "graduation year", "source_scope": "resume", "reason": "Look it up."},
    {"action": "done", "reason": "Finished.", "skipped_optional": [{"question": "Website", "reason": "None on file."}]},
    {"action": "done", "terminal_outcome": "external_failure", "reason": "The site rejected the request."},
    {"action": "click", "control": 1, "x": 5, "y": 5, "reason": "Then type.",
     "then": [{"action": "type", "control": 1, "text": "Jordan", "grounding_source": "profile", "grounding_quote": "Jordan"},
              {"action": "key", "key": "Tab"}]},
    {"action": "click", "x": 5, "y": 5, "then": [{"action": "click", "x": 9, "y": 9, "modifiers": ["ctrl"]}, "not-an-object"]},
    {"action": "click", "then": [{}, {}, {}]},
    {"reason": "No action at all."},
    {"action": "frobnicate", "x": 1, "y": 1},
    {"action": "type", "control": 2, "reason": "Missing text."},
]


@pytest.mark.parametrize("raw", RAW_DECISIONS)
def test_every_normalized_decision_round_trips_through_the_proposal(raw) -> None:
    normalized = vision_agent.normalize_action(raw)
    proposal = ActionProposal.from_normalized(normalized)
    assert proposal.to_normalized() == normalized
    assert proposal.kind == normalized["action"]
    assert len(proposal.followups) == len(normalized.get("then") or [])


def test_the_target_kind_says_which_references_the_model_gave() -> None:
    kinds = {
        TARGET_CONTROL_AND_POINT: {"action": "click", "control": 3, "x": 1, "y": 2},
        TARGET_CONTROL: {"action": "type", "control": 3, "text": "x"},
        TARGET_LABEL: {"action": "click", "target_text": "Commit"},
        TARGET_CARET: {"action": "type", "text": "x", "at_caret": True},
        TARGET_POINT: {"action": "click", "x": 1, "y": 2},
        TARGET_TOOL: {"action": "switch_tab", "target": "12"},
        TARGET_NONE: {"action": "type", "text": "x"},
    }
    for expected, raw in kinds.items():
        assert classify_target(vision_agent.normalize_action(raw)).kind == expected, expected
    assert classify_target(vision_agent.normalize_action(kinds[TARGET_CONTROL_AND_POINT])).ambiguous


def test_a_decision_the_contract_does_not_cover_fails_loudly() -> None:
    normalized = vision_agent.normalize_action({"action": "click", "x": 1, "y": 2})
    with pytest.raises(ValueError):
        ActionProposal.from_normalized({**normalized, "new_field": 1})


def test_loop_summary_tolerates_runtime_keys_and_rejections_are_structured() -> None:
    normalized = vision_agent.normalize_action(RAW_DECISIONS[25])
    summary = decision_summary({**normalized, "_model_metadata": {"model": "x"}, "_outcome_type": "valid_action"})
    assert summary == {"proposal_version": 1, "target_kind": TARGET_CONTROL_AND_POINT, "followup_target_kinds": [TARGET_CONTROL, TARGET_NONE]}
    rejection = ProposalRejection.from_sequence_invalid(
        {"reason": "target_reference_conflict", "label": "primary", "action": {"action": "type", "control": 2, "x": 5, "y": 5, "text": "x"}}
    )
    assert rejection.summary() == {
        "rejection_reason": "target_reference_conflict",
        "rejection_label": "primary",
        "rejection_action": "type",
        "rejection_target_kind": TARGET_CONTROL_AND_POINT,
    }


def test_the_loop_records_the_target_kind_and_the_structured_rejection(monkeypatch) -> None:
    import asyncio

    from app import extension_run
    from app.db import init_db
    from app.extension_run import run_vision_loop

    from test_extension_run import _ControlIndexClickBody
    from test_loop_budgets import _narrate, _stub_loop

    init_db()
    events: list[tuple[str, dict]] = []
    _stub_loop(monkeypatch, lambda user_id, image, view, context: {"action": "click", "control": 99, "reason": "Open the missing control."})
    monkeypatch.setattr(extension_run, "_db_event", lambda run_id, kind, payload, *args, **kwargs: events.append((kind, dict(payload))))

    asyncio.run(run_vision_loop(_ControlIndexClickBody(), url=None, narrate=_narrate, run_id=0, user_id=0, skip_navigate=True, max_steps=3))

    decisions = [payload for kind, payload in events if kind == "VISION_AGENT_DECISION"]
    rejected = [payload for kind, payload in events if kind == "STEP_TIMING" and payload.get("outcome") == "sequence_invalid"]
    assert decisions and all(item["target_kind"] == TARGET_CONTROL and item["proposal_version"] == 1 for item in decisions)
    assert rejected and all(
        item["rejection_action"] == "click" and item["rejection_target_kind"] == TARGET_CONTROL and item["rejection_reason"]
        for item in rejected
    )
