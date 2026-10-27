from __future__ import annotations

import pytest

from app import extension_run

VALUE_CONTROLS = [
    {"tag": "div", "role": "option", "text": "Refund rate"},
    {"tag": "input", "type": "radio", "text": "Monthly"},
    {"tag": "input", "type": "checkbox", "text": "Ana"},
    {"tag": "button", "role": "switch", "text": "Notifications"},
    {"tag": "li", "role": "menuitemradio", "text": "Sort by date"},
    {"tag": "li", "role": "menuitemcheckbox", "text": "Show grid"},
    {"tag": "button", "role": "", "text": "Mute", "aria_pressed": "false"},
    {"tag": "div", "role": "", "text": "Option B", "aria_checked": "true"},
    {"tag": "div", "role": "gridcell", "text": "15", "aria_selected": "false"},
]

ACTIVATIONS = [
    {"tag": "button", "type": "button", "role": "", "text": "Accept"},
    {"tag": "a", "role": "link", "text": "Next week"},
    {"tag": "button", "role": "tab", "text": "Invitations (2)", "aria_selected": "true"},
    {"tag": "button", "kind": "button/tab", "text": "Invitations (2)", "aria_selected": "true"},
    {"tag": "select", "text": "Country"},
]


@pytest.mark.parametrize("control", VALUE_CONTROLS)
def test_value_control_click_without_text_requires_grounding(control) -> None:
    assert extension_run._control_is_structural_choice(control)
    result = extension_run._vision_grounding_citation_check({"action": "click", "text": ""}, {}, control)
    assert result["ok"] is False
    assert result["derived_text"] is True
    assert result["derived_choice_text"] == control["text"]


@pytest.mark.parametrize("control", ACTIVATIONS)
def test_activation_click_without_text_takes_the_consequence_path(control) -> None:
    assert not extension_run._control_is_structural_choice(control)
    assert extension_run._vision_grounding_citation_check({"action": "click", "text": ""}, {}, control) == {"ok": True}
