"""Push alerts to paired phones through Firebase Cloud Messaging (HTTP v1).

Alerts carry no run content: a generic title and body plus ids (kind, run id, question id). The
phone fetches the details over the tailnet. Content would otherwise transit Google.

Triggers are observed at the database boundary (SQLAlchemy flush events on ``SessionLocal``) and
sent only after the transaction commits:

- a new pending question (``question``),
- a run entering sensitive entry (``sensitive_entry``),
- a run reaching review, completion, or a stop (``run_finished`` / ``run_stopped``).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from sqlalchemy import event, inspect, select
from sqlalchemy.orm import Session

from app import models
from app.config import get_settings
from app.db import SessionLocal


logger = logging.getLogger(__name__)

FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
ANDROID_CHANNEL_ID = "lumen-attention"
DEDUPE_SECONDS = 20
_SESSION_KEY = "lumen_push_pending"

_FINISHED_STATES = {"READY_FOR_REVIEW", "COMPLETED"}
_STOPPED_STATES = {"FAILED", "BLOCKED"}


def credentials_path() -> Path | None:
    configured = get_settings().firebase_credentials_path
    if configured:
        return Path(configured).expanduser()
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "Lumen" / "secrets" / "firebase-admin.json"
    return None


def _service_account() -> dict[str, Any] | None:
    path = credentials_path()
    if path is None or not path.is_file():
        return None
    try:
        account = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Firebase credentials at %s are unreadable", path)
        return None
    if not isinstance(account, dict):
        return None
    if account.get("type") != "service_account" or not account.get("private_key") or not account.get("project_id"):
        return None
    return account


def push_status() -> dict[str, Any]:
    account = _service_account()
    return {"configured": account is not None, "project_id": (account or {}).get("project_id")}


# ---------------------------------------------------------------------------------------------
# Alert content (generic on purpose)

def alert_for(kind: str, *, question_type: str = "", payment: bool = False) -> dict[str, str]:
    if kind == "question":
        if question_type == "final_action_approval":
            if payment:
                return {"title": "Lumen needs your OK to pay", "body": "Open Lumen to review the payment."}
            return {"title": "Lumen needs your approval", "body": "Open Lumen to review the final step."}
        return {"title": "Lumen has a question", "body": "Open Lumen to answer it."}
    if kind == "sensitive_entry":
        return {"title": "Lumen needs a sign-in", "body": "A run is waiting for a password or code."}
    if kind == "run_finished":
        return {"title": "Lumen finished", "body": "Open Lumen to see the result."}
    return {"title": "Lumen stopped", "body": "A run needs a look."}


# ---------------------------------------------------------------------------------------------
# Sending

class _AccessToken:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires = 0.0

    def get(self, account: dict[str, Any], client: httpx.Client) -> str:
        with self._lock:
            if self._token and time.time() < self._expires - 60:
                return self._token
            now = int(time.time())
            token_uri = account.get("token_uri") or "https://oauth2.googleapis.com/token"
            claims = {"iss": account["client_email"], "scope": FCM_SCOPE, "aud": token_uri, "iat": now, "exp": now + 3600}
            assertion = _sign_jwt(claims, account["private_key"])
            response = client.post(
                token_uri,
                data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
                timeout=15,
            )
            response.raise_for_status()
            payload = response.json()
            self._token = str(payload["access_token"])
            self._expires = time.time() + float(payload.get("expires_in") or 3600)
            return self._token


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _sign_jwt(claims: dict[str, Any], private_key_pem: str) -> str:
    header = {"alg": "RS256", "typ": "JWT"}
    signing_input = f"{_b64url(json.dumps(header).encode())}.{_b64url(json.dumps(claims).encode())}".encode("ascii")
    key = serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
    signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{signing_input.decode('ascii')}.{_b64url(signature)}"


_access_token = _AccessToken()


def _fcm_message(push_token: str, alert: dict[str, Any]) -> dict[str, Any]:
    data = {key: str(value) for key, value in alert["data"].items() if value is not None}
    tag = f"run-{alert['data'].get('run_id') or 'none'}"
    return {
        "message": {
            "token": push_token,
            "data": data,
            "notification": {"title": alert["title"], "body": alert["body"]},
            "android": {
                "priority": "HIGH",
                "collapse_key": tag,
                "notification": {"channel_id": ANDROID_CHANNEL_ID, "tag": tag},
            },
        }
    }


def _is_unregistered(response: httpx.Response) -> bool:
    if response.status_code == 404:
        return True
    try:
        details = (response.json().get("error") or {}).get("details") or []
    except ValueError:
        return False
    return any(isinstance(item, dict) and item.get("errorCode") == "UNREGISTERED" for item in details)


def _send_to_devices(alert: dict[str, Any]) -> None:
    account = _service_account()
    if account is None:
        return
    with SessionLocal() as db:
        devices = list(
            db.scalars(
                select(models.PairedDevice).where(
                    models.PairedDevice.revoked_at.is_(None),
                    models.PairedDevice.push_token.is_not(None),
                )
            ).all()
        )
        if not devices:
            return
        url = f"https://fcm.googleapis.com/v1/projects/{account['project_id']}/messages:send"
        with httpx.Client() as client:
            access_token = _access_token.get(account, client)
            for device in devices:
                response = client.post(
                    url,
                    json=_fcm_message(str(device.push_token), alert),
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=15,
                )
                if response.is_success:
                    continue
                if _is_unregistered(response):
                    device.push_token = None
                    db.commit()
                    continue
                logger.warning("Push to device %s failed with HTTP %s", device.id, response.status_code)


# Tests replace this to observe alerts without a network.
deliver: Callable[[dict[str, Any]], None] = _send_to_devices

_queue: "queue.Queue[dict[str, Any]]" = queue.Queue()
_worker: threading.Thread | None = None
_worker_lock = threading.Lock()
_recent: dict[tuple[Any, ...], float] = {}


def _work() -> None:
    while True:
        alert = _queue.get()
        try:
            deliver(alert)
        except Exception:  # noqa: BLE001 - a failed alert must never affect runs
            logger.exception("Push alert failed")


def _enqueue(alert: dict[str, Any]) -> None:
    global _worker
    key = (alert["data"].get("run_id"), alert["data"]["kind"], alert["data"].get("question_id"))
    now = time.monotonic()
    with _worker_lock:
        if now - _recent.get(key, -DEDUPE_SECONDS) < DEDUPE_SECONDS:
            return
        _recent[key] = now
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_work, name="lumen-push", daemon=True)
            _worker.start()
    _queue.put(alert)


def reset_for_tests() -> None:
    with _worker_lock:
        _recent.clear()


# ---------------------------------------------------------------------------------------------
# Triggers

def _alert(kind: str, *, run_id: int | None, question_id: int | None = None, question_type: str = "", payment: bool = False) -> dict[str, Any]:
    return {
        **alert_for(kind, question_type=question_type, payment=payment),
        "data": {"kind": kind, "run_id": run_id, "question_id": question_id},
    }


def _takeover_active(state: Any) -> bool:
    takeover = (state or {}).get("sensitive_browser_takeover") if isinstance(state, dict) else None
    return bool(isinstance(takeover, dict) and takeover.get("active"))


def _collect(session: Session, flush_context: Any) -> None:
    # Hooks run inside every flush and commit of the run loop: they must never fail a write.
    try:
        _collect_alerts(session)
    except Exception:  # noqa: BLE001
        logger.exception("Collecting push alerts failed")


def _collect_alerts(session: Session) -> None:
    pending: list[dict[str, Any]] = session.info.setdefault(_SESSION_KEY, [])
    question_runs: set[int] = set()
    for obj in session.new:
        if isinstance(obj, models.OperatorQuestion) and (obj.status or "pending") == "pending":
            schema = obj.expected_answer_schema if isinstance(obj.expected_answer_schema, dict) else {}
            final_action = schema.get("final_action") or {}
            payment = bool(isinstance(final_action, dict) and final_action.get("payment_floor"))
            pending.append(
                _alert("question", run_id=obj.run_id, question_id=obj.id, question_type=obj.question_type or "", payment=payment)
            )
            question_runs.add(obj.run_id)
    for obj in session.dirty:
        if not isinstance(obj, models.OperatorRun):
            continue
        attrs = inspect(obj).attrs
        state_history = attrs.current_state.history
        if state_history.added:
            new_state = str(state_history.added[0] or "").upper()
            if new_state in _FINISHED_STATES:
                pending.append(_alert("run_finished", run_id=obj.id))
            elif new_state in _STOPPED_STATES:
                pending.append(_alert("run_stopped", run_id=obj.id))
        confidence_history = attrs.confidence_state.history
        if confidence_history.added and obj.id not in question_runs:
            before = confidence_history.deleted[0] if confidence_history.deleted else None
            if _takeover_active(confidence_history.added[0]) and not _takeover_active(before):
                pending.append(_alert("sensitive_entry", run_id=obj.id))


def _dispatch(session: Session) -> None:
    pending = session.info.pop(_SESSION_KEY, None)
    if not pending:
        return
    try:
        if not push_status()["configured"]:
            return
        for alert in pending:
            _enqueue(alert)
    except Exception:  # noqa: BLE001 - the commit already happened; never report it as failed
        logger.exception("Dispatching push alerts failed")


def _discard(session: Session, *_args: Any) -> None:
    session.info.pop(_SESSION_KEY, None)


_installed = False


def install() -> None:
    """Attach the flush/commit hooks to the app's session factory (idempotent)."""

    global _installed
    if _installed:
        return
    event.listen(SessionLocal, "after_flush", _collect)
    event.listen(SessionLocal, "after_commit", _dispatch)
    event.listen(SessionLocal, "after_rollback", _discard)
    _installed = True
