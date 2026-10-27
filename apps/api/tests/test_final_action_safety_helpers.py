from __future__ import annotations

import base64
import hashlib

from app import extension_run


def test_activation_capable_keys_are_withheld() -> None:
    for key in ("Enter", "Return", "NumpadEnter", "Space", " "):
        assert extension_run._activation_capable_key(key) is True
    assert extension_run._activation_capable_key("Escape") is False


def test_sensitive_structure_and_type_result_never_preserve_value() -> None:
    control = {
        "tag": "div",
        "autocomplete": "section-checkout billing cc-number",
        "text": "Card number",
    }
    assert extension_run._control_has_sensitive_value_structure(control) is True

    secret = "4111111111111111"
    redacted = extension_run._redact_sensitive_type_result(
        {
            "ok": True,
            "active_value": secret,
            "active_value_ledger": secret,
            "before_value": "old-secret",
        },
        expected_text=secret,
    )
    serialized = repr(redacted)
    assert secret not in serialized
    assert "old-secret" not in serialized
    assert redacted["active_value_redacted"] is True
    assert redacted["active_value_length"] == 0
    assert redacted["active_has_value"] is False
    assert redacted["active_value_matches_expected"] is False


def test_sensitive_type_result_keeps_only_vocabulary_refusal_reasons() -> None:
    secret = "hunter2-secret"
    kept = extension_run._redact_sensitive_type_result(
        {"ok": False, "failure_reason": "control_target_drift", "target_identity_mismatch_field": "target_text"},
        expected_text=secret,
        redaction_reason="credential_replay",
    )
    assert kept["failure_reason"] == "control_target_drift"
    assert kept["target_identity_mismatch_field"] == "target_text"
    # A page-shaped string never passes, even in a reason field.
    dropped = extension_run._redact_sensitive_type_result(
        {"ok": False, "failure_reason": secret, "page_context_mismatch_field": f"url:{secret}"},
        expected_text=secret,
        redaction_reason="credential_replay",
    )
    assert "failure_reason" not in dropped and "page_context_mismatch_field" not in dropped
    assert secret not in repr(dropped)


def test_page_context_binds_screenshot_bytes_and_rejects_pixel_drift() -> None:
    image = base64.b64encode(b"masked screenshot bytes").decode("ascii")
    shot = {
        "url": "https://example.test/checkout",
        "surface_signature": "surface",
        "screenshot_mode": "capture_visible_tab",
        "screenshot_quality": 60,
        "image": image,
    }
    context = extension_run._final_page_context(shot)
    assert context["screenshot_sha256"] == hashlib.sha256(b"masked screenshot bytes").hexdigest()
    assert extension_run._final_page_context_matches(context, shot) is True

    changed = {**shot, "image": base64.b64encode(b"changed pixels").decode("ascii")}
    assert extension_run._final_page_context_matches(context, changed) is False


def test_approval_context_requires_exact_current_evidence_quotes() -> None:
    target = {
        "approval_context_summary": "Two seats are selected for the 7:30 PM showing.",
        "approval_context_evidence": ["Seats C8, C9", "7:30 PM"],
    }
    validated = extension_run._validated_final_approval_context(
        target,
        page_evidence={"viewport_text": "7:30 PM — Seats C8, C9 — Total $24.00"},
        receipts=[],
    )
    assert validated["valid"] is True
    assert validated["source"] == "model_grounded_current_evidence"

    invented = extension_run._validated_final_approval_context(
        {
            "approval_context_summary": "A premium seat is selected.",
            "approval_context_evidence": ["Premium seat"],
        },
        page_evidence={"viewport_text": "Standard seat C8"},
        receipts=[
            {
                "outcome": "verified_succeeded",
                "target": {"label": "Premium seat"},
                "execution": {"status": "succeeded"},
                "verification": {"status": "succeeded"},
            }
        ],
    )
    assert invented["valid"] is False
    assert invented["summary"] == ""


def test_refused_approval_context_names_the_exact_gap() -> None:
    page_evidence = {"viewport_text": "Order 1042 - 2 items - Total $24.00"}
    empty = extension_run._validated_final_approval_context({}, page_evidence=page_evidence, receipts=[])
    assert (empty["valid"], empty["summary_missing"], empty["evidence_missing"], empty["unmatched_evidence"]) == (
        False,
        True,
        True,
        [],
    )

    # One paraphrased quote among exact ones still refuses the whole context; the gap names only that quote.
    paraphrased = extension_run._validated_final_approval_context(
        {
            "approval_context_summary": "Order 1042 with two items will be placed.",
            "approval_context_evidence": ["Order 1042", "two items"],
        },
        page_evidence=page_evidence,
        receipts=[],
    )
    assert paraphrased["valid"] is False
    assert paraphrased["evidence"] == []
    assert (paraphrased["summary_missing"], paraphrased["evidence_missing"]) == (False, False)
    assert paraphrased["unmatched_evidence"] == ["two items"]

    feedback = extension_run._final_approval_context_feedback(
        {
            "label": "Place order",
            "control_index": 3,
            "risk_classification": "consequential_irreversible",
            "risk_evidence": "The button places the order.",
        },
        paraphrased,
        refusal=2,
        limit=3,
    )
    assert feedback.startswith("final_approval_context_ungrounded (refusal 2 of 3)")
    assert '"Place order" (control 3)' in feedback
    assert "consequential_irreversible" in feedback
    assert '["two items"]' in feedback
    assert "approval_context_summary" in feedback and "approval_context_evidence" in feedback
    assert "Nothing was clicked." in feedback

    blank = extension_run._final_approval_context_feedback({"label": "Submit"}, empty, refusal=1, limit=3)
    assert "approval_context_summary was empty; approval_context_evidence was empty" in blank
    assert "final submit control" in blank


def test_menu_option_click_without_text_still_gets_visible_effect_readback():
    from app import extension_run as er

    assert er._needs_reversible_click_visual_readback(
        "click", {"action": "click", "control": "Reviewer"}, {"ok": True, "target_option_like": True, "activation_attempted": True}
    )
    # a plain button click with no text names no value to read back
    assert not er._needs_reversible_click_visual_readback(
        "click", {"action": "click"}, {"ok": True, "activation_attempted": True}
    )


def test_recent_action_label_shows_glyph_beside_a_different_accessible_name():
    from app import extension_run as er

    assert er._recent_action_control_label("More", {"text": "+"}) == "More (+)"
    assert er._recent_action_control_label("Save", {"text": "Save"}) == "Save"
    assert er._recent_action_control_label("Save changes", {"text": "Save"}) == "Save changes"
    assert er._recent_action_control_label(None, {"text": "+"}) is None


def test_outcome_normalizer_keeps_awaiting_confirmation_flag():
    from app.lumen_model import LumenModelClient

    normalized = LumenModelClient()._normalize_final_action_outcome_data(
        {"status": "uncertain", "confidence": 0.2, "evidence": "A confirm dialog opened", "awaiting_confirmation": True}
    )
    assert normalized["awaiting_confirmation"] is True
    assert LumenModelClient()._normalize_final_action_outcome_data(
        {"status": "succeeded", "confidence": 0.9, "evidence": "done"}
    )["awaiting_confirmation"] is False


def test_unexecuted_click_gets_only_the_stale_message_not_click_unverified():
    from app import extension_run as er

    result = {"ok": False, "activation_attempted": False, "failure_reason": "coordinate_pixel_drift"}
    if er._vision_action_explicitly_not_executed("click", result):
        assert er._vision_action_progress("click", result, ledger_gained_value=False, verified=False) == (
            False,
            "pre_action_revalidation_failed",
        )


def test_approval_quote_may_match_the_rendered_receipt_line() -> None:
    receipts = [{
        "type": "browser_click",
        "outcome": "verified_succeeded",
        "verification": {"selected_value": "Seat C8"},
        "target": {"label": "Seat map"},
    }]
    target = {
        "approval_context_summary": "Seat C8 is selected.",
        "approval_context_evidence": ["selected 'Seat C8'"],
    }
    assert extension_run._validated_final_approval_context(target, page_evidence={}, receipts=receipts)["valid"] is True
    invented = {**target, "approval_context_evidence": ["selected 'Seat Z9'"]}
    assert extension_run._validated_final_approval_context(invented, page_evidence={}, receipts=receipts)["valid"] is False
