"""Remembered application answers are visible + editable from the profile."""

from fastapi.testclient import TestClient

from app import models
from app.db import SessionLocal, init_db
from app.main import app
from app.runtime import OperatorRuntimeEngine

client = TestClient(app)


def _seed_answer(question_pattern: str, answer: str) -> int:
    init_db()
    with SessionLocal() as db:
        uid = OperatorRuntimeEngine(db).ensure_default_user().id
        memory = models.AnswerMemory(user_id=uid, question_pattern=question_pattern, approved_answer=answer, confidence=0.9)
        db.add(memory)
        db.commit()
        return int(memory.id)


def test_profile_lists_saved_answers() -> None:
    aid = _seed_answer("field-1: Are you authorized to work in the US?", "Yes")
    body = client.get("/profiles/me").json()
    match = next((a for a in body["common_answers"] if a["id"] == aid), None)
    assert match is not None
    assert match["answer"] == "Yes"
    assert "authorized" in match["question_pattern"].lower()


def test_update_saved_answer() -> None:
    aid = _seed_answer("field-2: Expected salary?", "100000")
    res = client.patch(f"/operator/answers/{aid}", json={"answer": "120000"})
    assert res.status_code == 200 and res.json()["answer"] == "120000"
    body = client.get("/profiles/me").json()
    assert any(a["id"] == aid and a["answer"] == "120000" for a in body["common_answers"])


def test_delete_saved_answer() -> None:
    aid = _seed_answer("field-3: Require sponsorship?", "No")
    assert client.delete(f"/operator/answers/{aid}").json()["deleted"] is True
    body = client.get("/profiles/me").json()
    assert all(a["id"] != aid for a in body["common_answers"])


def test_update_missing_answer_is_404() -> None:
    assert client.patch("/operator/answers/99999999", json={"answer": "x"}).status_code == 404
