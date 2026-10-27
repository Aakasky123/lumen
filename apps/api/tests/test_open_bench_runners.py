"""MiniWoB++ and WebGames runners: shim injection, servers, grading, task selection, scorecard. No network, no Lumen API."""

from __future__ import annotations

import json
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from app.agent_eval.open_bench import bench_common as bc
from app.agent_eval.open_bench import miniwob_run as mw
from app.agent_eval.open_bench import miniwob_server as mws
from app.agent_eval.open_bench import webgames_run as wg
from app.agent_eval.open_bench import webgames_server as wgs


def _get(url: str):
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def _post(url: str, payload: dict):
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read() or b"{}")


# ---------------------------------------------------------------- MiniWoB

PAGE = """<!DOCTYPE html><html><head><title>t</title>
<link rel="stylesheet" href="../core/core.css">
<script src="../core/core.js"></script>
<script src="../core/d3.v3.min.js"></script></head><body><div id="query"></div></body></html>"""


@pytest.fixture()
def miniwob_root(tmp_path: Path) -> Path:
    root = tmp_path / "html"
    (root / "miniwob").mkdir(parents=True)
    (root / "core").mkdir()
    (root / "flight").mkdir()
    (root / "core" / "core.js").write_text("var core = {};", encoding="utf-8")
    (root / "flight" / "page.html").write_text(PAGE, encoding="utf-8")
    for name in ("click-button", "enter-text", "text-transform", "drag-box", "terminal"):
        (root / "miniwob" / f"{name}.html").write_text(PAGE, encoding="utf-8")
    return root


def test_shim_is_injected_right_after_core_js():
    html, found = mws.inject_shim(PAGE)
    assert found
    core = html.index("core/core.js")
    shim = html.index("__lumen_shim.js")
    assert core < shim < html.index("d3.v3.min.js")
    assert html.count("__lumen_shim.js") == 1


def test_shim_injection_tolerates_quote_style_and_reports_a_missing_core_tag():
    html, found = mws.inject_shim("<head><script type='text/javascript' src='../core/core.js'></script></head>")
    assert found and "__lumen_shim.js" in html
    unchanged, found = mws.inject_shim("<html><script src='other.js'></script></html>")
    assert not found and unchanged == "<html><script src='other.js'></script></html>"


def test_shim_script_carries_the_browsergym_protocol():
    js = mws.SHIM_JS
    assert "Math.seedrandom(seed)" in js and "qs.get('seed')" in js
    assert "defineProperty(core, 'EPISODE_MAX_TIME'" in js     # a task's later assignment must not stick
    assert "core.startEpisodeReal()" in js                     # no START click
    assert "core.getUtterance()" in js and "'start'" in js
    assert "WOB_RAW_REWARD_GLOBAL" in js and "'report'" in js  # the raw reward is what is scored
    assert "#reward-display" in js and "display: none" in js
    assert "lumen-task-ended" in js                              # neutral cover, no retry
    # the cover must not say how the episode went
    assert "reward" not in js.split("cover.textContent")[1].split(";")[0].lower()


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_shim_script_parses(tmp_path: Path):
    path = tmp_path / "shim.js"
    path.write_text(mws.SHIM_JS, encoding="utf-8")
    result = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_task_script_cannot_shorten_the_episode_time(tmp_path: Path):
    harness = tmp_path / "run.js"
    harness.write_text(
        "var fakeEl = {setAttribute(){}, appendChild(){}, style:{}};\n"
        "global.document = {head: fakeEl, body: fakeEl, createElement: () => ({setAttribute(){}, style:{}}), getElementById: () => fakeEl};\n"
        "global.location = {search: '?seed=4&sid=x', pathname: '/eval/bench/miniwob/miniwob/enter-date.html'};\n"
        "global.fetch = () => {};\n"
        "global.core = {EPISODE_MAX_TIME: 10000, startEpisode(){}, endEpisode(){}, startEpisodeReal(){}, getUtterance: () => 'u'};\n"
        "global.window = global; Math.seedrandom = () => {};\n"
        + mws.SHIM_JS + "\n"
        "core.EPISODE_MAX_TIME = 20000;  // what enter-date.html / choose-date.html do after the shim\n"
        "if (core.EPISODE_MAX_TIME < 1e8) { throw new Error('episode time shortened: ' + core.EPISODE_MAX_TIME); }\n",
        encoding="utf-8")
    result = subprocess.run(["node", str(harness)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_miniwob_server_routes_injection_and_reports(miniwob_root: Path):
    server, store = mws.start_server(miniwob_root)
    try:
        base = server.url
        assert mws.MOUNT.startswith("/eval/bench/")
        status, headers, body = _get(mws.task_url(base, "click-button", 3, "abc"))
        assert status == 200 and "text/html" in headers["Content-Type"]
        assert b"__lumen_shim.js" in body
        status, _, body = _get(base + mws.MOUNT + "flight/page.html")  # a sub-site page is not a task page
        assert status == 200 and b"__lumen_shim.js" not in body
        status, headers, body = _get(base + mws.SHIM_PATH)
        assert status == 200 and "javascript" in headers["Content-Type"] and b"seedrandom" in body
        status, _, body = _get(base + mws.MOUNT + "core/core.js")
        assert status == 200 and body == b"var core = {};"
        assert _get(base + mws.MOUNT + "miniwob/nope.html")[0] == 404
        assert _get(base + "/elsewhere")[0] == 404
        assert _get(base + mws.MOUNT + "../secret.txt")[0] == 404
        assert _get(base + mws.MOUNT + "%2e%2e/%2e%2e/secret")[0] == 404

        assert _post(base + "/__bench/start", {"sid": "abc", "task": "click-button", "seed": "3",
                                                "utterance": "Click the thing."})[0] == 200
        assert store.get("abc")["utterance"] == "Click the thing."
        _post(base + "/__bench/report", {"sid": "abc", "task": "click-button", "seed": "3", "raw_reward": 1,
                                         "reward": 0.9, "reason": None})
        _post(base + "/__bench/report", {"sid": "abc", "raw_reward": -1})  # a later report never replaces the first
        state = json.loads(_get(base + "/__bench/state?sid=abc")[2])
        assert state["raw_reward"] == 1 and state["utterance"] == "Click the thing."
        assert json.loads(_get(base + "/__bench/state?sid=none")[2]) == {}
    finally:
        server.close()


def test_miniwob_pages_with_different_sids_do_not_mix():
    store = mws.EpisodeStore()
    store.start("a", "t", "1", "x")
    store.start("b", "t", "1", "y")
    store.report("a", "t", "1", 1, 1, None)
    assert mw.grade(store.get("a"))["passed"] is True
    assert mw.grade(store.get("b"))["passed"] is False


def test_miniwob_grade_is_raw_reward_above_zero():
    assert mw.grade({"raw_reward": 1}) == {"passed": True, "raw_reward": 1.0, "reason": None}
    assert mw.grade({"raw_reward": 0.4, "reason": "partial"})["passed"] is True
    assert mw.grade({"raw_reward": 0})["passed"] is False
    assert mw.grade({"raw_reward": -1, "reason": "wrong"})["passed"] is False
    assert mw.grade({"raw_reward": "x"})["passed"] is False
    never = mw.grade({"starts": 1})
    assert never["passed"] is False and never["raw_reward"] is None
    assert mw.grade(None)["passed"] is False


def test_miniwob_task_discovery_excludes_text_transform(miniwob_root: Path):
    tasks = mw.discover_tasks(miniwob_root)
    assert "text-transform" not in tasks
    assert tasks == ["click-button", "drag-box", "enter-text", "terminal"]
    with pytest.raises(SystemExit):
        mw.select_tasks(tasks, tasks="text-transform")
    with pytest.raises(SystemExit):
        mw.select_tasks(tasks, tasks="no-such-task")
    assert mw.select_tasks(tasks, tasks="enter-text,click-button") == ["enter-text", "click-button"]


def test_miniwob_families_and_selection():
    available = ["click-button", "click-link", "enter-text", "enter-date", "drag-box", "terminal", "tic-tac-toe"]
    grouped = mw.families(available)
    assert grouped["click"] == ["click-button", "click-link"]
    assert grouped["misc"] == ["terminal", "tic-tac-toe"]
    assert mw.select_tasks(available, family_names="enter,drag") == ["enter-text", "enter-date", "drag-box"]
    with pytest.raises(SystemExit):
        mw.select_tasks(available, family_names="nonsense")
    assert mw.select_tasks(available, all_tasks=True) == available
    # the default sample keeps only tasks that exist
    assert mw.select_tasks(["click-button", "zzz"]) == ["click-button"]


def test_miniwob_jobs_name_seeds():
    one = mw.build_jobs(["click-button"], [1])
    assert one[0]["name"] == "click-button" and one[0]["family"] == "click"
    many = mw.build_jobs(["click-button", "terminal"], [1, 2])
    assert [j["name"] for j in many] == ["click-button@1", "click-button@2", "terminal@1", "terminal@2"]
    assert many[2]["family"] == "misc"


def test_miniwob_goal_uses_the_instruction_text():
    url = "http://127.0.0.1:1/eval/bench/miniwob/miniwob/click-button.html?seed=1&sid=x"
    goal = mw.make_goal(url, 'Click on the "OK"  button.\n')
    assert goal.startswith("Open " + url) and goal.endswith('Click on the "OK" button.')
    assert "instruction shown at the top of the page" in goal
    fallback = mw.make_goal(url, None)
    assert fallback.endswith("top of the page.") and ":" not in fallback.split("page", 1)[1]


def test_miniwob_default_sample_is_about_thirty_distinct_tasks():
    assert 28 <= len(mw.DEFAULT_SAMPLE) <= 32 and len(set(mw.DEFAULT_SAMPLE)) == len(mw.DEFAULT_SAMPLE)
    assert "text-transform" not in mw.DEFAULT_SAMPLE
    assert len({mw.family_of(t) for t in mw.DEFAULT_SAMPLE}) >= 8


def _real_miniwob() -> Path | None:
    try:
        return mw.html_root()
    except SystemExit:
        return None


@pytest.mark.skipif(_real_miniwob() is None, reason="MiniWoB source not fetched")
def test_miniwob_real_pages_all_take_the_shim_and_sample_exists():
    root = _real_miniwob()
    tasks = mw.discover_tasks(root)
    assert len(tasks) > 100
    missing = [t for t in mw.DEFAULT_SAMPLE if t not in tasks]
    assert not missing
    for task in tasks:
        _, found = mws.inject_shim((root / "miniwob" / f"{task}.html").read_text(encoding="utf-8"))
        assert found, task


# ---------------------------------------------------------------- WebGames

@pytest.fixture()
def dist(tmp_path: Path) -> Path:
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<html><div id=root></div></html>", encoding="utf-8")
    (root / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    (root / "code_gen.wasm").write_bytes(b"\x00asm\x01\x00\x00\x00")
    (tmp_path / "secret.txt").write_text("nope", encoding="utf-8")
    return root


def test_webgames_server_spa_fallback_assets_and_stubs(dist: Path):
    server, visits = wgs.start_server(dist)
    try:
        base = server.url
        for route in ("/date", "/maze/start/3", "/iframe-content/2", "/"):
            status, headers, body = _get(base + route)
            assert status == 200 and "text/html" in headers["Content-Type"] and b"id=root" in body, route
        status, headers, body = _get(base + "/assets/app.js")
        assert status == 200 and "javascript" in headers["Content-Type"] and body == b"console.log(1)"
        status, headers, body = _get(base + "/code_gen.wasm")
        assert status == 200 and headers["Content-Type"] == "application/wasm" and body.startswith(b"\x00asm")
        assert _get(base + "/assets/missing.js")[0] == 404  # a missing asset is not a page
        assert b"nope" not in _get(base + "/%2e%2e/secret.txt")[2]
        assert _get(base + "/api/record-view")[0] == 404
        for endpoint in ("/api/record-view", "/api/record-completion"):
            status, payload = _post(base + endpoint, {"taskId": "date"})
            assert status == 200 and payload == {"success": True}
        with pytest.raises(urllib.error.HTTPError):
            _post(base + "/api/other", {})
        assert "/date" in visits and "/maze/start/3" in visits
    finally:
        server.close()


def test_webgames_server_needs_a_built_dist(tmp_path: Path):
    with pytest.raises(SystemExit):
        wgs.start_server(tmp_path)


ROWS = [
    {"path": "date", "id": "date", "title": "d", "description": "Enter today's date", "tags": ["form", "date"],
     "password": "DATE_MASTER_2024", "difficulty": "easy"},
    {"path": "patience-easy", "id": "patience-easy", "description": "Wait 5 seconds to reveal the password.",
     "tags": ["timed", "refresh"], "password": "PATIENCE_EASY", "difficulty": "easy"},
    {"path": "maze/*", "id": "maze/*", "description": "Navigate through a series of doors.",
     "tags": ["navigation"], "password": "MAZE_PW"},
    {"path": "i-accept", "id": "i-accept", "description": "x", "tags": ["checkbox"], "password": "A"},
    {"path": "i-accept-hard", "id": "i-accept-hard", "description": "x", "tags": ["checkbox"], "password": "A2"},
    {"path": "robo-check-easy", "id": "robo-check-easy", "description": "x", "tags": ["captcha"], "password": "B"},
    {"path": "verify-challenge-hard", "id": "verify-challenge-hard", "description": "x", "tags": ["captcha"], "password": "C"},
    {"path": "illegal-material", "id": "illegal-material", "description": "x", "tags": ["warning"], "password": "D"},
]


def _write_rows(tmp_path: Path) -> Path:
    path = tmp_path / "tasks.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in ROWS) + "\n", encoding="utf-8")
    return path


def test_webgames_excludes_and_normalizes_wildcard_routes(tmp_path: Path):
    rows = wg.load_tasks(_write_rows(tmp_path))
    ids = [row["id"] for row in rows]
    assert ids == ["date", "patience-easy", "maze"]
    assert [row["family"] for row in rows] == ["form", "timed", "navigation"]
    for excluded in ("i-accept", "i-accept-hard", "robo-check-easy", "verify-challenge-hard", "illegal-material"):
        assert wg.is_excluded(excluded)
    assert not wg.is_excluded("date")
    with pytest.raises(SystemExit):
        wg.select_tasks(rows, tasks="robo-check")
    with pytest.raises(SystemExit):
        wg.select_tasks(rows, tasks="zzz")
    assert [r["id"] for r in wg.select_tasks(rows, tasks="maze,date")] == ["maze", "date"]
    assert [r["id"] for r in wg.select_tasks(rows, categories="timed")] == ["patience-easy"]
    with pytest.raises(SystemExit):
        wg.select_tasks(rows, categories="checkbox")
    assert [r["id"] for r in wg.select_tasks(rows, all_tasks=True)] == ids


def test_webgames_goal_is_the_official_prompt():
    row = {"path": "maze/*", "description": "Navigate through a series of doors."}
    goal = wg.make_goal("http://127.0.0.1:9", row)
    assert goal == ("Your task is: Navigate through a series of doors.. You must go to http://127.0.0.1:9/maze and "
                    "obtain the password for the game. To prove you have completed the task you must obtain and "
                    "report the password for the game. If you do not have the password, you have not managed to "
                    "complete the task.")
    assert wg.homepage("http://h/", {"path": "date"}) == "http://h/date"


def test_webgames_password_scoring_is_substring_of_the_answer():
    assert wg.grade("PW1", ["", "The password is PW1."], "")["passed"] is True
    assert wg.grade("PW1", ["status says PW1"], "")["passed"] is True
    assert wg.grade("PW1", ["pw1", "PW"], "")["passed"] is False            # case-sensitive like the official scorer
    assert wg.grade("PW1", [], "PW1 shown")["passed"] is False               # on the page but never reported
    assert wg.grade("", ["anything"], "")["passed"] is False                 # no password never passes
    shown = wg.grade("PW1", ["done"], "Congrats! PW1")
    assert shown == {"passed": False, "password_on_page": True}
    assert wg.grade("PW1", ["PW1"], "")["password_on_page"] is False


def test_webgames_default_sample_is_about_thirty_and_has_realtime_tasks():
    sample = wg.DEFAULT_SAMPLE
    assert 28 <= len(sample) <= 32 and len(set(sample)) == len(sample)
    assert not [t for t in sample if wg.is_excluded(t)]
    assert {"click-cubed-easy", "patience-easy", "bullseye-easy", "button-hold"} <= set(sample)


def _real_webgames_rows():
    try:
        return wg.load_tasks()
    except (SystemExit, OSError):
        return None


@pytest.mark.skipif(_real_webgames_rows() is None, reason="WebGames source not fetched")
def test_webgames_real_sample_exists_and_excludes_hold():
    rows = _real_webgames_rows()
    ids = {row["id"] for row in rows}
    assert not [t for t in wg.DEFAULT_SAMPLE if t not in ids]
    assert not [i for i in ids if wg.is_excluded(i)]
    assert len({row["family"] for row in wg.select_tasks(rows)}) >= 15
    assert all(row["password"] for row in rows)


# ---------------------------------------------------------------- common: scorecard, pool, lanes

def test_summarize_counts_per_family():
    results = [
        {"task": "a", "family": "click", "passed": True},
        {"task": "b", "family": "click", "passed": False, "timed_out": True},
        {"task": "c", "family": "enter", "passed": True},
        {"task": "d", "family": "enter", "passed": False, "final_state": "DRIVER_ERROR"},
    ]
    summary = bc.summarize(results)
    assert summary["passed"] == 2 and summary["total"] == 4 and summary["rate"] == 0.5
    assert summary["by_family"]["click"] == {"passed": 1, "total": 2, "rate": 0.5}
    assert summary["timed_out"] == 1 and summary["driver_errors"] == 1
    assert bc.summarize([])["rate"] is None
    text = bc.summary_text(summary, "X")
    assert text.splitlines()[0] == "X: 2/4 passed (50%)" and "click" in text


def test_result_line_matches_the_live_run_format():
    line = bc.result_line({"task": "click-button", "passed": True, "run_id": 7, "final_state": "COMPLETED", "elapsed_s": 41.2})
    assert line == "    -> click-button PASS  run=7  state=COMPLETED  41.2s"
    line = bc.result_line({"task": "date", "passed": False, "run_id": 9, "final_state": "TIMED_OUT",
                           "elapsed_s": 600, "password_on_page": True})
    assert line.startswith("    -> date FAIL  run=9") and "password_on_page=yes" in line


class _FakeLane:
    def __init__(self, n: int) -> None:
        self.device_id = f"bench-lane-{n}"
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class _FakeFactory:
    def __init__(self) -> None:
        self.lanes: list[_FakeLane] = []

    def fresh(self) -> _FakeLane:
        lane = _FakeLane(len(self.lanes) + 1)
        self.lanes.append(lane)
        return lane


def test_pool_gives_each_task_a_fresh_lane_and_writes_a_scorecard(tmp_path: Path):
    tasks = [{"name": f"t{i}", "family": "f1" if i % 2 else "f2"} for i in range(5)]
    factory = _FakeFactory()
    said: list[str] = []

    def run_task(task, lane, label):
        if task["name"] == "t3":
            raise RuntimeError("boom")
        return {"task": task["name"], "family": task["family"], "passed": task["name"] != "t1",
                "final_state": "COMPLETED", "run_id": 10, "elapsed_s": 1.0, "lane": lane.device_id}

    results = bc.run_pool(tasks, run_task, factory=factory, lanes=2, out=tmp_path, label_prefix="lane",
                          say=lambda label, message: said.append(message))
    assert len(results) == 5 and len(factory.lanes) == 5
    assert len({lane.device_id for lane in factory.lanes}) == 5 and all(lane.stopped for lane in factory.lanes)
    card = json.loads((tmp_path / "scorecard.json").read_text(encoding="utf-8"))
    assert card["summary"]["passed"] == 3 and card["summary"]["total"] == 5
    assert card["summary"]["driver_errors"] == 1
    assert set(card["summary"]["by_family"]) == {"f1", "f2"}
    assert [r["task"] for r in card["results"]] == ["t0", "t1", "t2", "t3", "t4"]
    assert (tmp_path / "t3.json").is_file()
    assert any("-> t1 FAIL" in line for line in said) and any("-> t0 PASS" in line for line in said)
    assert any("DRIVER_ERROR" in line for line in said)


def test_lane_factory_never_reuses_a_root_or_device_id(tmp_path: Path):
    started: list = []

    class FakeBenchLane:
        def __init__(self, index, chromium):
            self.device_id = f"bench-lane-{index}"
            self.root = tmp_path / "default"

        def start(self):
            started.append(self)

        def stop(self):
            pass

    class FakeLr:
        BenchLane = FakeBenchLane
        LANE_ROOT = tmp_path / "lanes"

        @staticmethod
        def _request(method, path, *args, **kwargs):
            return {"connections": [{"device_id": lane.device_id, "live_actions_ready": True} for lane in started]}

    factory = bc.LaneFactory(FakeLr, Path("chrome"))
    lanes = [factory.fresh() for _ in range(4)]
    assert len({lane.root for lane in lanes}) == 4 and len({lane.device_id for lane in lanes}) == 4
    assert all(lane.device_id.startswith("bench-lane-") and lane.root.parent == FakeLr.LANE_ROOT for lane in lanes)


def test_check_api_refuses_owner_ports():
    for api in ("http://127.0.0.1:8001", "http://127.0.0.1:8011/", "http://127.0.0.1:8041", "http://localhost"):
        with pytest.raises(SystemExit):
            bc.check_api(api, "http://127.0.0.1:8021")
    assert bc.check_api("http://127.0.0.1:8021/", "x") == "http://127.0.0.1:8021"


def test_safe_join_blocks_traversal(tmp_path: Path):
    (tmp_path / "root").mkdir()
    assert bc.safe_join(tmp_path / "root", "a/b.txt") == (tmp_path / "root" / "a" / "b.txt").resolve()
    assert bc.safe_join(tmp_path / "root", "../x") is None
    assert bc.safe_join(tmp_path / "root", "%2e%2e/x") is None
