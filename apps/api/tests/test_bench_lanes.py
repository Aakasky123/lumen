"""Gauntlet bench lanes: headless Chromium bodies (device ``bench-lane-<k>``) that live_run starts so
browser-only pages run in parallel. A lane takes only the runs of chat threads pinned to it, never the
user's own tasks, and a lane run never switches to the user's real screen."""

import inspect
import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import desktop_body, extension_run, models
from app.db import SessionLocal
from app.extension_body import ExtensionBody
from app.extension_hub import hub, is_bench_lane
from app.agent_eval.interaction_bench import live_run
from app.agent_eval.interaction_bench.v3_harness import MISSIONS
from app.surface_body import SurfaceSwitchingBody

from tests.test_in_browser_routing import _capture_schedule, _make_deferred_run, _make_user, _register_ready_body


def _lane() -> str:
    # Unique per test: a run routed to a lane stays RUNNING in this process's database, and a busy lane
    # refuses the next run.
    return f"bench-lane-{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def _isolate_hub():
    saved_conns, saved_lanes, saved_loop = dict(hub._connections), dict(hub._thread_lanes), hub.loop
    hub._connections.clear()
    hub._thread_lanes.clear()
    hub.loop = None
    try:
        yield
    finally:
        hub._connections.clear()
        hub._connections.update(saved_conns)
        hub._thread_lanes.clear()
        hub._thread_lanes.update(saved_lanes)
        hub.loop = saved_loop


def test_the_default_body_is_the_users_own_never_a_bench_lane() -> None:
    _register_ready_body("dev-user")
    _register_ready_body("bench-lane-1")  # registered last, still never the default

    assert hub.get(None).device_id == "dev-user"
    assert hub.get("bench-lane-1").device_id == "bench-lane-1"
    hub._connections.pop("dev-user")
    assert hub.get(None) is None
    assert is_bench_lane("bench-lane-1") and not is_bench_lane("dev-user") and not is_bench_lane(None)


def test_only_a_bench_lane_can_be_pinned_to_a_thread() -> None:
    with pytest.raises(ValueError):
        hub.pin_thread_to_lane(7, "dev-user")
    hub.pin_thread_to_lane(7, "bench-lane-2")
    assert hub.lane_for_thread(7) == "bench-lane-2"
    assert hub.lane_for_thread(8) is None


def test_a_pinned_thread_runs_on_its_lane_and_other_threads_on_the_users_body(monkeypatch) -> None:
    lane = _lane()
    uid = _make_user()
    lane_run, lane_thread = _make_deferred_run(uid, {}, url="http://127.0.0.1:8001/eval/bench/click_gauntlet")
    user_run, user_thread = _make_deferred_run(uid, {}, url="https://example.com/form")
    _register_ready_body("dev-user")
    _register_ready_body(lane)
    hub.pin_thread_to_lane(lane_thread, lane)
    hub.loop = object()
    scheduled = _capture_schedule(monkeypatch)

    assert extension_run.route_deferred_chat_run(lane_run, lane_thread)["status"] == "in_browser"
    assert extension_run.route_deferred_chat_run(user_run, user_thread)["status"] == "in_browser"
    assert len(scheduled) == 2
    with SessionLocal() as db:
        assert db.get(models.OperatorRun, lane_run).confidence_state["in_browser"]["device_id"] == lane
        assert db.get(models.OperatorRun, user_run).confidence_state["in_browser"]["device_id"] == "dev-user"


def test_a_pinned_thread_whose_lane_is_gone_blocks_instead_of_using_the_users_browser(monkeypatch) -> None:
    lane = _lane()
    uid = _make_user()
    rid, tid = _make_deferred_run(uid, {})
    _register_ready_body("dev-user")
    hub.pin_thread_to_lane(tid, lane)
    hub.loop = object()
    scheduled = _capture_schedule(monkeypatch)
    monkeypatch.setattr(
        extension_run, "_begin_chrome_self_launch_for_run",
        lambda *a, **k: pytest.fail("a bench thread must never launch the user's Chrome"),
    )

    routed = extension_run.route_deferred_chat_run(rid, tid)

    assert routed["status"] == "no_body"
    assert "bench lane" in routed["message"]
    assert scheduled == []
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, rid)
        assert run.current_state == "BLOCKED"
        assert "in_browser" not in (run.confidence_state or {})


def test_a_pinned_run_classified_as_desktop_still_runs_in_its_lane(monkeypatch) -> None:
    lane = _lane()
    uid = _make_user()
    rid, tid = _make_deferred_run(uid, {"browser_task": {"execution_surface": "desktop"}})
    _register_ready_body(lane)
    hub.pin_thread_to_lane(tid, lane)
    hub.loop = object()
    _capture_schedule(monkeypatch)

    assert extension_run.route_deferred_chat_run(rid, tid)["status"] == "in_browser"
    with SessionLocal() as db:
        in_browser = db.get(models.OperatorRun, rid).confidence_state["in_browser"]
    assert in_browser["device_id"] == lane
    assert in_browser.get("execution_surface") != "desktop"


def test_a_lane_body_never_gets_the_real_screen(monkeypatch) -> None:
    monkeypatch.setattr(desktop_body, "desktop_control_status", lambda: {"available": True, "enabled": True})
    monkeypatch.setattr(extension_run, "_windows_open_at_run_start", lambda run_id: frozenset())
    lane = ExtensionBody(_register_ready_body("bench-lane-1"))
    own = ExtensionBody(_register_ready_body("dev-user"))

    assert extension_run._surface_switching_body(lane, run_id=1, execution_surface="browser") is lane
    assert isinstance(extension_run._surface_switching_body(own, run_id=1, execution_surface="browser"), SurfaceSwitchingBody)


def test_pinning_is_local_admin_only_and_lane_only() -> None:
    from app.main import app

    client = TestClient(app, base_url="http://127.0.0.1:8001")
    body = {"thread_id": 41, "device_id": "bench-lane-1"}

    assert client.post("/operator/extension/bench-lanes/pin", json=body).status_code == 403
    local = {"X-Lumen-Local": "1"}
    assert client.post("/operator/extension/bench-lanes/pin", json={**body, "device_id": "dev-user"}, headers=local).status_code == 400
    response = client.post("/operator/extension/bench-lanes/pin", json=body, headers=local)
    assert response.status_code == 200
    assert response.json()["connected"] is False
    assert hub.lane_for_thread(41) == "bench-lane-1"


def test_a_page_that_opens_native_windows_or_a_workspace_is_a_desktop_page() -> None:
    manifest = json.loads((Path(live_run.HERE) / "manifest.json").read_text(encoding="utf-8"))
    desktop = {page["name"]: page["desktop"] for page in manifest["pages"]}
    for name, mission in MISSIONS.items():
        if mission.needs_workspace or "open_window(" in inspect.getsource(mission):
            assert desktop[name], f"{name} opens native windows or files, so it cannot run in a headless lane"
    assert all(desktop[page["name"]] for page in manifest["pages"] if page["generation"] in (3, 5))
    assert not desktop["v4_clicks"] and not desktop["click_gauntlet"]


def test_the_runner_clears_only_runs_on_the_pages_own_body() -> None:
    def run(device: str) -> dict:
        return {"confidence_state": {"in_browser": {"device_id": device}} if device else {}}

    assert live_run._on_body(run("bench-lane-2"), "bench-lane-2", True)
    assert not live_run._on_body(run("bench-lane-1"), "bench-lane-2", True)
    assert not live_run._on_body(run("dev-user"), "bench-lane-2", True)
    assert live_run._on_body(run("dev-user"), None, True)
    assert not live_run._on_body(run("bench-lane-1"), None, True)
    # A run not yet routed may be a lane's new run while lanes are running; alone, it is the user's.
    assert not live_run._on_body(run(""), None, True)
    assert live_run._on_body(run(""), None, False)


def test_lanes_use_the_newest_playwright_chromium(tmp_path, monkeypatch) -> None:
    for build, folder in (("1148", "chrome-win"), ("1223", "chrome-win64"), ("headless_shell-1300", "chrome-win")):
        prefix = "chromium_" if build.startswith("headless") else "chromium-"
        exe = tmp_path / "ms-playwright" / f"{prefix}{build}" / folder / "chrome.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("LUMEN_BENCH_CHROMIUM", raising=False)

    assert live_run.find_lane_chromium().parts[-3] == "chromium-1223"
    with pytest.raises(RuntimeError):
        live_run.find_lane_chromium(str(tmp_path / "missing.exe"))


def test_a_lane_loads_a_fresh_extension_copy_that_names_its_lane(tmp_path, monkeypatch) -> None:
    source = tmp_path / "extension"
    (source / "src").mkdir(parents=True)
    (source / "manifest.json").write_text("{}", encoding="utf-8")
    (source / "src" / "background.js").write_text("// worker", encoding="utf-8")
    (source / "node_modules").mkdir()
    monkeypatch.setattr(live_run, "EXTENSION_SOURCE", source)
    monkeypatch.setattr(live_run, "LANE_ROOT", tmp_path / "lanes")
    launched: list[list[str]] = []
    monkeypatch.setattr(live_run.subprocess, "Popen", lambda args, **kwargs: launched.append(args) or None)

    lane = live_run.BenchLane(2, Path("chromium.exe"))
    lane.start()

    copy = tmp_path / "lanes" / "lane-2" / "extension"
    assert json.loads((copy / "bench_lane.json").read_text(encoding="utf-8")) == {
        "device_id": "bench-lane-2", "backend": live_run.API,
    }
    assert (copy / "src" / "background.js").is_file() and not (copy / "node_modules").exists()
    assert "--headless=new" in launched[0] and f"--load-extension={copy}" in launched[0]
    lane.stop()
    assert not (tmp_path / "lanes" / "lane-2").exists()
