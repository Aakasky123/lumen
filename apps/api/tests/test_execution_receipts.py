"""Honesty boundary: planned != attempted != succeeded != VERIFIED. Run-control derives its reply
ONLY from a verified execution receipt (before/after read back from the DB) — a failed/no-op action
is reported accurately, never narrated as success. And an ambiguous stop fails closed instead of
guessing which run to kill. (Phase 1 of the trustworthy-loop work.)"""

import uuid

from sqlalchemy import select

from app import models
from app.cognition import OperatorChatService
from app.db import SessionLocal, init_db
from app.operation_receipt import compute_outcome


def test_compute_outcome_covers_every_axis() -> None:
    # outcome is DERIVED from independent attempt/execution/verification axes (runtime, not planner)
    assert compute_outcome(attempt={"status": "not_attempted", "reason": "precondition_unmet"}, execution={"status": "not_executed"}, verification={"status": "not_verified"}) == "precondition_failed"
    assert compute_outcome(attempt={"status": "not_attempted", "reason": "dependency_failed"}, execution={"status": "not_executed"}, verification={"status": "not_verified"}) == "dependency_skipped"
    assert compute_outcome(attempt={"status": "attempted"}, execution={"status": "rejected"}, verification={"status": "not_verified"}) == "execution_failed"
    assert compute_outcome(attempt={"status": "attempted"}, execution={"status": "succeeded"}, verification={"status": "failed"}) == "verification_failed"
    assert compute_outcome(attempt={"status": "attempted"}, execution={"status": "succeeded"}, verification={"status": "inconclusive"}) == "verification_inconclusive"
    assert compute_outcome(attempt={"status": "attempted"}, execution={"status": "succeeded"}, verification={"status": "succeeded"}) == "verified_succeeded"


def _new_user(db) -> int:
    user = models.User(email=f"rcpt-{uuid.uuid4().hex}@example.com", name="Receipt")
    db.add(user)
    db.flush()
    return int(user.id)


def _deactivate_all_in_browser(db) -> None:
    for run in db.scalars(select(models.OperatorRun).where(models.OperatorRun.current_state.in_(("RUNNING", "WAITING_FOR_USER")))).all():
        if isinstance((run.confidence_state or {}).get("in_browser"), dict):
            run.current_state = "CANCELLED"
    db.commit()


def _make_run(db, uid: int, state: str):
    run = models.OperatorRun(
        user_id=uid,
        goal="Apply",
        target_url="https://ats.example.com/apply",
        current_state=state,
        confidence_state={"in_browser": {"url": "https://ats.example.com/apply", "device_id": "dev-x"}},
    )
    db.add(run)
    db.flush()
    return run


def test_stop_produces_verified_receipt_and_grounded_reply() -> None:
    init_db()
    with SessionLocal() as db:
        uid = _new_user(db)
        run = _make_run(db, uid, "WAITING_FOR_USER")
        svc = OperatorChatService(db)
        thread = svc.create_thread(user_id=uid, run_id=run.id, title="t")
        db.commit()
        result = svc._try_run_control_from_chat(thread, chat_scope={"action": "stop"})
        assert result["applied"] is True
        receipt = result["receipts"][0]
        assert receipt["attempt"]["status"] == "attempted"
        assert receipt["execution"]["status"] == "succeeded"
        assert receipt["verification"]["status"] == "succeeded"
        assert receipt["verification"]["before"]["state"] == "WAITING_FOR_USER"
        assert receipt["verification"]["after"]["state"] == "CANCELLED"  # read back from the DB, not assumed
        assert receipt["outcome"] == "verified_succeeded"  # derived by the runtime, not supplied
        assert db.get(models.OperatorRun, run.id).current_state == "CANCELLED"  # the run REALLY transitioned
        assert "cancelled" in result["assistant_text"].lower()


def test_failed_stop_is_not_narrated_as_success(monkeypatch) -> None:
    init_db()
    with SessionLocal() as db:
        uid = _new_user(db)
        run = _make_run(db, uid, "RUNNING")
        svc = OperatorChatService(db)
        thread = svc.create_thread(user_id=uid, run_id=run.id, title="t")
        db.commit()
        import app.runtime as runtime

        # cancel_run "runs" but the state does NOT reach CANCELLED -> the receipt must be 'failed'
        # and the reply must NOT claim success.
        monkeypatch.setattr(runtime.OperatorRuntimeEngine, "cancel_run", lambda self, rid, **kwargs: db.get(models.OperatorRun, rid))
        result = svc._try_run_control_from_chat(thread, chat_scope={"action": "stop"})
        receipt = result["receipts"][0]
        # executor "succeeded" (returned) but the postcondition was not met -> verification_failed,
        # NOT a fabricated success.
        assert receipt["execution"]["status"] == "succeeded"
        assert receipt["verification"]["status"] == "failed"
        assert receipt["verification"]["after"]["state"] == "RUNNING"
        assert receipt["outcome"] == "verification_failed"
        assert result["applied"] is False
        text = result["assistant_text"].lower()
        assert "still running" in text
        assert "→ cancelled" not in text and "stopped run" not in text


def test_stop_with_no_active_run_fails_closed() -> None:
    init_db()
    with SessionLocal() as db:
        _deactivate_all_in_browser(db)
        uid = _new_user(db)
        svc = OperatorChatService(db)
        thread = svc.create_thread(user_id=uid, run_id=None, title="t")
        db.commit()
        result = svc._try_run_control_from_chat(thread, chat_scope={"action": "stop"})
        assert result["applied"] is False
        assert result["receipts"] == []
        assert "no active" in result["assistant_text"].lower()


def test_stop_with_multiple_active_runs_fails_closed() -> None:
    init_db()
    with SessionLocal() as db:
        _deactivate_all_in_browser(db)
        uid = _new_user(db)
        _make_run(db, uid, "WAITING_FOR_USER")
        _make_run(db, uid, "RUNNING")
        svc = OperatorChatService(db)
        thread = svc.create_thread(user_id=uid, run_id=None, title="t")
        db.commit()
        result = svc._try_run_control_from_chat(thread, chat_scope={"action": "stop"})
        assert result["applied"] is False
        assert result["receipts"] == []
        assert "which one" in result["assistant_text"].lower()
