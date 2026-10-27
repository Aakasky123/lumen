"""TOTP seed detection (case, grouping, context) and chat-message ingestion."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from app import models, totp
from app.db import SessionLocal, init_db

SEED = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
SEED_SPACED = "JBSW Y3DP EHPK 3PXP JBSW Y3DP EHPK 3PXP"


def test_detection_is_case_insensitive_any_grouping_and_needs_an_authenticator_context() -> None:
    lowered = " ".join(SEED.lower()[i : i + 4] for i in range(0, len(SEED), 4))
    dashed = "-".join(SEED[i : i + 8] for i in range(0, len(SEED), 8))
    uneven = f"{SEED[:5]} {SEED[5:10]} {SEED[10:15]} {SEED[15:20]} {SEED[20:25]} {SEED[25:30]} {SEED[30:]}"
    for written in (SEED, SEED.lower(), lowered, dashed, uneven, SEED_SPACED):
        cleaned, seeds = totp.redact_seeds(f"Use the TOTP secret key {written} to finish the login.")
        assert [totp.seed_identity(s) for s in seeds] == [SEED], written
        assert "[totp seed provided]" in cleaned
        assert SEED.lower()[:8] not in cleaned.lower().replace(" ", "").replace("-", "")


def test_an_ordinary_uppercase_id_is_never_scrubbed_and_otpauth_always_is() -> None:
    text = "Open order ABCDEFGH23456789 and confirm its status."
    assert totp.redact_seeds(text) == (text, [])
    assert totp.redact_seeds(f"Reference {SEED} is the invoice.")[1] == []
    assert totp.redact_seeds(f"Enter the verification code from key {SEED}.")[1]
    uri = f"otpauth://totp/Shop:me?secret={SEED}"
    cleaned, seeds = totp.redact_seeds(f"Order ABCDEFGH23456789, {uri}")
    assert seeds and "otpauth" not in cleaned and "ABCDEFGH23456789" in cleaned


def test_prose_is_not_a_key_and_a_neighbouring_word_is_covered_not_leaked() -> None:
    assert totp.find_seeds("the authenticator application asks for the verification code every thirty seconds " * 5) == []
    groups = " ".join(SEED.lower()[i : i + 4] for i in range(0, 32, 4))
    cleaned, seeds = totp.redact_seeds(f"With the authenticator key with {groups} go.")
    assert SEED.lower()[:4] not in cleaned and seeds


def test_the_seed_is_replaced_before_the_chat_message_is_stored_and_kept_encrypted_on_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.cognition import OperatorChatService
    from app.config import get_settings

    class _Stop(RuntimeError):
        pass

    def stop(*_a: Any, **_k: Any) -> None:
        raise _Stop()

    monkeypatch.setattr(OperatorChatService, "_classify_chat_scope", stop)
    init_db()
    with SessionLocal() as db:
        user = models.User(email=f"totp-chat-{uuid.uuid4().hex}@example.test", name="Totp Chat")
        db.add(user)
        db.flush()
        run = models.OperatorRun(
            user_id=user.id, application_id=None, goal="Log in.", target_url="https://shop.example.test/", current_state="RUNNING"
        )
        db.add(run)
        db.flush()
        thread = OperatorChatService(db).create_thread(user_id=int(user.id), run_id=int(run.id))
        db.commit()
        thread_id, run_id = int(thread.id), int(run.id)
    message = f"Log in to https://shop.example.test; the authenticator key is {SEED_SPACED}. Order ABCDEFGH23456789."
    with SessionLocal() as db:
        service = OperatorChatService(db)
        with pytest.raises(_Stop):
            service._prepare_chat_turn(thread_id=thread_id, content=message, payload={})
        db.commit()
    with SessionLocal() as db:
        rows = [m.content for m in db.query(models.ChatMessage).filter_by(thread_id=thread_id)]
        run = db.get(models.OperatorRun, run_id)
        stored = json.dumps([rows, run.goal, run.confidence_state], default=str)
        events = json.dumps(
            [(e.event_type, e.event_payload) for e in db.query(models.CognitiveEvent).filter_by(run_id=run_id)], default=str
        )
        titles = json.dumps([t.title for t in db.query(models.ChatThread).filter_by(id=thread_id)], default=str)
        encrypted = (run.confidence_state or {}).get("totp_seeds")
    for blob in (stored, events, titles):
        flat = blob.lower().replace(" ", "")
        assert SEED not in blob and SEED_SPACED not in blob and SEED.lower()[:8] not in flat
    assert any("[totp seed provided]" in row and "ABCDEFGH23456789" in row for row in rows)
    revealed = totp.reveal_seeds(encrypted, get_settings().token_encryption_key)
    assert [totp.seed_identity(s) for s in revealed] == [SEED]


def test_a_task_created_from_a_redacted_message_keeps_the_turns_encrypted_seed() -> None:
    from app.config import get_settings
    from app.runtime import OperatorRuntimeEngine

    init_db()
    key = get_settings().token_encryption_key
    _cleaned, stored = totp.protect_goal(f"authenticator key {SEED}", key)
    with SessionLocal() as db:
        user = models.User(email=f"totp-turn-{uuid.uuid4().hex}@example.test", name="Totp Turn")
        db.add(user)
        db.flush()
        run = OperatorRuntimeEngine(db).create_browser_task(
            user_id=int(user.id),
            goal="Log in at https://shop.example.test with the [totp seed provided].",
            target_url="https://shop.example.test/login",
            extra_totp_seeds=stored,
        )
        db.commit()
        revealed = totp.reveal_seeds((run.confidence_state or {}).get("totp_seeds"), key)
    assert [totp.seed_identity(s) for s in revealed] == [SEED]
