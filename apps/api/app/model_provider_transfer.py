"""Move model providers and their role assignments from one Lumen install to another.

Provider rows live in each install's own database with API keys encrypted under that install's
LUMEN_TOKEN_ENCRYPTION_KEY, so a fresh install (a second machine) starts with none. This exports
every provider row plus which row fills each seat (Brain, Checker, Escalation)
into one bundle whose API keys are sealed under a passphrase, and imports it on the other install,
re-encrypting the keys under that install's own key and pointing the roles at the new row ids.

    python -m app.model_provider_transfer export providers.lumen
    python -m app.model_provider_transfer import providers.lumen [--dry-run]
    python -m app.model_provider_transfer set-key --provider-type openrouter

The passphrase comes from LUMEN_TRANSFER_PASSPHRASE or an interactive prompt. A bundle with no sealed keys
(no salt) imports without one; `set-key` then stores one API key, read at a hidden prompt, on every row of a
provider type, so a key never has to travel between machines. Run from apps/api.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.agent_settings import get_agent_settings, update_agent_settings
from app.config import get_settings
from app.security import decrypt_secret, encrypt_secret

BUNDLE_FORMAT = "lumen-model-providers/1"
# The three seats: Brain, Escalation and Checker.
ROLE_KEYS = (
    "vision_provider_id",
    "vision_escalation_provider_id",
    "judge_provider_id",
)
# Seats an older bundle may name that the Checker now fills, in the order a bundle without a Checker adopts them.
LEGACY_CHECKER_ROLE_KEYS = ("secret_adjudicator_provider_id", "grounding_judge_provider_id")
COPIED_FIELDS = (
    "display_name",
    "provider_type",
    "model_name",
    "base_url",
    "api_key_hint",
    "temperature",
    "max_tokens",
    "timeout_seconds",
    "reasoning_effort",
    "structured_output",
    "endpoint_preference",
    "endpoint_routing",
    "enabled",
    "is_active",
)


def _bundle_fernet(passphrase: str, salt: bytes) -> Fernet:
    key = hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=2**15, r=8, p=1, maxmem=64 * 1024 * 1024, dklen=32)
    return Fernet(base64.urlsafe_b64encode(key))


def export_bundle(db: Session, user_id: int, *, token_key: str | None, passphrase: str) -> dict[str, Any]:
    salt = secrets.token_bytes(16)
    sealer = _bundle_fernet(passphrase, salt)
    rows = list(db.scalars(select(models.ModelProvider).order_by(models.ModelProvider.id)).all())
    providers = []
    for row in rows:
        item = {field: getattr(row, field) for field in COPIED_FIELDS}
        item["source_id"] = row.id
        item["sealed_api_key"] = None
        if row.encrypted_api_key:
            if not token_key:
                raise ValueError("LUMEN_TOKEN_ENCRYPTION_KEY is not set, so stored API keys cannot be read.")
            try:
                raw = decrypt_secret(row.encrypted_api_key, token_key)
            except InvalidToken as exc:
                raise ValueError(f"Provider {row.id} ({row.display_name}) key does not decrypt with this install's key.") from exc
            item["sealed_api_key"] = sealer.encrypt(raw.encode("utf-8")).decode("utf-8")
        providers.append(item)
    agent_settings = get_agent_settings(db, user_id)
    roles = {key: agent_settings.get(key) for key in ROLE_KEYS if agent_settings.get(key) is not None}
    return {
        "format": BUNDLE_FORMAT,
        "salt": base64.b64encode(salt).decode("ascii"),
        "providers": providers,
        "roles": roles,
    }


def import_bundle(db: Session, user_id: int, bundle: dict[str, Any], *, token_key: str | None, passphrase: str) -> list[str]:
    """Upsert each provider (matched on type + model + base url), then point the roles at the new ids.
    Returns a line per change. The caller commits (or rolls back for a dry run)."""
    if bundle.get("format") != BUNDLE_FORMAT:
        raise ValueError("Not a Lumen model-provider bundle.")
    providers = bundle.get("providers") or []
    sealer = None
    if any(item.get("sealed_api_key") for item in providers):
        if not token_key:
            raise ValueError("Set LUMEN_TOKEN_ENCRYPTION_KEY in this install's .env before importing API keys.")
        if not bundle.get("salt"):
            raise ValueError("The bundle has sealed keys but no salt.")
        sealer = _bundle_fernet(passphrase or "", base64.b64decode(bundle["salt"]))
    report: list[str] = []
    id_map: dict[int, int] = {}
    for item in providers:
        raw_key = None
        if item.get("sealed_api_key") and sealer is not None:
            try:
                raw_key = sealer.decrypt(item["sealed_api_key"].encode("utf-8")).decode("utf-8")
            except InvalidToken as exc:
                raise ValueError("Wrong passphrase for this bundle.") from exc
        existing = db.scalar(
            select(models.ModelProvider).where(
                models.ModelProvider.provider_type == item["provider_type"],
                models.ModelProvider.model_name == item["model_name"],
                models.ModelProvider.base_url.is_(None) if item.get("base_url") is None else models.ModelProvider.base_url == item["base_url"],
            )
        )
        row = existing or models.ModelProvider()
        for field in COPIED_FIELDS:
            setattr(row, field, item.get(field))
        if raw_key is not None:
            row.encrypted_api_key = encrypt_secret(raw_key, token_key or "")
        row.health_status = "unknown"
        row.last_error = None
        db.add(row)
        db.flush()
        id_map[int(item["source_id"])] = row.id
        report.append(f"{'updated' if existing else 'added'} provider {row.id}: {row.display_name} ({row.provider_type} {row.model_name})")
    bundle_roles = dict(bundle.get("roles") or {})
    if bundle_roles.get("judge_provider_id") is None:
        # An older bundle names the Checker's jobs as separate seats: the first one set becomes the Checker.
        for legacy_key in LEGACY_CHECKER_ROLE_KEYS:
            if bundle_roles.get(legacy_key) is not None:
                bundle_roles["judge_provider_id"] = bundle_roles[legacy_key]
                break
    roles = {}
    for key, source_id in bundle_roles.items():
        if key in ROLE_KEYS and source_id is not None and int(source_id) in id_map:
            roles[key] = id_map[int(source_id)]
            report.append(f"role {key} -> provider {roles[key]}")
    # The Brain is the active row; an older bundle's own active row is used only when it names no Brain.
    active_ids = [id_map[int(item["source_id"])] for item in providers if item.get("is_active")]
    if roles.get("vision_provider_id") is not None:
        active_ids = [roles["vision_provider_id"]]
    if active_ids:
        for row in db.scalars(select(models.ModelProvider)).all():
            row.is_active = row.id == active_ids[0]
    # The active row is set above (a row whose key travels separately may not be activatable yet).
    update_agent_settings(db, user_id, roles, activate_brain=False)
    return report


def bundle_has_keys(bundle: dict[str, Any]) -> bool:
    return any(item.get("sealed_api_key") for item in bundle.get("providers") or [])


def set_provider_key(db: Session, *, token_key: str | None, api_key: str, provider_type: str) -> list[str]:
    """Store one API key on every row of a provider type. The caller commits."""
    if not token_key:
        raise ValueError("Set LUMEN_TOKEN_ENCRYPTION_KEY in this install's .env before storing API keys.")
    if not api_key.strip():
        raise ValueError("An API key is required.")
    rows = list(
        db.scalars(
            select(models.ModelProvider)
            .where(models.ModelProvider.provider_type == provider_type)
            .order_by(models.ModelProvider.id)
        ).all()
    )
    if not rows:
        raise ValueError(f"No {provider_type} providers to set a key on.")
    hint = f"...{api_key.strip()[-4:]}"
    report = []
    for row in rows:
        row.encrypted_api_key = encrypt_secret(api_key.strip(), token_key)
        row.api_key_hint = hint
        row.health_status = "unknown"
        row.last_error = None
        report.append(f"key set on provider {row.id}: {row.display_name} ({row.model_name})")
    return report


def _passphrase(confirm: bool) -> str:
    value = os.environ.get("LUMEN_TRANSFER_PASSPHRASE")
    if value:
        return value
    value = getpass.getpass("Bundle passphrase: ")
    if confirm and getpass.getpass("Repeat passphrase: ") != value:
        raise SystemExit("Passphrases do not match.")
    if not value:
        raise SystemExit("A passphrase is required.")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export or import Lumen model providers and their roles.")
    sub = parser.add_subparsers(dest="command", required=True)
    export_cmd = sub.add_parser("export", help="write this install's providers to a passphrase-sealed bundle")
    export_cmd.add_argument("path", type=Path)
    import_cmd = sub.add_parser("import", help="add or update providers from a bundle and assign their roles")
    import_cmd.add_argument("path", type=Path)
    import_cmd.add_argument("--dry-run", action="store_true", help="show what would change without saving")
    key_cmd = sub.add_parser("set-key", help="store one API key (hidden prompt) on every row of a provider type")
    key_cmd.add_argument("--provider-type", required=True)
    args = parser.parse_args(argv)

    from app.db import SessionLocal, init_db
    from app.runtime import OperatorRuntimeEngine

    init_db()
    token_key = get_settings().token_encryption_key
    with SessionLocal() as db:
        user = OperatorRuntimeEngine(db).ensure_default_user()
        try:
            if args.command == "export":
                bundle = export_bundle(db, user.id, token_key=token_key, passphrase=_passphrase(confirm=True))
                args.path.write_text(json.dumps(bundle, indent=2), encoding="utf-8")
                print(f"Wrote {len(bundle['providers'])} providers and {len(bundle['roles'])} roles to {args.path}")
                return 0
            if args.command == "set-key":
                api_key = getpass.getpass(f"{args.provider_type} API key: ")
                report = set_provider_key(db, token_key=token_key, api_key=api_key, provider_type=args.provider_type)
                db.commit()
                print("\n".join(report))
                print("Saved. Restart the API so it picks up the key.")
                return 0
            bundle = json.loads(args.path.read_text(encoding="utf-8"))
            passphrase = _passphrase(confirm=False) if bundle_has_keys(bundle) else ""
            report = import_bundle(db, user.id, bundle, token_key=token_key, passphrase=passphrase)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print("\n".join(report))
        if args.dry_run:
            db.rollback()
            print("Dry run: nothing saved.")
        else:
            db.commit()
            print("Saved. Restart the API so it picks up the providers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
