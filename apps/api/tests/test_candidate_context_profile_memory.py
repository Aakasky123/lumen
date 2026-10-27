import uuid

from app import models, user_memory
from app.candidate_context import CandidateContextEngine
from app.db import SessionLocal, init_db


def test_profile_evidence_comes_from_the_memory_file_only(tmp_path, monkeypatch) -> None:
    """The profile the user sees (the memory file) is what form filling reads; leftover hidden
    profile_json keys from the old saved-answer system are not evidence any more."""
    monkeypatch.setattr(user_memory, "_memory_dir", lambda: tmp_path)
    init_db()
    with SessionLocal() as db:
        user = models.User(email=f"profile-memory-{uuid.uuid4().hex}@example.test", name="Profile Memory User")
        db.add(user)
        db.flush()
        db.add(
            models.UserProfile(
                user_id=user.id,
                profile_json={
                    "stale_hidden_key": "Something the user can't see",
                    "common_answers": [{"profile_key": "old_answer", "profile_value": "An old saved answer"}],
                },
            )
        )
        db.flush()
        user_memory.remember(user.id, "professional_software_engineering_experience", "More than 7 years", section="work")

        memory = CandidateContextEngine(db).compact_defaults_with_provenance(user.id)["values"]["profile_memory"]

        assert {
            "key": "professional_software_engineering_experience",
            "value": "More than 7 years",
            "source": "user_memory.professional_software_engineering_experience",
        } in memory
        assert not any(item.get("key") in {"stale_hidden_key", "old_answer"} for item in memory)
        db.rollback()
