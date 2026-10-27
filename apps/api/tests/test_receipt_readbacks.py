from __future__ import annotations

import pytest

from app import extension_run


@pytest.mark.parametrize(
    ("intended", "observed", "context", "verdict"),
    [
        ("25", "2027-10-25", "October 2027 Su Mo Tu", "match"),
        ("15", "October 15, 2027", "Oct 2027", "match"),
        ("5", "5 Oct 2027", "October 2027", "match"),
        ("25", "2027-11-25", "October 2027", "mismatch"),
        ("25", "2028-10-25", "October 2027", "mismatch"),
        ("25", "October 25", "November 2027", "mismatch"),
        ("25", "2027-11-25", "pick Oct 25, 2027", "mismatch"),
        ("25", "2027-10-25", "", "day_only"),
        ("25", "2027-10-25", "October 2027 and November 2027", "day_only"),
        ("25", "2027-10-15", "October 2027", "mismatch"),
        ("10", "2027-10-25", "October 2027", "mismatch"),
        ("2", "Terminal 2", "October 2027", "mismatch"),
        ("25", "10/25/2027", "October 2027", "mismatch"),
    ],
)
def test_day_number_pick_matches_the_fields_resulting_date(intended, observed, context, verdict) -> None:
    assert extension_run._visual_readback_matches(intended, observed) is False
    assert extension_run._day_pick_readback(intended, observed, context) == verdict


def _day_verdict(observed: str, header: str) -> dict:
    return extension_run._apply_reversible_click_visual_verdict(
        {"ok": True, "activation_attempted": True, "activation_count": 1, "visual_verification_required": True},
        action={"action": "click", "text": "25"},
        verdict={"status": "succeeded", "confidence": 0.95, "evidence": "field shows date", "observed_value": observed},
        context_shot={"title": "Booking", "page_evidence": {"text": header}},
    )


def test_day_pick_in_the_wrong_month_is_not_verified() -> None:
    wrong = _day_verdict("2027-11-25", "October 2027")
    assert wrong["visual_verification_status"] == "failed" and wrong["visual_readback_mismatch"] is True
    assert not wrong.get("selection_committed")


def test_day_pick_in_the_right_month_is_verified() -> None:
    right = _day_verdict("2027-10-25", "October 2027")
    assert right["visual_verification_status"] == "succeeded" and right["selection_committed"] is True


def test_day_pick_without_month_info_is_unverified_with_a_hint() -> None:
    unknown = _day_verdict("2027-10-25", "no calendar here")
    assert unknown["visual_verification_status"] == "uncertain"
    assert "day matches, month unverified" in unknown["visual_verification_evidence"]
    assert not unknown.get("selection_committed") and unknown["visual_verification_required"] is True


def test_a_control_the_agent_just_toggled_is_not_reported_lost() -> None:
    box = {"tag": "input", "type": "checkbox", "context_text": "Northstar", "selector": "#ns", "checked": False}
    signature = extension_run._control_signature(box)
    ledger = {signature: {"label": "Northstar", "verified_value": "Northstar", "verified": True, "last_step": 5}}
    satisfied = {signature: {"control": "Northstar", "value": "Northstar"}}
    assert extension_run._reopen_read_backs_the_page_lost({"controls": [box]}, ledger, satisfied, acted_since_step=4) == []
    assert ledger[signature]["verified_value"] is None and satisfied == {}

    ledger = {signature: {"label": "Northstar", "verified_value": "Northstar", "verified": True, "last_step": 3}}
    assert extension_run._reopen_read_backs_the_page_lost({"controls": [box]}, ledger, {}, acted_since_step=4) == ["Northstar"]


def test_a_no_effect_click_on_a_disabled_target_says_so() -> None:
    result = {"ok": True, "action": "click", "target_text": "12A", "target_disabled": True, "target_description": "Seat taken"}
    note = extension_run._vision_unverified_action_feedback("click", {"action": "click"}, result, False)
    assert note.startswith("click_unverified")
    assert "disabled" in note and "'Seat taken'" in note
    plain = extension_run._vision_unverified_action_feedback("click", {"action": "click"}, {"ok": True, "action": "click"}, False)
    assert "disabled" not in plain


def test_commit_key_receipt_reads_back_where_the_edit_went() -> None:
    base = {"ok": True, "action": "key", "key": "Enter"}
    landed = extension_run._vision_result_hint(
        {**base, "edit_commit": {"editor_hint": "Cell B2", "edit_value": "42", "editor_still_focused": False, "edit_value_visible_after": True}}
    )
    assert "(committed)" in landed and "'42'" in landed
    stuck = extension_run._vision_result_hint(
        {**base, "edit_commit": {"edit_value": "42", "editor_still_focused": True, "editor_value_after": "42"}}
    )
    assert "nothing was committed" in stuck
    lost = extension_run._vision_result_hint(
        {**base, "edit_commit": {"edit_value": "42", "editor_still_focused": False, "edit_value_visible_after": False}}
    )
    assert "not visible" in lost
    extra = extension_run._vision_verification_extra("key", {**base, "edit_commit": {"edit_value": "42"}})
    assert extra["edit_commit"]["edit_value"] == "42"
