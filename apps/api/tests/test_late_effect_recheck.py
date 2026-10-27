"""A reversible action that ends with no verified progress is re-observed once before the model hears
"no effect": a late reaction is reported as such (and never repeated), a still screen as re-checked."""

from __future__ import annotations

import asyncio

from app import extension_run
from app.db import init_db
from app.extension_run import _last_reply_summary, run_vision_loop

from tests.test_extension_run import (  # noqa: F401 - the autouse fixtures apply to this module too
    _DummyContextEngine,
    _complete_final_action_readiness_audit_by_default,
    _ordinary_click_consequence_by_default,
    _ordinary_secret_semantics_by_default,
)


class _SlowPageBody:
    """A click that shows nothing at first; the page reacts only from the `changes_at_capture`-th capture on."""

    def __init__(self, *, changes_at_capture: int | None) -> None:
        self.changes_at_capture = changes_at_capture
        self.captures = 0
        self.clicks: list[tuple[int, int]] = []

    async def capture_screenshot(self, **_kwargs: object) -> dict:
        self.captures += 1
        changed = self.changes_at_capture is not None and self.captures >= self.changes_at_capture
        return {
            "ok": True, "image": "jpeg", "width": 800, "height": 600,
            "url": "https://example.test/page", "title": "Page", "scroll_y": 0, "scroll_height": 600,
            "page_evidence": {
                "viewport_text": "Saved" if changed else "Save",
                "controls": [{"index": 1, "tag": "button", "text": "Save", "selector": "#save"}],
            },
        }

    async def vision_click(self, x: int, y: int, **_binding: object) -> dict:
        self.clicks.append((x, y))
        return {"ok": True, "action": "click"}


def _run(monkeypatch, body: _SlowPageBody, decisions: list[dict]):
    init_db()
    queue = iter(decisions)
    contexts: list[dict] = []
    events: list[tuple[str, dict]] = []

    def decide(user_id, image, view, context):
        contexts.append(dict(context))
        return next(queue)

    async def narrate(kind: str, data: dict) -> None:
        pass

    monkeypatch.setattr("app.candidate_context.CandidateContextEngine", _DummyContextEngine)
    monkeypatch.setattr(extension_run, "_db_event", lambda run_id, event_type, payload: events.append((event_type, payload)))
    monkeypatch.setattr(extension_run, "_record_context_usage_for_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(extension_run, "_decide_vision_action", decide)
    asyncio.run(run_vision_loop(body, url=None, narrate=narrate, run_id=0, user_id=0, skip_navigate=True, max_steps=4))
    return contexts, events


def _click() -> dict:
    return {"action": "click", "x": 100, "y": 200, "reason": "Press Save."}


def test_a_late_change_is_a_late_effect_and_the_click_is_not_dispatched_again(monkeypatch) -> None:
    # capture 1 = the step's observation, capture 2 = the re-check (changed), capture 3 = the next step's
    body = _SlowPageBody(changes_at_capture=2)
    contexts, events = _run(monkeypatch, body, [_click(), {"action": "done", "reason": "Saved."}])
    assert body.clicks == [(100, 200)]
    reply = contexts[1]["last_reply"]
    assert "late effect" in reply and "no effect" not in reply
    assert any("late_effect" in str(line) for line in contexts[1]["recent_actions"])
    assert [p["verdict"] for t, p in events if t == "VISION_LATE_EFFECT_RECHECK"] == ["late_effect"]


def test_a_still_screen_is_no_effect_rechecked_once(monkeypatch) -> None:
    body = _SlowPageBody(changes_at_capture=None)
    contexts, events = _run(monkeypatch, body, [_click(), {"action": "done", "reason": "Gave up."}])
    assert "no effect (re-checked after" in contexts[1]["last_reply"]
    # one observation, one re-check, one fresh observation for the next decision: never more than one re-check
    assert body.captures == 3
    assert [p["verdict"] for t, p in events if t == "VISION_LATE_EFFECT_RECHECK"] == ["no_effect"]


def test_a_chain_is_not_rechecked(monkeypatch) -> None:
    body = _SlowPageBody(changes_at_capture=None)
    chained = {**_click(), "then": [{"action": "click", "x": 120, "y": 220, "reason": "Press Save again."}]}
    _, events = _run(monkeypatch, body, [chained, {"action": "done", "reason": "x"}])
    assert not [1 for t, _ in events if t == "VISION_LATE_EFFECT_RECHECK"]


def test_a_verified_action_is_not_rechecked(monkeypatch) -> None:
    body = _SlowPageBody(changes_at_capture=None)

    async def verified_click(x: int, y: int, **_binding: object) -> dict:
        body.clicks.append((x, y))
        return {"ok": True, "action": "click", "target_state_changed": True}

    body.vision_click = verified_click  # type: ignore[method-assign]
    _, events = _run(monkeypatch, body, [_click(), {"action": "done", "reason": "x"}])
    assert not [1 for t, _ in events if t == "VISION_LATE_EFFECT_RECHECK"]


def test_a_typing_action_is_never_rechecked() -> None:
    assert "type" not in extension_run._LATE_EFFECT_RECHECK_KINDS
    assert "done" not in extension_run._LATE_EFFECT_RECHECK_KINDS


def test_last_reply_wording_for_each_verdict() -> None:
    prepared = [{"kind": "click", "action": {"action": "click"}, "bound_label": "Save"}]
    base = {"ok": True, "verified": False, "status": "executed"}
    late = _last_reply_summary(prepared, {0: {**base, "late_effect_recheck": "late_effect"}}, [])
    assert "late effect: the screen changed after a delay" in late and "do not repeat" in late
    none = _last_reply_summary(
        prepared, {0: {**base, "late_effect_recheck": "no_effect", "late_effect_recheck_seconds": 0.8}}, []
    )
    assert "no effect (re-checked after 0.8s)" in none
    assert "re-checked" not in _last_reply_summary(prepared, {0: base}, [])
