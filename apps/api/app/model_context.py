from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any


_MODEL_CONTEXT_WINDOWS: tuple[tuple[re.Pattern[str], int, str], ...] = (
    (re.compile(r"(^|[/:\s_-])deepseek[-_]?v4", re.I), 1_000_000, "known_model_profile:deepseek-v4"),
    (re.compile(r"(^|[/:\s_-])gpt-4\.1(?:$|[-_\s])", re.I), 1_047_576, "known_model_profile:gpt-4.1"),
    (re.compile(r"(^|[/:\s_-])gpt-4o(?:$|[-_\s])", re.I), 128_000, "known_model_profile:gpt-4o"),
    (re.compile(r"(^|[/:\s_-])o3(?:$|[-_\s])", re.I), 200_000, "known_model_profile:o3"),
    (re.compile(r"(^|[/:\s_-])o4[-_]?mini(?:$|[-_\s])", re.I), 200_000, "known_model_profile:o4-mini"),
    (re.compile(r"claude-(?:3(?:[-_.]5|[-_.]7)?|sonnet[-_.]?4|opus[-_.]?4)", re.I), 200_000, "known_model_profile:claude"),
)


def _int_value(*values: Any) -> int | None:
    for value in values:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return None


def normalize_token_usage(usage: dict[str, Any] | None) -> dict[str, int]:
    """Normalize provider token usage shapes without estimating missing counts."""
    usage = usage if isinstance(usage, dict) else {}
    prompt = _int_value(
        usage.get("prompt_tokens"),
        usage.get("input_tokens"),
        usage.get("promptTokenCount"),
        usage.get("inputTokenCount"),
    )
    completion = _int_value(
        usage.get("completion_tokens"),
        usage.get("output_tokens"),
        usage.get("candidatesTokenCount"),
        usage.get("outputTokenCount"),
    )
    total = _int_value(
        usage.get("total_tokens"),
        usage.get("totalTokens"),
        usage.get("totalTokenCount"),
    )
    if total is None and (prompt is not None or completion is not None):
        total = int(prompt or 0) + int(completion or 0)
    return {
        "prompt_tokens": int(prompt or 0),
        "completion_tokens": int(completion or 0),
        "total_tokens": int(total or 0),
    }


def model_context_window(provider_type: str | None, model_name: str | None) -> dict[str, Any]:
    """Return a model context window only when the backend has a defensible source.

    This intentionally does not use provider.max_tokens, because that is an output cap, not the model's
    context length. Unknown models stay unknown so the UI cannot pretend a random limit is real.
    """
    model = str(model_name or "").strip()
    if not model:
        return {"limit_tokens": None, "source": "unknown", "known": False}
    normalized = model.lower()
    for pattern, tokens, source in _MODEL_CONTEXT_WINDOWS:
        if pattern.search(normalized):
            return {"limit_tokens": tokens, "source": source, "known": True}
    return {"limit_tokens": None, "source": "unknown", "known": False}


def context_usage_from_metadata(metadata: dict[str, Any] | None, *, source: str = "") -> dict[str, Any] | None:
    metadata = metadata if isinstance(metadata, dict) else {}
    counts = normalize_token_usage(metadata.get("token_usage") if isinstance(metadata.get("token_usage"), dict) else {})
    used = counts["prompt_tokens"]
    if used <= 0:
        return None
    window = {
        "limit_tokens": metadata.get("context_window_tokens"),
        "source": metadata.get("context_window_source") or "unknown",
        "known": bool(metadata.get("context_window_known")),
    }
    if not _int_value(window.get("limit_tokens")):
        window = model_context_window(str(metadata.get("provider") or ""), str(metadata.get("model") or ""))
    limit = _int_value(window.get("limit_tokens"))
    return {
        "used_tokens": used,
        "limit_tokens": limit,
        "completion_tokens": counts["completion_tokens"],
        "total_tokens": counts["total_tokens"],
        "model": str(metadata.get("model") or ""),
        "provider": str(metadata.get("provider") or ""),
        "provider_display_name": str(metadata.get("provider_display_name") or ""),
        "provider_id": metadata.get("provider_id"),
        "purpose": str(metadata.get("purpose") or source or ""),
        "source": source or str(metadata.get("purpose") or "model_call"),
        "usage_source": "provider_response.token_usage",
        "context_window_source": str(window.get("source") or "unknown"),
        "context_window_known": bool(window.get("known") and limit),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
