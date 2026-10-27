"""Build the FormFactory probe's own Lumen stack data: a fresh database and data root, never the owner's.

    python -m app.agent_eval.open_bench.formfactory_stack build --data-root storage/formfactory-probe

Creates ``<data-root>/formfactory-probe.db`` through the app's own ``init_db`` (the schema comes from the
models, not from a copy of any database), then:

* the one test user is what the app itself creates for an empty database (``ensure_default_user``: a
  placeholder ``onboarding-*@example.com`` account named "Candidate"), so no real name or email exists;
* the ``model_providers`` rows are copied from ``--source-db`` (default storage/v6-orch.db) with their ids,
  because the Brain, Checker and Escalation seats are chosen by provider id and the keys are stored encrypted
  (readable only with the same ``LUMEN_TOKEN_ENCRYPTION_KEY``);
* the agent settings copied are only the provider choices and consents (see ``COPIED_SETTINGS``), plus
  Full autonomy (``final_action_mode = autonomous``); no user, run, chat, memory, résumé or vault row is read;
* ``<data-root>/stack.json`` records the user id and paths for the runner, which writes that user's memory
  markdown (``<data-root>/memory/user-<id>.md``) before every instance.

The data root MUST differ from the owner's (storage/): user ids are small integers and the memory file is
keyed by id, so a shared data root would put the synthetic profile over the owner's own memory file. Both
``build`` and the runner refuse a data root that is the repo's storage directory.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[5]
COPIED_SETTINGS = (
    "vision_provider_id", "vision_escalation_provider_id", "judge_provider_id", "strict_model_provider_allowlist",
    "autonomous_attestation_consent", "autonomous_relay_consent",
)
# Seats a source profile from before the three seats may name; the first one set fills a missing Checker.
LEGACY_CHECKER_SETTINGS = ("secret_adjudicator_provider_id", "grounding_judge_provider_id")
STACK_FILE = "stack.json"
DB_NAME = "formfactory-probe.db"


def refuse_owner_data_root(data_root: Path) -> None:
    owner = (REPO / "storage").resolve()
    if data_root.resolve() == owner:
        raise SystemExit(f"refusing: {data_root} is the owner's data root (its memory files would be overwritten)")


def _source_settings(source_db: Path) -> dict:
    """The settings JSON of the profile row that carries the most of COPIED_SETTINGS (the configured
    one), reading only those keys; nothing else of any user row leaves the source."""
    connection = sqlite3.connect(f"file:{source_db.as_posix()}?mode=ro", uri=True)
    try:
        best: dict = {}
        for (raw,) in connection.execute("SELECT agent_settings_json FROM user_profiles"):
            try:
                stored = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
            except (TypeError, ValueError):
                continue
            picked = {key: stored[key] for key in COPIED_SETTINGS if key in stored}
            if picked.get("judge_provider_id") is None:
                legacy = next((stored[key] for key in LEGACY_CHECKER_SETTINGS if stored.get(key) is not None), None)
                if legacy is not None:
                    picked["judge_provider_id"] = legacy
            if len(picked) > len(best):
                best = picked
        return best
    finally:
        connection.close()


def build(data_root: Path, source_db: Path, *, force: bool = False) -> dict:
    data_root = data_root.resolve()
    refuse_owner_data_root(data_root)
    db_path = data_root / DB_NAME
    if db_path.exists():
        if not force:
            raise SystemExit(f"{db_path} exists; pass --force to rebuild it")
        for suffix in ("", "-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)
    data_root.mkdir(parents=True, exist_ok=True)
    (data_root / "memory").mkdir(exist_ok=True)
    if "app.db" in sys.modules:
        raise SystemExit("build must run in a fresh process (app.db was already imported against another database)")
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    os.environ["LUMEN_DATA_ROOT"] = str(data_root)
    os.environ["LUMEN_ARTIFACT_ROOT"] = str(data_root / "artifacts")
    from sqlalchemy import text

    from app import models
    from app.agent_settings import update_agent_settings
    from app.db import SessionLocal, engine, init_db
    from app.runtime import OperatorRuntimeEngine

    init_db()
    source = sqlite3.connect(f"file:{source_db.as_posix()}?mode=ro", uri=True)
    try:
        source.row_factory = sqlite3.Row
        rows = [dict(row) for row in source.execute("SELECT * FROM model_providers ORDER BY id")]
    finally:
        source.close()
    mapped = {column.name for column in models.ModelProvider.__table__.columns}
    with engine.begin() as connection:
        for row in rows:
            row = {key: value for key, value in row.items() if key in mapped}
            columns = ", ".join(row)
            placeholders = ", ".join(f":{key}" for key in row)
            connection.execute(text(f"INSERT INTO model_providers ({columns}) VALUES ({placeholders})"), row)
    settings = _source_settings(source_db)
    with SessionLocal() as db:
        user = OperatorRuntimeEngine(db).ensure_default_user()
        update_agent_settings(db, user.id, {**settings, "final_action_mode": "autonomous"})
        db.commit()
        info = {"user_id": int(user.id), "user_email": user.email, "user_name": user.name}
    info.update({
        "data_root": str(data_root), "database": str(db_path), "providers_copied": [row["id"] for row in rows],
        "account_values": [info["user_email"], info["user_name"]],  # the grader separates values read off these
        "settings_copied": sorted(settings), "memory_file": str(data_root / "memory" / f"user-{info['user_id']}.md"),
    })
    (data_root / STACK_FILE).write_text(json.dumps(info, indent=2), encoding="utf-8")
    return info


def read_stack(data_root: Path) -> dict:
    refuse_owner_data_root(data_root)
    path = data_root / STACK_FILE
    if not path.is_file():
        raise SystemExit(f"{path} missing: run `python -m app.agent_eval.open_bench.formfactory_stack build` first")
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["build"])
    parser.add_argument("--data-root", default=str(REPO / "storage" / "formfactory-probe"))
    parser.add_argument("--source-db", default=str(REPO / "storage" / "v6-orch.db"))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    info = build(Path(args.data_root), Path(args.source_db), force=args.force)
    print(json.dumps(info, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
