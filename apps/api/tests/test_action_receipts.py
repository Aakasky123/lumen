"""Receipts for browser actions are logs-as-evidence."""

from app.extension_run import _action_receipt, _field_receipt_counts, _vision_verification_extra


def test_action_receipt_outcomes() -> None:
    r = _action_receipt(step=1, intent={"action": "fill", "selector": "#a", "label": "First name"}, ok=True, observed="Jordan", value_present=True, verified=True)
    assert r["type"] == "field_filled"
    assert r["outcome"] == "verified_succeeded"

    r = _action_receipt(step=2, intent={"action": "fill", "selector": "#b", "label": "Last name"}, ok=True, observed="", value_present=False, verified=False)
    assert r["outcome"] == "verification_inconclusive"

    r = _action_receipt(step=3, intent={"action": "fill", "selector": "#c", "label": "City"}, ok=True, observed="Newark", value_present=True, verified=False)
    assert r["outcome"] == "verification_failed"

    r = _action_receipt(step=4, intent={"action": "select", "selector": "#d", "label": "Salary"}, ok=False)
    assert r["outcome"] == "execution_failed"

    r = _action_receipt(
        step=6,
        intent={"action": "select", "selector": "#auth", "label": "Work authorization"},
        ok=True,
        observed="Yes",
        value_present=True,
        verified=False,
        verification_extra={"observed_value_matches": True, "owned_menu_visible_after": True, "owned_menu_closed": False, "failure_reason": "OWNED_MENU_STILL_OPEN"},
    )
    assert r["outcome"] == "verification_failed"
    assert r["verification"]["failure_reason"] == "OWNED_MENU_STILL_OPEN"
    assert r["verification"]["owned_menu_visible_after"] is True

    assert _action_receipt(step=0, intent={"action": "navigate", "label": "u", "selector": "u"}, ok=True)["outcome"] == "verified_succeeded"
    assert _action_receipt(step=5, intent={"action": "click", "selector": "#go", "label": "Apply"}, ok=True)["outcome"] == "verification_inconclusive"

    r = _action_receipt(
        step=6,
        intent={"action": "click", "selector": "#go", "label": "Apply"},
        ok=True,
        verified=True,
        verification_extra={"url_changed": True},
    )
    assert r["outcome"] == "verified_succeeded"
    assert r["verification"]["url_changed"] is True

    r = _action_receipt(
        step=7,
        intent={"action": "click", "selector": "968,183", "label": "Apply now"},
        ok=False,
        verification_extra=_vision_verification_extra("click", {"ok": False, "error": "ReferenceError: visionPointInfoInPage is not defined", "mode": "dom_activation"}),
    )
    assert r["outcome"] == "execution_failed"
    assert r["verification"]["body_error"] == "ReferenceError: visionPointInfoInPage is not defined"
    assert r["verification"]["mode"] == "dom_activation"


def test_field_receipt_counts() -> None:
    receipts = [
        _action_receipt(step=0, intent={"action": "navigate", "label": "u", "selector": "u"}, ok=True),
        _action_receipt(step=1, intent={"action": "fill", "selector": "#a", "label": "A"}, ok=True, observed="X", value_present=True, verified=True),
        _action_receipt(step=2, intent={"action": "fill", "selector": "#b", "label": "B"}, ok=True, observed="", value_present=False, verified=False),
        _action_receipt(step=3, intent={"action": "select", "selector": "#c", "label": "C"}, ok=False),
    ]
    assert _field_receipt_counts(receipts) == {"verified": 1, "unconfirmed": 1, "failed": 1, "total": 3}
