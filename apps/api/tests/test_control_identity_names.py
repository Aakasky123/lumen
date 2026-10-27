"""The names a control is expected to answer to keep every distinct raw form, since the in-page identity checks
compare them case-sensitively."""

from __future__ import annotations

from app.extension_run import _observed_control_identity_names, _observed_control_target_identity

# Workable's email input as run 49 observed it: label "* Email", name attribute "email", aria-labelledby "Email".
_WORKABLE_EMAIL = {
    "tag": "input",
    "type": "email",
    "selector": "#email",
    "text": "* Email",
    "name": "email",
    "accessibility_context": [
        {"relation": "label", "text": "* Email"},
        {"relation": "aria-labelledby", "text": "Email"},
    ],
}


def test_a_label_differing_from_the_name_attribute_only_in_case_is_kept() -> None:
    names = _observed_control_identity_names(_WORKABLE_EMAIL)
    # The in-page type check reads the live name from aria-labelledby: "Email".
    assert names == ["* Email", "email", "Email"]
    assert "Email" in _observed_control_target_identity(_WORKABLE_EMAIL, selector="#email")["target_names"]


def test_exact_repeats_still_merge() -> None:
    control = {"text": "First  name", "name": "First name", "accessibility_context": [{"relation": "label", "text": "First name"}]}
    assert _observed_control_identity_names(control) == ["First name"]
