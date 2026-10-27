from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.model_context import model_context_window
from app.model_usage import normalize_usage
from app import models
from app.config import get_settings
from app.judge_cache import judge_wire_parts
from app.prompt_cache import split_system_cache_prefix, user_wire_parts
from app.security import decrypt_secret, encrypt_secret, redact_otp_codes


SUPPORTED_PROVIDER_TYPES = {"openai", "deepseek", "openrouter", "anthropic", "gemini", "custom_openai_compatible"}

# OpenRouter's normalized reasoning.effort tiers. Which tiers a model honors is the model's
# business (GPT-6 Luna: none..max); the provider row only stores a recognized tier.
REASONING_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
STRUCTURED_OUTPUT_MODES = ("strict", "json_object")
# An OpenRouter endpoint tag: provider slug, optionally "/variant" (openai/fast, azure/us).
_ENDPOINT_TAG = re.compile(r"[a-z0-9][a-z0-9._-]*(/[a-z0-9][a-z0-9._-]*)?")
_ROUTING_SORTS = ("latency", "throughput", "price")


def normalize_endpoint_routing(value: Any) -> dict[str, Any] | None:
    """A row's OpenRouter routing knobs, or None when it sets none. Accepts a dict or its JSON text.
    allow_fallbacks (bool): False pins the row's endpoint_preference and fails instead of rerouting;
    require_parameters (bool, default True on the wire): no upstream that would silently ignore
    response_format/reasoning serves the call; sort: latency | throughput | price."""
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.strip() else None
        except ValueError:
            return None
    if not isinstance(value, dict):
        return None
    config: dict[str, Any] = {}
    for key in ("allow_fallbacks", "require_parameters"):
        if isinstance(value.get(key), bool):
            config[key] = value[key]
    if str(value.get("sort") or "").strip().lower() in _ROUTING_SORTS:
        config["sort"] = str(value["sort"]).strip().lower()
    return config or None


def _routing_column(value: Any) -> str | None:
    config = normalize_endpoint_routing(value)
    return json.dumps(config, sort_keys=True) if config else None


def _requires_parameters(provider: "ResolvedModelProvider") -> bool:
    """Default on: an upstream that cannot honor response_format is filtered out, never trusted."""
    return (normalize_endpoint_routing(provider.endpoint_routing) or {}).get("require_parameters", True)

# Eval-only per-call transport overrides (agent_eval.judge_replay legs). Never set in production, so
# every production call behaves exactly as without them. reasoning_enabled replaces the purpose's
# reasoning switch on an OpenRouter-style chat call; reasoning replaces an OpenRouter call's reasoning
# block outright (e.g. {"max_tokens": 1024} in place of the row's effort); routing is merged over the
# row's OpenRouter routing (require_parameters is never taken from it: that guard keeps its own semantics).
_EVAL_TRANSPORT_OVERRIDES: ContextVar[dict[str, Any] | None] = ContextVar("lumen_eval_transport_overrides", default=None)
EVAL_ROUTING_KEYS = ("order", "only", "ignore", "sort", "allow_fallbacks")


@contextmanager
def eval_transport_overrides(
    *,
    reasoning_enabled: bool | None = None,
    reasoning: dict[str, Any] | None = None,
    routing: dict[str, Any] | None = None,
) -> Iterator[None]:
    """Apply replay-leg overrides to model calls made in this context (this thread) only."""
    token = _EVAL_TRANSPORT_OVERRIDES.set(
        {
            "reasoning_enabled": reasoning_enabled,
            "reasoning": dict(reasoning) if reasoning else None,
            "routing": {key: value for key, value in (routing or {}).items() if key in EVAL_ROUTING_KEYS},
        }
    )
    try:
        yield
    finally:
        _EVAL_TRANSPORT_OVERRIDES.reset(token)


DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com",
    "openrouter": "https://openrouter.ai/api/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "custom_openai_compatible": "",
}

@dataclass(frozen=True)
class ResolvedModelProvider:
    id: int | None
    display_name: str
    provider_type: str
    model_name: str
    base_url: str
    api_key: str | None
    temperature: float
    max_tokens: int
    timeout_seconds: int
    enabled: bool
    is_active: bool
    source: str
    health_status: str = "unknown"
    last_error: str | None = None
    reasoning_effort: str | None = None
    structured_output: str | None = None
    endpoint_preference: str | None = None
    # OpenRouter routing knobs for this row (see normalize_endpoint_routing); None = defaults.
    endpoint_routing: dict[str, Any] | None = None
    # Runtime only, never stored: an OpenRouter host this one request must skip (a hedged decision
    # avoiding the row's pinned host); OpenRouter then routes by throughput, capped at this output
    # price (USD per million tokens) when it is known.
    endpoint_avoid: str | None = None
    endpoint_max_completion_price: float | None = None
    # Runtime only, never stored: OpenRouter hosts this one request must not use in any routing mode
    # (the retry of a reply one host cut off at the token cap skips that host).
    endpoint_skip: tuple[str, ...] = ()

    @property
    def live_enabled(self) -> bool:
        return bool(self.enabled and self.api_key and self.model_name and self.provider_type in SUPPORTED_PROVIDER_TYPES)

    @property
    def selected_from_ui(self) -> bool:
        return self.source == "database"

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "provider_type": self.provider_type,
            "model_name": self.model_name,
            "base_url": self.base_url,
            "api_key_masked": _mask_secret(self.api_key),
            "has_api_key": bool(self.api_key),
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "timeout_seconds": self.timeout_seconds,
            "enabled": self.enabled,
            "is_active": self.is_active,
            "source": self.source,
            "health_status": self.health_status,
            "last_error": self.last_error,
            "reasoning_effort": self.reasoning_effort,
            "structured_output": self.structured_output or "strict",
            "endpoint_preference": self.endpoint_preference,
            "endpoint_routing": self.endpoint_routing,
        }


class ModelProviderService:
    def __init__(self, db: Session | None = None):
        self.db = db
        self.settings = get_settings()

    def list_public(self) -> dict[str, Any]:
        items = []
        if self.db:
            items = [self.public_provider(provider) for provider in self._providers()]
        active = self.active_provider().public_dict()
        env_fallback = self.env_fallback_provider().public_dict()
        return {"items": items, "active": active, "env_fallback": env_fallback, "supported_provider_types": sorted(SUPPORTED_PROVIDER_TYPES)}

    def public_provider(self, provider: models.ModelProvider) -> dict[str, Any]:
        return {
            "id": provider.id,
            "display_name": provider.display_name,
            "provider_type": provider.provider_type,
            "model_name": provider.model_name,
            "base_url": provider.base_url or DEFAULT_BASE_URLS.get(provider.provider_type, ""),
            "api_key_masked": provider.api_key_hint or "",
            "has_api_key": bool(provider.encrypted_api_key),
            "temperature": provider.temperature,
            "max_tokens": provider.max_tokens,
            "timeout_seconds": provider.timeout_seconds,
            "enabled": bool(provider.enabled),
            "is_active": bool(provider.is_active),
            "health_status": provider.health_status,
            "last_error": provider.last_error,
            "reasoning_effort": provider.reasoning_effort,
            "structured_output": provider.structured_output or "strict",
            "endpoint_preference": provider.endpoint_preference,
            "endpoint_routing": normalize_endpoint_routing(provider.endpoint_routing),
            "last_tested_at": provider.last_tested_at.isoformat() if provider.last_tested_at else None,
            "created_at": provider.created_at.isoformat() if provider.created_at else None,
            "updated_at": provider.updated_at.isoformat() if provider.updated_at else None,
            "source": "database",
        }

    @staticmethod
    def _normalized_reasoning_effort(value: Any) -> str | None:
        effort = str(value or "").strip().lower()
        return effort if effort in REASONING_EFFORTS else None

    @staticmethod
    def _normalized_structured_output(value: Any) -> str | None:
        mode = str(value or "").strip().lower()
        return mode if mode in STRUCTURED_OUTPUT_MODES else None

    @staticmethod
    def _normalized_endpoint_preference(value: Any) -> str | None:
        tag = str(value or "").strip().lower()
        return tag if _ENDPOINT_TAG.fullmatch(tag) else None

    def create_provider(self, payload: dict[str, Any]) -> models.ModelProvider:
        self._require_db()
        provider_type = self._provider_type(payload.get("provider_type"))
        api_key = str(payload.get("api_key") or "").strip()
        base_url = str(payload.get("base_url") or DEFAULT_BASE_URLS.get(provider_type, "")).strip()
        if not api_key and payload.get("key_from_provider_id") is not None:
            api_key = self._saved_key_for(payload.get("key_from_provider_id"), provider_type, base_url)
        provider = models.ModelProvider(
            display_name=str(payload.get("display_name") or "").strip() or _default_display_name(provider_type, str(payload.get("model_name") or "")),
            provider_type=provider_type,
            model_name=str(payload.get("model_name") or "").strip(),
            base_url=base_url,
            encrypted_api_key=self._encrypt_api_key(api_key) if api_key else None,
            api_key_hint=_mask_secret(api_key) if api_key else "",
            temperature=_bounded_float(payload.get("temperature"), default=0.2, low=0.0, high=2.0),
            max_tokens=_bounded_int(payload.get("max_tokens"), default=2048, low=1, high=200000),
            timeout_seconds=_bounded_int(payload.get("timeout_seconds"), default=30, low=1, high=300),
            reasoning_effort=self._normalized_reasoning_effort(payload.get("reasoning_effort")),
            structured_output=self._normalized_structured_output(payload.get("structured_output")),
            endpoint_preference=self._normalized_endpoint_preference(payload.get("endpoint_preference")),
            endpoint_routing=_routing_column(payload.get("endpoint_routing")),
            enabled=bool(payload.get("enabled", True)),
            is_active=False,
            health_status="unknown",
            created_at=models.utcnow(),
            updated_at=models.utcnow(),
        )
        self._validate_provider(provider)
        self.db.add(provider)
        self.db.flush()
        return provider

    def activate_provider(self, provider_id: int) -> models.ModelProvider:
        provider = self._provider(provider_id)
        if not provider.enabled:
            raise ValueError("Cannot activate a disabled model provider.")
        if not provider.encrypted_api_key:
            raise ValueError("Cannot activate a model provider without an API key.")
        self._validate_provider(provider)
        for item in self._providers():
            item.is_active = item.id == provider.id
            item.updated_at = models.utcnow()
        self.db.flush()
        return provider

    def available_models(
        self,
        *,
        provider_type: str = "",
        api_key: str = "",
        base_url: str = "",
        key_from_provider_id: int | None = None,
    ) -> list[str]:
        """Models a not-yet-saved provider offers for the key in the request (or a saved provider's
        key, used server-side). Powers the model-selection dropdown."""
        provider_type = self._provider_type(provider_type)  # validates the type
        if not str(api_key or "").strip() and key_from_provider_id is not None:
            resolved_base = str(base_url or DEFAULT_BASE_URLS.get(provider_type, "")).strip()
            api_key = self._saved_key_for(key_from_provider_id, provider_type, resolved_base)
        return list_available_models(provider_type=provider_type, api_key=api_key, base_url=base_url)

    def _saved_key_for(self, provider_id: Any, provider_type: str, base_url: str) -> str:
        """The decrypted key of a saved provider on the same service (same type and base URL)."""
        try:
            source = self._provider(int(provider_id))
        except (TypeError, ValueError) as exc:
            raise ValueError("Unknown model provider for the saved key.") from exc
        same_service = (
            str(source.provider_type or "").strip().casefold() == str(provider_type or "").strip().casefold()
            and str(source.base_url or "").strip().rstrip("/").casefold()
            == str(base_url or "").strip().rstrip("/").casefold()
        )
        if not same_service:
            raise ValueError("A saved key can only be reused for another model on the same service.")
        key = self._decrypt_api_key(source)
        if not key:
            raise ValueError("That provider has no saved key.")
        return key

    def active_provider(self) -> ResolvedModelProvider:
        if self.db:
            provider = self.db.scalars(
                select(models.ModelProvider)
                .where(models.ModelProvider.is_active.is_(True), models.ModelProvider.enabled.is_(True))
                .order_by(models.ModelProvider.id.desc())
                .limit(1)
            ).first()
            if provider:
                return self.resolve_provider(provider)
        return self.env_fallback_provider()

    def provider_by_id(self, provider_id: int | None) -> ResolvedModelProvider | None:
        if not provider_id or not self.db:
            return None
        provider = self.db.get(models.ModelProvider, int(provider_id))
        if not provider or not provider.enabled:
            return None
        resolved = self.resolve_provider(provider)
        return resolved if resolved.live_enabled else None

    def strong_secondary_provider(self, *, exclude_id: int | None = None) -> ResolvedModelProvider | None:
        """Auto-pick a stronger enabled provider (model name signalling a pro / reasoner
        tier) to escalate to."""
        if not self.db:
            return None
        rows = self.db.scalars(
            select(models.ModelProvider).where(models.ModelProvider.enabled.is_(True)).order_by(models.ModelProvider.id.desc())
        ).all()
        for provider in rows:
            if exclude_id is not None and provider.id == exclude_id:
                continue
            name = str(provider.model_name or "").lower()
            if "pro" in name or "reasoner" in name:
                resolved = self.resolve_provider(provider)
                if resolved.live_enabled:
                    return resolved
        return None

    def vision_provider(self, *, user_id: int | None = None) -> ResolvedModelProvider | None:
        """The Brain seat: pick an enabled, vision-capable provider for screenshot decisions. Honours the
        per-user Settings choice (agent_settings.vision_provider_id, kept equal to the active row) first,
        then the env/config override,
        else auto-picks the newest enabled openai/anthropic/gemini provider. Returns None when none is
        configured (caller then degrades to ask_user — never claims a vision retry that didn't happen)."""
        # The user EXPLICITLY designated this provider as the vision brain — trust it for any type that can
        # carry an image (native openai/anthropic/gemini OR an OpenAI-compatible multimodal model like
        # MiniMax M3 via openrouter/custom). Only a known text-only type (deepseek) is rejected.
        vision_types = VISION_PROVIDER_TYPES | VISION_OPENAI_COMPATIBLE_TYPES
        # Designation precedence: the per-user Settings choice (agent_settings) wins, then the env/config
        # override, then auto-pick a natively-vision provider.
        explicit = None
        if user_id is not None and self.db is not None:
            try:
                from app.agent_settings import get_agent_settings

                explicit = get_agent_settings(self.db, user_id).get("vision_provider_id")
            except Exception:  # noqa: BLE001 - settings lookup must never break a model call
                explicit = None
        if explicit is None:
            explicit = getattr(self.settings, "vision_provider_id", None)
        chosen = self.provider_by_id(int(explicit)) if explicit else None
        if chosen and chosen.provider_type in vision_types:
            return chosen
        if not self.db:
            return None
        # Auto-pick only a NATIVELY vision-capable type (openai-compatible needs an explicit designation,
        # since the type alone doesn't prove the model behind it is multimodal).
        rows = self.db.scalars(
            select(models.ModelProvider).where(models.ModelProvider.enabled.is_(True)).order_by(models.ModelProvider.id.desc())
        ).all()
        for provider in rows:
            resolved = self.resolve_provider(provider)
            if resolved.provider_type in VISION_PROVIDER_TYPES and resolved.live_enabled:
                return resolved
        return None

    def judge_provider(self, *, user_id: int | None = None) -> ResolvedModelProvider | None:
        """The Checker seat: the user-designated row for bounded verdicts (grounding, target binding,
        payment floor, secret/credential classification, task completion), the secret gate's second
        opinion, and the vision backup. Designation only - the per-user Settings choice
        (agent_settings.judge_provider_id). Never auto-picked: which model is cheap/fast enough to grade
        with is the user's call, not a model-name heuristic. Returns None when nothing is designated (or
        the designated row is disabled or not live); callers then keep their current provider (vision or
        active) - routing is a pure opt-in."""
        explicit = None
        if user_id is not None and self.db is not None:
            try:
                from app.agent_settings import get_agent_settings

                explicit = get_agent_settings(self.db, user_id).get("judge_provider_id")
            except Exception:  # noqa: BLE001 - settings lookup must never break a model call
                explicit = None
        if not explicit:
            return None
        try:
            return self.provider_by_id(int(explicit))
        except (TypeError, ValueError):
            return None

    def grounding_judge_provider(self, *, user_id: int | None = None) -> ResolvedModelProvider | None:
        """The row the grounding judge runs on: the Checker (judge_provider)."""
        return self.judge_provider(user_id=user_id)

    def escalation_provider(self, *, user_id: int | None = None) -> ResolvedModelProvider | None:
        """The Escalation seat: the user-designated smarter vision brain a stuck part of a task escalates to.
        Designation only (never auto-picked), and it must be able to carry a screenshot. It is never the
        Checker: the secret gate's second opinion must stay a different model from the brain deciding steps."""
        explicit = None
        checker = None
        if user_id is not None and self.db is not None:
            try:
                from app.agent_settings import get_agent_settings

                settings = get_agent_settings(self.db, user_id)
                explicit = settings.get("vision_escalation_provider_id")
                checker = settings.get("judge_provider_id")
            except Exception:  # noqa: BLE001 - settings lookup must never break a model call
                explicit = None
        if not explicit or explicit == checker:
            return None
        try:
            chosen = self.provider_by_id(int(explicit))
        except (TypeError, ValueError):
            return None
        if chosen and chosen.provider_type in (VISION_PROVIDER_TYPES | VISION_OPENAI_COMPATIBLE_TYPES):
            return chosen
        return None

    def secret_adjudicator_provider(self, *, user_id: int | None = None) -> ResolvedModelProvider | None:
        """The secret gate's designated second model: the Checker (judge_provider). The caller still
        requires it to be a different model from the one whose verdict it adjudicates."""
        return self.judge_provider(user_id=user_id)

    def has_database_active_provider(self) -> bool:
        if not self.db:
            return False
        return bool(
            self.db.scalars(
                select(models.ModelProvider.id)
                .where(models.ModelProvider.is_active.is_(True), models.ModelProvider.enabled.is_(True))
                .limit(1)
            ).first()
        )

    def env_fallback_provider(self) -> ResolvedModelProvider:
        live = bool(not self.settings.use_mock_ai and self.settings.openai_api_key)
        return ResolvedModelProvider(
            id=None,
            display_name=".env OpenAI fallback" if live else "Deterministic fallback",
            provider_type="openai" if live else "mock",
            model_name=self.settings.skill_author_model if live else "deterministic-fallback",
            base_url=DEFAULT_BASE_URLS["openai"],
            api_key=self.settings.openai_api_key if live else None,
            temperature=0.2,
            max_tokens=2048,
            timeout_seconds=int(self.settings.skill_author_timeout_seconds or 30),
            enabled=live,
            is_active=live,
            source="env" if live else "fallback",
            health_status="unknown" if live else "unavailable",
            last_error=None if live else "mock_ai_enabled_or_openai_key_missing",
        )

    def resolve_provider(self, provider: models.ModelProvider) -> ResolvedModelProvider:
        api_key = self._decrypt_api_key(provider)
        return ResolvedModelProvider(
            id=provider.id,
            display_name=provider.display_name,
            provider_type=provider.provider_type,
            model_name=provider.model_name,
            base_url=provider.base_url or DEFAULT_BASE_URLS.get(provider.provider_type, ""),
            api_key=api_key,
            temperature=float(provider.temperature or 0.2),
            max_tokens=int(provider.max_tokens or 2048),
            timeout_seconds=int(provider.timeout_seconds or self.settings.skill_author_timeout_seconds or 30),
            enabled=bool(provider.enabled),
            is_active=bool(provider.is_active),
            source="database",
            health_status=provider.health_status,
            last_error=provider.last_error,
            reasoning_effort=provider.reasoning_effort or None,
            structured_output=self._normalized_structured_output(provider.structured_output),
            endpoint_preference=self._normalized_endpoint_preference(provider.endpoint_preference),
            endpoint_routing=normalize_endpoint_routing(provider.endpoint_routing),
        )

    def _providers(self) -> list[models.ModelProvider]:
        self._require_db()
        return list(self.db.scalars(select(models.ModelProvider).order_by(models.ModelProvider.id.asc())).all())

    def _provider(self, provider_id: int) -> models.ModelProvider:
        self._require_db()
        provider = self.db.get(models.ModelProvider, provider_id)
        if not provider:
            raise ValueError("Model provider not found.")
        return provider

    def _validate_provider(self, provider: models.ModelProvider) -> None:
        if provider.provider_type not in SUPPORTED_PROVIDER_TYPES:
            raise ValueError("Unsupported model provider type.")
        if not provider.model_name.strip():
            raise ValueError("Model name is required.")
        if provider.provider_type == "custom_openai_compatible" and not str(provider.base_url or "").strip():
            raise ValueError("Base URL is required for custom OpenAI-compatible providers.")

    def _encrypt_api_key(self, api_key: str) -> str:
        if not self.settings.token_encryption_key:
            raise ValueError("LUMEN_TOKEN_ENCRYPTION_KEY is required before storing model provider API keys.")
        return encrypt_secret(api_key, self.settings.token_encryption_key)

    def _decrypt_api_key(self, provider: models.ModelProvider) -> str | None:
        if not provider.encrypted_api_key:
            return None
        if not self.settings.token_encryption_key:
            return None
        try:
            return decrypt_secret(provider.encrypted_api_key, self.settings.token_encryption_key)
        except Exception:
            return None

    def _provider_type(self, value: object) -> str:
        provider_type = str(value or "").strip().lower()
        if provider_type not in SUPPORTED_PROVIDER_TYPES:
            raise ValueError("Unsupported model provider type.")
        return provider_type

    def _require_db(self) -> None:
        if not self.db:
            raise ValueError("Database session is required for model provider registry operations.")


def list_available_models(*, provider_type: str, api_key: str, base_url: str = "", timeout_seconds: int = 20) -> list[str]:
    """Query a provider for the models the key can use, so the UI can offer a real dropdown. Same shape
    for every provider (openai-compatible / anthropic / gemini): give it the key + base url, get back a
    sorted list of model ids. Raises ValueError on a clean failure (bad key, unreachable)."""
    provider_type = str(provider_type or "").strip().lower()
    api_key = str(api_key or "").strip()
    if provider_type not in SUPPORTED_PROVIDER_TYPES:
        raise ValueError("Unsupported model provider type.")
    if not api_key:
        raise ValueError("An API key is required to list models.")
    base = str(base_url or DEFAULT_BASE_URLS.get(provider_type) or "").strip().rstrip("/")
    try:
        if provider_type == "gemini":
            base = base or DEFAULT_BASE_URLS["gemini"]
            response = _http_get(f"{base}/models", params={"key": api_key, "pageSize": 1000}, timeout=timeout_seconds)
            response.raise_for_status()
            ids = []
            for model in response.json().get("models") or []:
                if not isinstance(model, dict):
                    continue
                name = str(model.get("name") or "").split("/")[-1]
                methods = model.get("supportedGenerationMethods") or []
                if name and (not methods or "generateContent" in methods):
                    ids.append(name)
            return sorted(set(ids))
        if provider_type == "anthropic":
            base = base or DEFAULT_BASE_URLS["anthropic"]
            response = _http_get(f"{base}/models", headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"}, timeout=timeout_seconds)
            response.raise_for_status()
            data = response.json().get("data") or []
            return sorted({str(model.get("id")) for model in data if isinstance(model, dict) and model.get("id")})
        # openai / deepseek / openrouter / custom_openai_compatible — OpenAI-compatible /models
        if not base:
            raise ValueError("A base URL is required to list models for this provider.")
        response = _http_get(f"{base}/models", headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout_seconds)
        response.raise_for_status()
        data = response.json().get("data") or []
        return sorted({str(model.get("id")) for model in data if isinstance(model, dict) and model.get("id")})
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status in (401, 403):
            raise ValueError("The API key was rejected by the provider.") from exc
        raise ValueError(f"The provider returned HTTP {status} when listing models.") from exc
    except httpx.HTTPError as exc:
        raise ValueError(f"Could not reach the provider to list models: {exc}") from exc


# Provider types whose APIs natively accept image input via their own message shape.
VISION_PROVIDER_TYPES = frozenset({"openai", "anthropic", "gemini"})
# OpenAI-compatible chat endpoints that can carry an image as an `image_url` content block when the model
# behind them is multimodal (e.g. MiniMax M3, Qwen-VL via OpenRouter/Together/custom). DeepSeek is NOT
# here — its API is text-only — so an image is never sent down the DeepSeek path.
VISION_OPENAI_COMPATIBLE_TYPES = frozenset({"openrouter", "custom_openai_compatible"})


def _canonical_payload_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _image_list(image: dict[str, Any] | list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """One vision call may carry several images: the primary screenshot first, then
    supplementary attachments (for example a native-resolution zoom crop). Callers keep
    passing a single dict; every provider body builder attaches list entries in order."""
    if isinstance(image, dict):
        return [image] if image.get("data") else []
    if isinstance(image, list):
        return [item for item in image if isinstance(item, dict) and item.get("data")]
    return []


def _provider_image_sha256(image: dict[str, Any] | list[dict[str, Any]] | None) -> str | None:
    hasher = hashlib.sha256()
    decoded_any = False
    for item in _image_list(image):
        try:
            raw = base64.b64decode(str(item.get("data") or ""), validate=True)
        except (ValueError, TypeError):
            continue
        if raw:
            hasher.update(raw)
            decoded_any = True
    return hasher.hexdigest() if decoded_any else None


def _provider_transport_receipt(
    *,
    provider: ResolvedModelProvider,
    schema: dict[str, Any] | None,
    requested_image: dict[str, Any] | list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Initialize a truthful transport receipt before any provider request body exists."""
    return {
        "transport_boundary": "provider_request_not_prepared",
        "provider_request_body_prepared": False,
        "provider_http_json_body_sha256": None,
        "provider_payload_system_sha256": None,
        "provider_payload_prompt_sha256": None,
        "schema_requested": bool(schema),
        "provider_requested_schema_sha256": _canonical_payload_sha256(schema) if schema else None,
        "schema_included": False,
        "provider_payload_schema_sha256": None,
        "image_requested": bool(requested_image),
        "provider_requested_image_sha256": _provider_image_sha256(requested_image),
        "image_included": False,
        "provider_payload_image_sha256": None,
        "provider_type_at_transport": provider.provider_type,
        "provider_id_at_transport": provider.id,
        "model_at_transport": provider.model_name,
    }


def _short_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _cached_prefix_receipt(system: str, prompt: Any, included_schema: dict[str, Any] | None) -> dict[str, Any]:
    """Short hashes of the bytes a provider can serve from its prompt cache: the system rules on the wire,
    the packet's stable keys, and the schema. When a call's cache read drops, comparing these with the
    previous call's receipt names the part that changed (a 12-hex digest, enough to tell apart, not to invert)."""
    system_part = _wire_system_text(system)
    stable_part, _tail = user_wire_parts(system, prompt)
    schema_part = json.dumps(included_schema, sort_keys=True, default=str) if included_schema else ""
    return {
        "cached_prefix_sha12": _short_sha("\x00".join((schema_part, system_part, stable_part))),
        "cached_prefix_parts_sha12": {
            "system": _short_sha(system_part),
            "stable_keys": _short_sha(stable_part),
            "schema": _short_sha(schema_part) if schema_part else None,
        },
    }


def _capture_provider_http_json(
    receipt: dict[str, Any] | None,
    *,
    body: dict[str, Any],
    system: str,
    prompt: dict[str, Any],
    included_schema: dict[str, Any] | None,
    included_image: dict[str, Any] | list[dict[str, Any]] | None,
) -> None:
    """Hash the exact JSON object handed to httpx, immediately before serialization.

    The receipt retains hashes and inclusion facts only. In particular, a schema that
    the caller requested is not marked as transported unless this concrete provider
    request body actually contains it.
    """
    if receipt is None:
        return
    reasoning_config = body.get("reasoning")
    receipt.update(_cached_prefix_receipt(system, prompt, included_schema))
    receipt.update(
        {
            "transport_boundary": "provider_http_json_body_before_serialization",
            "provider_request_body_prepared": True,
            "provider_http_json_body_sha256": _canonical_payload_sha256(body),
            "provider_payload_system_sha256": hashlib.sha256(str(system or "").encode("utf-8")).hexdigest(),
            "provider_payload_prompt_sha256": _canonical_payload_sha256(prompt),
            "schema_included": bool(included_schema),
            "provider_payload_schema_sha256": (
                _canonical_payload_sha256(included_schema) if included_schema else None
            ),
            "image_included": bool(included_image),
            "provider_payload_image_sha256": _provider_image_sha256(included_image),
            "reasoning_config_included": isinstance(reasoning_config, dict),
            "reasoning_enabled_at_transport": (
                reasoning_config.get("enabled")
                if isinstance(reasoning_config, dict)
                else None
            ),
        }
    )


# Prompt sections that only restate what the page rendered (its text, controls, prior pages, and the
# agent's own verified history on them). When the same call carries the screenshot, the pixels already
# show every code on the page, so scrubbing OTP-like tokens from this text protects nothing — it only
# blinds the model's memory of a value it saw on one page and needs on the next. Persisted copies of
# prompts stay redacted (_metadata, model-input retention).
PAGE_RENDERED_PROMPT_KEYS = frozenset(
    {
        "page_evidence",
        "last_page_view",
        "native_dialog",
        "browser_tabs",
        "control_inventory",
        "page_memory",
        "control_memory",
        "zoom_view",
        "your_notes",
        "your_plan",
        "front_window_text",
        "recent_actions",
        "last_reply",
        "verified_receipts",
        "session_ledger",
        "satisfied_controls",
    }
)


def _redact_prompt_for_transport(prompt: Any, *, screenshot_attached: bool) -> Any:
    if not screenshot_attached or not isinstance(prompt, dict):
        return redact_otp_codes(prompt)
    return {
        key: value if key in PAGE_RENDERED_PROMPT_KEYS else redact_otp_codes({key: value})[key]
        for key, value in prompt.items()
    }


def _wire_system_text(system: str) -> str:
    """The system message on the wire: the byte-stable rules only. The per-step directives after
    SYSTEM_DYNAMIC_BOUNDARY ride at the end of the user text (_wire_user_text), so they never end
    the cached prefix ahead of the prompt packet's stable keys."""
    return split_system_cache_prefix(system)[0]


def _wire_user_text(system: str, prompt: Any, suffix: str = "") -> str:
    """The full user text: the packet (stable keys first, as built) + dynamic directives + suffix."""
    stable, tail = user_wire_parts(system, prompt, suffix)
    return stable + tail


def _history_wire_turns(history: Any) -> list[tuple[str, str]]:
    """Message-history turns as (role, text), oldest first. Only user and assistant turns with text go on
    the wire (vision_agent.message_history_turns builds them); anything else is dropped, never sent malformed."""
    turns: list[tuple[str, str]] = []
    for turn in history or []:
        if not isinstance(turn, dict):
            continue
        role = str(turn.get("role") or "")
        text = str(turn.get("text") or "")
        if role in ("user", "assistant") and text:
            turns.append((role, text))
    return turns


_HTTP_CLIENT: httpx.Client | None = None
_HTTP_CLIENT_LOCK = threading.Lock()


def _shared_http_client() -> httpx.Client:
    """One shared connection pool. httpx.post builds a client and a fresh TLS connection per call
    (82 ms median to OpenRouter against 43 ms on a warm connection); every step makes one to four
    model calls. httpx.Client is thread-safe, so the to_thread workers share it."""
    global _HTTP_CLIENT
    client = _HTTP_CLIENT
    if client is None:
        with _HTTP_CLIENT_LOCK:
            if _HTTP_CLIENT is None:
                _HTTP_CLIENT = httpx.Client(limits=httpx.Limits(max_connections=32, max_keepalive_connections=16))
            client = _HTTP_CLIENT
    return client


def _http_post(url: str, **kwargs: Any) -> httpx.Response:
    return _shared_http_client().post(url, **kwargs)


def _http_get(url: str, **kwargs: Any) -> httpx.Response:
    """GET on the shared pool (catalog, pricing, model listing): no fresh TLS handshake per lookup."""
    return _shared_http_client().get(url, **kwargs)


def _explicit_cache_user_parts(system: str, prompt: Any, model_name: str, suffix: str = "") -> tuple[str, str]:
    """The user text as (text up to the cache breakpoint, per-call tail) for a provider that caches
    only at explicit breakpoints. The agent's packet breaks after its run-stable keys; a judge's
    (no stable keys) after its static task and rules once that block is long enough to cache."""
    stable, tail = user_wire_parts(system, prompt, suffix)
    if stable:
        return stable, tail
    _stable_system, dynamic = split_system_cache_prefix(system)
    judge_stable, judge_rest = judge_wire_parts(system, prompt, model_name)
    if not judge_stable:
        return stable, tail
    return judge_stable, judge_rest + dynamic + suffix


_TRANSIENT_TRANSPORT_RETRIES = 2
_TRANSIENT_HTTP_STATUSES = frozenset({429, 502, 503})
# A retry with less time left than this cannot finish a model call; surface the drop instead.
_TRANSIENT_RETRY_MIN_SECONDS = 2.0


def _post_with_transient_retry(url: str, **kwargs: Any) -> httpx.Response:
    """POST, retrying only transport failures where no response arrived (connection refused or
    reset, the server dropping the connection before replying). A timeout is not retried: the
    call already spent its budget, and a retry would double it. Retries share the first attempt's
    budget: a dropped connection late in a slow call must not start a fresh full-length call that
    outlives the caller's own deadline."""
    timeout = kwargs.get("timeout")
    deadline = time.monotonic() + float(timeout) if isinstance(timeout, (int, float)) and timeout > 0 else None
    for attempt in range(_TRANSIENT_TRANSPORT_RETRIES + 1):
        try:
            response = _http_post(url, **kwargs)
            status = int(getattr(response, "status_code", 200) or 200)
            if status in _TRANSIENT_HTTP_STATUSES and attempt < _TRANSIENT_TRANSPORT_RETRIES:
                # A rate limit or an upstream that answered without a completion (429/502/503) is
                # no verdict: a single-host model (Qwen via Alibaba) returned 429 on 1 of 6 calls,
                # and each one failed a fail-closed safety gate. Retry briefly inside the budget.
                retry_after = str(getattr(response, "headers", {}).get("retry-after", "") or "").strip()
                pause = min(3.0, float(retry_after)) if retry_after.replace(".", "", 1).isdigit() else 0.8 * (attempt + 1)
                if deadline is not None:
                    remaining = deadline - time.monotonic() - pause
                    if remaining < _TRANSIENT_RETRY_MIN_SECONDS:
                        return response
                    kwargs["timeout"] = remaining
                time.sleep(pause)
                continue
            return response
        except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError):
            if attempt >= _TRANSIENT_TRANSPORT_RETRIES:
                raise
            pause = 0.4 * (attempt + 1)
            if deadline is not None:
                remaining = deadline - time.monotonic() - pause
                if remaining < _TRANSIENT_RETRY_MIN_SECONDS:
                    raise
                kwargs["timeout"] = remaining
            time.sleep(pause)
    raise RuntimeError("unreachable")


def call_model_json(
    provider: ResolvedModelProvider,
    *,
    purpose: str,
    system: str,
    prompt: dict[str, Any],
    schema: dict[str, Any] | None = None,
    image: dict[str, Any] | list[dict[str, Any]] | None = None,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    # The screenshot is raw evidence handed to the model — never serialized into the text prompt and never
    # parsed here. Drop it silently if the provider can't see images (degrade to a text re-decide). Image
    # calls reach here only via the designated vision provider (lumen_model._call_model_json), so a vision
    # OpenAI-compatible model (MiniMax M3) carries it as an image_url block; DeepSeek never does.
    from app import external_judge

    if external_judge.handles(purpose):  # bench seat (external_judge.py): callers that skip lumen_model
        return external_judge.call(purpose=purpose, system=system, prompt=prompt, schema=schema, image=image)
    requested_image = image
    vision = image if (image and provider.provider_type in (VISION_PROVIDER_TYPES | VISION_OPENAI_COMPATIBLE_TYPES)) else None
    safe_prompt = _redact_prompt_for_transport(prompt, screenshot_attached=bool(vision))
    transport_receipt = _provider_transport_receipt(
        provider=provider,
        schema=schema,
        requested_image=requested_image,
    )
    if history:
        # Message history (vision_agent.message_history_turns): the turns this request actually carries.
        transport_receipt["message_history_turns"] = len(_history_wire_turns(history))
    started = time.perf_counter()
    if not provider.live_enabled:
        return {
            "ok": False,
            "data": {},
            "error": provider.last_error or "active_model_provider_unavailable",
            "metadata": {
                **_metadata(provider, latency_ms=0, error=provider.last_error or "active_model_provider_unavailable"),
                **transport_receipt,
            },
        }
    try:
        if provider.provider_type == "openai":
            body = _post_openai_responses(
                provider,
                system=system,
                prompt=safe_prompt,
                schema=schema,
                image=vision,
                transport_receipt=transport_receipt,
                history=history,
            )
            raw_text = body.get("output_text") or _responses_output_text(body)
            data, parse_status = _json_from_text_with_status(raw_text)
            usage = normalize_usage(provider.provider_type, body.get("usage", {}), provider.model_name)
            finish_reason = _finish_reason(provider.provider_type, body)
        elif provider.provider_type == "anthropic":
            body = _post_anthropic_messages(
                provider,
                system=system,
                prompt=safe_prompt,
                image=vision,
                transport_receipt=transport_receipt,
                history=history,
            )
            raw_text = _anthropic_output_text(body)
            data, parse_status = _json_from_text_with_status(raw_text)
            usage = normalize_usage(provider.provider_type, body.get("usage", {}), provider.model_name)
            finish_reason = _finish_reason(provider.provider_type, body)
        elif provider.provider_type == "gemini":
            body = _post_gemini(
                provider,
                system=system,
                prompt=safe_prompt,
                image=vision,
                transport_receipt=transport_receipt,
                history=history,
            )
            raw_text = _gemini_output_text(body)
            data, parse_status = _json_from_text_with_status(raw_text)
            usage = normalize_usage("gemini", body.get("usageMetadata", {}), provider.model_name)
            finish_reason = _finish_reason(provider.provider_type, body)
        else:
            reasoning_override = (_EVAL_TRANSPORT_OVERRIDES.get() or {}).get("reasoning_enabled")
            body = _post_openai_compatible_chat(
                provider,
                system=system,
                prompt=safe_prompt,
                schema=schema,
                image=vision,
                reasoning_enabled=reasoning_override if reasoning_override is not None else (
                    False
                    if provider.provider_type == "openrouter"
                    and purpose
                    in {
                        "operator_chat_scope",
                        "operator_chat_scope_protocol_repair",
                        "target_binding_judge",
                        "control_reference_conflict",
                        "secret_request_classification",
                        "credential_target_classification",
                        "pending_user_reply_interpretation",
                        # Runs before every click; measured at effort none on real submits and
                        # in-page steps: same verdicts, ~1s faster per click.
                        "blocked_click_semantics",
                        # A reading check, not a reasoning task.
                        "page_quote_visual_check",
                        "screen_text_transcription",
                        # Bounded to 256 / 768 output tokens: a judge row with a reasoning
                        # effort spent the whole allowance thinking and returned nothing, so the
                        # payment floor failed closed on every long page.
                        "payment_floor_judge",
                        "final_action_outcome_verification",
                        "task_completion_verification",
                        # Folds action records into a running summary (1,024-token budget): a full
                        # Gauntlet spent 37 minutes and most of its output tokens thinking first.
                        "session_context_compression",
                    }
                    else None
                ),
                transport_receipt=transport_receipt,
                history=history,
            )
            raw_text = _chat_output_text(body)
            data, parse_status = _json_from_text_with_status(raw_text)
            usage = normalize_usage(provider.provider_type, body.get("usage", {}), provider.model_name)
            finish_reason = _finish_reason(provider.provider_type, body)
        data, parse_status = _salvaged_if_cut_off(data, parse_status, raw_text, finish_reason)
        latency_ms = int((time.perf_counter() - started) * 1000)
        return {
            "ok": True,
            "data": data,
            "metadata": {
                **_metadata(
                    provider,
                    latency_ms=latency_ms,
                    token_usage=usage,
                    prompt=safe_prompt,
                    response=data,
                    purpose=purpose,
                    raw_output=raw_text,
                    parse_status=parse_status,
                    finish_reason=finish_reason,
                ),
                **transport_receipt,
            },
        }
    except Exception as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        return {
            "ok": False,
            "data": {},
            "error": _safe_error(exc),
            "metadata": {
                **_metadata(provider, latency_ms=latency_ms, error=_safe_error(exc), prompt=safe_prompt, purpose=purpose),
                **transport_receipt,
            },
        }


# Provider types that speak the OpenAI-style chat/completions SSE protocol and can
# stream token deltas. Other types (openai responses API, anthropic) fall back to a
# single-shot call emitted as one delta so the streaming surface still works for them.
_STREAMABLE_CHAT_PROVIDERS = {"deepseek", "openrouter", "custom_openai_compatible"}


def stream_model_text(provider: ResolvedModelProvider, *, system: str, prompt: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Stream a plain-text model reply.

    Yields ``{"type": "delta", "text": str}`` for each chunk, then exactly one terminal
    ``{"type": "final", "text": full, "usage": dict, "latency_ms": int, "error": str | None}``.
    Transport failures are surfaced on the terminal event (with any partial text already
    yielded) rather than raised, so the caller owns the fallback decision.
    """
    safe_prompt = redact_otp_codes(prompt)
    started = time.perf_counter()
    parts: list[str] = []
    usage: dict[str, Any] = {}
    error: str | None = None
    try:
        if not provider.live_enabled:
            raise RuntimeError(provider.last_error or "active_model_provider_unavailable")
        if provider.provider_type in _STREAMABLE_CHAT_PROVIDERS:
            for kind, value in _iter_openai_compatible_deltas(provider, system=system, prompt=safe_prompt):
                if kind == "delta" and value:
                    parts.append(value)
                    yield {"type": "delta", "text": value}
                elif kind == "usage" and isinstance(value, dict):
                    usage = normalize_usage(provider.provider_type, value, provider.model_name)
        else:
            if provider.provider_type == "anthropic":
                body = _post_anthropic_messages(provider, system=system, prompt=safe_prompt)
                text = _anthropic_output_text(body)
            elif provider.provider_type == "openai":
                body = _post_openai_responses(provider, system=system, prompt=safe_prompt, schema=None)
                text = body.get("output_text") or _responses_output_text(body)
            else:
                body = _post_openai_compatible_chat(provider, system=system, prompt=safe_prompt)
                text = _chat_output_text(body)
            usage = normalize_usage(provider.provider_type, body.get("usage", {}) or {}, provider.model_name)
            if text:
                parts.append(text)
                yield {"type": "delta", "text": text}
    except Exception as exc:  # noqa: BLE001 - surfaced on the terminal event for caller fallback
        error = _safe_error(exc)
    yield {
        "type": "final",
        "text": "".join(parts),
        "usage": usage,
        "latency_ms": int((time.perf_counter() - started) * 1000),
        "error": error,
    }


def _iter_openai_compatible_deltas(provider: ResolvedModelProvider, *, system: str, prompt: dict[str, Any]) -> Iterator[tuple[str, Any]]:
    body = {
        "model": provider.model_name,
        "messages": [{"role": "system", "content": _wire_system_text(system)}, {"role": "user", "content": _wire_user_text(system, prompt)}],
        "temperature": provider.temperature,
        "max_tokens": provider.max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if provider.provider_type == "openrouter":
        # Chat replies get the same routing, effort, and temperature handling as JSON calls: the
        # provider row's measured-fastest host and reasoning tier, not OpenRouter's defaults.
        if _openrouter_declines_temperature(provider.base_url, provider.model_name):
            del body["temperature"]
        if provider.reasoning_effort:
            body["reasoning"] = {"effort": provider.reasoning_effort}
        if _openrouter_routing(provider):
            body["provider"] = _openrouter_routing(provider)
    headers = {"Authorization": f"Bearer {provider.api_key}", "Content-Type": "application/json"}
    if provider.provider_type == "openrouter":
        headers.update({"HTTP-Referer": "https://lumen.local", "X-Title": "Lumen"})
    # Streamed chat replies reuse the warm shared pool too.
    client = _shared_http_client()
    with client.stream(
        "POST", _endpoint(provider.base_url, "chat/completions"), headers=headers, json=body, timeout=provider.timeout_seconds
    ) as response:
        if response.status_code >= 400:
            response.read()
            response.raise_for_status()
        for line in response.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            choices = chunk.get("choices") or []
            if choices:
                delta = (choices[0].get("delta") or {}).get("content")
                if delta:
                    yield ("delta", delta)
            if chunk.get("usage"):
                yield ("usage", chunk["usage"])


def model_provider_health(db: Session | None = None, *, refresh: bool = False, probe: bool = True) -> dict[str, Any]:
    service = ModelProviderService(db)
    provider = service.active_provider()
    report = {
        "provider": provider.provider_type,
        "display_name": provider.display_name,
        "configured": provider.live_enabled,
        "key_present": bool(provider.api_key),
        "model": provider.model_name,
        "base_url": provider.base_url,
        "validated": None,
        "last_validation_error": provider.last_error,
        "live_enabled": provider.live_enabled,
        "mock_mode": provider.provider_type == "mock",
        "active_provider_id": provider.id,
        "source": provider.source,
        "health_status": provider.health_status,
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "cached": False,
        "probed": bool(probe),
    }
    if not probe:
        return report
    result = call_model_json(
        provider,
        purpose="model_provider_health",
        system="You are a model provider health check. Return strict JSON only.",
        prompt={"instruction": "Return JSON with ok true.", "required": {"ok": True}},
        schema={
            "name": "model_provider_health",
            "schema": {
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
                "additionalProperties": True,
            },
        },
    )
    report["validated"] = bool(result.get("ok"))
    report["last_validation_error"] = None if result.get("ok") else _safe_error(result.get("error") or "model provider validation failed")
    report["metadata"] = _public_metadata(result.get("metadata") or {})
    return report


def _image_data_url(image: dict[str, Any]) -> str:
    return f"data:{image.get('media_type') or 'image/jpeg'};base64,{image.get('data') or ''}"


def _post_openai_responses(
    provider: ResolvedModelProvider,
    *,
    system: str,
    prompt: dict[str, Any],
    schema: dict[str, Any] | None,
    image: dict[str, Any] | list[dict[str, Any]] | None = None,
    transport_receipt: dict[str, Any] | None = None,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    user_text = _wire_user_text(system, prompt)
    user_content: Any = user_text
    images = _image_list(image)
    if images:
        user_content = [
            {"type": "input_text", "text": user_text},
            *[{"type": "input_image", "image_url": _image_data_url(item)} for item in images],
        ]
    earlier = [{"role": role, "content": text} for role, text in _history_wire_turns(history)]
    body: dict[str, Any] = {
        "model": provider.model_name,
        "input": [{"role": "system", "content": _wire_system_text(system)}, *earlier, {"role": "user", "content": user_content}],
        "max_output_tokens": provider.max_tokens,
    }
    if _openai_responses_supports_temperature(provider.model_name):
        body["temperature"] = provider.temperature
    if schema:
        body["text"] = {"format": {"type": "json_schema", "name": schema["name"], "schema": schema["schema"], "strict": False}}
    _capture_provider_http_json(
        transport_receipt,
        body=body,
        system=system,
        prompt=prompt,
        included_schema=schema,
        included_image=image,
    )
    response = _http_post(
        _endpoint(provider.base_url, "responses"),
        headers={"Authorization": f"Bearer {provider.api_key}", "Content-Type": "application/json"},
        json=body,
        timeout=provider.timeout_seconds,
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(f"{exc} response_body={_safe_error(response.text)}") from exc
    return response.json()


STRICT_REQUIRES_ALL_SCHEMAS = frozenset({"lumen_vision_action"})


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """The schema as a strict-decoding transport (OpenAI-style json_schema strict) needs it: every
    property required. The brain action schema lists only its core as required, so an optional key
    is omitted by the model and read as its default; strict decoding rejects optional properties, so
    this copy requires them all, as the schema did before the core was split out. Other schemas
    pass through unchanged."""
    if not isinstance(schema, dict) or schema.get("name") not in STRICT_REQUIRES_ALL_SCHEMAS:
        return schema

    def require_all(node: Any) -> Any:
        if isinstance(node, dict):
            copy = {key: require_all(value) for key, value in node.items()}
            properties = copy.get("properties")
            if copy.get("type") == "object" and isinstance(properties, dict) and copy.get("additionalProperties") is False:
                copy["required"] = list(properties)
            return copy
        if isinstance(node, list):
            return [require_all(item) for item in node]
        return node

    return require_all(schema)


def _deepseek_thinking_extra(provider: ResolvedModelProvider) -> dict[str, Any]:
    """Enable DeepSeek V4 thinking mode for JSON/agentic calls. V4 returns the
    chain-of-thought in `reasoning_content` and the final JSON in `content` (which
    our parser reads), and supports response_format json_object alongside thinking.
    Only applies to deepseek v4 models; legacy/other providers are untouched."""
    if provider.provider_type == "deepseek" and "v4" in str(provider.model_name or "").lower():
        return {"thinking": {"type": "enabled"}}
    return {}


def _post_openai_compatible_chat(
    provider: ResolvedModelProvider,
    *,
    system: str,
    prompt: dict[str, Any],
    schema: dict[str, Any] | None = None,
    image: dict[str, Any] | list[dict[str, Any]] | None = None,
    reasoning_enabled: bool | None = None,
    transport_receipt: dict[str, Any] | None = None,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    images = _image_list(image)
    # DeepSeek, Qwen, GLM, and OpenAI-family hosts cache matching prefixes on their own. Claude
    # behind OpenRouter caches only at explicit cache_control breakpoints: one after the rules
    # and one after the packet's run-stable keys, as on the native Anthropic path.
    explicit_cache = provider.provider_type == "openrouter" and str(provider.model_name or "").lower().startswith("anthropic/")
    # Message history: the earlier turns sit before the packet, so the packet's stable keys no longer open
    # the request and their breakpoint is dropped; the breakpoint moves to the last earlier turn instead.
    turns = _history_wire_turns(history)

    def system_message(text: str) -> dict[str, Any]:
        if not explicit_cache:
            return {"role": "system", "content": text}
        return {"role": "system", "content": [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]}

    earlier_messages: list[dict[str, Any]] = []
    for index, (role, text) in enumerate(turns):
        if explicit_cache and index == len(turns) - 1:
            earlier_messages.append({"role": role, "content": [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]})
        else:
            earlier_messages.append({"role": role, "content": text})

    def user_message(suffix: str = "") -> dict[str, Any]:
        stable_text, tail_text = (
            _explicit_cache_user_parts(system, prompt, provider.model_name, suffix)
            if explicit_cache
            else user_wire_parts(system, prompt, suffix)
        )
        if not images and not explicit_cache:
            return {"role": "user", "content": stable_text + tail_text}
        if explicit_cache:
            text_parts: list[dict[str, Any]] = []
            if stable_text:
                stable_part: dict[str, Any] = {"type": "text", "text": stable_text}
                if not turns:
                    stable_part["cache_control"] = {"type": "ephemeral"}
                text_parts.append(stable_part)
            if tail_text:
                text_parts.append({"type": "text", "text": tail_text})
        else:
            text_parts = [{"type": "text", "text": stable_text + tail_text}]
        # OpenAI-compatible vision shape (MiniMax M3, Qwen-VL, etc. via OpenRouter/Together/custom).
        return {
            "role": "user",
            "content": [
                *text_parts,
                *[{"type": "image_url", "image_url": {"url": _image_data_url(item)}} for item in images],
            ],
        }
    # A provider set to json_object gets its schema enforced by Lumen's own validation only: for
    # some models strict decoding is measurably worse (GPT-6 Luna looped on whitespace to the token
    # cap in 15% of strict calls, 0% in json_object mode), and asking for it would cost a round trip.
    structured_schema = (
        strict_schema(schema)
        if provider.provider_type == "openrouter" and schema and provider.structured_output != "json_object"
        else None
    )
    if schema and provider.provider_type == "openrouter" and transport_receipt is not None:
        transport_receipt["structured_output_mode"] = "strict" if structured_schema else "json_object"
    response_format = (
        {
            "type": "json_schema",
            "json_schema": {
                "name": structured_schema["name"],
                "strict": True,
                "schema": structured_schema["schema"],
            },
        }
        if structured_schema
        else {"type": "json_object"}
    )
    system_wire = _wire_system_text(system)
    mentions_json = "json" in f"{system_wire}\n{_wire_user_text(system, prompt)}".casefold()
    json_note = ""
    if not structured_schema and not mentions_json:
        # DeepSeek (and other OpenAI-compatible hosts) reject response_format=json_object
        # with 400 "Prompt must contain the word 'json'" unless the messages mention it.
        json_note = "\nReturn a single valid JSON object."
    body = {
        "model": provider.model_name,
        "messages": [system_message(system_wire), *earlier_messages, user_message(json_note)],
        "temperature": provider.temperature,
        "max_tokens": provider.max_tokens,
        "response_format": response_format,
        **_deepseek_thinking_extra(provider),
    }
    if provider.provider_type == "openrouter" and _openrouter_declines_temperature(provider.base_url, provider.model_name):
        # A model that does not accept temperature (OpenAI's reasoning models) ignores it anyway,
        # but under require_parameters its presence filters out every endpoint and the strict
        # schema degrades to json_object on every call. Leave it out so the schema is delivered.
        del body["temperature"]
        if transport_receipt is not None:
            transport_receipt["temperature_omitted"] = True
    # OpenRouter documents require_parameters as the fail-closed routing guard for models/providers
    # that cannot honor response_format (json_schema or json_object) or the other parameters sent.
    require_parameters = provider.provider_type == "openrouter" and (bool(structured_schema) or _requires_parameters(provider))
    if require_parameters:
        body["provider"] = {"require_parameters": True}
    if _openrouter_routing(provider):
        body["provider"] = {**body.get("provider", {}), **_openrouter_routing(provider)}
    if provider.provider_type == "openrouter":
        # OpenRouter exposes a normalized reasoning switch. Compact read-only gates need the
        # strict JSON verdict rather than a hidden chain that can consume the entire bounded
        # output allowance before emitting the schema — an explicit disable always wins.
        # Otherwise a provider-configured effort tier (REASONING_EFFORTS) shapes reasoning
        # depth; the full browser decision remains reasoning-enabled.
        if reasoning_enabled is not None and not reasoning_enabled:
            reasoning_off = _openrouter_reasoning_off(provider.base_url, provider.model_name)
            if reasoning_off is not None:
                body["reasoning"] = reasoning_off
            if reasoning_off != {"enabled": False} and transport_receipt is not None:
                transport_receipt["reasoning_mandatory_substitute"] = (reasoning_off or {}).get("effort") or "model_default"
        elif provider.reasoning_effort:
            body["reasoning"] = {"effort": provider.reasoning_effort}
        elif reasoning_enabled is not None:
            body["reasoning"] = {"enabled": True}
        reasoning_override = (_EVAL_TRANSPORT_OVERRIDES.get() or {}).get("reasoning")
        if reasoning_override:
            body["reasoning"] = dict(reasoning_override)
    headers = {"Authorization": f"Bearer {provider.api_key}", "Content-Type": "application/json"}
    if provider.provider_type == "openrouter":
        headers.update({"HTTP-Referer": "https://lumen.local", "X-Title": "Lumen"})
    _capture_provider_http_json(
        transport_receipt,
        body=body,
        system=system,
        prompt=prompt,
        included_schema=structured_schema,
        included_image=image,
    )
    response = _post_with_transient_retry(_endpoint(provider.base_url, "chat/completions"), headers=headers, json=body, timeout=provider.timeout_seconds)
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        # OpenRouter answers 404 when require_parameters filters out every endpoint — no host
        # for this model can honor response_format=json_schema. The schema contract is still
        # guaranteed downstream by deterministic validation (normalize_action re-decide), the
        # same boundary every non-structured provider already runs behind, so degrade this call
        # to json_object instead of making such models permanently unroutable. The transport
        # receipt is re-captured with the fallback body so schema_included stays truthful.
        if require_parameters and response.status_code == 404:
            fallback_body = {key: value for key, value in body.items() if key != "provider"}
            if _openrouter_routing(provider):
                fallback_body["provider"] = _openrouter_routing(provider)
            fallback_body["response_format"] = {"type": "json_object"}
            if not mentions_json:
                fallback_body["messages"] = [
                    system_message(system_wire),
                    *earlier_messages,
                    user_message("\nReturn a single valid JSON object."),
                ]
            if transport_receipt is not None:
                transport_receipt["structured_output_fallback" if structured_schema else "require_parameters_dropped"] = True
            _capture_provider_http_json(
                transport_receipt,
                body=fallback_body,
                system=system,
                prompt=prompt,
                included_schema=None,
                included_image=image,
            )
            response = _post_with_transient_retry(_endpoint(provider.base_url, "chat/completions"), headers=headers, json=fallback_body, timeout=provider.timeout_seconds)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as retry_exc:
                raise RuntimeError(f"{retry_exc} response_body={_safe_error(response.text)}") from retry_exc
            return _with_upstream_receipt(provider, response.json(), transport_receipt)
        if (
            response.status_code == 400
            and body.get("reasoning") == {"enabled": False}
            and "reasoning is mandatory" in str(response.text or "").casefold()
        ):
            # The catalog normally tells us a model cannot turn reasoning off, but a model it does not
            # list (or a catalog never fetched) would fail every gate closed on this 400. Ask once more
            # at the least reasoning the model will take.
            effort = provider.reasoning_effort if provider.reasoning_effort not in (None, "", "none", "minimal") else "low"
            retry_body = {**body, "reasoning": {"effort": effort}}
            if transport_receipt is not None:
                transport_receipt["reasoning_mandatory_substitute"] = effort
            _capture_provider_http_json(
                transport_receipt,
                body=retry_body,
                system=system,
                prompt=prompt,
                included_schema=structured_schema,
                included_image=image,
            )
            response = _post_with_transient_retry(_endpoint(provider.base_url, "chat/completions"), headers=headers, json=retry_body, timeout=provider.timeout_seconds)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as retry_exc:
                raise RuntimeError(f"{retry_exc} response_body={_safe_error(response.text)}") from retry_exc
            return _with_upstream_receipt(provider, response.json(), transport_receipt)
        raise RuntimeError(f"{exc} response_body={_safe_error(response.text)}") from exc
    return _with_upstream_receipt(provider, response.json(), transport_receipt)


def _openrouter_routing(provider: ResolvedModelProvider) -> dict[str, Any]:
    """The row's OpenRouter routing (_row_openrouter_routing) with any eval-leg routing override on
    top: an override that names order or sort replaces the row's order, sort, and allow_fallbacks."""
    routing = _row_openrouter_routing(provider)
    override = (_EVAL_TRANSPORT_OVERRIDES.get() or {}).get("routing")
    if provider.provider_type != "openrouter" or not override:
        return routing
    if "order" in override or "sort" in override:
        routing = {key: value for key, value in routing.items() if key not in ("order", "sort", "allow_fallbacks")}
    return {**routing, **override}


def _row_openrouter_routing(provider: ResolvedModelProvider) -> dict[str, Any]:
    """Try the provider's preferred OpenRouter endpoint first. Fallbacks stay allowed unless the row
    sets allow_fallbacks false, so by default an endpoint outage costs a reroute, never an
    unroutable model. A row with allow_fallbacks false is served by its preferred host only: a
    cut-off retry's skip or a hedge's avoid does not move it. Otherwise a request that skips or
    avoids a host is never pinned."""
    if provider.provider_type != "openrouter":
        return {}
    config = normalize_endpoint_routing(provider.endpoint_routing) or {}
    if provider.endpoint_preference and config.get("allow_fallbacks") is False:
        return {"order": [provider.endpoint_preference], "allow_fallbacks": False}
    skip = [host for host in dict.fromkeys(provider.endpoint_skip or ()) if host]
    if provider.endpoint_avoid:
        routing: dict[str, Any] = {
            "ignore": [provider.endpoint_avoid, *(host for host in skip if host != provider.endpoint_avoid)],
            "sort": "throughput",
        }
        if provider.endpoint_max_completion_price:
            routing["max_price"] = {"completion": provider.endpoint_max_completion_price}
        return routing
    routing = {}
    if provider.endpoint_preference and provider.endpoint_preference not in skip:
        routing["order"] = [provider.endpoint_preference]
        if config.get("allow_fallbacks") is False and not skip:
            routing["allow_fallbacks"] = False
    if skip:
        routing["ignore"] = skip
    if config.get("sort") and "order" not in routing:
        routing["sort"] = config["sort"]
    return routing


def _with_upstream_receipt(
    provider: ResolvedModelProvider,
    body: dict[str, Any],
    transport_receipt: dict[str, Any] | None,
) -> dict[str, Any]:
    # OpenRouter names the host that actually served the call; latency differs by host.
    if provider.provider_type == "openrouter" and transport_receipt is not None and isinstance(body, dict):
        transport_receipt["upstream_provider"] = str(body.get("provider") or "")[:64] or None
    return body


def _anthropic_supports_temperature(model_name: str) -> bool:
    """Opus 4.7+, Opus/Sonnet 5, and Fable/Mythos reject sampling params with a 400."""
    name = str(model_name or "").lower()
    return not re.search(r"opus-4-[78]|opus-5|sonnet-5|fable|mythos", name)


def _post_anthropic_messages(
    provider: ResolvedModelProvider,
    *,
    system: str,
    prompt: dict[str, Any],
    image: dict[str, Any] | list[dict[str, Any]] | None = None,
    transport_receipt: dict[str, Any] | None = None,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    # Two cache breakpoints: one on the stable rules block, one after the prompt packet's
    # run-stable keys, so a step whose stable keys are unchanged reads both from cache and one
    # with a new note still reads the rules. Per-step keys, the dynamic directives, and the
    # screenshot follow the second breakpoint. A prefix under the model's cacheable minimum
    # silently no-ops.
    # Message history (earlier turns) sits before the packet: the packet's stable breakpoint is then never
    # read again (the prefix before it changes each step), so a third breakpoint goes on the last earlier turn.
    turns = _history_wire_turns(history)
    earlier_messages: list[dict[str, Any]] = []
    for index, (role, text) in enumerate(turns):
        block: dict[str, Any] = {"type": "text", "text": text}
        if index == len(turns) - 1:
            block["cache_control"] = {"type": "ephemeral"}
        earlier_messages.append({"role": role, "content": [block]})
    stable_user, user_tail = _explicit_cache_user_parts(system, prompt, provider.model_name)
    images = _image_list(image)
    user_content: Any = stable_user + user_tail
    if images or stable_user:
        user_content = []
        if stable_user:
            stable_block: dict[str, Any] = {"type": "text", "text": stable_user}
            if not turns:
                stable_block["cache_control"] = {"type": "ephemeral"}
            user_content.append(stable_block)
        if user_tail:
            user_content.append({"type": "text", "text": user_tail})
        user_content.extend(
            {"type": "image", "source": {"type": "base64", "media_type": item.get("media_type") or "image/jpeg", "data": item.get("data") or ""}}
            for item in images
        )
    system_blocks: list[dict[str, Any]] = [
        {"type": "text", "text": _wire_system_text(system), "cache_control": {"type": "ephemeral"}}
    ]
    body = {
        "model": provider.model_name,
        "system": system_blocks,
        "messages": [*earlier_messages, {"role": "user", "content": user_content}],
        "max_tokens": provider.max_tokens,
    }
    if _anthropic_supports_temperature(provider.model_name):
        body["temperature"] = provider.temperature
    _capture_provider_http_json(
        transport_receipt,
        body=body,
        system=system,
        prompt=prompt,
        included_schema=None,
        included_image=image,
    )
    response = _http_post(
        _endpoint(provider.base_url, "messages"),
        headers={"x-api-key": str(provider.api_key), "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
        json=body,
        timeout=provider.timeout_seconds,
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(f"{exc} response_body={_safe_error(response.text)}") from exc
    return response.json()


def _post_gemini(
    provider: ResolvedModelProvider,
    *,
    system: str,
    prompt: dict[str, Any],
    image: dict[str, Any] | list[dict[str, Any]] | None = None,
    transport_receipt: dict[str, Any] | None = None,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    base = str(provider.base_url or DEFAULT_BASE_URLS["gemini"]).strip().rstrip("/")
    parts: list[dict[str, Any]] = [{"text": _wire_user_text(system, prompt)}]
    for item in _image_list(image):
        parts.append({"inlineData": {"mimeType": item.get("media_type") or "image/jpeg", "data": item.get("data") or ""}})
    # Gemini takes role "model" for the brain's earlier replies. The packet joins a trailing user turn
    # (the recorded outcome) so the contents keep alternating roles.
    contents: list[dict[str, Any]] = [
        {"role": "model" if role == "assistant" else "user", "parts": [{"text": text}]}
        for role, text in _history_wire_turns(history)
    ]
    if contents and contents[-1]["role"] == "user":
        contents[-1]["parts"].extend(parts)
    else:
        contents.append({"role": "user", "parts": parts})
    body = {
        "contents": contents,
        "systemInstruction": {"parts": [{"text": _wire_system_text(system)}]},
        "generationConfig": {"temperature": provider.temperature, "maxOutputTokens": provider.max_tokens, "responseMimeType": "application/json"},
    }
    _capture_provider_http_json(
        transport_receipt,
        body=body,
        system=system,
        prompt=prompt,
        included_schema=None,
        included_image=image,
    )
    response = _http_post(
        f"{base}/models/{provider.model_name}:generateContent",
        params={"key": str(provider.api_key)},
        json=body,
        timeout=provider.timeout_seconds,
    )
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(f"{exc} response_body={_safe_error(response.text)}") from exc
    return response.json()


def _gemini_output_text(body: dict[str, Any]) -> str:
    chunks = []
    for candidate in body.get("candidates") or []:
        for part in ((candidate.get("content") or {}).get("parts") or []):
            if isinstance(part, dict) and part.get("text"):
                chunks.append(str(part["text"]))
    return "\n".join(chunks)


def _endpoint(base_url: str, path: str) -> str:
    base = str(base_url or "").strip().rstrip("/")
    if not base:
        base = DEFAULT_BASE_URLS["openai"]
    terminal = {"/responses", "/chat/completions", "/messages"}
    if any(base.endswith(item) for item in terminal):
        return base
    return f"{base}/{path.strip('/')}"


def _openai_responses_supports_temperature(model_name: str) -> bool:
    normalized = str(model_name or "").strip().lower()
    return not normalized.startswith(("gpt-5", "o1", "o3", "o4"))


def _json_from_text_with_status(text: str) -> tuple[dict[str, Any], str]:
    cleaned = str(text or "").strip()
    if not cleaned:
        return {}, "empty"
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    try:
        return json.loads(cleaned), "json"
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            return {}, "non_json"
        try:
            return json.loads(match.group(0)), "json_extracted"
        except json.JSONDecodeError:
            return {}, "invalid_json"


# Finish reasons that mean a reply stopped at its output-token cap (chat "length", Anthropic
# "max_tokens", Gemini "MAX_TOKENS", the Responses API "incomplete:max_output_tokens").
_TOKEN_CAP_FINISH_REASONS = frozenset({"length", "max_tokens", "incomplete:max_output_tokens"})


def reply_cut_off_at_token_cap(metadata: dict[str, Any] | None) -> bool:
    """The provider stopped this reply at its output-token cap."""
    if not isinstance(metadata, dict):
        return False
    return str(metadata.get("finish_reason") or "").strip().lower() in _TOKEN_CAP_FINISH_REASONS


def reply_cut_off_without_json(metadata: dict[str, Any] | None) -> bool:
    """A reply cut off at its token cap before any JSON answer could be read from it."""
    return reply_cut_off_at_token_cap(metadata) and str(
        (metadata or {}).get("parse_status") or ""
    ).strip().lower() in {"empty", "non_json", "invalid_json"}


def _salvage_cut_off_json(text: str) -> dict[str, Any] | None:
    """The one answer a reply cut off at its token cap already holds, or None.

    The ordinary extractor reads one object from the first "{" to the last "}". A reply that answered
    and then ran on to the cap by emitting the same object again (cut mid-way) defeats it. Here every
    complete top-level object must be the same object, and anything after the last one must be a
    cut-off start of that same object; prose without braces may surround it. Any other brace, or two
    different objects (a draft and its revision), salvages nothing: the text does not determine the
    reply's answer.
    """
    cleaned = str(text or "")
    decoder = json.JSONDecoder()
    objects: list[tuple[Any, str]] = []
    position = 0
    tail_fragment = ""
    while True:
        start = cleaned.find("{", position)
        if start < 0:
            break
        if "}" in cleaned[position:start]:
            return None
        try:
            value, end = decoder.raw_decode(cleaned, start)
        except json.JSONDecodeError:
            tail_fragment = cleaned[start:]
            break
        objects.append((value, cleaned[start:end]))
        position = end
    if not tail_fragment and "}" in cleaned[position:]:
        return None
    if not objects or not all(isinstance(value, dict) and value for value, _ in objects):
        return None
    first_value, first_raw = objects[0]
    if any(value != first_value for value, _ in objects[1:]):
        return None
    if tail_fragment and not re.sub(r"\s+", "", first_raw).startswith(re.sub(r"\s+", "", tail_fragment)):
        return None
    return first_value


def _complete_top_level_objects(text: str) -> list[dict[str, Any]]:
    """Every complete top-level JSON object in the text, in order."""
    decoder = json.JSONDecoder()
    found: list[dict[str, Any]] = []
    raw = str(text or "")
    index = raw.find("{")
    while index != -1:
        try:
            value, end = decoder.raw_decode(raw, index)
        except json.JSONDecodeError:
            index = raw.find("{", index + 1)
            continue
        if isinstance(value, dict):
            found.append(value)
        index = raw.find("{", end)
    return found


def _salvaged_if_cut_off(
    data: dict[str, Any],
    parse_status: str,
    raw_text: str,
    finish_reason: str,
) -> tuple[dict[str, Any], str]:
    """A reply cut off at its token cap that the ordinary extractor could not read keeps the one
    answer its text already holds (parse_status json_salvaged). The salvaged object is still only a
    proposal: every caller validates it exactly as it validates a whole reply."""
    if not reply_cut_off_at_token_cap({"finish_reason": finish_reason}):
        return data, parse_status
    if parse_status == "json_extracted":
        # The extractor read one object, but the reply was cut off while starting another (a draft and
        # its revision): which one the model meant is unknown unless they are the same answer. Every
        # json caller (judges and gates too) then fails closed instead of taking the first draft.
        tail = str(raw_text or "")[str(raw_text or "").rfind("}") + 1 :]
        if "{" not in tail:
            return data, parse_status
        # A cut-off start can match the first object only because it stopped before any difference, so
        # here the answer stands only when it was given whole at least twice, identically.
        complete = _complete_top_level_objects(raw_text)
        if len(complete) >= 2 and all(item == complete[0] for item in complete):
            return complete[0], "json_salvaged"
        return {}, "invalid_json"
    if parse_status not in {"non_json", "invalid_json"}:
        return data, parse_status
    salvaged = _salvage_cut_off_json(raw_text)
    if salvaged is None:
        return data, parse_status
    return salvaged, "json_salvaged"


def _responses_output_text(body: dict[str, Any]) -> str:
    chunks: list[str] = []
    for item in body.get("output", []):
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                chunks.append(str(content["text"]))
    return "\n".join(chunks)


def _chat_output_text(body: dict[str, Any]) -> str:
    choices = body.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return str(message.get("content") or "")


def _anthropic_output_text(body: dict[str, Any]) -> str:
    chunks = []
    for item in body.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            chunks.append(str(item.get("text") or ""))
    return "\n".join(chunks)


def _finish_reason(provider_type: str, body: dict[str, Any]) -> str:
    if not isinstance(body, dict):
        return ""
    provider = str(provider_type or "").lower()
    if provider == "openai":
        status = str(body.get("status") or "")
        incomplete = body.get("incomplete_details") if isinstance(body.get("incomplete_details"), dict) else {}
        reason = str(incomplete.get("reason") or "")
        return ":".join(part for part in [status, reason] if part)
    if provider == "anthropic":
        return str(body.get("stop_reason") or "")
    if provider == "gemini":
        candidates = body.get("candidates") or []
        if candidates and isinstance(candidates[0], dict):
            return str(candidates[0].get("finishReason") or "")
        return ""
    choices = body.get("choices") or []
    if choices and isinstance(choices[0], dict):
        return str(choices[0].get("finish_reason") or "")
    return ""


def _metadata(
    provider: ResolvedModelProvider,
    *,
    latency_ms: int,
    token_usage: dict[str, Any] | None = None,
    error: str | None = None,
    prompt: dict[str, Any] | None = None,
    response: dict[str, Any] | None = None,
    purpose: str | None = None,
    raw_output: str | None = None,
    parse_status: str | None = None,
    finish_reason: str | None = None,
) -> dict[str, Any]:
    window = model_context_window(provider.provider_type, provider.model_name)
    if not window.get("known") and provider.provider_type == "openrouter":
        # The provider's own catalog beats any hardcoded model table: OpenRouter
        # publishes context_length per model, so new models (gpt-5.6-luna) report a
        # real window instead of "unknown" without anyone maintaining a list.
        reported = _openrouter_context_length(provider.base_url, provider.model_name)
        if reported:
            window = {
                "limit_tokens": reported,
                "source": "provider_catalog:openrouter",
                "known": True,
            }
    metadata = {
        "provider": provider.provider_type,
        "provider_display_name": provider.display_name,
        "provider_id": provider.id,
        "provider_source": provider.source,
        "model": provider.model_name,
        "base_url": provider.base_url,
        "context_window_tokens": window.get("limit_tokens"),
        "context_window_source": window.get("source"),
        "context_window_known": bool(window.get("known")),
        "latency_ms": latency_ms,
        "token_usage": token_usage or {},
        "purpose": purpose,
    }
    if error:
        metadata["error"] = _safe_error(error)
    if prompt is not None:
        metadata["prompt"] = redact_otp_codes(prompt)
    if response is not None:
        metadata["response"] = redact_otp_codes(response)
    if raw_output is not None:
        text = redact_otp_codes(str(raw_output))
        metadata["raw_output_empty"] = not bool(text.strip())
        metadata["raw_output_chars"] = len(text)
        if parse_status and parse_status not in {"json", "json_extracted"}:
            # A reply that did not parse: keep a short excerpt so the failure can be diagnosed afterwards.
            metadata["raw_output_excerpt"] = text[:400]
    if parse_status:
        metadata["parse_status"] = parse_status
    if finish_reason:
        metadata["finish_reason"] = finish_reason
    return metadata


# (fetched_at, model id -> context_length, model id -> accepted request parameters,
#  model id -> reasoning contract {"mandatory": bool, "efforts": tuple of supported effort tiers})
_OPENROUTER_MODEL_CONTEXT: (
    tuple[float, dict[str, int], dict[str, frozenset[str]], dict[str, dict[str, Any]]] | None
) = None
# Effort tiers from least to most reasoning; a gate that wants reasoning off takes the first one a
# reasoning-mandatory model supports.
_REASONING_EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
_OPENROUTER_CATALOG_LOCK = threading.Lock()
_OPENROUTER_MODEL_CONTEXT_TTL_SECONDS = 24 * 3600.0
_OPENROUTER_MODEL_CONTEXT_RETRY_SECONDS = 300.0


_OPENROUTER_ENDPOINT_PRICES: dict[str, tuple[float, dict[str, float]]] = {}
_OPENROUTER_ENDPOINT_PRICES_LOCK = threading.Lock()
_OPENROUTER_ENDPOINT_PRICES_TTL_SECONDS = 6 * 3600
_OPENROUTER_ENDPOINT_PRICES_RETRY_SECONDS = 300


def openrouter_endpoint_completion_price(base_url: str, model_name: str, endpoint: str) -> float | None:
    """One OpenRouter host's output price for a model (USD per million tokens), or None when unknown.

    From the public /models/{model}/endpoints listing, fetched at most every six hours per model
    (five minutes after a failed fetch). Matched by host tag ("wafer", "atlas-cloud/fp8") or its
    provider slug before any quantization suffix."""
    model = str(model_name or "").strip().lower()
    wanted = str(endpoint or "").strip().lower()
    if not model or not wanted:
        return None
    now = time.time()
    with _OPENROUTER_ENDPOINT_PRICES_LOCK:
        cached = _OPENROUTER_ENDPOINT_PRICES.get(model)
        if cached is None or now - cached[0] >= _OPENROUTER_ENDPOINT_PRICES_TTL_SECONDS:
            prices: dict[str, float] = {}
            stamp = now
            try:
                response = _http_get(
                    _endpoint(str(base_url or "").strip() or DEFAULT_BASE_URLS["openrouter"], f"models/{model}/endpoints"),
                    timeout=4.0,
                )
                response.raise_for_status()
                for item in ((response.json().get("data") or {}).get("endpoints") or []):
                    if not isinstance(item, dict):
                        continue
                    try:
                        price = float((item.get("pricing") or {}).get("completion")) * 1_000_000
                    except (TypeError, ValueError):
                        continue
                    tag = str(item.get("tag") or "").strip().lower()
                    if tag:
                        prices.setdefault(tag, price)
                        prices.setdefault(tag.split("/", 1)[0], price)
            except Exception:  # noqa: BLE001 - an unknown price only removes the cap
                stamp = now - _OPENROUTER_ENDPOINT_PRICES_TTL_SECONDS + _OPENROUTER_ENDPOINT_PRICES_RETRY_SECONDS
            cached = (stamp, prices)
            _OPENROUTER_ENDPOINT_PRICES[model] = cached
    return cached[1].get(wanted) or cached[1].get(wanted.split("/", 1)[0])


def _openrouter_catalog(
    base_url: str,
) -> tuple[dict[str, int], dict[str, frozenset[str]], dict[str, dict[str, Any]]]:
    """OpenRouter's public /models catalog as three tables: context length, the request
    parameters each model accepts (its ``supported_parameters``), and its reasoning contract
    (whether reasoning can be turned off, and which effort tiers it accepts).

    Fetched at most once per day per process (5-minute retry window after a failed fetch), by one
    thread at a time. A failed refresh keeps the last good tables: dropping them would silently turn
    a reasoning-mandatory brain's gates back into 400s. Never fetched and unreachable yields empty
    tables - unknown stays unknown."""
    with _OPENROUTER_CATALOG_LOCK:
        return _openrouter_catalog_locked(base_url)


def _openrouter_catalog_locked(
    base_url: str,
) -> tuple[dict[str, int], dict[str, frozenset[str]], dict[str, dict[str, Any]]]:
    global _OPENROUTER_MODEL_CONTEXT
    now = time.time()
    if _OPENROUTER_MODEL_CONTEXT is None or now - _OPENROUTER_MODEL_CONTEXT[0] >= _OPENROUTER_MODEL_CONTEXT_TTL_SECONDS:
        contexts: dict[str, int] = {}
        parameters: dict[str, frozenset[str]] = {}
        reasoning: dict[str, dict[str, Any]] = {}
        fetched_at = now
        try:
            response = _http_get(
                _endpoint(str(base_url or "").strip() or DEFAULT_BASE_URLS["openrouter"], "models"),
                timeout=4.0,
            )
            response.raise_for_status()
            for item in (response.json().get("data") or []):
                if not isinstance(item, dict):
                    continue
                model_id = str(item.get("id") or "").strip().lower()
                if not model_id:
                    continue
                supported = item.get("supported_parameters")
                if isinstance(supported, list):
                    parameters[model_id] = frozenset(str(name).strip().lower() for name in supported)
                contract = item.get("reasoning")
                if isinstance(contract, dict):
                    efforts = contract.get("supported_efforts")
                    reasoning[model_id] = {
                        "mandatory": contract.get("mandatory") is True,
                        "efforts": tuple(str(effort).strip().lower() for effort in efforts)
                        if isinstance(efforts, list)
                        else (),
                    }
                try:
                    context_length = int(item.get("context_length") or 0)
                except (TypeError, ValueError):
                    continue
                if context_length > 0:
                    contexts[model_id] = context_length
        except Exception:  # noqa: BLE001 - catalog unavailability must never fail a model call
            # Age the result so the next attempt happens after the short retry window instead
            # of on every single call while OpenRouter is unreachable.
            fetched_at = now - _OPENROUTER_MODEL_CONTEXT_TTL_SECONDS + _OPENROUTER_MODEL_CONTEXT_RETRY_SECONDS
            if _OPENROUTER_MODEL_CONTEXT is not None:
                _, contexts, parameters, reasoning = _OPENROUTER_MODEL_CONTEXT
        _OPENROUTER_MODEL_CONTEXT = (fetched_at, contexts, parameters, reasoning)
    return _OPENROUTER_MODEL_CONTEXT[1], _OPENROUTER_MODEL_CONTEXT[2], _OPENROUTER_MODEL_CONTEXT[3]


def _openrouter_context_length(base_url: str, model_name: str) -> int | None:
    """Context length for one model from OpenRouter's public /models catalog; None when the
    catalog is unreachable or the model is not listed."""
    key = str(model_name or "").strip().lower()
    if not key:
        return None
    return _openrouter_catalog(base_url)[0].get(key)


def _openrouter_declines_temperature(base_url: str, model_name: str) -> bool:
    """True only when the catalog lists this model and ``temperature`` is not among the
    parameters it accepts. With ``provider.require_parameters`` an unaccepted parameter
    leaves no endpoint to route to (404), so the strict schema would never be delivered.
    An unlisted model or an unreachable catalog returns False: the request stays as it was."""
    key = str(model_name or "").strip().lower()
    if not key:
        return False
    supported = _openrouter_catalog(base_url)[1].get(key)
    return supported is not None and "temperature" not in supported


def _openrouter_reasoning_off(base_url: str, model_name: str) -> dict[str, Any] | None:
    """The reasoning block for a call that wants reasoning off.

    A model whose catalog entry says reasoning is mandatory (Gemini 3.8 Flash, GLM 5.3 Flash)
    answers ``enabled: false`` with 400 "Reasoning is mandatory" on every call, which failed every
    gate closed. It gets its least-reasoning supported effort instead, or no reasoning block when
    it lists no tiers. Every other model, and any model the catalog does not list, keeps
    ``enabled: false``."""
    contract = _openrouter_catalog(base_url)[2].get(str(model_name or "").strip().lower())
    if not contract or not contract.get("mandatory"):
        return {"enabled": False}
    for effort in _REASONING_EFFORT_ORDER:
        if effort in contract.get("efforts", ()):
            return {"effort": effort}
    return None


def _public_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "provider",
        "provider_display_name",
        "provider_id",
        "provider_source",
        "model",
        "base_url",
        "context_window_tokens",
        "context_window_source",
        "context_window_known",
        "latency_ms",
        "token_usage",
        "purpose",
        "error",
        "raw_output_empty",
        "raw_output_chars",
        "parse_status",
        "finish_reason",
    }
    return {key: _safe_error(value) if key == "error" else value for key, value in metadata.items() if key in allowed}


def _mask_secret(value: str | None) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) <= 8:
        return "****"
    return f"{text[:4]}...{text[-4:]}"


def _safe_error(value: object) -> str:
    text = str(value or "")
    if not text:
        return "unknown_error"
    text = re.sub(r"sk-[A-Za-z0-9_-]+", "sk-[REDACTED]", text)
    text = re.sub(r"Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text, flags=re.IGNORECASE)
    return text.replace("\n", " ")[:500]


def _default_display_name(provider_type: str, model_name: str) -> str:
    label = {
        "openai": "OpenAI",
        "deepseek": "DeepSeek",
        "openrouter": "OpenRouter",
        "anthropic": "Anthropic",
        "custom_openai_compatible": "Custom OpenAI-compatible",
    }.get(provider_type, "Model Provider")
    return f"{label} {model_name}".strip()


def _bounded_float(value: object, *, default: float, low: float, high: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(low, min(high, parsed))


def _bounded_int(value: object, *, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(low, min(high, parsed))
