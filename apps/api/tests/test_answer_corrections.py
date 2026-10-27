"""Keep/Change flywheel: confirm-ask corrections become structured DATA (eval cases + queryable
records), never branching rules. A Change is a regression guard — the rejected value must not auto-
reappear for that field."""

import uuid

from app import models
from app.answer_corrections import (
    build_correction_record,
    correction_to_case,
    load_corrections,
    record_correction,
)
from app.db import SessionLocal, init_db

_CHANGE_PAYLOAD = {
    "field_key": "q1",
    "label": "Have you built integrations with LLMs?",
    "is_choice": True,
    "options": ["Yes", "No"],
    "proposed_option": "No",
    "user_choice": "change",
    "raw_user_answer": "Yes",
    "interpreted_answer": {"resolved_value": "Yes"},
}


def test_build_correction_record_captures_change_and_keep() -> None:
    rec = build_correction_record(task_payload=_CHANGE_PAYLOAD, target_url="https://ats.rippling.com/x/apply", run_id=5, ts="2026-06-26T00:00:00")
    assert rec is not None
    assert rec["agent_proposed"] == "No" and rec["user_action"] == "change" and rec["user_value"] == "Yes"
    assert rec["field_type"] == "choice" and rec["host"] == "ats.rippling.com" and rec["run_id"] == 5

    keep_payload = {**_CHANGE_PAYLOAD, "user_choice": "keep", "proposed_option": "Yes", "raw_user_answer": "Yes", "interpreted_answer": {"resolved_value": "Yes"}}
    keep = build_correction_record(task_payload=keep_payload, ts="t")
    assert keep is not None and keep["user_action"] == "keep"


def test_build_correction_record_is_none_for_non_confirm_ask() -> None:
    assert build_correction_record(task_payload={"field_key": "q", "label": "x", "user_choice": "change"}, ts="t") is None  # no surfaced proposal
    assert build_correction_record(task_payload={"proposed_answer": "X"}, ts="t") is None  # not keep/change


def test_correction_to_case_change_is_a_regression_guard() -> None:
    case = correction_to_case({"field_key": "q1", "field_label": "Have you built integrations with LLMs?", "options": ["Yes", "No"], "agent_proposed": "No", "user_action": "change", "user_value": "Yes"})
    assert case["unacceptable"] == ["No"]  # the rejected value must never auto-reappear for this field
    assert "Yes" in case["acceptable"]
    assert set(case["expected"]) == {"auto", "confirm", "ask"}

    keep_case = correction_to_case({"field_key": "q1", "field_label": "x", "options": ["Yes", "No"], "agent_proposed": "Yes", "user_action": "keep", "user_value": "Yes"})
    assert keep_case["unacceptable"] == []  # a kept proposal forbids nothing


def test_record_and_load_round_trip_is_user_scoped() -> None:
    init_db()
    with SessionLocal() as db:
        user = models.User(email=f"fly-{uuid.uuid4().hex}@example.com", name="Fly")
        db.add(user)
        db.flush()
        run = models.OperatorRun(user_id=user.id, goal="Apply", target_url="https://ats.rippling.com/x/apply", current_state="WAITING_FOR_USER")
        db.add(run)
        db.commit()

        rec = record_correction(db, run=run, task_payload=_CHANGE_PAYLOAD)
        db.commit()
        assert rec is not None

        loaded = load_corrections(db, user_id=user.id)
        assert any(r["field_key"] == "q1" and r["user_value"] == "Yes" for r in loaded)
        # scoped: a different user sees none of this user's corrections.
        assert load_corrections(db, user_id=user.id + 999_999) == []
