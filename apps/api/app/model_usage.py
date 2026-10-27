"""One shape for provider token usage, and a cost when the provider did not report one.

Providers disagree on names and on what "input" means: Anthropic's ``input_tokens`` excludes cache
reads and writes, Gemini's ``promptTokenCount`` includes cached content and ``candidatesTokenCount``
excludes thinking, OpenAI's ``prompt_tokens`` includes cached tokens and ``completion_tokens``
includes reasoning. ``normalize_usage`` maps each to:

  prompt_tokens         total input, cached and cache-written tokens included
  completion_tokens     total output, reasoning included
  cached_prompt_tokens  input served from the provider's cache
  cache_write_tokens    input written to the provider's cache
  reasoning_tokens      hidden reasoning/thinking tokens (already inside completion_tokens)
  cost                  provider-reported cost in USD, else computed from MODEL_PRICES, else absent
  raw                   the provider's usage block, untouched

A count the provider did not report is left out rather than guessed; a cost with no known price is absent.
"""

from __future__ import annotations

from typing import Any

# USD per million tokens: (input, output). Cache reads and writes are priced at their own rates only
# when CACHE_RATE_MULTIPLIERS names the model (a multiple of its input rate); otherwise at the input
# rate, which can only overstate the cost. Keys are lower-case model names.
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "qwen/qwen3.8-flash": (0.15, 0.47),
    "anthropic/claude-haiku-5.5": (0.10, 0.50),
}
# model -> (cache read multiple, cache write multiple) of the input rate.
CACHE_RATE_MULTIPLIERS: dict[str, tuple[float, float]] = {}


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0, int(value))


def _first(source: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        found = _count(source.get(key))
        if found is not None:
            return found
    return None


def _nested(source: dict[str, Any], container: str, key: str) -> int | None:
    inner = source.get(container)
    return _count(inner.get(key)) if isinstance(inner, dict) else None


def estimate_cost(
    model: str | None,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    cached_prompt_tokens: int | None = None,
    cache_write_tokens: int | None = None,
) -> float | None:
    """Cost from MODEL_PRICES, or None when the model's price or either token count is unknown."""
    key = str(model or "").strip().lower()
    price = MODEL_PRICES.get(key)
    if price is None or prompt_tokens is None or completion_tokens is None:
        return None
    read_mult, write_mult = CACHE_RATE_MULTIPLIERS.get(key, (1.0, 1.0))
    cached = min(cached_prompt_tokens or 0, prompt_tokens)
    written = min(cache_write_tokens or 0, max(0, prompt_tokens - cached))
    plain = prompt_tokens - cached - written
    input_rate, output_rate = price
    return (
        (plain + cached * read_mult + written * write_mult) * input_rate + completion_tokens * output_rate
    ) / 1_000_000


def normalize_usage(provider_type: str | None, raw: Any, model: str | None = None) -> dict[str, Any]:
    """The provider usage block in the common shape above. Idempotent; {} for a missing block."""
    if not isinstance(raw, dict) or not raw:
        return {}
    if isinstance(raw.get("raw"), dict) and "prompt_tokens" in raw:
        return raw
    kind = str(provider_type or "").strip().lower()
    written: int | None = None
    if kind == "anthropic" or ("input_tokens" in raw and ("cache_read_input_tokens" in raw or "cache_creation_input_tokens" in raw)):
        base = _first(raw, "input_tokens")
        cached = _first(raw, "cache_read_input_tokens")
        written = _first(raw, "cache_creation_input_tokens")
        parts = [value for value in (base, cached, written) if value is not None]
        prompt = sum(parts) if parts else None
        completion = _first(raw, "output_tokens")
        reasoning = None
    elif kind == "gemini" or "promptTokenCount" in raw or "candidatesTokenCount" in raw:
        prompt = _first(raw, "promptTokenCount")
        cached = _first(raw, "cachedContentTokenCount")
        reasoning = _first(raw, "thoughtsTokenCount")
        candidates = _first(raw, "candidatesTokenCount")
        completion = None if candidates is None and reasoning is None else (candidates or 0) + (reasoning or 0)
    else:
        # OpenAI chat / OpenRouter / compatible (prompt_tokens, completion_tokens) and the OpenAI
        # Responses API (input_tokens, output_tokens): input already includes cached tokens.
        prompt = _first(raw, "prompt_tokens", "input_tokens")
        completion = _first(raw, "completion_tokens", "output_tokens")
        cached = _nested(raw, "prompt_tokens_details", "cached_tokens")
        if cached is None:
            cached = _nested(raw, "input_tokens_details", "cached_tokens")
        written = _nested(raw, "prompt_tokens_details", "cache_write_tokens")
        reasoning = _nested(raw, "completion_tokens_details", "reasoning_tokens")
        if reasoning is None:
            reasoning = _nested(raw, "output_tokens_details", "reasoning_tokens")
    out: dict[str, Any] = {}
    for key, value in (
        ("prompt_tokens", prompt),
        ("completion_tokens", completion),
        ("cached_prompt_tokens", cached),
        ("cache_write_tokens", written),
        ("reasoning_tokens", reasoning),
    ):
        if value is not None:
            out[key] = value
    reported = raw.get("cost")
    if isinstance(reported, (int, float)) and not isinstance(reported, bool):
        out["cost"] = float(reported)
    else:
        estimated = estimate_cost(model, prompt, completion, cached, written)
        if estimated is not None:
            out["cost"] = round(estimated, 8)
            out["cost_source"] = "price_table"
    out["raw"] = raw
    return out
