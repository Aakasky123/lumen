"""Run-start prewarm: open the provider connection and, where caching is explicit, write the cache.

The run loop fires this in the background before its first screenshot, so it overlaps the
observation and never delays step 1. Two parts, decided per provider:

* Connection (every live provider, no tokens): one GET to the provider's base URL on the shared
  pooled client, so step 1's request reuses an established TLS connection instead of paying the
  handshake (82 ms against 43 ms warm, measured against OpenRouter).
* Cache (Anthropic native, and Claude behind OpenRouter): one request carrying the exact stable
  system block with its cache_control breakpoint and max_tokens=1. These providers cache only at
  explicit breakpoints and the first request pays the 1.25x write; paying it here instead of in
  step 1 means step 1 reads the rules block at 0.1x and starts sooner. Cost: the prewarm's write
  replaces step 1's write; step 1 then adds a read of the same tokens (about 0.1x of the system
  block, a few thousand tokens, well under a cent) plus one output token. If step 1 happens to
  race ahead of the prewarm both write, so the worst case is one extra 1.25x write of that block.
  OpenAI, DeepSeek, Qwen, GLM and Gemini cache a repeated prefix on their own with no write
  premium, bill the first request at the full input rate whether or not a prewarm ran, and a
  1-token request is invalid on OpenAI's reasoning models (output floor of 16): a prewarm there
  pays the prefix twice for a hit that is best-effort and host-sticky, so they get the connection
  warm only.

Every model call here is tallied in the model-call ledger under purpose "prewarm". Nothing here
raises into the run.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import replace
from typing import Any

from app import model_call_ledger
from app.prompt_cache import split_system_cache_prefix

_log = logging.getLogger(__name__)

PREWARM_PURPOSE = "prewarm"
_CONNECTION_TIMEOUT_SECONDS = 5.0
_CACHE_TIMEOUT_SECONDS = 20


def _env_disabled() -> bool:
    return os.environ.get("LUMEN_RUN_PREWARM", "").strip().lower() in {"0", "false", "no", "off"}


def prewarm_kind(provider: Any) -> str:
    """"cache" (connection plus cache write), "connection", or "none" for this provider."""
    if provider is None or not getattr(provider, "live_enabled", False):
        return "none"
    provider_type = str(getattr(provider, "provider_type", "") or "")
    if provider_type == "anthropic":
        return "cache"
    if provider_type == "openrouter" and str(getattr(provider, "model_name", "") or "").lower().startswith("anthropic/"):
        return "cache"
    return "connection"


def prewarm_enabled(settings: dict[str, Any] | None) -> bool:
    if _env_disabled():
        return False
    return bool((settings or {}).get("run_start_prewarm", True))


def system_fingerprint(system: str) -> str:
    """Hash of the rules block as it goes on the wire (the part before the dynamic boundary)."""
    stable, _dynamic = split_system_cache_prefix(system)
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16]


def warm_connection(provider: Any) -> bool:
    from app import model_provider

    base = str(getattr(provider, "base_url", "") or "").strip()
    if not base:
        base = str(model_provider.DEFAULT_BASE_URLS.get(str(provider.provider_type)) or "")
    if not base:
        return False
    try:
        model_provider._http_get(base, timeout=_CONNECTION_TIMEOUT_SECONDS)
        return True
    except Exception:  # noqa: BLE001 - any answer, even an error status, leaves the connection pooled; a failure costs nothing
        return False


def warm_cache(provider: Any, *, system: str, schema: dict[str, Any] | None, run_id: int | None) -> dict[str, Any] | None:
    """One max_tokens=1 request with the stable system block; tallied under purpose "prewarm"."""
    from app import model_provider

    stable_system, _dynamic = split_system_cache_prefix(system)
    if not stable_system:
        return None
    probe = replace(
        provider,
        max_tokens=1,
        timeout_seconds=min(int(provider.timeout_seconds or _CACHE_TIMEOUT_SECONDS), _CACHE_TIMEOUT_SECONDS),
        # A reasoning budget would exceed max_tokens=1; thinking settings leave the system-block cache alone.
        reasoning_effort=None,
        endpoint_skip=(),
    )
    result = model_provider.call_model_json(probe, purpose=PREWARM_PURPOSE, system=stable_system, prompt={}, schema=schema)
    model_call_ledger.record_for(run_id, PREWARM_PURPOSE, result)
    return result


def run_prewarm(provider: Any, *, system: str | None, schema: dict[str, Any] | None, run_id: int | None) -> dict[str, Any]:
    """Blocking body of the background task (call it in a worker thread). Never raises."""
    kind = prewarm_kind(provider)
    report: dict[str, Any] = {"kind": kind, "connection": False, "cache": None}
    if kind == "none":
        return report
    try:
        report["connection"] = warm_connection(provider)
        if kind == "cache" and system:
            result = warm_cache(provider, system=system, schema=schema, run_id=run_id)
            report["cache"] = bool(result and result.get("ok"))
    except Exception as exc:  # noqa: BLE001 - a prewarm never fails the run
        _log.debug("run prewarm failed: %s", exc)
    return report
