"""Agent capability settings: no strategy/variant toggles are surfaced, and the three model seats (Brain,
Checker, Escalation) keep their invariants."""

import uuid

import pytest

from app import models
from app.agent_settings import DEFAULTS, get_agent_settings, update_agent_settings
from app.db import SessionLocal, init_db
from app.runtime import OperatorRuntimeEngine


def _user_id(db) -> int:
    return OperatorRuntimeEngine(db).ensure_default_user().id


def test_defaults_only_surface_capability_settings() -> None:
    init_db()
    with SessionLocal() as db:
        settings = get_agent_settings(db, _user_id(db))
        assert settings == {
            "vision_provider_id": None,
            "vision_escalation_provider_id": None,
            "judge_provider_id": None,
            "strict_model_provider_allowlist": False,
            "final_action_mode": "approval",
            "autonomous_attestation_consent": False,
            "autonomous_relay_consent": False,
            "totp_capability": False,
            "run_start_prewarm": True,
            "chrome_profile_directory": None,
            "max_run_seconds": None,
            "max_run_tokens": None,
            "max_run_cost_usd": None,
        }
        assert set(settings) == set(DEFAULTS)


def _seat_user(db) -> int:
    user = models.User(email=f"seats-{uuid.uuid4().hex[:8]}@example.com", name="Seat Test")
    db.add(user)
    db.flush()
    return user.id


def _row(db, *, model_name: str, provider_type: str = "openrouter", keyed: bool = True, base_url: str | None = None) -> models.ModelProvider:
    row = models.ModelProvider(
        display_name=f"Seat test {uuid.uuid4().hex[:8]}",
        provider_type=provider_type,
        model_name=model_name,
        base_url=base_url or "https://provider.example.test/v1",
        encrypted_api_key="sealed" if keyed else None,
        enabled=True,
        is_active=False,
    )
    db.add(row)
    db.flush()
    return row


def test_setting_the_brain_activates_its_row_and_round_trips() -> None:
    init_db()
    with SessionLocal() as db:
        uid = _seat_user(db)
        first = _row(db, model_name="seat/brain-one")
        second = _row(db, model_name="seat/brain-two")
        updated = update_agent_settings(db, uid, {"vision_provider_id": first.id})
        assert updated["vision_provider_id"] == first.id
        assert first.is_active and not second.is_active
        update_agent_settings(db, uid, {"vision_provider_id": second.id})
        assert second.is_active and not first.is_active
        assert get_agent_settings(db, uid)["vision_provider_id"] == second.id
        # Clearing the Brain leaves the active row alone.
        assert update_agent_settings(db, uid, {"vision_provider_id": None})["vision_provider_id"] is None
        assert second.is_active
        db.rollback()


def test_a_brain_row_that_cannot_be_active_is_refused_and_nothing_changes() -> None:
    init_db()
    with SessionLocal() as db:
        uid = _seat_user(db)
        brain = _row(db, model_name="seat/brain")
        keyless = _row(db, model_name="seat/keyless", keyed=False)
        update_agent_settings(db, uid, {"vision_provider_id": brain.id})
        with pytest.raises(ValueError, match="without an API key"):
            update_agent_settings(db, uid, {"vision_provider_id": keyless.id, "judge_provider_id": brain.id})
        with pytest.raises(ValueError):
            update_agent_settings(db, uid, {"vision_provider_id": 987654})
        settings = get_agent_settings(db, uid)
        assert settings["vision_provider_id"] == brain.id and settings["judge_provider_id"] is None
        assert brain.is_active and not keyless.is_active
        db.rollback()


def test_judge_provider_update_persists_and_round_trips() -> None:
    init_db()
    with SessionLocal() as db:
        uid = _user_id(db)
        updated = update_agent_settings(db, uid, {"judge_provider_id": 7})
        db.commit()
        assert updated["judge_provider_id"] == 7
    with SessionLocal() as db2:
        assert get_agent_settings(db2, _user_id(db2))["judge_provider_id"] == 7
    with SessionLocal() as db3:
        cleared = update_agent_settings(db3, _user_id(db3), {"judge_provider_id": None})
        db3.commit()
        assert cleared["judge_provider_id"] is None


def test_the_checker_is_cleared_when_it_is_the_brain_or_serves_the_brains_model() -> None:
    init_db()
    with SessionLocal() as db:
        uid = _seat_user(db)
        brain = _row(db, model_name="seat/brain")
        checker = _row(db, model_name="seat/checker")
        twin = _row(db, model_name="SEAT/Brain", base_url="https://provider.example.test/v1/")
        settings = update_agent_settings(db, uid, {"vision_provider_id": brain.id, "judge_provider_id": checker.id})
        assert (settings["vision_provider_id"], settings["judge_provider_id"]) == (brain.id, checker.id)
        # The same row.
        assert update_agent_settings(db, uid, {"judge_provider_id": brain.id})["judge_provider_id"] is None
        # Another row serving the Brain's model at the same endpoint is the same model.
        assert update_agent_settings(db, uid, {"judge_provider_id": twin.id})["judge_provider_id"] is None
        # Moving the Brain onto the Checker's row clears the Checker: the Brain wins.
        update_agent_settings(db, uid, {"judge_provider_id": checker.id})
        settings = update_agent_settings(db, uid, {"vision_provider_id": checker.id})
        assert settings["vision_provider_id"] == checker.id and settings["judge_provider_id"] is None
        assert checker.is_active
        db.rollback()


def test_escalation_is_cleared_when_it_is_the_brain_or_the_checker() -> None:
    init_db()
    with SessionLocal() as db:
        uid = _seat_user(db)
        brain = _row(db, model_name="seat/brain")
        checker = _row(db, model_name="seat/checker")
        smart = _row(db, model_name="seat/smart")
        update_agent_settings(db, uid, {"vision_provider_id": brain.id, "judge_provider_id": checker.id})
        assert update_agent_settings(db, uid, {"vision_escalation_provider_id": brain.id})["vision_escalation_provider_id"] is None
        assert update_agent_settings(db, uid, {"vision_escalation_provider_id": checker.id})["vision_escalation_provider_id"] is None
        assert update_agent_settings(db, uid, {"vision_escalation_provider_id": smart.id})["vision_escalation_provider_id"] == smart.id
        # Making the escalation row the Checker takes it out of the escalation seat.
        settings = update_agent_settings(db, uid, {"judge_provider_id": smart.id})
        assert settings["judge_provider_id"] == smart.id and settings["vision_escalation_provider_id"] is None
        db.rollback()


def test_legacy_seats_fill_a_missing_checker_and_are_dropped_on_the_next_update() -> None:
    init_db()
    with SessionLocal() as db:
        uid = _seat_user(db)
        update_agent_settings(db, uid, {"final_action_mode": "approval"})
        profile = db.query(models.UserProfile).filter_by(user_id=uid).one()

        def stored(value: dict) -> dict:
            profile.agent_settings_json = value
            db.flush()
            return get_agent_settings(db, uid)

        legacy = {"vision_provider_id": 14, "grounding_judge_provider_id": 15, "secret_adjudicator_provider_id": 11}
        settings = stored(legacy)
        assert settings["judge_provider_id"] == 11
        assert not {"grounding_judge_provider_id", "secret_adjudicator_provider_id", "vision_backup_provider_id"} & set(settings)
        assert stored({"grounding_judge_provider_id": 15})["judge_provider_id"] == 15
        # A legacy seat that is the Brain's row never becomes the Checker.
        assert stored({"vision_provider_id": 14, "secret_adjudicator_provider_id": 14, "grounding_judge_provider_id": 15})["judge_provider_id"] == 15
        # A Checker already set wins over every legacy seat.
        assert stored({"judge_provider_id": 9, "secret_adjudicator_provider_id": 11})["judge_provider_id"] == 9
        # The next write keeps the adopted Checker and drops the legacy keys.
        stored({**legacy, "vision_backup_provider_id": 16})
        update_agent_settings(db, uid, {"final_action_mode": "autonomous"})
        assert profile.agent_settings_json["judge_provider_id"] == 11
        assert not {"grounding_judge_provider_id", "secret_adjudicator_provider_id", "vision_backup_provider_id"} & set(
            profile.agent_settings_json
        )
        db.rollback()


def test_the_schema_drops_the_retired_seats() -> None:
    from app.schemas import AgentSettingsUpdate

    payload = AgentSettingsUpdate(
        judge_provider_id=21,
        grounding_judge_provider_id=22,
        secret_adjudicator_provider_id=23,
        vision_backup_provider_id=24,
    ).model_dump(exclude_unset=True)
    assert payload == {"judge_provider_id": 21}


def test_final_action_mode_update_persists_both_supported_modes() -> None:
    init_db()
    with SessionLocal() as db:
        uid = _user_id(db)
        updated = update_agent_settings(db, uid, {"final_action_mode": "autonomous"})
        db.commit()
        assert updated["final_action_mode"] == "autonomous"
    with SessionLocal() as db:
        assert get_agent_settings(db, _user_id(db))["final_action_mode"] == "autonomous"
    with SessionLocal() as db:
        uid = _user_id(db)
        unchanged = update_agent_settings(db, uid, {"final_action_mode": "launch_the_rocket"})
        db.commit()
        assert unchanged["final_action_mode"] == "autonomous"
    with SessionLocal() as db:
        uid = _user_id(db)
        updated = update_agent_settings(db, uid, {"final_action_mode": "approval"})
        db.commit()
        assert updated["final_action_mode"] == "approval"
    with SessionLocal() as db2:
        assert get_agent_settings(db2, _user_id(db2))["final_action_mode"] == "approval"


def test_retired_strategy_keys_are_ignored() -> None:
    init_db()
    with SessionLocal() as db:
        uid = _user_id(db)
        update_agent_settings(
            db,
            uid,
            {
                "vision_provider_id": None,
                "final_action_mode": "approval",
            },
        )
        result = update_agent_settings(db, uid, {"retired_strategy_toggle": True, "nonsense": "x"})
        db.commit()
        assert result == {
            "vision_provider_id": None,
            "vision_escalation_provider_id": None,
            "judge_provider_id": None,
            "strict_model_provider_allowlist": False,
            "final_action_mode": "approval",
            "autonomous_attestation_consent": False,
            "autonomous_relay_consent": False,
            "totp_capability": False,
            "run_start_prewarm": True,
            "chrome_profile_directory": None,
            "max_run_seconds": None,
            "max_run_tokens": None,
            "max_run_cost_usd": None,
        }
    with SessionLocal() as db:
        assert get_agent_settings(db, _user_id(db)) == {
            "vision_provider_id": None,
            "vision_escalation_provider_id": None,
            "judge_provider_id": None,
            "strict_model_provider_allowlist": False,
            "final_action_mode": "approval",
            "autonomous_attestation_consent": False,
            "autonomous_relay_consent": False,
            "totp_capability": False,
            "run_start_prewarm": True,
            "chrome_profile_directory": None,
            "max_run_seconds": None,
            "max_run_tokens": None,
            "max_run_cost_usd": None,
        }


_DEFAULT_SETTINGS = {
    "vision_provider_id": None,
    "vision_escalation_provider_id": None,
    "judge_provider_id": None,
    "strict_model_provider_allowlist": False,
    "final_action_mode": "approval",
    "autonomous_attestation_consent": False,
    "autonomous_relay_consent": False,
    "totp_capability": False,
    "run_start_prewarm": True,
    "chrome_profile_directory": None,
    "max_run_seconds": None,
    "max_run_tokens": None,
    "max_run_cost_usd": None,
}


def test_endpoints_get_and_patch() -> None:
    init_db()
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        client.patch(
            "/agent-settings",
            json={
                "vision_provider_id": None,
                "judge_provider_id": None,
                "vision_escalation_provider_id": None,
                "strict_model_provider_allowlist": False,
                "final_action_mode": "approval",
            },
        )
        assert client.get("/agent-settings").json()["settings"] == _DEFAULT_SETTINGS
        patched = client.patch(
            "/agent-settings",
            json={
                "judge_provider_id": 8,
                "vision_backup_provider_id": 9,
                "strict_model_provider_allowlist": True,
                "final_action_mode": "autonomous",
                "retired_strategy_toggle": True,
            },
        )
        expected = {**_DEFAULT_SETTINGS, "judge_provider_id": 8, "strict_model_provider_allowlist": True, "final_action_mode": "autonomous"}
        assert patched.json()["settings"] == expected
        assert client.get("/agent-settings").json()["settings"] == expected
        # A Brain row that cannot be activated is refused, and nothing in the request is stored.
        refused = client.patch("/agent-settings", json={"vision_provider_id": 987654, "final_action_mode": "approval"})
        assert refused.status_code == 400
        assert client.get("/agent-settings").json()["settings"] == expected
        client.patch(
            "/agent-settings",
            json={"judge_provider_id": None, "strict_model_provider_allowlist": False, "final_action_mode": "approval"},
        )


def test_the_brain_and_the_active_row_stay_one_choice_through_both_endpoints() -> None:
    init_db()
    from fastapi.testclient import TestClient

    from app.main import app

    with SessionLocal() as db:
        uid = _user_id(db)
        before_active = [row.id for row in db.query(models.ModelProvider).filter_by(is_active=True).all()]
        before_settings = dict(_profile_settings(db, uid))
        image = _row(db, model_name="seat/endpoint-image")
        other = _row(db, model_name="seat/endpoint-other")
        text_only = _row(db, model_name="seat/endpoint-text", provider_type="deepseek", base_url="https://api.deepseek.com")
        created = [image.id, other.id, text_only.id]
        image_id, other_id, text_only_id = created
        db.commit()
    try:
        with TestClient(app) as client:
            # Activating a row that can carry a screenshot makes it the Brain.
            assert client.post(f"/model-providers/{image_id}/activate").status_code == 200
            assert client.get("/agent-settings").json()["settings"]["vision_provider_id"] == image_id
            # Setting the Brain activates its row.
            assert client.patch("/agent-settings", json={"vision_provider_id": other_id}).status_code == 200
            with SessionLocal() as db:
                active = [row.id for row in db.query(models.ModelProvider).filter_by(is_active=True).all()]
            assert active == [other_id]
            # A text-only row can still be the active (text) row, but it never becomes the Brain.
            assert client.post(f"/model-providers/{text_only_id}/activate").status_code == 200
            assert client.get("/agent-settings").json()["settings"]["vision_provider_id"] == other_id
    finally:
        with SessionLocal() as db:
            for row in db.query(models.ModelProvider).filter(models.ModelProvider.id.in_(created)).all():
                db.delete(row)
            for row in db.query(models.ModelProvider).filter(models.ModelProvider.id.in_(before_active)).all():
                row.is_active = True
            profile = db.query(models.UserProfile).filter_by(user_id=uid).order_by(models.UserProfile.id.desc()).first()
            if profile is not None:
                profile.agent_settings_json = before_settings
            db.commit()


def _profile_settings(db, user_id: int) -> dict:
    profile = db.query(models.UserProfile).filter_by(user_id=user_id).order_by(models.UserProfile.id.desc()).first()
    return dict(profile.agent_settings_json or {}) if profile is not None else {}


def test_attestation_consent_reaches_runs_only_under_autonomous_authority(monkeypatch) -> None:
    from app import extension_run

    init_db()
    with SessionLocal() as db:
        uid = _user_id(db)
        update_agent_settings(db, uid, {"autonomous_attestation_consent": True})
        db.commit()
    try:
        granted = extension_run._standing_user_authorizations(uid, "autonomous")
        assert len(granted) == 1 and granted[0]["source"] == "standing_user_authorization"
        assert "read/agree attestations" in granted[0]["answer"]
        assert extension_run._standing_user_authorizations(uid, "approval") == []
    finally:
        with SessionLocal() as db:
            update_agent_settings(db, uid, {"autonomous_attestation_consent": False})
            db.commit()
    assert extension_run._standing_user_authorizations(uid, "autonomous") == []


def test_relay_consent_reaches_runs_only_under_autonomous_authority() -> None:
    from app import extension_run

    init_db()
    with SessionLocal() as db:
        uid = _user_id(db)
        assert extension_run._standing_relay_consent(uid, "autonomous") == ""
        update_agent_settings(db, uid, {"autonomous_relay_consent": True})
        db.commit()
    try:
        granted = extension_run._standing_relay_consent(uid, "autonomous")
        assert "task page and the desktop applications it opens" in granted
        assert "one-time, verification, recovery, and backup codes" in granted and "payment data stay secret" in granted
        assert extension_run._standing_relay_consent(uid, "approval") == ""
    finally:
        with SessionLocal() as db:
            update_agent_settings(db, uid, {"autonomous_relay_consent": False})
            db.commit()
