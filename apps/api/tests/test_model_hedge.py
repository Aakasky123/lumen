"""A slow browser decision gets one parallel second request; the first valid answer wins."""

from __future__ import annotations

import time
from dataclasses import dataclass

import pytest

from app import model_call_ledger, model_hedge
from app.model_hedge import hedged_call


@dataclass(frozen=True)
class _Provider:
    provider_type: str = "openrouter"
    endpoint_preference: str | None = "wafer"
    endpoint_avoid: str | None = None
    endpoint_max_completion_price: float | None = None
    base_url: str = "https://openrouter.ai/api/v1"
    model_name: str = "deepseek/deepseek-v4.1-flash"
    endpoint_routing: dict | None = None


@pytest.fixture(autouse=True)
def _known_host_prices(monkeypatch):
    prices = {"wafer": 0.6}
    monkeypatch.setattr(model_hedge, "openrouter_endpoint_completion_price", lambda base, model, host: prices.get(host))


def _reply(ok: bool, label: str, cost: float = 0.001) -> dict:
    return {"ok": ok, "data": {"action": label}, "metadata": {"latency_ms": 1, "token_usage": {"cost": cost}}}


def test_a_fast_decision_is_never_hedged() -> None:
    calls: list[str | None] = []

    def call(provider, **kwargs):
        calls.append(provider.endpoint_preference)
        return _reply(True, "click")

    result = hedged_call(call, _Provider(), hedge_after=1.0, purpose="vision_agent_step")

    assert calls == ["wafer"]
    assert result["data"] == {"action": "click"} and "hedge" not in result["metadata"]


def test_a_slow_decision_is_answered_by_the_hedge_and_the_loser_is_still_tallied() -> None:
    routes: list[str | None] = []
    avoided: list[str | None] = []
    caps: list[float | None] = []

    def call(provider, **kwargs):
        routes.append(provider.endpoint_preference)
        avoided.append(provider.endpoint_avoid)
        caps.append(provider.endpoint_max_completion_price)
        if provider.endpoint_preference == "wafer":
            time.sleep(0.4)
            return _reply(True, "slow", cost=0.002)
        return _reply(True, "fast", cost=0.001)

    token = model_call_ledger.bind(77)
    try:
        result = hedged_call(call, _Provider(), hedge_after=0.05, purpose="vision_agent_step")
        time.sleep(0.6)  # the slow primary finishes in the background
    finally:
        tally = model_call_ledger.release(token, 77)

    assert routes == ["wafer", None], "the hedge drops the pin"
    assert avoided == [None, "wafer"], "and skips the pinned host outright"
    assert caps == [None, 0.6], "at no more than the pinned host's output price"
    assert result["data"] == {"action": "fast"}
    assert result["metadata"]["hedge"]["winner"] == "hedge"
    assert tally["vision_agent_step:hedge_extra"]["calls"] == 1
    assert tally["vision_agent_step:hedge_extra"]["cost_usd"] == 0.002


def test_a_row_pinned_without_fallbacks_hedges_on_its_own_host() -> None:
    routes: list[tuple[str | None, str | None]] = []

    def call(provider, **kwargs):
        routes.append((provider.endpoint_preference, provider.endpoint_avoid))
        time.sleep(0.4 if len(routes) == 1 else 0.0)
        return _reply(True, "first" if len(routes) == 1 else "second")

    pinned = _Provider(endpoint_routing={"allow_fallbacks": False})
    result = hedged_call(call, pinned, hedge_after=0.05, purpose="vision_agent_step")
    time.sleep(0.5)

    assert routes == [("wafer", None), ("wafer", None)], "the hedge stays on the pinned host"
    assert result["data"] == {"action": "second"}


def test_when_both_fail_the_primary_failure_comes_back_unchanged() -> None:
    def call(provider, **kwargs):
        time.sleep(0.1 if provider.endpoint_preference == "wafer" else 0.15)
        return {"ok": False, "error": provider.endpoint_preference or "rerouted", "metadata": {}}

    result = hedged_call(call, _Provider(), hedge_after=0.02, purpose="vision_agent_step")

    assert result["ok"] is False and result["error"] == "wafer"
    assert result["metadata"]["hedge"] == {"fired_after_seconds": 0.02, "winner": "primary", "ok": False, "latency_ms": {"primary": 0, "hedge": 0}}


def test_an_exception_from_a_fast_primary_propagates() -> None:
    def call(provider, **kwargs):
        raise RuntimeError("transport broke")

    with pytest.raises(RuntimeError, match="transport broke"):
        hedged_call(call, _Provider(), hedge_after=1.0, purpose="vision_agent_step")
