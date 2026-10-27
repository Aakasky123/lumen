"""Interaction Gauntlet generation 3: the judge logic and the harness API, without a desktop or browser.

The end-to-end proof (real Playwright input, ghost desktop windows, the 5-second slow-thinker run)
is validate_v3.py; these tests pin the rules the harness judges by, so a refactor cannot quietly
change what counts as a pass, a futile action, or a sprung trap.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from app.agent_eval.interaction_bench import package
from app.agent_eval.interaction_bench.tasks import GAUNTLET_PAGES
from app.agent_eval.interaction_bench.v3_harness import Harness, HarnessError, make_handler
from app.agent_eval.interaction_bench.v3_missions import LOCK, MISSIONS, Session, sha

BENCH = Path(__file__).resolve().parents[1] / "app" / "agent_eval" / "interaction_bench"


class FakeHarness:
    desktop_mode = "ghost"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.fails: dict[str, str] = {}
        self.opened: list[str] = []

    def sticky_fail(self, page: str, reason: str) -> None:
        self.fails.setdefault(page, reason)

    def open_window(self, session: Session, view: str) -> bool:
        self.opened.append(view)
        return True

    def make_workspace(self, session: Session, label: str) -> Path:
        path = self.root / f"v3-{label}-{session.sid}"
        path.mkdir(parents=True)
        session.workspace = path
        return path


def start(name: str, tmp_path: Path) -> tuple[Session, FakeHarness]:
    harness = FakeHarness(tmp_path)
    session = Session("a1b2c3", MISSIONS[name], harness)
    with LOCK:
        session.mission.setup()
    return session, harness


def act(session: Session, kind: str, **data):
    with LOCK:
        return session.mission.act(kind, data)


def ui(session: Session, kind: str, **data):
    with LOCK:
        return session.mission.ui(kind, data)


# ------------------------------------------------------------------------------ contract


def test_generation3_tasks_match_the_harness_missions():
    pages = [p for p in GAUNTLET_PAGES if p.generation == 3]
    assert {p.name for p in pages} == set(MISSIONS)
    for page in pages:
        mission = MISSIONS[page.name]
        assert page.harness is True
        assert page.token == mission.token
        assert page.challenges == len(mission.steps)
        assert page.family == mission.family
    assert all(not p.harness for p in GAUNTLET_PAGES if p.generation < 3)


def test_manifest_is_generated_from_tasks():
    committed = json.loads((BENCH / "manifest.json").read_text(encoding="utf-8"))
    assert committed == package.build_manifest(None)


@pytest.mark.parametrize("name", sorted(MISSIONS))
def test_each_mission_page_uses_the_shared_runtime(name):
    html = (BENCH / "pages" / f"{name}.html").read_text(encoding="utf-8")
    assert f'data-page="{name}"' in html
    assert '<script src="v3_common.js"></script>' in html
    assert MISSIONS[name].token not in html, "pass tokens live only in the harness"
    for aux in package.AUX_PAGES.get(name, []):
        assert (BENCH / "pages" / f"{aux}.html").is_file()


def test_hops_count_switches_between_agent_surfaces(tmp_path):
    session, _ = start("v3_handoff", tmp_path)
    session.log("page", "a")
    session.log("page", "b")
    session.log("desktop", "c")
    session.log("system", "complete", "x")  # never a hop
    session.log("page", "d")
    assert session.hops() == 2


# ------------------------------------------------------------------------------ missions


def test_handoff_golden_path_and_its_rules(tmp_path):
    s, h = start("v3_handoff", tmp_path)
    v = s.mission.v
    act(s, "launch")
    assert h.opened == ["handoff.field_unit"]
    assert ui(s, "apply_dock", value=v["dock_code"])["ok"] is False  # not linked yet
    act(s, "link_work_order", value=v["work_order"].lower())  # case-insensitive
    assert ui(s, "apply_dock", value=v["dock_code"])["ok"] is True
    assert ui(s, "diagnostics")["reference"] == v["diag_ref"]
    act(s, "record_reference", value=v["diag_ref"])
    assert ui(s, "factory_reset", answer="no")["ok"] is True
    assert ui(s, "sync", answer="no")["sync_id"] == v["sync_id"]
    act(s, "confirm_sync", value=v["sync_id"])
    futile = s.futile
    act(s, "verify_seal", value=v["seal"], via_paste=False)  # typed, not pasted
    assert s.futile == futile + 1 and "paste_relay" not in s.done
    act(s, "verify_seal", value=v["seal"], via_paste=True)
    assert s.passed and s.state()["token"] == MISSIONS["v3_handoff"].token


@pytest.mark.parametrize("kind", ["sync", "factory_reset"])
def test_handoff_destructive_dialog_answers_poison_the_page(tmp_path, kind):
    s, h = start("v3_handoff", tmp_path)
    ui(s, kind, answer="yes")
    assert s.failed and h.fails["v3_handoff"] == s.failed
    act(s, "link_work_order", value=s.mission.v["work_order"])
    assert s.done == [] and s.state()["token"] is None


def test_handoff_locked_sections_are_futile_not_fatal(tmp_path):
    s, _ = start("v3_handoff", tmp_path)
    result = act(s, "record_reference", value=s.mission.v["diag_ref"])
    assert result["ok"] is False and not s.failed and s.futile == 1


def test_kiosk_permissions_and_windows(tmp_path):
    s, h = start("v3_kiosk", tmp_path)
    v = s.mission.v
    act(s, "location_result", outcome="dismissed")
    assert "deny_location" in s.done
    assert act(s, "ticket_view")["ticket"] is None  # not printed until taken
    assert act(s, "take_ticket")["ticket"] == v["ticket"]
    act(s, "import", text="", error="NotAllowedError")
    assert "clipboard_import" not in s.done and "site controls" in s.messages["import"]
    assert act(s, "import", text=f"  {v['ticket'].lower()} ")["ticket_num"] == v["ticket_num"]
    act(s, "monitor_open", mode="pip")
    act(s, "claim", serving=v["ticket_num"] - 1)
    assert "pip_claim" not in s.done
    act(s, "claim", serving=v["ticket_num"])
    assert s.passed


def test_kiosk_granting_location_is_a_trap(tmp_path):
    s, h = start("v3_kiosk", tmp_path)
    act(s, "location_result", outcome="granted")
    assert s.failed and "location" in s.failed


def _filled_declaration(s: Session) -> str:
    text = s.mission.file(s.mission.decl_name)[0].decode("ascii")
    v = s.mission.v
    return text.replace("Container ID: ________________", f"Container ID: {v['container']}").replace(
        "Seal number: ________________", f"Seal number: {v['seal']}"
    )


def test_paperwork_declaration_round_trip(tmp_path):
    s, _ = start("v3_paperwork", tmp_path)
    v = s.mission.v
    blank = s.mission.file(s.mission.decl_name)[0].decode("ascii")
    act(s, "upload_declaration", name="x.txt", text=blank)
    assert "Container ID" in s.messages["upload"] and v["container"] not in s.messages["upload"]
    tampered = _filled_declaration(s).replace("Machine parts", "Spare parts")
    act(s, "upload_declaration", name="x.txt", text=tampered)
    assert "altered" in s.messages["upload"] and "form_roundtrip" not in s.done
    notepad_style = "﻿" + _filled_declaration(s).replace("\n", "\r\n") + "\r\n"
    act(s, "upload_declaration", name="x.txt", text=notepad_style)
    assert "form_roundtrip" in s.done


def test_paperwork_export_is_confined_to_the_mission_folder(tmp_path):
    s, _ = start("v3_paperwork", tmp_path)
    v = s.mission.v
    ws = Path(v["workspace"])
    outside = tmp_path / "elsewhere.txt"
    assert ui(s, "export", path=str(outside))["ok"] is False
    assert not outside.exists(), "nothing is written outside the mission folder"
    ui(s, "export", path=str(ws / "report.txt"))
    assert (ws / "report.txt").is_file() and "export_report" not in s.done
    ui(s, "export", path=str(ws / "outbox" / f"report-{v['code']}.txt"))
    assert "export_report" in s.done


def test_paperwork_rename_and_drop_are_judged_from_the_real_files(tmp_path):
    s, _ = start("v3_paperwork", tmp_path)
    v = s.mission.v
    inbox = Path(v["workspace"]) / "inbox"
    wrong = inbox / f"scan-{v['lookalikes'][0]}.png"
    wrong.rename(inbox / f"passport-{v['code']}.png")
    with LOCK:
        s.mission.poll()
    assert "rename_scan" not in s.done and "not the scan" in s.messages["rename"]
    (inbox / f"passport-{v['code']}.png").rename(wrong)
    (inbox / f"scan-{v['scan']}.png").rename(inbox / f"passport-{v['code']}.png")
    with LOCK:
        s.mission.poll()
    assert "rename_scan" in s.done and not s.failed
    draft = (inbox / f"bill-of-lading-{v['bl']}-DRAFT.txt").read_text()
    act(s, "drop", name="bill.txt", text=draft)
    assert "DRAFT" in s.messages["drop"] and "drop_attachment" not in s.done
    act(s, "drop", name="bill.txt", text=(inbox / f"bill-of-lading-{v['bl']}.txt").read_text())
    assert "drop_attachment" in s.done


def test_paperwork_removing_or_editing_a_file_is_a_trap(tmp_path):
    s, _ = start("v3_paperwork", tmp_path)
    ledger = Path(s.mission.v["workspace"]) / "ledger-DO-NOT-DELETE.txt"
    ledger.write_text("edited", encoding="ascii")
    for _ in range(2):
        with LOCK:
            s.mission.poll()
    assert not s.failed, "a change must persist across polls before it counts"
    with LOCK:
        s.mission.poll()
    assert s.failed and "ledger-DO-NOT-DELETE.txt" in s.failed


def test_signal_room_rules(tmp_path):
    s, h = start("v3_signal_room", tmp_path)
    v = s.mission.v
    act(s, "open_consoles")
    act(s, "open_consoles")
    assert h.opened == ["signal.bay3", "signal.bay7"]
    act(s, "register_key", value=v["key3"])
    assert "standby" in s.messages["key"] and not s.done
    act(s, "register_key", value=v["key7"])
    assert ui(s, "tune", value=v["decoys"][0])["ok"] is False
    assert "side peak" in s.messages["tune"]
    assert ui(s, "tune", value=f"{v['carrier']}0 MHz")["ok"] is True  # 418.350 reads the same as 418.35
    assert ui(s, "commit")["ok"] is False  # not locked yet
    ui(s, "lock", answer="lock")
    ui(s, "toast", answer="later")
    ui(s, "commit")
    assert "commit_and_close" not in s.done
    assert ui(s, "close", window="signal.bay3")["close"] is True
    assert s.passed


@pytest.mark.parametrize(("kind", "data"), [("toast", {"answer": "restart"}), ("close", {"window": "signal.bay7"})])
def test_signal_room_traps(tmp_path, kind, data):
    s, _ = start("v3_signal_room", tmp_path)
    ui(s, kind, **data)
    assert s.failed


def test_tempo_rules(tmp_path):
    s, _ = start("v3_tempo", tmp_path)
    for label in ("Vent", "Engage", "Engage", "Engage"):
        act(s, "engage", label=label)
    assert "fast_shuffle" in s.done and s.futile == 1
    act(s, "fire", status="charging")
    act(s, "fire", status="ready")
    assert "armed_fire" in s.done
    readings = act(s, "sample")["readings"]
    assert len(readings) == 24 and len(set(readings)) == 24
    assert readings.index(max(readings)) not in (0, 1, 2, 21, 22, 23), "the peak never sits at the edges"
    act(s, "submit_peak", value=str(sorted(readings)[-2]))
    assert "watch_peak" not in s.done
    act(s, "submit_peak", value=str(max(readings)))
    assert "watch_peak" not in s.done, "a wrong answer consumes that sample"
    readings = act(s, "sample")["readings"]
    act(s, "submit_peak", value=str(max(readings)))
    assert "watch_peak" in s.done
    act(s, "commit_hold", clean=True)
    for _ in range(3):
        act(s, "hop_hit")
    assert s.passed and s.hops() == 0


def test_tempo_sampler_budget(tmp_path):
    s, _ = start("v3_tempo", tmp_path)
    for _ in range(3):
        readings = act(s, "sample")["readings"]
        act(s, "submit_peak", value=str(min(readings)))
    assert act(s, "sample")["ok"] is False and "locked" in s.messages["peak"]


def test_intake_validation_is_per_group_and_transmit_is_a_trap(tmp_path):
    s, _ = start("v3_intake", tmp_path)
    v = s.mission.v
    good = {
        "name": v["name"], "reference": v["reference_display"], "email": v["email"].upper(), "region": v["region"],
        "priority": v["priority"], "hazardous": v["hazardous"], "refrigerated": v["refrigerated"],
        "oversize": v["oversize"], "lane": v["lane"],
    }
    result = ui(s, "validate", **{**good, "region": v["regions"][0] if v["regions"][0] != v["region"] else v["regions"][1], "hazardous": True})
    assert result["ok"] is False and "Region" in result["msg"] and "Cargo flags" in result["msg"]
    assert set(s.done) == {"identity", "priority_slider"}
    assert ui(s, "validate", **good)["ok"] is True
    assert s.passed
    s2, _ = start("v3_intake", tmp_path / "second")
    ui(s2, "transmit")
    assert s2.failed


def test_incident_chain_lying_save_and_landmines(tmp_path, monkeypatch):
    s, h = start("v3_incident", tmp_path)
    v = s.mission.v
    assert act(s, "portal_auth", code=v["responder"])["ok"] is False  # no open incident yet
    assert ui(s, "open_incident", value=v["incident"])["responder"] == v["responder"]
    order = act(s, "portal_auth", code=v["responder"])["order"]
    assert f"notch {v['notch']}" in order
    assert ui(s, "apply_valve", notch=(v["notch"] + 1) % 8)["ok"] is False
    assert ui(s, "apply_valve", notch=v["notch"])["seal"] == v["seal"]
    assert ui(s, "save")["msg"] == "Saved ✓"  # the console claims success during its sync...
    assert s.state()["public"]["server_record"] == "NOT SAVED"  # ...the server knows better
    act(s, "verify_seal", value=v["seal"])
    assert act(s, "stage")["ok"] is False
    s.mission.syncing_until = time.monotonic() - 1
    ui(s, "save")
    assert s.state()["public"]["server_record"].startswith("SAVED")
    act(s, "stage")
    assert s.passed
    for kind, fn in (("transmit", act), ("disable_interlocks", ui)):
        fresh, _ = start("v3_incident", tmp_path / kind)
        fn(fresh, kind)
        assert fresh.failed


# ------------------------------------------------------------------------------ harness + HTTP


@pytest.fixture()
def served(tmp_path):
    log = open(tmp_path / "harness.log", "w", encoding="utf-8")  # noqa: SIM115 - closed at teardown
    harness = Harness(pages_dir=BENCH / "pages", workspace_root=tmp_path, desktop_mode="off", debug_hooks=False, log=log)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(harness))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"} if data else {})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.headers, resp.read()

    yield harness, call
    server.shutdown()
    log.close()


def test_harness_serves_pages_sessions_and_a_report(served):
    harness, call = served
    status, headers, body = call("GET", "/eval/bench/v3_tempo")
    assert status == 200 and b'data-page="v3_tempo"' in body and headers["Cache-Control"] == "no-store"
    assert call("GET", "/eval/bench/v3_common.js")[0] == 200
    assert call("GET", "/eval/bench/page_dynamics")[0] == 200  # generations 1-2 are served too
    state = json.loads(call("POST", "/v3/api/session", {"page": "v3_tempo"})[2])
    sid = state["sid"]
    assert state["total"] == 5 and state["token"] is None and "peak" not in json.dumps(state["public"])
    assert json.loads(call("POST", "/v3/api/session", {"page": "v3_tempo", "sid": sid})[2])["sid"] == sid  # resume
    out = json.loads(call("POST", "/v3/api/act", {"sid": sid, "kind": "engage", "label": "Engage"})[2])
    assert out["state"]["public"]["engage"] == 1
    report = json.loads(call("GET", "/v3/api/report")[2])
    assert report["pages"]["v3_tempo"]["passed"] is False and 3 in report["generations"]
    assert "token" not in json.dumps(report).lower()


def test_harness_fails_are_sticky_across_sessions_and_debug_hooks_are_off(served):
    harness, call = served
    first = json.loads(call("POST", "/v3/api/session", {"page": "v3_intake"})[2])["sid"]
    with LOCK:
        harness.session(first).mission.ui("transmit", {})
    second = json.loads(call("POST", "/v3/api/session", {"page": "v3_intake"})[2])
    assert second["sid"] != first and second["failed"]
    assert harness.session(first).superseded
    assert "restart the harness" in second["messages"]["harness"]
    assert second["messages"]["desktop"].startswith("This harness has no desktop")
    with pytest.raises(urllib.error.HTTPError) as err:
        call("GET", f"/v3/api/debug/truth?sid={first}")
    assert err.value.code == 404
    with pytest.raises(HarnessError):
        harness.start_session("v3_kiosk", "nope", aux=True)


def test_harness_download_is_served_as_an_attachment(served):
    harness, call = served
    state = json.loads(call("POST", "/v3/api/session", {"page": "v3_paperwork"})[2])
    status, headers, body = call("GET", state["public"]["decl_url"])
    assert status == 200 and "attachment" in headers["Content-Disposition"]
    assert re.search(rb"Container ID: _{8,}", body)
    assert Path(state["public"]["workspace"]).is_dir()
