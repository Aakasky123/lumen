"""Grounded-response enforcement: a reply may assert a run-lifecycle change only if a matching
verified_succeeded receipt exists. Deterministic SAFETY (output truthfulness), not decision policy."""

from app.claim_validator import claimed_lifecycle_type, enforce_grounded_reply

_CANCEL_RECEIPT = [{"type": "cancel_run", "outcome": "verified_succeeded"}]


def test_unsupported_stop_claim_is_stripped() -> None:
    text = "I stopped run #293 for you. Anything else I can help with?"
    grounded, stripped = enforce_grounded_reply(text, receipts=[])
    assert len(stripped) == 1
    assert "stopped run" not in grounded.lower()
    assert "anything else" in grounded.lower()  # the benign sentence is kept


def test_supported_stop_claim_is_kept() -> None:
    text = "I stopped run #293 for you."
    grounded, stripped = enforce_grounded_reply(text, receipts=_CANCEL_RECEIPT)
    assert stripped == []
    assert grounded == text


def test_benign_sentences_are_not_stripped() -> None:
    # lifecycle verbs without a run/application/session referent must not be touched
    for text in ["Sure — let's get you started!", "I can help you apply to jobs.", "Send me a link and I'll begin."]:
        grounded, stripped = enforce_grounded_reply(text, receipts=[])
        assert stripped == []
        assert grounded == text


def test_all_claim_falls_back_to_grounded_message() -> None:
    grounded, stripped = enforce_grounded_reply("I cancelled your run and started a new application session.", receipts=[])
    assert stripped  # at least one unsupported claim removed
    assert "haven't" in grounded.lower() or "didn't" in grounded.lower()


def test_claimed_lifecycle_type_mapping() -> None:
    assert claimed_lifecycle_type("I stopped run #12.") == "cancel_run"
    assert claimed_lifecycle_type("Started a new application.") == "start_run"
    assert claimed_lifecycle_type("Paused the run.") == "pause_run"
    assert claimed_lifecycle_type("Submitted your application.") == "submit"
    assert claimed_lifecycle_type("How does applying work?") is None
