"""The macOS desktop eval: sandbox safety, every task's setup and checker, no live model or app needed."""

from __future__ import annotations

import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from app.agent_eval.mac_bench import run as mac_run
from app.agent_eval.mac_bench import tasks as T


@pytest.fixture(autouse=True)
def no_apps(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(T, "_osascript", lambda script, timeout=15.0: calls.append(script) or "false")
    return calls


def make(name: str, tmp_path: Path, **kw) -> tuple[T.Task, T.Ctx, str]:
    task = T.TASKS[name]
    ctx = T.Ctx(T.Sandbox(name, tmp_path / "bench"), cleanup_apps=False, **kw)
    return task, ctx, task.setup(ctx)


def test_sandbox_is_marked_and_confined(tmp_path: Path) -> None:
    box = T.Sandbox("finder_rename", tmp_path / "bench")
    assert box.path.parent == (tmp_path / "bench").resolve() and (box.path / T.MARKER).is_file()
    assert box.destroy() and not box.path.exists()


def test_remove_refuses_unmarked_outside_and_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "bench"
    root.mkdir()
    precious = tmp_path / "mac-precious"
    precious.mkdir()
    (precious / "keep.txt").write_text("user file")
    with pytest.raises(T.SandboxError):
        T.remove_sandbox(precious, root)  # not under the root
    unmarked = root / "mac-x"
    unmarked.mkdir()
    with pytest.raises(T.SandboxError):
        T.remove_sandbox(unmarked, root)  # no marker
    other = root / "notes"
    other.mkdir()
    (other / T.MARKER).write_text("")
    with pytest.raises(T.SandboxError):
        T.remove_sandbox(other, root)  # not named mac-*
    link = root / "mac-link"
    link.symlink_to(precious)
    assert T.remove_sandbox(link, root) is False
    assert (precious / "keep.txt").read_text() == "user file"


def test_purge_stale_only_removes_marked_bench_folders(tmp_path: Path) -> None:
    root = tmp_path / "bench"
    a = T.Sandbox("finder_move", root)
    (root / "mac-unmarked").mkdir()
    (root / "mine").mkdir()
    assert T.purge_stale(root) == 1
    assert not a.path.exists() and (root / "mac-unmarked").exists() and (root / "mine").exists()
    assert T.purge_stale(tmp_path / "missing") == 0


def test_task_registry_has_sixteen_tasks_with_budgets_and_traps() -> None:
    assert len(T.TASKS) == 16
    assert {t.family for t in T.TASKS.values()} == {"TextEdit", "Finder", "Calculator", "Safari", "Preview", "System Settings"}
    assert {t.tier for t in T.TASKS.values()} == {"basic", "multi-step", "hard", "long"}
    assert all(t.budget_min > 0 for t in T.TASKS.values())
    hard = [t for t in T.TASKS.values() if t.tier in ("hard", "long")]
    assert len(hard) == 8 and sum(bool(t.trap) for t in hard) >= 2 and all(t.skills for t in hard)


def test_textedit_new_doc(tmp_path: Path) -> None:
    task, ctx, goal = make("textedit_new_doc", tmp_path)
    assert str(ctx.dir) in goal and ctx.state["text"] in goal
    assert task.check(ctx)["checks"]["file_exists"] is False
    target = ctx.state["target"]
    target.write_text(ctx.state["text"] + "\n")
    assert task.check(ctx)["passed"]
    target.write_text("{\\rtf1\\ansi " + ctx.state["text"] + "}")
    g = task.check(ctx)
    assert not g["passed"] and not g["checks"]["plain_text"]
    target.write_text("something else")
    assert not task.check(ctx)["checks"]["content_matches"]


def test_textedit_edit_file(tmp_path: Path) -> None:
    task, ctx, goal = make("textedit_edit_file", tmp_path)
    target = ctx.state["target"]
    assert "DRAFT" in target.read_text() and not task.check(ctx)["passed"]
    target.write_text(ctx.state["expected"] + "\n")
    assert task.check(ctx)["passed"]
    target.write_text(ctx.state["expected"].replace("Operations", "Ops"))
    assert not task.check(ctx)["checks"]["edited_exactly"]  # collateral edit


def test_finder_new_folder(tmp_path: Path) -> None:
    task, ctx, _ = make("finder_new_folder", tmp_path)
    assert not task.check(ctx)["passed"]
    made = ctx.dir / ctx.state["name"]
    made.mkdir()
    (made / ".DS_Store").write_text("x")
    assert task.check(ctx)["passed"]
    (ctx.dir / "stray.txt").write_text("x")
    assert not task.check(ctx)["checks"]["nothing_else_changed"]


def test_finder_rename(tmp_path: Path) -> None:
    task, ctx, goal = make("finder_rename", tmp_path)
    assert ctx.state["old"].name in goal and ctx.state["new"].name in goal
    assert not task.check(ctx)["passed"]
    ctx.state["old"].rename(ctx.state["new"])
    assert task.check(ctx)["passed"]
    ctx.state["new"].write_text("changed")
    assert not task.check(ctx)["checks"]["content_intact"]
    ctx.state["other"].unlink()
    g = task.check(ctx)
    assert g["failed"] and g["ratio"] == 0.0


def test_finder_copy_instead_of_move_fails(tmp_path: Path) -> None:
    task, ctx, _ = make("finder_move", tmp_path)
    src, dest = ctx.state["src"], ctx.state["dest"]
    (dest / src.name).write_bytes(src.read_bytes())  # a copy: the origin is still there
    g = task.check(ctx)
    assert g["checks"]["in_archive"] and not g["checks"]["removed_from_origin"] and not g["passed"]
    src.unlink()
    assert task.check(ctx)["passed"]


def test_finder_sort_by_type(tmp_path: Path) -> None:
    task, ctx, _ = make("finder_sort_by_type", tmp_path)
    folder = ctx.state["folder"]
    folder.mkdir()
    for p in ctx.state["csvs"]:
        p.rename(folder / p.name)
    assert task.check(ctx)["passed"]
    moved_txt = ctx.state["txts"][0]
    moved_txt.rename(folder / moved_txt.name)
    g = task.check(ctx)
    assert g["failed"] and not g["passed"] and not g["checks"]["only_csv_in_folder"]


@pytest.mark.parametrize("shown,ok", [(["‎10,063"], True), (["", "10063"], True), (["10,064"], False), ([], False)])
def test_calculator_reads_display_through_ax(tmp_path: Path, shown: list[str], ok: bool) -> None:
    task, ctx, goal = make("calculator_product", tmp_path)
    a, b = [int(w) for w in goal.split("work out ")[1].split(" times ")[0:1] + goal.split(" times ")[1].split(".")[0:1]]
    expected = str(a * b)
    assert ctx.state["expected"] == expected
    shown = [s.replace("10,063", f"{a * b:,}").replace("10063", expected) if ok else s for s in shown]
    ctx.read_ax_values = lambda: shown
    g = task.check(ctx)
    assert g["passed"] is ok
    if not shown:
        assert not g["checks"]["calculator_readable"]


def test_normalize_number() -> None:
    assert T.normalize_number("‪1,234,567‬") == "1234567"
    assert T.normalize_number("−5") == "-5"


SAFARI_UA = "Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15"
CHROME_UA = "Mozilla/5.0 (Macintosh) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


def _visit(url: str, ua: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    return urllib.request.urlopen(req, timeout=5).read().decode()


def _confirm(base: str, code: str, ua: str) -> None:
    data = urllib.parse.urlencode({"code": code}).encode()
    urllib.request.urlopen(urllib.request.Request(base + "/confirm", data=data, headers={"User-Agent": ua}), timeout=5).read()


def test_safari_task_with_real_loopback_server(tmp_path: Path) -> None:
    task, ctx, goal = make("safari_confirm_code", tmp_path)
    try:
        server = ctx.state["server"]
        assert server.url in goal and ctx.state["code"] not in goal
        assert not task.check(ctx)["passed"]
        assert ctx.state["code"] in _visit(server.url, SAFARI_UA)
        _confirm(server.base, ctx.state["code"], SAFARI_UA)
        assert task.check(ctx)["passed"]
    finally:
        task.teardown(ctx)
    with pytest.raises(OSError):
        _visit(server.url, SAFARI_UA)  # server stopped by teardown


def test_safari_wrong_browser_and_invented_code(tmp_path: Path) -> None:
    task, ctx, _ = make("safari_confirm_code", tmp_path)
    try:
        server = ctx.state["server"]
        _visit(server.url, CHROME_UA)
        _confirm(server.base, ctx.state["code"], CHROME_UA)
        g = task.check(ctx)
        assert g["checks"]["confirmed_with_correct_code"] and not g["checks"]["loaded_in_safari"] and not g["passed"]
        ctx2 = T.Ctx(T.Sandbox("safari_confirm_code", tmp_path / "bench"), cleanup_apps=False)
        task.setup(ctx2)
        _confirm(ctx2.state["server"].base, "KESTREL-0000", SAFARI_UA)
        assert task.check(ctx2)["failed"]
        task.teardown(ctx2)
    finally:
        task.teardown(ctx)


def test_teardown_closes_only_sandbox_documents(tmp_path: Path, no_apps, monkeypatch) -> None:
    task = T.TASKS["textedit_new_doc"]
    ctx = T.Ctx(T.Sandbox("textedit_new_doc", tmp_path / "bench"), cleanup_apps=True)
    task.setup(ctx)
    task.teardown(ctx)
    # _app_running said "false" so nothing was closed; with it running, the script is scoped to the sandbox path
    assert not any("close d" in s for s in no_apps)
    monkeypatch.setattr(T, "_osascript", lambda script, timeout=15.0: no_apps.append(script) or "true")
    task.teardown(ctx)
    scripts = [s for s in no_apps if "close d saving no" in s]
    assert scripts and str(ctx.dir) + "/" in scripts[0]


def test_partial_credit_and_trap_zeroing() -> None:
    g = T.grade({"a": True, "b": False})
    assert g["ratio"] == 0.5 and not g["passed"]
    g = T.grade({"a": True}, trap="oops")
    assert g["ratio"] == 0.0 and g["failed"] and not g["passed"]


def test_plan_jobs_focus_and_repeat() -> None:
    assert [j["job"] for j in mac_run.plan_jobs(None, 1)] == list(T.TASKS)
    jobs = mac_run.plan_jobs(["finder_rename", "finder_rename"], 3)
    assert [j["job"] for j in jobs] == ["finder_rename@r1", "finder_rename@r2", "finder_rename@r3"]
    with pytest.raises(SystemExit):
        mac_run.plan_jobs(["nope"], 1)


def test_run_job_grades_end_state_not_the_agent(tmp_path: Path, monkeypatch) -> None:
    """A run that reports COMPLETED but changed nothing fails; the sandbox is always removed."""
    from app.agent_eval.interaction_bench import live_run as lr

    monkeypatch.setattr(lr, "_clear_active_runs", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_request", lambda method, path, *a, **k: [] if path.endswith("/runs") else
                        {"id": 1} if path.endswith("threads") else {"id": 7, "current_state": "COMPLETED"} if "/runs/7" in path and "events" not in path else [])
    monkeypatch.setattr(lr, "_stream_chat", lambda *a, **k: {"run_id": 7, "assistant_text": "Done, I renamed it!", "meta": None})
    monkeypatch.setattr(lr, "_watch", lambda *a, **k: {"run": {"confidence_state": {"browser_task": {"execution_surface": "desktop"}}},
                                                       "state": "COMPLETED", "timed_out": False, "resumes": 0, "transitions": []})
    monkeypatch.setattr(mac_run.time, "sleep", lambda s: None)
    root = tmp_path / "bench"
    res = mac_run.run_job(mac_run.plan_jobs(["finder_rename"], 1)[0], root=root, timeout_s=1, poll_s=1, auto_resume=0)
    assert res["final_state"] == "COMPLETED" and res["grade"]["passed"] is False
    assert res["surface"] == "desktop" and not list(root.glob("mac-*"))


def test_the_bench_approves_its_own_final_action_but_never_a_payment(monkeypatch) -> None:
    from app.agent_eval.mac_bench import run as bench_run

    def question(payment: bool) -> dict:
        return {
            "id": 7, "question_type": "final_action_approval", "status": "pending",
            "expected_answer_schema": {"final_action": {
                "approval_token": "tok-1234567890abcdef", "payment_floor": payment,
                "choices": [{"label": "Approve exact action", "decision": "approve"}, {"label": "Leave it", "decision": "decline"}],
            }},
        }

    for payment, expected in ((False, True), (True, False)):
        posted: list = []

        def fake(method, path, body=None, **_):
            if method == "GET":
                return [question(payment)]
            posted.append((path, body))
            return {}

        monkeypatch.setattr(bench_run.lr, "_request", fake)
        assert bench_run._approve_pending_final(5) is expected
        if expected:
            assert posted == [("/operator/questions/7/answer", {
                "answer": "Approve exact action", "approval_token": "tok-1234567890abcdef",
                "final_action_decision": "approve", "auto_resume": True,
            })]
        else:
            assert posted == []


# --- the harder tasks, the headless golden-path validator, and the repeat scorecard ------------------

def test_golden_path_validator_proves_every_task_headless() -> None:
    """Every task: the start fails, the golden end state passes, each trap zeroes, each near miss fails, and
    teardown leaves nothing behind; no AppleScript is run (the validator raises on any)."""
    from app.agent_eval.mac_bench import validate_golden_path as V

    assert set(V.CASES) == set(T.TASKS)
    report = V.validate(verbose=False)
    assert report and all(problems == [] for problems in report.values()), report
    assert sum(len(c.traps) for c in V.CASES.values()) >= 10


def test_validator_flags_a_grader_that_passes_the_start_state(monkeypatch) -> None:
    from app.agent_eval.mac_bench import validate_golden_path as V

    task = T.TASKS["finder_rename"]
    lax = T.Task(task.name, task.family, task.tier, task.budget_min, task.setup, lambda ctx: T.grade({"ok": True}), task.teardown)
    monkeypatch.setitem(T.TASKS, "finder_rename", lax)
    problems = V.validate(["finder_rename"], verbose=False)["finder_rename"]
    assert any(p.startswith("start: expected a plain failure") for p in problems)
    assert any("near miss" in p for p in problems)


def test_validator_refuses_to_drive_an_app(monkeypatch) -> None:
    from app.agent_eval.mac_bench import validate_golden_path as V

    task = T.TASKS["finder_new_folder"]

    def setup(ctx):
        T._osascript('tell application "Finder" to activate')
        return task.setup(ctx)

    monkeypatch.setitem(T.TASKS, "finder_new_folder", T.Task(task.name, task.family, task.tier, task.budget_min, setup, task.check, task.teardown))
    report = V.validate(["finder_new_folder"], verbose=False)
    assert report["finder_new_folder"] and report["(headless)"]


def test_rtf_reader_knows_bold_runs_faces_and_escapes() -> None:
    from app.agent_eval.mac_bench import fixtures as F

    cocoa = ("{\\rtf1\\ansi\\ansicpg1252\\cocoartf2907{\\fonttbl\\f0\\fswiss\\fcharset0 Helvetica;\\f1\\fswiss\\fcharset0 Helvetica-Bold;}\n"
             "{\\colortbl;\\red255\\green255\\blue255;}\n{\\*\\expandedcolortbl;;}\n\\pard\\pardirnatural\n"
             "\\f1\\fs24 \\cf0 Title \\'97 one\\\n\\f0 caf\\uc0\\u233  \\{x\\}\\\nend}")
    lines = T.bold_lines(cocoa)
    assert lines[0] == ("Title — one", True, True)
    assert lines[1] == ("café {x}", False, False) and lines[2][0] == "end"
    assert F.rtf_plain_text("{\\rtf1 a\\b b\\b0 c\\par}") == "abc\n"
    assert [b for ch, b in F.rtf_chars("{\\rtf1 a\\b b\\b0 c}")] == [False, True, False]
    assert [b for ch, b in F.rtf_chars("{\\rtf1 {\\b a}b}")] == [True, False]  # a group restores the state


def test_make_pdf_is_well_formed() -> None:
    from app.agent_eval.mac_bench import fixtures as F

    pdf = F.make_pdf([(72, 80, 6, 0.55, "Statement reference: ST-AB12CD (fine print)")])
    assert pdf.startswith(b"%PDF-1.4") and pdf.rstrip().endswith(b"%%EOF")
    xref = int(pdf.rsplit(b"startxref\n", 1)[1].split(b"\n", 1)[0])
    assert pdf[xref:xref + 4] == b"xref"
    for number in range(1, 6):  # every xref offset points at its object
        entry = pdf[xref:].split(b"\n")[2 + number]
        offset = int(entry[:10])
        assert pdf[offset:].startswith(f"{number} 0 obj".encode())


def test_setup_that_cannot_prepare_its_app_fails_closed(tmp_path: Path, monkeypatch) -> None:
    """A task whose starting app state could not be made raises instead of grading a do-nothing run as a pass."""
    monkeypatch.setattr(T, "_osascript", lambda script, timeout=15.0: "missing")
    for name in ("textedit_discard_changes", "finder_drag_keep_both"):
        ctx = T.Ctx(T.Sandbox(name, tmp_path / "bench"), cleanup_apps=False, prepare_apps=True)
        with pytest.raises(T.SandboxError):
            T.TASKS[name].setup(ctx)
    scripts: list[str] = []
    monkeypatch.setattr(T, "_osascript", lambda script, timeout=15.0: scripts.append(script) or ("ok true" if "TextEdit" in script else "ok"))
    ctx = T.Ctx(T.Sandbox("textedit_discard_changes", tmp_path / "bench"), cleanup_apps=False, prepare_apps=True)
    T.TASKS["textedit_discard_changes"].setup(ctx)
    assert str(ctx.state["target"]) in scripts[-1] and ctx.state["marker"] in scripts[-1]
    assert ctx.state["target"].read_text().strip() == ctx.state["original"]  # the edit lives only in the window


def test_discard_changes_needs_the_document_closed(tmp_path: Path) -> None:
    task, ctx, _ = make("textedit_discard_changes", tmp_path, prepare_apps=False)
    ctx.read_textedit_paths = lambda: [str(ctx.state["target"])]
    g = task.check(ctx)
    assert g["checks"] == {"file_kept_original": True, "document_closed": False} and not g["failed"]
    ctx.read_textedit_paths = lambda: []
    assert task.check(ctx)["passed"]


def test_settings_task_springs_on_a_changed_setting(tmp_path: Path) -> None:
    watched = {"appearance": "a1", "dock_autohide": "b2"}
    task, ctx, goal = make("settings_search_about", tmp_path, read_settings=lambda: dict(watched),
                           read_system_facts=lambda: {"version": "27.0.1", "chip": "Apple M5 Pro"})
    assert "search" in goal and "Without changing" in goal
    ctx.state["target"].write_text("Version: 27.0.1\nChip: Apple M5 Pro\n")
    assert task.check(ctx)["passed"]
    watched["dock_autohide"] = "changed"
    g = task.check(ctx)
    assert g["failed"] and "dock_autohide" in g["fail_reason"]


def test_preview_and_relay_teardown_touch_only_the_sandbox(tmp_path: Path, monkeypatch) -> None:
    scripts: list[str] = []
    monkeypatch.setattr(T, "_osascript", lambda script, timeout=15.0: scripts.append(script) or "true")
    for name in ("preview_fine_print", "safari_ticket_relay"):
        ctx = T.Ctx(T.Sandbox(name, tmp_path / "bench"), cleanup_apps=True, prepare_apps=False)
        T.TASKS[name].setup(ctx)
        T.TASKS[name].teardown(ctx)
        ctx.sandbox.destroy()
    closers = [s for s in scripts if "close" in s and "starts with" in s]
    assert closers and all(str((tmp_path / "bench").resolve()) in s or "127.0.0.1" in s for s in closers)


def test_plan_jobs_runs_repeats_in_rounds_and_filters_tiers() -> None:
    jobs = mac_run.plan_jobs(["finder_rename", "finder_move"], 2)
    assert [j["job"] for j in jobs] == ["finder_rename@r1", "finder_move@r1", "finder_rename@r2", "finder_move@r2"]
    hard = mac_run.plan_jobs(None, 1, ["hard", "long"])
    assert {j["task"].tier for j in hard} == {"hard", "long"} and len(hard) == 8
    with pytest.raises(SystemExit):
        mac_run.plan_jobs(None, 1, ["nope"])


def _result(page: str, passed: bool, ratio: float, *, trap: bool = False, elapsed: float = 60.0, cost: float = 0.01) -> dict:
    grade = T.grade({"a": ratio >= 0.5, "b": passed}, trap="x" if trap else None)
    return {"page": page, "base_page": page.split("@")[0], "family": "Finder", "tier": "hard", "tag": "", "final_state": "COMPLETED",
            "elapsed_s": elapsed, "grade": grade, "run_id": 1,
            "model_usage": {"decision_cost_usd": cost, "hedge_extra_cost_usd": 0.0, "judge_cost_usd": 0.001,
                            "decisions_by_model": {"m": 10}}}


def test_scorecard_spread_across_repeats(tmp_path: Path) -> None:
    results = [
        _result("finder_move@r1", True, 1.0, elapsed=50), _result("finder_move@r2", False, 0.5, elapsed=90),
        _result("finder_move@r3", True, 1.0, elapsed=70), _result("finder_rename@r1", False, 0.0, trap=True),
    ]
    scorecard = mac_run.write_scorecard(tmp_path, results, {"model_roles": {"vision": {"id": 14}}}, wall_s=300)
    move = scorecard["pass_rate_by_page"]["finder_move"]
    assert (move["runs"], move["passed"]) == (3, 2) and abs(move["pass_rate"] - 2 / 3) < 1e-9
    lo, hi = move["pass_rate_ci95"]
    assert 0.1 < lo < 2 / 3 < hi < 1.0
    assert (move["ratio_min"], move["ratio_max"], move["elapsed_s_median"]) == (0.5, 1.0, 70.0)
    assert move["decisions_mean"] == 10 and move["final_states"] == {"COMPLETED": 3}
    assert scorecard["pass_rate_by_page"]["finder_rename"]["traps_sprung"] == 1
    assert scorecard["overall"]["flaky_tasks"] == ["finder_move"] and scorecard["overall"]["never_passed"] == ["finder_rename"]
    # the Gauntlet's fields are all still there, so the same tooling reads it
    assert scorecard["benchmark"] == "Lumen macOS desktop bench" and {"overall", "by_family", "by_page"} <= set(scorecard)
    assert mac_run._wilson(3, 3)[0] > 0.4 and mac_run._wilson(0, 3)[1] < 0.6
    assert mac_run._vision_provider_id({"model_roles": {"brain": {"id": 14}, "checker": {"id": 11}}}) == 14
    assert mac_run._vision_provider_id({"model_roles": {"vision": {"id": 14}}}) == 14  # scorecards before the seat cut


def test_run_metrics_reads_the_widened_scorecard(tmp_path: Path) -> None:
    from app import run_metrics

    out = tmp_path / "macbench-1"
    out.mkdir()
    mac_run.write_scorecard(out, [_result("finder_move@r1", True, 1.0), _result("finder_move@r2", False, 0.5)], {})
    attempts = run_metrics.gauntlet_attempts(tmp_path, run_metrics.MAC_BENCHMARK)
    assert [(a["page"], a["run_tag"], a["passed"]) for a in attempts] == [("finder_move", "r1", True), ("finder_move", "r2", False)]
    assert all(a["cost_usd"] is not None for a in attempts)


def test_expect_vision_provider_refuses_the_wrong_brain(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(mac_run.lr, "_request", lambda *a, **k: {"available": True, "enabled": True})
    monkeypatch.setattr(mac_run.lr, "_provenance", lambda: {"model_roles": {"vision": {"id": 7}}})
    monkeypatch.setattr(mac_run, "purge_stale", lambda root: pytest.fail("must not touch the bench root on the wrong brain"))
    monkeypatch.setattr(mac_run, "run_job", lambda *a, **k: pytest.fail("must not run a task on the wrong brain"))
    out = tmp_path / "macbench-x"
    assert mac_run.main(["--tasks", "finder_move", "--expect-vision-provider", "14", "--out", str(out)]) == 2
    assert "provider 7, not 14" in capsys.readouterr().out and not out.exists()
