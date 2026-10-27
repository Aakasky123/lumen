"""The markdown user memory: one readable file, upserts by key, no scores, no blobs."""

from __future__ import annotations

import pytest

from app import user_memory


@pytest.fixture()
def isolated_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(user_memory, "_memory_dir", lambda: tmp_path)
    return tmp_path


def test_remember_writes_one_readable_markdown_bullet(isolated_memory) -> None:
    fact = user_memory.remember(1, "name", "Jordan Park", source_run_id=531)
    assert fact is not None and fact.key == "name"

    text = user_memory.markdown(1)
    assert text.startswith("# What Lumen knows about you")
    assert "- name: Jordan Park" in text
    assert "run 531" in text

    facts = user_memory.facts(1)
    assert [(f.key, f.value) for f in facts] == [("name", "Jordan Park")]


def test_remember_upserts_by_key_never_duplicates(isolated_memory) -> None:
    user_memory.remember(1, "location", "Jersey City, NJ")
    user_memory.remember(1, "location", "Hoboken, NJ")

    facts = user_memory.facts(1)
    assert len(facts) == 1
    assert facts[0].value == "Hoboken, NJ"
    assert user_memory.markdown(1).count("- location:") == 1


def test_keys_are_normalized_and_junk_is_rejected(isolated_memory) -> None:
    fact = user_memory.remember(1, "Preferred Name!", "Jordan")
    assert fact is not None and fact.key == "preferred_name"
    assert user_memory.remember(1, "   ", "value") is None
    assert user_memory.remember(1, "key", "   ") is None
    # Multiline values collapse to one line so the file stays line-parseable.
    fact = user_memory.remember(1, "summary", "line one\nline two")
    assert fact is not None and fact.value == "line one line two"


def test_forget_removes_the_line(isolated_memory) -> None:
    user_memory.remember(1, "name", "Jordan")
    user_memory.remember(1, "location", "NJ")
    assert user_memory.forget(1, "name") is True
    assert user_memory.forget(1, "name") is False
    assert [f.key for f in user_memory.facts(1)] == ["location"]
    assert "- name:" not in user_memory.markdown(1)


def test_users_are_isolated_and_hand_edits_survive(isolated_memory) -> None:
    user_memory.remember(1, "name", "Jordan")
    user_memory.remember(2, "name", "Someone Else")
    assert user_memory.facts(1)[0].value == "Jordan"
    assert user_memory.facts(2)[0].value == "Someone Else"

    # A user editing their own file by hand stays parseable; prose lines are ignored.
    path = user_memory.memory_path(1)
    path.write_text(
        user_memory.markdown(1) + "\nSome prose the user typed.\n- nickname: Aak\n",
        encoding="utf-8",
    )
    keys = [f.key for f in user_memory.facts(1)]
    assert keys == ["name", "nickname"]
    user_memory.remember(1, "name", "Jordan S.")
    assert "Some prose the user typed." in user_memory.markdown(1)


def test_chat_remember_writes_markdown_and_never_bluffs(monkeypatch) -> None:
    """A conversational "remember X" must produce a VERIFIED markdown write; the reply
    is rendered from the write result, never from the model's claim."""
    from app import models, user_memory
    from app.cognition import OperatorChatService
    from app.db import SessionLocal, init_db
    from app.runtime import OperatorRuntimeEngine

    init_db()
    with SessionLocal() as db:
        user = OperatorRuntimeEngine(db).ensure_default_user()
        service = OperatorChatService(db)
        thread = models.ChatThread(user_id=user.id, title="memory test")
        db.add(thread)
        db.flush()

        result = service._try_remember_user_fact_from_chat(
            thread,
            chat_scope={
                "action": "remember_user_fact",
                "remember_request": {"key": "Preferred Name", "value": "Jordan"},
            },
        )
        assert result is not None and result["remembered"] is True
        assert result["key"] == "preferred_name"
        assert "preferred name: Jordan" in result["assistant_text"]
        assert any(
            fact.key == "preferred_name" and fact.value == "Jordan"
            for fact in user_memory.facts(user.id)
        )

        # A non-remember classification stays out of the memory path entirely.
        assert (
            service._try_remember_user_fact_from_chat(
                thread,
                chat_scope={"action": "none", "remember_request": {"key": "", "value": ""}},
            )
            is None
        )


def test_artifact_upload_remembers_model_chosen_facts(monkeypatch) -> None:
    """An uploaded document flows through the model extractor; only model-chosen facts
    land in the markdown memory, stamped with the document as source."""
    from app import models, user_memory
    from app.application_artifacts import ApplicationArtifactService
    from app.lumen_model import LumenModelClient
    from app.db import SessionLocal, init_db
    from app.runtime import OperatorRuntimeEngine

    init_db()
    monkeypatch.setattr(
        LumenModelClient,
        "extract_user_facts_from_document",
        lambda _self, payload: {
            "ok": True,
            "facts": [
                {"key": "current_title", "value": "Software Engineer"},
                {"key": "phone", "value": "555-0100"},
            ],
        },
    )
    with SessionLocal() as db:
        user = OperatorRuntimeEngine(db).ensure_default_user()
        artifact = models.ApplicationArtifact(
            user_id=user.id,
            artifact_kind="resume",
            filename="resume.pdf",
            text_content="Jordan Park. Software Engineer. 555-0100.",
            metadata_json={"display_name": "Resume"},
        )
        db.add(artifact)
        db.flush()

        result = ApplicationArtifactService(db).remember_user_facts(artifact)
        assert [item["key"] for item in result["remembered"]] == ["current_title", "phone"]
        facts = {fact.key: fact for fact in user_memory.facts(user.id)}
        assert facts["current_title"].value == "Software Engineer"
        assert "from Resume" in facts["current_title"].note

        # No extracted text -> honest no-op, never a fabricated extraction.
        empty = models.ApplicationArtifact(
            user_id=user.id, artifact_kind="artifact", filename="img.png", text_content="", metadata_json={}
        )
        db.add(empty)
        db.flush()
        assert ApplicationArtifactService(db).remember_user_facts(empty)["remembered"] == []


def test_reconciled_remember_dedupes_semantically_via_model(isolated_memory, monkeypatch) -> None:
    """The same underlying fact phrased differently must never create a second bullet:
    the model decides skip/update/add against existing facts; code enforces the shape."""
    from app.lumen_model import LumenModelClient

    user_memory.remember(1, "work_authorization", "Authorized to work in the US, no sponsorship needed")

    captured: dict = {}

    def fake_reconcile(_self, *, existing, candidates):
        captured["existing"] = existing
        captured["candidates"] = candidates
        return {
            "ok": True,
            "decisions": [
                {"candidate_index": 0, "action": "skip", "key": "work_authorization", "value": ""},
                {"candidate_index": 1, "action": "update", "key": "work_authorization", "value": "US citizen, never needs sponsorship"},
                {"candidate_index": 2, "action": "add", "key": "preferred_stack", "value": "Python"},
            ],
        }

    monkeypatch.setattr(LumenModelClient, "reconcile_memory_facts", fake_reconcile)
    applied = user_memory.remember_all_reconciled(
        object(),
        1,
        [
            {"key": "visa_sponsorship", "value": "No sponsorship needed"},
            {"key": "needs_sponsorship", "value": "US citizen, never needs sponsorship"},
            {"key": "preferred_stack", "value": "Python"},
        ],
    )

    assert [item["action"] for item in applied] == ["skip", "update", "add"]
    facts = {fact.key: fact.value for fact in user_memory.facts(1)}
    # ONE sponsorship bullet, updated in place under the existing key; no visa_sponsorship
    # or needs_sponsorship keys ever appear.
    assert facts == {
        "work_authorization": "US citizen, never needs sponsorship",
        "preferred_stack": "Python",
    }
    assert captured["existing"][0]["key"] == "work_authorization"


def test_reconciled_remember_guards_the_envelope_and_degrades_safely(isolated_memory, monkeypatch) -> None:
    from app.lumen_model import LumenModelClient

    user_memory.remember(1, "location", "Jersey City, NJ")

    # An "update" targeting a key that does not exist becomes an add under the
    # candidate's own key - the model cannot invent a third key spelling.
    monkeypatch.setattr(
        LumenModelClient,
        "reconcile_memory_facts",
        lambda _self, *, existing, candidates: {
            "ok": True,
            "decisions": [{"candidate_index": 0, "action": "update", "key": "hometown_area", "value": "Hoboken, NJ"}],
        },
    )
    applied = user_memory.remember_all_reconciled(object(), 1, [{"key": "current_city", "value": "Hoboken, NJ"}])
    assert applied[0]["action"] == "add"
    assert applied[0]["key"] == "current_city"

    # Model unavailable: degrade to exact-key upsert - never lose the fact.
    monkeypatch.setattr(
        LumenModelClient,
        "reconcile_memory_facts",
        lambda _self, *, existing, candidates: {"ok": False, "decisions": [], "reason": "down"},
    )
    applied = user_memory.remember_all_reconciled(object(), 1, [{"key": "phone", "value": "555-0100"}])
    assert applied[0]["action"] == "add"
    assert any(fact.key == "phone" for fact in user_memory.facts(1))


def test_conflicting_fact_writes_nothing_and_carries_both_sides(isolated_memory, monkeypatch) -> None:
    """Two different emails is the USER's call: a conflict decision must not touch the
    file and must surface both values so the caller can ask."""
    from app.lumen_model import LumenModelClient

    user_memory.remember(1, "email", "jordan.park@example.edu")
    monkeypatch.setattr(
        LumenModelClient,
        "reconcile_memory_facts",
        lambda _self, *, existing, candidates: {
            "ok": True,
            "decisions": [
                {"candidate_index": 0, "action": "conflict", "key": "email", "value": "jordan.park@example.com"}
            ],
        },
    )
    applied = user_memory.remember_all_reconciled(
        object(), 1, [{"key": "email_address", "value": "jordan.park@example.com"}]
    )

    assert applied[0]["action"] == "conflict"
    assert applied[0]["key"] == "email"
    assert applied[0]["existing_value"] == "jordan.park@example.edu"
    assert applied[0]["value"] == "jordan.park@example.com"
    # The file is untouched: still exactly one email bullet with the original value.
    facts = {fact.key: fact.value for fact in user_memory.facts(1)}
    assert facts == {"email": "jordan.park@example.edu"}


def test_chat_conflict_asks_and_forget_action_deletes(monkeypatch) -> None:
    from app import models, user_memory
    from app.lumen_model import LumenModelClient
    from app.cognition import OperatorChatService
    from app.db import SessionLocal, init_db
    from app.runtime import OperatorRuntimeEngine

    init_db()
    with SessionLocal() as db:
        user = OperatorRuntimeEngine(db).ensure_default_user()
        service = OperatorChatService(db)
        thread = models.ChatThread(user_id=user.id, title="conflict test")
        db.add(thread)
        db.flush()

        user_memory.remember(user.id, "email", "jordan.park@example.edu")
        monkeypatch.setattr(
            LumenModelClient,
            "reconcile_memory_facts",
            lambda _self, *, existing, candidates: {
                "ok": True,
                "decisions": [
                    {"candidate_index": 0, "action": "conflict", "key": "email", "value": "jordan.park@example.com"}
                ],
            },
        )
        result = service._try_remember_user_fact_from_chat(
            thread,
            chat_scope={
                "action": "remember_user_fact",
                "remember_request": {"key": "email", "value": "jordan.park@example.com"},
            },
        )
        assert result is not None and result["action"] == "conflict"
        assert "jordan.park@example.edu" in result["assistant_text"]
        assert "jordan.park@example.com" in result["assistant_text"]
        assert "Which should I keep" in result["assistant_text"]

        # The user resolves by forgetting the old value - model classifies forget_user_fact.
        forget_result = service._try_remember_user_fact_from_chat(
            thread,
            chat_scope={
                "action": "forget_user_fact",
                "remember_request": {"key": "email", "value": ""},
            },
        )
        assert forget_result is not None and forget_result["action"] == "forget"
        assert "Forgotten" in forget_result["assistant_text"]
        assert not any(fact.key == "email" for fact in user_memory.facts(user.id))


def test_facts_live_under_section_headings_and_can_move(isolated_memory) -> None:
    user_memory.remember(1, "years_python", "7", section="work")
    user_memory.remember(1, "name", "Jordan")  # a standard key lands in its own section
    user_memory.remember(1, "favorite_airline", "United", section="preferences")
    text = user_memory.markdown(1)
    assert text.index("## About you") < text.index("## Work") < text.index("## Preferences")
    assert {fact.key: fact.section for fact in user_memory.facts(1)} == {
        "name": "about",
        "years_python": "work",
        "favorite_airline": "preferences",
    }
    # Updating without a section keeps it in place; with one, it moves and an emptied section goes away.
    user_memory.remember(1, "favorite_airline", "Delta")
    assert next(f for f in user_memory.facts(1) if f.key == "favorite_airline").section == "preferences"
    user_memory.remember(1, "years_python", "8", section="other")
    assert "## Work" not in user_memory.markdown(1)
    assert next(f for f in user_memory.facts(1) if f.key == "years_python").section == "other"
    user_memory.forget(1, "favorite_airline")
    assert "## Preferences" not in user_memory.markdown(1)


def test_standard_facts_mirror_into_the_profile_columns(isolated_memory) -> None:
    import uuid

    from app import models
    from app.db import SessionLocal, init_db

    init_db()
    with SessionLocal() as db:
        user = models.User(email=f"mirror-{uuid.uuid4().hex}@example.com", name="Account Name")
        db.add(user)
        db.flush()
        # An existing column with no fact in memory is left alone (never wiped by an unrelated write).
        db.add(models.UserProfile(user_id=user.id, profile_json={}, phone="+1 555 0100"))
        db.flush()
        user_memory.remember(user.id, "location", "Hoboken, NJ")
        user_memory.remember(user.id, "linkedin", "https://linkedin.com/in/me")
        user_memory.remember(user.id, "name", "Someone From A Resume")
        user_memory.remember(user.id, "email", "other@example.com")
        user_memory.mirror_to_profile(db, user.id)
        profile = db.query(models.UserProfile).filter(models.UserProfile.user_id == user.id).one()
        assert (profile.location, profile.linkedin, profile.phone) == ("Hoboken, NJ", "https://linkedin.com/in/me", "+1 555 0100")
        # The account row is never touched by facts.
        assert (user.name, user.email.startswith("mirror-")) == ("Account Name", True)
        # A column clears only when its own fact is removed.
        user_memory.forget(user.id, "location")
        user_memory.mirror_to_profile(db, user.id, cleared=("location",))
        assert profile.location is None and profile.linkedin == "https://linkedin.com/in/me"
        db.rollback()


def test_learned_writes_refuse_secrets_and_implicit_sensitive_facts(isolated_memory) -> None:
    applied = user_memory.remember_all_reconciled(
        None,
        1,
        [
            {"key": "ssn", "value": "123-45-6789"},
            {"key": "card", "value": "4111 1111 1111 1111"},
            {"key": "phone", "value": "+1 (555) 010-0142"},
            {"key": "veteran_status", "value": "Not a veteran", "section": "sensitive"},
        ],
        implicit=True,
    )
    actions = {record["key"]: record["action"] for record in applied}
    assert actions == {"ssn": "refused", "card": "refused", "veteran_status": "refused", "phone": "add"}
    assert [fact.key for fact in user_memory.facts(1)] == ["phone"]
    # Asked for explicitly, a sensitive fact is kept (a secret-shaped number never is).
    explicit = user_memory.remember_all_reconciled(None, 1, [{"key": "veteran_status", "value": "Not a veteran", "section": "sensitive"}])
    assert explicit[0]["action"] == "add"


def test_explicit_saves_overwrite_conflicts_and_deleted_facts_stay_deleted(isolated_memory, monkeypatch) -> None:
    from app.lumen_model import LumenModelClient

    user_memory.remember(1, "salary_expectation", "$120,000")
    monkeypatch.setattr(
        LumenModelClient,
        "reconcile_memory_facts",
        lambda self, *, existing, candidates: {"ok": True, "decisions": [{"candidate_index": 0, "action": "conflict", "key": "salary_expectation", "value": candidates[0]["value"]}]},
    )
    asked = user_memory.remember_all_reconciled(None, 1, [{"key": "salary_expectation", "value": "$150,000"}])
    assert asked[0]["action"] == "conflict" and user_memory.facts(1)[0].value == "$120,000"
    kept = user_memory.remember_all_reconciled(None, 1, [{"key": "salary_expectation", "value": "$150,000"}], overwrite_conflicts=True)
    assert kept[0]["action"] == "update" and user_memory.facts(1)[0].value == "$150,000"

    # The user deletes the fact while the model is deciding: an update must not bring it back.
    def delete_then_update(self, *, existing, candidates):
        user_memory.forget(1, "salary_expectation")
        return {"ok": True, "decisions": [{"candidate_index": 0, "action": "update", "key": "salary_expectation", "value": "$160,000"}]}

    monkeypatch.setattr(LumenModelClient, "reconcile_memory_facts", delete_then_update)
    raced = user_memory.remember_all_reconciled(None, 1, [{"key": "salary_expectation", "value": "$160,000"}])
    assert raced[0]["action"] == "skip" and user_memory.facts(1) == []


def test_values_cannot_break_the_line_format(isolated_memory) -> None:
    fact = user_memory.remember(1, "note", "keep <!-- this --> whole")
    assert fact is not None and user_memory.facts(1)[0].value == fact.value == "keep this whole"


def test_memory_routes_add_edit_move_and_delete(isolated_memory) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    added = client.post("/memory", json={"label": "Notice period", "value": "Two weeks", "section": "work"})
    assert added.status_code == 200, added.text
    fact = next(item for item in added.json()["facts"] if item["key"] == "notice_period")
    assert fact == {**fact, "label": "Notice period", "value": "Two weeks", "section": "work", "source": "added by you"}
    assert client.post("/memory", json={"label": "notice period", "value": "x"}).status_code == 409

    edited = client.patch("/memory/notice_period", json={"label": "Notice", "value": "One month", "section": "preferences"})
    assert edited.status_code == 200, edited.text
    keys = {item["key"]: item for item in edited.json()["facts"]}
    assert "notice_period" not in keys
    assert keys["notice"]["value"] == "One month" and keys["notice"]["section"] == "preferences"
    assert keys["notice"]["source"] == "edited by you"

    assert client.delete("/memory/notice").status_code == 200
    assert client.delete("/memory/notice").status_code == 404
    assert [section["key"] for section in client.get("/memory").json()["sections"]] == list(user_memory.SECTIONS)


def test_chat_learns_durable_facts_mentioned_in_passing(isolated_memory) -> None:
    from app import models
    from app.cognition import OperatorChatService
    from app.db import SessionLocal, init_db
    from app.runtime import OperatorRuntimeEngine

    init_db()
    with SessionLocal() as db:
        user = OperatorRuntimeEngine(db).ensure_default_user()
        thread = models.ChatThread(user_id=user.id, title="passing")
        db.add(thread)
        db.flush()
        OperatorChatService(db)._learn_user_facts_in_passing(
            thread,
            chat_scope={
                "action": "start_browser_task",
                "user_facts": [{"key": "location", "value": "Hoboken, NJ", "section": "contact"}],
            },
        )
        stored = {fact.key: fact for fact in user_memory.facts(user.id)}
        assert stored["location"].value == "Hoboken, NJ"
        assert stored["location"].section == "contact"
        assert "you, in chat" in stored["location"].note
        profile = db.query(models.UserProfile).filter(models.UserProfile.user_id == user.id).first()
        assert profile.location == "Hoboken, NJ"
        db.rollback()


def test_form_filling_reads_profile_memory_as_profile_evidence(isolated_memory) -> None:
    import uuid

    from app import models
    from app.candidate_context import CandidateContextEngine
    from app.db import SessionLocal, init_db

    init_db()
    with SessionLocal() as db:
        user = models.User(email=f"evidence-{uuid.uuid4().hex}@example.com", name="Evidence")
        db.add(user)
        db.flush()
        db.add(models.UserProfile(user_id=user.id, profile_json={}))
        db.flush()
        user_memory.remember(user.id, "notice_period", "Two weeks", section="work")
        context = CandidateContextEngine(db).compact_defaults_with_provenance(user.id)
        assert {"key": "notice_period", "value": "Two weeks", "source": "user_memory.notice_period"} in context["values"]["profile_memory"]
        db.rollback()


def test_model_learned_standard_facts_land_in_their_own_section() -> None:
    from app.lumen_model import LumenModelClient

    facts = LumenModelClient()._memory_fact_list(
        [
            {"key": "location", "value": "Hoboken, NJ", "section": "preferences"},
            {"key": "notice_period", "value": "Two weeks", "section": "work"},
            {"key": "", "value": "dropped"},
        ],
        limit=5,
    )
    assert facts == [
        {"key": "location", "value": "Hoboken, NJ", "section": "contact"},
        {"key": "notice_period", "value": "Two weeks", "section": "work"},
    ]
