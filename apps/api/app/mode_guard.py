from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.agent_settings import get_agent_settings
from app.model_provider_health import get_model_provider_health
from app.resume_selection import active_base_resume, selected_resume_artifact


PROFILE_PLACEHOLDER_OR_INCOMPLETE = "profile_placeholder_or_incomplete"
RESUME_ARTIFACT_UNAVAILABLE = "resume_artifact_unavailable"


@dataclass(frozen=True)
class LumenExecutionModeReport:
    """Readiness of the default user's model, profile, résumé, and approval mode."""

    model_mode: str
    profile_mode: str
    resume_mode: str
    final_action_mode: str
    warnings: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_mode_report(*, db: Session | None = None, include_model_probe: bool = False) -> LumenExecutionModeReport:
    model_health = get_model_provider_health(db=db, refresh=False, probe=include_model_probe)
    user = _default_operational_user(db) if db else None
    profile = _profile_readiness(db, user=user)
    resume = _resume_readiness(db, user=user)
    warnings: list[str] = []
    if model_health.get("validated") is False:
        warnings.append("model_provider_unvalidated")
    if model_health.get("live_enabled") is False:
        warnings.append("live_model_unavailable")
    if profile["mode"] != "complete":
        warnings.append(PROFILE_PLACEHOLDER_OR_INCOMPLETE)
    if resume["mode"] != "available":
        warnings.append(RESUME_ARTIFACT_UNAVAILABLE)
    return LumenExecutionModeReport(
        model_mode=_model_mode(model_health),
        profile_mode=profile["mode"],
        resume_mode=resume["mode"],
        final_action_mode=_final_action_mode(db, user=user),
        warnings=warnings,
        details={"profile": profile, "resume": resume, "model_health": model_health},
    )


def _model_mode(model_health: dict[str, Any]) -> str:
    if model_health.get("live_enabled") and model_health.get("validated") is True:
        return "live_model_validated"
    if model_health.get("live_enabled"):
        return "live_model_unvalidated"
    return "mock_or_deterministic"


def _final_action_mode(db: Session | None, *, user: models.User | None) -> str:
    if db is None or user is None:
        return "approval"
    configured = str(get_agent_settings(db, int(user.id)).get("final_action_mode") or "approval").strip().lower()
    return configured if configured in {"approval", "autonomous"} else "approval"


def _profile_readiness(db: Session | None, *, user: models.User | None) -> dict[str, Any]:
    if not db:
        return {"mode": "unknown", "missing": ["database_context_missing"], "placeholder": []}
    if not user:
        return {"mode": "incomplete", "missing": ["user"], "placeholder": []}
    profile = db.scalars(select(models.UserProfile).where(models.UserProfile.user_id == user.id).order_by(models.UserProfile.id.desc())).first()
    resume = active_base_resume(db, user.id)
    missing: list[str] = []
    placeholder: list[str] = []
    first, last = _split_name(user.name)
    if not first:
        missing.append("first_name")
    if not last:
        missing.append("last_name")
    if not _looks_real_email(user.email):
        placeholder.append("email")
    if not profile:
        missing.extend(["phone", "location", "linkedin", "work_authorization", "sponsorship_status", "salary_expectation"])
    else:
        profile_json = profile.profile_json or {}
        for field_name in ("phone", "location", "linkedin", "work_authorization", "sponsorship_status"):
            if not _meaningful(getattr(profile, field_name, None)):
                missing.append(field_name)
        salary = getattr(profile, "salary_expectation", None) or profile_json.get("salary_minimum_base_usd") or profile_json.get("salary_strategy")
        if not _meaningful_salary(salary):
            missing.append("salary_strategy_or_value")
    if not resume or not (_meaningful(resume.latex_source) or _meaningful(resume.base_resume_text) or _meaningful(resume.pdf_url)):
        missing.append("usable_resume_source")
    mode = "complete" if not missing and not placeholder else ("placeholder" if placeholder else "incomplete")
    return {"mode": mode, "user_id": user.id, "missing": _unique(missing), "placeholder": _unique(placeholder)}


def _resume_readiness(db: Session | None, *, user: models.User | None) -> dict[str, Any]:
    if not db:
        return {"mode": "unknown", "reason": "database_context_missing"}
    artifact = selected_resume_artifact(db, int(user.id)) if user else None
    if not artifact:
        return {"mode": "unavailable", "reason": "artifact_not_found"}
    path = Path(artifact.pdf_path or "")
    exists = bool(artifact.pdf_path and path.exists())
    if not exists or int(artifact.file_size or 0) <= 80:
        return {"mode": "unavailable", "artifact_id": artifact.id, "pdf_path": artifact.pdf_path, "exists": exists}
    return {"mode": "available", "artifact_id": artifact.id, "artifact_type": artifact.artifact_type, "pdf_path": artifact.pdf_path}


def _default_operational_user(db: Session) -> models.User | None:
    users = list(db.scalars(select(models.User).order_by(models.User.id.asc())).all())
    user = next((item for item in users if _looks_real_email(item.email)), None)
    if user:
        return user
    onboarding = next((item for item in users if str(item.email or "").lower().startswith("onboarding-") and str(item.email or "").lower().endswith("@example.com")), None)
    if onboarding:
        return onboarding
    return next((item for item in users if item.email in {"jordan@example.com", "candidate@example.com"}), None) or (users[0] if users else None)


def _split_name(name: str | None) -> tuple[str, str]:
    parts = [part for part in str(name or "").strip().split() if part]
    if not parts:
        return "", ""
    return parts[0], " ".join(parts[1:])


def _looks_real_email(email: str | None) -> bool:
    value = str(email or "").strip().lower()
    if not value or "@" not in value:
        return False
    if value.endswith("@example.com") or value.endswith("@test.com") or value in {"user@example.com", "test@example.com"}:
        return False
    return True


def _meaningful(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return text.lower() not in {"n/a", "na", "none", "unknown", "todo", "tbd", "placeholder"}


def _meaningful_salary(value: Any) -> bool:
    text = str(value or "").strip().lower()
    if not _meaningful(text):
        return False
    return text not in {"open", "open to discuss", "market", "market range", "negotiable"}


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result
