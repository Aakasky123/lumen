"""Hedged browser decisions: a decision still unanswered after a few seconds gets one parallel second
request, and the first valid answer wins.

A decision normally returns in 1-4 s, but the tail is long: a runaway reply that generates to the
token cap (40 s for 8,192 tokens), or a host whose throughput sags. The second request skips an
OpenRouter row's preferred host and asks for the highest-throughput one, unless the row sets
allow_fallbacks false: then it goes to the same host. Either reply is an ordinary model decision; every downstream gate is unchanged. A reply cut off at its token cap
with no JSON in it is not an answer and waits for the other request. When both
fail, the first failure is returned exactly as an unhedged call would have returned it, so the
loop's own failover still sees it. The losing reply's cost is still tallied on the run.
"""

from __future__ import annotations

import contextvars
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import replace
from typing import Any, Callable

from app import eval_ablation, model_call_ledger
from app.model_provider import (
    normalize_endpoint_routing,
    openrouter_endpoint_completion_price,
    reply_cut_off_without_json,
)

# Only past long legitimate thinking: at 15 s the hedge fired on 20% of desktop-page decisions, won 19 of 49,
# saved ~6% of wall time and cost 31% of the spend. A runaway toward the 8,192-token cap (40-75 s on the
# pinned host) is still cut to about 35 s.
HEDGE_AFTER_SECONDS = 25.0
_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="model-hedge")


def _hedge_route(provider: Any) -> Any:
    """Skip the row's pinned host and take the highest-throughput host that costs no more per
    output token than the pinned one: the first request is slow either on that host or because it is
    generating a long reply, and a faster host helps both without a pricier tier (live: the uncapped
    sort chose a host at twice the pinned output price)."""
    pinned = getattr(provider, "endpoint_preference", None)
    routing = normalize_endpoint_routing(getattr(provider, "endpoint_routing", None)) or {}
    if routing.get("allow_fallbacks") is False:
        # A row pinned to its host only hedges on that same host: a fresh sample still rescues a runaway.
        return provider
    if getattr(provider, "provider_type", "") == "openrouter" and pinned:
        try:
            cap = openrouter_endpoint_completion_price(provider.base_url, provider.model_name, pinned)
        except Exception:  # noqa: BLE001 - an unknown price only removes the cap
            cap = None
        return replace(provider, endpoint_preference=None, endpoint_avoid=pinned, endpoint_max_completion_price=cap)
    return provider


def _outcome(future: Future) -> tuple[dict[str, Any] | None, BaseException | None]:
    error = future.exception()
    if error is not None:
        return None, error
    result = future.result()
    return (result if isinstance(result, dict) else {}), None


def hedged_call(
    call: Callable[..., dict[str, Any]],
    provider: Any,
    *,
    hedge_after: float = HEDGE_AFTER_SECONDS,
    **kwargs: Any,
) -> dict[str, Any]:
    if eval_ablation.brain_overrides_active():
        return eval_ablation.call_brain(hedged_call, call, provider, hedge_after=hedge_after, **kwargs)
    run_id = model_call_ledger.current_run()
    purpose = str(kwargs.get("purpose") or "")
    primary = _POOL.submit(contextvars.copy_context().run, call, provider, **kwargs)
    done, _ = wait([primary], timeout=hedge_after)
    if done:
        result, error = _outcome(primary)
        if error is not None:
            raise error
        return result
    hedge = _POOL.submit(contextvars.copy_context().run, call, _hedge_route(provider), **kwargs)
    labels = {primary: "primary", hedge: "hedge"}
    pending = {primary, hedge}
    returned: Future | None = None
    cut_off: Future | None = None
    while pending and returned is None:
        done, pending = wait(pending, return_when=FIRST_COMPLETED)
        for future in done:
            result, error = _outcome(future)
            if returned is not None or error is not None or not result.get("ok"):
                continue
            # A reply cut off at its token cap with no JSON in it is no answer: it must not beat the
            # other request, which may still answer (live, every such hedge "win" came from a host
            # that ran 8,192 tokens of prose to the cap in ~20 s while the pinned host was thinking).
            if reply_cut_off_without_json(result.get("metadata")):
                cut_off = cut_off or future
                continue
            returned = future
    # Neither answered: a cut-off reply (the loop recovers those within the step) over the primary's
    # failure; otherwise the primary's failure, exactly as an unhedged call would have returned it.
    returned = returned or cut_off or primary
    for future in labels:
        if future is returned:
            continue
        if future.done():
            model_call_ledger.record_for(run_id, f"{purpose}:hedge_extra", _outcome(future)[0] or {})
        else:
            # The slower request still finishes in the background; its cost belongs to the run.
            future.add_done_callback(
                lambda finished, run=run_id: model_call_ledger.record_for(
                    run, f"{purpose}:hedge_extra", _outcome(finished)[0] or {}
                )
            )
    result, error = _outcome(returned)
    if error is not None:
        raise error
    result = dict(result or {})
    metadata = dict(result.get("metadata") or {})
    latencies = {
        label: int(((_outcome(future)[0] or {}).get("metadata") or {}).get("latency_ms") or 0)
        for future, label in labels.items()
        if future.done() and future.exception() is None
    }
    metadata["hedge"] = {
        "fired_after_seconds": hedge_after,
        "winner": labels[returned],
        "ok": bool(result.get("ok")),
        "latency_ms": latencies,
    }
    result["metadata"] = metadata
    return result
