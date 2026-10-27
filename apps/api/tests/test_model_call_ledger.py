"""Every structured model call made inside a run is tallied on that run, by purpose."""

from __future__ import annotations

import asyncio

from app import model_call_ledger


def _result(*, ok: bool = True, latency: int = 100, cost: float | None = 0.001, model: str = "m") -> dict:
    usage = {"prompt_tokens": 10, "completion_tokens": 5}
    if cost is not None:
        usage["cost"] = cost
    return {"ok": ok, "metadata": {"latency_ms": latency, "token_usage": usage, "model": model}}


def test_calls_in_worker_threads_land_on_the_bound_run_and_unknown_cost_stays_unknown() -> None:
    async def run() -> dict:
        token = model_call_ledger.bind(42)
        try:
            await asyncio.to_thread(model_call_ledger.record, "payment_floor_judge", _result(cost=0.0002))
            await asyncio.to_thread(model_call_ledger.record, "payment_floor_judge", _result(ok=False, latency=50, cost=0.0001))
            model_call_ledger.record("grounding_judge", _result(cost=None, model="judge"))
        finally:
            return model_call_ledger.release(token, 42)

    tally = asyncio.run(run())

    assert tally["payment_floor_judge"] == {
        "calls": 2, "failed": 1, "latency_ms": 150, "prompt_tokens": 20, "completion_tokens": 10,
        "cached_prompt_tokens": 0, "cache_write_tokens": 0, "reasoning_tokens": 0,
        "cost_usd": 0.0003, "cost_known_usd": 0.0003, "cost_unknown_calls": 0, "models": ["m"],
    }
    assert tally["grounding_judge"]["cost_usd"] is None
    assert tally["grounding_judge"]["cost_known_usd"] == 0.0 and tally["grounding_judge"]["cost_unknown_calls"] == 1
    # Outside a bound run nothing is recorded.
    model_call_ledger.record("payment_floor_judge", _result())
    assert model_call_ledger.release(model_call_ledger.bind(43), 43) == {}


def _ledger_result(provider_type: str, raw: dict, model: str = "m") -> dict:
    from app.model_usage import normalize_usage

    return {"ok": True, "metadata": {"latency_ms": 1, "model": model, "token_usage": normalize_usage(provider_type, raw, model)}}


def test_anthropic_and_gemini_shaped_usage_land_as_real_tokens_in_the_ledger() -> None:
    token = model_call_ledger.bind(77)
    model_call_ledger.record(
        "vision_agent_step",
        _ledger_result("anthropic", {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 80, "cache_creation_input_tokens": 10}),
    )
    model_call_ledger.record(
        "vision_agent_step",
        _ledger_result("gemini", {"promptTokenCount": 100, "candidatesTokenCount": 7, "cachedContentTokenCount": 60, "thoughtsTokenCount": 3}),
    )
    totals = model_call_ledger.totals(77)
    tally = model_call_ledger.release(token, 77)["vision_agent_step"]

    assert (tally["prompt_tokens"], tally["completion_tokens"]) == (200, 15)
    assert (tally["cached_prompt_tokens"], tally["cache_write_tokens"], tally["reasoning_tokens"]) == (140, 10, 3)
    assert totals["tokens"] == 215 and totals["cached_prompt_tokens"] == 140 and totals["cache_write_tokens"] == 10
    # No reported cost and no known price: unknown, not zero.
    assert totals["cost_usd"] is None and totals["cost_unknown_calls"] == 2


def test_ledger_cost_comes_from_the_price_table_when_the_provider_reports_none() -> None:
    token = model_call_ledger.bind(78)
    model_call_ledger.record(
        "grounding_judge",
        _ledger_result("openrouter", {"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}, "qwen/qwen3.8-flash"),
    )
    totals = model_call_ledger.totals(78)
    model_call_ledger.release(token, 78)
    assert totals["cost_usd"] == 0.62
