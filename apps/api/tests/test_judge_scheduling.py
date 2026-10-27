"""Judge-call scheduling: the per-provider limiter and overlapped per-action gates."""

from __future__ import annotations

import asyncio
import itertools
import threading
import time
from dataclasses import dataclass

import pytest

from app import gate_overlap, judge_limiter, model_call_ledger


@dataclass(frozen=True)
class _Row:
    id: int | None = 11
    provider_type: str = "openrouter"
    model_name: str = "qwen"


@pytest.fixture(autouse=True)
def _fresh_limiters(monkeypatch):
    monkeypatch.setattr(judge_limiter, "_limiters", {})
    monkeypatch.delenv("LUMEN_JUDGE_CONCURRENCY", raising=False)


def test_default_limit_is_two_and_row_override_parses(monkeypatch) -> None:
    assert judge_limiter.configured_limit(_Row()) == 2
    monkeypatch.setenv("LUMEN_JUDGE_CONCURRENCY", "default=3, 11=1, 12=0, junk")
    assert judge_limiter.configured_limit(_Row(id=11)) == 1
    assert judge_limiter.configured_limit(_Row(id=12)) == 0
    assert judge_limiter.configured_limit(_Row(id=13)) == 3


def test_limiter_caps_concurrency_and_reports_queue_wait() -> None:
    in_flight = 0
    peak = 0
    guard = threading.Lock()
    waits: list[int] = []

    def worker() -> None:
        nonlocal in_flight, peak
        with judge_limiter.slot(_Row(), "grounding_judge", deadline_monotonic=time.monotonic() + 10) as queued:
            assert queued["granted"]
            waits.append(queued["queue_wait_ms"])
            with guard:
                in_flight += 1
                peak = max(peak, in_flight)
            time.sleep(0.05)
            with guard:
                in_flight -= 1

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak == 2
    assert max(waits) >= 20  # the later calls queued behind a 50 ms hold


def test_queue_wait_counts_toward_the_deadline_and_the_call_is_not_made() -> None:
    held = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with judge_limiter.slot(_Row(), "grounding_judge"):
            held.set()
            release.wait(5)

    holders = [threading.Thread(target=holder) for _ in range(2)]
    for t in holders:
        t.start()
    held.wait(2)
    time.sleep(0.05)
    started = time.monotonic()
    with judge_limiter.slot(_Row(), "grounding_judge", deadline_monotonic=started + 0.15) as queued:
        assert queued["granted"] is False
    elapsed = time.monotonic() - started
    assert 0.1 <= elapsed < 1.0
    assert queued["queue_wait_ms"] >= 100
    # A deadline already in the past does not wait at all.
    with judge_limiter.slot(_Row(), "grounding_judge", deadline_monotonic=time.monotonic() - 1) as queued:
        assert queued["granted"] is False
    release.set()
    for t in holders:
        t.join()
    with judge_limiter.slot(_Row(), "grounding_judge", deadline_monotonic=time.monotonic() + 1) as queued:
        assert queued["granted"] is True


def test_other_purposes_and_unlimited_rows_pass_through(monkeypatch) -> None:
    with judge_limiter.slot(_Row(), "payment_floor_judge") as queued:
        assert queued == {"granted": True, "queue_wait_ms": 0}
    monkeypatch.setenv("LUMEN_JUDGE_CONCURRENCY", "11=0")
    with judge_limiter.slot(_Row(), "grounding_judge") as a, judge_limiter.slot(_Row(), "grounding_judge") as b, judge_limiter.slot(
        _Row(), "grounding_judge"
    ) as c:
        assert a["granted"] and b["granted"] and c["granted"]


def test_distinct_rows_have_their_own_slots() -> None:
    with judge_limiter.slot(_Row(id=11), "grounding_judge"), judge_limiter.slot(_Row(id=11), "grounding_judge"):
        with judge_limiter.slot(_Row(id=12), "grounding_judge", deadline_monotonic=time.monotonic() + 1) as other:
            assert other["granted"]


# --- overlapped gates: the combination equals the serial one -------------------------------------

_FIRST = ["complete", "pending", "error"]
_SECOND = ["clear", "payment", "fail_closed", "error"]


def _first_outcome(kind: str) -> dict:
    if kind == "error":
        raise RuntimeError("audit unavailable")
    return {"complete": kind == "complete"}


def _second_outcome(kind: str) -> dict:
    if kind == "error":
        raise ValueError("floor unavailable")
    return {"payment": kind == "payment", "fail_closed": kind == "fail_closed"}


def _combine(first: dict | None, first_error: Exception | None, second: dict | None, second_error: Exception | None):
    """The caller's decision logic, shared by both schedules: withhold on an incomplete or failed audit
    (the second gate then does not count), else the second gate decides; its error propagates."""
    if first_error is not None or not first or not first["complete"]:
        return ("withheld", None)
    if second_error is not None:
        return ("raised", type(second_error).__name__)
    if second["payment"] or second["fail_closed"]:
        return ("stop_for_user", None)
    return ("proceed", None)


async def _serial(first_kind: str, second_kind: str):
    first = first_error = second = second_error = None
    try:
        first = await asyncio.to_thread(_first_outcome, first_kind)
    except Exception as exc:  # noqa: BLE001
        first_error = exc
    if first_error is None and first["complete"]:
        try:
            second = await asyncio.to_thread(_second_outcome, second_kind)
        except Exception as exc:  # noqa: BLE001
            second_error = exc
    return _combine(first, first_error, second, second_error)


async def _overlapped(first_kind: str, second_kind: str):
    first = first_error = second = second_error = None
    overlap = gate_overlap.Overlapped(lambda: asyncio.to_thread(_second_outcome, second_kind))
    try:
        first = await asyncio.to_thread(_first_outcome, first_kind)
    except Exception as exc:  # noqa: BLE001
        first_error = exc
    if first_error is None and first["complete"]:
        try:
            second = await overlap.result()
        except Exception as exc:  # noqa: BLE001
            second_error = exc
    else:
        overlap.discard()
    return _combine(first, first_error, second, second_error)


@pytest.mark.parametrize("first_kind,second_kind", list(itertools.product(_FIRST, _SECOND)))
def test_overlapped_gates_combine_exactly_like_serial(first_kind: str, second_kind: str) -> None:
    async def run():
        serial = await _serial(first_kind, second_kind)
        overlapped = await _overlapped(first_kind, second_kind)
        await asyncio.sleep(0.05)  # a discarded worker's late error must not surface
        return serial, overlapped

    serial, overlapped = asyncio.run(run())
    assert overlapped == serial


def test_overlapped_gates_actually_run_side_by_side_and_keep_run_context() -> None:
    seen: list[int | None] = []

    def slow_second() -> str:
        seen.append(model_call_ledger.current_run())
        time.sleep(0.2)
        return "floor"

    def slow_first() -> str:
        time.sleep(0.2)
        return "audit"

    async def run():
        token = model_call_ledger.bind(4242)
        try:
            started = time.monotonic()
            overlap = gate_overlap.Overlapped(lambda: asyncio.to_thread(slow_second))
            first = await asyncio.to_thread(slow_first)
            second = await overlap.result()
            return first, second, time.monotonic() - started
        finally:
            model_call_ledger.release(token, 4242)

    first, second, elapsed = asyncio.run(run())
    assert (first, second) == ("audit", "floor")
    assert elapsed < 0.35  # serial would be ~0.4 s
    assert seen == [4242]
