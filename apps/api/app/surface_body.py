"""One run, two surfaces: the page (browser extension) and the real screen (OS computer use).

The browser body sees and operates only the page, precisely. Everything outside it - Chrome's own
permission prompts, save-password and download bubbles, print, file and sign-in dialogs, other
applications - exists only on the real screen, which the desktop body sees and operates with the OS
mouse and keyboard. The model moves between them with the generic ``switch_surface`` action; this
object only routes each primitive to the body of the active surface. It never decides when to switch.
"""

from __future__ import annotations

from typing import Any, Callable

SURFACES = ("browser", "desktop")


class SurfaceUnavailable(Exception):
    """A surface cannot be entered right now; the message is a short factual reason code."""


class SurfaceSwitchingBody:
    """Routes body primitives to the active surface's body; the other body is created on first use."""

    def __init__(
        self,
        primary: Any,
        *,
        primary_surface: str,
        factories: dict[str, Callable[[], Any]],
    ) -> None:
        surface = str(primary_surface or "browser").strip().lower()
        if surface not in SURFACES:
            surface = "browser"
        self._bodies: dict[str, Any] = {surface: primary}
        self._factories: dict[str, Callable[[], Any]] = {
            key: factory for key, factory in (factories or {}).items() if key in SURFACES and key != surface
        }
        self._active = surface

    @property
    def execution_surface(self) -> str:
        return self._active

    def available_surfaces(self) -> list[str]:
        return [surface for surface in SURFACES if surface in self._bodies or surface in self._factories]

    def body_for(self, surface: str) -> Any:
        return self._bodies.get(surface)

    async def switch_surface(self, target: str) -> dict[str, Any]:
        wanted = str(target or "").strip().lower()
        if wanted not in SURFACES:
            return {"ok": False, "action": "switch_surface", "failure_reason": "unknown_surface", "surface": self._active}
        if wanted == self._active:
            return {"ok": True, "action": "switch_surface", "surface": wanted, "previous_surface": wanted, "already_active": True}
        body = self._bodies.get(wanted)
        if body is None:
            factory = self._factories.get(wanted)
            if factory is None:
                return {"ok": False, "action": "switch_surface", "failure_reason": "surface_unavailable", "surface": self._active}
            try:
                body = factory()
            except SurfaceUnavailable as exc:
                return {
                    "ok": False,
                    "action": "switch_surface",
                    "failure_reason": str(exc) or "surface_unavailable",
                    "surface": self._active,
                }
            except Exception:  # noqa: BLE001 - an unavailable surface is a fact for the model, not a crash
                return {"ok": False, "action": "switch_surface", "failure_reason": "surface_unavailable", "surface": self._active}
            if body is None:
                return {"ok": False, "action": "switch_surface", "failure_reason": "surface_unavailable", "surface": self._active}
            self._bodies[wanted] = body
        previous = self._active
        self._active = wanted
        return {"ok": True, "action": "switch_surface", "surface": wanted, "previous_surface": previous}

    def __getattr__(self, name: str) -> Any:
        # Only reached for attributes this router does not define: every body primitive and
        # capability probe (getattr/callable/inspect.signature) resolves against the active body.
        bodies = self.__dict__.get("_bodies")
        active = self.__dict__.get("_active")
        if bodies is None or active not in bodies:
            raise AttributeError(name)
        return getattr(bodies[active], name)
