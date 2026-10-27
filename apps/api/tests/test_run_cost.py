"""A run's model spend lands on its row as calls happen, and each chat thread reports it."""

import uuid

from app import model_call_ledger, models
from app.cognition import OperatorChatService
from app.db import SessionLocal, init_db


def _call(cost: float) -> dict:
    return {"ok": True, "metadata": {"latency_ms": 5, "model": "m", "token_usage": {"cost": cost}}}


def _run_with_thread(db) -> tuple[int, int, int]:
    user = models.User(email=f"cost-{uuid.uuid4().hex}@example.com", name="Cost")
    db.add(user)
    db.flush()
    run = models.OperatorRun(user_id=user.id, goal="Apply", target_url="https://ats.example.com/apply", current_state="RUNNING")
    db.add(run)
    db.flush()
    thread = models.ChatThread(user_id=user.id, run_id=run.id, mode="run", status="active")
    idle = models.ChatThread(user_id=user.id, run_id=None, mode="global", status="active")
    db.add_all([thread, idle])
    db.commit()
    return user.id, run.id, thread.id


def test_spend_is_flushed_to_the_run_while_it_runs_and_on_release(monkeypatch) -> None:
    init_db()
    with SessionLocal() as db:
        user_id, run_id, thread_id = _run_with_thread(db)

    token = model_call_ledger.bind(run_id)
    model_call_ledger.record("vision_agent_step", _call(0.0005))  # the run's first spend shows at once
    model_call_ledger.record("vision_agent_step", _call(0.0002))  # small and soon after: batched
    with SessionLocal() as db:
        assert abs(float(db.get(models.OperatorRun, run_id).cost_estimate) - 0.0005) < 1e-9
    model_call_ledger.record("grounding_judge", _call(0.0028))  # crosses the batch: flushed
    with SessionLocal() as db:
        assert abs(float(db.get(models.OperatorRun, run_id).cost_estimate) - 0.0035) < 1e-9
    model_call_ledger.record("vision_agent_step", _call(0.0004))
    tally = model_call_ledger.release(token, run_id)
    assert tally["vision_agent_step"]["calls"] == 3

    with SessionLocal() as db:
        assert abs(float(db.get(models.OperatorRun, run_id).cost_estimate) - 0.0039) < 1e-9
        threads = {thread.id: thread for thread in OperatorChatService(db).list_threads(user_id=user_id)}
        assert threads[thread_id].cost_usd == 0.0039
        assert [thread.cost_usd for thread in threads.values() if thread.run_id is None] == [None]


def test_a_thread_that_carried_several_runs_reports_them_all() -> None:
    init_db()
    with SessionLocal() as db:
        user_id, run_id, thread_id = _run_with_thread(db)
        earlier = models.OperatorRun(user_id=user_id, goal="Apply", target_url="https://ats.example.com/a", current_state="SUCCEEDED", cost_estimate=0.02)
        db.add(earlier)
        db.flush()
        db.get(models.OperatorRun, run_id).cost_estimate = 0.005
        db.add_all([
            models.ChatMessage(thread_id=thread_id, role="assistant", content="Started", payload_json={"run_id": earlier.id}),
            models.ChatMessage(thread_id=thread_id, role="assistant", content="Started", payload_json={"run_id": run_id}),
            models.ChatMessage(thread_id=thread_id, role="user", content="hi", payload_json={}),
        ])
        db.commit()
        threads = {thread.id: thread for thread in OperatorChatService(db).list_threads(user_id=user_id)}
        assert threads[thread_id].cost_usd == 0.025
