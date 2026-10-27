"""Paired-phone remote access: the gateway, pairing, step-up signatures, push triggers."""

import base64
import hashlib
import json
import uuid

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from fastapi.testclient import TestClient

from app import models, push, remote_access
from app.config import get_settings
from app.db import SessionLocal, init_db
from app.main import app


local = TestClient(app, base_url="http://127.0.0.1:8001", headers={"X-Lumen-Local": "1"})
remote = TestClient(remote_access.RemoteGateway(app))


@pytest.fixture(autouse=True)
def _fresh_state():
    init_db()
    remote_access.reset_for_tests()
    push.reset_for_tests()
    yield
    remote_access.reset_for_tests()


def _spki(private_key) -> str:
    der = private_key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return base64.b64encode(der).decode("ascii")


def _pair(private_key=None) -> tuple[str, object, int]:
    private_key = private_key or ec.generate_private_key(ec.SECP256R1())
    code = local.post("/devices/pairing-codes").json()["code"]
    response = remote.post(
        "/devices/pair",
        json={"code": code, "name": "Test phone", "platform": "android", "public_key": _spki(private_key)},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return body["token"], private_key, body["device"]["id"]


def _sign(private_key, message: bytes) -> str:
    if isinstance(private_key, ec.EllipticCurvePrivateKey):
        signature = private_key.sign(message, ec.ECDSA(hashes.SHA256()))
    else:
        signature = private_key.sign(message, padding.PKCS1v15(), hashes.SHA256())
    return base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")


def _step_up_headers(token: str, private_key, method: str, target: str, body: bytes) -> dict[str, str]:
    auth = {"Authorization": f"Bearer {token}"}
    nonce = remote.post("/devices/me/challenges", headers=auth).json()["nonce"]
    message = remote_access.step_up_message(nonce, method, target, hashlib.sha256(body).hexdigest())
    return {**auth, "X-Lumen-Step-Up": f"{nonce}.{_sign(private_key, message)}", "Content-Type": "application/json"}


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/devices"),
        ("POST", "/devices/pairing-codes"),
        ("DELETE", "/devices/1"),
        ("GET", "/devices/remote-status"),
        ("POST", "/model-providers"),
        ("POST", "/model-providers/1/activate"),
        ("POST", "/operator/desktop/control"),
        ("GET", "/operator/extension/observe"),
        ("GET", "/eval/pages/anything"),
        ("GET", "/operator/runs/../devices"),
    ],
)
def test_unlisted_routes_are_denied_remotely_even_with_a_token(method: str, path: str) -> None:
    token, _key, _device = _pair()
    response = remote.request(method, path, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403


def test_listed_routes_need_a_device_token() -> None:
    assert remote.get("/operator/runs").status_code == 401
    assert remote.get("/operator/runs", headers={"Authorization": "Bearer not-a-real-token"}).status_code == 401
    token, _key, _device = _pair()
    assert remote.get("/operator/runs", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_local_listener_is_unchanged() -> None:
    assert local.get("/operator/runs").status_code == 200
    assert local.get("/devices").status_code == 200


def test_pairing_code_works_once() -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    code = local.post("/devices/pairing-codes").json()["code"]
    payload = {"code": code, "name": "Phone", "public_key": _spki(key)}
    assert remote.post("/devices/pair", json=payload).status_code == 200
    assert remote.post("/devices/pair", json=payload).status_code == 400


def test_pairing_rejects_weak_or_foreign_keys() -> None:
    code = local.post("/devices/pairing-codes").json()["code"]
    weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
    response = remote.post("/devices/pair", json={"code": code, "name": "Phone", "public_key": _spki(weak)})
    assert response.status_code == 400
    p384 = ec.generate_private_key(ec.SECP384R1())
    response = remote.post("/devices/pair", json={"code": code, "name": "Phone", "public_key": _spki(p384)})
    assert response.status_code == 400


def test_revoked_device_is_locked_out() -> None:
    token, _key, device_id = _pair()
    auth = {"Authorization": f"Bearer {token}"}
    assert remote.get("/devices/me", headers=auth).status_code == 200
    assert local.delete(f"/devices/{device_id}").status_code == 200
    assert remote.get("/devices/me", headers=auth).status_code == 401


def test_device_can_unpair_itself_and_register_push() -> None:
    token, _key, _device = _pair()
    auth = {"Authorization": f"Bearer {token}"}
    response = remote.post("/devices/me/push-token", json={"push_token": "fcm-token"}, headers=auth)
    assert response.json()["device"]["has_push"] is True
    assert remote.delete("/devices/me", headers=auth).status_code == 200
    assert remote.get("/devices/me", headers=auth).status_code == 401


@pytest.mark.parametrize("key_factory", [
    lambda: ec.generate_private_key(ec.SECP256R1()),
    lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048),
])
def test_step_up_route_needs_a_signature_bound_to_the_request(key_factory) -> None:
    token, key, _device = _pair(key_factory())
    body = json.dumps({"final_action_mode": "approval"}).encode()

    missing = remote.patch("/agent-settings", content=body, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    assert missing.status_code == 403
    assert missing.headers.get("X-Lumen-Step-Up-Required") == "1"

    headers = _step_up_headers(token, key, "PATCH", "/agent-settings", body)
    assert remote.patch("/agent-settings", content=body, headers=headers).status_code == 200
    # The same nonce never works twice.
    assert remote.patch("/agent-settings", content=body, headers=headers).status_code == 401

    # A signature for one body does not authorize another.
    headers = _step_up_headers(token, key, "PATCH", "/agent-settings", body)
    other = json.dumps({"final_action_mode": "autonomous"}).encode()
    assert remote.patch("/agent-settings", content=other, headers=headers).status_code == 401


def test_another_devices_key_cannot_step_up() -> None:
    token, _key, _device = _pair()
    stranger = ec.generate_private_key(ec.SECP256R1())
    body = b'{"final_action_mode":"approval"}'
    headers = _step_up_headers(token, stranger, "PATCH", "/agent-settings", body)
    assert remote.patch("/agent-settings", content=body, headers=headers).status_code == 401


def test_sensitive_browser_continue_is_reachable_with_a_token() -> None:
    token, _key, _device = _pair()
    response = remote.post(
        "/operator/runs/999999/sensitive-browser-continue",
        json={"action": "sensitive_browser_continue", "handoff_id": "h" * 20},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code not in {401, 403}


def test_credential_vault_writes_need_step_up_but_reads_do_not() -> None:
    token, _key, _device = _pair()
    auth = {"Authorization": f"Bearer {token}"}
    assert remote.get("/credential-vault", headers=auth).status_code != 403
    response = remote.post(
        "/credential-vault",
        json={"origin": "https://example.test", "account_label": "me", "fields": {"username": "me"}},
        headers=auth,
    )
    assert response.status_code == 403
    assert response.json()["step_up_required"] is True


def _payment_question() -> int:
    with SessionLocal() as db:
        user = models.User(email=f"remote-payment-{uuid.uuid4().hex}@example.com", name="Remote Payment Test")
        db.add(user)
        db.flush()
        run = models.OperatorRun(
            user_id=user.id,
            goal="Book it",
            target_url="https://example.test/checkout",
            current_state="WAITING_FOR_USER",
            confidence_state={"pending_final_action": {"status": "awaiting_payment_confirmation", "approval_token": "t" * 32}},
        )
        db.add(run)
        db.flush()
        question = models.OperatorQuestion(
            run_id=run.id,
            question_type="final_action_approval",
            prompt="Pay?",
            expected_answer_schema={"final_action": {"payment_floor": True, "approval_token": "t" * 32}},
            status="pending",
        )
        db.add(question)
        db.commit()
        return question.id


def test_remote_payment_approval_needs_step_up_but_decline_does_not() -> None:
    question_id = _payment_question()
    token, key, _device = _pair()
    auth = {"Authorization": f"Bearer {token}"}
    approve = {"answer": "Confirm and submit", "approval_token": "t" * 32, "final_action_decision": "approve"}

    response = remote.post(f"/operator/questions/{question_id}/answer", json=approve, headers=auth)
    assert response.status_code == 403
    assert response.headers.get("X-Lumen-Step-Up-Required") == "1"

    body = json.dumps(approve).encode()
    target = f"/operator/questions/{question_id}/answer"
    response = remote.post(target, content=body, headers=_step_up_headers(token, key, "POST", target, body))
    assert response.status_code != 403

    decline = {"answer": "Leave it for my review", "approval_token": "t" * 32, "final_action_decision": "decline"}
    assert remote.post(f"/operator/questions/{_payment_question()}/answer", json=decline, headers=auth).status_code != 403


def test_latest_screenshot_serves_only_files_inside_the_run(tmp_path) -> None:
    with SessionLocal() as db:
        user = models.User(email=f"remote-shot-{uuid.uuid4().hex}@example.com", name="Remote Shot Test")
        db.add(user)
        db.flush()
        run = models.OperatorRun(user_id=user.id, goal="Look", target_url="https://example.test", current_state="RUNNING")
        db.add(run)
        db.commit()
        run_id = run.id
    run_dir = get_settings().artifact_root / "runs" / str(run_id) / "vision-trajectory"
    run_dir.mkdir(parents=True, exist_ok=True)
    inside = run_dir / "a-model.jpg"
    inside.write_bytes(b"\xff\xd8inside")
    outside = tmp_path / "secret.jpg"
    outside.write_bytes(b"\xff\xd8outside")
    manifest = run_dir / "model-input-manifest.jsonl"
    manifest.write_text(
        json.dumps({"step": 3, "model_screenshot_path": str(inside)}) + "\n"
        + json.dumps({"step": 4, "model_screenshot_path": str(outside)}) + "\n",
        encoding="utf-8",
    )
    token, _key, _device = _pair()
    response = remote.get(f"/operator/runs/{run_id}/screenshot/latest", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.content == b"\xff\xd8inside"
    assert response.headers["X-Lumen-Step"] == "3"


def test_push_alerts_follow_commits_and_carry_no_run_content(monkeypatch) -> None:
    sent: list[dict] = []
    monkeypatch.setattr(push, "deliver", sent.append)
    monkeypatch.setattr(push, "push_status", lambda: {"configured": True, "project_id": "test"})
    monkeypatch.setattr(push, "_enqueue", sent.append)
    push.install()

    with SessionLocal() as db:
        user = models.User(email=f"remote-push-{uuid.uuid4().hex}@example.com", name="Remote Push Test")
        db.add(user)
        db.flush()
        run = models.OperatorRun(user_id=user.id, goal="Secret goal text", target_url="https://example.test", current_state="RUNNING")
        db.add(run)
        db.flush()
        db.add(models.OperatorQuestion(run_id=run.id, question_type="field_approval", prompt="What is your SSN?", status="pending"))
        db.rollback()
    assert sent == []

    with SessionLocal() as db:
        user = models.User(email=f"remote-push-{uuid.uuid4().hex}@example.com", name="Remote Push Test")
        db.add(user)
        db.flush()
        run = models.OperatorRun(user_id=user.id, goal="Secret goal text", target_url="https://example.test", current_state="RUNNING")
        db.add(run)
        db.flush()
        db.add(models.OperatorQuestion(run_id=run.id, question_type="field_approval", prompt="What is your SSN?", status="pending"))
        db.commit()
        run.current_state = "READY_FOR_REVIEW"
        db.commit()
        run.confidence_state = {"sensitive_browser_takeover": {"active": True}}
        db.commit()
        run_id = run.id

    kinds = [alert["data"]["kind"] for alert in sent]
    assert kinds == ["question", "run_finished", "sensitive_entry"]
    assert all(alert["data"]["run_id"] == run_id for alert in sent)
    serialized = json.dumps(sent)
    assert "Secret goal text" not in serialized and "SSN" not in serialized


def test_device_admin_routes_need_loopback_host_and_local_header() -> None:
    bare = TestClient(app, base_url="http://127.0.0.1:8001")
    assert bare.post("/devices/pairing-codes").status_code == 403
    rebound = TestClient(app, base_url="http://attacker.example:8001", headers={"X-Lumen-Local": "1"})
    assert rebound.post("/devices/pairing-codes").status_code == 403
    assert rebound.get("/devices").status_code == 403
    assert local.post("/devices/pairing-codes").status_code == 200


def test_closing_bench_tabs_needs_loopback_host_and_local_header(monkeypatch) -> None:
    from app.extension_hub import hub

    monkeypatch.setattr(hub, "get", lambda device_id=None: None)
    bare = TestClient(app, base_url="http://127.0.0.1:8001")
    assert bare.post("/operator/extension/close-bench-tabs").status_code == 403
    rebound = TestClient(app, base_url="http://attacker.example:8001", headers={"X-Lumen-Local": "1"})
    assert rebound.post("/operator/extension/close-bench-tabs").status_code == 403
    # Past the guard: with no extension connected the route answers 409, not 403.
    assert local.post("/operator/extension/close-bench-tabs").status_code == 409


def test_unauthenticated_bodies_are_not_buffered() -> None:
    big = b"x" * (70 * 1024)
    assert remote.post("/devices/pair", content=big, headers={"Content-Type": "application/json"}).status_code == 413
    # A device route refuses on the missing token before reading the body at all.
    assert remote.post("/devices/me/push-token", content=b"x" * (2 * 1024 * 1024)).status_code == 401


def test_unknown_payment_verdict_counts_as_payment() -> None:
    with SessionLocal() as db:
        user = models.User(email=f"remote-unknown-{uuid.uuid4().hex}@example.com", name="Remote Unknown Test")
        db.add(user)
        db.flush()
        run = models.OperatorRun(
            user_id=user.id,
            goal="Order it",
            target_url="https://example.test/checkout",
            current_state="WAITING_FOR_USER",
            confidence_state={"pending_final_action": {"status": "awaiting_submit_approval", "payment_unknown": True}},
        )
        db.add(run)
        db.flush()
        question = models.OperatorQuestion(
            run_id=run.id,
            question_type="final_action_approval",
            prompt="Place order?",
            expected_answer_schema={"final_action": {"payment_floor": False}},
            status="pending",
        )
        db.add(question)
        db.commit()
        assert remote_access.is_payment_approval_question(db, question) is True


def test_runs_started_from_a_phone_without_step_up_snapshot_approval_mode() -> None:
    from fastapi import FastAPI
    from fastapi.responses import StreamingResponse

    from app.agent_settings import update_agent_settings
    from app.runtime import OperatorRuntimeEngine, _final_action_safety_policy

    with SessionLocal() as db:
        user_id = OperatorRuntimeEngine(db).ensure_default_user().id
        before = update_agent_settings(db, user_id, {})["final_action_mode"]
        update_agent_settings(db, user_id, {"final_action_mode": "autonomous"})
        db.commit()
    try:
        probe = FastAPI()

        @probe.get("/operator/runs")
        def sync_handler() -> dict:
            with SessionLocal() as db:
                return _final_action_safety_policy(db, user_id)

        @probe.get("/operator/runs/{run_id}")
        def streaming_handler(run_id: int) -> StreamingResponse:
            def stream():
                with SessionLocal() as db:
                    yield json.dumps(_final_action_safety_policy(db, user_id))

            return StreamingResponse(stream(), media_type="application/json")

        token, key, _device = _pair()
        gateway = TestClient(remote_access.RemoteGateway(probe))
        auth = {"Authorization": f"Bearer {token}"}
        held = gateway.get("/operator/runs", headers=auth).json()
        assert held["final_action_mode"] == "approval"
        assert held["authority_source"] == "paired_device_without_step_up"
        assert gateway.get("/operator/runs/1", headers=auth).json()["final_action_mode"] == "approval"

        nonce = remote.post("/devices/me/challenges", headers=auth).json()["nonce"]
        message = remote_access.step_up_message(nonce, "GET", "/operator/runs", hashlib.sha256(b"").hexdigest())
        stepped = gateway.get("/operator/runs", headers={**auth, "X-Lumen-Step-Up": f"{nonce}.{_sign(key, message)}"}).json()
        assert stepped["final_action_mode"] == "autonomous"

        with SessionLocal() as db:
            assert _final_action_safety_policy(db, user_id)["final_action_mode"] == "autonomous"
    finally:
        with SessionLocal() as db:
            update_agent_settings(db, user_id, {"final_action_mode": before})
            db.commit()


def test_push_hooks_never_fail_a_write(monkeypatch) -> None:
    monkeypatch.setattr(push, "_enqueue", lambda alert: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(push, "push_status", lambda: {"configured": True, "project_id": "test"})
    push.install()
    with SessionLocal() as db:
        user = models.User(email=f"remote-hook-{uuid.uuid4().hex}@example.com", name="Remote Hook Test")
        db.add(user)
        db.flush()
        run = models.OperatorRun(user_id=user.id, goal="g", target_url="https://example.test", current_state="RUNNING")
        db.add(run)
        db.flush()
        db.add(models.OperatorQuestion(run_id=run.id, question_type="field_approval", expected_answer_schema=["odd"], status="pending"))
        db.commit()
        run.current_state = "READY_FOR_REVIEW"
        db.commit()


def _policy_probe_client():
    from fastapi import FastAPI

    from app.runtime import _final_action_safety_policy

    probe = FastAPI()

    @probe.get("/operator/runs")
    def policy() -> dict:
        with SessionLocal() as db:
            from app.runtime import OperatorRuntimeEngine

            return _final_action_safety_policy(db, OperatorRuntimeEngine(db).ensure_default_user().id)

    return TestClient(remote_access.RemoteGateway(probe))


def test_one_fingerprint_unlocks_full_autonomy_for_a_while() -> None:
    from datetime import timedelta

    from app.agent_settings import update_agent_settings
    from app.runtime import OperatorRuntimeEngine

    with SessionLocal() as db:
        user_id = OperatorRuntimeEngine(db).ensure_default_user().id
        before = update_agent_settings(db, user_id, {})["final_action_mode"]
        update_agent_settings(db, user_id, {"final_action_mode": "autonomous"})
        db.commit()
    try:
        token, key, device_id = _pair()
        auth = {"Authorization": f"Bearer {token}"}
        probe = _policy_probe_client()
        assert probe.get("/operator/runs", headers=auth).json()["final_action_mode"] == "approval"

        # The unlock itself needs the fingerprint.
        assert remote.post("/devices/me/autonomy", json={}, headers=auth).status_code == 403
        body = b"{}"
        unlocked = remote.post("/devices/me/autonomy", content=body, headers=_step_up_headers(token, key, "POST", "/devices/me/autonomy", body))
        assert unlocked.status_code == 200
        assert unlocked.json()["device"]["autonomy_until"]

        # Plain requests now start runs with Full autonomy, no per-message fingerprint.
        assert probe.get("/operator/runs", headers=auth).json()["final_action_mode"] == "autonomous"
        assert probe.get("/operator/runs", headers=auth).json()["final_action_mode"] == "autonomous"

        # An expired unlock holds runs to approvals again.
        with SessionLocal() as db:
            device = db.get(models.PairedDevice, device_id)
            device.autonomy_until = models.utcnow() - timedelta(minutes=1)
            db.commit()
        assert probe.get("/operator/runs", headers=auth).json()["final_action_mode"] == "approval"

        # Locking ends it early.
        remote.post("/devices/me/autonomy", content=body, headers=_step_up_headers(token, key, "POST", "/devices/me/autonomy", body))
        assert remote.delete("/devices/me/autonomy", headers=auth).json()["device"]["autonomy_until"] is None
        assert probe.get("/operator/runs", headers=auth).json()["final_action_mode"] == "approval"
    finally:
        with SessionLocal() as db:
            update_agent_settings(db, user_id, {"final_action_mode": before})
            db.commit()


def test_turning_on_full_autonomy_from_the_phone_unlocks_it_there() -> None:
    from app.agent_settings import update_agent_settings
    from app.runtime import OperatorRuntimeEngine

    with SessionLocal() as db:
        user_id = OperatorRuntimeEngine(db).ensure_default_user().id
        before = update_agent_settings(db, user_id, {})["final_action_mode"]
    try:
        token, key, _device = _pair()
        auth = {"Authorization": f"Bearer {token}"}
        on = json.dumps({"final_action_mode": "autonomous"}).encode()
        assert remote.patch("/agent-settings", content=on, headers=_step_up_headers(token, key, "PATCH", "/agent-settings", on)).status_code == 200
        assert remote.get("/devices/me", headers=auth).json()["device"]["autonomy_until"]
        off = json.dumps({"final_action_mode": "approval"}).encode()
        assert remote.patch("/agent-settings", content=off, headers=_step_up_headers(token, key, "PATCH", "/agent-settings", off)).status_code == 200
        assert remote.get("/devices/me", headers=auth).json()["device"]["autonomy_until"] is None
    finally:
        with SessionLocal() as db:
            update_agent_settings(db, user_id, {"final_action_mode": before})
            db.commit()


def test_paired_phone_can_read_and_edit_the_profile_memory() -> None:
    token, _key, _device = _pair()
    auth = {"Authorization": f"Bearer {token}"}
    assert remote.get("/memory", headers=auth).status_code == 200
    assert remote.delete("/memory/no_such_fact", headers=auth).status_code == 404
    assert remote.get("/memory").status_code == 401
