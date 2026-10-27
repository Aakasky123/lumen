"""The start reply promises what the run's own final-action policy will do: under Full autonomy it does not claim
it will pause for approval."""

from __future__ import annotations

from types import SimpleNamespace

from app.cognition import _final_action_promise


def _run(policy: dict | None) -> SimpleNamespace:
    return SimpleNamespace(confidence_state={"final_action_policy": policy} if policy is not None else {})


def test_full_autonomy_says_lumen_takes_the_final_step() -> None:
    text = _final_action_promise(_run({"final_action_mode": "autonomous", "standing_authority_granted": True}))
    assert "Full autonomy is on" in text and "explicit approval" not in text


def test_approval_mode_and_unknown_policy_promise_the_pause() -> None:
    for run in (_run({"final_action_mode": "approval", "standing_authority_granted": False}), _run(None), None):
        assert _final_action_promise(run) == "I will pause before any consequential final action for your explicit approval."
