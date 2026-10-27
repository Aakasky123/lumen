"""The desktop body for this machine's OS: app.desktop_body on Windows, app.desktop_body_macos on macOS.

Callers import the body and its status functions from here; every name is looked up on the platform
module at call time, so a test that pins the module (``pin``) or patches a function on it still applies.
"""

from __future__ import annotations

import re
import sys
from types import ModuleType
from typing import Any

_PINNED: ModuleType | None = None


def module() -> ModuleType:
    if _PINNED is not None:
        return _PINNED
    if sys.platform == "darwin":
        from app import desktop_body_macos

        return desktop_body_macos
    from app import desktop_body

    return desktop_body


def pin(platform_module: ModuleType | None) -> None:
    """Use this module whatever the OS (tests); None goes back to the OS's own."""
    global _PINNED
    _PINNED = platform_module


def desktop_control_status() -> dict[str, Any]:
    return module().desktop_control_status()


def set_runtime_enabled(enabled: bool) -> bool:
    return module().set_runtime_enabled(enabled)


def pointer_events(since: int = 0) -> dict[str, Any]:
    return module().pointer_events(since)


def window_ids_snapshot() -> frozenset[str]:
    return module().window_ids_snapshot()


def launch_application_named(name_fragment: str) -> bool:
    return module().launch_application_named(name_fragment)


def chrome_profiles() -> list[dict[str, str]]:
    return module().chrome_profiles()


def screen_locked() -> bool:
    """Whether the computer's screen is locked (macOS reads it; elsewhere unknown, so False)."""
    locked = getattr(module(), "screen_locked", None)
    return bool(locked()) if callable(locked) else False


def bring_browser_to_front() -> bool:
    """Raise the user's Chrome where the OS refuses the extension's own focus request (macOS); elsewhere the
    extension raises its window itself."""
    raise_browser = getattr(module(), "bring_browser_to_front", None)
    return bool(raise_browser()) if callable(raise_browser) else False


def request_permissions() -> dict[str, bool]:
    """Show the OS's own permission prompts where it has them (macOS); nothing to ask elsewhere."""
    request = getattr(module(), "request_permissions", None)
    return request() if callable(request) else {}


def os_text(text: str) -> str:
    """User-facing and prompt wording written for Windows ("a Windows app", "Explorer"), said for this
    machine's desktop. Unchanged on Windows: the platform module's OS_TEXT_REPLACEMENTS are empty there."""
    for windows_phrase, os_phrase in getattr(module(), "OS_TEXT_REPLACEMENTS", ()):
        text = text.replace(windows_phrase, os_phrase)
    return text


_USE_VERBS = r"(?:in|on|using|with|via|through|use|open|launch|from)"
# What may follow a browser name that is an instruction to use the app: the end, punctuation, a conjunction or
# action verb, "browser"/"app", or an address. A noun continuing a title or activity ("Brave New World",
# "Opera website", "safari in Kenya", "Vivaldi's Four Seasons") is not.
_APP_TAIL = (
    r"(?=\s*(?:$|[.,;:!?)\]]|\("
    r"|(?:browser|app|application|to|and|then|or|so|please|go|navigate|open|search|find|visit|read|type|click|log|sign|check|look)\b"
    r"|https?://|www\.|[\w-]+\.[a-z]{2,}\b))"
)
_NEGATION_BEFORE = re.compile(
    r"(?:don'?t|do\s+not|never|not|no|without|avoid|instead\s+of|rather\s+than|except)\s+(?:\w+\s+){0,2}$", re.IGNORECASE
)


def _browser_mentions(text: str, names: str) -> list[str]:
    """Each non-negated mention of one of `names` as the browser app to use, in order."""
    pattern = re.compile(
        r"\b" + _USE_VERBS + r"\s+(?:the\s+|my\s+)?(?:google\s+)?(" + names + r")\b" + _APP_TAIL
        + r"|\b(" + names + r")\s+(?:app|application|browser)\b",
        re.IGNORECASE,
    )
    found = []
    for match in pattern.finditer(text):
        if _NEGATION_BEFORE.search(text[: match.start()][-40:]):
            continue
        found.append(match.group(1) or match.group(2))
    return found


def desktop_surface_usable() -> bool:
    """Whether the desktop body can run now (available and not paused). False when permissions are missing or
    control is off, and when the status cannot be read."""
    try:
        status = desktop_control_status()
        return bool(status.get("available") and status.get("enabled"))
    except Exception:  # noqa: BLE001 - unknown status is not usable
        return False


def named_other_browser(text: str) -> str | None:
    """The browser app other than Chrome that `text` clearly tells Lumen to use ("in Safari open ...", "Launch
    Safari and ...", "the Firefox browser"), when this OS's desktop body is the only way to drive it; None on
    Windows, which has no such list, when none is named, when the mention is negated ("don't use Safari") or part of
    a title or activity ("Brave New World", "go on safari"), or when Chrome is also named as the browser to use."""
    apps = getattr(module(), "OTHER_BROWSER_APPS", ())
    if not apps:
        return None
    text = str(text or "")
    if _browser_mentions(text, "chrome"):
        return None
    found = _browser_mentions(text, "|".join(apps))
    return found[0] if found else None


def permission_message() -> str:
    """What the user is told when the desktop body is missing an OS permission it needs."""
    message = getattr(module(), "permission_message", None)
    if callable(message):
        return message()
    return "I cannot start this computer task because Lumen Desktop is missing a permission it needs."


def __getattr__(name: str) -> Any:
    # DesktopBody and any other module attribute: the platform module's own, looked up now.
    if name.startswith("__"):
        raise AttributeError(name)
    return getattr(module(), name)
