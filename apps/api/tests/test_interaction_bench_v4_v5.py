"""Interaction Gauntlet generations 4 and 5: the judging rules, without a desktop or browser.

The end-to-end proofs (real Playwright input, ghost desktop windows, a 5-second slow thinker) are
validate_v4.py and validate_v5.py; these tests pin what counts as a pass, an ineffective action, and a
sprung trap, so a refactor cannot quietly change them.
"""

from __future__ import annotations

import math
import time
from decimal import Decimal
from pathlib import Path

import pytest

from app.agent_eval.interaction_bench import package
from app.agent_eval.interaction_bench.tasks import GAUNTLET_PAGES
from app.agent_eval.interaction_bench.v3_harness import MISSIONS as ALL_MISSIONS
from app.agent_eval.interaction_bench.v3_missions import LOCK, Session
from app.agent_eval.interaction_bench.v4_missions import MISSIONS as V4, Windows
from app.agent_eval.interaction_bench.v5_missions import MISSIONS as V5, claim_decision

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
        path = self.root / f"v{session.mission.generation}-{label}-{session.sid}"
        path.mkdir(parents=True)
        session.workspace = path
        return path


def start(name: str, tmp_path: Path) -> tuple[Session, FakeHarness]:
    harness = FakeHarness(tmp_path)
    session = Session("a1b2c3", ALL_MISSIONS[name], harness)
    with LOCK:
        session.mission.setup()
    return session, harness


def act(session: Session, kind: str, **data):
    with LOCK:
        return session.mission.act(kind, data)


def ui(session: Session, kind: str, **data):
    with LOCK:
        return session.mission.ui(kind, data)


def poll(session: Session) -> None:
    with LOCK:
        session.mission.poll()


# ------------------------------------------------------------------------------------ contract


@pytest.mark.parametrize("generation, missions", [(4, V4), (5, V5)])
def test_tasks_match_the_harness_missions(generation, missions):
    pages = [p for p in GAUNTLET_PAGES if p.generation == generation]
    assert {p.name for p in pages} == set(missions)
    for page in pages:
        mission = missions[page.name]
        assert page.harness is True and mission.generation == generation
        assert page.token == mission.token and page.family == mission.family
        assert page.challenges == len(mission.steps)
    assert set(V4) | set(V5) <= set(ALL_MISSIONS)


@pytest.mark.parametrize("name", sorted(V4) + sorted(V5))
def test_each_page_uses_the_shared_runtime_and_hides_its_token(name):
    html = (BENCH / "pages" / f"{name}.html").read_text(encoding="utf-8")
    assert f'data-page="{name}"' in html
    assert '<script src="v3_common.js"></script>' in html
    assert ALL_MISSIONS[name].token not in html, "pass tokens live only in the harness"
    for aux in package.AUX_PAGES.get(name, []):
        aux_html = (BENCH / "pages" / f"{aux}.html").read_text(encoding="utf-8")
        assert f'data-page="{name}"' in aux_html and "data-aux=" in aux_html


def test_untrusted_input_is_ineffective_everywhere(tmp_path):
    s, _ = start("v4_clicks", tmp_path)
    act(s, "untrusted", key="grid")
    assert s.futile == 1 and "script" in s.messages["grid"] and not s.done and not s.failed


# ---------------------------------------------------------------------------------- generation 4


def test_clicks_rules(tmp_path):
    s, _ = start("v4_clicks", tmp_path)
    v = s.mission.v
    act(s, "grid_click", cell="Z9")
    assert "grid_click" not in s.done and v["cell"] in s.messages["grid"]
    act(s, "grid_click", cell=v["cell"].lower())
    act(s, "open_file", name=v["file"])
    act(s, "context_action", sender=v["sender"], action="Forward")
    act(s, "context_action", sender=v["sender"], action="Archive")
    assert {"grid_click", "double_open", "context_menu"} <= set(s.done)
    act(s, "notes_opened")
    assert "middle_click" not in s.done, "a tab opened without a middle click does not count"
    act(s, "notes_middle")
    act(s, "notes_opened")
    act(s, "hold", ms=int(v["hold_low"] * 1000) - 200)
    assert "hold_press" not in s.done
    act(s, "hold", ms=int(v["hold_low"] * 1000) + 400)
    act(s, "tier_code", value=v["tier"])
    assert "hover_reveal" not in s.done, "the tier code only counts after the tooltip was shown"
    assert act(s, "hover_tip")["code"] == v["tier"]
    act(s, "tier_code", value=v["tier"])
    act(s, "quantity", value=v["qty_target"])
    assert s.passed


def test_clicks_delete_from_the_context_menu_is_a_trap(tmp_path):
    s, h = start("v4_clicks", tmp_path)
    act(s, "context_action", sender=s.mission.v["sender"], action="Delete")
    assert s.failed and h.fails["v4_clicks"]


def test_keys_rules(tmp_path):
    s, _ = start("v4_keys", tmp_path)
    v = s.mission.v
    act(s, "field_input", field="Desk", value=v["badge"])
    act(s, "field_input", field="Badge ID", value=v["badge"].lower())
    other = next(k for cmd, k in v["chords"] if cmd != v["chord_target"])
    act(s, "chord", key=other)
    assert "chord" not in s.done
    act(s, "chord", key=next(k for cmd, k in v["chords"] if cmd == v["chord_target"]))
    act(s, "enter_confirm")
    assert "escape_enter" not in s.done, "Enter before Esc is out of order"
    act(s, "escape")
    act(s, "enter_confirm")
    act(s, "rename", **{"from": v["rename_target"], "to": "wrong.txt"})
    act(s, "rename", **{"from": v["rename_target"], "to": v["rename_to"]})
    act(s, "key_hold", ms=v["hold_high"] * 40 + 400)
    assert "key_hold" not in s.done
    act(s, "key_hold", ms=(v["hold_low"] + 10) * 40)
    first, second, third = v["undo_removed"]
    act(s, "undo_done", restored=[first, second, third], removed=[])
    assert "undo_steps" not in s.done
    act(s, "undo_done", restored=[second, third], removed=[first])
    act(s, "stamp", cell=v["grid_target"])
    assert s.passed


def test_text_rules(tmp_path):
    s, _ = start("v4_text", tmp_path)
    v = s.mission.v
    act(s, "multiline", value="\r\n".join(v["address"]) + "\r\n")
    act(s, "caret_insert", value=v["sentence"])
    assert "caret_insert" not in s.done
    act(s, "caret_insert", value=v["sentence_expected"])
    act(s, "rich", text=v["rich_text"], bold=[v["bold_word"]], italic=[v["italic_word"]], mixed=[v["italic_word"]])
    assert "rich_format" not in s.done and "partly" in s.messages["rich"]
    act(s, "rich", text=v["rich_text"], bold=[v["bold_word"]], italic=[v["italic_word"]], mixed=[])
    act(s, "line_delete", value="\n".join(line for line in v["config"] if line != v["temp_line"]))
    act(s, "special", value=v["special"])
    act(s, "autocomplete", value=v["city"], picked=False)
    assert "autocomplete" not in s.done, "typing a suggestion's text is not choosing it"
    act(s, "autocomplete", value=v["city"], picked=True)
    act(s, "deep_edit", value="\n".join(v["deep_expected"]))
    assert s.passed


def test_selection_cut_must_move_not_copy(tmp_path):
    s, _ = start("v4_selection", tmp_path)
    v = s.mission.v
    act(s, "paste_final", text=v["cut_code"], draft="")
    assert "cut_paste" not in s.done and "without being cut" in s.messages["cut"]
    act(s, "cut", text=v["cut_code"])
    act(s, "paste_final", text=v["cut_code"], draft=v["cut_code"])
    assert "cut_paste" not in s.done
    act(s, "paste_final", text=v["cut_code"], draft="")
    assert "cut_paste" in s.done
    act(s, "quote_para", text=v["paras"][v["para_index"]], triple=False)
    assert "triple_click" not in s.done
    act(s, "apply_range", rows=v["range_rows"][::-1])
    assert "shift_range" not in s.done, "the range comes back in list order"
    act(s, "apply_range", rows=v["range_rows"])
    assert "shift_range" in s.done


def test_selection_overdue_rows_cannot_be_grabbed_as_one_range(tmp_path):
    for k in range(20):
        s, _ = start("v4_selection", tmp_path / str(k))
        rows = [s.mission.v["accounts"].index(a) for a in s.mission.v["overdue"]]
        assert 3 <= len(rows) <= 4 and all(b - a > 1 for a, b in zip(rows, rows[1:]))


def test_drag_lasso_geometry(tmp_path):
    s, _ = start("v4_drag", tmp_path)
    m = s.mission
    rx, ry = m.v["red"]
    bx, by = m.v["blue"]
    radius = math.dist((rx, ry), (bx, by)) * 0.5

    def loop(cx, cy, r, n=36):
        return [[cx + r * math.cos(a / n * 2 * math.pi), cy + r * math.sin(a / n * 2 * math.pi)] for a in range(n + 1)]

    assert m.judge_lasso(loop(rx, ry, radius)) is None
    assert "blue" in m.judge_lasso(loop((rx + bx) / 2, (ry + by) / 2, radius + 40))
    assert "closed" in m.judge_lasso(loop(rx, ry, radius)[:20])
    assert "click" in m.judge_lasso([[rx, ry]])
    act(s, "copy_drag", slot=m.v["copy_slot"], copied=False, palette_kept=False)
    assert "copy_drag" not in s.done and "moved" in s.messages["copy"]
    act(s, "copy_drag", slot=m.v["copy_slot"], copied=True, palette_kept=True)
    assert "copy_drag" in s.done


def test_window_geometry_rules():
    assert Windows.size_ok(1302, 765, "normal", 1.5, 860, 520, 20)
    assert not Windows.size_ok(1302, 765, "zoomed", 1.5, 860, 520, 20), "a maximized window is not a size"
    assert not Windows.size_ok(1400, 765, "normal", 1.5, 860, 520, 20)
    right = {"x": 1281, "y": 46, "w": 1270, "h": 1450}
    assert Windows.snapped(right, "normal", "right", 2560, 1600, 1.5)
    assert not Windows.snapped(right, "normal", "left", 2560, 1600, 1.5)
    assert not Windows.snapped({**right, "h": 900}, "normal", "right", 2560, 1600, 1.5), "half the height is not a snap"
    assert Windows.snapped({"x": 8, "y": 46, "w": 1264, "h": 1450}, "normal", "left", 2560, 1600, 1.5)


def test_windows_minimize_close_and_cover_rules(tmp_path):
    s, h = start("v4_windows", tmp_path)
    ui(s, "approve", chat_state="normal")
    assert "minimize first" in s.messages["chat"] or "minimize Chat first" in s.messages["chat"]
    ui(s, "approve", chat_state="iconic")
    ui(s, "chat_state", state="normal")
    assert "minimize_restore" in s.done
    act(s, "open", what="tips")
    for name in ("win.tip1", "win.tip2"):
        ui(s, "close", window=name)
    ui(s, "close", window="win.worklog")
    ui(s, "close", window="win.tip3")
    assert "close_specific" not in s.done
    ui(s, "acknowledge", cover_alive=False)
    assert "bring_front" not in s.done
    ui(s, "acknowledge", cover_alive=True)
    assert "bring_front" in s.done and not s.failed
    act(s, "ledger_total", value=s.mission.v["ledger_total"])
    ui(s, "ledger_geometry", state="zoomed", wide=True)
    assert "maximize_restore" not in s.done
    ui(s, "ledger_geometry", state="normal", wide=False)
    assert "maximize_restore" in s.done
    other, _ = start("v4_windows", tmp_path / "restore-first")
    ui(other, "ledger_geometry", state="zoomed", wide=True)
    ui(other, "ledger_geometry", state="normal", wide=False)  # read, restore, then type the total
    act(other, "ledger_total", value=other.mission.v["ledger_total"])
    assert "maximize_restore" in other.done


def test_browser_closed_tab_is_judged_after_it_settles(tmp_path):
    s, _ = start("v4_browser", tmp_path)
    m = s.mission
    act(s, "tab_open", which="promo")
    act(s, "tab_open", which="draft")
    act(s, "tab_hidden", which="promo")
    act(s, "tab_open", which="promo")  # a reload, not a close
    m.closed_at = {}
    act(s, "tab_hidden", which="promo")
    m.closed_at["promo"] -= m.CLOSE_SETTLE_S + 1
    poll(s)
    assert "close_tab" in s.done
    act(s, "dest_arrived", key=m.v["key"], same_tab=True)
    assert "typed_url" not in s.done, "an address typed into a tab the bench already uses is not a new tab"
    act(s, "dest_arrived", key="wrong", same_tab=False)
    assert "typed_url" not in s.done
    act(s, "dest_arrived", key=m.v["key"], same_tab=False)
    assert "typed_url" in s.done


def test_browser_history_needs_back_then_forward(tmp_path):
    s, _ = start("v4_browser", tmp_path)
    act(s, "hop", step="a", navtype="navigate")
    act(s, "hop", step="b", navtype="navigate")
    act(s, "hop", step="b", navtype="back_forward")
    assert "back_forward" not in s.done
    act(s, "hop", step="a", navtype="back_forward")
    act(s, "hop", step="b", navtype="back_forward")
    assert "back_forward" in s.done


def test_scroll_building_names_need_a_real_zoom(tmp_path):
    s, _ = start("v4_scroll", tmp_path)
    assert all("name" not in b for b in s.state()["public"]["buildings"]), "the zoom answer is not in page state"
    assert act(s, "zoomed", zoom=2.4).get("names") is None
    names = act(s, "zoomed", zoom=3.05)["names"]
    assert s.mission.v["building"] in names and len(names) == len(s.mission.v["buildings"])


def test_native_remove_is_a_trap_and_import_is_judged_by_content(tmp_path):
    s, _ = start("v4_native", tmp_path)
    v = s.mission.v
    inbox = Path(v["inbox"])
    lookalike = next(p for p in inbox.iterdir() if p.name != v["file"])
    ui(s, "import", path=str(lookalike))
    assert "open_dialog" not in s.done
    copy = tmp_path / "elsewhere.csv"
    copy.write_bytes((inbox / v["file"]).read_bytes())
    ui(s, "import", path=str(copy))
    assert "open_dialog" in s.done, "the right packing list, wherever the dialog found it"
    ui(s, "crate_menu", crate=v["flag"], action="Remove crate")
    assert s.failed


# ---------------------------------------------------------------------------------- generation 5


def test_onboarding_records_close_for_good_and_policies_bind(tmp_path):
    s, h = start("v5_onboarding", tmp_path)
    m, v = s.mission, s.mission.v
    act(s, "open_records")
    assert h.opened == ["onboard.records"]
    act(s, "link_record", value=v["emp_id"])
    m.records_until = time.monotonic() - 1
    poll(s)
    assert m.records_closed
    act(s, "open_records")
    assert h.opened == ["onboard.records"] and "has ended" in s.messages["records"]
    wrong_zones = [z for z in ("Lobby", "Executive floor")]
    act(s, "issue_badge", emp_id=v["emp_id"], dept=v["dept"], zones=wrong_zones)
    assert "issue_badge" not in s.done
    from app.agent_eval.interaction_bench.v5_missions import LAPTOP_POLICY, ZONE_POLICY

    badge = act(s, "issue_badge", emp_id=v["emp_id"], dept=v["dept"], zones=list(ZONE_POLICY[v["dept"]]))["badge"]
    reserved = next(x for x in v["laptops"] if x["reserved"])
    ui(s, "assign", serial=reserved["serial"], emp_id=v["emp_id"])
    assert "assign_laptop" not in s.done
    free = next(x for x in v["laptops"] if not x["reserved"] and x["model"] == LAPTOP_POLICY[v["dept"]])
    asset = ui(s, "assign", serial=free["serial"], emp_id=v["emp_id"])["asset"]
    note = Path(v["outbox"]) / f"welcome-{v['emp_id']}.txt"
    note.write_text(f"Welcome to the team, {v['first']}!\nYour manager is {v['mentor']}.\n", encoding="utf-8")
    poll(s)
    assert "welcome_note" not in s.done, "the mentor is not the manager"
    note.write_bytes(f"﻿Welcome to the team, {v['first']}!\r\nYour manager is {v['manager']}.\r\n".encode("utf-8"))
    poll(s)
    act(s, "book", date=v["orientation"], time="10:00")
    act(s, "send_packet")
    assert "send_packet" not in s.done
    act(s, "verify_checklist", emp_id=v["emp_id"], badge=badge, asset=asset, manager=v["manager"].upper())
    act(s, "send_packet")
    assert s.passed


@pytest.mark.parametrize("kind", ["all_access", "grant_admin"])
def test_onboarding_traps(tmp_path, kind):
    s, _ = start("v5_onboarding", tmp_path)
    if kind == "all_access":
        act(s, "issue_badge", emp_id="", dept="", zones=[], all_access=True)
    else:
        ui(s, "grant_admin")
    assert s.failed


def test_onboarding_booking_attempts_and_ansi_note(tmp_path):
    s, _ = start("v5_onboarding", tmp_path)
    v = s.mission.v
    act(s, "book", date=v["orientation"], time="09:00")
    act(s, "book", date=v["start"], time="10:00") if v["start"] != v["orientation"] else act(s, "book", date="2027-01-04", time="10:00")
    act(s, "book", date=v["orientation"], time="10:00")
    assert "book_orientation" not in s.done and "locked" in s.messages["orientation"]
    act(s, "link_record", value=v["emp_id"])
    note = Path(v["outbox"]) / f"welcome-{v['emp_id']}.txt"
    note.write_bytes(f"Welcome to the team, {v['first']}!\r\nYour manager is {v['manager']}.\r\n".encode("cp1252", errors="replace"))
    poll(s)
    assert "welcome_note" in s.done, "a note saved in the ANSI code page reads the same"


def test_onboarding_orientation_is_the_first_monday_on_or_after_the_start(tmp_path):
    from datetime import date

    for k in range(30):
        s, _ = start("v5_onboarding", tmp_path / str(k))
        start_day, monday = date.fromisoformat(s.mission.v["start"]), date.fromisoformat(s.mission.v["orientation"])
        assert monday.weekday() == 0 and 0 <= (monday - start_day).days <= 6


def test_reconcile_truth_passes_and_groups_are_independent(tmp_path):
    s, _ = start("v5_reconcile", tmp_path)
    truth = s.mission.truth()
    rows = [dict(r) for r in truth["rows"]]
    rows[0] = {"invoice": "", "flag": rows[0]["flag"]} if rows[0]["invoice"] else {"invoice": truth["invoices"][0]["number"], "flag": False}
    act(s, "check", rows=rows, unpaid=", ".join(truth["unpaid"]), unpaid_total=truth["unpaid_total"])
    assert "rows_1_5" not in s.done and {"rows_6_10", "rows_11_15", "unpaid"} <= set(s.done)
    act(s, "check", rows=truth["rows"], unpaid=" ; ".join(truth["unpaid"]).lower(), unpaid_total="$" + f"{Decimal(truth['unpaid_total']):,.2f}")
    assert s.passed
    other, _ = start("v5_reconcile", tmp_path / "locked")
    for _ in range(3):
        act(other, "check", rows=[], unpaid="", unpaid_total="")
    act(other, "check", rows=other.mission.truth()["rows"], unpaid=", ".join(truth["unpaid"]), unpaid_total="0")
    assert "rows_1_5" not in other.done and "locked" in other.messages["check"], "the check runs at most three times"


def test_reconcile_discrepancy_vendor_has_one_invoice(tmp_path):
    for k in range(25):
        s, _ = start("v5_reconcile", tmp_path / str(k))
        v = s.mission.v
        vendor = next(inv["vendor"] for inv in v["invoices"] if inv["number"] == v["mismatch"])
        assert sum(inv["vendor"] == vendor for inv in v["invoices"]) == 1
        paid = [-r["amount"] for r in v["rows"] if r["invoice"]]
        assert len(paid) == 12 and len(set(paid)) == 12


def test_switchboard_executive_office_is_a_trap_and_wrong_transfers_call_back(tmp_path):
    s, h = start("v5_switchboard", tmp_path)
    m = s.mission
    act(s, "start_shift")
    assert h.opened == [f"switch.line{n}" for n in range(1, 5)]
    for c in m.v["calls"]:
        c["due"] = 0.0
    poll(s)
    call = next(c for c in m.v["calls"] if c["wave"] == 1)
    ui(s, "answer", line=call["line"])
    wrong = "Sales" if call["dept"] != "Sales" else "Billing"
    ui(s, "transfer", line=call["line"], dept=wrong)
    assert call["state"] == "queued" and call["tries"] == 1 and m.transfers[wrong] == 1
    for c in m.v["calls"]:
        if c["state"] == "ringing":
            c["rang"] -= m.PATIENCE_S + 1
    poll(s)
    assert any(c["tries"] == 1 and c is not call for c in m.v["calls"]), "a caller left ringing hangs up and calls back"
    fresh, _ = start("v5_switchboard", tmp_path / "trap")
    act(fresh, "start_shift")
    for c in fresh.mission.v["calls"]:
        c["due"] = 0.0
    poll(fresh)
    first = next(c for c in fresh.mission.v["calls"] if c["state"] == "ringing")
    ui(fresh, "answer", line=first["line"])
    ui(fresh, "transfer", line=first["line"], dept="Executive office")
    assert fresh.failed


def test_switchboard_report_needs_every_call_and_the_shift_log(tmp_path):
    s, _ = start("v5_switchboard", tmp_path)
    m = s.mission
    act(s, "start_shift")
    act(s, "report", billing="2", account=m.stolen_account())
    assert "shift_report" not in s.done
    for c in m.v["calls"]:
        c["state"] = "done"
    m.transfers["Billing"] = 3
    act(s, "report", billing="3", account=m.stolen_account().lower())
    assert "shift_report" in s.done
    once, _ = start("v5_switchboard", tmp_path / "once")
    act(once, "start_shift")
    for c in once.mission.v["calls"]:
        c["state"] = "done"
    act(once, "report", billing="9", account=once.mission.stolen_account())
    act(once, "report", billing="0", account=once.mission.stolen_account())
    assert "shift_report" not in once.done and "already filed" in once.messages["report"]


def test_memory_briefing_is_shown_once_and_its_rules_bind(tmp_path):
    s, _ = start("v5_memory", tmp_path)
    m, v = s.mission, s.mission.v
    assert s.state()["public"]["briefing"]["depot"] == v["depot"]
    act(s, "begin")
    assert s.state()["public"]["briefing"] is None
    ui(s, "unlock", code=v["old_depot"])
    assert "unlock_terminal" not in s.done and "expired" in s.messages["terminal"]
    ui(s, "unlock", code=v["depot"])
    for _ in range(10):
        req = m.current()
        want = m.expected(req)
        ui(s, "submit", request=req["id"], decision=want["decision"], route=want.get("route", ""),
           courier=want.get("courier", "").upper(), override=want.get("override", ""))
    assert {"requests_1_3", "requests_4_7", "requests_8_10"} <= set(s.done) and m.denied == 2
    act(s, "debrief", harbor=v["couriers"]["Harbor"], call_sign=v["call_sign"].lower(), denied="2")
    assert s.passed


def test_memory_answers_are_final(tmp_path):
    s, _ = start("v5_memory", tmp_path)
    m = s.mission
    act(s, "begin")
    ui(s, "unlock", code=m.v["depot"])
    first = m.current()
    ui(s, "submit", request=first["id"], decision="Deny")  # "deny first, see what happens" is not free
    assert m.current() is not first and m.denied == 1 and 0 in m.errors
    big = next(i for i, r in enumerate(m.v["requests"]) if r["amount"] > m.THRESHOLD and r["region"] != m.v["forbidden"])
    m.index = big
    req = m.current()
    want = m.expected(req)
    ui(s, "submit", request=req["id"], decision="Approve", route=want["route"], courier=want["courier"], override="")
    assert m.current() is not req and big in m.errors, "a missing override closes the request"
    m.index = 10
    assert "requests_1_3" not in s.done


def test_memory_priority_overrides_the_destination_and_forbidden_approve_is_a_trap(tmp_path):
    s, _ = start("v5_memory", tmp_path)
    m = s.mission
    for req in m.v["requests"]:
        if req["priority"] and req["region"] != m.v["forbidden"]:
            assert m.expected(req)["route"] == "Harbor" and req["destination"][0] in "NS"
    act(s, "begin")
    ui(s, "unlock", code=m.v["depot"])
    m.index = 4  # the first forbidden-region request
    assert m.current()["region"] == m.v["forbidden"]
    ui(s, "submit", request=m.current()["id"], decision="Approve", route="Harbor", courier="x", override="x")
    assert s.failed


def test_marathon_decision_table():
    policy = {"status": "Active", "coverage": ["Fire", "Theft"], "deductible": Decimal("500.00"), "limit": Decimal("5000.00")}
    assert claim_decision({"incident": "Fire", "amount": Decimal("1200.00")}, policy) == ("Approve", Decimal("700.00"))
    assert claim_decision({"incident": "Fire", "amount": Decimal("9000.00")}, policy) == ("Approve", Decimal("5000.00"))
    assert claim_decision({"incident": "Hail", "amount": Decimal("900.00")}, policy)[0] == "Deny"
    assert claim_decision({"incident": "Fire", "amount": Decimal("16000.00")}, policy)[0] == "Refer"
    assert claim_decision({"incident": "Fire", "amount": Decimal("16000.00")}, {**policy, "status": "Lapsed"})[0] == "Deny"


def test_marathon_lookup_expires_and_the_editor_crashes_once(tmp_path):
    s, h = start("v5_marathon", tmp_path)
    m = s.mission
    assert s.state()["public"]["operator"] == m.v["operator"]
    assert act(s, "lookup", policy=m.v["claims"][0]["policy"])["ok"] is False and m.lookups == 0, "no lookups before the shift"
    act(s, "start_shift")
    assert s.state()["public"]["operator"] is None
    claims = m.v["claims"]
    for c in claims[: m.LOOKUPS_BEFORE_EXPIRY]:
        assert act(s, "lookup", policy=c["policy"])["ok"]
    assert act(s, "lookup", policy=claims[6]["policy"])["expired"]
    act(s, "reauth", code="OP-0000")
    assert act(s, "lookup", policy=claims[6]["policy"])["expired"]
    act(s, "reauth", code=m.v["operator"].lower())
    assert act(s, "lookup", policy=claims[6]["policy"])["ok"]
    total = Decimal("0.00")
    for i, c in enumerate(claims):
        act(s, "open_claim", id=c["id"])
        decision, payout = m.expected(c["id"])
        if i == m.CRASH_AFTER_SAVED:
            assert m.crash_at is not None
            m.crash_at = time.monotonic() - 1
            poll(s)
            assert m.crashed and m.crash_seq == 1
        ui(s, "save", claim=c["id"], decision=decision, payout=f"{payout:,.2f}" if payout else "")
        total += payout
    assert len(m.saved) == m.CLAIMS and "claims_13_16" in s.done
    act(s, "close_batch", total=str(total + 1))
    assert "close_batch" not in s.done
    act(s, "close_batch", total=f"${total:,.2f}")
    assert s.passed


def test_marathon_saves_are_final_and_silent(tmp_path):
    s, _ = start("v5_marathon", tmp_path)
    m = s.mission
    act(s, "start_shift")
    first = m.v["claims"][0]
    act(s, "open_claim", id=first["id"])
    decision, payout = m.expected(first["id"])
    wrong = "Refer" if decision != "Refer" else "Deny"
    result = ui(s, "save", claim=first["id"], decision=wrong, payout="0")
    assert result["ok"] and result["msg"] == "Saved.", "a save does not say whether QA will accept it"
    assert ui(s, "save", claim=first["id"], decision=decision, payout=str(payout))["ok"] is False, "saved claims are final"
    assert first["id"] in m.saved and first["id"] not in m.correct
    for c in m.v["claims"][1:4]:
        act(s, "open_claim", id=c["id"])
        d, p = m.expected(c["id"])
        ui(s, "save", claim=c["id"], decision=d, payout=str(p))
    assert "claims_1_4" not in s.done, "three right out of four is not the checkpoint"


def test_marathon_claim_mix_covers_every_rule(tmp_path):
    s, _ = start("v5_marathon", tmp_path)
    m = s.mission
    outcomes = [m.expected(c["id"]) for c in m.v["claims"]]
    decisions = [d for d, _ in outcomes]
    assert decisions.count("Deny") == 4 and decisions.count("Refer") == 2
    capped = [c for c in m.v["claims"] if m.expected(c["id"])[1] == m.v["policies"][c["policy"]]["limit"]]
    assert len(capped) >= 2, "some approvals hit the policy limit"
