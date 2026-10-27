"""Process-wide concurrency limiter for judge calls, per provider row.

Several runs and lanes share one judge provider. Measured on the grounding judge (Qwen, provider 11):
p50 17 s alone, 36 s with three or more overlapping calls. Past a small number in flight extra calls
only slow each other, so a bounded semaphore per provider row queues the rest.

* Thread-safe: calls come from asyncio.to_thread workers and executors.
* The wait counts toward the caller's own deadline. A call that cannot get a slot before its deadline
  is not made: the slot reports granted=False and the judge reports "no verdict", which every gate
  already treats as fail-closed. Nothing waits unbounded (MAX_QUEUE_WAIT_SECONDS caps it too).
* Limits are per API process. A second API process (another port) has its own limiter, so two
  processes sharing a provider may have up to 2x the configured number in flight.
* Configure with LUMEN_JUDGE_CONCURRENCY, e.g. "default=2,11=3,12=0": a row id maps to its
  limit, 0 means unlimited. The default (2) applies to any row without its own entry.
"""

from __future__ import annotations

import contextvars
import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

DEFAULT_LIMIT = 2
# Purposes limited so far; others pass straight through.
LIMITED_PURPOSES = frozenset({"grounding_judge"})
# A queue wait never exceeds this even with no deadline (no unbounded waits).
MAX_QUEUE_WAIT_SECONDS = 120.0

# The calling judge's own deadline (time.monotonic()), set around its provider call.
call_deadline: contextvars.ContextVar[float | None] = contextvars.ContextVar("lumen_judge_call_deadline", default=None)

_lock = threading.Lock()
_limiters: dict[tuple[str, int], "_Limiter"] = {}


class _Limiter:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._cond = threading.Condition()
        self._in_flight = 0

    def acquire(self, timeout: float) -> bool:
        end = time.monotonic() + max(0.0, timeout)
        with self._cond:
            while self._in_flight >= self.limit:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            self._in_flight += 1
            return True

    def release(self) -> None:
        with self._cond:
            self._in_flight -= 1
            self._cond.notify()


def provider_key(provider: Any) -> str:
    pid = getattr(provider, "id", None)
    if pid is not None:
        return f"id:{pid}"
    return f"{getattr(provider, 'provider_type', '')}:{getattr(provider, 'model_name', '')}"


def configured_limit(provider: Any) -> int:
    """Concurrency for the provider's row; 0 means unlimited."""
    default = DEFAULT_LIMIT
    own: int | None = None
    pid = getattr(provider, "id", None)
    for part in str(os.environ.get("LUMEN_JUDGE_CONCURRENCY") or "").split(","):
        name, _, raw = part.partition("=")
        try:
            value = max(0, int(raw.strip()))
        except ValueError:
            continue
        name = name.strip()
        if name == "default":
            default = value
        elif pid is not None and name == str(pid):
            own = value
    return default if own is None else own


def _limiter_for(provider: Any, purpose: str, limit: int) -> _Limiter:
    key = (f"{purpose}|{provider_key(provider)}", limit)
    with _lock:
        limiter = _limiters.get(key)
        if limiter is None:
            limiter = _limiters[key] = _Limiter(limit)
        return limiter


@contextmanager
def slot(provider: Any, purpose: str, *, deadline_monotonic: float | None = None) -> Iterator[dict[str, Any]]:
    """Hold a provider slot for the body. Yields {"granted": bool, "queue_wait_ms": int}; when
    granted is False the deadline passed while queued and the caller must not make the call."""
    limit = configured_limit(provider) if purpose in LIMITED_PURPOSES else 0
    if limit <= 0:
        yield {"granted": True, "queue_wait_ms": 0}
        return
    limiter = _limiter_for(provider, purpose, limit)
    started = time.monotonic()
    budget = MAX_QUEUE_WAIT_SECONDS
    if deadline_monotonic is not None:
        budget = min(budget, deadline_monotonic - started)
    granted = limiter.acquire(budget)
    waited_ms = int((time.monotonic() - started) * 1000)
    if not granted:
        yield {"granted": False, "queue_wait_ms": waited_ms}
        return
    try:
        yield {"granted": True, "queue_wait_ms": waited_ms}
    finally:
        limiter.release()
