from __future__ import annotations

import re
from pathlib import Path


NO_ARTIFACT_ROLE_VALUES = {"", "none", "unknown", "n/a", "na", "null"}


def normalize_artifact_role(value: object, *, default: str = "document") -> str:
    """Normalize a model/user supplied artifact role without classifying it."""
    raw = re.sub(r"\s+", " ", str(value or "")).strip().lower()
    if raw in NO_ARTIFACT_ROLE_VALUES:
        raw = default
    role = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    return (role or default)[:64]


def artifact_display_name(*, filename: object = "", display_name: object = "", artifact_kind: object = "") -> str:
    explicit = re.sub(r"\s+", " ", str(display_name or "")).strip()
    if explicit:
        return explicit[:120]
    stem = Path(str(filename or "")).stem.strip()
    if stem:
        return stem[:120]
    role = normalize_artifact_role(artifact_kind, default="artifact")
    return role.replace("_", " ").title()
