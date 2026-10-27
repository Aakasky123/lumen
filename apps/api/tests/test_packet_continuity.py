"""The brain's packet continuity: its own reasons, what counts as a verified receipt, a refusal said once,
and notes that do not repeat a sentence already kept (run 1796 audit)."""

from app import extension_run, vision_agent
from app.receipt_view import brain_receipts, is_brain_visible_receipt


def test_a_recent_action_keeps_the_brains_reason_up_to_200_characters():
    reason = "r" * 250
    entry = extension_run._recent_action_entry(
        "scroll", {"action": "scroll", "reason": reason},
        ok=True, result_hint="", control_label=None, type_value=None,
    )
    assert entry.split(" :: ", 1)[1] == "r" * 200


def test_a_read_only_step_is_named_by_its_action_not_by_the_brains_reason():
    reason = "Race and veteran sections are done; scrolling to reach Submit"
    assert extension_run._receipt_target_label(
        "scroll", reason=reason, bound_label=None, sensitive_type=False,
    ) == "scroll"
    for kind in ("wait", "watch", "zoom", "read_clipboard", "read_download", "read_print_view", "read_form"):
        assert extension_run._receipt_target_label(
            kind, reason=reason, bound_label="Some control", sensitive_type=False,
        ) == kind


def test_value_and_control_receipts_keep_the_label_they_always_had():
    assert extension_run._receipt_target_label(
        "click", reason="Pick Yes", bound_label="Yes", sensitive_type=False,
    ) == "Pick Yes"
    assert extension_run._receipt_target_label(
        "type", reason="Type the phone", bound_label="Phone", sensitive_type=False,
    ) == "Type the phone"
    assert extension_run._receipt_target_label(
        "type", reason="secret", bound_label="Password", sensitive_type=True,
    ) == "sensitive type into [sensitive control]"
    assert extension_run._receipt_target_label(
        "upload", reason="Upload", bound_label="Resume", sensitive_type=False, upload_label="Resume field",
    ) == "Resume field"


def test_a_scroll_receipt_is_a_page_scrolled_receipt_and_not_brain_visible_even_when_verified():
    receipt = extension_run._action_receipt(
        step=1,
        intent={"action": "scroll", "label": "scroll", "selector": "550,300"},
        ok=True,
        verified=True,
    )
    assert receipt["type"] == "page_scrolled"
    assert receipt["outcome"] == "verified_succeeded"
    assert not is_brain_visible_receipt(receipt)
    assert brain_receipts([receipt]) == []


def test_a_form_outline_read_never_becomes_a_brain_visible_receipt():
    # A read_form receipt is named by its action, never the brain's reason, so a reason such as "Pay $499 to Acme"
    # cannot come back as a verified receipt (or as approval-quote evidence).
    assert extension_run._receipt_target_label(
        "read_form", reason="Pay $499 to Acme LLC", bound_label=None, sensitive_type=False,
    ) == "read_form"
    receipt = {"type": "form_outline_read", "outcome": "verified_succeeded", "target": {"label": "read_form"}}
    assert not is_brain_visible_receipt(receipt)
    assert brain_receipts([receipt]) == []


def test_a_real_verified_value_stays_visible_to_the_brain():
    typed = {
        "type": "browser_action", "outcome": "verified_succeeded", "target": {"label": "Phone"},
        "verification": {"active_value": "+1 555", "active_label": "Phone", "value_match": True},
    }
    assert is_brain_visible_receipt(typed)
    assert brain_receipts([typed]) == ["action 'Phone': field reads '+1 555'; read-back matched"]


def test_read_only_receipts_do_not_take_the_brains_window():
    scroll = {"type": "page_scrolled", "outcome": "verified_succeeded", "target": {"label": "scroll"}, "verification": {}}
    click = {
        "type": "browser_click", "outcome": "verified_succeeded", "target": {"label": "Yes"},
        "verification": {"target_text": "Yes"},
    }
    lines = brain_receipts([click] + [scroll] * 20, limit=12)
    assert lines == brain_receipts([click], limit=12)
    assert len(lines) == 1


def test_a_refused_primary_is_shown_once_in_last_reply_not_again_as_a_recent_action():
    refused = (
        "REFUSED, NOT EXECUTED (type at primary): sequence_invalid at primary: "
        "grounding_citation_page_value_about_user invalid_model_action:grounding_citation_page_value_about_user. "
        "A value-setting type action cannot execute."
    )
    last_reply = "REFUSED, NOT EXECUTED (type at primary): sequence_invalid at primary: " \
        "grounding_citation_page_value_about_user invalid_model_action:grounding_citation_page_value_about_user. " \
        "A value-setting type action cannot exec"
    recent = ["run_journal_restored: older note.", "scroll@550,300 ok :: reason", refused]
    shown = extension_run._packet_recent_actions(recent, reply_start=2, last_reply_text=last_reply)
    assert shown == recent[:2]


def test_a_refusal_last_reply_does_not_state_stays_in_recent_actions():
    refused = "REFUSED, NOT EXECUTED (click at primary): sequence_invalid at primary: other_reason."
    shown = extension_run._packet_recent_actions(
        [refused], reply_start=0, last_reply_text="primary click 'Yes' verified",
    )
    assert shown == [refused]


def test_an_older_refusal_before_the_reply_stays_as_history():
    refused = "REFUSED, NOT EXECUTED (type at primary): sequence_invalid at primary: grounding_citation_x."
    last_reply = "REFUSED, NOT EXECUTED (type at primary): sequence_invalid at primary: grounding_citation_x."
    recent = [refused, "scroll ok :: next"]
    assert extension_run._packet_recent_actions(recent, reply_start=1, last_reply_text=last_reply) == recent


def test_recent_actions_show_the_last_ten_lines_only():
    recent = [f"line {n}" for n in range(14)]
    assert extension_run._packet_recent_actions(recent, reply_start=None, last_reply_text="") == recent[-10:]


def test_a_repeated_note_sentence_is_written_once():
    kept = ["Pronouns field filled with n/a per page instruction."]
    assert vision_agent.new_note_sentences("Pronouns field filled with n/a per page instruction.", kept) == ""
    assert vision_agent.new_note_sentences(
        "  pronouns FIELD filled with N/A per page   instruction. ", kept,
    ) == ""


def test_a_sentence_repeated_inside_one_decision_is_written_once_and_the_rest_keeps_its_separator():
    text = "Pronouns field filled with n/a per page instruction.; Pronouns field filled with n/a per page instruction.; Hybrid answered Yes."
    assert vision_agent.new_note_sentences(text, []) == (
        "Pronouns field filled with n/a per page instruction.; Hybrid answered Yes."
    )


def test_a_new_sentence_is_kept_when_only_a_similar_one_is_kept():
    kept = ["Pronouns field filled with n/a."]
    assert vision_agent.new_note_sentences("Pronouns field filled with n/a per page instruction.", kept) == (
        "Pronouns field filled with n/a per page instruction."
    )


def test_a_note_whose_every_sentence_is_already_kept_adds_nothing():
    kept = ["Hybrid Yes verified.", "Work authorization No verified."]
    assert vision_agent.new_note_sentences("Hybrid Yes verified.; Work authorization No verified.", kept) == ""
