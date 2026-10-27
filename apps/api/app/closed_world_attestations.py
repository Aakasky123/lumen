"""Server-owned closed-world set attestations stored inside ``UserProfile.profile_json``.

The create endpoint is itself the user's completeness attestation. Clients provide only the
set identity and its members; IDs, exhaustive trust, provenance, lifecycle, and timestamps are
server-authored. Ordinary profile writes must preserve this protected slice verbatim.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models


CLOSED_WORLD_ATTESTATIONS_KEY = "closed_world_attestations"
SERVER_STAMP = "lumen.closed_world_attestation.v1"
MAX_ATTESTATIONS = 64
MAX_MEMBERS = 100
MAX_MEMBER_LENGTH = 240
MAX_CANONICAL_VALUE_LENGTH = 900
SUPPORTED_SETS = {("employment_history", "employers")}


class ClosedWorldAttestationError(ValueError):
    pass


class ClosedWorldAttestationNotFoundError(ClosedWorldAttestationError):
    pass


def preserve_server_owned_profile_json(
    existing_profile_json: Any,
    incoming_profile_json: Any,
) -> dict[str, Any]:
    """Merge an ordinary profile write without accepting attestation data from the client."""

    existing = dict(existing_profile_json) if isinstance(existing_profile_json, dict) else {}
    incoming = dict(incoming_profile_json) if isinstance(incoming_profile_json, dict) else {}
    incoming.pop(CLOSED_WORLD_ATTESTATIONS_KEY, None)
    if CLOSED_WORLD_ATTESTATIONS_KEY in existing:
        incoming[CLOSED_WORLD_ATTESTATIONS_KEY] = existing[
            CLOSED_WORLD_ATTESTATIONS_KEY
        ]
    return incoming


def list_for_user(
    db: Session,
    *,
    user_id: int,
) -> list[dict[str, Any]]:
    profile = _profile_for_user(db, user_id=user_id, create=False)
    if profile is None:
        return []
    items = _trusted_items(profile.profile_json)
    items.sort(
        key=lambda item: (
            str(item.get("updated_at") or ""),
            str(item.get("id") or ""),
        ),
        reverse=True,
    )
    return [_public_item(item) for item in items]


def create(
    db: Session,
    *,
    user_id: int,
    scope: str,
    set_key: str,
    members: list[str],
) -> dict[str, Any]:
    normalized_scope, normalized_set_key = _supported_set(scope, set_key)
    normalized_members = _normalize_members(members)
    profile = _profile_for_user(db, user_id=user_id, create=True)
    assert profile is not None
    profile_json = dict(profile.profile_json or {})
    raw_items = list(profile_json.get(CLOSED_WORLD_ATTESTATIONS_KEY) or [])
    now = models.utcnow().isoformat()
    trusted = _trusted_items(profile_json)
    next_version = 1 + max(
        (
            int(item.get("version") or 0)
            for item in trusted
            if item["scope"] == normalized_scope
            and item["set_key"] == normalized_set_key
        ),
        default=0,
    )
    revoked_ids: set[str] = set()
    for item in trusted:
        if (
            item["scope"] == normalized_scope
            and item["set_key"] == normalized_set_key
            and item["status"] == "active"
        ):
            revoked_ids.add(item["id"])
    rewritten: list[Any] = []
    for raw in raw_items:
        if not isinstance(raw, dict) or str(raw.get("id") or "") not in revoked_ids:
            rewritten.append(raw)
            continue
        rewritten.append(
            {
                **raw,
                "status": "revoked",
                "revoked_at": now,
                "updated_at": now,
            }
        )
    item = {
        "_server_stamp": SERVER_STAMP,
        "id": str(uuid4()),
        "scope": normalized_scope,
        "set_key": normalized_set_key,
        "members": normalized_members,
        "exhaustive": True,
        "status": "active",
        "attested_by": "user",
        "source": "profile_attestation_api",
        "version": next_version,
        "created_at": now,
        "updated_at": now,
        "revoked_at": None,
    }
    _context_record(item)
    rewritten.append(item)
    profile_json[CLOSED_WORLD_ATTESTATIONS_KEY] = rewritten[-MAX_ATTESTATIONS:]
    profile.profile_json = profile_json
    profile.updated_at = models.utcnow()
    db.flush()
    return _public_item(item)


def revoke(
    db: Session,
    *,
    user_id: int,
    attestation_id: str,
) -> dict[str, Any]:
    normalized_id = str(attestation_id or "").strip()
    profile = _profile_for_user(db, user_id=user_id, create=False)
    if profile is None:
        raise ClosedWorldAttestationNotFoundError("Closed-world attestation not found.")
    profile_json = dict(profile.profile_json or {})
    raw_items = list(profile_json.get(CLOSED_WORLD_ATTESTATIONS_KEY) or [])
    trusted_by_id = {item["id"]: item for item in _trusted_items(profile_json)}
    trusted = trusted_by_id.get(normalized_id)
    if trusted is None:
        raise ClosedWorldAttestationNotFoundError("Closed-world attestation not found.")
    if trusted["status"] == "revoked":
        return _public_item(trusted)
    now = models.utcnow().isoformat()
    rewritten: list[Any] = []
    updated: dict[str, Any] | None = None
    for raw in raw_items:
        if not isinstance(raw, dict) or str(raw.get("id") or "") != normalized_id:
            rewritten.append(raw)
            continue
        updated = {
            **raw,
            "status": "revoked",
            "revoked_at": now,
            "updated_at": now,
        }
        rewritten.append(updated)
    if updated is None:
        raise ClosedWorldAttestationNotFoundError("Closed-world attestation not found.")
    profile_json[CLOSED_WORLD_ATTESTATIONS_KEY] = rewritten
    profile.profile_json = profile_json
    profile.updated_at = models.utcnow()
    db.flush()
    return _public_item(updated)


def active_for_candidate_context(
    profile_json: Any,
    *,
    limit: int = 16,
) -> list[dict[str, Any]]:
    """Return active trusted records atomically, ahead of lossy profile-memory compaction."""

    active = [
        item for item in _trusted_items(profile_json) if item["status"] == "active"
    ]
    active.sort(
        key=lambda item: (
            str(item.get("updated_at") or ""),
            str(item.get("id") or ""),
        ),
        reverse=True,
    )
    serialized: list[dict[str, Any]] = []
    for item in active[: max(0, int(limit))]:
        try:
            context_record = _context_record(item)
        except ClosedWorldAttestationError:
            continue
        serialized.append(
            {
                "attestation_id": item["id"],
                "context_key": context_record["key"],
                "canonical_value": context_record["value"],
                "attestation_sha256": context_record["attestation_sha256"],
                "scope": item["scope"],
                "set_key": item["set_key"],
                "members": list(item["members"]),
                "exhaustive": True,
                "status": "active",
                "attested_by": "user",
                "source": "profile_attestation_api",
                "server_stamped": True,
                "revocable": True,
                "version": item["version"],
                "attested_at": item["created_at"],
                "complete_through": item["created_at"],
                "updated_at": item["updated_at"],
            }
        )
    return serialized


def profile_memory_records(profile_json: Any, *, limit: int = 16) -> list[dict[str, str]]:
    """Project each active attestation as one prioritized, canonical profile-memory fact."""

    return [
        {
            "key": item["context_key"],
            "value": item["canonical_value"],
            "source": (
                "user_profiles.profile_json.closed_world_attestations."
                f"{item['attestation_id']}"
            ),
        }
        for item in active_for_candidate_context(profile_json, limit=limit)
    ]


def _profile_for_user(
    db: Session,
    *,
    user_id: int,
    create: bool,
) -> models.UserProfile | None:
    profile = db.scalars(
        select(models.UserProfile)
        .where(models.UserProfile.user_id == user_id)
        .with_for_update()
    ).first()
    if profile is None and create:
        profile = models.UserProfile(user_id=user_id, profile_json={})
        db.add(profile)
        db.flush()
    return profile


def _supported_set(scope: str, set_key: str) -> tuple[str, str]:
    normalized = (
        str(scope or "").strip().lower(),
        str(set_key or "").strip().lower(),
    )
    if normalized not in SUPPORTED_SETS:
        raise ClosedWorldAttestationError(
            "Unsupported closed-world set. The current contract supports "
            "scope=employment_history and set_key=employers."
        )
    return normalized


def _normalize_members(raw_members: Any) -> list[str]:
    if not isinstance(raw_members, list) or len(raw_members) > MAX_MEMBERS:
        raise ClosedWorldAttestationError(
            f"members must be a list containing at most {MAX_MEMBERS} values."
        )
    members: list[str] = []
    seen: set[str] = set()
    for raw in raw_members:
        member = re.sub(r"\s+", " ", str(raw or "")).strip()
        if not member:
            raise ClosedWorldAttestationError("members cannot contain an empty value.")
        if len(member) > MAX_MEMBER_LENGTH:
            raise ClosedWorldAttestationError(
                f"Each member must be at most {MAX_MEMBER_LENGTH} characters."
            )
        signature = member.casefold()
        if signature in seen:
            continue
        seen.add(signature)
        members.append(member)
    return members


def _trusted_items(profile_json: Any) -> list[dict[str, Any]]:
    if not isinstance(profile_json, dict):
        return []
    raw_items = profile_json.get(CLOSED_WORLD_ATTESTATIONS_KEY)
    if not isinstance(raw_items, list):
        return []
    items: list[dict[str, Any]] = []
    for raw in raw_items[-MAX_ATTESTATIONS:]:
        if not isinstance(raw, dict) or raw.get("_server_stamp") != SERVER_STAMP:
            continue
        try:
            scope, set_key = _supported_set(raw.get("scope"), raw.get("set_key"))
            members = _normalize_members(raw.get("members"))
            version = max(1, int(raw.get("version") or 1))
        except (ClosedWorldAttestationError, TypeError, ValueError):
            continue
        status = str(raw.get("status") or "").strip().lower()
        if (
            not str(raw.get("id") or "").strip()
            or raw.get("exhaustive") is not True
            or status not in {"active", "revoked"}
            or raw.get("attested_by") != "user"
            or raw.get("source") != "profile_attestation_api"
            or not str(raw.get("created_at") or "").strip()
            or not str(raw.get("updated_at") or "").strip()
        ):
            continue
        items.append(
            {
                **raw,
                "id": str(raw["id"]),
                "scope": scope,
                "set_key": set_key,
                "members": members,
                "version": version,
                "status": status,
            }
        )
    return items


def _public_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(item.get("id") or ""),
        "scope": str(item.get("scope") or ""),
        "set_key": str(item.get("set_key") or ""),
        "members": list(item.get("members") or []),
        "exhaustive": item.get("exhaustive") is True,
        "status": str(item.get("status") or ""),
        "attested_by": str(item.get("attested_by") or ""),
        "source": str(item.get("source") or ""),
        "version": max(1, int(item.get("version") or 1)),
        "created_at": str(item.get("created_at") or ""),
        "updated_at": str(item.get("updated_at") or ""),
        "revoked_at": (
            str(item.get("revoked_at")) if item.get("revoked_at") else None
        ),
    }


def _context_record(item: dict[str, Any]) -> dict[str, str]:
    payload = {
        "attestation_id": str(item.get("id") or ""),
        "subject": str(item.get("scope") or ""),
        "set_key": str(item.get("set_key") or ""),
        "set_semantics": "complete_membership",
        "members": list(item.get("members") or []),
        "complete_through": str(item.get("created_at") or ""),
        "membership_answer": "Yes",
        "absence_answer": "No",
        "exhaustive": True,
        "provenance": "explicit_user_attestation",
        "source": "profile_attestation_api",
        "version": max(1, int(item.get("version") or 1)),
    }
    payload_bytes = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    attestation_sha256 = hashlib.sha256(payload_bytes).hexdigest()
    value = json.dumps(
        {**payload, "attestation_sha256": attestation_sha256},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if len(value) > MAX_CANONICAL_VALUE_LENGTH:
        raise ClosedWorldAttestationError(
            "The complete member set is too large for the bounded attestation context."
        )
    return {
        "key": f"closed_world_attestation.{payload['attestation_id']}",
        "value": value,
        "attestation_sha256": attestation_sha256,
    }
