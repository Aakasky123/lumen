"""Round-15 gaps: the reply's consequential-action line."""

from __future__ import annotations

from app import extension_run
from app.operation_receipt import make_receipt


def _receipt(label: str, *, final: bool, verified: bool) -> dict:
    return make_receipt(
        operation_id=f"click:{label}",
        type="browser_click",
        target={"label": label},
        attempt={"status": "attempted"},
        execution={"status": "succeeded"},
        verification={"status": "succeeded" if verified else "inconclusive", **({"final_action": True} if final else {})},
    )


def test_consequential_sentence_makes_no_negative_claim_without_a_final_action_receipt() -> None:
    plain = [_receipt("Next", final=False, verified=True)]
    assert extension_run._consequential_actions_sentence(plain) == ""
    assert extension_run._consequential_actions_sentence([]) == ""


def test_consequential_sentence_names_verified_and_unverified_final_actions() -> None:
    receipts = [
        _receipt("Save", final=True, verified=True),
        _receipt("Delete row", final=True, verified=True),
        _receipt("Send", final=True, verified=False),
    ]
    text = extension_run._consequential_actions_sentence(receipts)
    assert "No consequential" not in text
    assert "2 consequential action(s) ran" in text and "Save" in text and "Delete row" in text
    assert "1 consequential action(s) were attempted without a verified outcome" in text

