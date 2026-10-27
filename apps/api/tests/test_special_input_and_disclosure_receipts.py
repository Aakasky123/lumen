from app import extension_run
from app.receipt_view import _detail_facts


def test_a_normalized_date_read_back_is_a_match_and_a_mismatch_is_a_failure() -> None:
    action = {"text": "25/12/2012"}
    stored = {
        "active_value": "2012-12-25",
        "active_input_type": "date",
        "normalized_expected_value": "2012-12-25",
        "active_value_matches_expected": True,
    }
    assert extension_run._type_read_back_matches(action, stored) is True
    assert extension_run._type_read_back_failed(action, stored) is False
    # A date input that kept nothing is a read-back mismatch, never an unverified success.
    empty = {**stored, "active_value": "", "active_value_matches_expected": False}
    assert extension_run._type_read_back_matches(action, empty) is False
    assert extension_run._type_read_back_failed(action, empty) is True


def test_a_point_on_the_same_menu_item_is_not_arbitrated() -> None:
    wrapper = {"selector": "#menu > li:nth-child(1)", "tag": "li", "text": "Alice", "x": 20, "y": 10, "width": 50, "height": 28}
    item = {"selector": "#item1", "tag": "div", "role": "menuitem", "text": "Alice", "x": 20, "y": 10, "width": 50, "height": 28}
    resolved = extension_run._reconcile_action_control_references(
        {"action": "click", "text": ""}, indexed_control=item, coordinate_control=wrapper
    )
    assert resolved["ok"] is True
    assert resolved["control"] is item
    assert resolved["source"] == "index_contains_point"
    # A different item at the point is still a conflict for the arbiter.
    other = {**wrapper, "selector": "#menu > li:nth-child(2)", "text": "Bob"}
    assert extension_run._reconcile_action_control_references(
        {"action": "click", "text": ""}, indexed_control=item, coordinate_control=other
    )["ok"] is False
    # A smaller control inside a larger one is not one item either.
    inner = {**item, "selector": "#inner", "x": 25, "y": 12, "width": 10, "height": 10}
    assert extension_run._reconcile_action_control_references(
        {"action": "click", "text": ""}, indexed_control=item, coordinate_control=inner
    )["ok"] is False


def test_an_accordion_header_click_reports_its_expanded_state_not_a_choice() -> None:
    verification = {
        "target_disclosure": True,
        "expansion_changed": True,
        "target_expanded_before": "false",
        "target_expanded_after": "true",
        "target_state_changed": True,
    }
    facts = _detail_facts(verification, "Section one", {})
    assert "now expanded" in facts
    assert "choice committed" not in facts
    collapsed = _detail_facts({**verification, "target_expanded_after": "false", "target_expanded_before": "true"}, "x", {})
    assert "now collapsed" in collapsed
