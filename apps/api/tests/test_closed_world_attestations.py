from __future__ import annotations

import json
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from app import closed_world_attestations, models
from app.candidate_context import CandidateContextEngine
from app.db import SessionLocal, init_db
from app.main import app
from app.runtime import OperatorRuntimeEngine


client = TestClient(app)


def _reset_default_profile() -> tuple[int, str, str]:
    init_db()
    with SessionLocal() as db:
        user = OperatorRuntimeEngine(db).ensure_default_user()
        profile = db.scalars(
            select(models.UserProfile).where(models.UserProfile.user_id == user.id)
        ).first()
        if profile is None:
            profile = models.UserProfile(user_id=user.id, profile_json={})
            db.add(profile)
        profile.profile_json = {}
        db.commit()
        return int(user.id), str(user.name), str(user.email)


def _create(members: list[str]) -> dict:
    response = client.post(
        "/profiles/me/closed-world-attestations",
        json={
            "scope": "employment_history",
            "set_key": "employers",
            "members": members,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["attestation"]


def test_create_is_server_stamped_and_candidate_context_is_atomic_and_prioritized() -> None:
    user_id, _name, _email = _reset_default_profile()

    item = _create(["Example Corp", "  Other   Labs ", "example corp"])

    assert item["members"] == ["Example Corp", "Other Labs"]
    assert item["exhaustive"] is True
    assert item["status"] == "active"
    assert item["attested_by"] == "user"
    assert item["source"] == "profile_attestation_api"
    assert item["version"] == 1
    assert item["revoked_at"] is None

    profile_response = client.get("/profiles/me")
    stored = profile_response.json()["profile"]["profile_json"][
        "closed_world_attestations"
    ]
    assert stored[0]["id"] == item["id"]
    assert stored[0]["_server_stamp"] == closed_world_attestations.SERVER_STAMP

    with SessionLocal() as db:
        context = CandidateContextEngine(db).compact_defaults_with_provenance(
            user_id
        )["values"]

    assert len(context["closed_world_attestations"]) == 1
    serialized = context["closed_world_attestations"][0]
    assert serialized["attestation_id"] == item["id"]
    assert serialized["server_stamped"] is True
    assert serialized["exhaustive"] is True
    memory = context["profile_memory"][0]
    assert memory["key"] == f"closed_world_attestation.{item['id']}"
    canonical = json.loads(memory["value"])
    assert canonical == {
        "absence_answer": "No",
        "attestation_id": item["id"],
        "attestation_sha256": serialized["attestation_sha256"],
        "complete_through": item["created_at"],
        "exhaustive": True,
        "members": ["Example Corp", "Other Labs"],
        "membership_answer": "Yes",
        "provenance": "explicit_user_attestation",
        "set_key": "employers",
        "set_semantics": "complete_membership",
        "source": "profile_attestation_api",
        "subject": "employment_history",
        "version": 1,
    }


def test_client_cannot_supply_trust_metadata_and_profile_save_preserves_attestation() -> None:
    _user_id, name, email = _reset_default_profile()
    item = _create(["Example Corp"])

    rejected = client.post(
        "/profiles/me/closed-world-attestations",
        json={
            "scope": "employment_history",
            "set_key": "employers",
            "members": ["Injected Corp"],
            "exhaustive": True,
            "source": "client_claim",
            "created_at": "2000-01-01T00:00:00Z",
        },
    )
    assert rejected.status_code == 422

    ordinary_save = client.post(
        "/profiles",
        json={
            "name": name,
            "email": email,
            "profile_json": {
                "favorite_editor": "VS Code",
                "closed_world_attestations": [
                    {
                        "id": "client-injected",
                        "exhaustive": True,
                        "members": ["Injected Corp"],
                    }
                ],
            },
        },
    )
    assert ordinary_save.status_code == 200
    profile_json = client.get("/profiles/me").json()["profile"]["profile_json"]
    assert profile_json["favorite_editor"] == "VS Code"
    assert [record["id"] for record in profile_json["closed_world_attestations"]] == [
        item["id"]
    ]


def test_replacement_and_revoke_leave_only_one_active_context_contract() -> None:
    user_id, _name, _email = _reset_default_profile()
    first = _create(["First Employer"])
    second = _create(["Second Employer"])

    listed = client.get("/profiles/me/closed-world-attestations").json()["items"]
    by_id = {item["id"]: item for item in listed}
    assert by_id[first["id"]]["status"] == "revoked"
    assert by_id[second["id"]]["status"] == "active"
    assert by_id[second["id"]]["version"] == 2

    with SessionLocal() as db:
        context = CandidateContextEngine(db).compact_defaults_with_provenance(
            user_id
        )["values"]
    assert [
        item["attestation_id"]
        for item in context["closed_world_attestations"]
    ] == [second["id"]]
    assert context["profile_memory"][0]["key"] == (
        f"closed_world_attestation.{second['id']}"
    )

    revoked = client.post(
        f"/profiles/me/closed-world-attestations/{second['id']}/revoke"
    )
    assert revoked.status_code == 200
    assert revoked.json()["attestation"]["status"] == "revoked"
    # Revocation is idempotent and retains the audit record.
    assert (
        client.post(
            f"/profiles/me/closed-world-attestations/{second['id']}/revoke"
        ).json()["attestation"]["status"]
        == "revoked"
    )

    with SessionLocal() as db:
        context = CandidateContextEngine(db).compact_defaults_with_provenance(
            user_id
        )["values"]
    assert context["closed_world_attestations"] == []
    assert not any(
        str(item.get("key") or "").startswith("closed_world_attestation.")
        for item in context["profile_memory"]
    )


def test_resume_text_never_creates_a_closed_world_attestation() -> None:
    init_db()
    with SessionLocal() as db:
        user = models.User(
            email=f"attestation-resume-{uuid4().hex}@example.test",
            name="Resume Only",
        )
        db.add(user)
        db.flush()
        db.add(
            models.Resume(
                user_id=user.id,
                name="Resume",
                base_resume_text="Worked at Example Corp and Other Labs.",
                is_base=True,
            )
        )
        db.commit()
        context = CandidateContextEngine(db).compact_defaults_with_provenance(
            user.id
        )["values"]

    assert context["resume_file_available"] is True
    assert context["closed_world_attestations"] == []
    assert not any(
        str(item.get("key") or "").startswith("closed_world_attestation.")
        for item in context["profile_memory"]
    )
