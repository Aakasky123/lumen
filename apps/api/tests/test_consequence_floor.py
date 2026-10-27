"""A control judged stricter earlier in a run is never judged more leniently later in that run."""

from __future__ import annotations

from typing import Any

import pytest

from app import consequence_floor, extension_run, model_call_ledger
from app.db import init_db

RUN = 424242


def _control(index: int, text: str, selector: str, row: str = "") -> dict[str, Any]:
    return {"index": index, "tag": "button", "text": text, "selector": selector, "row_text": row, "x": 10, "y": 10}


@pytest.fixture()
def judge(monkeypatch):
    init_db()
    answers: list[dict[str, Any]] = []

    def fake(self, payload, *, image=None):  # noqa: ANN001
        return dict(answers.pop(0))

    monkeypatch.setattr("app.lumen_model.LumenModelClient.judge_blocked_click_semantics", fake)
    token = model_call_ledger.bind(RUN)
    try:
        yield answers
    finally:
        model_call_ledger.release(token, RUN)
        consequence_floor.release(RUN)


def _run(control: dict[str, Any]) -> dict[str, Any]:
    return extension_run._judge_click_consequence(
        0,
        goal="Book a trip",
        safety_policy={},
        shot={"url": "https://travel.example/hold#x", "title": "t", "page_evidence": {"controls": [control]}},
        action={"action": "click", "control_index": control["index"]},
        bound_control=control,
        selector=control["selector"],
        x=10,
        y=10,
        recent_verified_history=[],
        operator_instructions=[],
    )


def _v(classification: str, confidence: float = 0.99) -> dict[str, Any]:
    return {"classification": classification, "confidence": confidence, "evidence": f"said {classification}"}


def test_ordinary_after_irreversible_for_same_control_stays_gated(judge) -> None:
    judge.extend([_v("irreversible_final"), _v("intermediate")])
    control = _control(1, "Hold booking", "#hold")
    assert _run(control)["classification"] == "irreversible_final"
    again = _run(control)
    assert again["classification"] == "irreversible_final"
    assert again["metadata"]["monotonic_override"]["from"] == "intermediate"


def test_ordinary_after_uncertain_is_uncertain(judge) -> None:
    judge.extend([_v("uncertain", 0.5), _v("intermediate")])
    control = _control(1, "Hold booking", "#hold")
    _run(control)
    assert _run(control)["classification"] == "uncertain"


def test_same_label_on_a_different_row_is_not_affected(judge) -> None:
    judge.extend([_v("irreversible_final"), _v("intermediate")])
    assert _run(_control(1, "Hold booking", "#hold-1", "Flight A"))["classification"] == "irreversible_final"
    other = _run(_control(2, "Hold booking", "#hold-2", "Flight B"))
    assert other["classification"] == "intermediate"
    assert "monotonic_override" not in (other.get("metadata") or {})


def test_first_verdict_unchanged_and_stricter_later_verdict_raises_floor(judge) -> None:
    judge.extend([_v("intermediate"), _v("irreversible_final"), _v("intermediate")])
    control = _control(1, "Hold booking", "#hold")
    first = _run(control)
    assert first == _v("intermediate")
    assert _run(control)["classification"] == "irreversible_final"
    assert _run(control)["classification"] == "irreversible_final"


def test_failed_judge_call_is_not_recorded(judge) -> None:
    judge.extend([{**_v("uncertain", 0.0), "fail_closed": True}, _v("intermediate")])
    control = _control(1, "Hold booking", "#hold")
    _run(control)
    assert _run(control)["classification"] == "intermediate"


def test_floor_is_scoped_to_the_run(judge) -> None:
    judge.extend([_v("irreversible_final"), _v("intermediate")])
    control = _control(1, "Hold booking", "#hold")
    _run(control)
    consequence_floor.release(RUN)
    assert _run(control)["classification"] == "intermediate"


def _payment_calls(monkeypatch, answers, selector="#hold"):
    queue = [dict(a) for a in answers]
    monkeypatch.setattr(extension_run, "_payment_floor_verdict_unfloored", lambda *a, **k: dict(queue.pop(0)))
    evidence = {"url": "https://travel.example/hold"}
    return [
        extension_run._payment_floor_verdict(0, submit_label="Hold booking", page_evidence=evidence, selector=selector)
        for _ in answers
    ]


_T = {"payment": True, "payment_unknown": False, "summary": "pays"}
_F = {"payment": False, "payment_unknown": False, "summary": ""}


@pytest.fixture()
def payment_run():
    token = model_call_ledger.bind(RUN)
    try:
        yield
    finally:
        model_call_ledger.release(token, RUN)
        consequence_floor.release(RUN)


def test_single_payment_true_asks_for_that_click(monkeypatch, payment_run) -> None:
    assert _payment_calls(monkeypatch, [_T])[0]["payment"] is True


def test_one_true_then_false_judges_the_second_click_on_its_own(monkeypatch, payment_run) -> None:
    results = _payment_calls(monkeypatch, [_T, _F, _F])
    assert [r["payment"] for r in results] == [True, False, False]
    assert not any(r.get("monotonic_override") for r in results)


def test_two_trues_make_payment_sticky_for_that_control_only(monkeypatch, payment_run) -> None:
    results = _payment_calls(monkeypatch, [_T, _T, _F])
    assert [r["payment"] for r in results] == [True, True, True]
    assert results[2]["monotonic_override"] is True
    assert _payment_calls(monkeypatch, [_F], selector="#other")[0]["payment"] is False


def test_deterministic_signal_makes_payment_sticky_at_once(monkeypatch, payment_run) -> None:
    det = {**_T, "deterministic_hit": True, "deterministic_reason": "autocomplete:cc-number"}
    results = _payment_calls(monkeypatch, [det, _F])
    assert [r["payment"] for r in results] == [True, True]
