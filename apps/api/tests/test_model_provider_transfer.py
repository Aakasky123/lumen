"""Moving providers between installs: keys re-encrypt under the new install's key, roles follow the rows."""

import pytest

from app import models
from app.agent_settings import get_agent_settings, update_agent_settings
from app.db import SessionLocal, init_db
from app.model_provider_transfer import export_bundle, import_bundle, set_provider_key
from app.runtime import OperatorRuntimeEngine
from app.security import decrypt_secret, encrypt_secret


def _export(passphrase: str = "pass-phrase") -> dict:
    init_db()
    with SessionLocal() as db:
        uid = OperatorRuntimeEngine(db).ensure_default_user().id
        brain = models.ModelProvider(
            display_name="Brain", provider_type="openrouter", model_name="transfer/brain-model",
            base_url="https://openrouter.ai/api/v1", encrypted_api_key=encrypt_secret("sk-brain", "old-key"),
            structured_output="json_object", endpoint_routing='{"allow_fallbacks": false}', is_active=True,
        )
        judge = models.ModelProvider(display_name="Judge", provider_type="anthropic", model_name="transfer/judge-model")
        db.add_all([brain, judge])
        db.flush()
        update_agent_settings(db, uid, {"vision_provider_id": brain.id, "judge_provider_id": judge.id})
        bundle = export_bundle(db, uid, token_key="old-key", passphrase=passphrase)
        db.rollback()
    return bundle


def test_bundle_seals_keys_and_import_rebinds_roles() -> None:
    bundle = _export()
    assert "sk-brain" not in str(bundle)
    with SessionLocal() as db:
        uid = OperatorRuntimeEngine(db).ensure_default_user().id
        import_bundle(db, uid, bundle, token_key="new-key", passphrase="pass-phrase")
        brain = db.query(models.ModelProvider).filter_by(model_name="transfer/brain-model").one()
        judge = db.query(models.ModelProvider).filter_by(model_name="transfer/judge-model").one()
        assert decrypt_secret(brain.encrypted_api_key, "new-key") == "sk-brain"
        assert brain.structured_output == "json_object" and brain.is_active
        assert judge.encrypted_api_key is None
        settings = get_agent_settings(db, uid)
        assert settings["vision_provider_id"] == brain.id
        assert settings["judge_provider_id"] == judge.id
        # A second import updates the same rows instead of duplicating them.
        import_bundle(db, uid, bundle, token_key="new-key", passphrase="pass-phrase")
        assert db.query(models.ModelProvider).filter_by(model_name="transfer/brain-model").count() == 1
        db.rollback()


def test_wrong_passphrase_and_missing_install_key_refuse() -> None:
    bundle = _export()
    with SessionLocal() as db:
        uid = OperatorRuntimeEngine(db).ensure_default_user().id
        with pytest.raises(ValueError, match="Wrong passphrase"):
            import_bundle(db, uid, bundle, token_key="new-key", passphrase="nope")
        with pytest.raises(ValueError, match="LUMEN_TOKEN_ENCRYPTION_KEY"):
            import_bundle(db, uid, bundle, token_key=None, passphrase="pass-phrase")
        db.rollback()


def test_a_keyless_bundle_imports_without_a_passphrase_and_set_key_fills_every_row_of_the_type() -> None:
    bundle = _export()
    bundle.pop("salt")
    for item in bundle["providers"]:
        item["sealed_api_key"] = None
    with SessionLocal() as db:
        uid = OperatorRuntimeEngine(db).ensure_default_user().id
        import_bundle(db, uid, bundle, token_key=None, passphrase="")
        brain = db.query(models.ModelProvider).filter_by(model_name="transfer/brain-model").one()
        judge = db.query(models.ModelProvider).filter_by(model_name="transfer/judge-model").one()
        assert get_agent_settings(db, uid)["vision_provider_id"] == brain.id
        report = set_provider_key(db, token_key="new-key", api_key=" sk-or-one ", provider_type="openrouter")
        assert decrypt_secret(brain.encrypted_api_key, "new-key") == "sk-or-one"
        assert brain.api_key_hint == "...-one" and "sk-or-one" not in "\n".join(report)
        assert judge.encrypted_api_key is None
        with pytest.raises(ValueError, match="LUMEN_TOKEN_ENCRYPTION_KEY"):
            set_provider_key(db, token_key=None, api_key="sk", provider_type="openrouter")
        with pytest.raises(ValueError, match="No gemini providers"):
            set_provider_key(db, token_key="new-key", api_key="sk", provider_type="gemini")
        db.rollback()


def test_an_older_bundle_maps_its_adjudicator_to_the_checker_and_its_brain_becomes_the_active_row() -> None:
    bundle = _export()
    by_model = {item["model_name"]: item["source_id"] for item in bundle["providers"]}
    # An older bundle: the gate adjudicator and the grounding judge were their own seats, and the active
    # (Text) row was not the vision brain.
    bundle["roles"] = {
        "vision_provider_id": by_model["transfer/brain-model"],
        "secret_adjudicator_provider_id": by_model["transfer/judge-model"],
        "grounding_judge_provider_id": by_model["transfer/brain-model"],
        "vision_backup_provider_id": by_model["transfer/judge-model"],
    }
    for item in bundle["providers"]:
        item["is_active"] = item["model_name"] == "transfer/judge-model"
    with SessionLocal() as db:
        uid = OperatorRuntimeEngine(db).ensure_default_user().id
        report = import_bundle(db, uid, bundle, token_key="new-key", passphrase="pass-phrase")
        brain = db.query(models.ModelProvider).filter_by(model_name="transfer/brain-model").one()
        judge = db.query(models.ModelProvider).filter_by(model_name="transfer/judge-model").one()
        settings = get_agent_settings(db, uid)
        assert settings["vision_provider_id"] == brain.id
        assert settings["judge_provider_id"] == judge.id
        assert brain.is_active and not judge.is_active
        assert not [line for line in report if "adjudicator" in line or "grounding" in line or "backup" in line]
        db.rollback()


def test_export_names_only_the_three_seats() -> None:
    bundle = _export()
    assert set(bundle["roles"]) == {"vision_provider_id", "judge_provider_id"}
