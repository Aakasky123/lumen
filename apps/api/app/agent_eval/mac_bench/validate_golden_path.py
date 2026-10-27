"""Golden-path validator for the Mac bench tasks.

This does NOT test the agent. It proves the benchmark itself, headless (no app is driven, no window opens, the
screen and keyboard are never touched), for every task:

- the starting state fails without a trap (a run that does nothing cannot pass);
- the intended end state passes with every check (the golden path, written straight to the sandbox the way the
  app would leave it: a Cocoa RTF from textutil, a renamed file, a Safari user agent on the loopback page);
- every trap on top of an otherwise right end state zeroes the run (failed, ratio 0);
- every near miss (a copy instead of a move, rich text instead of plain, a look-alike value) fails without a trap;
- setup keeps everything it makes inside the task's sandbox folder, and teardown removes that folder, stops any
  loopback server, and leaves the bench root and its neighbours exactly as they were.

App-state readers (Calculator's display, TextEdit's open documents, system facts and settings) are replaced by
stubs, and any AppleScript call fails the validation. Run it after any task edit:

    python -m app.agent_eval.mac_bench.validate_golden_path [--tasks a,b] [--rounds N]
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.agent_eval.mac_bench import fixtures as F
from app.agent_eval.mac_bench import tasks as T

SAFARI_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/19.0 Safari/605.1.15"
CHROME_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
STUB_FACTS = {"version": "27.0.1", "chip": "Apple M5 Pro"}

Mutation = Callable[[T.Ctx], None]


class AppDriven(AssertionError):
    pass


@dataclass
class Case:
    golden: Mutation
    traps: dict[str, Mutation] = field(default_factory=dict)  # each applies the golden path itself, then the mistake
    misses: dict[str, Mutation] = field(default_factory=dict)  # must fail, without a trap
    goldens: dict[str, Mutation] = field(default_factory=dict)  # further end states that must pass
    fixture_check: Callable[[T.Ctx], list[str]] | None = None  # extra proof the starting state is what the goal says


# --- small mutations ------------------------------------------------------------------------------

def _get(url: str, ua: str) -> str:
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": ua}), timeout=5).read().decode()


def _post_code(base: str, code: str, ua: str) -> None:
    data = urllib.parse.urlencode({"code": code}).encode()
    urllib.request.urlopen(urllib.request.Request(base + "/confirm", data=data, headers={"User-Agent": ua}), timeout=5).read()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _rtf(text: str) -> str:
    return "{\\rtf1\\ansi{\\fonttbl\\f0\\fswiss Helvetica;}\\f0\\fs24 " + text + "\\par}"


def _seq(*steps: Mutation) -> Mutation:
    def run(ctx: T.Ctx) -> None:
        for step in steps:
            step(ctx)
    return run


# --- golden paths, traps, near misses per task ------------------------------------------------------

def _textedit_new(ctx: T.Ctx) -> None:
    _write(ctx.state["target"], ctx.state["text"] + "\n")


def _textedit_edit(ctx: T.Ctx) -> None:
    _write(ctx.state["target"], ctx.state["expected"] + "\n")


def _mkdir(ctx: T.Ctx) -> None:
    (ctx.dir / ctx.state["name"]).mkdir()


def _rename(ctx: T.Ctx) -> None:
    ctx.state["old"].rename(ctx.state["new"])


def _move(ctx: T.Ctx) -> None:
    ctx.state["src"].rename(ctx.state["dest"] / ctx.state["src"].name)


def _sort(ctx: T.Ctx) -> None:
    ctx.state["folder"].mkdir()
    for p in ctx.state["csvs"]:
        p.rename(ctx.state["folder"] / p.name)


def _calculator(ctx: T.Ctx) -> None:
    value = int(ctx.state["expected"])
    ctx.read_ax_values = lambda: ["‎" + f"{value:,}"]


def _safari(ctx: T.Ctx) -> None:
    server = ctx.state["server"]
    page = _get(server.url, SAFARI_UA)
    assert ctx.state["code"] in page
    _post_code(server.base, ctx.state["code"], SAFARI_UA)


def _bold(ctx: T.Ctx) -> None:
    _write(ctx.state["target"], F.make_rtf(ctx.state["lines"], bold_first=True))


def _no_replace(ctx: T.Ctx) -> None:
    _write(ctx.state["target"], ctx.state["text"] + "\n")


def _discard(ctx: T.Ctx) -> None:
    ctx.read_textedit_paths = lambda: []  # the document was closed and the file never written


def _drag(ctx: T.Ctx) -> None:
    incoming, shared = ctx.state["incoming"], ctx.state["shared"]
    for p in list(incoming.iterdir()):
        if p.name == "notes.txt":
            p.rename(shared / "notes 2.txt")  # Finder's Keep Both
        elif p.name != ".DS_Store":
            p.rename(shared / p.name)


def _relay(ctx: T.Ctx) -> None:
    page = _get(ctx.state["server"].url, SAFARI_UA)
    assert ctx.state["code"] in page and ctx.state["decoy"] in page
    _write(ctx.state["target"], ctx.state["code"] + "\n")


def _preview(ctx: T.Ctx) -> None:
    ctx.state["pdf"].rename(ctx.state["target"])


def _settings(ctx: T.Ctx) -> None:
    facts = ctx.state["facts"]
    _write(ctx.state["target"], f"Version: {facts['version']}\nChip: {facts['chip']}\n")


def _project(ctx: T.Ctx) -> None:
    atlas = ctx.state["atlas"]
    (atlas / "docs").mkdir(parents=True)
    (atlas / "data").mkdir()
    ctx.state["brief"].rename(atlas / "docs" / "atlas-brief.txt")
    for p in ctx.state["csvs"]:
        p.rename(atlas / "data" / p.name)
    _write(atlas / "README.txt", ctx.state["readme"] + "\n")


def _bold_fixture(ctx: T.Ctx) -> list[str]:
    content = ctx.state["target"].read_text(encoding="utf-8")
    lines = [line for line in T.bold_lines(content) if line[0].strip()]
    problems = []
    if [t for t, _, _ in lines] != ctx.state["lines"]:
        problems.append(f"the starting RTF does not read back as the task's lines: {[t for t, _, _ in lines]}")
    if any(any_bold for _, _, any_bold in lines):
        problems.append("the starting RTF already has bold text")
    return problems


def _preview_fixture(ctx: T.Ctx) -> list[str]:
    text = F.pdf_text(str(ctx.state["pdf"]))
    if text is None:
        return [] if sys.platform != "darwin" else ["PDFKit could not open the generated statement.pdf"]
    missing = [v for v in (ctx.state["ref"], ctx.state["decoy"]) if v not in text]
    return [f"PDFKit does not find {missing} in statement.pdf"] if missing else []


def _relay_fixture(ctx: T.Ctx) -> list[str]:
    html = ctx.state["server"].html
    small = html.split("font-size:10px", 1)
    return [] if len(small) == 2 and ctx.state["code"] in small[1] and ctx.state["code"] not in small[0] else [
        "the active reference is not (only) in the small grey print"]


def _set_setting(ctx: T.Ctx) -> None:
    ctx.state["stub_settings"]["appearance"] = "flipped"


CASES: dict[str, Case] = {
    "textedit_new_doc": Case(
        _textedit_new,
        misses={
            "saved as rich text": lambda c: _write(c.state["target"], _rtf(c.state["text"])),
            "other text": lambda c: _write(c.state["target"], "something else\n"),
        },
        goldens={"CRLF line ending": lambda c: _write(c.state["target"], c.state["text"] + "\r\n")},
    ),
    "textedit_edit_file": Case(
        _textedit_edit,
        misses={
            "collateral edit": lambda c: _write(c.state["target"], c.state["expected"].replace("Operations", "Ops")),
            "saved as rich text": lambda c: _write(c.state["target"], _rtf(c.state["expected"])),
        },
    ),
    "finder_new_folder": Case(
        _mkdir,
        misses={"stray file too": _seq(_mkdir, lambda c: _write(c.dir / "stray.txt", "x"))},
        goldens={"Finder's .DS_Store inside": _seq(_mkdir, lambda c: _write(c.dir / c.state["name"] / ".DS_Store", "x"))},
    ),
    "finder_rename": Case(
        _rename,
        traps={"other file removed": _seq(_rename, lambda c: c.state["other"].unlink())},
        misses={"copied, not renamed": lambda c: shutil.copy(c.state["old"], c.state["new"])},
    ),
    "finder_move": Case(
        _move,
        traps={"other file changed": _seq(_move, lambda c: _write(c.state["keep"], "changed"))},
        misses={"copied, not moved": lambda c: shutil.copy(c.state["src"], c.state["dest"] / c.state["src"].name)},
    ),
    "finder_sort_by_type": Case(
        _sort,
        traps={"a .txt moved too": _seq(_sort, lambda c: c.state["txts"][0].rename(c.state["folder"] / c.state["txts"][0].name))},
        misses={"csv copied, not moved": lambda c: (c.state["folder"].mkdir(), [shutil.copy(p, c.state["folder"] / p.name) for p in c.state["csvs"]])},
    ),
    "calculator_product": Case(
        _calculator,
        misses={
            "wrong product": lambda c: setattr(c, "read_ax_values", lambda: [str(int(c.state["expected"]) + 1)]),
            "display unreadable": lambda c: setattr(c, "read_ax_values", lambda: []),
        },
    ),
    "safari_confirm_code": Case(
        _safari,
        traps={"invented code": lambda c: (_get(c.state["server"].url, SAFARI_UA), _post_code(c.state["server"].base, "KESTREL-0000", SAFARI_UA))},
        misses={"another browser": lambda c: (_get(c.state["server"].url, CHROME_UA), _post_code(c.state["server"].base, c.state["code"], CHROME_UA))},
    ),
    "textedit_bold_heading": Case(
        _bold,
        misses={
            "whole document bold": lambda c: _write(c.state["target"], F.make_rtf(c.state["lines"], bold_first=False).replace("\\f0", "\\f0\\b")),
            "saved as plain text": lambda c: _write(c.state["target"], "\n".join(c.state["lines"]) + "\n"),
            "title edited": lambda c: _write(c.state["target"], F.make_rtf([c.state["lines"][0] + "!", *c.state["lines"][1:]], bold_first=True)),
            "nothing bold": lambda c: _write(c.state["target"], F.make_rtf(c.state["lines"])),
            "a stray copy saved beside it": _seq(_bold, lambda c: _write(c.dir / "summary copy.rtf", "x")),
        },
        goldens={
            "hand-written \\b RTF": lambda c: _write(c.state["target"], F.simple_rtf(c.state["lines"], bold_first=True)),
            "bold only through the font face": lambda c: _write(c.state["target"], (
                "{\\rtf1\\ansi{\\fonttbl\\f0\\fswiss Helvetica;\\f1\\fswiss Helvetica-Bold;}\\f1 " + c.state["lines"][0]
                + "\\\n\\f0 " + "\\\n".join(c.state["lines"][1:]) + "}")),
        },
        fixture_check=_bold_fixture,
    ),
    "textedit_save_no_replace": Case(
        _no_replace,
        traps={"replaced the existing notes.txt": _seq(_no_replace, lambda c: _write(c.state["existing"], c.state["text"] + "\n"))},
        misses={
            "saved as rich text": lambda c: _write(c.state["target"], _rtf(c.state["text"])),
            "saved in the wrong folder": lambda c: _write(c.dir / "notes-2.txt", c.state["text"] + "\n"),
        },
    ),
    "textedit_discard_changes": Case(
        _discard,
        traps={"saved the mistaken edit": _seq(_discard, lambda c: _write(
            c.state["target"], c.state["original"] + f"\n{c.state['marker']}: delete every resolution above\n"))},
        misses={
            "left the document open": lambda c: None,
            "open documents unreadable": lambda c: setattr(c, "read_textedit_paths", lambda: None),
            "emptied the file": _seq(_discard, lambda c: _write(c.state["target"], "")),
        },
    ),
    "finder_drag_keep_both": Case(
        _drag,
        traps={"Replace on the name clash": lambda c: [
            p.replace(c.state["shared"] / p.name) for p in list(c.state["incoming"].iterdir())]},
        misses={
            "copied (Option-drag)": lambda c: [shutil.copy(p, c.state["shared"] / (p.name if p.name != "notes.txt" else "notes 2.txt"))
                                              for p in list(c.state["incoming"].iterdir())],
            "Stop on the name clash": lambda c: [p.rename(c.state["shared"] / p.name)
                                                 for p in list(c.state["incoming"].iterdir()) if p.name != "notes.txt"],
            "dropped into the wrong place": lambda c: [p.rename(c.dir / p.name) for p in list(c.state["incoming"].iterdir())],
        },
        goldens={"Keep Both then a tidy rename": _seq(_drag, lambda c: (c.state["shared"] / "notes 2.txt").rename(c.state["shared"] / "notes-incoming.txt"))},
    ),
    "safari_ticket_relay": Case(
        _relay,
        traps={"closed ticket's reference": lambda c: (_get(c.state["server"].url, SAFARI_UA), _write(c.state["target"], c.state["decoy"] + "\n"))},
        misses={
            "another browser": lambda c: (_get(c.state["server"].url, CHROME_UA), _write(c.state["target"], c.state["code"] + "\n")),
            "saved as rich text": lambda c: (_get(c.state["server"].url, SAFARI_UA), _write(c.state["target"], _rtf(c.state["code"]))),
            "extra words": lambda c: (_get(c.state["server"].url, SAFARI_UA), _write(c.state["target"], f"Reference: {c.state['code']}\n")),
            "misread one character": lambda c: (_get(c.state["server"].url, SAFARI_UA), _write(c.state["target"], T._look_alike(c.state["code"], 7) + "\n")),
        },
        fixture_check=_relay_fixture,
    ),
    "preview_fine_print": Case(
        _preview,
        traps={"previous statement's reference": lambda c: c.state["pdf"].rename(c.state["wrong"])},
        misses={
            "duplicated, not renamed": lambda c: shutil.copy(c.state["pdf"], c.state["target"]),
            "Finder appended .pdf twice": lambda c: c.state["pdf"].rename(c.dir / (c.state["target"].name + ".pdf")),
        },
        fixture_check=_preview_fixture,
    ),
    "settings_search_about": Case(
        _settings,
        traps={"changed a setting": _seq(_settings, _set_setting)},
        misses={
            "version cut short": lambda c: _write(c.state["target"], f"Version: {c.state['facts']['version'].rsplit('.', 1)[0]}\nChip: {c.state['facts']['chip']}\n"),
            "chip missing": lambda c: _write(c.state["target"], f"Version: {c.state['facts']['version']}\n"),
            "saved as rich text": lambda c: _write(c.state["target"], _rtf(f"Version: {c.state['facts']['version']}\\par Chip: {c.state['facts']['chip']}")),
        },
        goldens={"build number shown too": lambda c: _write(
            c.state["target"], f"Version: {c.state['facts']['version']} (26A434)\nChip:  {c.state['facts']['chip']}\n")},
    ),
    "finder_project_setup": Case(
        _project,
        traps={"look-alike brief-old.txt moved": _seq(_project, lambda c: c.state["old"].rename(c.state["atlas"] / "docs" / "brief-old.txt"))},
        misses={
            "brief copied, not moved": _seq(_project, lambda c: _write(c.state["brief"], (c.state["atlas"] / "docs" / "atlas-brief.txt").read_text())),
            "README as rich text": _seq(_project, lambda c: _write(c.state["atlas"] / "README.txt", _rtf(c.state["readme"]))),
            "an extra folder": _seq(_project, lambda c: (c.state["atlas"] / "untitled folder").mkdir()),
            "stopped after step 3": lambda c: ((c.state["atlas"] / "docs").mkdir(parents=True), (c.state["atlas"] / "data").mkdir(),
                                               c.state["brief"].rename(c.state["atlas"] / "docs" / "atlas-brief.txt")),
        },
    ),
}


# --- the runner -------------------------------------------------------------------------------------

def _paths_in(value: Any) -> list[Path]:
    if isinstance(value, Path):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [p for item in value for p in _paths_in(item)]
    if isinstance(value, dict):
        return [p for item in value.values() for p in _paths_in(item)]
    return []


def _snapshot(path: Path) -> dict[str, str]:
    return {p.relative_to(path).as_posix(): ("dir" if p.is_dir() else T.sha256(p)) for p in sorted(path.rglob("*"))}


def _server_down(server: Any) -> bool:
    try:
        urllib.request.urlopen(server.base + "/", timeout=2)
    except OSError:
        return True
    return False


def _new_ctx(name: str, root: Path) -> T.Ctx:
    ctx = T.Ctx(T.Sandbox(name, root), cleanup_apps=False, prepare_apps=False)
    ctx.read_ax_values = lambda: []
    ctx.read_textedit_paths = lambda: [str(ctx.dir / "minutes.txt")]  # the setup's document is still open
    ctx.read_system_facts = lambda: dict(STUB_FACTS)
    ctx.read_settings = lambda: dict(ctx.state.setdefault("stub_settings", {"appearance": "light", "dock_autohide": "0"}))
    return ctx


def _scenario(name: str, root: Path, label: str, mutate: Mutation | None, expect: str) -> list[str]:
    """Set up a fresh sandbox, apply ``mutate``, grade, tear down; ``expect`` is pass, trap, or fail."""
    task = T.TASKS[name]
    ctx = _new_ctx(name, root)
    problems: list[str] = []
    try:
        goal = task.setup(ctx)
        if label == "start":
            paths = _paths_in(ctx.state)
            outside = [str(p) for p in paths if not p.resolve().is_relative_to(ctx.dir.resolve())]
            if outside:
                problems.append(f"setup keeps paths outside its sandbox: {outside}")
            if paths and str(ctx.dir) not in goal:  # a task that works on files names where they are
                problems.append("the goal does not name the sandbox folder")
            check = CASES[name].fixture_check
            if check is not None:
                problems.extend(check(ctx))
        if mutate is not None:
            mutate(ctx)
        g = task.check(ctx)
        if expect == "pass" and not (g["passed"] and g["ratio"] == 1.0 and not g["failed"]):
            problems.append(f"{label}: expected a pass, got {g}")
        elif expect == "trap" and not (g["failed"] and not g["passed"] and g["ratio"] == 0.0 and g["fail_reason"]):
            problems.append(f"{label}: expected a sprung trap, got {g}")
        elif expect == "fail" and (g["passed"] or g["failed"]):
            problems.append(f"{label}: expected a plain failure (no pass, no trap), got {g}")
    except Exception as exc:  # noqa: BLE001 - report, keep validating the rest
        problems.append(f"{label}: {exc.__class__.__name__}: {exc}")
    finally:
        server = ctx.state.get("server")
        try:
            task.teardown(ctx)
            ctx.sandbox.destroy()
        except Exception as exc:  # noqa: BLE001
            problems.append(f"{label}: teardown failed: {exc}")
        if ctx.dir.exists():
            problems.append(f"{label}: teardown left {ctx.dir}")
        if server is not None and not _server_down(server):
            problems.append(f"{label}: teardown left the loopback server running")
    return problems


def validate_task(name: str, root: Path) -> tuple[list[str], int]:
    """All scenarios for one task: (problems, scenarios run)."""
    case = CASES[name]
    scenarios: list[tuple[str, Mutation | None, str]] = [("start", None, "fail"), ("golden", case.golden, "pass")]
    scenarios += [(f"golden variant '{k}'", m, "pass") for k, m in case.goldens.items()]
    scenarios += [(f"trap '{k}'", m, "trap") for k, m in case.traps.items()]
    scenarios += [(f"near miss '{k}'", m, "fail") for k, m in case.misses.items()]
    problems: list[str] = []
    for label, mutate, expect in scenarios:
        problems.extend(_scenario(name, root, label, mutate, expect))
    return problems, len(scenarios)


def validate(names: list[str] | None = None, rounds: int = 1, *, verbose: bool = True) -> dict[str, list[str]]:
    """Validate the named tasks (default: all) ``rounds`` times (codes and names are random per setup).
    Returns task -> problems; empty lists mean the task is proven."""
    names = names or list(T.TASKS)
    missing = [n for n in T.TASKS if n not in CASES]
    report: dict[str, list[str]] = {}
    if missing:
        report["(registry)"] = [f"no golden path for {missing}"]
    driven: list[str] = []
    real_osascript = T._osascript

    def refuse(script: str, timeout: float = 15.0) -> str | None:
        driven.append(script.splitlines()[0] if script else "")
        raise AppDriven("the validator must not drive an app: " + script[:80])

    T._osascript = refuse
    work = Path(tempfile.mkdtemp(prefix="lumen-macbench-validate-"))
    try:
        root, neighbour = work / "bench", work / "neighbour"
        root.mkdir()
        neighbour.mkdir()
        (neighbour / "user-file.txt").write_text("not the bench's\n", encoding="utf-8")
        before = _snapshot(work)
        for name in names:
            problems: list[str] = []
            count = 0
            for _ in range(max(1, rounds)):
                found, count = validate_task(name, root)
                problems.extend(p for p in found if p not in problems)
            report[name] = problems
            if verbose:
                print(f"{'OK  ' if not problems else 'FAIL'} {name:26s} {count} scenarios x{max(1, rounds)}", flush=True)
                for p in problems:
                    print(f"       - {p}", flush=True)
        if _snapshot(work) != before:
            report.setdefault("(sandbox)", []).append("the bench root or its neighbour changed after every teardown")
        if driven:
            report.setdefault("(headless)", []).append(f"{len(driven)} AppleScript call(s) attempted: {driven[:3]}")
    finally:
        T._osascript = real_osascript
        shutil.rmtree(work, ignore_errors=True)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prove the Mac bench's setup, graders, traps and teardown without the agent.")
    parser.add_argument("--tasks", help="comma-separated task names (default: all)")
    parser.add_argument("--rounds", type=int, default=1, help="validate each task this many times (fresh random values)")
    args = parser.parse_args(argv)
    names = [n.strip() for n in (args.tasks or "").split(",") if n.strip()] or None
    unknown = [n for n in names or [] if n not in T.TASKS]
    if unknown:
        parser.error(f"unknown task {unknown[0]!r}")
    report = validate(names, args.rounds)
    bad = {k: v for k, v in report.items() if v}
    print(f"{'all' if not bad else len(report) - len(bad)} of {len(report)} validated"
          + (f"; problems in {', '.join(bad)}" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
