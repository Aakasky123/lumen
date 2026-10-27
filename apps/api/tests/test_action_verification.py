from app.extension_run import _vision_action_verified

ACTION = {"kind": "type", "text": "Ada Lovelace"}
BASE = {"ok": True, "active_typable": True, "active_has_value": True, "active_tag": "INPUT"}


def test_type_into_field_read_back_with_wrong_value_is_unverified():
    wrong = {**BASE, "active_value": "Ada Love", "active_value_length": 8}
    assert _vision_action_verified("type", wrong, ACTION) is False


def test_type_with_redacted_mismatch_is_unverified():
    redacted = {**BASE, "active_value_redacted": True, "active_value_matches_expected": False}
    assert _vision_action_verified("type", redacted, ACTION) is False


def test_type_with_matching_value_is_verified():
    right = {**BASE, "active_value": "Ada Lovelace", "active_value_matches_expected": True}
    assert _vision_action_verified("type", right, ACTION) is True


def test_type_without_any_read_back_keeps_the_loose_fallback():
    assert _vision_action_verified("type", dict(BASE), ACTION) is True


def test_rich_text_editor_confirmed_by_the_body_is_verified_without_typable_focus():
    rich = {
        "ok": True,
        "active_typable": False,
        "active_has_value": True,
        "active_value": "Dear team, Ada Lovelace",
        "active_value_matches_expected": True,
        "value_read_back": True,
        "typed_at_caret": True,
    }
    assert _vision_action_verified("type", rich, ACTION) is True


def test_rich_text_editor_that_cannot_be_read_back_is_not_a_mismatch():
    unread = {
        **BASE,
        "active_has_value": False,
        "active_value": "",
        "active_value_matches_expected": False,
        "value_read_back": False,
    }
    # Nothing was read, so nothing contradicts; typable alone without a value is still unproven.
    assert _vision_action_verified("type", unread, ACTION) is False
    assert _vision_action_verified("type", {**unread, "active_has_value": True}, ACTION) is True


def test_click_with_no_observed_effect_is_unverified():
    assert _vision_action_verified("click", {"ok": True, "surface_changed": False}) is False
