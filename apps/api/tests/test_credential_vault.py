from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select, text

from app import credential_vault, models
from app.db import SessionLocal, init_db
from app.main import app
from app.schemas import CredentialVaultCreate


TEST_ENCRYPTION_KEY = "unit-test-credential-vault-encryption-key"


@pytest.fixture(autouse=True)
def _configured_vault_key(monkeypatch):
    monkeypatch.setattr(
        credential_vault,
        "get_settings",
        lambda: SimpleNamespace(token_encryption_key=TEST_ENCRYPTION_KEY),
    )


def _user(db, prefix: str) -> models.User:
    user = models.User(email=f"{prefix}-{uuid4().hex}@example.test", name=f"{prefix} user")
    db.add(user)
    db.flush()
    return user


def test_normalize_origin_is_exact_and_rejects_unsafe_origins() -> None:
    assert credential_vault.normalize_origin("https://TowerLoan.WD12.myworkdayjobs.com:443/") == (
        "https://towerloan.wd12.myworkdayjobs.com"
    )
    assert credential_vault.normalize_origin("https://example.test:8443") == "https://example.test:8443"
    assert credential_vault.normalize_origin("http://localhost:3000/") == "http://localhost:3000"
    assert credential_vault.origin_from_url(
        "https://towerloan.wd12.myworkdayjobs.com/job/123?jr_id=abc#application"
    ) == "https://towerloan.wd12.myworkdayjobs.com"

    rejected = (
        "http://example.test",
        "ftp://example.test",
        "https://username:password@example.test",
        "https://example.test?next=other",
        "https://example.test#fragment",
        "https://example.test/account",
        "https://example.test\\@evil.test",
    )
    for value in rejected:
        with pytest.raises(credential_vault.CredentialVaultError):
            credential_vault.normalize_origin(value)


def test_service_encrypts_at_rest_and_enforces_tenant_origin_and_slot_boundaries() -> None:
    init_db()
    unique = uuid4().hex
    origin = f"https://vault-{unique}.example.test"
    other_origin = f"https://other-{unique}.example.test"
    username = f"vault-user-{unique}"
    email = f"vault-{unique}@mail.example"
    password = f"vault-password-{unique}"

    with SessionLocal() as db:
        owner = _user(db, "vault-owner")
        other_user = _user(db, "vault-other")
        item = credential_vault.create(
            db,
            user_id=owner.id,
            origin=origin,
            account_label="Primary account",
            fields={"username": username, "email": email, "password": password},
            auto_use=True,
        )
        db.commit()
        item_id = item.id

        raw_row = db.execute(
            text("SELECT * FROM credential_vault_items WHERE id = :item_id"),
            {"item_id": item_id},
        ).one()
        serialized_row = repr(tuple(raw_row))
        assert username not in serialized_row
        assert email not in serialized_row
        assert password not in serialized_row
        assert item.encrypted_payload_json.startswith("gAAAA")
        assert item.available_slots_json == ["username", "email", "password"]

        metadata = credential_vault.metadata_for_item(item)
        assert metadata["available_slots"] == ["username", "email", "password"]
        assert "fields" not in metadata
        assert "encrypted_payload_json" not in metadata
        assert username not in repr(metadata)
        assert email not in repr(metadata)
        assert password not in repr(metadata)

        matched = credential_vault.list_metadata_for_url(
            db,
            user_id=owner.id,
            url=f"{origin}/login?return=%2Fjobs#account",
        )
        assert [entry["id"] for entry in matched] == [item_id]
        assert credential_vault.list_metadata_for_url(
            db,
            user_id=owner.id,
            url=f"{other_origin}/login",
        ) == []
        assert credential_vault.list_metadata_for_url(
            db,
            user_id=other_user.id,
            url=f"{origin}/login",
        ) == []

        assert credential_vault.resolve_slot(
            db,
            user_id=owner.id,
            item_id=item_id,
            slot="username",
            url=f"{origin}/login",
        ) == username
        assert credential_vault.resolve_slot(
            db,
            user_id=owner.id,
            item_id=item_id,
            slot="password",
            url=f"{origin}/login",
        ) == password
        with pytest.raises(credential_vault.CredentialVaultNotFoundError):
            credential_vault.resolve_slot(
                db,
                user_id=other_user.id,
                item_id=item_id,
                slot="password",
                url=f"{origin}/login",
            )
        with pytest.raises(credential_vault.CredentialVaultAccessError):
            credential_vault.resolve_slot(
                db,
                user_id=owner.id,
                item_id=item_id,
                slot="password",
                url=f"{other_origin}/login",
            )

        assert item.last_used_at is None
        credential_vault.mark_used(db, user_id=owner.id, item_id=item_id, url=f"{origin}/account")
        db.commit()
        db.refresh(item)
        assert item.last_used_at is not None


def test_api_create_conflict_update_and_delete_never_return_plaintext() -> None:
    init_db()
    unique = uuid4().hex
    origin = f"https://api-vault-{unique}.example.test"
    username = f"api-user-{unique}"
    old_password = f"api-old-password-{unique}"
    new_password = f"api-new-password-{unique}"
    email = f"api-{unique}@mail.example"

    with TestClient(app) as client:
        invalid_secret_marker = f"DO-NOT-ECHO-{unique}"
        invalid = client.post(
            "/credential-vault",
            json={
                "origin": origin,
                "account_label": "Primary",
                "fields": {"password": invalid_secret_marker + ("x" * 2049)},
            },
        )
        assert invalid.status_code == 422
        assert invalid_secret_marker not in invalid.text

        created = client.post(
            "/credential-vault",
            json={
                "origin": f"{origin}:443",
                "account_label": "Primary",
                "fields": {"username": username, "password": old_password},
                "auto_use": True,
            },
        )
        assert created.status_code == 200
        assert created.json()["origin"] == origin
        assert created.json()["available_slots"] == ["username", "password"]
        assert username not in created.text
        assert old_password not in created.text
        item_id = created.json()["id"]

        other_origin_password = f"api-other-origin-password-{unique}"
        other_origin_created = client.post(
            "/credential-vault",
            json={
                "origin": f"https://api-other-vault-{unique}.example.test",
                "account_label": "Other site",
                "fields": {"password": other_origin_password},
                "auto_use": True,
            },
        )
        assert other_origin_created.status_code == 200

        exact_origin_items = client.get(
            "/credential-vault",
            params={"origin": f"{origin}:443"},
        )
        assert exact_origin_items.status_code == 200
        assert [entry["id"] for entry in exact_origin_items.json()["items"]] == [item_id]
        assert username not in exact_origin_items.text
        assert old_password not in exact_origin_items.text
        assert other_origin_password not in exact_origin_items.text

        invalid_origin_filter = client.get(
            "/credential-vault",
            params={"origin": f"{origin}/login"},
        )
        assert invalid_origin_filter.status_code == 400

        conflicting_password = f"api-conflicting-password-{unique}"
        conflict = client.post(
            "/credential-vault",
            json={
                "origin": origin,
                "account_label": "Primary",
                "fields": {"email": email, "password": conflicting_password},
                "auto_use": True,
            },
        )
        assert conflict.status_code == 409
        assert "already exist" in conflict.json()["detail"]
        assert email not in conflict.text
        assert conflicting_password not in conflict.text

        with SessionLocal() as db:
            persisted = db.scalars(
                select(models.CredentialVaultItem).where(
                    models.CredentialVaultItem.id == item_id,
                )
            ).one()
            assert persisted.available_slots_json == ["username", "password"]
            assert credential_vault.resolve_slot(
                db,
                user_id=persisted.user_id,
                item_id=item_id,
                slot="password",
                url=f"{origin}/login",
            ) == old_password
            with pytest.raises(credential_vault.CredentialVaultNotFoundError):
                credential_vault.resolve_slot(
                    db,
                    user_id=persisted.user_id,
                    item_id=item_id,
                    slot="email",
                    url=f"{origin}/login",
                )

        explicitly_updated = client.patch(
            f"/credential-vault/{item_id}",
            json={
                "fields": {"email": email, "password": new_password},
                "auto_use": True,
            },
        )
        assert explicitly_updated.status_code == 200
        assert explicitly_updated.json()["id"] == item_id
        assert explicitly_updated.json()["available_slots"] == ["username", "email", "password"]
        assert email not in explicitly_updated.text
        assert new_password not in explicitly_updated.text

        listed = client.get("/credential-vault")
        assert listed.status_code == 200
        matching = [entry for entry in listed.json()["items"] if entry["id"] == item_id]
        assert len(matching) == 1
        assert username not in listed.text
        assert email not in listed.text
        assert old_password not in listed.text
        assert new_password not in listed.text

        updated = client.patch(
            f"/credential-vault/{item_id}",
            json={
                "account_label": "Updated account",
                "fields": {"username": None},
                "auto_use": False,
            },
        )
        assert updated.status_code == 200
        assert updated.json()["account_label"] == "Updated account"
        assert updated.json()["available_slots"] == ["email", "password"]
        assert updated.json()["auto_use"] is False
        assert username not in updated.text
        assert email not in updated.text
        assert new_password not in updated.text

        deleted = client.delete(f"/credential-vault/{item_id}")
        assert deleted.status_code == 200
        assert deleted.json() == {"deleted": True, "id": item_id}
        assert all(entry["id"] != item_id for entry in client.get("/credential-vault").json()["items"])


def test_schema_forbids_extra_slots_and_bounds_secret_lengths() -> None:
    valid = {
        "origin": "https://schema.example.test",
        "account_label": "Primary",
        "fields": {"username": "candidate"},
    }
    with pytest.raises(ValidationError):
        CredentialVaultCreate.model_validate({**valid, "unexpected": True})
    with pytest.raises(ValidationError):
        CredentialVaultCreate.model_validate(
            {
                **valid,
                "fields": {"username": "candidate", "api_token": "not-supported"},
            }
        )
    with pytest.raises(ValidationError):
        CredentialVaultCreate.model_validate(
            {
                **valid,
                "fields": {"password": "x" * 2049},
            }
        )


def test_missing_encryption_key_fails_closed_without_persisting_or_echoing_secrets(monkeypatch) -> None:
    init_db()
    monkeypatch.setattr(
        credential_vault,
        "get_settings",
        lambda: SimpleNamespace(token_encryption_key=None),
    )
    unique = uuid4().hex
    origin = f"https://missing-key-{unique}.example.test"
    password = f"missing-key-password-{unique}"

    with SessionLocal() as db:
        user = _user(db, "missing-key")
        user_id = user.id
        with pytest.raises(credential_vault.CredentialVaultConfigurationError):
            credential_vault.create(
                db,
                user_id=user_id,
                origin=origin,
                account_label="Primary",
                fields={"password": password},
            )
        assert db.scalars(
            select(models.CredentialVaultItem).where(
                models.CredentialVaultItem.user_id == user_id,
                models.CredentialVaultItem.origin == origin,
            )
        ).first() is None

    with TestClient(app) as client:
        response = client.post(
            "/credential-vault",
            json={
                "origin": origin,
                "account_label": "Primary",
                "fields": {"password": password},
                "auto_use": True,
            },
        )
        assert response.status_code == 503
        assert "LUMEN_TOKEN_ENCRYPTION_KEY" in response.text
        assert password not in response.text

    with SessionLocal() as db:
        assert db.scalars(
            select(models.CredentialVaultItem).where(models.CredentialVaultItem.origin == origin)
        ).first() is None
