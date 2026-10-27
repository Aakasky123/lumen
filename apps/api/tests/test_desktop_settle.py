"""The adaptive post-action settle: fake frame sequences, a fake clock, no real waiting."""

from __future__ import annotations

from typing import Any

from PIL import Image

from app import desktop_settle as ds


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _sequence(clock: _Clock, frames: list[Any], capture_cost: float = 0.03):
    """Grab returning each frame in turn (the last repeats); every grab costs `capture_cost` seconds."""
    calls: list[int] = []

    def grab() -> Any:
        clock.now += capture_cost
        index = min(len(calls), len(frames) - 1)
        calls.append(index)
        return frames[index]

    grab.calls = calls  # type: ignore[attr-defined]
    return grab


def _flat(shade: int) -> Image.Image:
    return Image.new("RGB", (640, 400), (shade,) * 3)


def _settle(clock: _Clock, grab, **kwargs):
    return ds.settle_until_stable(grab, sleep=clock.sleep, clock=clock.clock, **kwargs)


def test_the_minimum_wait_is_never_below_the_old_fixed_pause() -> None:
    assert ds.MIN_WAIT_SECONDS >= ds.FALLBACK_SECONDS


def test_a_screen_already_still_settles_after_the_minimum_and_a_stable_window() -> None:
    clock = _Clock()
    grab = _sequence(clock, [_flat(10)])
    result = _settle(clock, grab)
    assert result.stable and result.frames >= ds.STABLE_FRAMES and len(grab.calls) == result.frames
    assert clock.sleeps[0] == ds.MIN_WAIT_SECONDS
    assert result.waited >= ds.MIN_WAIT_SECONDS + ds.STABLE_WINDOW_SECONDS - 0.1
    assert result.waited < 0.8


def test_a_late_change_is_not_missed() -> None:
    # Identical frames until 0.4 s after the action, then a dialog: the settle must not return before it shows.
    clock = _Clock()
    base, dialog = _flat(240), _flat(240)
    dialog.paste((20, 20, 20), (100, 80, 500, 320))
    seen: list[float] = []

    def grab() -> Any:
        clock.now += 0.03
        seen.append(clock.now)
        return base if clock.now < 0.4 else dialog

    result = _settle(clock, grab)
    assert result.stable and result.waited >= 0.4 + ds.STABLE_WINDOW_SECONDS - 0.05
    assert any(t >= 0.4 for t in seen)


def test_a_single_matching_pair_is_not_enough() -> None:
    # Two equal frames, a change, then still: the early pair must not count.
    clock = _Clock()
    grab = _sequence(clock, [_flat(10), _flat(10), _flat(200), _flat(200), _flat(200), _flat(200)])
    result = _settle(clock, grab)
    assert result.stable and result.frames >= 5


def test_it_waits_while_the_screen_changes_then_stops_at_the_first_match() -> None:
    clock = _Clock()
    grab = _sequence(clock, [_flat(s) for s in (10, 60, 120, 200, 200, 90)])
    result = _settle(clock, grab)
    assert result.stable and result.frames >= 6 and len(grab.calls) == result.frames
    assert result.waited >= ds.MIN_WAIT_SECONDS + 5 * 0.08


def test_a_screen_that_never_settles_stops_at_the_cap() -> None:
    clock = _Clock()
    grab = _sequence(clock, [_flat((i * 40) % 250) for i in range(200)])
    result = _settle(clock, grab, max_wait=1.0)
    assert not result.stable
    assert 1.0 <= result.waited < 1.0 + ds.FRAME_INTERVAL_SECONDS + 0.05
    assert sum(clock.sleeps) <= 1.0 + 1e-9


def test_a_caret_sized_flicker_is_ignored() -> None:
    base = _flat(240)
    caret = base.copy()
    caret.paste((0, 0, 0), (300, 200, 303, 216))
    clock = _Clock()
    result = _settle(clock, _sequence(clock, [base, caret, base]))
    assert result.stable and result.frames >= ds.STABLE_FRAMES


def test_a_dialog_opening_is_a_real_change() -> None:
    base = _flat(240)
    dialog = base.copy()
    dialog.paste((20, 20, 20), (100, 80, 500, 320))
    clock = _Clock()
    result = _settle(clock, _sequence(clock, [base, dialog, dialog, dialog]))
    assert result.stable and result.frames >= 4


def test_a_failing_capture_falls_back_to_the_old_fixed_pause() -> None:
    clock = _Clock()

    def grab() -> Any:
        raise RuntimeError("no screen recording permission")

    result = _settle(clock, grab)
    assert not result.stable and result.frames == 0
    assert clock.now >= ds.FALLBACK_SECONDS  # the minimum wait already covered the old pause
    assert clock.now < ds.FALLBACK_SECONDS + 0.1


def test_the_test_hook_makes_the_action_settle_instant(monkeypatch) -> None:
    called: list[int] = []
    monkeypatch.setattr(ds, "INSTANT", True)
    assert ds.settle_after_action(lambda: called.append(1)).stable and not called

    monkeypatch.setattr(ds, "INSTANT", False)
    seen: dict[str, Any] = {}

    def fake(grab, **kwargs):
        seen["grab"] = grab
        return ds.SettleResult(True, 2, 0.1)

    monkeypatch.setattr(ds, "settle_until_stable", fake)
    marker = object()
    assert ds.settle_after_action(marker).frames == 2 and seen["grab"] is marker  # type: ignore[arg-type]
