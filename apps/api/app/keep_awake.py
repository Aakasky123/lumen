"""Keep the machine awake while a run is working.

Browser runs drive Chrome through CDP, and CDP input never reaches Windows' input queue, so to the
OS a run in progress looks like an idle machine: the display turns off, the laptop sleeps, and idle
screensavers (ASUS OLED Care Pixel Refresh) cover the screen the agent is working on. Desktop runs
inject real input but still go quiet while the brain thinks.

Every active run holds one shared power request (system + display required); the last run out
clears it. On Windows the request is a kernel handle, so a killed process releases it with no
cleanup, and ``powercfg /requests`` names Lumen as the holder. On macOS it is an IOKit power
assertion (``PreventUserIdleDisplaySleep``, listed by ``pmset -g assertions``), which the kernel
also releases when the process exits. Elsewhere this is a no-op.
"""

from __future__ import annotations

import ctypes
import functools
import logging
import platform
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from app.config import get_settings


logger = logging.getLogger(__name__)

_REASON = "Lumen is running a task"
_POWER_REQUEST_CONTEXT_VERSION = 0
_POWER_REQUEST_CONTEXT_SIMPLE_STRING = 0x1
_POWER_REQUEST_DISPLAY_REQUIRED = 0
_POWER_REQUEST_SYSTEM_REQUIRED = 1
_REQUEST_TYPES = (_POWER_REQUEST_SYSTEM_REQUIRED, _POWER_REQUEST_DISPLAY_REQUIRED)
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
# macOS: IOPMAssertionCreateWithName and IOPMAssertionRelease, with the assertion type and level.
_MAC_ASSERTION_TYPE = "PreventUserIdleDisplaySleep"
_MAC_ASSERTION_LEVEL_ON = 255  # kIOPMAssertionLevelOn
_MAC_IOKIT = "/System/Library/Frameworks/IOKit.framework/IOKit"
_MAC_CORE_FOUNDATION = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
_CF_STRING_ENCODING_UTF8 = 0x08000100
_IO_RETURN_SUCCESS = 0

_LOCK = threading.Lock()
_holders = 0
_request: int | None = None


class _DetailedReason(ctypes.Structure):
    _fields_ = [
        ("LocalizedReasonModule", ctypes.c_void_p),
        ("LocalizedReasonId", ctypes.c_ulong),
        ("ReasonStringCount", ctypes.c_ulong),
        ("ReasonStrings", ctypes.c_void_p),
    ]


class _Reason(ctypes.Union):
    _fields_ = [("Detailed", _DetailedReason), ("SimpleReasonString", ctypes.c_wchar_p)]


class _ReasonContext(ctypes.Structure):
    _fields_ = [("Version", ctypes.c_ulong), ("Flags", ctypes.c_ulong), ("Reason", _Reason)]


def _system() -> str:
    return platform.system().lower()


def _enabled() -> bool:
    return _system() in ("windows", "darwin") and bool(get_settings().keep_awake_enabled)


@functools.lru_cache(maxsize=1)
def _kernel32() -> Any:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.PowerCreateRequest.argtypes = [ctypes.POINTER(_ReasonContext)]
    kernel32.PowerCreateRequest.restype = ctypes.c_void_p
    kernel32.PowerSetRequest.argtypes = [ctypes.c_void_p, ctypes.c_int]
    kernel32.PowerSetRequest.restype = ctypes.c_int
    kernel32.PowerClearRequest.argtypes = [ctypes.c_void_p, ctypes.c_int]
    kernel32.PowerClearRequest.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    return kernel32


@functools.lru_cache(maxsize=1)
def _mac_power_api() -> tuple[Any, Any, Any, Any]:
    """(IOPMAssertionCreateWithName, IOPMAssertionRelease, CFStringCreateWithCString, CFRelease), typed."""
    iokit = ctypes.CDLL(_MAC_IOKIT)
    core_foundation = ctypes.CDLL(_MAC_CORE_FOUNDATION)
    create_assertion = iokit.IOPMAssertionCreateWithName
    create_assertion.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    create_assertion.restype = ctypes.c_int32
    release_assertion = iokit.IOPMAssertionRelease
    release_assertion.argtypes = [ctypes.c_uint32]
    release_assertion.restype = ctypes.c_int32
    cf_create_string = core_foundation.CFStringCreateWithCString
    cf_create_string.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    cf_create_string.restype = ctypes.c_void_p
    cf_release = core_foundation.CFRelease
    cf_release.argtypes = [ctypes.c_void_p]
    cf_release.restype = None
    return create_assertion, release_assertion, cf_create_string, cf_release


def _mac_cf_string(cf_create_string: Any, text: str) -> int:
    value = cf_create_string(None, text.encode("utf-8"), _CF_STRING_ENCODING_UTF8)
    if not value:
        raise OSError(f"CFStringCreateWithCString failed for {text!r}")
    return int(value)


def _mac_create_assertion() -> int:
    create_assertion, _release, cf_create_string, cf_release = _mac_power_api()
    assertion_type = _mac_cf_string(cf_create_string, _MAC_ASSERTION_TYPE)
    try:
        name = _mac_cf_string(cf_create_string, _REASON)
        try:
            assertion_id = ctypes.c_uint32(0)
            result = create_assertion(
                assertion_type, _MAC_ASSERTION_LEVEL_ON, name, ctypes.pointer(assertion_id)
            )
        finally:
            cf_release(name)
    finally:
        cf_release(assertion_type)
    if result != _IO_RETURN_SUCCESS:
        raise OSError(f"IOPMAssertionCreateWithName failed: 0x{result & 0xFFFFFFFF:08x}")
    return int(assertion_id.value)


def _mac_release_assertion(assertion_id: int) -> None:
    _create, release_assertion, _cf, _release_cf = _mac_power_api()
    result = release_assertion(assertion_id)
    if result != _IO_RETURN_SUCCESS:
        raise OSError(f"IOPMAssertionRelease failed: 0x{result & 0xFFFFFFFF:08x}")


def _create_request() -> int | None:
    if _system() == "darwin":
        return _mac_create_assertion()
    kernel32 = _kernel32()
    context = _ReasonContext(_POWER_REQUEST_CONTEXT_VERSION, _POWER_REQUEST_CONTEXT_SIMPLE_STRING)
    context.Reason.SimpleReasonString = _REASON
    handle = kernel32.PowerCreateRequest(ctypes.byref(context))
    if not handle or handle == _INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    for kind in _REQUEST_TYPES:
        if not kernel32.PowerSetRequest(handle, kind):
            error = ctypes.WinError(ctypes.get_last_error())
            _clear_request(handle)
            raise error
    return int(handle)


def _clear_request(handle: int) -> None:
    if _system() == "darwin":
        _mac_release_assertion(handle)
        return
    kernel32 = _kernel32()
    for kind in _REQUEST_TYPES:
        kernel32.PowerClearRequest(handle, kind)
    kernel32.CloseHandle(handle)
def _acquire() -> None:
    global _holders, _request
    with _LOCK:
        _holders += 1
        if _request is not None or not _enabled():
            return
        try:
            _request = _create_request()
        except OSError as exc:
            # Staying awake is a convenience; a run must never fail because the OS refused it.
            logger.warning("keep-awake power request unavailable: %s", exc)


def _release() -> None:
    global _holders, _request
    with _LOCK:
        _holders = max(0, _holders - 1)
        if _holders or _request is None:
            return
        handle, _request = _request, None
        try:
            _clear_request(handle)
        except OSError as exc:
            logger.warning("keep-awake power request release failed: %s", exc)


def holding() -> bool:
    """True while this process holds the power request."""
    return _request is not None


@contextmanager
def keep_awake() -> Iterator[None]:
    """Hold the display and system awake for the duration of the block."""
    _acquire()
    try:
        yield
    finally:
        _release()
