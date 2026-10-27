"""Freeing the body to start a new application is BRAIN-understood, not keyword-gated. The user can
talk normally ("start a new one here", "scrap it and apply here", "switch to this") and Lumen ends
the current run and starts the new one — no magic "end it" phrase. CI checks the plumbing (offline =>
honest false, never a keyword guess; model decision honored). The live model's interpretation is
validated separately."""

from app.cognition import OperatorChatService
from app.db import SessionLocal, init_db


class _FakeModel:
    def __init__(self, live: bool, replace: bool | None = None) -> None:
        self.live_enabled = live
        self._replace = replace

    def _call_model_json(self, *, purpose, system, prompt, schema):
        return {"ok": True, "data": {"replace": self._replace, "reason": "x"}} if self._replace is not None else {"ok": False}


def _svc(model: _FakeModel) -> OperatorChatService:
    init_db()
    svc = OperatorChatService(SessionLocal())
    svc.lumen_model = model  # type: ignore[assignment]
    return svc


def test_offline_returns_false_no_keyword_guess() -> None:
    # no live model -> ask a natural question, never infer a switch from keywords
    assert _svc(_FakeModel(live=False))._user_wants_to_replace_active_run("start a new one here", "ats.rippling.com") is False


def test_model_decision_is_honored() -> None:
    assert _svc(_FakeModel(live=True, replace=True))._user_wants_to_replace_active_run("start a new one here", "x") is True
    assert _svc(_FakeModel(live=True, replace=False))._user_wants_to_replace_active_run("what's it doing", "x") is False


def test_model_unavailable_this_call_returns_false() -> None:
    assert _svc(_FakeModel(live=True, replace=None))._user_wants_to_replace_active_run("switch to this", "x") is False
