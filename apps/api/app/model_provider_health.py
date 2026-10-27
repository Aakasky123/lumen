from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.model_provider import ModelProviderService, call_model_json, model_provider_health
from app.runtime import OperatorRuntimeEngine


_CACHE_TTL_SECONDS = 300
_CACHE: dict[str, Any] | None = None


def get_model_provider_health(*, db: Session | None = None, refresh: bool = False, probe: bool = True) -> dict[str, Any]:
    """Return a small, cached model-provider validation report.

    The probe intentionally sends no candidate, job, resume, or application data.
    """

    global _CACHE
    now = datetime.now(timezone.utc)
    if not refresh and _CACHE and _cache_is_fresh(_CACHE, now) and (not probe or _CACHE.get("probed")):
        return {**_CACHE, "cached": True}

    report = model_provider_health(db, refresh=refresh, probe=probe)
    # The top-level fields describe the active text provider; `vision` reports the
    # independent browser-driving provider.
    service = ModelProviderService(db)
    vision = None
    if db is not None:
        try:
            vision_user = OperatorRuntimeEngine(db).ensure_default_user()
            vision = service.vision_provider(user_id=vision_user.id)
        except Exception:
            vision = None
    active_id = report.get("active_provider_id")
    if vision is not None and vision.id != active_id:
        section = {"provider": vision.provider_type, "model": vision.model_name, "status": "unknown", "latency_ms": None, "error": vision.last_error}
        if probe:
            started = datetime.now(timezone.utc)
            try:
                result = call_model_json(vision, purpose="model_provider_health", system="Return strict JSON only.", prompt={"ok": True}, schema=None)
                section["latency_ms"] = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
                section["status"] = "healthy" if result.get("ok") else "degraded"
                section["error"] = None if result.get("ok") else str(result.get("error") or "vision probe failed")
            except Exception as exc:  # fail closed
                section["latency_ms"] = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
                section["status"], section["error"] = "degraded", str(exc)[:240]
        report["vision"] = section
        if section["status"] == "degraded" or (section["status"] == "unknown" and probe):
            report["status"] = "degraded"
        elif report.get("validated") is True:
            report["status"] = "healthy"
    elif vision is None:
        report["vision"] = {"provider": None, "model": None, "status": "unconfigured", "latency_ms": None, "error": None}
    report["cached_at"] = now.isoformat()
    _CACHE = report
    return report


def clear_model_provider_health_cache() -> None:
    global _CACHE
    _CACHE = None


def _cache_is_fresh(cache: dict[str, Any], now: datetime) -> bool:
    cached_at = cache.get("cached_at")
    if not cached_at:
        return False
    try:
        cached = datetime.fromisoformat(str(cached_at))
    except ValueError:
        return False
    if cached.tzinfo is None:
        cached = cached.replace(tzinfo=timezone.utc)
    return now - cached <= timedelta(seconds=_CACHE_TTL_SECONDS)

