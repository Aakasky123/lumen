"""The brain packet's size and cache shape: receipt lines, schema core, shared prefix."""

from __future__ import annotations

import json

from app import model_provider, vision_agent
from app.agent_eval import prompt_budget
from app.prompt_cache import prompt_wire_parts
from app.receipt_view import brain_receipts, receipt_line
from app.vision_agent import prepare_model_input

CLICK_RECEIPT = {
    "type": "browser_click",
    "outcome": "verified_succeeded",
    "target": {"label": "Choose Reviewer.", "selector": "div:nth-of-type(3) > div"},
    "verification": {
        "mode": "control_selector", "url_changed": False, "title_changed": False, "surface_changed": True,
        "active_typable": False, "active_tag": "body", "target_tag": "div", "adopted_new_tab": False,
        "trusted_click_attempted": True, "trusted_click_ok": True, "activation_attempted": True,
        "activation_dispatched": True, "activation_count": 1, "dispatch_path_selected": "cdp_trusted_click",
        "verification_state": "model_visual_readback", "visual_verification_required": False,
        "final_submit_revalidated": False, "target_role": "option", "target_text": "Reviewer",
        "target_option_like": True, "target_selected_before": False, "target_selected_after": True,
        "option_menu_closed": True, "selection_committed": True, "selected_value": "Reviewer",
        "selection_evidence": "model_visual_readback", "owner_matches_target_before": False,
        "reversible_page_context_basis": "exact_url_and_live_target_identity",
        "preflight_target_selector": "div:nth-of-type(3) > div > div",
    },
}


def test_a_receipt_line_keeps_what_happened_and_drops_dispatch_telemetry() -> None:
    line = receipt_line(CLICK_RECEIPT, detailed=True)
    assert "selected 'Reviewer'" in line and "menu closed" in line and "[option]" in line
    assert "Choose Reviewer." in line
    for telemetry in ("trusted_click", "cdp_trusted_click", "reversible_page_context", "activation", "selector"):
        assert telemetry not in line
    # The stored receipt is untouched: approval and verification code keep reading every field.
    assert CLICK_RECEIPT["verification"]["trusted_click_attempted"] is True
    assert len(line) < len(json.dumps(CLICK_RECEIPT)) / 3


def test_older_receipts_shrink_to_one_line_and_the_newest_keep_every_finding() -> None:
    typed = {
        "type": "browser_action", "outcome": "verified_succeeded", "target": {"label": "Fill the date"},
        "verification": {"active_value": "Jun 4, 2027", "active_label": "Date", "value_match": True,
                         "progress_made": True, "progress_class": "verified_control_value",
                         "owner_readback_changed": True, "a_future_field": "kept until named telemetry"},
    }
    lines = brain_receipts([typed] * 5 + [CLICK_RECEIPT], limit=12, detailed=1)
    assert len(lines) == 6
    assert "field 'Jun 4, 2027'" in lines[0] and "progress=" not in lines[0] and "a_future_field" not in lines[0]
    assert "a_future_field=kept until named telemetry" in receipt_line(typed, detailed=True)
    assert "selected 'Reviewer'" in lines[-1]
    assert all(len(line) < 400 for line in lines)
    # A failed or unverified receipt never reaches the brain.
    assert brain_receipts([{**typed, "outcome": "failed"}]) == []


def test_a_failed_readback_and_a_page_that_did_not_move_stay_visible() -> None:
    receipt = {
        "type": "browser_action", "outcome": "verified_succeeded", "target": {"label": "Scroll"},
        "verification": {"value_match": False, "surface_changed": False, "scroll_changed": False,
                         "before_scroll_y": 450, "scroll_y": 450, "scroll_attempted": True},
    }
    line = receipt_line(receipt, detailed=True)
    assert "read-back does NOT match" in line and "page unchanged" in line and "scroll did not move" in line
    assert "page scrolled 450->450" in line


def test_the_action_schema_requires_only_its_core_and_the_strict_copy_requires_everything() -> None:
    schema = vision_agent.action_schema()
    body = schema["schema"]
    assert body["required"] == ["reason", "notes", "plan", "action"]
    then_items = body["properties"]["then"]["items"]
    assert then_items["required"] == ["action"]
    strict = model_provider.strict_schema(schema)
    assert strict["schema"]["required"] == list(body["properties"])
    assert strict["schema"]["properties"]["then"]["items"]["required"] == list(then_items["properties"])
    assert len(strict["schema"]["required"]) >= 38
    # The canonical schema is not mutated by making the strict copy, and other schemas pass through.
    assert body["required"] == ["reason", "notes", "plan", "action"]
    other = {"name": "other", "schema": {"type": "object", "properties": {"a": {}}, "required": []}}
    assert model_provider.strict_schema(other) is other


def test_a_minimal_reply_normalizes_to_the_same_action_as_one_that_spells_out_every_default() -> None:
    minimal = {"reason": "Open it.", "action": "click", "control": 4, "x": 120, "y": 80}
    full = {
        "reason": "Open it.", "notes": "", "plan": "", "action": "click", "control": 4, "x": 120, "y": 80,
        "text": "", "key": "", "direction": "", "amount": 0, "button": "", "clicks": 0, "hold_ms": 0,
        "modifiers": [], "follow": False, "target_text": "", "at_caret": False, "path": [], "to_x": 0,
        "to_y": 0, "artifact_role": "", "target": "", "wait_ms": 0, "credential_item_id": 0,
        "credential_slot": "", "question": "", "options": [], "skipped_optional": [], "query": "",
        "source_scope": "", "proposed_answer": "", "display_reason": "", "confirmation_required": False,
        "grounding_source": "", "grounding_quote": "", "approval_context_summary": "",
        "approval_context_evidence": [], "terminal_outcome": "", "then": [],
    }
    assert vision_agent.normalize_action(minimal) == vision_agent.normalize_action(full)
    follow_up = vision_agent.normalize_action({**minimal, "then": [{"action": "key", "key": "Tab"}]})
    assert follow_up["then"][0]["key"] == "Tab" and not follow_up.get("then_dropped")


def test_the_key_names_line_marks_every_key_but_action_and_reason_optional() -> None:
    line = vision_agent.output_keys_instruction(vision_agent.action_schema())
    assert 'Required keys: "action"' in line and "Every other key is optional" in line


def _packet(**changes):
    long_text = "Alderbank Admin Users table. " * 40
    context = {
        "goal": "Grant the Reviewer role to every Finance user hired before 2024.",
        "execution_surface": "browser",
        "your_notes": ["Finance users: Eli, Farah, Grant"],
        "your_plan": "[done] open Users\n[now] select users\n[ ] assign role",
        "page_memory": [{"url": "http://x/admin", "title": "Admin", "scroll_y": 0, "text": long_text}],
        "control_memory": [],
        "session_context": {"summary": "Opened the Users page and filtered to Finance."},
        "session_ledger": [],
        "page_evidence": {
            "viewport_text": long_text,
            "controls": [
                {"index": index, "tag": "button", "text": f"Select user {index}", "x": 10, "y": 20 * index,
                 "width": 90, "height": 18}
                for index in range(1, 30)
            ],
        },
        "verified_receipts": [CLICK_RECEIPT] * 3,
        "recent_actions": ["click[Select user 3] ok"],
    }
    context.update(changes)
    return prepare_model_input({"width": 800, "height": 600, "url": "http://x/admin", "scroll_y": 0}, context)["prompt"]


def _shared_prefix(first: dict, second: dict) -> tuple[int, int]:
    a, b = "".join(prompt_wire_parts(first)), "".join(prompt_wire_parts(second))
    return prompt_budget.common_prefix_length(a, b), len(b)


def test_a_step_that_leaves_the_page_alone_shares_everything_before_its_receipts() -> None:
    before = _packet()
    new_receipt = {**CLICK_RECEIPT, "target": {"label": "Choose Finance."}}
    after = _packet(
        verified_receipts=[CLICK_RECEIPT] * 3 + [new_receipt],
        recent_actions=["click[Select user 3] ok", "click[Finance] ok"],
    )
    shared, total = _shared_prefix(before, after)
    # Receipts and recent actions close the packet: the goal, notes, plan, memories, page
    # evidence and the whole control inventory sit in the shared prefix.
    first_text = "".join(prompt_wire_parts(before))
    assert shared >= first_text.index('"verified_receipts"')
    assert shared / total > 0.85
    keys = list(before)
    assert keys.index("control_inventory") < keys.index("verified_receipts") < keys.index("recent_actions")


def test_a_new_note_keeps_the_match_through_the_old_ones_and_the_stable_text_is_unchanged_by_page_state() -> None:
    before = _packet()
    after = _packet(your_notes=["Finance users: Eli, Farah, Grant", "Hired before 2024: Eli, Grant"])
    shared, _total = _shared_prefix(before, after)
    assert shared >= "".join(prompt_wire_parts(before)).index('"your_plan"') - 40
    # Page memory and session context come after the run-stable block, so a page change never reaches it.
    scrolled = _packet(page_memory=[{"url": "http://x/admin", "title": "Admin", "scroll_y": 400, "text": "other"}])
    assert prompt_wire_parts(before)[0] == prompt_wire_parts(scrolled)[0]
    assert len(prompt_wire_parts(before)[0]) > 100


def test_derivation_is_optional_in_the_core_and_still_carried_by_the_normalizer() -> None:
    schema = vision_agent.action_schema()["schema"]
    assert "derivation" in schema["properties"] and "derivation" not in schema["required"]
    assert "derivation" in model_provider.strict_schema(vision_agent.action_schema())["schema"]["required"]
    bare = vision_agent.normalize_action({"action": "type", "control": 2, "text": "5", "reason": "r"})
    assert bare["derivation"] == {}
    derivation = {"op": "sum", "inputs": [{"quote": "2", "value": "2"}, {"quote": "3", "value": "3"}], "result": "5"}
    carried = vision_agent.normalize_action(
        {"action": "type", "control": 2, "text": "5", "reason": "r", "derivation": derivation}
    )
    assert carried["derivation"].get("op") == "sum"


def test_the_brain_inventory_omits_empty_and_repeated_facts_but_keeps_toggle_state() -> None:
    rows = [
        {"index": 1, "kind": "input", "text": "Date", "value": "", "context": "Date | Who changed it", "box": [1, 2, 3, 4],
         "disabled": False, "checked": False, "accessibility_context": [{"relation": "label", "text": "Date"}],
         "dom_context": [{"relation": "previous_sibling", "text": "Who changed it", "depth": 1},
                         {"relation": "previous_sibling", "text": "Unrelated card", "depth": 2}]},
        {"index": 2, "kind": "input/checkbox", "text": "Agree", "value": "", "box": [1, 2, 3, 4], "checked": False, "disabled": True},
    ]
    from app.vision_agent import slim_inventory_rows

    first, second = slim_inventory_rows(rows)
    assert "value" not in first and "disabled" not in first and "checked" not in first
    assert "accessibility_context" not in first and [i["text"] for i in first["dom_context"]] == ["Unrelated card"]
    assert second["checked"] is False and second["disabled"] is True
    assert rows[0]["value"] == "" and "dom_context" in rows[0]  # the compact rows are untouched


def test_alarming_telemetry_shows_by_value_in_detailed_and_brief_lines() -> None:
    base = {"type": "browser_type", "outcome": "verified_succeeded", "target": {"label": "Email"}}
    quiet = {**base, "verification": {
        "execution_outcome_unknown": False, "active_aria_invalid": False, "active_validity_valid": True,
        "page_fallback": False, "trusted_click_ok": True, "verification_state": "observed_effect",
    }}
    loud = {**base, "verification": {
        "execution_outcome_unknown": True, "active_aria_invalid": True, "active_validity_valid": False,
        "page_fallback": True, "trusted_click_ok": False, "verification_state": "model_visual_readback",
        "visual_readback_mismatch": True,
    }}
    for detailed in (True, False):
        line = receipt_line(quiet, detailed=detailed)
        assert "unknown" not in line and "aria" not in line and "fallback" not in line and "verification" not in line
        line = receipt_line(loud, detailed=detailed)
        for fragment in ("outcome unknown", "aria-invalid", "validity", "fallback", "trusted click failed",
                         "verification=model_visual_readback", "read-back differs"):
            assert fragment in line, (detailed, fragment, line)


def _assert_derivation_shape(derivation: dict) -> None:
    props = derivation["properties"]
    assert "op" in props and "operand" in props and "rule_quote" in props
    assert set(props["inputs"]["items"]["properties"]) == {"quote", "value"}
    assert {"op", "inputs", "operand", "rule_quote"} <= set(derivation["required"])
    assert props["rule_quote"]["description"] and props["operand"]["description"]
    assert "rule_quote" in derivation["description"]


def test_every_brain_facing_schema_carries_the_whole_derivation_shape() -> None:
    for schema in (vision_agent.action_schema(), model_provider.strict_schema(vision_agent.action_schema())):
        body = schema["schema"]["properties"]
        _assert_derivation_shape(body["derivation"])
        _assert_derivation_shape(body["then"]["items"]["properties"]["derivation"])
    line = vision_agent.output_keys_instruction(vision_agent.action_schema())
    assert "rule_quote" in line and "\"operand\"" in line and "\"quote\"" in line and "\"value\"" in line


def test_derivation_refusal_names_the_schema_field() -> None:
    from app import derivation_check

    result = derivation_check.verify_derivation(
        {"op": "sum", "inputs": [{"quote": "a 2", "value": "2"}, {"quote": "b 3", "value": "3"}]},
        "5",
        goal_text="a 2 b 3",
        page_text="",
    )
    assert result.reason == "derivation_rule_missing" and "rule_quote" in result.note


def test_a_hidden_input_receipt_is_unverified_and_a_space_key_is_named() -> None:
    typed = {
        "type": "browser_action", "outcome": "verified_succeeded", "target": {"label": "terminal"},
        "verification": {"active_value": "ls", "value_match": True, "active_hidden": True,
                         "active_hidden_reason": "opacity 0", "progress_class": "verified_control_value"},
    }
    line = receipt_line(typed, detailed=True)
    assert "typed into a hidden input (opacity 0); no visible effect confirmed (unverified)" in line
    assert "read-back matched" not in line
    assert "typed into a hidden input" in receipt_line(typed, detailed=False)
    # A visible change makes the typing observable again.
    seen = {**typed, "verification": {**typed["verification"], "surface_changed": True}}
    assert "read-back matched" in receipt_line(seen, detailed=True)
    space = {"type": "browser_key", "outcome": "verified_succeeded", "target": {}, "verification": {"key": " "}}
    assert "key 'Space'" in receipt_line(space, detailed=True)
