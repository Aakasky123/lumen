"""A constrained CHOICE control must not be subject to the free-text anti-fabrication gate. The gate
flips an ungrounded short free-text value (a salary, a bare "Yes") to a confirm-ask — correct for typed
text, WRONG for a dropdown, where the body validates the chosen value against the real options on open.
This is why the LLM-integrations dropdown's grounded "Yes" became a confirm-ask instead of auto-filling.
"""

from app.lumen_model import LumenModelClient
from app.db import SessionLocal, init_db


def _confident_yes(self, **kwargs):
    return {"ok": True, "data": {"answer": "Yes", "confidence": 0.9, "rationale": "résumé shows production LLM work", "needs_followup": False, "source": "resume"}, "metadata": {}}


def test_constrained_choice_grounded_answer_is_not_flipped(monkeypatch) -> None:
    init_db()
    monkeypatch.setattr(LumenModelClient, "live_enabled", property(lambda self: True))
    monkeypatch.setattr(LumenModelClient, "_call_model_json", _confident_yes)
    with SessionLocal() as db:
        result = LumenModelClient(db).answer_application_field(
            {"label": "Have you built integrations with LLMs in production?", "allowed_options": [], "is_constrained_choice": True, "control_kind": "combobox", "profile": {}, "resume_facts": {}}
        )
    # The body validates "Yes" against the real options -> it auto-fills, it is NOT flipped to unsure.
    assert result["answer"] == "Yes"
    assert not result.get("needs_followup")


def test_free_text_ungrounded_answer_is_still_flipped(monkeypatch) -> None:
    # The mirror: the same bare "Yes" as FREE TEXT (not a choice) is still flipped to a confirm-ask, so
    # the gate keeps protecting typed fields from fabrication.
    init_db()
    monkeypatch.setattr(LumenModelClient, "live_enabled", property(lambda self: True))
    monkeypatch.setattr(LumenModelClient, "_call_model_json", _confident_yes)
    with SessionLocal() as db:
        result = LumenModelClient(db).answer_application_field(
            {"label": "Anything else you'd like us to know?", "allowed_options": [], "is_constrained_choice": False, "profile": {}, "resume_facts": {}}
        )
    assert result.get("needs_followup") is True
    assert result.get("proposed_answer") == "Yes"
    assert result.get("answer") == ""
