from app.extension_run import _reply_item_target
from app.receipt_view import receipt_line


def _r(label, text, kind="browser_click"):
    return {"type": kind, "target": {"label": label}, "verification": {"target_text": text}}


def test_action_name_label_is_not_shown_as_target():
    assert receipt_line(_r("click", "Move boat right"), detailed=False) == "click 'Move boat right'"


def test_short_container_label_reads_as_context():
    assert receipt_line(_r("hanoi", "Select"), detailed=False) == "click 'Select' in 'hanoi'"


def test_reason_sentence_label_is_dropped():
    assert receipt_line(_r("Load goat first.", "Load goat"), detailed=False) == "click 'Load goat' (your note: Load goat first.)"


def test_label_without_text_kept():
    assert receipt_line(_r("Email", ""), detailed=False) == "click 'Email'"


def test_heading_bound_label_replaced_by_control_text():
    item = {"bound_label": "Button Megastar Challenge (Easy) Welcome to the easy version",
            "bound_control": {"text": "Maybe this one? Click to find out!"}, "action": {}}
    assert _reply_item_target(item, {"action": "click"}, 60) == "Maybe this one? Click to find out!"


def test_bare_point_click_names_its_point_and_where_it_landed():
    # WebGames buttons-hard run 1919: a click on unlisted text was named by the heading it landed in.
    item = {"bound_label": "Button Megastar Challenge (Hard) Welcome", "bound_control": {"text": "Button Megastar"}}
    assert _reply_item_target(item, {"action": "click", "x": 617, "y": 471}, 80) == (
        "at 617,471 inside 'Button Megastar Challeng'"
    )
    assert _reply_item_target({}, {"action": "click", "x": 5, "y": 6}, 80) == "at 5,6"
    assert _reply_item_target(item, {"action": "click", "x": 1, "y": 2, "control": 9}, 80) != "at 1,2"


def test_a_key_to_the_focused_element_names_its_chord():
    # WebGames key-combo-easy run 1934: Shift+A read as "primary key 'at 0,0' verified".
    assert _reply_item_target({}, {"action": "key", "key": "Shift+A", "x": 0, "y": 0}, 60) == "Shift+A"
    assert _reply_item_target({}, {"action": "key", "key": "Enter", "control": 4}, 60) == "control 4"


def test_a_pointless_action_has_no_point_label_and_a_right_click_says_its_button():
    # WebGames maze run 1941 ("primary back 'at 0,0'") and right-click run 1940.
    assert _reply_item_target({}, {"action": "back", "x": 0, "y": 0}, 60) == ""
    assert _reply_item_target({}, {"action": "click", "x": 560, "y": 200, "button": "right"}, 60) == (
        "at 560,200 (right button)"
    )
    assert _reply_item_target({}, {"action": "click", "x": 5, "y": 6, "button": "left"}, 60) == "at 5,6"
