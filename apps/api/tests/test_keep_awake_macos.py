"""macOS keep-awake: an IOKit power assertion, exercised through fake native calls.

The live test (LUMEN_MAC_LIVE=1 on macOS) takes a real assertion and reads it back with ``pmset``.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys

import pytest

from app import keep_awake


class _FakeIOKit:
    """Stands in for IOPMAssertionCreateWithName / IOPMAssertionRelease / CoreFoundation string calls."""

    def __init__(self, *, create_result: int = 0, release_result: int = 0) -> None:
        self.create_result = create_result
        self.release_result = release_result
        self.created: list[tuple[str, int, str]] = []
        self.released: list[int] = []
        self.cf_released: list[int] = []
        self._strings: dict[int, str] = {}
        self._next_string = 0x1000
        self._next_id = 4242

    def cf_create_string(self, _alloc, text: bytes, _encoding: int) -> int:
        self._next_string += 0x10
        self._strings[self._next_string] = text.decode("utf-8")
        return self._next_string

    def cf_release(self, value: int) -> None:
        self.cf_released.append(value)

    def create_assertion(self, assertion_type, level, name, out_id) -> int:
        self.created.append((self._strings[assertion_type], level, self._strings[name]))
        out_id.contents.value = self._next_id
        return self.create_result

    def release_assertion(self, assertion_id: int) -> int:
        self.released.append(assertion_id)
        return self.release_result


@pytest.fixture
def fake_iokit(monkeypatch):
    fake = _FakeIOKit()
    monkeypatch.setattr(keep_awake, "_mac_power_api", lambda: (fake.create_assertion, fake.release_assertion, fake.cf_create_string, fake.cf_release))
    monkeypatch.setattr(keep_awake, "_system", lambda: "darwin")
    monkeypatch.setattr(keep_awake, "_enabled", lambda: True)
    monkeypatch.setattr(keep_awake, "_holders", 0)
    monkeypatch.setattr(keep_awake, "_request", None)
    return fake


def test_darwin_takes_a_display_and_idle_assertion_with_the_task_reason(fake_iokit) -> None:
    with keep_awake.keep_awake():
        assert keep_awake.holding()
    assert fake_iokit.created == [("PreventUserIdleDisplaySleep", 255, keep_awake._REASON)]
    assert fake_iokit.released == [4242]
    assert not keep_awake.holding()
    # Both CFString arguments are released after each call, so nothing leaks per run.
    assert len(fake_iokit.cf_released) == 2


def test_overlapping_darwin_runs_share_one_assertion(fake_iokit) -> None:
    with keep_awake.keep_awake():
        with keep_awake.keep_awake():
            pass
    assert len(fake_iokit.created) == 1
    assert fake_iokit.released == [4242]


def test_refused_assertion_never_fails_the_run(monkeypatch) -> None:
    fake = _FakeIOKit(create_result=-536870207)  # kIOReturnNotPrivileged-style refusal
    monkeypatch.setattr(keep_awake, "_mac_power_api", lambda: (fake.create_assertion, fake.release_assertion, fake.cf_create_string, fake.cf_release))
    monkeypatch.setattr(keep_awake, "_system", lambda: "darwin")
    monkeypatch.setattr(keep_awake, "_enabled", lambda: True)
    monkeypatch.setattr(keep_awake, "_holders", 0)
    monkeypatch.setattr(keep_awake, "_request", None)
    ran: list[bool] = []
    with keep_awake.keep_awake():
        ran.append(True)
        assert not keep_awake.holding()
    assert ran == [True]
    assert fake.released == []


def test_failed_release_is_logged_and_the_state_is_still_cleared(monkeypatch, caplog) -> None:
    fake = _FakeIOKit(release_result=-1)
    monkeypatch.setattr(keep_awake, "_mac_power_api", lambda: (fake.create_assertion, fake.release_assertion, fake.cf_create_string, fake.cf_release))
    monkeypatch.setattr(keep_awake, "_system", lambda: "darwin")
    monkeypatch.setattr(keep_awake, "_enabled", lambda: True)
    monkeypatch.setattr(keep_awake, "_holders", 0)
    monkeypatch.setattr(keep_awake, "_request", None)
    with caplog.at_level("WARNING"):
        with keep_awake.keep_awake():
            pass
    assert not keep_awake.holding()
    assert "power request release failed" in caplog.text


def test_darwin_is_enabled_only_when_the_setting_is_on(monkeypatch) -> None:
    monkeypatch.setattr(keep_awake, "_system", lambda: "darwin")
    monkeypatch.setattr(keep_awake, "get_settings", lambda: type("S", (), {"keep_awake_enabled": True})())
    assert keep_awake._enabled()
    monkeypatch.setattr(keep_awake, "get_settings", lambda: type("S", (), {"keep_awake_enabled": False})())
    assert not keep_awake._enabled()
    monkeypatch.setattr(keep_awake, "_system", lambda: "linux")
    monkeypatch.setattr(keep_awake, "get_settings", lambda: type("S", (), {"keep_awake_enabled": True})())
    assert not keep_awake._enabled()


def test_the_native_wiring_declares_the_iokit_signatures(monkeypatch) -> None:
    """The real ctypes path: the library handles get their argument and return types declared."""

    class _Fn:
        pass

    created: dict[str, _Fn] = {}

    class _Library:
        def __init__(self, path: str) -> None:
            self.path = path

        def __getattr__(self, name: str) -> _Fn:
            fn = created.setdefault(f"{self.path.rsplit('/', 1)[-1]}.{name}", _Fn())
            return fn

    monkeypatch.setattr(keep_awake.ctypes, "CDLL", _Library)
    keep_awake._mac_power_api.cache_clear()
    try:
        create, release, cf_create, cf_release = keep_awake._mac_power_api()
    finally:
        keep_awake._mac_power_api.cache_clear()
    assert created["IOKit.IOPMAssertionCreateWithName"] is create
    assert create.argtypes == [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    assert create.restype is ctypes.c_int32
    assert release.argtypes == [ctypes.c_uint32] and release.restype is ctypes.c_int32
    assert cf_create.argtypes == [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    assert cf_create.restype is ctypes.c_void_p
    assert cf_release.argtypes == [ctypes.c_void_p]


@pytest.mark.skipif(
    not (sys.platform == "darwin" and os.environ.get("LUMEN_MAC_LIVE")),
    reason="live macOS power assertion: set LUMEN_MAC_LIVE=1 on macOS",
)
def test_live_assertion_is_listed_by_pmset_while_held(monkeypatch) -> None:
    monkeypatch.setattr(keep_awake, "_enabled", lambda: True)
    monkeypatch.setattr(keep_awake, "_holders", 0)
    monkeypatch.setattr(keep_awake, "_request", None)

    def listed() -> str:
        return subprocess.run(["/usr/bin/pmset", "-g", "assertions"], capture_output=True, text=True, check=True).stdout

    with keep_awake.keep_awake():
        assert keep_awake.holding()
        assert "PreventUserIdleDisplaySleep" in listed()
        assert keep_awake._REASON in listed()
    assert not keep_awake.holding()
    assert keep_awake._REASON not in listed()
