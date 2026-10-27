from __future__ import annotations

import json
import re

from app import extension_run, vision_agent
from app.prompt_cache import SYSTEM_DYNAMIC_BOUNDARY


def test_the_action_cheat_sheet_lists_every_schema_action_and_its_examples_parse() -> None:
    schema = vision_agent.action_schema()
    sheet = vision_agent.action_cheat_sheet(schema)
    properties = schema["schema"]["properties"]
    # Room for keys a brain otherwise learns only from a refusal (approval context, derivation inputs,
    # ask_user's multi_select). The browser's full sheet also carries read_form's line (the desktop and OSWorld
    # sheets, which do not offer read_form, stay under 1950).
    assert len(sheet) <= 2150
    for name in properties["action"]["enum"]:
        assert re.search(rf"(^|[ /;]){name}([ /;]|$)", sheet, re.M), name
    for name in vision_agent.THEN_ACTIONS:
        assert name in sheet
    for name, example, _note in vision_agent._CHEAT_SHEET_EXAMPLES:
        assert set(example) <= set(properties), name
        action = vision_agent.normalize_action(example)
        assert action["action"] == name and not action["invalid_reason"], name
        assert f'{name} {json.dumps(example, separators=(",", ":"))}' in sheet
    for keys in vision_agent._CHEAT_SHEET_OTHER_KEYS.values():
        assert set(keys) <= set(properties)
    # A new schema action is listed without anyone editing the tables.
    extended = {"schema": {"properties": {**properties, "action": {"enum": [*properties["action"]["enum"], "teleport"]}}}}
    assert "teleport (no keys)" in vision_agent.action_cheat_sheet(extended)


def test_the_derivation_ops_are_listed_where_the_brain_looks_and_exact_synonyms_resolve() -> None:
    from app import derivation_check

    schema = vision_agent.action_schema()
    ops = derivation_check.DERIVATION_OPERATIONS
    sheet = vision_agent.action_cheat_sheet(schema)
    assert all(op in sheet for op in ops)
    assert all(op in vision_agent.output_keys_instruction(schema) for op in ops)
    op_schema = schema["schema"]["properties"]["derivation"]["properties"]["op"]
    assert set(op_schema["enum"]) == {"", *ops} and op_schema["description"]
    derivation = {"op": "multiply", "inputs": [{"quote": "7 units", "value": "7"}], "operand": "12", "rule_quote": "x12"}
    kwargs = {"goal_text": "7 units x12", "page_text": ""}
    assert derivation_check.verify_derivation(derivation, "84", **kwargs).reason != "derivation_operation_unknown"
    assert derivation_check.verify_derivation({**derivation, "op": "exponent"}, "84", **kwargs).reason == "derivation_operation_unknown"


def test_the_cheat_sheet_rides_the_stable_system_part_before_the_dynamic_boundary() -> None:
    model_input = vision_agent.prepare_model_input(
        {"width": 800, "height": 600, "url": "http://x/", "title": "x", "scroll_y": 0, "scroll_height": 600},
        {"goal": "Do it", "execution_surface": "browser"},
    )
    system = model_input["system"]
    assert "ACTION CHEAT SHEET" in system
    boundary = system.index(SYSTEM_DYNAMIC_BOUNDARY) if SYSTEM_DYNAMIC_BOUNDARY in system else len(system)
    assert system.index("ACTION CHEAT SHEET") < boundary


def test_an_invalid_reply_is_told_the_key_it_needed_and_the_close_key_it_sent() -> None:
    wait = vision_agent.normalize_action({"action": "wait", "seconds": 3})
    assert wait["invalid_reason"] == "missing_wait_duration"
    assert "`wait_ms`" in wait["key_hint"] and "did you mean `wait_ms`" in wait["key_hint"]
    drag = vision_agent.normalize_action({"action": "drag", "x": 1, "y": 2, "end_x": 5, "end_y": 6})
    assert drag["invalid_reason"] == "missing_drag_destination"
    assert "did you mean `to_x`" in drag["key_hint"] and "to_y" in drag["key_hint"]
    nav = vision_agent.normalize_action({"action": "navigate", "url": "https://a.example"})
    assert nav["invalid_reason"] == "missing_navigate_url" and "did you mean `text`" in nav["key_hint"]
    unknown = vision_agent.normalize_action({"action": "teleport"})
    assert unknown["invalid_reason"] == "unknown_action:teleport" and "click" in unknown["key_hint"]
    # A valid reply carries no hint, and a follow-up's drop names its key.
    assert "key_hint" not in vision_agent.normalize_action({"action": "wait", "wait_ms": 500})
    chained = vision_agent.normalize_action({"action": "key", "key": "Tab", "then": [{"action": "wait", "seconds": 2}]})
    assert "wait_ms" in chained["then_dropped"][0]["expected"]
    feedback = extension_run._vision_invalid_feedback(wait, wait["invalid_reason"])
    assert feedback.startswith("invalid_model_action:missing_wait_duration.") and "did you mean `wait_ms`" in feedback
