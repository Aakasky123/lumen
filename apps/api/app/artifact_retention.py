"""Bounded disk use for per-run screenshots.

Every run keeps its observation and model screenshots under ``<artifact_root>/runs/<run_id>/``. Image
files are the bulk of that and are only useful while a run is recent (review, replay bake-offs, the
live screenshot endpoint) or kept as an eval case. Older runs lose their image files and keep every
JSON record (observations, model inputs, manifests, receipts); readers already skip a missing image.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
_DAY_SECONDS = 86_400
_PRUNE_INTERVAL_SECONDS = _DAY_SECONDS


def protected_run_ids(artifact_root: Path) -> set[int]:
    """Runs saved as eval cases (``eval_candidates/run_<id>.json``) keep their frames."""
    folder = Path(artifact_root) / "eval_candidates"
    if not folder.is_dir():
        return set()
    ids: set[int] = set()
    for path in folder.glob("run_*.json"):
        match = re.fullmatch(r"run_(\d+)\.json", path.name)
        if match:
            ids.add(int(match.group(1)))
    return ids


def _newest_mtime(run_dir: Path) -> float:
    newest = 0.0
    for dirpath, _dirnames, filenames in os.walk(run_dir):
        for name in filenames:
            try:
                newest = max(newest, os.path.getmtime(os.path.join(dirpath, name)))
            except OSError:
                continue
    return newest


def prune_run_frames(
    runs_root: Path,
    *,
    keep_runs: int,
    keep_days: int,
    protected: set[int] | frozenset[int] = frozenset(),
    now: float | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Delete image files of runs outside both windows: the ``keep_runs`` most recently active runs
    and anything touched in the last ``keep_days`` days. Protected runs and all JSON stay."""
    stats = {"runs_seen": 0, "runs_pruned": 0, "files_deleted": 0, "bytes_freed": 0, "dry_run": dry_run}
    root = Path(runs_root)
    if not root.is_dir() or (keep_runs <= 0 and keep_days <= 0):
        return stats
    runs = [(path, _newest_mtime(path)) for path in root.iterdir() if path.is_dir() and path.name.isdigit()]
    stats["runs_seen"] = len(runs)
    runs.sort(key=lambda item: item[1], reverse=True)
    cutoff = (time.time() if now is None else now) - max(0, keep_days) * _DAY_SECONDS
    for index, (run_dir, newest) in enumerate(runs):
        if index < max(0, keep_runs) or newest >= cutoff or int(run_dir.name) in protected:
            continue
        pruned_any = False
        for dirpath, _dirnames, filenames in os.walk(run_dir):
            for name in filenames:
                if Path(name).suffix.lower() not in IMAGE_SUFFIXES:
                    continue
                path = os.path.join(dirpath, name)
                try:
                    size = os.path.getsize(path)
                    if not dry_run:
                        os.remove(path)
                except OSError:
                    continue
                stats["files_deleted"] += 1
                stats["bytes_freed"] += size
                pruned_any = True
        stats["runs_pruned"] += int(pruned_any)
    return stats


def start_background_pruning(artifact_root: Path, *, keep_runs: int, keep_days: int) -> threading.Thread | None:
    """Prune once shortly after startup, then daily, on a daemon thread (never on the event loop)."""
    if keep_runs <= 0 and keep_days <= 0:
        return None

    def loop() -> None:
        while True:
            try:
                stats = prune_run_frames(
                    Path(artifact_root) / "runs",
                    keep_runs=keep_runs,
                    keep_days=keep_days,
                    protected=protected_run_ids(Path(artifact_root)),
                )
                if stats["files_deleted"]:
                    logger.info("Pruned old run frames: %s", stats)
            except Exception:  # noqa: BLE001 - housekeeping must never take the backend down
                logger.exception("Run frame pruning failed")
            time.sleep(_PRUNE_INTERVAL_SECONDS)

    thread = threading.Thread(target=loop, name="run-frame-retention", daemon=True)
    thread.start()
    return thread
