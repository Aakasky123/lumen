"""normalize_usage maps every provider's usage block to one shape; cost only from a report or a known price."""

from __future__ import annotations

import pytest

from app.model_usage import normalize_usage


@pytest.mark.parametrize(
    ("provider", "raw", "expected"),
    [
        (
            "anthropic",
            {"input_tokens": 12, "output_tokens": 30, "cache_read_input_tokens": 800, "cache_creation_input_tokens": 100},
            {"prompt_tokens": 912, "completion_tokens": 30, "cached_prompt_tokens": 800, "cache_write_tokens": 100},
        ),
        (
            "gemini",
            {"promptTokenCount": 500, "candidatesTokenCount": 20, "cachedContentTokenCount": 400, "thoughtsTokenCount": 15},
            {"prompt_tokens": 500, "completion_tokens": 35, "cached_prompt_tokens": 400, "reasoning_tokens": 15},
        ),
        (
            "openrouter",
            {
                "prompt_tokens": 300, "completion_tokens": 40, "cost": 0.0042,
                "prompt_tokens_details": {"cached_tokens": 250},
                "completion_tokens_details": {"reasoning_tokens": 25},
            },
            {"prompt_tokens": 300, "completion_tokens": 40, "cached_prompt_tokens": 250, "reasoning_tokens": 25, "cost": 0.0042},
        ),
        (
            "openai",
            {"input_tokens": 90, "output_tokens": 10, "input_tokens_details": {"cached_tokens": 64}, "output_tokens_details": {"reasoning_tokens": 4}},
            {"prompt_tokens": 90, "completion_tokens": 10, "cached_prompt_tokens": 64, "reasoning_tokens": 4},
        ),
        ("deepseek", {"prompt_tokens": 7, "completion_tokens": 3}, {"prompt_tokens": 7, "completion_tokens": 3}),
    ],
)
def test_each_provider_shape_normalizes(provider: str, raw: dict, expected: dict) -> None:
    out = normalize_usage(provider, raw)
    assert out.pop("raw") == raw
    assert out == expected


def test_missing_block_is_empty_and_normalizing_twice_is_a_no_op() -> None:
    assert normalize_usage("anthropic", None) == {} and normalize_usage("gemini", {}) == {}
    once = normalize_usage("anthropic", {"input_tokens": 1, "output_tokens": 2})
    assert normalize_usage("anthropic", once) is once


def test_cost_is_computed_only_for_a_known_price() -> None:
    priced = normalize_usage("openrouter", {"prompt_tokens": 2_000_000, "completion_tokens": 1_000_000}, "Qwen/Qwen3.8-Flash")
    assert priced["cost"] == pytest.approx(0.77) and priced["cost_source"] == "price_table"
    assert "cost" not in normalize_usage("openrouter", {"prompt_tokens": 5, "completion_tokens": 5}, "unpriced/model")
    # A provider-reported cost wins over the table.
    assert normalize_usage("openrouter", {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.5}, "qwen/qwen3.8-flash")["cost"] == 0.5


def test_cache_rates_apply_when_the_model_names_them(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import model_usage

    monkeypatch.setitem(model_usage.MODEL_PRICES, "x/y", (1.0, 2.0))
    monkeypatch.setitem(model_usage.CACHE_RATE_MULTIPLIERS, "x/y", (0.1, 1.25))
    usage = normalize_usage("anthropic", {"input_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000, "cache_creation_input_tokens": 1_000_000, "output_tokens": 0}, "x/y")
    assert usage["cost"] == pytest.approx(1.0 + 0.1 + 1.25)
