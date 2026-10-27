from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal, cast
from urllib.parse import SplitResult, urlsplit

from cryptography.fernet import InvalidToken
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models, totp
from app.config import get_settings
from app.security import decrypt_secret, encrypt_secret


CredentialSlot = Literal["username", "email", "password", "totp"]

ALLOWED_SLOTS: tuple[CredentialSlot, ...] = ("username", "email", "password", "totp")
MAX_ORIGIN_LENGTH = 512
MAX_ACCOUNT_LABEL_LENGTH = 128
MAX_SECRET_LENGTH = 2048
_HOST_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)


class CredentialVaultError(ValueError):
    """Base error whose message is safe to expose; it never contains credential values."""


class CredentialVaultConfigurationError(CredentialVaultError):
    pass


class CredentialVaultNotFoundError(CredentialVaultError):
    pass


class CredentialVaultConflictError(CredentialVaultError):
    pass


class CredentialVaultAccessError(CredentialVaultError):
    pass


class CredentialVaultCorruptError(CredentialVaultError):
    pass


def normalize_origin(origin: str) -> str:
    """Validate and canonicalize a stored origin.

    Stored origins are deliberately stricter than navigation URLs: only a scheme,
    host, and optional non-default port are accepted. Paths, queries, fragments,
    and URL-embedded credentials are rejected.
    """

    parsed = _split_url(origin, allow_navigation_details=False)
    return _serialized_origin(parsed)


def origin_from_url(url: str) -> str:
    """Return the canonical origin for a full browser navigation URL."""

    parsed = _split_url(url, allow_navigation_details=True)
    return _serialized_origin(parsed)


def list_metadata(
    db: Session,
    *,
    user_id: int,
) -> list[dict[str, Any]]:
    stmt = select(models.CredentialVaultItem).where(
        models.CredentialVaultItem.user_id == user_id,
        models.CredentialVaultItem.revoked_at.is_(None),
    )
    stmt = stmt.order_by(
        models.CredentialVaultItem.updated_at.desc(),
        models.CredentialVaultItem.id.desc(),
    )
    return [metadata_for_item(item) for item in db.scalars(stmt).all()]


def list_metadata_for_url(
    db: Session,
    *,
    user_id: int,
    url: str,
) -> list[dict[str, Any]]:
    return _list_metadata_for_normalized_origin(db, user_id=user_id, origin=origin_from_url(url))


def list_metadata_for_origin(
    db: Session,
    *,
    user_id: int,
    origin: str,
) -> list[dict[str, Any]]:
    return _list_metadata_for_normalized_origin(db, user_id=user_id, origin=normalize_origin(origin))


def _list_metadata_for_normalized_origin(
    db: Session,
    *,
    user_id: int,
    origin: str,
) -> list[dict[str, Any]]:
    stmt = select(models.CredentialVaultItem).where(
        models.CredentialVaultItem.user_id == user_id,
        models.CredentialVaultItem.origin == origin,
        models.CredentialVaultItem.revoked_at.is_(None),
    )
    stmt = stmt.order_by(
        models.CredentialVaultItem.auto_use.desc(),
        models.CredentialVaultItem.last_used_at.desc().nullslast(),
        models.CredentialVaultItem.updated_at.desc(),
        models.CredentialVaultItem.id.desc(),
    )
    return [metadata_for_item(item) for item in db.scalars(stmt).all()]


def create(
    db: Session,
    *,
    user_id: int,
    origin: str,
    account_label: str,
    fields: Mapping[str, str | None],
    auto_use: bool = True,
    encryption_key: str | None = None,
) -> models.CredentialVaultItem:
    normalized_origin = normalize_origin(origin)
    normalized_label = _normalize_account_label(account_label)
    field_updates = _normalize_field_updates(fields)
    if not field_updates or not any(value is not None for value in field_updates.values()):
        raise CredentialVaultError("At least one credential field is required.")

    existing = db.scalars(
        select(models.CredentialVaultItem).where(
            models.CredentialVaultItem.user_id == user_id,
            models.CredentialVaultItem.origin == normalized_origin,
            models.CredentialVaultItem.account_label == normalized_label,
        )
    ).first()
    if existing is not None:
        raise CredentialVaultConflictError(
            "Credentials for this exact site and account label already exist. "
            "Choose a different account label or explicitly update the existing entry."
        )

    created_fields = _merge_fields({}, field_updates)
    encrypted_payload = _encrypt_fields(created_fields, encryption_key=encryption_key)
    now = models.utcnow()

    item = models.CredentialVaultItem(
        user_id=user_id,
        origin=normalized_origin,
        account_label=normalized_label,
        encrypted_payload_json=encrypted_payload,
        available_slots_json=_available_slots(created_fields),
        auto_use=bool(auto_use),
        revoked_at=None,
        created_at=now,
        updated_at=now,
    )
    db.add(item)
    _flush_account_identity_write(db)
    return item


def update(
    db: Session,
    *,
    user_id: int,
    item_id: int,
    origin: str | None = None,
    account_label: str | None = None,
    fields: Mapping[str, str | None] | None = None,
    auto_use: bool | None = None,
    encryption_key: str | None = None,
) -> models.CredentialVaultItem:
    item = _owned_item(db, user_id=user_id, item_id=item_id)
    if item.revoked_at is not None:
        raise CredentialVaultConflictError("Revoked credential vault items cannot be updated.")

    normalized_origin = normalize_origin(origin) if origin is not None else item.origin
    normalized_label = _normalize_account_label(account_label) if account_label is not None else item.account_label
    if normalized_origin != item.origin or normalized_label != item.account_label:
        conflict = db.scalars(
            select(models.CredentialVaultItem).where(
                models.CredentialVaultItem.user_id == user_id,
                models.CredentialVaultItem.origin == normalized_origin,
                models.CredentialVaultItem.account_label == normalized_label,
                models.CredentialVaultItem.id != item.id,
            )
        ).first()
        if conflict is not None:
            raise CredentialVaultConflictError("A credential vault item already uses that origin and account label.")

    if fields is not None:
        field_updates = _normalize_field_updates(fields)
        if not field_updates:
            raise CredentialVaultError("Credential fields must include at least one supported slot.")
        merged_fields = _merge_fields(
            _decrypt_fields(item, encryption_key=encryption_key),
            field_updates,
        )
        item.encrypted_payload_json = _encrypt_fields(merged_fields, encryption_key=encryption_key)
        item.available_slots_json = _available_slots(merged_fields)

    item.origin = normalized_origin
    item.account_label = normalized_label
    if auto_use is not None:
        item.auto_use = bool(auto_use)
    item.updated_at = models.utcnow()
    _flush_account_identity_write(db)
    return item


def resolve_slot(
    db: Session,
    *,
    user_id: int,
    item_id: int,
    slot: CredentialSlot,
    url: str,
    encryption_key: str | None = None,
) -> str:
    if slot not in ALLOWED_SLOTS:
        raise CredentialVaultAccessError("Unsupported credential slot.")
    item = _owned_item(db, user_id=user_id, item_id=item_id)
    _assert_item_available_for_url(item, url=url, require_auto_use=True)
    fields = _decrypt_fields(item, encryption_key=encryption_key)
    resolved = fields.get(slot)
    if not isinstance(resolved, str):
        raise CredentialVaultNotFoundError("Credential slot not found.")
    return resolved


def mark_used(
    db: Session,
    *,
    user_id: int,
    item_id: int,
    url: str,
) -> models.CredentialVaultItem:
    item = _owned_item(db, user_id=user_id, item_id=item_id)
    _assert_item_available_for_url(item, url=url, require_auto_use=False)
    item.last_used_at = models.utcnow()
    item.updated_at = item.last_used_at
    db.flush()
    return item


def delete(
    db: Session,
    *,
    user_id: int,
    item_id: int,
) -> None:
    item = _owned_item(db, user_id=user_id, item_id=item_id)
    db.delete(item)
    db.flush()


def metadata_for_item(item: models.CredentialVaultItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "origin": item.origin,
        "account_label": item.account_label,
        "available_slots": _sanitize_available_slots(item.available_slots_json),
        "auto_use": bool(item.auto_use),
        "created_at": _isoformat(item.created_at),
        "updated_at": _isoformat(item.updated_at),
        "last_used_at": _isoformat(item.last_used_at),
        "revoked_at": _isoformat(item.revoked_at),
    }


def _split_url(value: str, *, allow_navigation_details: bool) -> SplitResult:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CredentialVaultError("Credential origin must be an absolute URL without surrounding whitespace.")
    if len(value) > 2048:
        raise CredentialVaultError("Credential URL is too long.")
    if "\\" in value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise CredentialVaultError("Credential URL contains invalid characters.")
    if not allow_navigation_details:
        if "?" in value or "#" in value:
            raise CredentialVaultError("Credential origin cannot contain a query or fragment.")

    parsed = urlsplit(value)
    if not parsed.scheme or not parsed.netloc or parsed.hostname is None:
        raise CredentialVaultError("Credential origin must include a scheme and hostname.")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise CredentialVaultError("Credential URLs cannot contain embedded credentials.")
    if not allow_navigation_details and parsed.path not in ("", "/"):
        raise CredentialVaultError("Credential origin cannot contain a path.")
    try:
        parsed.port
    except ValueError as exc:
        raise CredentialVaultError("Credential origin contains an invalid port.") from exc

    scheme = parsed.scheme.lower()
    hostname = _canonical_hostname(parsed.hostname)
    if scheme != "https" and not (scheme == "http" and _is_localhost(hostname)):
        raise CredentialVaultError("Credential origins must use HTTPS.")
    return parsed._replace(scheme=scheme, netloc=_netloc(hostname, parsed.port))


def _serialized_origin(parsed: SplitResult) -> str:
    scheme = parsed.scheme.lower()
    hostname = _canonical_hostname(cast(str, parsed.hostname))
    port = parsed.port
    if (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
        port = None
    origin = f"{scheme}://{_netloc(hostname, port)}"
    if len(origin) > MAX_ORIGIN_LENGTH:
        raise CredentialVaultError("Credential origin is too long.")
    return origin


def _canonical_hostname(hostname: str) -> str:
    candidate = hostname.rstrip(".")
    if not candidate:
        raise CredentialVaultError("Credential origin must include a valid hostname.")
    try:
        return ipaddress.ip_address(candidate).compressed.lower()
    except ValueError:
        pass
    try:
        ascii_hostname = candidate.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise CredentialVaultError("Credential origin must include a valid hostname.") from exc
    if len(ascii_hostname) > 253 or any(not _HOST_LABEL_RE.fullmatch(label) for label in ascii_hostname.split(".")):
        raise CredentialVaultError("Credential origin must include a valid hostname.")
    return ascii_hostname


def _netloc(hostname: str, port: int | None) -> str:
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    return f"{rendered_host}:{port}" if port is not None else rendered_host


def _is_localhost(hostname: str) -> bool:
    if hostname == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _normalize_account_label(account_label: str) -> str:
    if not isinstance(account_label, str):
        raise CredentialVaultError("Account label must be text.")
    normalized = account_label.strip()
    if not normalized or len(normalized) > MAX_ACCOUNT_LABEL_LENGTH:
        raise CredentialVaultError("Account label must contain between 1 and 128 characters.")
    if any(ord(char) < 32 or ord(char) == 127 for char in normalized):
        raise CredentialVaultError("Account label contains invalid characters.")
    return normalized


def _normalize_field_updates(fields: Mapping[str, str | None]) -> dict[CredentialSlot, str | None]:
    if not isinstance(fields, Mapping):
        raise CredentialVaultError("Credential fields must be an object.")
    updates: dict[CredentialSlot, str | None] = {}
    for raw_slot, value in fields.items():
        if raw_slot not in ALLOWED_SLOTS:
            raise CredentialVaultError("Credential fields contain an unsupported slot.")
        slot = cast(CredentialSlot, raw_slot)
        if value is not None:
            if not isinstance(value, str) or not value or len(value) > MAX_SECRET_LENGTH:
                raise CredentialVaultError("Credential values must contain between 1 and 2048 characters.")
            if slot == "totp" and totp.parse_seed(value) is None:
                raise CredentialVaultError("The totp value must be a base32 seed or an otpauth://totp URI.")
        updates[slot] = value
    return updates


def _merge_fields(
    existing: Mapping[CredentialSlot, str],
    updates: Mapping[CredentialSlot, str | None],
) -> dict[CredentialSlot, str]:
    merged = dict(existing)
    for slot, value in updates.items():
        if value is None:
            merged.pop(slot, None)
        else:
            merged[slot] = value
    if not merged:
        raise CredentialVaultError("A credential vault item must retain at least one credential field.")
    return {slot: merged[slot] for slot in ALLOWED_SLOTS if slot in merged}


def _encrypt_fields(
    fields: Mapping[CredentialSlot, str],
    *,
    encryption_key: str | None,
) -> str:
    key = _require_encryption_key(encryption_key)
    serialized = json.dumps(
        {"version": 1, "fields": dict(fields)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return encrypt_secret(serialized, key)


def _decrypt_fields(
    item: models.CredentialVaultItem,
    *,
    encryption_key: str | None,
) -> dict[CredentialSlot, str]:
    key = _require_encryption_key(encryption_key)
    try:
        raw_payload = decrypt_secret(item.encrypted_payload_json, key)
        payload = json.loads(raw_payload)
    except (InvalidToken, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise CredentialVaultCorruptError("Credential vault payload could not be decrypted.") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1 or set(payload) != {"version", "fields"}:
        raise CredentialVaultCorruptError("Credential vault payload is invalid.")
    raw_fields = payload.get("fields")
    if not isinstance(raw_fields, dict) or not raw_fields:
        raise CredentialVaultCorruptError("Credential vault payload is invalid.")
    try:
        fields = _normalize_field_updates(cast(dict[str, str | None], raw_fields))
    except CredentialVaultError as exc:
        raise CredentialVaultCorruptError("Credential vault payload is invalid.") from exc
    if any(value is None for value in fields.values()):
        raise CredentialVaultCorruptError("Credential vault payload is invalid.")
    return cast(dict[CredentialSlot, str], fields)


def _require_encryption_key(encryption_key: str | None) -> str:
    key = encryption_key if encryption_key is not None else get_settings().token_encryption_key
    if not key:
        raise CredentialVaultConfigurationError(
            "LUMEN_TOKEN_ENCRYPTION_KEY is required for credential vault secret access."
        )
    return key


def _available_slots(fields: Mapping[CredentialSlot, str]) -> list[CredentialSlot]:
    return [slot for slot in ALLOWED_SLOTS if slot in fields]


def _sanitize_available_slots(raw_slots: Any) -> list[CredentialSlot]:
    if not isinstance(raw_slots, list):
        return []
    present = set(raw_slots)
    return [slot for slot in ALLOWED_SLOTS if slot in present]


def _owned_item(
    db: Session,
    *,
    user_id: int,
    item_id: int,
) -> models.CredentialVaultItem:
    item = db.scalars(
        select(models.CredentialVaultItem).where(
            models.CredentialVaultItem.id == item_id,
            models.CredentialVaultItem.user_id == user_id,
        )
    ).first()
    if item is None:
        raise CredentialVaultNotFoundError("Credential vault item not found.")
    return item


def _flush_account_identity_write(db: Session) -> None:
    try:
        db.flush()
    except IntegrityError as exc:
        raise CredentialVaultConflictError(
            "Credentials for this exact site and account label already exist. "
            "Choose a different account label or explicitly update the existing entry."
        ) from exc


def _assert_item_available_for_url(
    item: models.CredentialVaultItem,
    *,
    url: str,
    require_auto_use: bool,
) -> None:
    if item.revoked_at is not None:
        raise CredentialVaultAccessError("Credential vault item has been revoked.")
    if require_auto_use and not item.auto_use:
        raise CredentialVaultAccessError("Credential vault item is not enabled for automatic use.")
    if origin_from_url(url) != item.origin:
        raise CredentialVaultAccessError("Credential vault item does not match the current origin.")


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
