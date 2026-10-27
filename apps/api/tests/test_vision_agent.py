"""Pure-vision agent brain: screenshot in, ONE coordinate action out. These pin the contract (grounding
context reaches the model, coordinates are validated, no image -> no action). The loop + never-submit
floor are exercised in extension_run / the extension."""

from __future__ import annotations

import json

from app import vision_agent


def test_action_schema_enumerates_the_toolbox() -> None:
    schema = vision_agent.action_schema()["schema"]
    enum = schema["properties"]["action"]["enum"]
    assert set(enum) == {
        "click",
        "type",
        "use_credential",
        "scroll",
        "key",
        "drag",
        "hover",
        "back",
        "forward",
        "navigate",
        "reload",
        "switch_tab",
        "zoom",
        "upload",
        "open_file",
        "read_download",
        "launch_app",
        "focus_window",
        "switch_surface",
        "wait",
        "watch",
        "read_clipboard",
        "read_print_view",
        "read_form",
        "query_candidate",
        "run_command",
        "web_search",
        "fetch_url",
        "ask_user",
        "done",
    }
    assert "zoom" not in schema["properties"]["then"]["items"]["properties"]["action"]["enum"]
    assert "then" in schema["properties"]
    assert schema["properties"]["then"]["maxItems"] == vision_agent.MAX_THEN_ACTIONS == 4
    four = [{"action": "key", "key": "Tab"}] * 4
    assert vision_agent.normalize_action({"action": "wait", "wait_ms": 200, "reason": "r", "then": four}).get("invalid_reason", "") == ""
    assert len(vision_agent.normalize_action({"action": "wait", "wait_ms": 200, "reason": "r", "then": four})["then"]) == 4
    five = vision_agent.normalize_action({"action": "wait", "wait_ms": 200, "reason": "r", "then": four + [{"action": "key", "key": "Tab"}]})
    assert "then_too_many_actions" in str(five) and len(five["then"]) == 4
    assert set(schema["properties"]["then"]["items"]["properties"]["action"]["enum"]) == {"click", "type", "scroll", "key", "drag", "hover", "upload", "wait", "watch", "read_clipboard"}
    assert "use_credential" not in schema["properties"]["then"]["items"]["properties"]["action"]["enum"]
    assert "artifact_role" in vision_agent.action_schema()["schema"]["properties"]
    assert "direction" in vision_agent.action_schema()["schema"]["properties"]
    assert "amount" in vision_agent.action_schema()["schema"]["properties"]
    assert "grounding_source" in vision_agent.action_schema()["schema"]["properties"]
    assert "grounding_quote" in vision_agent.action_schema()["schema"]["properties"]
    assert "credential_item_id" in schema["properties"]
    assert "credential_slot" in schema["properties"]
    assert schema["properties"]["credential_item_id"]["minimum"] == 0
    assert schema["properties"]["credential_slot"]["enum"] == [
        "username",
        "email",
        "password",
        "",
    ]
    assert "query" in schema["properties"]
    assert "source_scope" in schema["properties"]
    assert "proposed_answer" in schema["properties"]
    assert "display_reason" in schema["properties"]
    assert "confirmation_required" in schema["properties"]
    assert "skipped_optional" in schema["properties"]
    assert "approval_context_summary" in schema["properties"]
    assert "approval_context_evidence" in schema["properties"]
    assert schema["properties"]["approval_context_evidence"]["maxItems"] == 6
    assert schema["properties"]["skipped_optional"]["maxItems"] == 20
    grounding_sources = ["goal", "profile", "resume", "saved_answer", "human_answer", "page", "file", ""]
    assert vision_agent.action_schema()["schema"]["properties"]["grounding_source"]["enum"] == grounding_sources
    assert schema["properties"]["then"]["items"]["properties"]["grounding_source"]["enum"] == grounding_sources
    primary_text_contract = schema["properties"]["text"]["description"]
    followup_text_contract = schema["properties"]["then"]["items"]["properties"]["text"]["description"]
    action_contract = schema["properties"]["action"]["description"]
    key_contract = schema["properties"]["key"]["description"]
    scroll_control_contract = schema["properties"]["control"]["description"]
    followup_scroll_control_contract = schema["properties"]["then"]["items"]["properties"]["control"]["description"]
    assert "Entering a value uses type with non-empty text" in action_contract
    assert "Keyboard navigation and editing use key" in action_contract
    # query_candidate before ask_user, and use_credential for login controls, are stated once, in the prompt's rules.
    rules = vision_agent.build_prompt({"width": 800, "height": 600, "url": "http://x/"}, {"goal": "g"})["instruction"]
    assert "use query_candidate before asking the user" in rules and "use_credential" in rules
    assert "use_credential" not in action_contract
    assert "value-setting choice click" in primary_text_contract
    assert "ordinary activation/navigation/start/continue/open/dismiss/focus click" in primary_text_contract
    assert "Never use type with empty text to move focus" in primary_text_contract
    assert "never copy a target label" in primary_text_contract
    assert primary_text_contract.startswith(followup_text_contract)
    assert "Always empty for use_credential" in primary_text_contract
    assert "use_credential" not in followup_text_contract
    assert "Use action=key for focus navigation and editing" in key_contract
    assert "never encode that as type" in key_contract
    assert "positive" in scroll_control_contract and "nearest scrollable ancestor" in scroll_control_contract
    assert "control=0" in scroll_control_contract and "page scroll" in scroll_control_contract
    assert "positive" in followup_scroll_control_contract and "page scroll" in followup_scroll_control_contract
    assert "Desktop scroll must use control=0" in vision_agent.DESKTOP_AGENT_SYSTEM


def test_compact_control_inventory_shows_a_textless_drag_surface_and_slider_values() -> None:
    rows = vision_agent.compact_control_inventory(
        {
            "controls": [
                {"index": 1, "tag": "div", "text": "", "x": 40, "y": 400, "width": 90, "height": 90, "affordances": ["cursor:grab"]},
                {
                    "index": 2, "tag": "div", "role": "slider", "text": "", "x": 10, "y": 600, "width": 16, "height": 16,
                    "aria_valuenow": "40", "aria_valuemin": "0", "aria_valuemax": "100",
                },
            ]
        }
    )
    assert rows[0]["affordances"] == ["cursor:grab"] and rows[0]["box"] == [40, 400, 90, 90]
    assert (rows[1]["aria_valuenow"], rows[1]["aria_valuemin"], rows[1]["aria_valuemax"]) == ("40", "0", "100")


def test_compact_control_inventory_passes_through_affordances_and_state() -> None:
    rows = vision_agent.compact_control_inventory(
        {
            "controls": [
                {
                    "index": 1,
                    "tag": "checkbox",
                    "role": "CheckBox",
                    "text": "Remember me",
                    "x": 10,
                    "y": 20,
                    "width": 18,
                    "height": 18,
                    "checked": True,
                    "affordances": ["toggle", "invoke"],
                },
                {
                    "index": 2,
                    "tag": "edit",
                    "role": "Edit",
                    "text": "",
                    "x": 10,
                    "y": 50,
                    "width": 200,
                    "height": 24,
                    "focused": True,
                    "accepts_text": True,
                    "affordances": ["set_value"],
                },
                {
                    "index": 3,
                    "tag": "combobox",
                    "role": "ComboBox",
                    "text": "Country",
                    "x": 10,
                    "y": 90,
                    "width": 200,
                    "height": 24,
                    "expanded": False,
                    "focused": False,
                    "affordances": ["expand_collapse", "set_value"],
                },
            ]
        }
    )

    assert rows[0]["checked"] is True
    assert rows[0]["affordances"] == ["toggle", "invoke"]
    assert rows[1]["focused"] is True
    assert rows[1]["accepts_text"] is True
    assert rows[2]["expanded"] is False
    # focused/accepts_text are emitted only when true - never as row-noise falses.
    assert "focused" not in rows[2]
    assert "accepts_text" not in rows[2]


def test_normalize_action_clamps_coords_and_defaults() -> None:
    a = vision_agent.normalize_action({"action": "click", "x": "519.6", "y": -3, "reason": "select No"})
    assert a["action"] == "click" and a["x"] == 520 and a["y"] == 0
    assert a["then"] == []
    # Unknown provider output is a schema failure, never a fabricated user question.
    unknown = vision_agent.normalize_action({"action": "frobnicate"})
    assert unknown["action"] == ""
    assert unknown["invalid_reason"] == "unknown_action:frobnicate"
    assert unknown["_outcome_type"] == vision_agent.OUTCOME_SCHEMA_INVALID


def test_normalize_action_preserves_model_owned_scroll_target_and_page_fallback() -> None:
    targeted = vision_agent.normalize_action(
        {"action": "scroll", "control": 7, "direction": "down", "amount": 240}
    )
    assert targeted["control"] == 7
    assert targeted["direction"] == "down"
    assert targeted["amount"] == 240

    page = vision_agent.normalize_action(
        {"action": "scroll", "control": 0, "x": 320, "y": 240, "direction": "up", "amount": 180}
    )
    assert page["control"] == 0
    assert page["x"] == 320 and page["y"] == 240
    assert page["direction"] == "up"
    assert page["amount"] == 180


def test_action_schema_offers_the_full_human_input_vocabulary() -> None:
    schema = vision_agent.action_schema()["schema"]
    properties = schema["properties"]

    # Mouse: every button, single/double/triple, drag destination.
    assert set(properties["button"]["enum"]) == {"left", "right", "middle", ""}
    for field in ("button", "clicks", "to_x", "to_y"):
        assert field in schema["properties"]

    # Keyboard: the model must be told the editing/navigation keys exist, and that a
    # press can repeat (the "how do I clear this field" case).
    key_description = properties["key"]["description"]
    for key in ("Backspace", "Delete", "Home", "End", "PageUp", "F1-F24", "Ctrl+A"):
        assert key in key_description
    assert "amount" in key_description
    assert "repeat" in properties["amount"]["description"]

    # Scrolling works in all four directions.
    assert "left" in properties["direction"]["description"]
    assert "right" in properties["direction"]["description"]


def test_normalize_action_carries_pointer_and_drag_fields() -> None:
    right_click = vision_agent.normalize_action(
        {"action": "click", "x": 120, "y": 90, "button": "right", "clicks": 2, "reason": "context menu"}
    )
    assert right_click["button"] == "right"
    assert right_click["clicks"] == 2
    assert right_click["invalid_reason"] == ""

    drag = vision_agent.normalize_action(
        {"action": "drag", "x": 10, "y": 20, "to_x": 300, "to_y": 240, "reason": "select the line"}
    )
    assert (drag["to_x"], drag["to_y"]) == (300, 240)
    assert drag["invalid_reason"] == ""

    # A drag with no destination cannot execute and must fail closed with repair feedback.
    assert (
        vision_agent.normalize_action({"action": "drag", "x": 10, "y": 20, "reason": "drag"})[
            "invalid_reason"
        ]
        == "missing_drag_destination"
    )
    assert (
        vision_agent.normalize_action({"action": "hover", "reason": "reveal the menu"})[
            "invalid_reason"
        ]
        == "missing_hover_target"
    )


def test_normalize_action_zoom_requires_a_real_rectangle() -> None:
    zoom = vision_agent.normalize_action(
        {"action": "zoom", "x": 100, "y": 200, "to_x": 500, "to_y": 360, "reason": "read the fine print"}
    )
    assert zoom["action"] == "zoom"
    assert zoom["invalid_reason"] == ""
    assert (zoom["x"], zoom["y"], zoom["to_x"], zoom["to_y"]) == (100, 200, 500, 360)

    # Degenerate or inverted rectangles fail closed with repair feedback.
    for bad in (
        {"action": "zoom", "x": 100, "y": 200, "reason": "no corner"},
        {"action": "zoom", "x": 500, "y": 200, "to_x": 100, "to_y": 360, "reason": "inverted"},
        {"action": "zoom", "x": 100, "y": 200, "to_x": 500, "to_y": 200, "reason": "flat"},
    ):
        assert vision_agent.normalize_action(bad)["invalid_reason"] == "invalid_zoom_region"

    # A region given by its size is the same region (v6_calendar).
    sized = vision_agent.normalize_action(
        {"action": "zoom", "x": 440, "y": 140, "width": 270, "height": 300, "reason": "busy bars"}
    )
    assert sized["invalid_reason"] == "" and (sized["to_x"], sized["to_y"]) == (710, 440)

    # json_object providers reach the primitive through intuitive aliases.
    aliased = vision_agent.normalize_action(
        {"action": "zoom_in", "x": 10, "y": 10, "to_x": 90, "to_y": 60, "reason": "closer look"}
    )
    assert aliased["action"] == "zoom"
    assert aliased["invalid_reason"] == ""


def test_normalize_action_absorbs_redundant_done_success_annotations() -> None:
    # json_object providers fill terminal_outcome with intuitive success words even
    # though the field is reserved for external_failure. done already means complete,
    # so the annotation is absorbed instead of failing the terminal step three times.
    for value in ("success", "Completed", "task_complete"):
        action = vision_agent.normalize_action(
            {"action": "done", "terminal_outcome": value, "reason": "all fields verified"}
        )
        assert action["invalid_reason"] == ""
        assert action["terminal_outcome"] == ""

    failure = vision_agent.normalize_action(
        {"action": "done", "terminal_outcome": "external_failure", "reason": "site 500s"}
    )
    assert failure["terminal_outcome"] == "external_failure"
    assert failure["invalid_reason"] == ""

    unknown = vision_agent.normalize_action(
        {"action": "done", "terminal_outcome": "gave_up", "reason": "?"}
    )
    assert unknown["invalid_reason"] == "invalid_terminal_outcome"


def test_normalize_action_binds_desktop_tool_target_aliases() -> None:
    # json_object-mode providers ignore the schema field names, so a launch_app/focus_window
    # id arriving as app_id/window_id must still bind instead of failing the whole step.
    launch = vision_agent.normalize_action(
        {"action": "launch_app", "app_id": "app:c4d9b2346922ff24", "reason": "open Notepad"}
    )
    assert launch["target"] == "app:c4d9b2346922ff24"
    assert launch["invalid_reason"] == ""

    focus = vision_agent.normalize_action(
        {"action": "focus_window", "window_id": "hwnd:1247394", "reason": "front Notepad"}
    )
    assert focus["target"] == "hwnd:1247394"
    assert focus["invalid_reason"] == ""

    missing = vision_agent.normalize_action({"action": "launch_app", "reason": "open Notepad"})
    assert missing["invalid_reason"] == "missing_launch_app_target"


def test_normalize_action_accepts_exact_bound_credential_use_without_values() -> None:
    action = vision_agent.normalize_action(
        {
            "action": "use_credential",
            "control": 7,
            "x": 310,
            "y": 244,
            "credential_item_id": 0,
            "credential_slot": "email",
            "question": "Email Address",
            "text": "",
            "grounding_source": "",
            "grounding_quote": "",
            "reason": "The rendered account-creation form requires an email identifier.",
        }
    )

    assert action["_outcome_type"] == vision_agent.OUTCOME_VALID_ACTION
    assert action["action"] == "use_credential"
    assert action["control"] == 7
    assert action["credential_item_id"] == 0
    assert action["credential_slot"] == "email"
    assert action["text"] == ""
    assert action["grounding_source"] == ""
    assert action["grounding_quote"] == ""


def test_normalize_action_fails_closed_on_invalid_credential_actions() -> None:
    missing_target = vision_agent.normalize_action(
        {
            "action": "use_credential",
            "credential_item_id": 0,
            "credential_slot": "username",
        }
    )
    invalid_slot = vision_agent.normalize_action(
        {
            "action": "use_credential",
            "control": 3,
            "credential_item_id": 0,
            "credential_slot": "token",
        }
    )
    missing_item_id = vision_agent.normalize_action(
        {
            "action": "use_credential",
            "control": 3,
            "credential_slot": "password",
        }
    )
    leaked_text = vision_agent.normalize_action(
        {
            "action": "use_credential",
            "control": 3,
            "credential_item_id": 9,
            "credential_slot": "password",
            "text": "must-not-survive",
        }
    )

    assert missing_target["invalid_reason"] == "missing_credential_control"
    assert invalid_slot["invalid_reason"] == "invalid_credential_slot"
    assert missing_item_id["invalid_reason"] == "invalid_credential_item_id"
    assert leaked_text["invalid_reason"] == "credential_action_must_not_include_text"
    assert leaked_text["text"] == ""
    assert all(
        item["_outcome_type"] == vision_agent.OUTCOME_SCHEMA_INVALID
        for item in (missing_target, invalid_slot, missing_item_id, leaked_text)
    )


def test_normalize_action_rejects_credential_use_inside_then() -> None:
    action = vision_agent.normalize_action(
        {
            "action": "click",
            "control": 1,
            "x": 50,
            "y": 60,
            "then": [
                {
                    "action": "use_credential",
                    "control": 2,
                    "credential_item_id": 4,
                    "credential_slot": "password",
                }
            ],
        }
    )

    assert action["_outcome_type"] != vision_agent.OUTCOME_SCHEMA_INVALID
    assert action["then"] == []
    assert action["then_dropped"][0]["reason"] == "then_action_not_allowed:use_credential"


def test_prepare_model_input_frontloads_required_tool_repair() -> None:
    prepared = vision_agent.prepare_model_input(
        {"width": 1120, "height": 828, "url": "https://example.test"},
        {
            "required_action_repair": (
                "invalid_model_action:missing_click_target. "
                "Return a real target, scroll, or done."
            )
        },
    )

    assert "TOOL CALL REPAIR REQUIRED" in prepared["system"]
    assert "missing_click_target" in prepared["system"]
    assert "was not executed" in prepared["system"]


def test_prepare_model_input_frontloads_browser_state_recovery() -> None:
    prepared = vision_agent.prepare_model_input(
        {"width": 1120, "height": 828, "url": "https://example.test"},
        {
            "required_state_repair": (
                "The last interaction only opened or closed a transient surface. "
                "Inspect the current displayed value before acting."
            )
        },
    )

    assert "BROWSER STATE RECOVERY REQUIRED" in prepared["system"]
    assert "Inspect the current displayed value before acting" in prepared["system"]


def test_prompt_teaches_range_controls_by_drag_and_track() -> None:
    prepared = vision_agent.prepare_model_input({"width": 1120, "height": 828, "url": "https://example.test"}, {})
    system = prepared["system"]
    assert "drag its handle to the estimated position first" in system
    assert "Never click the handle itself" in system
    assert "click its handle" not in system
    assert "put the grounded target value in text when this action should land on it" in system
    assert "for an intermediate step leave text empty" in system


def test_prompt_teaches_replacing_one_placeholder_in_a_document() -> None:
    action = vision_agent.action_schema()["schema"]["properties"]["action"]["description"]
    assert "double-click it (clicks=2) to select just that word, then type with at_caret=true" in action
    assert "A file dialog opens in the folder it last used" in action
    assert "type at the caret only when selected_text is exactly the part to replace" in action
    compact = vision_agent.compact_page_evidence(
        {"caret": {"line_text": "Seal number: ____", "column": 12, "selection_length": 5, "selected_text": " ____"}}
    )
    assert compact["caret"]["selected_text"] == " ____"
    question = vision_agent.action_schema()["schema"]["properties"]["question"]["description"]
    assert "a file's rename box, the Address box), never the task" in question


def test_the_focused_multi_line_text_reaches_the_prompt_as_raw_numbered_lines() -> None:
    # v3_paperwork run 1650: in Notepad the model saw only a collapsed one-line excerpt of the document
    # and mangled its lines (merged two with a triple-click, left stray underscores).
    lines = ["1|Shipping manifest", "2|Container ID:  ________________", "3|Seal number: __", "4|"]
    evidence = {
        "viewport_text": "Visible Windows: [hwnd:1] manifest.txt - Notepad (active)",
        "controls": [
            {
                "index": 3,
                "tag": "document",
                "role": "Document",
                "text": "Text editor",
                "value": "Shipping manifest Container ID: ________________ Seal number: __",
                "focused": True,
                "x": 10,
                "y": 60,
                "width": 900,
                "height": 600,
            }
        ],
        "focused_text_lines": {"control": 3, "line_count": 4, "caret_line": 2, "lines": lines},
    }

    compact = vision_agent.compact_page_evidence(evidence)
    assert compact["focused_text_lines"] == {"control": 3, "line_count": 4, "caret_line": 2, "lines": lines}
    assert compact["focused_text_lines"]["lines"][1] == "2|Container ID:  ________________", "spaces are kept"

    prepared = vision_agent.prepare_model_input(
        {"width": 1120, "height": 828, "url": "desktop://windows/hwnd:1"},
        {"goal": "Fill the manifest.", "execution_surface": "desktop", "page_evidence": evidence},
    )
    assert prepared["prompt"]["page_evidence"]["focused_text_lines"]["lines"] == lines
    inventory_row = prepared["prompt"]["control_inventory"][0]
    assert inventory_row["index"] == 3 and inventory_row["focused"] is True
    system = prepared["system"]
    assert "page_evidence.focused_text_lines, when present, is the focused multi-line text control's own text" in system
    assert (
        "In a text editor a double-click selects one word or one run of underscores, a triple-click may select "
        "the whole line including its line break, and typing replaces the selection." in system
    )
    # The browser prompt carries the same body facts (a textarea is a multi-line text control too).
    browser_system = vision_agent.prepare_model_input({"width": 1120, "height": 828, "url": "https://example.test"}, {})["system"]
    assert "page_evidence.focused_text_lines" in browser_system
    # Without the field nothing is added to the evidence.
    assert "focused_text_lines" not in vision_agent.compact_page_evidence({"viewport_text": "x"})


def test_focused_text_lines_are_bounded_around_the_caret_without_collapsing_spaces() -> None:
    lines = [f"{number}|  Line {number}  " + "_" * 90 for number in range(1, 101)]
    view = vision_agent.compact_focused_text_lines(
        {"control": 2, "line_count": 100, "caret_line": 70, "lines": lines}, max_lines=40, max_chars=3000
    )
    numbers = [int(line.split("|", 1)[0]) for line in view["lines"]]
    assert sum(len(line) + 1 for line in view["lines"]) <= 3000
    assert 70 in numbers and numbers == sorted(numbers)
    assert numbers == list(range(numbers[0], numbers[-1] + 1)), "a contiguous run of lines"
    assert view["lines"][numbers.index(70)] == "70|  Line 70  " + "_" * 90
    assert vision_agent.compact_focused_text_lines({"lines": "not a list"}) == {}
    assert vision_agent.compact_focused_text_lines(None) == {}
    no_caret = vision_agent.compact_focused_text_lines({"lines": lines}, max_lines=5)
    assert [line.split("|", 1)[0] for line in no_caret["lines"]] == ["1", "2", "3", "4", "5"]


def test_prompt_says_last_page_view_is_the_task_page_re_read_each_desktop_step() -> None:
    prepared = vision_agent.prepare_model_input(
        {"width": 1120, "height": 828, "url": "desktop://windows/hwnd:1"},
        {
            "goal": "File the paperwork.",
            "execution_surface": "desktop",
            "surfaces": ["browser", "desktop"],
            "last_page_view": {
                "url": "https://example.test/paperwork",
                "title": "Paperwork",
                "text": "1. Rename the invoice",
                "document_text": "1. Rename the invoice 4. Label the customs form",
            },
        },
    )
    assert "last_page_view is this task's web page" in prepared["system"]
    assert "document_text, its whole text" in prepared["system"]
    assert "re-read from its tab every step" in prepared["system"]
    assert "document_text is for reading" in prepared["system"]
    assert prepared["prompt"]["last_page_view"]["text"] == "1. Rename the invoice"
    assert prepared["prompt"]["last_page_view"]["document_text"] == "1. Rename the invoice 4. Label the customs form"


def test_prepare_model_input_makes_exact_mutation_constraint_durable() -> None:
    prepared = vision_agent.prepare_model_input(
        {"width": 1120, "height": 828, "url": "https://example.test"},
        {
            "recovery_constraints": [
                {
                    "constraint_id": "opaque-scope:opaque-action",
                    "action": "type",
                    "control": "Contact number",
                    "progress_class": "control_validation_rejected",
                    "rejection_count": 1,
                    "rule": "exact_same_scope_action_and_value_fingerprint_is_withheld",
                }
            ]
        },
    )

    assert "MUTATION CONSTRAINT" in prepared["system"]
    assert "Do not retry a withheld mutation" in prepared["system"]
    assert prepared["prompt"]["recovery_constraints"][0]["action"] == "type"


def test_prepare_model_input_aligns_control_scope_cooldown_with_structured_constraint() -> None:
    constraint = {
        "constraint_id": "control-kind:opaque-scope:type",
        "action": "type",
        "control": "Contact number",
        "progress_class": "control_validation_rejected",
        "rejection_count": 1,
        "distinct_fingerprint_count": 2,
        "withheld_until": "durable_progress_elsewhere",
        "rule": "same_exact_control_action_kind_is_withheld_until_durable_progress",
    }
    prepared = vision_agent.prepare_model_input(
        {"width": 1120, "height": 828, "url": "https://example.test"},
        {"recovery_constraints": [constraint]},
    )

    assert "every further same-kind mutation on that exact control" in prepared["system"]
    assert "non-mutating tool" in prepared["system"]
    assert prepared["prompt"]["recovery_constraints"] == [constraint]


def test_browser_system_rejects_page_placeholders_as_grounded_values() -> None:
    assert "placeholder, example, sample, or format hint is not an answer" in vision_agent.AGENT_SYSTEM
    assert "controls are the credential exception" in vision_agent.AGENT_SYSTEM
    assert "you never receive, type, cite, or repeat it" in vision_agent.AGENT_SYSTEM
    prompt = vision_agent.build_prompt(
        {"width": 1120, "height": 828, "url": "https://example.test"},
        {},
    )
    assert "format string, or validation hint" in prompt["instruction"]
    assert "must never be copied into a field as its value" in prompt["instruction"]


def test_normalize_action_preserves_bounded_approval_context_evidence() -> None:
    action = vision_agent.normalize_action(
        {
            "action": "click",
            "x": 500,
            "y": 420,
            "reason": "The final confirmation control is visible.",
            "approval_context_summary": "Two adjacent seats are selected for the 7:30 PM showing.",
            "approval_context_evidence": [
                "7:30 PM",
                "Seats C8, C9",
                "7:30 PM",
            ],
        }
    )

    assert action["approval_context_summary"] == "Two adjacent seats are selected for the 7:30 PM showing."
    assert action["approval_context_evidence"] == ["7:30 PM", "Seats C8, C9"]


def test_normalize_top_level_actions_preserve_explicit_optional_skips_for_distinct_controls() -> None:
    done = vision_agent.normalize_action(
        {
            "action": "done",
            "reason": "All required work is complete.",
            "skipped_optional": [
                {
                    "control": 8,
                    "question": "  Optional portfolio note  ",
                    "reason": "The rendered label marks this field optional.",
                },
                {
                    "control": 9,
                    "question": "Optional portfolio note",
                    "reason": "A distinct current control with the same visible question.",
                },
            ],
        }
    )
    scroll = vision_agent.normalize_action(
        {
            "action": "scroll",
            "direction": "down",
            "amount": 300,
            "reason": "The next pending control is below the viewport.",
            "skipped_optional": [
                {"control": 8, "question": "Optional portfolio note", "reason": "It is visibly optional."}
            ],
        }
    )

    assert done["invalid_reason"] == ""
    assert done["skipped_optional"] == [
        {
            "control": 8,
            "question": "Optional portfolio note",
            "reason": "The rendered label marks this field optional.",
        },
        {
            "control": 9,
            "question": "Optional portfolio note",
            "reason": "A distinct current control with the same visible question.",
        },
    ]
    assert scroll["invalid_reason"] == ""
    assert scroll["_outcome_type"] == vision_agent.OUTCOME_VALID_ACTION
    assert scroll["skipped_optional"] == [
        {
            "control": 8,
            "question": "Optional portfolio note",
            "reason": "It is visibly optional.",
        }
    ]


def test_normalize_action_rejects_optional_skip_sidecar_inside_then() -> None:
    action = vision_agent.normalize_action(
        {
            "action": "click",
            "control": 2,
            "x": 200,
            "y": 220,
            "reason": "Open the next reversible control.",
            "then": [
                {
                    "action": "scroll",
                    "direction": "down",
                    "amount": 300,
                    "skipped_optional": [
                        {
                            "control": 8,
                            "question": "Optional portfolio note",
                            "reason": "It is visibly optional.",
                        }
                    ],
                }
            ],
        }
    )

    assert action["then"] == []
    assert action["then_dropped"][0]["reason"].startswith("optional_skip")


def test_normalize_action_requires_empty_then_with_top_level_optional_skip() -> None:
    action = vision_agent.normalize_action(
        {
            "action": "scroll",
            "direction": "down",
            "amount": 300,
            "reason": "Continue below the current optional control.",
            "skipped_optional": [
                {
                    "control": 8,
                    "question": "Optional portfolio note",
                    "reason": "It is visibly optional.",
                }
            ],
            "then": [
                {
                    "action": "click",
                    "control": 9,
                    "x": 200,
                    "y": 220,
                    "reason": "Open the next control.",
                }
            ],
        }
    )

    assert action["_outcome_type"] == vision_agent.OUTCOME_SCHEMA_INVALID
    assert action["invalid_reason"] == "optional_skip_sidecar_requires_empty_then"
    assert action["sequence_invalid_reason"] == "optional_skip_sidecar_requires_empty_then"


def test_normalize_action_rejects_empty_click_used_only_as_skip_sidecar() -> None:
    action = vision_agent.normalize_action(
        {
            "action": "click",
            "control": 0,
            "x": 0,
            "y": 0,
            "reason": "Record the optional field without a browser target.",
            "skipped_optional": [
                {
                    "control": 8,
                    "question": "Optional challenge",
                    "reason": "The rendered page marks it optional.",
                }
            ],
        }
    )

    assert action["_outcome_type"] == vision_agent.OUTCOME_SCHEMA_INVALID
    assert action["invalid_reason"] == "missing_click_target"


def test_normalize_action_fails_closed_on_malformed_optional_skip_sidecars() -> None:
    malformed_sidecars = [
        "",
        ["not-an-object"],
        [{"control": 8, "question": "", "reason": "Visible evidence."}],
        [{"control": 8, "question": ["Optional note"], "reason": "Visible evidence."}],
        [{"control": 8, "question": "Optional note", "reason": {"visible": True}}],
        [
            {
                "control": 8,
                "question": "Optional note",
                "reason": "Visible evidence.",
            },
            {
                "control": 8,
                "question": "A conflicting declaration",
                "reason": "Other visible evidence.",
            },
        ],
    ]

    for skipped_optional in malformed_sidecars:
        action = vision_agent.normalize_action(
            {
                "action": "scroll",
                "direction": "down",
                "amount": 300,
                "reason": "Continue to the next viewport.",
                "skipped_optional": skipped_optional,
            }
        )

        assert action["_outcome_type"] == vision_agent.OUTCOME_SCHEMA_INVALID
        assert action["invalid_reason"].startswith("optional_skip")


def test_normalize_action_supports_candidate_query_and_grounded_proposal() -> None:
    query = vision_agent.normalize_action(
        {
            "action": "query_candidate",
            "query": "Evidence for how the candidate applied AI and its impact",
            "source_scope": "resume",
            "reason": "The compact context lacks project detail.",
        }
    )
    proposal = vision_agent.normalize_action(
        {
            "action": "ask_user",
            "question": "How have you applied AI in your engineering work and what impact did it have?",
            "proposed_answer": "I built a document intelligence platform with semantic search and contextual chat.",
            "grounding_source": "resume",
            "grounding_quote": "semantic search and contextual chat",
            "reason": "Showing a grounded draft for approval.",
        }
    )

    assert query["invalid_reason"] == ""
    assert query["query"].startswith("Evidence for")
    assert query["source_scope"] == "resume"
    assert proposal["invalid_reason"] == ""
    assert proposal["proposed_answer"].startswith("I built")


def test_normalize_action_supports_non_mutating_coverage_confirmation() -> None:
    action = vision_agent.normalize_action(
        {
            "action": "ask_user",
            "control": 1,
            "question": "Have you previously worked for Example Corp?",
            "options": ["Yes", "No"],
            "proposed_answer": "No",
            "display_reason": "I found no matching employer in the bounded authorized sources.",
            "confirmation_required": True,
            "grounding_source": "",
            "grounding_quote": "",
            "reason": "Ask the user to confirm the bounded absence proposal.",
        }
    )

    assert action["invalid_reason"] == ""
    assert action["confirmation_required"] is True
    assert action["display_reason"].startswith("I found no matching")

    mutation = vision_agent.normalize_action(
        {
            "action": "click",
            "control": 2,
            "x": 100,
            "y": 200,
            "text": "No",
            "display_reason": "No match was found.",
            "confirmation_required": True,
        }
    )
    assert mutation["invalid_reason"] == "confirmation_fields_require_ask_user"


def test_normalize_action_drops_backend_internal_final_submit_flag() -> None:
    action = vision_agent.normalize_action({"action": "click", "x": 10, "y": 20, "allow_final_submit": True})

    assert action["action"] == "click"
    assert "allow_final_submit" not in action


def test_normalize_action_parses_verified_followup_sequence() -> None:
    action = vision_agent.normalize_action(
        {
            "action": "click",
            "control": 1,
            "x": 100,
            "y": 120,
            "reason": "Focus the email field.",
            "then": [
                {
                    "action": "input_text",
                    "control": 1,
                    "text": "a@b.com",
                    "grounding": {"source": "profile", "quote": "a@b.com"},
                }
            ],
        }
    )

    assert action["then"] == [
        {
            "action": "type",
            "control": 1,
            "x": 0,
            "y": 0,
            "text": "a@b.com",
            "key": "",
            "direction": "",
                "amount": 0,
                "artifact_role": "",
                "target": "",
                "wait_ms": 0,
            "button": "",
            "clicks": 0,
            "hold_ms": 0,
            "modifiers": [],
            "path": [],
            "to_x": 0,
            "to_y": 0,
            "follow": False,
            "target_text": "",
            "at_caret": False,
                "grounding_source": "profile",
            "grounding_quote": "a@b.com",
            "derivation": {},
            "invalid_reason": "",
        }
    ]
    assert action["sequence_invalid_reason"] == ""


def test_normalize_action_rejects_user_or_terminal_actions_inside_then() -> None:
    ask = vision_agent.normalize_action({"action": "click", "x": 10, "y": 20, "then": [{"action": "ask_user", "question": "Visible question?"}]})
    done = vision_agent.normalize_action({"action": "click", "x": 10, "y": 20, "then": [{"action": "done"}]})

    assert ask["then"] == [] and ask["sequence_invalid_reason"] == ""
    assert ask["then_dropped"][0]["reason"] == "then_action_not_allowed:ask_user"
    assert done["then_dropped"][0]["reason"] == "then_action_not_allowed:done"


def test_invalid_follow_up_drops_only_itself_and_names_the_missing_key() -> None:
    action = vision_agent.normalize_action(
        {
            "action": "type", "x": 10, "y": 20, "text": "a", "reason": "r",
            "grounding_source": "goal", "grounding_quote": "a",
            "then": [{"action": "click", "x": 5, "y": 6}, {"action": "key"}, {"action": "click", "x": 7, "y": 8}],
        }
    )
    assert action["_outcome_type"] != vision_agent.OUTCOME_SCHEMA_INVALID
    assert len(action["then"]) == 1 and action["then"][0]["action"] == "click"
    dropped = action["then_dropped"][0]
    assert dropped["index"] == 1 and dropped["reason"] == "missing_key" and '"key"' in dropped["expected"]


def test_normalize_done_preserves_model_owned_external_failure_terminal_outcome() -> None:
    action = vision_agent.normalize_action(
        {
            "action": "done",
            "terminal_outcome": "external_failure",
            "reason": "The page says the application submission was rejected.",
        }
    )

    assert action["action"] == "done"
    assert action["terminal_outcome"] == "external_failure"
    assert action["invalid_reason"] == ""
    schema = vision_agent.action_schema()["schema"]
    assert "terminal_outcome" in schema["properties"]
    assert "terminal_outcome" in schema["properties"]


def test_normalize_action_rejects_terminal_outcome_on_browser_mutation() -> None:
    action = vision_agent.normalize_action(
        {
            "action": "click",
            "x": 10,
            "y": 20,
            "terminal_outcome": "external_failure",
        }
    )

    assert action["invalid_reason"] == "terminal_outcome_requires_done"


def test_normalize_action_reads_point_array_and_other_coord_shapes() -> None:
    # MiMo returns point:[x,y] at some resolutions; we must read it, else coords default to (0,0) and the
    # click lands in the corner (the live bug).
    p = vision_agent.normalize_action({"action": "click", "point": [1669, 91]})
    assert (p["x"], p["y"]) == (1669, 91)
    c = vision_agent.normalize_action({"action": "click", "coordinate": [300, 400]})
    assert (c["x"], c["y"]) == (300, 400)
    t = vision_agent.normalize_action({"action": "click", "target": [630, 337]})
    assert (t["x"], t["y"]) == (630, 337)
    d = vision_agent.normalize_action({"action": "click", "position": {"x": 12, "y": 34}})
    assert (d["x"], d["y"]) == (12, 34)
    s = vision_agent.normalize_action({"type": "click", "start_point": [640, 558], "reason": "click button"})
    assert s["action"] == "click"
    assert (s["x"], s["y"]) == (640, 558)


def test_normalize_action_accepts_type_as_action_when_provider_uses_tool_type() -> None:
    action = vision_agent.normalize_action({"type": "type", "text": "+15550100142", "reason": "enter phone"})

    assert action["action"] == "type"
    assert action["text"] == "+15550100142"
    assert action["invalid_reason"] == ""


def test_normalize_action_accepts_string_action_input_for_type_text() -> None:
    action = vision_agent.normalize_action({"action": "type", "action_input": "Grounded answer text", "reason": "type answer"})

    assert action["action"] == "type"
    assert action["text"] == "Grounded answer text"
    assert action["invalid_reason"] == ""


def test_normalize_action_surfaces_grounding_citation_fields() -> None:
    action = vision_agent.normalize_action({
        "action": "type",
        "text": "Ada",
        "grounding": {"source": "Profile", "quote": "Ada Lovelace"},
        "reason": "type cited name",
    })
    alias = vision_agent.normalize_action({
        "action": "click",
        "x": 10,
        "y": 20,
        "text": "Yes",
        "groundingSource": "human_answer",
        "source_quote": "Yes",
        "reason": "select cited option",
    })

    assert action["grounding_source"] == "profile"
    assert action["grounding_quote"] == "Ada Lovelace"
    assert alias["grounding_source"] == "human_answer"
    assert alias["grounding_quote"] == "Yes"


def test_normalize_action_preserves_goal_grounding_citation() -> None:
    action = vision_agent.normalize_action(
        {
            "action": "click",
            "control": 4,
            "x": 300,
            "y": 220,
            "text": "7:00 PM",
            "grounding_source": "goal",
            "grounding_quote": "Choose the 7:00 PM showtime",
            "reason": "The requested showtime is visible.",
        }
    )

    assert action["grounding_source"] == "goal"
    assert action["grounding_quote"] == "Choose the 7:00 PM showtime"
    assert action["invalid_reason"] == ""


def test_normalize_action_accepts_provider_wrappers_and_aliases() -> None:
    action = vision_agent.normalize_action({"arguments": {"kind": "tap", "point": [25, 40], "rationale": "open menu"}})
    assert action["action"] == "click"
    assert (action["x"], action["y"]) == (25, 40)
    assert action["reason"] == "open menu"


def test_normalize_action_accepts_upload_role_aliases() -> None:
    action = vision_agent.normalize_action({"action": "upload_file", "point": [320, 180], "file_type": "Resume", "reason": "attach resume"})
    assert action["action"] == "upload"
    assert action["artifact_role"] == "resume"
    assert (action["x"], action["y"]) == (320, 180)


def test_normalize_action_accepts_center_coordinate_aliases() -> None:
    action = vision_agent.normalize_action({
        "action": "upload",
        "center_x": 634,
        "center_y": 207,
        "artifact_role": "resume",
        "reason": "attach resume",
    })

    assert action["action"] == "upload"
    assert (action["x"], action["y"]) == (634, 207)
    assert action["invalid_reason"] == ""


def test_normalize_action_accepts_nested_action_input_coordinates() -> None:
    action = vision_agent.normalize_action({"action": "click", "reason": "click visible button", "action_input": {"x": 634, "y": 563}})
    assert action["action"] == "click"
    assert (action["x"], action["y"]) == (634, 563)


def test_normalize_action_accepts_scroll_direction_and_amount_variants() -> None:
    up = vision_agent.normalize_action({"action": "scroll", "target_x": 640, "target_y": 200, "direction": "up", "reason": "look above"})
    assert up["action"] == "scroll"
    assert up["direction"] == "up"
    assert up["amount"] == 600
    assert (up["x"], up["y"]) == (640, 200)

    down = vision_agent.normalize_action({"action": "scroll", "scroll_direction": "down", "scroll_amount": 300, "reason": "look below"})
    assert down["direction"] == "down"
    assert down["amount"] == 300

    signed = vision_agent.normalize_action({"action": "scroll", "dy": -450, "reason": "look above"})
    assert signed["direction"] == "up"
    assert signed["amount"] == 450


def test_normalize_action_marks_blank_user_question_invalid() -> None:
    action = vision_agent.normalize_action({"action": "ask_user", "question": "this question"})
    assert action["action"] == "ask_user"
    assert action["invalid_reason"] == "missing_user_question"


def test_normalize_action_does_not_turn_reason_into_user_question() -> None:
    action = vision_agent.normalize_action({
        "action": "ask_user",
        "question": "",
        "reason": "I need to scroll down to find the field.",
    })

    assert action["action"] == "ask_user"
    assert action["question"] == ""
    assert action["invalid_reason"] == "missing_user_question"


def test_normalize_action_accepts_explicit_prompt_as_user_question() -> None:
    action = vision_agent.normalize_action({
        "action": "ask_user",
        "prompt": "What interests you about this role and working at Capacity?",
        "reason": "The answer needs the user's personal motivation.",
    })

    assert action["action"] == "ask_user"
    assert action["question"] == "What interests you about this role and working at Capacity?"
    assert action["invalid_reason"] == ""


def test_normalize_action_preserves_model_owned_labels_from_rich_choice_objects() -> None:
    action = vision_agent.normalize_action(
        {
            "action": "ask_user",
            "question": "How many years of experience do you have?",
            "options": [
                {"label": "0", "description": "No professional experience"},
                {"label": "5+", "description": "Five or more years"},
            ],
            "reason": "The visible choice is not grounded in user data.",
        }
    )

    assert action["_outcome_type"] == vision_agent.OUTCOME_VALID_ACTION
    assert action["options"] == ["0", "5+"]


def test_build_prompt_carries_goal_viewport_and_grounding() -> None:
    view = {"width": 1280, "height": 720, "url": "u", "title": "t", "scroll_y": 0, "scroll_height": 2000}
    prompt = vision_agent.build_prompt(view, {
        "goal": "Apply",
        "profile": {"first_name": "Ada"},
        "profile_provenance": [{"path": "sponsorship", "source": "user_profiles.sponsorship_status"}],
        "saved_answers": [{"q": "x", "a": "y"}],
        "human_answers": [{"question": "Built with LLMs?", "answer": "yes"}],
        "latest_candidate_query_result": {
            "query": "What is the candidate's LinkedIn profile URL?",
            "result_count": 1,
            "new_count": 0,
            "facts": [
                {
                    "source": "profile",
                    "label": "profile.contact.linkedin",
                    "value": "https://www.linkedin.com/in/ada",
                }
            ],
        },
        "available_documents": [{"role": "resume", "filename": "resume.pdf"}],
        "credential_vault": [
            {
                "id": 42,
                "origin": "https://example.test",
                "account_label": "Personal",
                "available_slots": ["email", "password", "token"],
                "auto_use": True,
                "password": "must-not-reach-model",
            }
        ],
        "page_memory": [{"title": "Job", "text": "Capacity builds AI-powered support automation.", "url": "https://example.test", "scroll_y": 0}],
        "control_memory": [{"scroll_y": 500, "controls": [{"context": "Expected salary", "text": "", "box": [10, 300, 100, 24]}]}],
        "page_evidence": {
            "viewport_text": "First required field " + ("extra " * 400),
            "controls": [
                {"index": 2, "tag": "div", "role": "combobox", "text": "No", "nearby_text": "Visa", "x": 10, "y": 200, "width": 100, "height": 24, "outer_html": "x" * 2000},
                {"index": 1, "tag": "div", "role": "combobox", "text": "Select", "context_text": "Work authorization*", "x": 10, "y": 100, "width": 100, "height": 24, "debug": "not for model"},
            ],
        },
    })
    assert prompt["goal"] == "Apply"
    assert prompt["task_context"] == "application"
    assert prompt["viewport"]["width"] == 1280
    assert prompt["user_profile"] == {"first_name": "Ada"}
    assert prompt["user_profile_provenance"] == [{"path": "sponsorship", "source": "user_profiles.sponsorship_status"}]
    assert prompt["human_answers"] == [{"question": "Built with LLMs?", "answer": "yes"}]
    assert prompt["latest_candidate_query_result"]["result_count"] == 1
    assert prompt["latest_candidate_query_result"]["new_count"] == 0
    assert prompt["latest_candidate_query_result"]["facts"][0]["value"] == "https://www.linkedin.com/in/ada"
    assert prompt["available_documents"] == [{"role": "resume", "filename": "resume.pdf"}]
    assert prompt["credential_vault"] == [
        {
            "id": 42,
            "origin": "https://example.test",
            "account_label": "Personal",
            "available_slots": ["email", "password"],
            "auto_use": True,
        }
    ]
    assert "must-not-reach-model" not in str(prompt)
    assert "return the top-level action `use_credential`" in prompt["instruction"]
    assert "exact-origin metadata item with auto_use=true" in prompt["instruction"]
    assert "securely ask for it in the sidebar" in prompt["instruction"]
    assert "`use_credential` is top-level only" in prompt["instruction"]
    assert prompt["page_memory"][0]["text"] == "Capacity builds AI-powered support automation."
    assert prompt["control_memory"][0]["controls"][0]["context"] == "Expected salary"
    # Visible controls are listed once, in control_inventory; page_evidence carries page text.
    assert "controls" not in prompt["page_evidence"]
    assert 1400 < len(prompt["page_evidence"]["viewport_text"]) <= 4000
    assert all("outer_html" not in row and "debug" not in row for row in prompt["control_inventory"])
    assert prompt["control_inventory"][0]["context"] == "Work authorization*"
    assert prompt["control_inventory"][0]["box"] == [10, 100, 100, 24]
    assert "viewport.width x viewport.height pixels" in prompt["instruction"]
    assert "focused a typable field" in prompt["instruction"]
    assert "To fill a visible text field, follow the Typing rule below" in prompt["instruction"]
    assert "`control` to the target field's index when it is listed in control_inventory" in prompt["instruction"]
    assert "Type with no control and no x,y only when the field that must receive the text is already focused" in prompt["instruction"]
    assert "use action=`key` with key=`Tab` to move focus" in prompt["instruction"]
    assert "Never return action=`type` with empty text" in prompt["instruction"]
    assert "ask_user only after allowed evidence is unavailable or exhausted" in prompt["instruction"]
    assert "satisfied_controls contains controls" in prompt["instruction"]
    assert "at most one generic key/click action" in prompt["instruction"]
    assert "document upload control is visible" in prompt["instruction"]
    assert "before manually typing lower text fields" in prompt["instruction"]
    assert "human_answers are explicit answers" in prompt["instruction"]
    assert "Grounding citation" in prompt["instruction"]
    assert "grounding_source plus a short verbatim grounding_quote" in prompt["instruction"]
    assert "set text to the chosen option label" in prompt["instruction"]
    assert "ordinary click that only activates, navigates, starts, continues, opens, dismisses, or focuses a control" in prompt["instruction"]
    assert "control plus x/y identify the target" in prompt["instruction"]
    assert "Never copy a button, link, or target label into text" in prompt["instruction"]
    assert "Use page only for values copied from the page" in prompt["instruction"]
    assert "a display label, heading" in prompt["instruction"]
    assert "is not the datum itself" in prompt["instruction"]
    assert "Work visible fillable controls from top to bottom" in prompt["instruction"]
    assert "Treat work authorization and sponsorship as distinct facts" in prompt["instruction"]
    assert "neighboring dropdown's value" in prompt["instruction"]
    assert "Fill every field you can ground" in prompt["instruction"]
    assert "whether it looks required or optional" in prompt["instruction"]
    assert "If a required field needs a value that is not grounded" in prompt["instruction"]
    assert "ask_user is for missing information that the rendered page makes required" in prompt["instruction"]
    assert "newly composed open-ended proposed_answer" in prompt["instruction"]
    assert "including a visible asterisk" in prompt["instruction"]
    assert "Never declare a field in `skipped_optional` merely because" in prompt["instruction"]
    assert "use ask_user instead of skipping" in prompt["instruction"]
    assert "Do NOT use ask_user to narrate" in prompt["instruction"]
    assert "`question` must be the non-empty exact visible question" in prompt["instruction"]
    assert "do not use ask_user" in prompt["instruction"]
    assert "take the click/type/upload action" in prompt["instruction"]
    assert "Source absence is not ordinary value grounding" in prompt["instruction"]
    assert "matching_fact_found=false and exhaustive=false" in prompt["instruction"]
    assert "Never type or click the proposed value until a human_answer confirms it" in prompt["instruction"]
    assert "Never infer exhaustive coverage from the presence of a profile" in prompt["instruction"]
    assert "A trusted profile attestation is a source-labelled canonical fact" in prompt["instruction"]
    assert "do not interrupt the user merely to confirm it" in prompt["instruction"]
    assert "Never encode scrolling as a click" in prompt["instruction"]
    assert "generic opt-out answers" in prompt["instruction"]
    assert "Never infer identity attributes" in prompt["instruction"]
    assert "from a name" in prompt["instruction"]
    assert "ask_user only when the rendered page makes the field required" in prompt["instruction"]
    assert "leave it unchanged and continue" in prompt["instruction"]
    assert "Leave ungrounded optional fields unchanged and continue" in prompt["instruction"]
    assert "skippable" not in prompt["instruction"]
    assert "default, common, safer, neutral, an opt-out" in prompt["instruction"]
    assert "Do not choose 'Choose not to disclose'" in prompt["instruction"]
    assert "invalid_model_action" in prompt["instruction"]
    assert "page_evidence as raw extra eyes" in prompt["instruction"]
    assert "page_memory is text this run's pages showed earlier that is no longer on the current page" in prompt["instruction"]
    assert "Use control_memory as raw prior controls observed at earlier scroll positions that are not on screen" in prompt["instruction"]
    assert "scroll back to that area" in prompt["instruction"]
    assert "control_inventory first" in prompt["instruction"]
    assert "For open-ended textareas" in prompt["instruction"]
    assert "type directly only when an exact answer is already approved" in prompt["instruction"]
    assert "return ask_user with that draft as `proposed_answer` before typing it" in prompt["instruction"]
    assert "Distinct questions require distinct, question-specific answers; never reuse a previous composition" in prompt["instruction"]
    assert "Do NOT scroll just because you acted" in prompt["instruction"]
    assert "Set direction to up, down, left, or right" in prompt["instruction"]
    assert "partly visible" in prompt["instruction"]
    assert "the goal is genuinely complete and no final browser action remains" in prompt["instruction"]
    assert "The safety body intercepts it before execution" in prompt["instruction"]
    assert "Reaching a submit control does not prove the preceding work is complete" in prompt["instruction"]
    assert "audit the earlier rendered questions in page_memory and control_memory" in prompt["instruction"]
    assert "no selected or checked option is not complete" in prompt["instruction"]
    assert "Before returning done" in prompt["instruction"]
    assert "Only click an option when the desired grounded value is not already selected" in prompt["instruction"]
    assert "If it is already selected, the field is complete" in prompt["instruction"]


def test_build_prompt_inventory_keeps_region_scroll_and_pointer_state() -> None:
    # The prompt promises scrollable regions carry scroll{...} and covered controls carry occluded;
    # building the inventory from already-compacted evidence silently dropped both.
    prompt = vision_agent.build_prompt(
        {"width": 1120, "height": 533, "url": "http://bench.test/ledger", "title": "Ledger", "scroll_y": 0, "scroll_height": 812},
        {
            "goal": "Find entry LX-437.",
            "page_evidence": {
                "viewport_text": "Ledger",
                "controls": [
                    {
                        "index": 3, "tag": "div", "text": "Ledger entry LG-000", "x": 16, "y": 225, "width": 565, "height": 264,
                        "scroll": {"top": 0, "height": 21600, "visible_height": 400},
                    },
                    {
                        "index": 4, "tag": "button", "text": "Lane 6", "x": 744, "y": 300, "width": 90, "height": 30,
                        "occluded": True, "covered": True, "covered_by": "div.sticky.bar", "clipped": False,
                        "focused": True, "accepts_text": False,
                    },
                ],
            },
        },
    )

    rows = {row["index"]: row for row in prompt["control_inventory"]}
    assert rows[3]["scroll"] == {"top": 0, "height": 21600, "visible_height": 400}
    assert rows[4]["occluded"] is True
    # Why it is occluded rides along as raw facts; a false reason is not emitted.
    assert rows[4]["covered"] is True and rows[4]["covered_by"] == "div.sticky.bar"
    assert "clipped" not in rows[4] and "behind_modal" not in rows[4]
    assert rows[4]["focused"] is True


def test_build_prompt_is_generic_for_non_application_browser_tasks() -> None:
    prompt = vision_agent.build_prompt(
        {
            "width": 1280,
            "height": 720,
            "url": "https://cinema.example/movies",
            "title": "Movie times",
            "scroll_y": 0,
            "scroll_height": 1600,
        },
        {
            "application_context": False,
            "candidate_data_access": "none",
            "goal": "Choose the 7:00 PM showtime and an aisle seat, then stop before payment.",
            "human_answers": [{"question": "Which date?", "answer": "Saturday"}],
            "verified_receipts": [
                {
                    "type": "browser_click",
                    "outcome": "verified_succeeded",
                    "evidence": "selected showtime 7:00 PM",
                }
            ],
            "page_evidence": {
                "viewport_text": "Showtimes 5:00 PM 7:00 PM",
                "controls": [],
            },
        },
    )

    instruction = prompt["instruction"]
    assert prompt["task_context"] == "generic"
    assert "grounding_source=`goal`" in instruction
    assert "copy a short exact contiguous quote from `goal`" in instruction
    assert "user preference, choice, or piece of information necessary" in instruction
    assert "This includes non-form tasks" in instruction
    assert "the page does not need to mark a field required" in instruction
    assert "direct answers from the current run" in instruction
    assert "approval_context_summary" in instruction
    assert prompt["verified_receipts"][0]["outcome"] == "verified_succeeded"
    assert "not durable answers from earlier runs" in instruction
    assert "Application context" not in instruction
    assert "open-ended application" not in instruction
    assert "document upload control" not in instruction
    assert "Work visible fillable controls from top to bottom" not in instruction
    assert "work authorization" not in instruction
    assert "sponsorship" not in instruction
    assert "race/ethnicity" not in instruction
    assert "profile.profile_memory" not in instruction
    assert "resume" not in instruction
    assert "user_profile" not in prompt
    assert "available_documents" not in prompt


def test_explicit_application_context_adds_only_application_guidance() -> None:
    prompt = vision_agent.build_prompt(
        {"width": 800, "height": 600, "url": "https://jobs.example/apply", "title": "Apply"},
        {
            "application_context": True,
            "candidate_data_access": "preloaded",
            "goal": "Complete this job application to the final review boundary.",
        },
    )

    assert prompt["task_context"] == "application"
    assert "Application context" in prompt["instruction"]
    assert "Work visible fillable controls from top to bottom" in prompt["instruction"]
    assert "Treat work authorization and sponsorship as distinct facts" in prompt["instruction"]
    assert "race/ethnicity" in prompt["instruction"]
    assert "This application run includes its authorized candidate context" in prompt["instruction"]
    assert "sources: none" not in prompt["instruction"]


def test_build_prompt_keeps_optional_skip_accounting_as_a_top_level_sidecar() -> None:
    prompt = vision_agent.build_prompt(
        {
            "width": 800,
            "height": 600,
            "url": "https://forms.example/review",
            "title": "Review",
        },
        {
            "application_context": True,
            "candidate_data_access": "preloaded",
            "goal": "Complete the form to its final review boundary.",
        },
    )

    instruction = prompt["instruction"]
    assert "`skipped_optional`" in instruction
    assert "top-level primary action" in instruction
    assert "never in `then`" in instruction
    assert "set `then: []`" in instruction
    assert "does not replace the primary action" in instruction
    assert "does not mutate the skipped control" in instruction


def test_generic_candidate_query_prompt_exposes_only_frozen_source_scopes() -> None:
    prompt = vision_agent.build_prompt(
        {"width": 800, "height": 600, "url": "https://forms.example/start", "title": "Form"},
        {
            "application_context": False,
            "candidate_data_access": "on_demand",
            "candidate_data_scopes": ["profile"],
            "goal": "Use my stored profile to fill the form, then stop before any final action.",
        },
    )

    assert prompt["candidate_data_scopes"] == ["profile"]
    assert "only to these stored candidate-data sources: profile" in prompt["instruction"]
    assert "Use source_scope=all only when profile, resume, and memory are all authorized" in prompt["instruction"]


def test_compact_control_inventory_preserves_raw_control_context_top_to_bottom() -> None:
    inventory = vision_agent.compact_control_inventory(
        {
            "controls": [
                {"index": 9, "tag": "li", "role": "option", "text": "5+", "nearby_text": "5+", "x": 30, "y": 500, "width": 200, "height": 40},
                {
                    "index": 1,
                    "tag": "div",
                    "role": "combobox",
                    "id": "field-24",
                    "aria_controls": "field-24-list",
                    "aria_labelledby": "field-24-label",
                    "class_name": "choice selected",
                    "aria_pressed": "true",
                    "data_state": "on",
                    "visual_state": {
                        "background_color": "rgb(20, 20, 20)",
                        "color": "rgb(255, 255, 255)",
                    },
                    "text": "Select",
                    "context_text": "Are you authorized?*",
                    "accessibility_context": [{"relation": "aria-labelledby", "text": "Are you authorized?*"}],
                    "dom_context": [
                        {"relation": "previous_sibling", "depth": 2, "text": "Are you authorized?*"},
                        {"relation": "previous_sibling", "depth": 3, "text": "Earlier neighboring question"},
                    ],
                    "ancestor_chain": [
                        {"depth": 1, "tag": "div", "id": "field-container", "role": "group"},
                    ],
                    "x": 30,
                    "y": 50,
                    "width": 200,
                    "height": 24,
                },
                {"index": 2, "tag": "div", "role": "combobox", "text": "No", "nearby_text": "Visa sponsorship?", "x": 30, "y": 150, "width": 200, "height": 24},
            ]
        }
    )

    assert [item["index"] for item in inventory] == [1, 2, 9]
    assert inventory[0]["kind"] == "div/combobox"
    assert inventory[0]["text"] == "Select"
    assert inventory[0]["context"] == "Are you authorized?*"
    assert inventory[0]["accessibility_context"][0]["relation"] == "aria-labelledby"
    assert inventory[0]["aria_controls"] == "field-24-list"
    assert inventory[0]["class_name"] == "choice selected"
    assert inventory[0]["aria_pressed"] == "true"
    assert inventory[0]["data_state"] == "on"
    assert inventory[0]["visual_state"]["background_color"] == "rgb(20, 20, 20)"
    assert inventory[0]["ancestor_chain"][0]["id"] == "field-container"
    assert inventory[0]["dom_context"][0] == {
        "relation": "previous_sibling",
        "text": "Are you authorized?*",
        "depth": 2,
    }


def test_build_prompt_does_not_leak_optional_demographic_default() -> None:
    view = {"width": 1280, "height": 720, "url": "u", "title": "t", "scroll_y": 0, "scroll_height": 2000}
    prompt = vision_agent.build_prompt(
        view,
        {
            "goal": "Apply",
            "profile": {
                "sensitive_answer_rules": {"invent_sensitive_facts": False},
            },
        },
    )

    rendered = str(prompt)
    assert "Prefer not to say when available" not in rendered
    assert "eeo_default" not in rendered


def test_decide_returns_none_without_a_screenshot() -> None:
    class _Model:
        def _call_model_json(self, **_: object) -> dict:  # pragma: no cover - must not be called
            raise AssertionError("model called without an image")

    assert vision_agent.decide_next_action(_Model(), None, {"width": 800, "height": 600}, {}) is None


def test_final_action_readiness_audit_withholds_when_rendered_questions_are_pending() -> None:
    captured: dict[str, object] = {}

    class _Model:
        def _call_model_json(self, **kwargs: object) -> dict:
            captured.update(kwargs)
            return {
                "ok": True,
                "data": {
                    "complete": False,
                    "summary": "A required choice has no selected option.",
                    "pending": [
                        {
                            "question": "Are you legally authorized to work?",
                            "evidence": "Neither Yes nor No is selected.",
                            "next_step": "Return to the choice and select the grounded answer.",
                        }
                    ],
                },
                "metadata": {"provider": "fixture", "model": "fixture-auditor"},
            }

    result = vision_agent.audit_final_action_readiness(
        _Model(),
        {"media_type": "image/jpeg", "data": "B64"},
        {
            "goal": "Complete the form",
            "control_memory": [
                {
                    "controls": [
                        {
                            "context": "Are you legally authorized to work?",
                            "text": "Yes",
                            "checked": False,
                        }
                    ]
                }
            ],
            "session_ledger": [],
        },
    )

    assert result["ok"] is True
    assert result["complete"] is False
    assert result["pending"][0]["question"] == "Are you legally authorized to work?"
    assert captured["purpose"] == "vision_final_action_readiness_audit"
    assert "final submit control being visible is not evidence" in captured["prompt"]["instruction"]
    assert "sibling visual_state differences" in captured["prompt"]["instruction"]
    assert "different application is pending" in captured["prompt"]["instruction"]
    assert "current referral/attribution parameter is pending" in captured["prompt"]["instruction"]
    assert "page-enforced input limit as complete" in captured["prompt"]["instruction"]
    assert "bounded raw decomposition of the current document URL" in captured["prompt"]["instruction"]
    assert "direct current-run human corrections" in captured["prompt"]["instruction"]


def test_application_prompt_prefers_current_referral_evidence_and_shortens_limited_answers() -> None:
    prompt = vision_agent.build_prompt(
        {
            "width": 800,
            "height": 600,
            "url": "https://example.test/apply?utm_source=jobright",
        },
        {
            "goal": "Apply for this job",
            "profile": {},
            "profile_provenance": [],
            "resume_facts": {},
            "saved_answers": [
                {"question": "How did you find us?", "answer": "LinkedIn"}
            ],
            "human_answers": [
                {
                    "question": "What interests you about the role?",
                    "answer": "A long approved professional answer",
                }
            ],
        },
    )

    rendered = prompt["instruction"]
    assert "Referral/source answers are likewise task-bound" in rendered
    assert "CURRENT URL and rendered page evidence first" in rendered
    assert "current attribution/referral query parameter is page evidence" in rendered
    assert "page-enforced input limit truncates or rejects" in rendered
    assert "shorter faithful version" in rendered
    assert prompt["url_query_parameters"] == [
        {"name": "utm_source", "value": "jobright"}
    ]
    assert "operator_instructions are direct, current-run human corrections" in rendered


def test_final_action_readiness_audit_withholds_unsatisfied_operator_instruction() -> None:
    captured: dict[str, object] = {}

    class _Model:
        def _call_model_json(self, **kwargs: object) -> dict:
            captured.update(kwargs)
            return {
                "ok": True,
                "data": {
                    "complete": True,
                    "summary": "The form is otherwise complete.",
                    "pending": [],
                    "instructions_satisfied": False,
                    "instruction_checks": [
                        {
                            "instruction": "Use Jobright as the source.",
                            "satisfied": False,
                            "evidence": "The current rendered value is LinkedIn.",
                            "next_step": "Replace LinkedIn with Jobright and verify the read-back.",
                        }
                    ],
                },
                "metadata": {},
            }

    result = vision_agent.audit_final_action_readiness(
        _Model(),
        {"media_type": "image/jpeg", "data": "B64"},
        {
            "goal": "Complete the form",
            "operator_instructions": [
                {
                    "instruction": "Use Jobright as the source.",
                    "source": "human_review_correction",
                }
            ],
        },
    )

    assert result["complete"] is False
    assert result["instructions_satisfied"] is False
    assert result["pending"][0]["question"] == "Operator instruction: Use Jobright as the source."
    assert captured["prompt"]["operator_instructions"] == [
        {
            "instruction": "Use Jobright as the source.",
            "source": "human_review_correction",
        }
    ]


def test_final_action_readiness_audit_requires_no_pending_items_for_completion() -> None:
    class _Model:
        def _call_model_json(self, **_: object) -> dict:
            return {
                "ok": True,
                "data": {
                    "complete": True,
                    "summary": "All preceding controls have current verified values.",
                    "pending": [],
                },
                "metadata": {},
            }

    result = vision_agent.audit_final_action_readiness(
        _Model(),
        {"media_type": "image/jpeg", "data": "B64"},
        {"goal": "Complete the form"},
    )

    assert result["complete"] is True


def test_decide_passes_the_image_and_parses_the_action() -> None:
    captured: dict = {}

    class _Model:
        def _call_model_json(self, **kw: object) -> dict:
            captured.update(kw)
            return {"ok": True, "data": {"action": "click", "x": 100, "y": 200, "text": "", "key": "", "question": "", "options": [], "reason": "click Apply"}}

    img = {"media_type": "image/jpeg", "data": "B64"}
    action = vision_agent.decide_next_action(_Model(), img, {"width": 800, "height": 600}, {"goal": "Apply"})
    assert captured.get("image") == img
    assert action and action["action"] == "click" and action["x"] == 100 and action["y"] == 200


def test_decide_consumes_the_same_prepared_input_envelope_without_rebuilding(monkeypatch) -> None:
    captured: dict = {}
    prepared = {
        "system": "prepared system",
        "prompt": {"instruction": "prepared prompt"},
        "schema": {"name": "prepared_schema", "schema": {"type": "object"}},
    }

    class _Model:
        def _call_model_json(self, **kw: object) -> dict:
            captured.update(kw)
            return {
                "ok": True,
                "data": {
                    "action": "done",
                    "x": 1,
                    "y": 1,
                    "text": "",
                    "key": "",
                    "question": "",
                    "options": [],
                    "reason": "The task is ready for review.",
                },
            }

    monkeypatch.setattr(
        vision_agent,
        "build_prompt",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("prepared input was rebuilt")),
    )
    action = vision_agent.decide_next_action(
        _Model(),
        {"media_type": "image/jpeg", "data": "B64"},
        {"width": 800, "height": 600},
        {"_prepared_model_input": prepared},
    )

    assert action and action["action"] == "done"
    assert captured["system"] is prepared["system"]
    assert captured["prompt"] is prepared["prompt"]
    assert captured["schema"] is prepared["schema"]


def test_decide_threads_max_tokens_override_from_context() -> None:
    captured: dict = {}

    class _Model:
        def _call_model_json(self, **kw: object) -> dict:
            captured.update(kw)
            return {"ok": True, "data": {"action": "done", "x": 1, "y": 1, "text": "", "key": "", "question": "", "options": [], "reason": "review ready"}}

    action = vision_agent.decide_next_action(
        _Model(),
        {"media_type": "image/jpeg", "data": "B64"},
        {"width": 800, "height": 600},
        {"goal": "Review", "max_tokens_override": 4096},
    )

    assert action and action["action"] == "done"
    assert captured["max_tokens_override"] == 4096


def test_decide_threads_timeout_seconds_override_from_context() -> None:
    captured: dict = {}

    class _Model:
        def _call_model_json(self, **kw: object) -> dict:
            captured.update(kw)
            return {"ok": True, "data": {"action": "done", "x": 1, "y": 1, "text": "", "key": "", "question": "", "options": [], "reason": "review ready"}}

    action = vision_agent.decide_next_action(
        _Model(),
        {"media_type": "image/jpeg", "data": "B64"},
        {"width": 800, "height": 600},
        {"goal": "Review", "timeout_seconds_override": 70},
    )

    assert action and action["action"] == "done"
    assert captured["timeout_seconds_override"] == 70


def test_decide_attaches_safe_model_metadata() -> None:
    class _Model:
        def _call_model_json(self, **_: object) -> dict:
            return {
                "ok": True,
                "data": {"action": "done", "x": 0, "y": 0, "text": "", "key": "", "question": "", "options": [], "reason": "review ready"},
                "metadata": {
                    "provider": "openrouter",
                    "parse_status": "json",
                    "raw_output_excerpt": "{\"action\":\"done\"}",
                    "token_usage": {"prompt_tokens": 1200, "completion_tokens": 40},
                    "context_window_tokens": 128000,
                    "context_window_source": "known_model_profile:test",
                    "context_window_known": True,
                    "transport_boundary": "provider_http_json_body_before_serialization",
                    "provider_request_body_prepared": True,
                    "provider_http_json_body_sha256": "body-hash",
                    "schema_requested": True,
                    "provider_requested_schema_sha256": "requested-schema-hash",
                    "schema_included": False,
                    "upstream_provider": "Together",
                },
            }

    action = vision_agent.decide_next_action(_Model(), {"media_type": "image/jpeg", "data": "B64"}, {"width": 800, "height": 600}, {})
    assert action and action["_model_metadata"]["provider"] == "openrouter"
    # The serving host reaches VISION_MODEL_TRANSPORT_RECEIPT; latency differs by host.
    assert action["_model_metadata"]["upstream_provider"] == "Together"
    assert action["_model_metadata"]["parse_status"] == "json"
    assert "raw_output_excerpt" not in action["_model_metadata"]
    assert action["_model_metadata"]["token_usage"]["prompt_tokens"] == 1200
    assert action["_model_metadata"]["context_window_tokens"] == 128000
    assert action["_model_metadata"]["provider_request_body_prepared"] is True
    assert action["_model_metadata"]["provider_http_json_body_sha256"] == "body-hash"
    assert action["_model_metadata"]["schema_requested"] is True
    assert action["_model_metadata"]["provider_requested_schema_sha256"] == "requested-schema-hash"
    assert action["_model_metadata"]["schema_included"] is False


def test_decide_empty_provider_output_is_not_laundered_into_ask_user() -> None:
    class _Model:
        def _call_model_json(self, **_: object) -> dict:
            return {
                "ok": True,
                "data": {},
                "metadata": {
                    "provider": "openrouter",
                    "model": "vision-model",
                    "parse_status": "empty",
                    "raw_output_empty": True,
                    "raw_output_chars": 0,
                    "finish_reason": "length",
                },
            }

    outcome = vision_agent.decide_next_action(_Model(), {"media_type": "image/jpeg", "data": "B64"}, {"width": 800, "height": 600}, {})

    assert outcome is not None
    assert outcome["_outcome_type"] == vision_agent.OUTCOME_EMPTY_COMPLETION
    assert outcome["failure_reason"] == "provider_empty_output"
    assert outcome.get("action") is None
    assert outcome["_model_metadata"]["raw_output_empty"] is True
    assert outcome["_model_metadata"]["finish_reason"] == "length"


def test_decide_empty_json_object_uses_empty_completion_recovery() -> None:
    class _Model:
        def _call_model_json(self, **_: object) -> dict:
            return {
                "ok": True,
                "data": {},
                "metadata": {
                    "provider": "openrouter",
                    "model": "vision-model",
                    "parse_status": "json",
                    "raw_output_empty": False,
                    "raw_output_chars": 2,
                    "raw_output_excerpt": "{}",
                    "finish_reason": "stop",
                },
            }

    outcome = vision_agent.decide_next_action(_Model(), {"media_type": "image/jpeg", "data": "B64"}, {"width": 800, "height": 600}, {})

    assert outcome is not None
    assert outcome["_outcome_type"] == vision_agent.OUTCOME_EMPTY_COMPLETION
    assert outcome["failure_reason"] == "provider_empty_object"
    assert outcome.get("action") is None
    assert "raw_output_excerpt" not in outcome["_model_metadata"]


def test_decide_non_mapping_provider_result_is_internal_error() -> None:
    class _Model:
        def _call_model_json(self, **_: object) -> object:
            return None

    outcome = vision_agent.decide_next_action(_Model(), {"media_type": "image/jpeg", "data": "B64"}, {"width": 800, "height": 600}, {})

    assert outcome is not None
    assert outcome["_outcome_type"] == vision_agent.OUTCOME_INTERNAL_ERROR
    assert outcome["failure_reason"] == "internal_model_result_error"
    assert outcome.get("action") is None


def test_history_navigation_actions_are_part_of_the_vocabulary() -> None:
    from app import vision_agent

    assert "back" in vision_agent.ACTIONS
    assert "forward" in vision_agent.ACTIONS
    assert "back" not in vision_agent.THEN_ACTIONS


def test_wheel_point_requires_both_coordinates() -> None:
    from app.extension_run import _vision_wheel_point

    assert _vision_wheel_point({"action": "scroll", "x": 320, "y": 210}) == (320, 210)
    assert _vision_wheel_point({"action": "scroll", "x": 0, "y": 210}) is None
    assert _vision_wheel_point({"action": "scroll"}) is None
    assert _vision_wheel_point({"action": "scroll", "x": "12", "y": "oops"}) is None


def test_prompt_carries_an_open_browser_dialog_verbatim():
    from app.vision_agent import _native_dialog_for_prompt

    assert _native_dialog_for_prompt({"native_dialog": {"type": "prompt", "message": "Enter the archive code:", "default_prompt": "", "typed_prompt": "PX-1"}}) == {
        "type": "prompt",
        "message": "Enter the archive code:",
        "typed_prompt": "PX-1",
    }
    assert _native_dialog_for_prompt({"viewport_text": "no dialog"}) is None


def test_switch_tab_needs_a_tab_target_and_the_prompt_lists_only_multiple_run_tabs():
    from app.vision_agent import _browser_tabs_for_prompt, normalize_action

    ok = normalize_action({"action": "switch_tab", "target": "812", "reason": "Return to the task tab."})
    assert ok["action"] == "switch_tab" and ok["target"] == "812" and not ok.get("invalid_reason")
    aliased = normalize_action({"action": "switch_tab", "tab_id": "1138290245", "reason": "Return to the desk tab."})
    assert aliased["target"] == "1138290245" and not aliased.get("invalid_reason")
    missing = normalize_action({"action": "switch_tab", "reason": "Return."})
    assert missing.get("invalid_reason") == "missing_tab_target"

    assert _browser_tabs_for_prompt({"browser_tabs": [{"tab_id": "1", "title": "Task", "active": True}]}) is None
    tabs = _browser_tabs_for_prompt(
        {"browser_tabs": [{"tab_id": "1", "title": "Archive", "url": "u1", "active": False}, {"tab_id": "2", "title": "Relay", "url": "u2", "active": True}]}
    )
    assert [tab["tab_id"] for tab in tabs] == ["1", "2"] and tabs[1]["active"] is True


def test_switch_surface_needs_a_real_surface_and_is_offered_only_when_both_exist() -> None:
    assert vision_agent.normalize_action({"action": "switch_surface", "target": "desktop", "reason": "r"})["invalid_reason"] == ""
    assert vision_agent.normalize_action({"action": "switch_surface", "target": "", "reason": "r"})["invalid_reason"] == "missing_switch_surface_target"
    assert vision_agent.normalize_action({"action": "switch_surface", "target": "tab-2", "reason": "r"})["invalid_reason"] == "missing_switch_surface_target"
    assert "switch_surface" not in vision_agent.THEN_ACTIONS

    both = json.dumps(vision_agent.build_prompt({"width": 800, "height": 600}, {"goal": "g", "execution_surface": "browser", "surfaces": ["browser", "desktop"], "page_evidence": {}}))
    one = json.dumps(vision_agent.build_prompt({"width": 800, "height": 600}, {"goal": "g", "execution_surface": "browser", "surfaces": ["browser"], "page_evidence": {}}))
    assert "Two surfaces" in both and '"surfaces": ["browser", "desktop"]' in both
    assert "Two surfaces" not in one and '"surfaces"' not in one


def test_type_at_caret_is_the_models_choice_and_defaults_to_replacing() -> None:
    from app.vision_agent import action_schema, normalize_action

    assert normalize_action({"action": "type", "control": 1, "text": "X", "reason": "r"})["at_caret"] is False
    assert normalize_action({"action": "type", "control": 1, "text": "X", "at_caret": True, "reason": "r"})["at_caret"] is True
    schema = action_schema()["schema"]
    assert "at_caret" in schema["properties"] and "at_caret" in schema["properties"]


def test_a_wait_can_last_through_an_eight_second_hold() -> None:
    normalized = vision_agent.normalize_action(
        {"action": "wait", "wait_ms": 9000, "text": "Commit now", "reason": "The window opens after the hold."}
    )
    assert normalized["wait_ms"] == 9000
    assert vision_agent.normalize_action({"action": "wait", "wait_ms": 90000, "reason": "x"})["wait_ms"] == 30000


def test_a_click_can_name_a_control_that_does_not_exist_yet_by_its_label() -> None:
    normalized = vision_agent.normalize_action(
        {
            "action": "click", "x": 120, "y": 300, "reason": "Hold, then commit when the window opens.",
            "then": [
                {"action": "wait", "wait_ms": 9000, "text": "Commit now"},
                {"action": "click", "target_text": " Commit ", "reason": "Press Commit."},
            ],
        }
    )
    assert normalized["then"][1]["target_text"] == "Commit"
    assert not normalized["then"][1]["invalid_reason"]
    schema = vision_agent.action_schema()["schema"]
    assert "target_text" in schema["properties"] and "target_text" in schema["properties"]


def test_a_follow_up_keeps_the_whole_gesture_the_model_described() -> None:
    # A then-drag used to lose its destination and route (dragging to 0,0), and a then double-click
    # or shift-click arrived as a plain click.
    action = vision_agent.normalize_action(
        {
            "action": "click",
            "x": 10,
            "y": 10,
            "reason": "Select the first file, then extend and move.",
            "then": [
                {"action": "click", "x": 30, "y": 40, "clicks": 2, "modifiers": ["shift"], "hold_ms": 300, "button": "right"},
                {"action": "drag", "x": 5, "y": 5, "to_x": 50, "to_y": 60, "path": [{"x": 20, "y": 25}], "modifiers": ["ctrl"], "hold_ms": 700},
            ],
        }
    )

    click, drag = action["then"]
    assert action["sequence_invalid_reason"] == ""
    assert (click["clicks"], click["modifiers"], click["hold_ms"], click["button"]) == (2, ["shift"], 300, "right")
    assert (drag["to_x"], drag["to_y"], drag["path"]) == (50, 60, [{"x": 20, "y": 25}])
    assert (drag["modifiers"], drag["hold_ms"]) == (["ctrl"], 700)


def test_navigate_takes_an_absolute_web_url_and_reload_needs_nothing() -> None:
    opened = vision_agent.normalize_action({"action": "navigate", "text": "https://example.com/careers", "target": "new_tab"})
    assert (opened["action"], opened["text"], opened["target"], opened["invalid_reason"]) == (
        "navigate",
        "https://example.com/careers",
        "new_tab",
        "",
    )
    assert vision_agent.normalize_action({"action": "open_url", "text": "https://example.com"})["action"] == "navigate"
    assert vision_agent.normalize_action({"action": "navigate"})["invalid_reason"] == "missing_navigate_url"
    for bad in ("javascript:alert(1)", "file:///C:/x.txt", "example.com", "https://user:pw@example.com/"):
        assert vision_agent.normalize_action({"action": "navigate", "text": bad})["invalid_reason"] == "invalid_navigate_url", bad
    assert (
        vision_agent.normalize_action({"action": "navigate", "text": "https://example.com", "target": "window"})["invalid_reason"]
        == "invalid_navigate_target"
    )
    reload = vision_agent.normalize_action({"action": "refresh"})
    assert (reload["action"], reload["invalid_reason"]) == ("reload", "")
    then_actions = vision_agent.action_schema()["schema"]["properties"]["then"]["items"]["properties"]["action"]["enum"]
    assert "navigate" not in then_actions and "reload" not in then_actions


def test_held_keys_on_a_scroll_ride_only_on_a_wheel_at_a_point() -> None:
    zoom = vision_agent.normalize_action({"action": "scroll", "x": 400, "y": 300, "direction": "up", "modifiers": ["ctrl"]})
    assert (zoom["modifiers"], zoom["invalid_reason"]) == (["ctrl"], "")
    # Naming the control the point sits in (as the general rule asks) keeps the wheel at the point.
    named = vision_agent.normalize_action(
        {"action": "scroll", "control": 12, "x": 300, "y": 400, "direction": "up", "amount": 3, "modifiers": ["ctrl"], "reason": "zoom in"}
    )
    assert (named["control"], named["x"], named["y"], named["modifiers"], named["invalid_reason"]) == (0, 300, 400, ["ctrl"], "")
    for unbound in (
        {"action": "scroll", "direction": "down", "modifiers": ["ctrl"]},
        {"action": "scroll", "control": 4, "modifiers": ["shift"]},
        {"action": "scroll", "x": 400, "y": 300, "text": "Total", "modifiers": ["ctrl"]},
    ):
        assert vision_agent.normalize_action(unbound)["invalid_reason"] == "scroll_modifiers_need_wheel_point"


def test_the_prompt_describes_the_address_bar_and_held_gestures() -> None:
    schema = vision_agent.action_schema()["schema"]
    assert "For drag" in schema["properties"]["hold_ms"]["description"]
    assert "For key" in schema["properties"]["hold_ms"]["description"]
    assert "ctrl-drag" in schema["properties"]["modifiers"]["description"]
    assert "For hover" in schema["properties"]["wait_ms"]["description"]
    assert "new_tab" in schema["properties"]["target"]["description"]
    prompt = vision_agent.build_prompt({"width": 800, "height": 600, "url": "https://example.com"}, {})
    assert "navigate / reload" in prompt["instruction"]


def test_a_drag_route_is_read_from_point_pairs_and_never_silently_flattened() -> None:
    # Pointer-hell runs: DeepSeek/GLM (json_object, no schema) wrote path as [x, y] pairs, the parser
    # dropped every point, and each corridor trace ran as a straight drag.
    base = {"action": "drag", "x": 60, "y": 507, "to_x": 377, "to_y": 457, "reason": "Trace the conduit."}
    pairs = vision_agent.normalize_action({**base, "path": [[110, 495], [215, 415], [318, 483]]})
    assert pairs["path"] == [{"x": 110, "y": 495}, {"x": 215, "y": 415}, {"x": 318, "y": 483}]
    assert not pairs.get("invalid_reason")
    objects = vision_agent.normalize_action({**base, "path": [{"x": 110, "y": 495}]})
    assert objects["path"] == [{"x": 110, "y": 495}]
    as_text = vision_agent.normalize_action({**base, "path": "[[110, 495], [215, 415]]"})
    assert as_text["path"] == [{"x": 110, "y": 495}, {"x": 215, "y": 415}]
    unreadable = vision_agent.normalize_action({**base, "path": [110, 495, 215]})
    assert unreadable.get("invalid_reason") == "unreadable_drag_path"
    straight = vision_agent.normalize_action(base)
    assert straight["path"] == [] and not straight.get("invalid_reason")


def test_the_key_names_line_gives_the_shape_of_a_route() -> None:
    line = vision_agent.output_keys_instruction(vision_agent.action_schema())
    assert '{"x": int, "y": int}' in line


def test_a_route_written_whole_in_path_carries_its_own_press_and_release_points() -> None:
    # After routes started to flow, the brain wrote the whole route in path and left x,y or to_x,to_y
    # out; those drags were refused as missing an origin or a destination.
    whole = vision_agent.normalize_action(
        {"action": "drag", "path": [[60, 507], [110, 475], [215, 415], [377, 457]], "reason": "Trace."}
    )
    assert (whole["x"], whole["y"], whole["to_x"], whole["to_y"]) == (60, 507, 377, 457)
    assert whole["path"] == [{"x": 110, "y": 475}, {"x": 215, "y": 415}]
    assert not whole.get("invalid_reason")
    origin_given = vision_agent.normalize_action(
        {"action": "drag", "x": 60, "y": 507, "path": [[110, 475], [377, 457]], "reason": "Trace."}
    )
    assert (origin_given["to_x"], origin_given["to_y"]) == (377, 457)
    assert origin_given["path"] == [{"x": 110, "y": 475}]
    one_point = vision_agent.normalize_action({"action": "drag", "path": [[60, 507]], "reason": "Trace."})
    assert one_point.get("invalid_reason") == "missing_drag_origin"


def test_folder_view_reaches_the_model_bounded() -> None:
    evidence = {
        "viewport_text": "x",
        "folder_view": {"path": r"C:\Users\me\inbox", "items": [f"file-{i}.txt" for i in range(80)] + ["a.png.png"], "item_count": 81},
    }
    compact = vision_agent.compact_page_evidence(evidence)
    assert compact["folder_view"]["path"] == r"C:\Users\me\inbox"
    assert len(compact["folder_view"]["items"]) == 60 and compact["folder_view"]["item_count"] == 81
    assert "folder_view" not in vision_agent.compact_page_evidence({"viewport_text": "x"})


def test_a_key_with_modifiers_becomes_the_chord_a_keyboard_sends() -> None:
    # v4_text run 1688: key "b" with modifiers ["ctrl"] typed "b" over the selected word instead of bolding it.
    assert vision_agent.normalize_action({"action": "key", "key": "b", "modifiers": ["ctrl"], "reason": "r"})["key"] == "Ctrl+b"
    assert vision_agent.normalize_action({"action": "key", "key": "Ctrl+b", "modifiers": ["ctrl"], "reason": "r"})["key"] == "Ctrl+b"
    assert vision_agent.normalize_action({"action": "key", "key": "Tab", "modifiers": ["shift"], "reason": "r"})["key"] == "Shift+Tab"
    assert vision_agent.normalize_action({"action": "key", "key": "Enter", "reason": "r"})["key"] == "Enter"


def test_a_key_no_keyboard_has_is_refused_not_pressed() -> None:
    # v4_scroll run 1702: "ctrl+scroll_up" and "ctrl+wheel_up" went out as key events that did nothing.
    for unknown in ("ctrl+scroll_up", "Ctrl+wheel_up", "scroll_down", "mouse_wheel"):
        assert vision_agent.normalize_action({"action": "key", "key": unknown, "reason": "zoom"})["invalid_reason"] == "unknown_key", unknown
    for known in ("Enter", "Ctrl+B", "Shift+Tab", "page_down", "PgDn", "F2", "Ctrl++", "NUMPAD5", "Ctrl+Shift+End", "a", "?", "Esc"):
        assert vision_agent.normalize_action({"action": "key", "key": known, "reason": "r"})["invalid_reason"] == "", known
    # Held modifiers fold into the chord first, and the chord is what is checked.
    folded = vision_agent.normalize_action({"action": "key", "key": "End", "modifiers": ["ctrl"], "reason": "r"})
    assert (folded["key"], folded["invalid_reason"]) == ("Ctrl+End", "")


def test_notes_drop_the_step_labels_a_brain_copies_back_and_join_a_list() -> None:
    # v5_reconcile run 1712: the brain copied your_notes' "step N:" labels into new notes and once sent a
    # list, which piled up as "step 8: step 8: ..." and "['step 11: ...']".
    clean = vision_agent._clean_notes
    assert clean("step 8: step 8: Inv4: Northwind INV-3823 $6,402.02") == "Inv4: Northwind INV-3823 $6,402.02"
    assert clean(["step 11: Inv7: Brightwater INV-5315 $648.96"]) == "Inv7: Brightwater INV-5315 $648.96"
    assert clean("['step 12: Inv8: Tidewater INV-6357']") == "Inv8: Tidewater INV-6357"
    assert clean("call sign Heron-6; next step 3: check the override") == "call sign Heron-6; next step 3: check the override"
    assert clean(None) == "" and len(clean("x" * 3000)) == vision_agent.NOTES_PER_DECISION_CHARS


def test_the_brain_is_told_to_note_whatever_a_later_report_could_ask() -> None:
    # v5_switchboard run 1740: the page said the line windows keep no history and a report would ask about
    # the shift; the brain never noted the stolen-card caller's account and could not answer.
    text = open(vision_agent.__file__, encoding="utf-8").read()
    assert "debrief at the end, windows that keep no history), note, the moment you see it, every fact such a" in text
    assert "every identifier or count a later report or debrief could ask about" in vision_agent.NOTES_DESCRIPTION


def test_the_inventory_shows_each_surrounding_text_once() -> None:
    # v6_docs: the same task cards rode in every row's dom_context (18 of 33 thousand characters a step).
    card = {"relation": "previous_sibling", "text": "1. Review the suggestions ...", "depth": 4}
    controls = [
        {"index": 1, "tag": "button", "text": "Accept", "x": 10, "y": 10, "width": 50, "height": 20,
         "dom_context": [{"relation": "previous_sibling", "text": "LintPilot: Insert the", "depth": 0}, card]},
        {"index": 2, "tag": "button", "text": "Reject", "x": 70, "y": 10, "width": 50, "height": 20,
         "dom_context": [{"relation": "previous_sibling", "text": "Accept", "depth": 0}, card]},
        {"index": 3, "tag": "button", "text": "Save", "x": 10, "y": 60, "width": 50, "height": 20,
         "dom_context": [card]},
    ]
    rows = vision_agent.compact_control_inventory({"controls": controls}, limit=10)
    assert [item["text"] for item in rows[0]["dom_context"]] == ["LintPilot: Insert the", card["text"]]
    assert [item["text"] for item in rows[1]["dom_context"]] == ["Accept"]
    assert "dom_context" not in rows[2]


def test_inventory_drops_bare_ancestors_and_baseline_style():
    from app.vision_agent import compact_control_inventory

    style = {"background_color": "rgb(1, 1, 1)", "color": "rgb(2, 2, 2)"}
    controls = [
        {"index": i, "tag": "button", "text": f"b{i}", "x": 0, "y": i * 30, "width": 20, "height": 20,
         "visual_state": dict(style), "ancestor_chain": [{"depth": 1, "tag": "div"}, {"depth": 2, "tag": "ul", "role": "listbox"}]}
        for i in range(1, 5)
    ]
    controls[0]["visual_state"] = {"background_color": "rgb(9, 9, 9)", "color": "rgb(2, 2, 2)"}
    rows = compact_control_inventory({"controls": controls})
    assert rows[0]["visual_state"]["background_color"] == "rgb(9, 9, 9)"
    assert all("visual_state" not in row for row in rows[1:])
    assert rows[0]["ancestor_chain"] == [{"depth": 2, "tag": "ul", "role": "listbox"}]


def test_controls_that_share_a_label_carry_their_row():
    rows = vision_agent.compact_control_inventory({"controls": [
        {"index": 1, "tag": "button", "text": "Link", "x": 10, "y": 10, "width": 40, "height": 20, "row_text": "Diego Lund diego@x.example"},
        {"index": 2, "tag": "button", "text": "Link", "x": 10, "y": 40, "width": 40, "height": 20, "row_text": "Robert Pena robert@y.example"},
        {"index": 3, "tag": "button", "text": "Save", "x": 10, "y": 70, "width": 40, "height": 20, "row_text": "Footer"},
    ]})
    assert [row.get("row") for row in rows] == ["Diego Lund diego@x.example", "Robert Pena robert@y.example", None]
    assert all("_row" not in row for row in rows)


def test_build_prompt_inventory_keeps_list_position_of_clipped_list_options() -> None:
    prompt = vision_agent.build_prompt(
        {"width": 1120, "height": 533, "url": "http://bench.test/cal", "title": "Cal", "scroll_y": 0, "scroll_height": 812},
        {
            "goal": "Set the start time to 15:30.",
            "page_evidence": {
                "viewport_text": "Start time",
                "controls": [
                    {"index": 1, "tag": "li", "role": "option", "text": "09:00", "x": 20, "y": 100, "width": 80, "height": 20},
                    {"index": 2, "tag": "li", "role": "option", "text": "15:30", "x": 20, "y": 400, "width": 80, "height": 20,
                     "list_position": "in list, scrolled out"},
                ],
            },
        },
    )
    rows = {row["index"]: row for row in prompt["control_inventory"]}
    assert "list_position" not in rows[1]
    assert rows[2]["list_position"] == "in list, scrolled out"
    assert "list_position on a control" in prompt["instruction"]


def test_then_followup_carries_derivation_and_string_derivation_is_refused_with_keys() -> None:
    from app.derivation_check import verify_derivation

    derivation = {"op": "sum", "inputs": [{"quote": "a 1", "value": "1"}], "operand": "", "rule_quote": "r"}
    action = vision_agent._normalize_action_payload(
        {
            "action": "click",
            "control": 1,
            "then": [{"action": "type", "control": 2, "text": "1", "grounding_source": "page", "grounding_quote": "r", "derivation": derivation}],
        },
        include_then=True,
    )
    assert action["then"][0]["derivation"]["op"] == "sum"
    result = verify_derivation(vision_agent._clean_derivation("1+2=3"), "3", goal_text="", page_text="")
    assert result.status == "invalid" and "OBJECT" in result.note and "rule_quote" in result.note


def test_done_answer_is_optional_bounded_and_only_on_done() -> None:
    plain = vision_agent.normalize_action({"action": "done", "reason": "ok"})
    assert plain["answer"] == "" and plain["invalid_reason"] == ""
    got = vision_agent.normalize_action({"action": "done", "reason": "ok", "answer": "  pw " + chr(10) + " 123 " + "x" * 900})
    assert got["answer"].startswith("pw 123 xxx") and len(got["answer"]) == 500
    assert vision_agent.normalize_action({"action": "wait", "answer": "x", "reason": "r"})["answer"] == ""
    schema = vision_agent.action_schema()["schema"]
    assert "answer" in schema["properties"] and "answer" not in schema["required"]


def test_compact_control_inventory_keeps_long_field_values_and_marks_every_cut() -> None:
    whole = "Gravida magna consectetur. " * 20
    rows = vision_agent.compact_control_inventory(
        {
            "controls": [
                {"index": 1, "tag": "textarea", "value": whole, "x": 0, "y": 0, "width": 100, "height": 50},
                {"index": 2, "tag": "textarea", "value": "z" * 2600, "x": 0, "y": 60, "width": 100, "height": 50},
                {"index": 3, "tag": "button", "text": "ok", "value": "q" * 400, "x": 0, "y": 120, "width": 10, "height": 10},
                {"index": 4, "tag": "input", "value": "[REDACTED]", "x": 0, "y": 140, "width": 10, "height": 10},
            ]
        }
    )
    by_index = {row["index"]: row for row in rows}
    assert by_index[1]["value"] == whole and whole.endswith(" ")
    assert "value_truncated" not in by_index[1]
    assert len(by_index[2]["value"]) <= 2000 and by_index[2]["value_truncated"] is True
    assert by_index[2]["value_length"] == 2600
    assert len(by_index[3]["value"]) == 160 and by_index[3]["value_truncated"] is True
    assert by_index[3]["value_length"] == 400
    assert "value_truncated" not in by_index[4]


def test_compact_control_inventory_keeps_text_values_verbatim() -> None:
    value = "  lead  gap\nline two\n\ntrail "
    rows = vision_agent.compact_control_inventory(
        {
            "controls": [
                {"index": 1, "tag": "textarea", "value": value, "x": 0, "y": 0, "width": 100, "height": 50},
                {"index": 2, "tag": "input", "value": "abc ", "x": 0, "y": 60, "width": 100, "height": 50},
                {"index": 3, "tag": "button", "text": "ok", "value": " a  b ", "x": 0, "y": 120, "width": 10, "height": 10},
            ]
        }
    )
    by_index = {row["index"]: row for row in rows}
    assert by_index[1]["value"] == value
    assert by_index[2]["value"] == "abc "
    assert by_index[3]["value"] == "a b"


def test_compact_control_inventory_shares_one_value_budget() -> None:
    controls = [
        {"index": i, "tag": "textarea", "value": "w" * 2000, "x": 0, "y": i * 60, "width": 100, "height": 50}
        for i in range(1, 8)
    ]
    rows = vision_agent.compact_control_inventory({"controls": controls})
    assert sum(len(row["value"]) for row in rows) <= 6000 + 160 * len(rows)
    assert all(row.get("value_truncated") or len(row["value"]) == 2000 for row in rows)


def test_prompt_passes_css_scale_and_states_the_rule() -> None:
    scaled = vision_agent.build_prompt({"width": 1120, "height": 700, "css_px_per_screenshot_px": 1.27}, {"goal": "g"})
    assert scaled["viewport"]["css_px_per_screenshot_px"] == 1.27
    plain = vision_agent.build_prompt({"width": 1120, "height": 700}, {"goal": "g"})
    assert "css_px_per_screenshot_px" not in plain["viewport"]
    assert "divide by css_px_per_screenshot_px" in plain["instruction"]


def test_type_text_keeps_gated_text_stripped_and_carries_verbatim_text_exact() -> None:
    typed = vision_agent.normalize_action({"action": "type", "text": "abc ", "reason": "r", "then": [{"action": "type", "text": " x ", "reason": "r"}]})
    assert typed["text"] == "abc" and typed["text_exact"] == "abc "
    assert typed["then"][0]["text"] == "x" and typed["then"][0]["text_exact"] == " x "
    fallback = vision_agent.normalize_action({"action": "type", "action_input": "abc ", "reason": "r"})
    assert fallback["text"] == "abc" and fallback["text_exact"] == "abc "
    assert vision_agent.normalize_action({"action": "type", "text": "abc", "reason": "r"}).get("text_exact", "") == ""
    assert vision_agent.normalize_action({"action": "done", "answer": "ok ", "text": "ok ", "reason": "r"}).get("text_exact", "") == ""


def test_exact_type_text_dispatch_invariant() -> None:
    from app import extension_run

    assert extension_run._exact_type_text({"text": "abc", "text_exact": "abc "}) == "abc "
    assert extension_run._exact_type_text({"text": "abc", "text_exact": "abd "}) == "abc"
    assert extension_run._exact_type_text({"text": "abc", "text_exact": "abc "}, sensitive=True) == "abc"
    assert extension_run._exact_type_text({"text": "abc"}) == "abc"


def test_exact_type_text_allows_only_surrounding_plain_spaces() -> None:
    from app.extension_run import _exact_type_text

    assert _exact_type_text({"text": "abc", "text_exact": "abc "}) == "abc "
    assert _exact_type_text({"text": "abc", "text_exact": "  abc"}) == "  abc"
    # a newline or tab could act as Enter/Tab on a key-sending body: never typed from text_exact
    assert _exact_type_text({"text": "abc", "text_exact": "abc\n"}) == "abc"
    assert _exact_type_text({"text": "abc", "text_exact": "abc\t"}) == "abc"
    assert _exact_type_text({"text": "abc", "text_exact": "abc\u00a0"}) == "abc"
    assert _exact_type_text({"text": "abc", "text_exact": "abc "}, sensitive=True) == "abc"


def test_emphasized_text_reaches_only_the_brain_packet_and_viewport_text_is_unchanged() -> None:
    evidence = {
        "viewport_text": "Lorem Neque, dolor sit amet.",
        "emphasized_text": [{"text": "Neque,", "style": "bold"}, {"text": "x" * 300, "style": "italic"}, "junk"],
    }
    view = {"width": 1280, "height": 720, "url": "u", "title": "t", "scroll_y": 0, "scroll_height": 2000}
    prompt = vision_agent.build_prompt(view, {"goal": "Bold the word", "page_evidence": evidence})
    sent = json.dumps(prompt, ensure_ascii=False)
    assert '"Neque,"' in sent and '"style": "bold"' in sent
    assert "page_evidence.emphasized_text lists formatted spans" in sent
    assert "x" * 121 not in sent
    # Judges and quote checks read compact_page_evidence without the flag: no emphasized_text, same viewport_text.
    plain = vision_agent.compact_page_evidence(evidence)
    assert "emphasized_text" not in plain
    brain = vision_agent.compact_page_evidence(evidence, include_emphasized=True)
    assert brain["viewport_text"] == plain["viewport_text"] == "Lorem Neque, dolor sit amet."
    assert brain["emphasized_text"][0] == {"text": "Neque,", "style": "bold"}
    assert len(brain["emphasized_text"][1]["text"]) == 120


def test_a_verified_field_value_is_shown_once_in_the_session_ledger() -> None:
    ledger = [
        {"control": "Email", "action": "type", "attempts": 1, "value": "a@b.co"},
        {"control": "Secret", "action": "type", "attempts": 1, "value": "[REDACTED]", "value_redacted": True},
    ]
    satisfied = [
        {"control": "Email", "value": "a@b.co", "source": "goal"},
        {"control": "Email", "value": "other@b.co", "source": "goal"},
        {"control": "Plan", "value": "Pro", "source": "goal"},
    ]
    assert vision_agent.satisfied_controls_beyond_ledger(satisfied, ledger) == satisfied[1:]
    recent = [
        "type[Email] ok value='a@b.co' :: fill email",
        "type[Email] ok value='a@b.co' (field already has text) :: with a hint",
        "type[Email] FAIL value='a@b.co' :: failed",
        "click[Next] ok :: go",
        "type[Email] ok value='old@b.co' :: earlier value",
        "type[Email] ok value='a@b.co' :: newest lines stay",
        "type[Secret] ok value='[REDACTED]' :: stays",
        "click[Done] ok :: x",
    ]
    kept = vision_agent.recent_actions_beyond_ledger(recent, ledger)
    assert kept == recent[1:]
    # The newest lines are never dropped: last_reply and the next step read them as the last step's outcome.
    assert vision_agent.recent_actions_beyond_ledger(recent[:3], ledger) == recent[:3]
    # No ledger: nothing is dropped.
    assert vision_agent.recent_actions_beyond_ledger(recent, []) == recent
    prompt = vision_agent.build_prompt(
        {"width": 800, "height": 600, "url": "http://x/"},
        {"goal": "g", "session_ledger": ledger, "satisfied_controls": satisfied, "recent_actions": recent},
    )
    assert prompt["satisfied_controls"] == satisfied[1:] and prompt["recent_actions"] == recent[1:]
    assert prompt["session_ledger"] == ledger


def test_the_prompt_states_each_rule_once() -> None:
    model_input = vision_agent.prepare_model_input(
        {"width": 800, "height": 600, "url": "http://x/", "title": "x", "scroll_y": 0, "scroll_height": 600},
        {"goal": "Do it", "execution_surface": "browser"},
    )
    system = model_input["system"]
    assert system.count("Use done only when") == 1
    assert system.count("External failure has priority") == 1 and system.count("do not dismiss, retry, or investigate") == 1
    assert system.count("never click a combobox, checkbox, or option to confirm") == 1
    assert system.count("Typing: set action=`type`") == 1
    assert "type with no control and no x,y" not in system
