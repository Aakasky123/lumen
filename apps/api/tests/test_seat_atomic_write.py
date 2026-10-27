import os
import threading

from app.agent_eval import brain_seat, judge_seat


def test_write_atomic_replaces_and_skips_same_size(tmp_path, monkeypatch):
    target = tmp_path / "system-abc.txt"
    judge_seat.write_atomic(target, "hello", skip_if_same_size=True)
    mtime = target.stat().st_mtime_ns
    calls = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b: (calls.append((a, b)), real(a, b))[1])
    judge_seat.write_atomic(target, "hello", skip_if_same_size=True)
    assert not calls and target.stat().st_mtime_ns == mtime
    judge_seat.write_atomic(target, "world!")
    assert calls and target.read_text() == "world!"
    assert not list(tmp_path.glob("*.tmp"))
    assert brain_seat.write_atomic is judge_seat.write_atomic


def test_concurrent_writers_never_expose_partial_file(tmp_path):
    target = tmp_path / "prompt.json"
    text = "x" * 200_000
    judge_seat.write_atomic(target, text)
    stop = threading.Event()
    bad = []

    def writer():
        while not stop.is_set():
            judge_seat.write_atomic(target, text)

    threads = [threading.Thread(target=writer) for _ in range(3)]
    for t in threads:
        t.start()
    for _ in range(300):
        try:
            got = target.read_text()
        except OSError:
            continue
        if got != text:
            bad.append(len(got))
    stop.set()
    for t in threads:
        t.join()
    assert not bad
