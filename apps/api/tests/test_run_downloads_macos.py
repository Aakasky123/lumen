"""macOS Mark of the Web: a browser-downloaded file carries the com.apple.quarantine attribute.

The live test (LUMEN_MAC_LIVE=1 on macOS) sets and reads the real attribute with /usr/bin/xattr.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from app import run_downloads


QUARANTINE = b"com.apple.quarantine"


class _FakeGetxattr:
    """Stands in for libc getxattr: answers a size query (NULL buffer) from a table of path -> attributes."""

    def __init__(self, attributes: dict[str, set[bytes]]) -> None:
        self.attributes = attributes
        self.calls: list[tuple[bytes, bytes]] = []

    def __call__(self, path: bytes, name: bytes, value, size: int, position: int, options: int) -> int:
        assert value is None and size == 0 and position == 0 and options == 0
        self.calls.append((path, name))
        return 4 if name in self.attributes.get(path.decode(), set()) else -1


@pytest.fixture
def mac(monkeypatch):
    monkeypatch.setattr(run_downloads.sys, "platform", "darwin")
    fake = _FakeGetxattr({})
    monkeypatch.setattr(run_downloads, "_mac_getxattr", lambda: fake)
    return fake


def test_a_quarantined_file_carries_the_mark(tmp_path: Path, mac) -> None:
    path = tmp_path / "report.pdf"
    path.write_bytes(b"%PDF")
    mac.attributes[str(path)] = {QUARANTINE}
    assert run_downloads.has_mark_of_the_web(str(path)) is True
    assert mac.calls == [(os.fsencode(str(path)), QUARANTINE)]


def test_a_file_the_user_saved_without_quarantine_has_no_mark(tmp_path: Path, mac) -> None:
    path = tmp_path / "saved.pdf"
    path.write_bytes(b"%PDF")
    assert run_downloads.has_mark_of_the_web(str(path)) is False


def test_another_attribute_is_not_the_mark(tmp_path: Path, mac) -> None:
    path = tmp_path / "tagged.pdf"
    path.write_bytes(b"%PDF")
    mac.attributes[str(path)] = {b"com.apple.metadata:kMDItemWhereFroms"}
    assert run_downloads.has_mark_of_the_web(str(path)) is False


def test_a_missing_file_has_no_mark(tmp_path: Path, mac) -> None:
    assert run_downloads.has_mark_of_the_web(str(tmp_path / "gone.pdf")) is False


def test_the_check_reads_no_value_and_fails_closed_when_libc_is_unusable(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(run_downloads.sys, "platform", "darwin")

    def unusable():
        raise OSError("libSystem unavailable")

    monkeypatch.setattr(run_downloads, "_mac_getxattr", unusable)
    assert run_downloads.has_mark_of_the_web(str(tmp_path / "x.pdf")) is False


def test_other_systems_keep_the_old_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(run_downloads.sys, "platform", "linux")

    def must_not_run():
        raise AssertionError("getxattr must not be used off macOS")

    monkeypatch.setattr(run_downloads, "_mac_getxattr", must_not_run)
    assert run_downloads.has_mark_of_the_web(str(tmp_path / "any.pdf")) is True


def test_a_quarantined_download_is_listed_and_an_unmarked_one_is_not(tmp_path: Path, mac, monkeypatch) -> None:
    snapshot = run_downloads.make_snapshot(run_downloads.scan_folder(tmp_path))
    snapshot["recent_names"] = []
    downloaded = tmp_path / "invoice.pdf"
    downloaded.write_bytes(b"%PDF")
    saved = tmp_path / "saved.pdf"
    saved.write_bytes(b"%PDF")
    mac.attributes[str(downloaded)] = {QUARANTINE}
    # Past the Mark-of-the-Web grace after creation, so an unmarked file is not treated as still arriving.
    real_time = time.time
    monkeypatch.setattr(run_downloads, "time", types.SimpleNamespace(time=lambda: real_time() + 3600.0))
    names = [item["name"] for item in run_downloads.list_downloads(snapshot, run_downloads.scan_folder(tmp_path))]
    assert names == ["invoice.pdf"]


@pytest.mark.skipif(
    not (sys.platform == "darwin" and os.environ.get("LUMEN_MAC_LIVE")),
    reason="live macOS quarantine attribute: set LUMEN_MAC_LIVE=1 on macOS",
)
def test_live_quarantine_attribute_is_read_from_the_file(tmp_path: Path) -> None:
    marked = tmp_path / "marked.pdf"
    plain = tmp_path / "plain.pdf"
    marked.write_bytes(b"%PDF")
    plain.write_bytes(b"%PDF")
    subprocess.run(
        ["/usr/bin/xattr", "-w", "com.apple.quarantine", "0081;00000000;Lumen Test;", str(marked)],
        check=True,
    )
    assert run_downloads.has_mark_of_the_web(str(marked)) is True
    assert run_downloads.has_mark_of_the_web(str(plain)) is False
