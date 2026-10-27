"""Every user-message write path loses a TOTP seed: chat turns, question answers, payload text, direct writes."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from app import models, totp
from app.cognition import OperatorChatService
from app.config import get_settings
from app.db import SessionLocal, init_db

SEED = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
SEED_SPACED = "JBSW Y3DP EHPK 3PXP JBSW Y3DP EHPK 3PXP"


def _everything(run_id: int, thread_id: int, question_id: int | None = None) -> str:
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        parts: list[Any] = [run.goal, run.status_message]
        parts += [(m.content, m.payload_json) for m in db.query(models.ChatMessage).filter_by(thread_id=thread_id)]
        parts += [(e.event_type, e.event_payload) for e in db.query(models.CognitiveEvent).filter_by(run_id=run_id)]
        if question_id:
            question = db.get(models.OperatorQuestion, question_id)
            parts += [question.answer_payload, question.prompt]
        state = {k: v for k, v in (run.confidence_state or {}).items() if k != "totp_seeds"}
        parts.append(state)
    return json.dumps(parts, default=str)


def _setup(with_question: bool = False) -> tuple[int, int, int | None]:
    init_db()
    with SessionLocal() as db:
        user = models.User(email=f"totp-writes-{uuid.uuid4().hex}@example.test", name="Totp Writes")
        db.add(user)
        db.flush()
        run = models.OperatorRun(
            user_id=user.id, application_id=None, goal="Log in.", target_url="https://shop.example.test/", current_state="WAITING_FOR_USER"
        )
        db.add(run)
        db.flush()
        thread = OperatorChatService(db).create_thread(user_id=int(user.id), run_id=int(run.id))
        question_id = None
        if with_question:
            question = models.OperatorQuestion(
                run_id=run.id, thread_id=thread.id, question_type="free_text", prompt="What is the key?", status="pending"
            )
            db.add(question)
            db.flush()
            question_id = int(question.id)
        db.commit()
        return int(run.id), int(thread.id), question_id


def _stored_seeds(run_id: int) -> list[str]:
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        stored = (run.confidence_state or {}).get("totp_seeds")
    return [totp.seed_identity(s) for s in totp.reveal_seeds(stored, get_settings().token_encryption_key)]


def test_a_question_answer_is_scrubbed_and_its_seed_kept_encrypted_on_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    run_id, thread_id, question_id = _setup(with_question=True)
    monkeypatch.setattr(OperatorChatService, "_apply_answer", lambda self, run, question, payload: {"applied": False})
    monkeypatch.setattr(OperatorChatService, "_require_live_question_run", lambda self, rid: self.db.get(models.OperatorRun, rid))
    answer = f"The authenticator key is {SEED_SPACED}."
    with SessionLocal() as db:
        try:
            OperatorChatService(db).answer_question(
                question_id=question_id,
                answer=answer,
                payload={"note": f"TOTP secret key {SEED}", "nested": {"again": [f"setup key {SEED.lower()}"]}},
                auto_resume=False,
            )
        finally:
            db.commit()
    blob = _everything(run_id, thread_id, question_id)
    flat = blob.lower().replace(" ", "")
    assert SEED not in blob and SEED_SPACED not in blob and SEED.lower()[:8] not in flat
    assert "[totp seed provided]" in blob
    assert set(_stored_seeds(run_id)) == {SEED}


def test_every_user_message_write_is_scrubbed_in_the_shared_writer_including_payload_text() -> None:
    run_id, thread_id, _ = _setup()
    with SessionLocal() as db:
        service = OperatorChatService(db)
        thread = db.get(models.ChatThread, thread_id)
        service._add_message(
            thread,
            role="user",
            content=f"authenticator key {SEED}",
            payload={"text": f"2FA secret key: {SEED_SPACED}", "items": [{"quote": f"TOTP {SEED.lower()}"}], "n": 3},
        )
        service._add_message(thread, role="assistant", content="ordinary reply ABCDEFGH23456789", payload={})
        db.commit()
    blob = _everything(run_id, thread_id)
    assert SEED not in blob and SEED_SPACED not in blob and SEED.lower()[:8] not in blob.lower().replace(" ", "")
    assert "ABCDEFGH23456789" in blob and '"n": 3' in blob
    assert set(_stored_seeds(run_id)) == {SEED}


def test_the_chat_message_payload_is_scrubbed_on_the_turn_path(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Stop(RuntimeError):
        pass

    def stop(*_a: Any, **_k: Any) -> None:
        raise _Stop()

    monkeypatch.setattr(OperatorChatService, "_classify_chat_scope", stop)
    run_id, thread_id, _ = _setup()
    with SessionLocal() as db:
        with pytest.raises(_Stop):
            OperatorChatService(db)._prepare_chat_turn(
                thread_id=thread_id, content="here", payload={"pasted": f"authenticator key {SEED}"}
            )
        db.commit()
    blob = _everything(run_id, thread_id)
    assert SEED not in blob and SEED.lower()[:8] not in blob.lower().replace(" ", "")
    assert set(_stored_seeds(run_id)) == {SEED}
