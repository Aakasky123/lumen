"""Single-target-first click protocol: a valid control id is the target, and the other descriptors (a point, the
click's text) are reconciled with it instead of refusing the step; only a genuine conflict is refused, with one
repair line the brain can act on next step."""

from __future__ import annotations

from app import extension_run
from app.db import init_db

from tests.test_extension_run import _ConflictingControlReferenceBody, _run_scripted


def _radio(index: int, label: str, value: str, y: int, *, visible_label: bool = True) -> dict:
    row = {
        "index": index,
        "tag": "spl-radio",
        "role": "radio",
        "text": "",
        "value": value,
        "derived_label": label,
        "selector": f"#radio-{index}",
        "x": 100,
        "y": y,
        "width": 600,
        "height": 40,
    }
    if visible_label:
        row["derived_label_visible"] = True
    return row


# --- the click's text reconciled with the id's own words ---------------------------------------------------------


def test_a_custom_radio_is_named_by_its_own_shown_label_not_its_value_attribute() -> None:
    # Runs 68-75: "Yes" on a radio whose value attribute is "1" was refused 30+ times as click_target_text_mismatch.
    yes = _radio(36, "Yes", "1", 100)
    assert extension_run._click_target_identity_check({"action": "click", "text": "Yes"}, yes)["ok"] is True
    assert extension_run._click_target_identity_check({"action": "click", "text": "yes"}, yes)["ok"] is True
    long_label = _radio(50, "No, I do not have a disability and have not had one in the past.", "0", 200)
    assert extension_run._click_target_identity_check(
        {"action": "click", "text": "No, I do not have a disability and have not had one in the past."}, long_label
    )["ok"] is True


def test_a_label_the_body_derived_from_attributes_or_class_names_never_names_the_control() -> None:
    # Only words the person sees beside the control count; "c spl wrapper primary" (class words) does not.
    derived = _radio(36, "Yes", "1", 100, visible_label=False)
    check = extension_run._click_target_identity_check({"action": "click", "text": "Yes"}, derived)
    assert check["ok"] is False and check["reason"] == "click_target_text_mismatch"


def test_the_shown_label_still_refuses_a_different_option() -> None:
    no = _radio(37, "No", "0", 140)
    check = extension_run._click_target_identity_check({"action": "click", "text": "Yes"}, no)
    assert check["ok"] is False and check["reason"] == "click_target_text_mismatch"
    assert check["observed_text"] == "No", "the refusal quotes what the person sees, not the value attribute"


def test_a_mismatch_names_the_one_listed_control_whose_own_text_is_the_value() -> None:
    # Runs 30/41: index on 'Accept' with text 'Decline' (an off-by-one id). Not rebound: the brain fixes the id.
    accept = {"index": 14, "tag": "button", "text": "Accept", "selector": "#accept"}
    decline = {"index": 16, "tag": "button", "text": "Decline", "selector": "#decline"}
    maybe = {"index": 15, "tag": "button", "text": "Maybe", "selector": "#maybe"}
    index_map = {i: {"selector": c["selector"], "control": c} for i, c in ((14, accept), (15, maybe), (16, decline))}
    line = extension_run._click_target_mismatch_feedback("Decline", "Accept", accept, index_map)
    assert "NOT RUN" in line and "'Decline' is control 16" in line and "control=16" in line


def test_a_mismatch_with_identical_labels_elsewhere_names_no_single_control() -> None:
    # Identical-label buttons in two sections: the text cannot pick one, so the line lists both and binds nothing.
    save = {"index": 3, "tag": "button", "text": "Save", "selector": "#save-a"}
    delete_a = {"index": 4, "tag": "button", "text": "Delete", "selector": "#delete-a"}
    delete_b = {"index": 8, "tag": "button", "text": "Delete", "selector": "#delete-b"}
    index_map = {c["index"]: {"selector": c["selector"], "control": c} for c in (save, delete_a, delete_b)}
    line = extension_run._click_target_mismatch_feedback("Delete", "Save", save, index_map)
    assert "Several listed controls" in line and "4, 8" in line and "is control" not in line


def test_a_list_opener_named_by_the_option_it_should_choose_is_told_to_open_it_first() -> None:
    # Run 75: 'Asian' on the 'Race/Ethnicity' combobox.
    combobox = {"index": 35, "tag": "input", "role": "combobox", "text": "Race/Ethnicity", "aria_controls": "menu-eth"}
    check = extension_run._click_target_identity_check({"action": "click", "text": "Asian"}, combobox)
    assert check["ok"] is False
    line = extension_run._click_target_mismatch_feedback("Asian", "Race/Ethnicity", combobox, {})
    assert "opens a list" in line and "text empty" in line


def test_the_index_and_the_point_are_reconciled_by_the_radios_shown_label() -> None:
    yes = _radio(36, "Yes", "1", 100)
    no = _radio(37, "No", "0", 140)
    resolved = extension_run._reconcile_action_control_references(
        {"action": "click", "text": "No"}, indexed_control=no, coordinate_control=yes
    )
    assert resolved["ok"] is True and resolved["source"] == "index_exact_text"
    resolved = extension_run._reconcile_action_control_references(
        {"action": "click", "text": "Yes"}, indexed_control=no, coordinate_control=yes
    )
    assert resolved["ok"] is True and resolved["source"] == "coordinate_exact_text"


# --- the point reconciled with the id ----------------------------------------------------------------------------


def _resolve(action: dict, bound: dict, coordinate: dict | None, source: str, controls: list[dict], index: int) -> dict:
    return extension_run._control_point_conflict_resolution(
        action,
        bound_control=bound,
        coordinate_control=coordinate,
        reconciliation_source=source,
        page_evidence={"controls": controls},
        coord_scale=1.0,
        control_index=index,
    )


def test_a_point_just_off_the_ids_box_on_nothing_is_that_control_at_its_centre() -> None:
    # Runs 27/29: the point sat 15px under the named button; the brain re-sent the same id next step.
    button = {"index": 36, "tag": "button", "text": "Create account", "selector": "#create", "x": 78, "y": 250, "width": 245, "height": 30}
    result = _resolve({"action": "click", "control": 36, "x": 150, "y": 295}, button, None, "index", [button], 36)
    assert result["ok"] is True and result["source"] == "index_point_near_miss"
    assert result["point"] == (200, 265)


def test_a_point_far_from_the_ids_box_is_refused_with_one_repair_line() -> None:
    # Runs 35/38: the point was ~200px right of a row checkbox, on the clickable mail row around it (not listed);
    # the brain meant the row. Clicking the checkbox would have been a misclick.
    checkbox = {"index": 18, "tag": "input", "type": "checkbox", "text": "Select Offsite", "selector": "#cb", "x": 180, "y": 410, "width": 12, "height": 12}
    result = _resolve({"action": "click", "control": 18, "x": 390, "y": 415}, checkbox, None, "index", [checkbox], 18)
    assert result["ok"] is False
    assert "on no listed control" in result["detail"] and "control=18 alone" in result["detail"]
    assert "control=0 with the point" in result["detail"] and "(186,416)" in result["detail"]


def test_a_near_point_on_another_control_is_the_id_only_when_the_text_names_the_id() -> None:
    # Runs 74/75: id 'No', text 'No', point 15px into the 'Yes' row above it.
    yes = _radio(36, "Yes", "1", 100)
    no = _radio(37, "No", "0", 140)
    action = {"action": "click", "control": 37, "x": 400, "y": 130, "text": "No"}
    by_text = _resolve(action, no, yes, "index_exact_text", [yes, no], 37)
    assert by_text["ok"] is True and by_text["source"] == "index_exact_text_point_near_miss"
    assert by_text["point"] == (400, 160)
    # The same geometry decided by the arbitration judge, or by nothing, stays a conflict (run 1901).
    by_judge = _resolve(action, no, yes, "model_index", [yes, no], 37)
    assert by_judge["ok"] is False and "on control 36" in by_judge["detail"]


def test_a_text_decided_point_far_off_on_another_control_is_still_refused() -> None:
    delete = {"index": 4, "tag": "button", "text": "Delete", "selector": "#delete", "x": 100, "y": 100, "width": 80, "height": 30}
    save = {"index": 5, "tag": "button", "text": "Save", "selector": "#save", "x": 400, "y": 100, "width": 80, "height": 30}
    result = _resolve({"action": "click", "control": 4, "x": 420, "y": 110, "text": "Delete"}, delete, save, "index_exact_text", [delete, save], 4)
    assert result["ok"] is False


def test_a_near_point_closer_to_another_listed_control_is_refused() -> None:
    # A point in the gap between two options, nearer the other one: which was meant is not known.
    first = {"index": 1, "tag": "div", "role": "option", "text": "A", "selector": "#a", "x": 100, "y": 100, "width": 200, "height": 30}
    second = {"index": 2, "tag": "div", "role": "option", "text": "B", "selector": "#b", "x": 100, "y": 140, "width": 200, "height": 30}
    result = _resolve({"action": "click", "control": 1, "x": 150, "y": 138}, first, None, "index", [first, second], 1)
    assert result["ok"] is False


def test_a_near_point_on_the_list_around_the_option_is_that_option() -> None:
    # The listbox row that holds the option is around the target, not a second target; far off it still refuses.
    listbox = {"index": 9, "tag": "div", "role": "listbox", "text": "A B", "selector": "#list", "x": 80, "y": 80, "width": 300, "height": 200}
    option = {"index": 10, "tag": "div", "role": "option", "text": "A", "selector": "#opt-a", "x": 100, "y": 100, "width": 200, "height": 30}
    near = _resolve({"action": "click", "control": 10, "x": 150, "y": 140}, option, listbox, "index", [listbox, option], 10)
    assert near["ok"] is True and near["point"] == (200, 115)
    far = _resolve({"action": "click", "control": 10, "x": 150, "y": 250}, option, listbox, "index", [listbox, option], 10)
    assert far["ok"] is False and "on control 9" in far["detail"]


def test_a_canvas_id_with_a_point_outside_its_box_is_refused() -> None:
    # On a canvas/svg the point says which painted part is meant; a point outside its box names none of it.
    canvas = {"index": 3, "tag": "canvas", "text": "", "selector": "#chart", "x": 100, "y": 100, "width": 300, "height": 200}
    result = _resolve({"action": "click", "control": 3, "x": 250, "y": 310}, canvas, None, "index", [canvas], 3)
    assert result["ok"] is False and "canvas/svg" in result["detail"]


class _PointBody(_ConflictingControlReferenceBody):
    async def vision_click_control(self, selector: str, fallback_x: int, fallback_y: int, **_binding: object) -> dict:
        self.control_clicks.append((selector, fallback_x, fallback_y))
        return {
            "ok": True,
            "action": "click",
            "mode": "control_selector",
            "surface_changed": True,
            "target_role": "combobox",
            "target_text": "Select" if selector == "#experience" else "No",
        }


def test_a_near_miss_point_clicks_the_named_control_at_its_centre(monkeypatch) -> None:
    init_db()
    body = _PointBody()
    contexts = _run_scripted(monkeypatch, body, [
        {"action": "click", "control": 9, "x": 250, "y": 270, "reason": "Open experience."},
    ], max_steps=2)
    assert body.control_clicks == [("#experience", 250, 240)]
    notes = [item for context in contexts for item in context.get("recent_actions", [])]
    assert any("control_reference_reconciled" in item and "control=9 was used" in item for item in notes)
    assert not any("control_point_conflict" in item for item in notes)


def test_a_far_point_is_not_clicked_and_the_brain_is_told_both_ways_to_fix_it(monkeypatch) -> None:
    init_db()
    body = _PointBody()
    contexts = _run_scripted(monkeypatch, body, [
        {"action": "click", "control": 9, "x": 250, "y": 420, "reason": "Open experience."},
    ], max_steps=2)
    assert body.control_clicks == []
    notes = [item for context in contexts for item in context.get("recent_actions", [])]
    assert any(
        "control_point_conflict" in item and "control=9 alone" in item and "on no listed control" in item
        for item in notes
    )


def test_a_point_on_the_other_control_with_no_deciding_text_stays_a_conflict(monkeypatch) -> None:
    # The id names one field, the point another, and nothing but a model guess could pick: nothing is clicked.
    init_db()
    body = _PointBody()
    monkeypatch.setattr(
        extension_run,
        "_call_control_reference_judge",
        lambda *args, **kwargs: {"choice": "index", "reason": "the id"},
    )
    _run_scripted(monkeypatch, body, [
        {"action": "click", "control": 7, "x": 250, "y": 240, "reason": "Open."},
    ], max_steps=2)
    assert body.control_clicks == []


def test_a_text_mismatch_refusal_names_the_control_that_reads_the_text(monkeypatch) -> None:
    init_db()
    body = _PointBody()
    contexts = _run_scripted(monkeypatch, body, [
        {"action": "click", "control": 7, "text": "Select", "reason": "Open experience."},
    ], max_steps=2)
    assert body.control_clicks == []
    notes = [item for context in contexts for item in context.get("recent_actions", [])]
    assert any("click_target_text_mismatch" in item and "'Select' is control 9" in item for item in notes)


# --- a listed control the body has no selector for ---------------------------------------------------------------


def test_a_selectorless_listed_control_is_reached_by_its_own_point_first_in_a_chain() -> None:
    row = {"index": 42, "tag": "input", "role": "combobox", "text": "Worked here before?", "x": 100, "y": 300, "width": 600, "height": 50}
    evidence = {"controls": [row]}
    on = {"action": "click", "control": 42, "x": 400, "y": 320}
    assert extension_run._primary_point_on_listed_control(on, evidence, 42, 1.0, batch_index=0, kind="click") is True
    assert extension_run._primary_point_on_listed_control(on, evidence, 42, 1.0, batch_index=1, kind="click") is False
    off = {"action": "click", "control": 42, "x": 400, "y": 400}
    assert extension_run._primary_point_on_listed_control(off, evidence, 42, 1.0, batch_index=0, kind="click") is False
    with_selector = {"controls": [{**row, "selector": "#q"}]}
    assert extension_run._primary_point_on_listed_control(on, with_selector, 42, 1.0, batch_index=0, kind="click") is False
    assert extension_run._primary_point_on_listed_control(on, evidence, 42, 1.0, batch_index=0, kind="scroll") is False


def test_a_selectorless_listed_control_refusal_says_how_to_reach_it() -> None:
    # Run 75 re-sent "type control=42" twenty times after "control=42 is not resolvable".
    row = {"index": 42, "tag": "input", "role": "combobox", "text": "Worked here before?", "x": 100, "y": 300, "width": 600, "height": 50}
    evidence = {"controls": [row]}
    typed = extension_run._listed_unresolvable_control_detail(evidence, 42, "type", 1.0)
    assert "no selector" in typed and "(400,325)" in typed and "then with control=0" in typed
    clicked = extension_run._listed_unresolvable_control_detail(evidence, 42, "click", 1.0)
    assert "point alone" in clicked and "(400,325)" in clicked
    credential = extension_run._listed_unresolvable_control_detail(evidence, 42, "use_credential", 1.0)
    assert "point" not in credential, "a credential never goes in by point"


class _SelectorlessFieldBody:
    def __init__(self) -> None:
        self.point_clicks: list[tuple[int, int]] = []
        self.control_clicks: list[str] = []
        self.keys: list[str] = []

    async def capture_screenshot(self) -> dict:
        return {
            "ok": True,
            "image": "jpeg",
            "width": 800,
            "height": 600,
            "url": "https://example.test/screening",
            "title": "Screening",
            "scroll_y": 0,
            "scroll_height": 600,
            "page_evidence": {
                "viewport_text": "Have you worked here before?",
                "controls": [
                    {"index": 42, "tag": "input", "role": "combobox", "type": "text", "text": "Have you worked here before?",
                     "x": 100, "y": 300, "width": 600, "height": 50},
                ],
            },
        }

    async def vision_click(self, x: int, y: int, **_kwargs: object) -> dict:
        self.point_clicks.append((x, y))
        return {"ok": True, "action": "click", "mode": "coordinate", "surface_changed": True}

    async def vision_click_control(self, selector: str, fallback_x: int, fallback_y: int, **_kwargs: object) -> dict:
        self.control_clicks.append(selector)
        return {"ok": False, "action": "click", "failure_reason": "control_not_found"}

    async def vision_key(self, key: str, **_kwargs: object) -> dict:
        self.keys.append(key)
        return {"ok": True, "action": "key", "key": key}


def test_a_chain_led_by_a_selectorless_control_with_its_own_point_runs(monkeypatch) -> None:
    init_db()
    body = _SelectorlessFieldBody()
    contexts = _run_scripted(monkeypatch, body, [
        {"action": "click", "control": 42, "x": 400, "y": 320, "reason": "Open the question's list.",
         "then": [{"action": "key", "key": "ArrowDown"}]},
    ], max_steps=2)
    assert body.point_clicks == [(400, 320)]
    notes = [item for context in contexts for item in context.get("recent_actions", [])]
    assert not any("control_index_invalid" in item and "NOT RUN" in item for item in notes)


def test_a_selectorless_control_named_by_id_alone_in_a_chain_is_refused_with_its_point(monkeypatch) -> None:
    init_db()
    body = _SelectorlessFieldBody()
    contexts = _run_scripted(monkeypatch, body, [
        {"action": "click", "control": 42, "reason": "Open the question's list.",
         "then": [{"action": "key", "key": "ArrowDown"}]},
    ], max_steps=2)
    assert body.point_clicks == [] and body.control_clicks == []
    notes = [item for context in contexts for item in context.get("recent_actions", [])]
    assert any("control_index_invalid" in item and "(400,325)" in item for item in notes)
