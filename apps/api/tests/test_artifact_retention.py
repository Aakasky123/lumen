"""Old runs lose their screenshot files; recent, protected, and all JSON records stay."""

from __future__ import annotations

import os
import time
from pathlib import Path

from app.artifact_retention import prune_run_frames, protected_run_ids

DAY = 86_400


def _run(root: Path, run_id: int, *, age_days: float, now: float) -> Path:
    folder = root / "runs" / str(run_id) / "vision-trajectory"
    folder.mkdir(parents=True)
    for name, data in (("step-001.jpg", b"jpeg"), ("step-001-model.png", b"png"), ("step-001.json", b"{}")):
        path = folder / name
        path.write_bytes(data)
        stamp = now - age_days * DAY
        os.utime(path, (stamp, stamp))
    return folder


def test_only_old_unprotected_runs_lose_images_and_json_always_stays(tmp_path: Path) -> None:
    now = time.time()
    old = _run(tmp_path, 1, age_days=90, now=now)
    protected = _run(tmp_path, 2, age_days=90, now=now)
    recent_by_days = _run(tmp_path, 3, age_days=5, now=now)
    recent_by_count = _run(tmp_path, 4, age_days=60, now=now)
    (tmp_path / "eval_candidates").mkdir()
    (tmp_path / "eval_candidates" / "run_2.json").write_text("{}", encoding="utf-8")

    stats = prune_run_frames(
        # The two most recently active runs are 3 (5 days) and 4 (60 days); 4 stays only by count.
        tmp_path / "runs", keep_runs=2, keep_days=30, protected=protected_run_ids(tmp_path), now=now
    )

    assert sorted(p.name for p in old.iterdir()) == ["step-001.json"]
    for kept in (protected, recent_by_days, recent_by_count):
        assert sorted(p.name for p in kept.iterdir()) == ["step-001-model.png", "step-001.jpg", "step-001.json"]
    assert (stats["runs_pruned"], stats["files_deleted"], stats["bytes_freed"]) == (1, 2, len(b"jpeg") + len(b"png"))


def test_a_dry_run_or_zero_windows_delete_nothing(tmp_path: Path) -> None:
    now = time.time()
    old = _run(tmp_path, 7, age_days=400, now=now)

    assert prune_run_frames(tmp_path / "runs", keep_runs=0, keep_days=30, now=now, dry_run=True)["files_deleted"] == 2
    assert prune_run_frames(tmp_path / "runs", keep_runs=0, keep_days=0, now=now)["files_deleted"] == 0
    assert len(list(old.iterdir())) == 3
