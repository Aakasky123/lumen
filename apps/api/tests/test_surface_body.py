"""One run, two surfaces: the router only routes primitives to the active surface's body."""

from __future__ import annotations

import asyncio

from app import extension_run
from app.surface_body import SurfaceSwitchingBody, SurfaceUnavailable


class _Page:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def vision_click(self, x: int, y: int) -> dict:
        self.calls.append(f"page:{x},{y}")
        return {"ok": True}


class _Screen:
    execution_surface = "desktop"

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def vision_click(self, x: int, y: int) -> dict:
        self.calls.append(f"screen:{x},{y}")
        return {"ok": True}

    async def vision_launch_app(self, target: str) -> dict:
        return {"ok": True, "launched_app_id": target}


def test_router_sends_each_primitive_to_the_active_surface_and_creates_the_other_once() -> None:
    page, screen = _Page(), _Screen()
    created: list[int] = []

    def enter_desktop() -> _Screen:
        created.append(1)
        return screen

    body = SurfaceSwitchingBody(page, primary_surface="browser", factories={"desktop": enter_desktop})
    assert body.execution_surface == "browser"
    assert body.available_surfaces() == ["browser", "desktop"]
    assert getattr(body, "vision_launch_app", None) is None, "a desktop-only primitive is absent on the page"

    asyncio.run(body.vision_click(1, 2))
    switched = asyncio.run(body.switch_surface("desktop"))
    assert switched == {"ok": True, "action": "switch_surface", "surface": "desktop", "previous_surface": "browser"}
    assert callable(getattr(body, "vision_launch_app", None))
    asyncio.run(body.vision_click(3, 4))
    asyncio.run(body.switch_surface("browser"))
    asyncio.run(body.switch_surface("desktop"))
    asyncio.run(body.vision_click(5, 6))

    assert page.calls == ["page:1,2"]
    assert screen.calls == ["screen:3,4", "screen:5,6"]
    assert created == [1]


def test_an_unavailable_surface_is_a_fact_and_the_run_stays_where_it_is() -> None:
    def busy() -> None:
        raise SurfaceUnavailable("desktop_busy_with_another_run")

    body = SurfaceSwitchingBody(_Page(), primary_surface="browser", factories={"desktop": busy})
    result = asyncio.run(body.switch_surface("desktop"))
    assert result["ok"] is False and result["failure_reason"] == "desktop_busy_with_another_run"
    assert body.execution_surface == "browser"
    assert asyncio.run(body.switch_surface("moon"))["failure_reason"] == "unknown_surface"
    assert asyncio.run(body.switch_surface("browser"))["already_active"] is True

    lone = SurfaceSwitchingBody(_Page(), primary_surface="browser", factories={})
    assert lone.available_surfaces() == ["browser"]
    assert asyncio.run(lone.switch_surface("desktop"))["failure_reason"] == "surface_unavailable"


def test_only_real_browser_or_desktop_bodies_gain_a_second_surface() -> None:
    fake = _Page()
    assert extension_run._surface_switching_body(fake, run_id=0, execution_surface="browser") is fake
