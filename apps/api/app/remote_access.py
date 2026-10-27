"""Remote access for paired phones.

The backend keeps its local listener (127.0.0.1:8001) exactly as it was: the cockpit, the
extension, and local tools are trusted. Phones reach a second listener on
127.0.0.1:<remote_port> that `tailscale serve` exposes to the tailnet. Every request on that
listener passes through ``RemoteGateway``:

- Routes are denied unless listed in ``REMOTE_ROUTES``.
- Every listed route except pairing needs a device bearer token (only its SHA-256 is stored).
- ``step_up`` routes also need a step-up assertion: the device's hardware-backed key signs a
  one-time server challenge bound to the exact method, path, and body. Handlers can require the
  same for a specific decision (payment approvals) through ``require_step_up``.

Remoteness is decided by the socket a request arrived on, never by headers, so a request that
reaches the gateway cannot be mistaken for a local one.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import contextvars
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.config import get_settings
from app.db import SessionLocal
from app.models import utcnow


logger = logging.getLogger(__name__)

PAIRING_CODE_TTL_SECONDS = 300
CHALLENGE_TTL_SECONDS = 120
MAX_ACTIVE_PAIRING_CODES = 5
MAX_CHALLENGES_PER_DEVICE = 20
MAX_REMOTE_BODY_BYTES = 25 * 1024 * 1024
# Unauthenticated bodies (pairing) are tiny; never buffer more before knowing the caller.
MAX_OPEN_BODY_BYTES = 64 * 1024
LAST_SEEN_WRITE_INTERVAL = timedelta(seconds=60)
PAIR_FAILURE_WINDOW_SECONDS = 600
PAIR_FAILURE_LIMIT = 20
STEP_UP_HEADER = "x-lumen-step-up"
STEP_UP_REQUIRED_HEADER = "X-Lumen-Step-Up-Required"
STEP_UP_FORMAT = "lumen-step-up/v1"
SCOPE_KEY = "lumen_remote"

OPEN = "open"
DEVICE = "device"
STEP_UP = "step_up"


class RemoteAccessError(ValueError):
    pass


@dataclass(frozen=True)
class RemoteDevice:
    id: int
    user_id: int
    name: str
    public_key_spki: str
    autonomy_until: datetime | None = None

    def autonomy_unlocked(self) -> bool:
        return self.autonomy_until is not None and _aware(self.autonomy_until) > utcnow()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def token_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------------
# Route policy

_ID = r"\d+"
_ROUTE_TABLE: list[tuple[frozenset[str], str, str]] = [
    (frozenset({"POST"}), r"/devices/pair", OPEN),
    (frozenset({"GET", "DELETE"}), r"/devices/me", DEVICE),
    (frozenset({"POST"}), r"/devices/me/push-token", DEVICE),
    (frozenset({"POST"}), r"/devices/me/challenges", DEVICE),
    (frozenset({"POST"}), r"/devices/me/autonomy", STEP_UP),
    (frozenset({"DELETE"}), r"/devices/me/autonomy", DEVICE),
    (frozenset({"GET"}), r"/health", DEVICE),
    (frozenset({"GET"}), r"/health/model", DEVICE),
    (frozenset({"GET"}), r"/model-providers", DEVICE),
    (frozenset({"GET", "POST"}), r"/operator/chat/threads", DEVICE),
    (frozenset({"DELETE"}), rf"/operator/chat/threads/{_ID}", DEVICE),
    (frozenset({"GET", "POST"}), rf"/operator/chat/threads/{_ID}/messages", DEVICE),
    (frozenset({"GET"}), r"/operator/runs", DEVICE),
    (frozenset({"GET"}), rf"/operator/runs/{_ID}", DEVICE),
    (frozenset({"GET"}), rf"/operator/runs/{_ID}/(events|questions|screenshot/latest)", DEVICE),
    (frozenset({"POST"}), rf"/operator/runs/{_ID}/(pause|resume|cancel)", DEVICE),
    # Continuing a sign-in hand-off only makes Lumen look again; the vault write before it steps up.
    (frozenset({"POST"}), rf"/operator/runs/{_ID}/sensitive-browser-continue", DEVICE),
    # Payment approvals step up inside the handler (require_step_up), where the question is known.
    (frozenset({"POST"}), rf"/operator/questions/{_ID}/answer", DEVICE),
    (frozenset({"POST"}), rf"/operator/questions/{_ID}/artifacts/(upload|generate)", DEVICE),
    (frozenset({"POST"}), rf"/operator/questions/{_ID}/artifacts/{_ID}/apply", DEVICE),
    (frozenset({"GET", "POST"}), r"/artifacts", DEVICE),
    (frozenset({"POST"}), r"/artifacts/preview", DEVICE),
    (frozenset({"GET"}), rf"/artifacts/{_ID}/download", DEVICE),
    (frozenset({"PATCH", "DELETE"}), rf"/artifacts/{_ID}", DEVICE),
    (frozenset({"GET"}), rf"/resume-artifacts/{_ID}/download", DEVICE),
    (frozenset({"GET"}), r"/profiles/me", DEVICE),
    (frozenset({"GET", "POST"}), r"/memory", DEVICE),
    (frozenset({"PATCH", "DELETE"}), r"/memory/[a-z0-9_]+", DEVICE),
    (frozenset({"PATCH", "DELETE"}), rf"/operator/answers/{_ID}", DEVICE),
    (frozenset({"GET"}), r"/profiles/me/closed-world-attestations", DEVICE),
    (frozenset({"POST"}), r"/profiles/me/closed-world-attestations", STEP_UP),
    (frozenset({"POST"}), r"/profiles/me/closed-world-attestations/[A-Za-z0-9_-]+/revoke", STEP_UP),
    (frozenset({"GET"}), r"/agent-settings", DEVICE),
    # Agent settings include Full autonomy, which lets payments run without a prompt.
    (frozenset({"PATCH"}), r"/agent-settings", STEP_UP),
    (frozenset({"GET"}), r"/credential-vault", DEVICE),
    (frozenset({"POST"}), r"/credential-vault", STEP_UP),
    (frozenset({"PATCH", "DELETE"}), rf"/credential-vault/{_ID}", STEP_UP),
    (frozenset({"GET"}), r"/operator/desktop/status", DEVICE),
    (frozenset({"GET"}), r"/operator/extension/status", DEVICE),
]
REMOTE_ROUTES: list[tuple[frozenset[str], re.Pattern[str], str]] = [
    (methods, re.compile(pattern), policy) for methods, pattern, policy in _ROUTE_TABLE
]


def remote_route_policy(method: str, path: str) -> str | None:
    """The policy for a remote request, or None when the route is not available remotely."""

    method = method.upper()
    for methods, pattern, policy in REMOTE_ROUTES:
        if method in methods and pattern.fullmatch(path):
            return policy
    return None


# ---------------------------------------------------------------------------------------------
# Pairing codes and step-up challenges (process memory: short-lived by design)

_lock = threading.Lock()
_pairing_codes: dict[str, float] = {}
_challenges: dict[str, tuple[int, float]] = {}
_pair_failures: list[float] = []


def _prune(now: float) -> None:
    for key in [key for key, expires in _pairing_codes.items() if expires <= now]:
        _pairing_codes.pop(key, None)
    for key in [key for key, (_device, expires) in _challenges.items() if expires <= now]:
        _challenges.pop(key, None)
    while _pair_failures and _pair_failures[0] <= now - PAIR_FAILURE_WINDOW_SECONDS:
        _pair_failures.pop(0)


def issue_pairing_code() -> dict[str, Any]:
    code = secrets.token_urlsafe(24)
    now = time.monotonic()
    with _lock:
        _prune(now)
        while len(_pairing_codes) >= MAX_ACTIVE_PAIRING_CODES:
            _pairing_codes.pop(min(_pairing_codes, key=_pairing_codes.__getitem__))
        _pairing_codes[token_sha256(code)] = now + PAIRING_CODE_TTL_SECONDS
    return {
        "code": code,
        "expires_at": utcnow() + timedelta(seconds=PAIRING_CODE_TTL_SECONDS),
        "expires_in_seconds": PAIRING_CODE_TTL_SECONDS,
    }


def _consume_pairing_code(code: str) -> bool:
    now = time.monotonic()
    with _lock:
        _prune(now)
        expires = _pairing_codes.pop(token_sha256(code), None)
        if expires is not None and expires > now:
            return True
        _pair_failures.append(now)
        if len(_pair_failures) >= PAIR_FAILURE_LIMIT:
            # Someone is guessing: void every outstanding code; the user can show a new one.
            _pairing_codes.clear()
            _pair_failures.clear()
        return False


def issue_challenge(device_id: int) -> dict[str, Any]:
    nonce = secrets.token_urlsafe(24)
    now = time.monotonic()
    with _lock:
        _prune(now)
        mine = sorted(
            (expires, key) for key, (owner, expires) in _challenges.items() if owner == device_id
        )
        for _expires, key in mine[: max(0, len(mine) - MAX_CHALLENGES_PER_DEVICE + 1)]:
            _challenges.pop(key, None)
        _challenges[nonce] = (device_id, now + CHALLENGE_TTL_SECONDS)
    return {"nonce": nonce, "expires_in_seconds": CHALLENGE_TTL_SECONDS, "format": STEP_UP_FORMAT}


def _consume_challenge(nonce: str, device_id: int) -> bool:
    now = time.monotonic()
    with _lock:
        entry = _challenges.pop(nonce, None)
    return entry is not None and entry[0] == device_id and entry[1] > now


def reset_for_tests() -> None:
    with _lock:
        _pairing_codes.clear()
        _challenges.clear()
        _pair_failures.clear()


# ---------------------------------------------------------------------------------------------
# Keys and signatures

def _b64decode(value: str) -> bytes:
    text = "".join(str(value or "").split())
    padded = text + "=" * (-len(text) % 4)
    try:
        if "-" in text or "_" in text:
            return base64.urlsafe_b64decode(padded)
        return base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RemoteAccessError("Not valid base64.") from exc


def normalize_public_key(spki_b64: str) -> str:
    """Validate a device public key (base64 SubjectPublicKeyInfo DER) and return it canonically.

    Accepted: EC P-256 (SHA256withECDSA) or RSA of at least 2048 bits (SHA256withRSA, PKCS#1 v1.5).
    """

    der = _b64decode(spki_b64)
    try:
        key = serialization.load_der_public_key(der)
    except ValueError as exc:
        raise RemoteAccessError("The device key is not a valid public key.") from exc
    if isinstance(key, ec.EllipticCurvePublicKey):
        if not isinstance(key.curve, ec.SECP256R1):
            raise RemoteAccessError("Device EC keys must use P-256.")
    elif isinstance(key, rsa.RSAPublicKey):
        if key.key_size < 2048:
            raise RemoteAccessError("Device RSA keys must be at least 2048 bits.")
    else:
        raise RemoteAccessError("Device keys must be EC P-256 or RSA.")
    canonical = key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(canonical).decode("ascii")


def step_up_message(nonce: str, method: str, target: str, body_sha256: str) -> bytes:
    """The exact bytes a device signs: the challenge bound to one request."""

    return "\n".join([STEP_UP_FORMAT, nonce, method.upper(), target, body_sha256]).encode("utf-8")


def request_target(path: str, query_string: bytes | str = b"") -> str:
    query = query_string.decode("latin-1") if isinstance(query_string, bytes) else str(query_string or "")
    return f"{path}?{query}" if query else path


def _verify_signature(public_key_spki: str, signature: bytes, message: bytes) -> None:
    key = serialization.load_der_public_key(_b64decode(public_key_spki))
    try:
        if isinstance(key, ec.EllipticCurvePublicKey):
            key.verify(signature, message, ec.ECDSA(hashes.SHA256()))
        elif isinstance(key, rsa.RSAPublicKey):
            key.verify(signature, message, padding.PKCS1v15(), hashes.SHA256())
        else:
            raise RemoteAccessError("Unsupported device key.")
    except InvalidSignature as exc:
        raise RemoteAccessError("The step-up signature does not match this device.") from exc


def verify_step_up(device: RemoteDevice, header_value: str, *, method: str, target: str, body_sha256: str) -> None:
    """Verify ``<nonce>.<base64 signature>`` for this exact request. Each nonce works once."""

    nonce, separator, signature_b64 = str(header_value or "").strip().partition(".")
    if not separator or not nonce or not signature_b64:
        raise RemoteAccessError("The step-up assertion is malformed.")
    if not _consume_challenge(nonce, device.id):
        raise RemoteAccessError("The step-up challenge is unknown, used, or expired.")
    _verify_signature(device.public_key_spki, _b64decode(signature_b64), step_up_message(nonce, method, target, body_sha256))


# ---------------------------------------------------------------------------------------------
# Devices

def pair_device(
    db: Session,
    *,
    user_id: int,
    code: str,
    name: str,
    platform: str,
    public_key: str,
) -> tuple[models.PairedDevice, str]:
    """Trade a one-time pairing code for a device token. Caller commits."""

    normalized_key = normalize_public_key(public_key)
    if not _consume_pairing_code(code):
        raise RemoteAccessError("This pairing code is invalid or expired. Show a new code on your PC.")
    token = secrets.token_urlsafe(32)
    now = utcnow()
    device = models.PairedDevice(
        user_id=user_id,
        name=name.strip()[:128] or "Phone",
        platform=platform.strip()[:32] or "android",
        token_sha256=token_sha256(token),
        public_key_spki=normalized_key,
        last_seen_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(device)
    db.flush()
    return device, token


def active_devices(db: Session, *, user_id: int) -> list[models.PairedDevice]:
    return list(
        db.scalars(
            select(models.PairedDevice)
            .where(models.PairedDevice.user_id == user_id, models.PairedDevice.revoked_at.is_(None))
            .order_by(models.PairedDevice.created_at.desc())
        ).all()
    )


def unlock_autonomy(device: models.PairedDevice) -> datetime:
    """Let tasks this device starts use Full autonomy for the configured window. Caller commits."""

    until = utcnow() + timedelta(hours=float(get_settings().remote_autonomy_hours))
    device.autonomy_until = until
    device.updated_at = utcnow()
    return until


def revoke_device(db: Session, *, user_id: int, device_id: int) -> models.PairedDevice:
    device = db.get(models.PairedDevice, device_id)
    if device is None or device.user_id != user_id or device.revoked_at is not None:
        raise RemoteAccessError("Device not found.")
    now = utcnow()
    device.revoked_at = now
    device.push_token = None
    device.autonomy_until = None
    device.updated_at = now
    return device


def _authenticate(token: str) -> RemoteDevice | None:
    with SessionLocal() as db:
        device = db.scalar(
            select(models.PairedDevice).where(
                models.PairedDevice.token_sha256 == token_sha256(token),
                models.PairedDevice.revoked_at.is_(None),
            )
        )
        if device is None:
            return None
        now = utcnow()
        last_seen = _aware(device.last_seen_at) if device.last_seen_at is not None else None
        if last_seen is None or now - last_seen >= LAST_SEEN_WRITE_INTERVAL:
            device.last_seen_at = now
            db.commit()
        return RemoteDevice(
            id=device.id,
            user_id=device.user_id,
            name=device.name,
            public_key_spki=device.public_key_spki,
            autonomy_until=device.autonomy_until,
        )


# ---------------------------------------------------------------------------------------------
# Gateway

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


class _BodyTooLarge(Exception):
    pass


async def _read_body(receive: Receive, limit: int) -> bytes | None:
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return None
        chunk = message.get("body") or b""
        size += len(chunk)
        if size > limit:
            raise _BodyTooLarge
        chunks.append(chunk)
        if not message.get("more_body"):
            return b"".join(chunks)


def _replay(body: bytes, receive: Receive) -> Receive:
    delivered = False

    async def replay() -> dict[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        # After the body, pass through so streaming handlers still see the client disconnect.
        return await receive()

    return replay


def _header(scope: Scope, name: str) -> str:
    wanted = name.lower().encode("latin-1")
    for key, value in scope.get("headers") or []:
        if key.lower() == wanted:
            return value.decode("latin-1")
    return ""


async def _respond(send: Send, status: int, detail: str, *, step_up_required: bool = False) -> None:
    body = json.dumps({"detail": detail, "step_up_required": step_up_required}).encode("utf-8")
    headers = [(b"content-type", b"application/json"), (b"cache-control", b"no-store")]
    if status == 401 and not step_up_required:
        headers.append((b"www-authenticate", b"Bearer"))
    if step_up_required:
        headers.append((STEP_UP_REQUIRED_HEADER.lower().encode("latin-1"), b"1"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class RemoteGateway:
    """ASGI wrapper for the remote listener. See the module docstring for the rules."""

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4403})
            return
        if scope["type"] != "http":
            return
        method = str(scope.get("method") or "GET").upper()
        path = str(scope.get("path") or "")
        policy = remote_route_policy(method, path)
        if policy is None:
            await _respond(send, 403, "This endpoint is not available to paired devices.")
            return
        device: RemoteDevice | None = None
        step_up_verified = False
        if policy != OPEN:
            # Authenticate from headers before buffering any body.
            authorization = _header(scope, "authorization")
            scheme, _, token = authorization.partition(" ")
            if scheme.lower() == "bearer" and token.strip():
                device = await asyncio.to_thread(_authenticate, token.strip())
            if device is None:
                await _respond(send, 401, "This device is not paired. Pair it again from Lumen on your PC.")
                return
        try:
            body = await _read_body(receive, MAX_OPEN_BODY_BYTES if policy == OPEN else MAX_REMOTE_BODY_BYTES)
        except _BodyTooLarge:
            await _respond(send, 413, "The request body is too large.")
            return
        if body is None:
            return
        if device is not None:
            assertion = _header(scope, STEP_UP_HEADER)
            if assertion:
                try:
                    verify_step_up(
                        device,
                        assertion,
                        method=method,
                        target=request_target(path, scope.get("query_string") or b""),
                        body_sha256=hashlib.sha256(body).hexdigest(),
                    )
                    step_up_verified = True
                except RemoteAccessError as exc:
                    await _respond(send, 401, str(exc), step_up_required=True)
                    return
            if policy == STEP_UP and not step_up_verified:
                await _respond(send, 403, "Confirm this on your paired phone first.", step_up_required=True)
                return
        scope = dict(scope)
        scope[SCOPE_KEY] = {
            "device_id": device.id if device else None,
            "user_id": device.user_id if device else None,
            "step_up_verified": step_up_verified,
        }
        # A run created while serving this request snapshots approval mode unless the phone stepped up
        # or holds a fingerprint unlock (see autonomy_held). Context variables follow the request into
        # thread-pool handlers.
        unlocked = step_up_verified or bool(device and device.autonomy_unlocked())
        held = _autonomy_held.set(not unlocked)
        try:
            await self.app(scope, _replay(body, receive), send)
        finally:
            _autonomy_held.reset(held)


_autonomy_held: contextvars.ContextVar[bool] = contextvars.ContextVar("lumen_remote_autonomy_held", default=False)


def autonomy_held() -> bool:
    """True while serving a paired-device request with no verified step-up and no live autonomy unlock.

    Full autonomy lets a run make payments without asking, so a run started this way snapshots approval
    mode instead: a stolen device token alone can never start a run that pays without a question. One
    fingerprint (POST /devices/me/autonomy) unlocks the phone for `remote_autonomy_hours`.
    """

    return _autonomy_held.get()


def remote_context(request: Request) -> dict[str, Any] | None:
    context = request.scope.get(SCOPE_KEY)
    return context if isinstance(context, dict) else None


def require_device(request: Request) -> dict[str, Any]:
    context = remote_context(request)
    if not context or not context.get("device_id"):
        raise HTTPException(status_code=403, detail="Only a paired device can use this endpoint.")
    return context


def require_step_up(request: Request) -> None:
    """For remote requests, insist the request carried a verified step-up assertion."""

    context = remote_context(request)
    if context is None:
        return
    if not context.get("step_up_verified"):
        raise HTTPException(
            status_code=403,
            detail="Confirm this on your paired phone first.",
            headers={STEP_UP_REQUIRED_HEADER: "1"},
        )


def is_payment_approval_question(db: Session, question: models.OperatorQuestion) -> bool:
    if question.question_type != "final_action_approval":
        return False
    final_action = (question.expected_answer_schema or {}).get("final_action") or {}
    if isinstance(final_action, dict) and final_action.get("payment_floor"):
        return True
    run = db.get(models.OperatorRun, question.run_id)
    pending = ((run.confidence_state or {}) if run else {}).get("pending_final_action") or {}
    if not isinstance(pending, dict):
        return False
    # An unknown payment verdict (judge unavailable) is treated as a payment: fail closed.
    return (
        pending.get("status") == "awaiting_payment_confirmation"
        or bool(pending.get("payment"))
        or bool(pending.get("payment_unknown"))
    )


# ---------------------------------------------------------------------------------------------
# The embedded listener

try:
    import uvicorn

    class _EmbeddedServer(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self):  # the host server owns process signals
            yield

except ImportError:  # pragma: no cover - uvicorn ships with the API
    uvicorn = None  # type: ignore[assignment]
    _EmbeddedServer = None  # type: ignore[assignment,misc]


_listener: dict[str, Any] = {"server": None, "task": None, "error": None}


def _bind(port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        sock.close()
        raise
    sock.setblocking(False)
    return sock


async def _serve(app: Any, port: int) -> None:
    # A --reload restart can briefly leave the old process holding the port.
    sock = None
    for attempt in range(30):
        try:
            sock = _bind(port)
            break
        except OSError as exc:
            _listener["error"] = f"Port {port} is busy: {exc}"
            if attempt == 0:
                logger.warning("Remote listener could not bind 127.0.0.1:%s yet; retrying", port)
            await asyncio.sleep(2)
    if sock is None:
        logger.error("Remote listener gave up binding 127.0.0.1:%s", port)
        return
    config = uvicorn.Config(
        RemoteGateway(app),
        host="127.0.0.1",
        port=port,
        lifespan="off",
        proxy_headers=False,
        # Leave process-wide logging to the host server: no dictConfig, no level or access-log changes.
        log_config=None,
        log_level=None,
        access_log=True,
    )
    server = _EmbeddedServer(config)
    _listener["server"] = server
    _listener["error"] = None
    try:
        await server.serve(sockets=[sock])
    except Exception as exc:  # noqa: BLE001 - the remote listener must never take the backend down
        _listener["error"] = str(exc)
        logger.exception("Remote listener stopped")
    finally:
        _listener["server"] = None
        sock.close()


def start_remote_listener(app: Any) -> None:
    port = int(get_settings().remote_port or 0)
    if port <= 0 or uvicorn is None or _listener.get("task") is not None:
        return
    _listener["task"] = asyncio.get_running_loop().create_task(_serve(app, port))


async def stop_remote_listener() -> None:
    server = _listener.get("server")
    task = _listener.get("task")
    if server is not None:
        server.should_exit = True
    if task is not None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, timeout=5)
    _listener["task"] = None


def listener_status() -> dict[str, Any]:
    port = int(get_settings().remote_port or 0)
    server = _listener.get("server")
    return {
        "enabled": port > 0,
        "port": port,
        "running": bool(server is not None and getattr(server, "started", False)),
        "error": _listener.get("error"),
    }


# ---------------------------------------------------------------------------------------------
# Tailscale discovery

_TAILSCALE_CACHE_SECONDS = 30
_tailscale_cache: dict[str, Any] = {"at": 0.0, "value": None}


def _tailscale_executable() -> str | None:
    found = shutil.which("tailscale")
    if found:
        return found
    if sys.platform == "win32":
        candidate = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tailscale" / "tailscale.exe"
        if candidate.is_file():
            return str(candidate)
    return None


def _run_json(args: list[str]) -> Any:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    completed = subprocess.run(args, capture_output=True, timeout=5, creationflags=flags, check=False)
    if completed.returncode != 0:
        return None
    try:
        return json.loads(completed.stdout.decode("utf-8", "replace") or "null")
    except json.JSONDecodeError:
        return None


def _serve_proxy(serve_status: Any) -> str | None:
    if not isinstance(serve_status, dict):
        return None
    for host, entry in (serve_status.get("Web") or {}).items():
        if not str(host).endswith(":443"):
            continue
        handler = ((entry or {}).get("Handlers") or {}).get("/") or {}
        if handler.get("Proxy"):
            return str(handler["Proxy"])
    return None


def tailscale_status(*, refresh: bool = False) -> dict[str, Any]:
    now = time.monotonic()
    cached = _tailscale_cache.get("value")
    if cached is not None and not refresh and now - float(_tailscale_cache["at"]) < _TAILSCALE_CACHE_SECONDS:
        return cached
    executable = _tailscale_executable()
    value: dict[str, Any] = {"installed": bool(executable), "logged_in": False, "dns_name": None, "url": None, "serve_proxy": None}
    if executable:
        try:
            status = _run_json([executable, "status", "--json"]) or {}
            dns_name = str(((status.get("Self") or {}).get("DNSName")) or "").rstrip(".")
            value["logged_in"] = status.get("BackendState") == "Running"
            value["dns_name"] = dns_name or None
            value["url"] = f"https://{dns_name}" if dns_name else None
            value["serve_proxy"] = _serve_proxy(_run_json([executable, "serve", "status", "--json"]))
        except (OSError, subprocess.SubprocessError) as exc:
            value["error"] = str(exc)
    _tailscale_cache.update({"at": now, "value": value})
    return value


def remote_access_status(*, refresh: bool = False) -> dict[str, Any]:
    settings = get_settings()
    listener = listener_status()
    tailscale = tailscale_status(refresh=refresh)
    expected_proxy = f"http://127.0.0.1:{listener['port']}"
    serve_ok = bool(tailscale.get("serve_proxy")) and str(tailscale["serve_proxy"]).rstrip("/") == expected_proxy
    public_url = (settings.remote_public_url or "").rstrip("/") or tailscale.get("url")
    problem = None
    if not listener["enabled"]:
        problem = "Remote access is turned off (LUMEN_REMOTE_PORT=0)."
    elif not listener["running"]:
        problem = listener.get("error") or "The remote listener is starting."
    elif not tailscale.get("installed"):
        problem = "Install Tailscale on this PC and sign in."
    elif not tailscale.get("logged_in"):
        problem = "Sign in to Tailscale on this PC."
    elif not settings.remote_public_url and not serve_ok:
        problem = "Tailscale is not serving Lumen to your tailnet yet."
    return {
        "ready": problem is None,
        "problem": problem,
        "public_url": public_url,
        "listener": listener,
        "tailscale": tailscale,
        "serve_command": f"tailscale serve --bg --https=443 {expected_proxy}",
    }


def pairing_uri(public_url: str | None, code: str) -> str | None:
    if not public_url:
        return None
    return "lumen://pair?" + urlencode({"v": 1, "url": public_url, "code": code})
