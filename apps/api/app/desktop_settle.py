"""Adaptive post-action settle for the desktop bodies (Windows and macOS).

A fixed pause after a click or key press is too long for a window that answers at once and too short for a
dialog that takes half a second to open. The settle waits a minimum no shorter than the old fixed pause (a
reaction slower than that must not be missed by an early "stable"), then compares cheap frames (the screen
shrunk to a grayscale thumbnail) taken about ``interval`` apart until ``stable_frames`` consecutive frames
match one another across at least ``stable_window`` seconds (mean absolute difference at or under
``threshold``, so a blinking caret or a small spinner does not keep it waiting), and gives up at ``max_wait``. It never raises: a capture that fails falls back to the old fixed pause.

``INSTANT`` is the test hook (tests/conftest.py sets it): with it on, ``settle_after_action`` returns at once
without capturing, the same way ``extension_run._VISION_STEP_SETTLE_SECONDS`` is zeroed. ``settle_until_stable``
takes its clock, sleep, and grab as arguments, so the algorithm itself is tested against fake frame sequences.
"""

from __future__ import annotations

import time
from typing import Any, Callable, NamedTuple

from PIL import Image, ImageChops, ImageStat

# Never below the old fixed pause (FALLBACK_SECONDS, 0.22 s): a screen that is still right after an action may be
# about to react, and one matching pair 80 ms apart cannot tell "done" from "not started".
MIN_WAIT_SECONDS = 0.25
FRAME_INTERVAL_SECONDS = 0.08
# Stable = this many consecutive frames, all matching their neighbour, spanning at least this long.
STABLE_FRAMES = 3
STABLE_WINDOW_SECONDS = 0.15
MAX_WAIT_SECONDS = 1.2
# Mean absolute difference (0-255) between two thumbnails. Measured idle on a Retina Mac, 64x40: 0.001-0.44
# (menu-bar clock, other windows animating); a caret blink is under 0.05; a menu or dialog opening is 3+.
STABLE_THRESHOLD = 0.75
THUMB_SIZE = (64, 40)
# What the bodies slept before this module: used when frames cannot be captured.
FALLBACK_SECONDS = 0.22

INSTANT = False


class SettleResult(NamedTuple):
    stable: bool
    frames: int
    waited: float


def thumbnail(image: Any) -> Any:
    """The frame as a small grayscale image (resize first: converting a full Retina frame costs far more)."""
    return image.resize(THUMB_SIZE, Image.BOX).convert("L")


def frame_difference(a: Any, b: Any) -> float:
    return float(ImageStat.Stat(ImageChops.difference(a, b)).mean[0])


def settle_until_stable(
    grab: Callable[[], Any],
    *,
    min_wait: float = MIN_WAIT_SECONDS,
    interval: float = FRAME_INTERVAL_SECONDS,
    max_wait: float = MAX_WAIT_SECONDS,
    stable_frames: int = STABLE_FRAMES,
    stable_window: float = STABLE_WINDOW_SECONDS,
    threshold: float = STABLE_THRESHOLD,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    diff: Callable[[Any, Any], float] = frame_difference,
    shrink: Callable[[Any], Any] = thumbnail,
) -> SettleResult:
    """Wait for ``grab()`` frames to stop changing. Returns whether the screen held still for the window."""
    start = clock()
    deadline = start + max(0.0, max_wait)
    if min_wait > 0:
        sleep(min(min_wait, max(0.0, max_wait)))
    previous: Any = None
    frames = 0
    run = 0  # consecutive frames matching their predecessor, counting the first of the run
    run_start = start
    while True:
        frame_started = clock()
        try:
            current = shrink(grab())
        except Exception:  # noqa: BLE001 - a settle never fails an action
            rest = max(0.0, FALLBACK_SECONDS - (clock() - start))
            if rest:
                sleep(rest)
            return SettleResult(False, frames, clock() - start)
        frames += 1
        now = clock()
        if previous is not None and diff(previous, current) <= threshold:
            run += 1
        else:
            run = 1
            run_start = now
        if run >= max(2, stable_frames) and now - run_start >= stable_window:
            return SettleResult(True, frames, now - start)
        previous = current
        if now >= deadline:
            return SettleResult(False, frames, now - start)
        # Frames are `interval` apart from start to start: the capture itself counts toward the gap.
        gap = interval - (now - frame_started)
        if gap > 0:
            sleep(min(gap, max(0.0, deadline - now)))


def settle_after_action(grab: Callable[[], Any]) -> SettleResult:
    if INSTANT:
        return SettleResult(True, 0, 0.0)
    return settle_until_stable(grab)
