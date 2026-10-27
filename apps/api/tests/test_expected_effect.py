import asyncio

from app import extension_run as er
from app import vision_agent


def _shot(text):
    return {"title": "", "page_evidence": {"text": text}, "image": "x"}


def test_expect_parses_on_action_and_then_and_absent_is_unchanged():
    action = vision_agent.normalize_action(
        {"action": "click", "x": 5, "y": 6, "expect": "  Billing tab\n is selected ", "then": [{"action": "key", "key": "Tab", "expect": "focus moves"}]}
    )
    assert action["expect"] == "Billing tab is selected"
    assert action["then"][0]["expect"] == "focus moves"
    plain = vision_agent.normalize_action({"action": "click", "x": 5, "y": 6})
    assert "expect" not in plain


def test_pending_only_for_ambiguous_progress_and_present_expect():
    act = {"expect": "Billing tab is selected"}
    assert er._expect_pending_for(act, "click", "verified_without_durable_progress", _shot("a"))
    assert er._expect_pending_for(act, "click", "no_verified_progress", _shot("a"))
    assert er._expect_pending_for(act, "click", "page_transition", _shot("a")) is None
    assert er._expect_pending_for(act, "click", "verified_control_value", _shot("a")) is None
    assert er._expect_pending_for({}, "click", "no_verified_progress", _shot("a")) is None
    assert er._expect_pending_for(act, "type", "no_verified_progress", _shot("a")) is None


def test_expect_met_by_new_text_without_model_call(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("judge must not run")

    monkeypatch.setattr(er, "_expected_effect_verdict", boom)
    pending = er._expect_pending_for(
        {"expect": 'the "Billing details" panel shows'}, "click", "focus_only", _shot("Account")
    )
    status, line = asyncio.run(er._check_pending_expect(1, "g", pending, _shot("Account Billing details")))
    assert status == "met" and "observed: met" in line


def test_text_already_present_is_not_evidence(monkeypatch):
    monkeypatch.setattr(er, "_expected_effect_verdict", lambda *a, **k: {"status": "uncertain", "metadata": {}})
    pending = er._expect_pending_for({"expect": '"Billing details"'}, "click", "focus_only", _shot("Billing details"))
    assert asyncio.run(er._check_pending_expect(1, "g", pending, _shot("Billing details"))) == ("", "")


def test_not_met_says_wrong_target(monkeypatch):
    monkeypatch.setattr(
        er,
        "_expected_effect_verdict",
        lambda *a, **k: {"status": "failed", "confidence": 0.9, "evidence": "Account tab still selected", "metadata": {"m": 1}},
    )
    pending = er._expect_pending_for({"expect": "Billing tab is selected"}, "click", "no_verified_progress", _shot("x"))
    status, line = asyncio.run(er._check_pending_expect(1, "g", pending, _shot("x")))
    assert status == "not_met"
    assert "not met" in line and "wrong target" in line


def test_unavailable_judge_fails_open(monkeypatch):
    monkeypatch.setattr(
        er, "_expected_effect_verdict", lambda *a, **k: {"status": "uncertain", "confidence": 0.0, "evidence": "none", "metadata": {}}
    )
    pending = er._expect_pending_for({"expect": "Billing tab is selected"}, "click", "no_verified_progress", _shot("x"))
    assert asyncio.run(er._check_pending_expect(1, "g", pending, _shot("x"))) == ("", "")


def test_progress_classification_unchanged_by_expect():
    assert er._vision_action_progress("click", {"ok": True}, ledger_gained_value=False, verified=True) == (
        False,
        "verified_without_durable_progress",
    )
