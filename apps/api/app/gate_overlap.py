"""Run a per-action gate beside an independent one, then consume it exactly as the serial code would.

Gates are overlapped only when neither one's input depends on the other's output and the second is
read-only (a judge call). The combination logic stays where it was, in the caller, and reads the
second gate at the same point as before:

* the second gate's result (or exception) is observed only when the caller reaches the point where
  the serial code would have called it, so precedence and fail-closed behavior are unchanged;
* when the first gate ends the action (a withhold, a cancel, an exception), discard() drains the
  second without letting its late result or error affect the run.

The worker runs through asyncio.to_thread, which copies the caller's contextvars, so
model_call_ledger.current_run() and the judge deadline propagate to it.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Generic, TypeVar

T = TypeVar("T")


def _drain(task: "asyncio.Future[Any]") -> None:
    try:
        task.exception()
    except BaseException:  # noqa: BLE001 - cancelled or failed: nothing here may affect the run
        pass


class Overlapped(Generic[T]):
    def __init__(self, factory: Callable[[], Awaitable[T]]) -> None:
        self._task: asyncio.Task[T] = asyncio.ensure_future(factory())

    def result(self) -> Awaitable[T]:
        """Await the gate; raises exactly what the serial call would have raised."""
        return self._task

    def discard(self) -> None:
        if self._task.done():
            _drain(self._task)
        else:
            self._task.add_done_callback(_drain)
