"""Interaction Gauntlet generation 4 — the human-input atlas (judging logic; stdlib only).

Generations 1-3 test hard situations. Generation 4 tests coverage: every input primitive a person uses
on a computer, one challenge each, so a failure names the missing skill instead of a page. Pointer
(click variants, hold, hover, repeat), keyboard (focus traversal, arrows, chords, Esc/Enter, F2,
held keys, undo), text editing, selection and the clipboard, drag and drop in all its forms,
scrolling and zooming, the browser's form controls and its own UI (tabs, address bar, history), and
on the desktop: window management (move, resize, maximize, minimize, snap, close, switch) and native
app controls (cascade and context menus, double-click, cross-window drag, wheel, text selection,
accelerators, the Open dialog).

Same contract as generation 3: the harness (``v3_harness.py``) judges, every value is randomized per
session, the pass token is revealed only when every challenge is complete, and nothing in page source
answers anything. Pages ignore script-dispatched (untrusted) input. Challenges on a page are
independent — any order — so partial credit maps one-to-one onto primitives. Traps are rare here and
only where a slip is genuinely destructive (a Delete in a context menu, Remove crate).

Locking: as in v3_missions, every Mission method runs with ``LOCK`` held by its caller.
"""

from __future__ import annotations

import math
import time
import unicodedata
from datetime import date, timedelta
from pathlib import Path
from typing import Any

try:  # package import inside the repo; flat import in the standalone bundle
    from .v3_missions import RNG, Mission, norm, rand_code, rand_digits, sha, squash
except ImportError:  # pragma: no cover - standalone bundle
    from v3_missions import RNG, Mission, norm, rand_code, rand_digits, sha, squash  # type: ignore

_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"


def _lines(text: Any) -> list[str]:
    """A text block as lines: newline style, a BOM, trailing spaces, and outer blank lines do not count."""
    raw = unicodedata.normalize("NFC", str(text or "")).lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in raw.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    return lines


def _as_int(value: Any, default: int = -10**9) -> int:
    try:
        return int(round(float(str(value).strip())))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = float("nan")) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


class Gen4(Mission):
    generation = 4
    focusable = True  # every generation-4 challenge is independent


# =============================================================================================
# 1. Clicks — every way a pointer presses something
# =============================================================================================

_SENDERS = (
    "Amara Okafor", "Bastien Leroux", "Chiara Benedetti", "Dmitri Volkov", "Elif Kaya", "Farah Haddad",
    "Gustavo Ruiz", "Hana Takeda", "Ines Moreau", "Jonas Lindqvist",
)


class Clicks(Gen4):
    name = "v4_clicks"
    slug = "v4-clicks"
    token = "BENCH_PASS v4-clicks-2b7e19"
    family = "pointer"
    steps = ("grid_click", "double_open", "context_menu", "middle_click", "hold_press", "hover_reveal", "repeat_click")
    COLS = "ABCDEFGHJKLM"
    ROWS = 10

    def setup(self) -> None:
        numbers: list[str] = []
        while len(numbers) < 6:
            n = rand_digits(4)
            if n not in numbers:
                numbers.append(n)
        senders = RNG.sample(_SENDERS, 5)
        start = RNG.randint(2, 9)
        target = RNG.choice([n for n in range(2, 15) if abs(n - start) >= 3])
        low = RNG.choice([1.5, 2.0, 2.5])
        self.v = {
            "cell": f"{RNG.choice(self.COLS)}{RNG.randint(1, self.ROWS)}",
            "files": [f"report-{n}.pdf" for n in numbers],
            "file": f"report-{RNG.choice(numbers)}.pdf",
            "senders": senders,
            "sender": RNG.choice(senders),
            "hold_low": low,
            "hold_high": low + 1.0,
            "tier": f"T-{rand_code(4)}",
            "qty_start": start,
            "qty_target": target,
        }
        self.middle_at = 0.0
        self.revealed = False

    def public(self) -> dict[str, Any]:
        v = self.v
        return {k: v[k] for k in ("cell", "files", "file", "senders", "sender", "hold_low", "hold_high", "qty_start", "qty_target")}

    def act(self, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        s, v = self.s, self.v
        if kind == "grid_click":
            cell = norm(data.get("cell")).upper()
            s.log("page", kind, cell)
            if cell == v["cell"]:
                s.complete("grid_click")
                return s.say("grid", f"Cell {cell} pinned ✓")
            return s.futility("grid", f"That click landed on cell {cell or '?'} — the target is {v['cell']}.")
        if kind == "open_file":
            name = norm(data.get("name"))
            s.log("page", kind, name)
            if name == v["file"]:
                s.complete("double_open")
                return s.say("open", f"{name} opened ✓")
            return s.futility("open", f"Opened {name} — that is not {v['file']}.")
        if kind == "context_action":
            sender, action = norm(data.get("sender")), norm(data.get("action"))
            s.log("page", kind, f"{action}:{sender}")
            if action == "Delete":
                return s.fail("deleted a message from its context menu (the task was to archive one)")
            if action != "Archive":
                return s.futility("context", f"Chose {action or 'nothing'} — the task is Archive.")
            if sender == v["sender"]:
                s.complete("context_menu")
                return s.say("context", f"Message from {sender} archived ✓")
            return s.futility("context", f"Archived the message from {sender} — the task was the message from {v['sender']}.")
        if kind == "notes_left_click":
            s.log("page", kind)
            return s.futility("middle", "That link does not open with a left click here — open it in a new background tab with the middle mouse button.")
        if kind == "notes_middle":
            s.log("page", kind)
            self.middle_at = time.monotonic()
            return {"ok": True}
        if kind == "notes_opened":
            s.log("browser_window", kind)
            if "middle_click" in s.done:
                return {"ok": True}
            if self.middle_at and time.monotonic() - self.middle_at <= 20:
                s.complete("middle_click")
                return s.say("middle", "Release notes opened in a background tab ✓")
            return s.futility("middle", "Release notes opened, but not from a middle click on the link — middle-click the link on the mission page.")
        if kind == "hold":
            ms = _as_int(data.get("ms"), 0)
            s.log("page", kind, ms)
            low, high = v["hold_low"], v["hold_high"]
            if low * 1000 <= ms <= high * 1000:
                s.complete("hold_press")
                return s.say("hold", f"Armed — held {ms / 1000:.1f} s ✓")
            return s.futility("hold", f"Held {ms / 1000:.1f} s — release between {low:.1f} and {high:.1f} seconds.")
        if kind == "hover_tip":
            s.log("page", kind)
            self.revealed = True
            return {"ok": True, "code": v["tier"]}
        if kind == "tier_code":
            value = norm(data.get("value")).upper()
            s.log("page", kind, value)
            if self.revealed and value == v["tier"]:
                s.complete("hover_reveal")
                return s.say("tier", "Tier code accepted ✓")
            return s.futility("tier", "That is not the tier code from the tooltip.")
        if kind == "quantity":
            value = _as_int(data.get("value"))
            s.log("page", kind, value)
            if value == v["qty_target"]:
                s.complete("repeat_click")
                return s.say("qty", f"Quantity {value} confirmed ✓")
            return s.futility("qty", f"Quantity {value} confirmed — the order needs exactly {v['qty_target']}.")
        return super().act(kind, data)


# =============================================================================================
# 2. Keys — the keyboard beyond typing
# =============================================================================================

_FIELD_LABELS = ("Full name", "Department", "Desk", "Badge ID", "Extension", "Building", "Locker")
_COMMANDS = ("Mark as reviewed", "Archive thread", "Snooze", "Assign to me", "Add label", "Print preview")
_CHORD_KEYS = ("E", "U", "K", "L", "Y", "H")  # Ctrl+Shift+<key>: none of these is a Chrome shortcut
_UNDO_ITEMS = ("Q3 budget.xlsx", "Vendor list.csv", "Kickoff notes.docx", "Floor plan.pdf", "Roster.xlsx", "Logo final.png")


class Keys(Gen4):
    name = "v4_keys"
    slug = "v4-keys"
    token = "BENCH_PASS v4-keys-8d41c6"
    family = "keyboard"
    steps = ("shift_tab", "arrow_grid", "chord", "escape_enter", "f2_rename", "key_hold", "undo_steps")
    GRID = "ABCDEFGHI"

    def setup(self) -> None:
        others = [label for label in _FIELD_LABELS if label != "Badge ID"]
        RNG.shuffle(others)
        badge_at = RNG.randint(0, 3)  # Start sits after the last field: Badge ID is 4-7 Shift+Tabs away
        fields = others[:badge_at] + ["Badge ID"] + others[badge_at:]
        while True:
            col, row = RNG.randrange(9), RNG.randrange(9)
            if abs(col - 4) + abs(row - 4) >= 4:
                break
        keys = list(_CHORD_KEYS)
        RNG.shuffle(keys)
        files = []
        while len(files) < 6:
            name = f"notes-{rand_code(4).lower()}.txt"
            if name not in files:
                files.append(name)
        low = RNG.choice([40, 50, 60])
        removed = RNG.sample(_UNDO_ITEMS, 3)
        self.v = {
            "fields": fields,
            "badge": f"BD-{rand_digits(5)}",
            "grid_target": f"{self.GRID[col]}{row + 1}",
            "chords": [[cmd, key] for cmd, key in zip(_COMMANDS, keys)],
            "chord_target": RNG.choice(_COMMANDS),
            "confirm_code": rand_code(4),
            "files": files,
            "rename_target": RNG.choice(files),
            "rename_to": f"handover-{rand_digits(4)}.txt",
            "hold_low": low,
            "hold_high": low + 20,
            "undo_items": list(_UNDO_ITEMS),
            "undo_removed": removed,  # removed in this order on load; restore the last two only
        }
        self.escaped = False

    def public(self) -> dict[str, Any]:
        return dict(self.v)

    def act(self, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        s, v = self.s, self.v
        if kind == "field_input":
            field, value = norm(data.get("field")), norm(data.get("value")).upper()
            s.log("page", kind, f"{field}={value}")
            if field == "Badge ID" and value == v["badge"]:
                s.complete("shift_tab")
                return s.say("tab", "Badge ID entered by keyboard ✓")
            if field != "Badge ID" and value:
                return s.futility("tab", f"That went into {field} — the badge belongs in Badge ID.")
            return s.futility("tab", "Badge ID does not hold the badge number yet.")
        if kind == "field_focus_rejected":
            s.log("page", kind, data.get("field"))
            return s.futility("tab", "Focus reached a field without the keyboard — these fields take focus from Tab / Shift+Tab only.")
        if kind == "stamp":
            cell = norm(data.get("cell")).upper()
            s.log("page", kind, cell)
            if cell == v["grid_target"]:
                s.complete("arrow_grid")
                return s.say("grid", f"Stamped {cell} ✓")
            return s.futility("grid", f"Stamped {cell} — the target is {v['grid_target']}.")
        if kind == "chord":
            key = norm(data.get("key")).upper()
            s.log("page", kind, key)
            command = next((cmd for cmd, k in v["chords"] if k == key), None)
            if command == v["chord_target"]:
                s.complete("chord")
                return s.say("chord", f"'{command}' applied ✓")
            return s.futility("chord", f"Ctrl+Shift+{key} is '{command or 'unassigned'}' — the task is '{v['chord_target']}'.")
        if kind == "escape":
            s.log("page", kind)
            self.escaped = True
            return s.say("modal", "Overlay dismissed — now confirm with Enter.")
        if kind == "enter_confirm":
            s.log("page", kind)
            if self.escaped:
                s.complete("escape_enter")
                return s.say("modal", "Confirmed with Enter ✓")
            return s.futility("modal", "Dismiss the overlay with Esc first.")
        if kind == "rename":
            old, new = norm(data.get("from")), norm(data.get("to"))
            s.log("page", kind, f"{old}->{new}")
            if old != v["rename_target"]:
                return s.futility("rename", f"{old} is not the file to rename — it was put back.")
            if new != v["rename_to"]:
                return s.futility("rename", f"Renamed to {new or '(empty)'} — the new name is {v['rename_to']}. It was put back.")
            s.complete("f2_rename")
            return s.say("rename", f"Renamed to {new} ✓")
        if kind == "key_hold":
            ms = _as_int(data.get("ms"), 0)
            pct = min(100, ms * 25 // 1000)
            s.log("page", kind, ms)
            if v["hold_low"] <= ms * 25 / 1000 <= v["hold_high"]:
                s.complete("key_hold")
                return s.say("hold", f"Charged to {pct}% ✓")
            return s.futility("hold", f"Released at {pct}% — release between {v['hold_low']}% and {v['hold_high']}%.")
        if kind == "undo_done":
            restored = {norm(x) for x in data.get("restored") or []}
            removed = {norm(x) for x in data.get("removed") or []}
            s.log("page", kind, ",".join(sorted(restored)))
            first, second, third = v["undo_removed"]
            if restored == {second, third} and first in removed:
                s.complete("undo_steps")
                return s.say("undo", "The last two removals were undone ✓")
            if first in restored:
                return s.futility("undo", f"{first} was restored too — it was removed on purpose. Redo (Ctrl+Y) puts it back.")
            return s.futility("undo", f"Restore exactly {third} and {second}.")
        return super().act(kind, data)


# =============================================================================================
# 3. Text editing — entering and changing text precisely
# =============================================================================================

_PEOPLE = ("Dr. Imani Okoro", "Mateo Álvarez-Ruiz", "Priya Raghunathan", "Søren Kjær", "Aoife Ní Bhriain", "Kwame Mensah")
_STREETS = ("Larkspur Avenue", "Harbor View Road", "Old Mill Lane", "Kestrel Way", "Juniper Court", "Bellwether Street")
_CITIES = (("Portland, OR", "97214"), ("Madison, WI", "53703"), ("Asheville, NC", "28801"), ("Boise, ID", "83702"), ("Tucson, AZ", "85701"))
_INSERTS = (
    ("Please confirm the shipment for order {n} before Friday so the warehouse can plan the loading dock.", "shipment", "expedited"),
    ("The board approved the contract with the regional supplier after a short review of the terms.", "contract", "amended"),
    ("Remember to attach the invoice when you email the client about the outstanding balance.", "invoice", "revised"),
    ("Our team will present the roadmap at the town hall meeting next Thursday afternoon.", "roadmap", "updated"),
)
_RICH = (
    "The quarterly review covers revenue targets, hiring plans, and supplier risks for next year.",
    "Before launch we must finalize pricing, translate documentation, and rehearse the keynote demo.",
    "Our audit found duplicate payments, missing receipts, and several unapproved vendor accounts.",
)
_SPECIALS = (
    "Größe 1.299,50 € — naïve café ½",
    "Çağlar & Zoë: 3×4 m² @ 18 °C",
    "Søren’s “final” draft – 2½ pages ✓",
    "Ångström ≈ 10⁻¹⁰ m; Ł ß æ œ",
)
_AUTOCOMPLETE = (
    ("Saint-Étienne (FR-42)", ("Saint-Étienne-du-Rouvray (FR-76)", "Saint-Estève (FR-66)", "Saint-Étienne-de-Tinée (FR-06)")),
    ("San José (CR-SJ)", ("San Jose (US-CA)", "San José del Cabo (MX-BCS)", "San Josecito (CR-H)")),
    ("Newport (GB-NWP)", ("Newport (US-RI)", "Newport News (US-VA)", "Newport Beach (US-CA)")),
)
_AUTOCOMPLETE_FILLER = (
    "Aarhus (DK-82)", "Bergamo (IT-BG)", "Cádiz (ES-CA)", "Dresden (DE-SN)", "Eindhoven (NL-NB)", "Fès (MA-05)",
    "Galway (IE-G)", "Hamamatsu (JP-22)", "Innsbruck (AT-7)", "Jaipur (IN-RJ)", "Kraków (PL-12)", "Lyon (FR-69)",
    "Malmö (SE-M)", "Nantes (FR-44)", "Oulu (FI-14)", "Porto (PT-13)", "Québec (CA-QC)", "Rennes (FR-35)",
    "Sapporo (JP-01)", "Tampere (FI-11)", "Utrecht (NL-UT)", "Valencia (ES-V)", "Wrocław (PL-02)", "Zürich (CH-ZH)",
    "Santa Fe (US-NM)", "Santiago (CL-RM)", "Sankt Gallen (CH-SG)", "Salzburg (AT-5)", "Newcastle (GB-NET)",
    "New Haven (US-CT)", "Nice (FR-06)", "San Diego (US-CA)",
)
_CONFIG_KEYS = (
    "server_name", "listen_port", "worker_count", "cache_ttl", "log_level", "max_upload_mb", "session_timeout",
    "tls_mode", "retry_limit", "queue_depth", "metrics_path",
)
_OWNERS = ("ops", "finance", "legal", "sales", "support", "infra")


class TextEditing(Gen4):
    name = "v4_text"
    slug = "v4-text"
    token = "BENCH_PASS v4-text-f3a805"
    family = "text_editing"
    steps = ("multiline", "caret_insert", "rich_format", "line_delete", "special_chars", "autocomplete", "deep_edit")

    def setup(self) -> None:
        city, zip_code = RNG.choice(_CITIES)
        address = [
            RNG.choice(_PEOPLE),
            f"Unit {RNG.randint(2, 9)}{RNG.choice('ABCD')}, {RNG.randint(100, 4999)} {RNG.choice(_STREETS)}",
            f"{city} {zip_code}",
            "United States",
        ]
        template, anchor, word = RNG.choice(_INSERTS)
        sentence = template.format(n=rand_digits(5))
        rich = RNG.choice(_RICH)
        candidates = [w.strip(",.") for w in rich.split() if len(w.strip(",.")) >= 6]
        candidates = [w for w in candidates if rich.count(w) == 1]
        bold, italic = RNG.sample(candidates, 2)
        keys = RNG.sample(_CONFIG_KEYS, 8)
        config = [f"{key} = {RNG.randint(2, 900)}" for key in keys]
        temp_line = f"TEMP_OVERRIDE_{rand_code(3)} = true"
        config.insert(RNG.randint(1, len(config) - 1), temp_line)
        target, lookalikes = RNG.choice(_AUTOCOMPLETE)
        options = sorted({target, *lookalikes, *_AUTOCOMPLETE_FILLER})
        base = RNG.randint(1000, 7000)
        records = []
        statuses = []
        for i in range(160):
            status = RNG.choice(("hold", "ready", "shipped", "hold", "review"))
            statuses.append(status)
            records.append(f"R-{base + i} | status: {status} | owner: {RNG.choice(_OWNERS)}")
        idx = RNG.choice([i for i in range(95, 150) if statuses[i] == "hold"] or [120])
        if statuses[idx] != "hold":
            records[idx] = records[idx].replace(f"status: {statuses[idx]}", "status: hold")
        expected_deep = list(records)
        expected_deep[idx] = records[idx].replace("status: hold", "status: ready")
        self.v = {
            "address": address,
            "sentence": sentence,
            "anchor": anchor,
            "insert_word": word,
            "sentence_expected": sentence.replace(f" {anchor}", f" {word} {anchor}", 1),
            "rich_text": rich,
            "bold_word": bold,
            "italic_word": italic,
            "config": config,
            "temp_line": temp_line,
            "special": RNG.choice(_SPECIALS),
            "cities": options,
            "city": target,
            "deep_lines": records,
            "deep_record": records[idx].split(" | ")[0],
            "deep_expected": expected_deep,
        }

    def public(self) -> dict[str, Any]:
        v = self.v
        return {k: v[k] for k in (
            "address", "sentence", "anchor", "insert_word", "rich_text", "bold_word", "italic_word", "config", "temp_line",
            "special", "cities", "city", "deep_lines", "deep_record",
        )}

    def act(self, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        s, v = self.s, self.v
        value = data.get("value")
        s.log("page", kind, str(value or "")[:40])
        if kind == "multiline":
            got = [squash(line) for line in _lines(value)]
            want = [squash(line) for line in v["address"]]
            if got == want:
                s.complete("multiline")
                return s.say("multiline", "Address saved exactly ✓")
            if len(got) != len(want):
                return s.futility("multiline", f"The address has {len(got)} lines; it needs {len(want)} — one per line, in order.")
            bad = next(i for i, (a, b) in enumerate(zip(got, want), 1) if a != b)
            return s.futility("multiline", f"Line {bad} does not match the address card.")
        if kind == "caret_insert":
            if norm(value) == v["sentence_expected"]:
                s.complete("caret_insert")
                return s.say("insert", "Sentence updated ✓")
            return s.futility("insert", f"The sentence must read exactly as before, with «{v['insert_word']}» added right before «{v['anchor']}».")
        if kind == "rich":
            text = squash(data.get("text"))
            bold = {norm(w).strip(",.") for w in data.get("bold") or []} - {""}
            italic = {norm(w).strip(",.") for w in data.get("italic") or []} - {""}
            mixed = [norm(w).strip(",.") for w in data.get("mixed") or [] if norm(w)]
            if text != squash(v["rich_text"]):
                return s.futility("rich", "The paragraph's words were changed — only formatting may change. Reset the paragraph and try again.")
            if mixed:
                return s.futility("rich", f"«{mixed[0]}» is only partly formatted — a whole word must be bold or italic.")
            if bold == {v["bold_word"]} and italic == {v["italic_word"]}:
                s.complete("rich_format")
                return s.say("rich", "Formatting applied ✓")
            return s.futility(
                "rich",
                f"Bold: {', '.join(sorted(bold)) or 'none'}; italic: {', '.join(sorted(italic)) or 'none'} — "
                f"only «{v['bold_word']}» bold and only «{v['italic_word']}» italic.",
            )
        if kind == "line_delete":
            want = [line for line in v["config"] if line != v["temp_line"]]
            if _lines(value) == want:
                s.complete("line_delete")
                return s.say("config", "Override line removed ✓")
            if v["temp_line"] in _lines(value):
                return s.futility("config", "The override line is still there.")
            return s.futility("config", "Other lines changed (or a blank line was left behind) — only the override line may go.")
        if kind == "special":
            if norm(value) == v["special"]:
                s.complete("special_chars")
                return s.say("special", "Exact match ✓")
            return s.futility("special", "Not an exact match — check every accent, symbol, and dash.")
        if kind == "autocomplete":
            if not data.get("picked"):
                return s.futility("city", "This field takes a choice from its suggestion list — typing alone does not choose.")
            if norm(value) == v["city"]:
                s.complete("autocomplete")
                return s.say("city", f"{v['city']} selected ✓")
            return s.futility("city", f"Selected {norm(value) or 'nothing'} — the shipment goes to {v['city']}.")
        if kind == "deep_edit":
            got = _lines(value)
            if got == v["deep_expected"]:
                s.complete("deep_edit")
                return s.say("deep", f"{v['deep_record']} set to ready ✓")
            changed = [i for i, (a, b) in enumerate(zip(got, v["deep_lines"])) if a != b]
            if len(got) != len(v["deep_lines"]):
                return s.futility("deep", "Lines were added or removed — change one word on one line only.")
            if not changed:
                return s.futility("deep", f"Nothing changed yet — {v['deep_record']} still says hold.")
            return s.futility("deep", f"{len(changed)} line(s) differ from the expected edit — only {v['deep_record']}'s status may change.")
        return super().act(kind, data)


# =============================================================================================
# 4. Selection and the clipboard
# =============================================================================================

_PHRASES = (
    ("The inspector counted fourteen sealed crates on the north platform before the barge left at dawn.", "fourteen sealed crates"),
    ("Payroll will move to the new provider on the first business day of March, after the final parallel run.", "first business day of March"),
    ("Visitors must sign the register at the front desk and wear the orange lanyard while on the factory floor.", "wear the orange lanyard"),
    ("The backup generator failed its monthly test because the fuel filter was clogged with sediment.", "fuel filter was clogged"),
)
_PARAGRAPHS = (
    "Harbor Freight Office, 12 Quay Street, Galway H91 E2K4, Ireland.",
    "Northwind Depot, 4410 Industrial Parkway, Dayton OH 45414, United States.",
    "Kestrel Logistics, Unit 7 Mill Lane Estate, Leeds LS12 6BD, United Kingdom.",
    "Brightwater Supply, 88 Rue des Forges, 69007 Lyon, France.",
    "Tidewater Imports, 301 Harbour Road, Fremantle WA 6160, Australia.",
)
_STATUSES = ("Paid", "Pending", "Draft", "Paid", "Pending")


class SelectionClipboard(Gen4):
    name = "v4_selection"
    slug = "v4-selection"
    token = "BENCH_PASS v4-selection-6c92d0"
    family = "selection_clipboard"
    steps = ("drag_select", "triple_click", "shift_range", "ctrl_set", "marquee", "keyboard_select", "copy_paste", "cut_paste")
    CORNERS = {
        "top-left": ("t0", "t1", "t4", "t5"),
        "top-right": ("t2", "t3", "t6", "t7"),
        "bottom-left": ("t8", "t9", "t12", "t13"),
        "bottom-right": ("t10", "t11", "t14", "t15"),
    }

    def setup(self) -> None:
        paragraph, phrase = RNG.choice(_PHRASES)
        paras = RNG.sample(_PARAGRAPHS, 3)
        numbers: set[str] = set()
        while len(numbers) < 16:
            numbers.add(f"INV-{rand_digits(4)}")
        invoices = sorted(numbers)
        i = RNG.randint(1, 8)
        j = i + RNG.randint(3, 6)
        accounts: set[str] = set()
        while len(accounts) < 12:
            accounts.add(f"AC-{rand_digits(3)}")
        rows = RNG.sample(sorted(accounts), 12)
        want = RNG.choice([3, 4])
        overdue: list[int] = []
        while len(overdue) < want:  # non-adjacent rows, so a range selection cannot grab them
            overdue = []
            for k in RNG.sample(range(12), 12):
                if len(overdue) < want and all(abs(k - o) > 1 for o in overdue):
                    overdue.append(k)
        statuses = [RNG.choice(_STATUSES) for _ in range(12)]
        for k in overdue:
            statuses[k] = "Overdue"
        corner = RNG.choice(list(self.CORNERS))
        num, letters, tail = rand_digits(4), rand_code(2, _LETTERS), rand_digits(2)
        self.v = {
            "paragraph": paragraph,
            "phrase": phrase,
            "paras": paras,
            "para_index": RNG.randrange(3),
            "invoices": invoices,
            "range_from": invoices[i],
            "range_to": invoices[j],
            "range_rows": invoices[i:j + 1],
            "accounts": rows,
            "statuses": statuses,
            "overdue": [rows[k] for k in sorted(overdue)],
            "corner": corner,
            "corner_tiles": list(self.CORNERS[corner]),
            "order_code": f"ORD-{num}-{letters}-{tail}-{RNG.choice(_LETTERS)}",
            "order_part": f"{num}-{letters}",
            "token": f"{rand_code(3)}-{rand_code(4)}-{rand_code(3)}",
            "cut_code": f"TC-{rand_digits(6)}",
        }
        self.cut_seen = False

    def public(self) -> dict[str, Any]:
        v = self.v
        out = {k: v[k] for k in (
            "paragraph", "phrase", "paras", "para_index", "invoices", "range_from", "range_to", "accounts", "statuses",
            "corner", "order_code", "order_part", "token", "cut_code",
        )}
        return out

    def act(self, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        s, v = self.s, self.v
        text = squash(data.get("text"))
        s.log("page", kind, text[:40])
        if kind == "quote_phrase":
            if text == v["phrase"]:
                s.complete("drag_select")
                return s.say("phrase", "Exact phrase quoted ✓")
            return s.futility("phrase", f"The selection was «{text[:60] or 'empty'}» — select exactly «{v['phrase']}».")
        if kind == "quote_para":
            want = v["paras"][v["para_index"]]
            if text == squash(want):
                if data.get("triple"):
                    s.complete("triple_click")
                    return s.say("para", "Paragraph quoted ✓")
                return s.futility("para", "Right paragraph — now select it with a triple-click.")
            return s.futility("para", "The selection is not exactly the requested paragraph.")
        if kind == "apply_range":
            rows = [norm(r) for r in data.get("rows") or []]
            if rows == v["range_rows"]:
                s.complete("shift_range")
                return s.say("range", f"{len(rows)} invoices selected ✓")
            return s.futility("range", f"{len(rows)} row(s) selected — select {v['range_from']} through {v['range_to']}, nothing else.")
        if kind == "apply_set":
            rows = sorted(norm(r) for r in data.get("rows") or [])
            if rows == sorted(v["overdue"]):
                s.complete("ctrl_set")
                return s.say("set", "Overdue accounts selected ✓")
            return s.futility("set", f"{len(rows)} row(s) selected — select every Overdue account and nothing else.")
        if kind == "marquee":
            tiles = sorted(norm(t) for t in data.get("tiles") or [])
            if tiles == sorted(v["corner_tiles"]):
                s.complete("marquee")
                return s.say("marquee", "Block selected ✓")
            return s.futility("marquee", f"The rectangle selected {len(tiles)} tile(s) — exactly the four tiles of the {v['corner']} block.")
        if kind == "use_selection":
            if norm(data.get("text")) == v["order_part"]:
                s.complete("keyboard_select")
                return s.say("part", "Selection used ✓")
            return s.futility("part", f"The field's selection was «{norm(data.get('text')) or 'empty'}» — select just «{v['order_part']}».")
        if kind == "paste_token":
            if norm(data.get("text")) == v["token"]:
                s.complete("copy_paste")
                return s.say("token", "Token pasted ✓")
            return s.futility("token", "The pasted text is not the access token.")
        if kind == "typed_into_paste_only":
            return s.futility("token", "This field takes paste only — copy the token and paste it.")
        if kind == "cut":
            if v["cut_code"] in norm(data.get("text")):
                self.cut_seen = True
            return {"ok": True}
        if kind == "paste_final":
            if norm(data.get("text")) != v["cut_code"]:
                return s.futility("cut", "Final received something other than the transfer code.")
            if not self.cut_seen:
                return s.futility("cut", "The code reached Final without being cut from Draft — cut it (Ctrl+X), don't copy it.")
            if norm(data.get("draft")):
                return s.futility("cut", "Draft still holds text — the code must move, leaving Draft empty.")
            s.complete("cut_paste")
            return s.say("cut", "Transfer code moved ✓")
        return super().act(kind, data)


# =============================================================================================
# 5. Drag — HTML5 drops, pointer drags, sliders, splitters, copy-drags, long-press drags, lassos
# =============================================================================================

_REORDER = ("Alpha", "Bravo", "Charlie", "Delta", "Echo")
_KANBAN = ("Audit log export", "Billing webhook", "Cache warmup", "Dark mode", "Error budget", "Feature flags",
           "Gateway retries", "Health checks", "Inbox search", "Job scheduler")
_LONG = ("Groceries", "Pharmacy", "Dry cleaning", "Post office", "Hardware store")


def _point_in_polygon(x: float, y: float, poly: list[tuple[float, float]]) -> bool:
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            cross = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < cross:
                inside = not inside
    return inside


class Drag(Gen4):
    name = "v4_drag"
    slug = "v4-drag"
    token = "BENCH_PASS v4-drag-91e4b7"
    family = "drag_drop"
    steps = ("html5_drop", "reorder", "kanban", "dual_slider", "splitter", "copy_drag", "long_press_drag", "lasso")
    CANVAS = (640, 340)

    def setup(self) -> None:
        cards = []
        while len(cards) < 4:
            card = f"DOC-{rand_digits(4)}"
            if card not in cards:
                cards.append(card)
        start = list(_REORDER)
        RNG.shuffle(start)
        target = list(_REORDER)
        while sum(a != b for a, b in zip(start, target)) < 4:
            RNG.shuffle(target)
        names = list(_KANBAN)
        RNG.shuffle(names)
        backlog, doing, review = names[:4], names[4:6], names[6:9]
        move = RNG.choice(backlog)
        position = RNG.randint(1, 2)
        expected = {
            "Backlog": [c for c in backlog if c != move],
            "Doing": list(doing),
            "Review": review[:position] + [move] + review[position:],
        }
        low = RNG.choice([n for n in range(8, 42) if n % 5])
        high = RNG.choice([n for n in range(56, 93) if n % 5])
        width = RNG.randint(300, 500)
        long_items = list(_LONG)
        RNG.shuffle(long_items)
        cw, ch = self.CANVAS
        while True:
            rx, ry = RNG.randint(150, cw - 150), RNG.randint(90, ch - 90)
            angle = RNG.uniform(0, 2 * math.pi)
            dist = RNG.uniform(95, 125)
            bx, by = rx + dist * math.cos(angle), ry + dist * math.sin(angle)
            if 50 <= bx <= cw - 50 and 50 <= by <= ch - 50:
                break
        self.v = {
            "cards": cards,
            "card": RNG.choice(cards),
            "order_start": start,
            "order_target": target,
            "kanban": {"Backlog": backlog, "Doing": doing, "Review": review},
            "kanban_move": move,
            "kanban_after": review[position - 1],
            "kanban_before": review[position],
            "kanban_expected": expected,
            "range_low": low,
            "range_high": high,
            "split_min": width,
            "split_max": width + 20,
            "copy_slot": RNG.randint(1, 3),
            "long_items": long_items,
            "long_target": RNG.choice(long_items[1:]),
            "red": [round(rx), round(ry)],
            "blue": [round(bx), round(by)],
        }

    def public(self) -> dict[str, Any]:
        v = self.v
        return {k: v[k] for k in (
            "cards", "card", "order_start", "order_target", "kanban", "kanban_move", "kanban_after", "kanban_before",
            "range_low", "range_high", "split_min", "split_max", "copy_slot", "long_items", "long_target", "red", "blue",
        )}

    def judge_lasso(self, points: list[Any]) -> str | None:
        """None when the loop encloses the red star and not the blue one; otherwise why not."""
        try:
            pts = [(float(p[0]), float(p[1])) for p in points]
        except (TypeError, ValueError, IndexError):
            return "That stroke could not be read."
        if len(pts) < 8:
            return "That was a click or a short flick — draw a loop around the red star."
        length = sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
        if length < 150:
            return "The stroke is too short to be a loop around the star."
        if math.dist(pts[0], pts[-1]) > 40:
            return "The loop is not closed — finish where you started."
        red_in = _point_in_polygon(*self.v["red"], pts)
        blue_in = _point_in_polygon(*self.v["blue"], pts)
        if not red_in:
            return "The loop does not go around the red star."
        if blue_in:
            return "The loop also encloses the blue star — circle the red one only."
        return None

    def act(self, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        s, v = self.s, self.v
        if kind == "drop_bin":
            card = norm(data.get("card"))
            s.log("page", kind, card)
            if card == v["card"]:
                s.complete("html5_drop")
                return s.say("bin", f"{card} archived ✓")
            return s.futility("bin", f"{card or 'Nothing'} landed in the bin — it bounced back; archive {v['card']}.")
        if kind == "check_order":
            order = [norm(x) for x in data.get("order") or []]
            s.log("page", kind, ",".join(order))
            if order == v["order_target"]:
                s.complete("reorder")
                return s.say("order", "Order matches ✓")
            wrong = sum(a != b for a, b in zip(order, v["order_target"]))
            return s.futility("order", f"{wrong} position(s) differ from the target order.")
        if kind == "save_board":
            board = data.get("board") or {}
            got = {col: [norm(x) for x in board.get(col) or []] for col in ("Backlog", "Doing", "Review")}
            s.log("page", kind, "|".join(",".join(c) for c in got.values()))
            if got == v["kanban_expected"]:
                s.complete("kanban")
                return s.say("board", "Board saved ✓")
            if v["kanban_move"] not in got["Review"]:
                return s.futility("board", f"{v['kanban_move']} is not in Review yet.")
            return s.futility("board", f"Review must read … {v['kanban_after']}, {v['kanban_move']}, {v['kanban_before']} … and nothing else may move.")
        if kind == "apply_range":
            low, high = _as_int(data.get("low")), _as_int(data.get("high"))
            s.log("page", kind, f"{low}-{high}")
            if (low, high) == (v["range_low"], v["range_high"]):
                s.complete("dual_slider")
                return s.say("range", f"Range {low}–{high} applied ✓")
            return s.futility("range", f"Applied {low}–{high} — the range is {v['range_low']}–{v['range_high']}.")
        if kind == "lock_layout":
            width = _as_float(data.get("width"))
            s.log("page", kind, width)
            if v["split_min"] <= width <= v["split_max"]:
                s.complete("splitter")
                return s.say("split", f"Layout locked at {width:.0f} px ✓")
            return s.futility("split", f"The left pane is {width:.0f} px — it must be {v['split_min']}–{v['split_max']} px.")
        if kind == "copy_drag":
            slot, copied, kept = _as_int(data.get("slot")), bool(data.get("copied")), bool(data.get("palette_kept"))
            s.log("page", kind, f"slot{slot} copied={copied}")
            if not copied or not kept:
                return s.futility("copy", "That drag moved the template — the palette is empty now. Reset it and drag with Ctrl held to copy.")
            if slot != v["copy_slot"]:
                return s.futility("copy", f"The copy went into slot {slot} — it belongs in slot {v['copy_slot']}.")
            s.complete("copy_drag")
            return s.say("copy", f"Template copied into slot {slot} ✓")
        if kind == "long_press_order":
            order = [norm(x) for x in data.get("order") or []]
            s.log("page", kind, ",".join(order))
            rest = [x for x in v["long_items"] if x != v["long_target"]]
            if order == [v["long_target"], *rest]:
                s.complete("long_press_drag")
                return s.say("long", f"{v['long_target']} is first ✓")
            return s.futility("long", f"{v['long_target']} must be first, with the others in their original order.")
        if kind == "lasso":
            problem = self.judge_lasso(data.get("points") or [])
            s.log("page", kind, problem or "ok")
            if problem is None:
                s.complete("lasso")
                return s.say("lasso", "Red star circled ✓")
            return s.futility("lasso", problem)
        if kind == "drag_attempt":
            s.log("page", kind, data.get("what"))
            return s.futility(str(data.get("key") or "long"), str(data.get("msg") or "That drag did not register."))
        return super().act(kind, data)


# =============================================================================================
# 6. Scroll and zoom — nested regions, horizontal lanes, panning, Ctrl+wheel, long documents
# =============================================================================================

_BUILDINGS = (
    "Ashgrove Hall", "Birchwood Lofts", "Coralline House", "Driftwood Court", "Elmstead Tower", "Fernbank Works",
    "Glasshouse Row", "Hollyoak Mews", "Ironstone Exchange", "Juniper Arcade", "Kingfisher Yard", "Lanternfield",
)


class ScrollZoom(Gen4):
    name = "v4_scroll"
    slug = "v4-scroll"
    token = "BENCH_PASS v4-scroll-47d2a1"
    family = "scroll_zoom"
    steps = ("inner_scroll", "horizontal", "pan_map", "zoom_map", "long_document")
    WORLD = (2400, 1600)
    VIEW = (640, 360)
    PAGES = 80

    def setup(self) -> None:
        quarters = [f"{year}-Q{q}" for year in range(2027, 2047) for q in range(1, 5)]
        ww, wh = self.WORLD
        flags: list[dict[str, Any]] = []
        ids: set[str] = set()
        while len(flags) < 14:
            fid = f"F-{RNG.randint(10, 99)}"
            x, y = RNG.randint(80, ww - 80), RNG.randint(80, wh - 80)
            if fid in ids or any(math.dist((x, y), (f["x"], f["y"])) < 160 for f in flags):
                continue
            ids.add(fid)
            flags.append({"id": fid, "x": x, "y": y})
        far = [f for f in flags if f["x"] > 1100 or f["y"] > 760]
        target = RNG.choice(far) if far else flags[-1]
        names = RNG.sample(_BUILDINGS, 6)
        cx, cy = RNG.randint(700, ww - 400), RNG.randint(400, wh - 300)
        buildings = []
        for i, nm in enumerate(names):
            buildings.append({"name": nm, "x": cx + (i % 3) * 46 - 46, "y": cy + (i // 3) * 40 - 20, "star": False})
        star = RNG.randrange(len(buildings))
        buildings[star]["star"] = True
        self.v = {
            "row_target": RNG.randint(180, 290),
            "quarters": quarters,
            "quarter": RNG.choice(quarters[55:79]),
            "flags": flags,
            "flag": target["id"],
            "buildings": buildings,
            "building": buildings[star]["name"],
            "closing_code": f"CL-{rand_code(6)}",
        }
        self.doc_end_seen = False

    def public(self) -> dict[str, Any]:
        v = self.v
        return {
            "row_target": v["row_target"], "quarters": v["quarters"], "quarter": v["quarter"], "flags": v["flags"],
            "flag": v["flag"], "pages": self.PAGES,
            "buildings": [{"x": b["x"], "y": b["y"], "star": b["star"]} for b in v["buildings"]],  # names: zoom in first
        }

    def truth(self) -> dict[str, Any]:
        return {"building": self.v["building"], "closing_code": self.v["closing_code"], "flag": self.v["flag"]}

    def act(self, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        s, v = self.s, self.v
        if kind == "pick_row":
            row = _as_int(data.get("row"))
            s.log("page", kind, row)
            if row == v["row_target"]:
                s.complete("inner_scroll")
                return s.say("row", f"Row {row} picked ✓")
            return s.futility("row", f"Picked row {row} — the target is row {v['row_target']}.")
        if kind == "open_event":
            quarter = norm(data.get("quarter"))
            s.log("page", kind, quarter)
            if quarter == v["quarter"]:
                s.complete("horizontal")
                return s.say("timeline", f"{quarter} opened ✓")
            return s.futility("timeline", f"Opened {quarter} — the task is {v['quarter']}.")
        if kind == "flag_click":
            flag = norm(data.get("flag")).upper()
            s.log("page", kind, flag)
            if flag == v["flag"]:
                s.complete("pan_map")
                return s.say("flag", f"Flag {flag} found ✓")
            return s.futility("flag", f"That is flag {flag} — find {v['flag']}.")
        if kind == "building_name":
            value = squash(data.get("value"))
            s.log("page", kind, value)
            if value.lower() == v["building"].lower():
                s.complete("zoom_map")
                return s.say("building", "Building identified ✓")
            return s.futility("building", "That is not the name of the starred building.")
        if kind == "zoomed":
            zoom = _as_float(data.get("zoom"), 0.0)
            s.log("page", kind, zoom)
            if zoom >= 3:
                return {"ok": True, "names": [b["name"] for b in v["buildings"]]}
            return {"ok": False}
        if kind == "zoomed":
            zoom = _as_float(data.get("zoom"), 0.0)
            s.log("page", kind, zoom)
            if zoom >= 3:
                return {"ok": True, "names": [b["name"] for b in v["buildings"]]}
            return {"ok": False}
        if kind == "doc_end":
            s.log("page", kind)
            self.doc_end_seen = True
            return {"ok": True, "code": v["closing_code"]}
        if kind == "closing_code":
            value = norm(data.get("value")).upper()
            s.log("page", kind, value)
            if self.doc_end_seen and value == v["closing_code"]:
                s.complete("long_document")
                return s.say("doc", "Closing code accepted ✓")
            return s.futility("doc", "That is not the closing code from the last page.")
        return super().act(kind, data)


# =============================================================================================
# 7. Form controls — the browser's own pickers and choosers
# =============================================================================================

_PORTS = (
    "Aberdeen", "Algeciras", "Antwerp", "Auckland", "Baltimore", "Barcelona", "Bremerhaven", "Busan", "Cape Town",
    "Charleston", "Chittagong", "Colombo", "Dalian", "Dar es Salaam", "Durban", "Felixstowe", "Fremantle", "Gdańsk",
    "Genoa", "Gothenburg", "Halifax", "Hamburg", "Houston", "Jebel Ali", "Kaohsiung", "Karachi", "Kingston",
    "Klaipėda", "Le Havre", "Lisbon", "Manzanillo", "Marseille", "Melbourne", "Mombasa", "Montreal", "Mumbai",
    "Naples", "Ningbo", "Oakland", "Piraeus", "Port Said", "Qingdao", "Rotterdam", "Santos", "Savannah",
    "Southampton", "Tacoma", "Tanjung Pelepas", "Valencia", "Yokohama",
)
_MULTI = (
    "Adhesives", "Bearings", "Cabling", "Dowels", "Enclosures", "Fasteners", "Gaskets", "Hinges", "Insulation",
    "Junction boxes", "Kick plates", "Latches", "Mounts", "Nameplates", "O-rings", "Pulleys", "Rivets", "Sealant",
    "Terminals", "Washers",
)


class FormControls(Gen4):
    name = "v4_widgets"
    slug = "v4-widgets"
    token = "BENCH_PASS v4-widgets-c0f5e8"
    family = "form_controls"
    steps = ("native_select", "date_input", "time_input", "color_input", "range_exact", "multi_select", "file_pick")
    needs_workspace = True

    def setup(self) -> None:
        s = self.s
        ws = s.harness.make_workspace(s, "widgets")
        inbox = ws / "inbox"
        inbox.mkdir(parents=True)
        code, other = rand_code(4), rand_code(4)

        def manifest(tag: str, rows: int) -> bytes:
            lines = ["sku,qty,bin"] + [f"{tag}-{rand_digits(3)},{RNG.randint(1, 40)},{rand_code(2)}" for _ in range(rows)]
            return ("\r\n".join(lines) + "\r\n").encode("ascii")

        files = {
            f"manifest-{code}.csv": manifest(code, 6),
            f"manifest-{code}-old.csv": manifest(code, 4),
            f"manifest-{other}.csv": manifest(other, 6),
        }
        for name, data in files.items():
            (inbox / name).write_bytes(data)
        day = date(2027, 1, 1) + timedelta(days=RNG.randint(1, 360))
        while day.day == 1:
            day += timedelta(days=3)
        hour, minute = RNG.randint(6, 21), RNG.choice([m for m in range(1, 60) if m % 15])
        self.v = {
            "workspace": str(ws),
            "inbox": str(inbox),
            "ports": list(_PORTS),
            "port": RNG.choice(_PORTS[5:]),
            "date": day.isoformat(),
            "date_text": day.strftime("%A, %B %d, %Y").replace(" 0", " "),
            "time": f"{hour:02d}:{minute:02d}",
            "time_text": f"{(hour - 1) % 12 + 1}:{minute:02d} {'AM' if hour < 12 else 'PM'}",
            "color": "#" + "".join(f"{RNG.randint(16, 239):02x}" for _ in range(3)),
            "range": RNG.choice([n for n in range(120, 980) if n % 10]),
            "multi_options": list(_MULTI),
            "multi": sorted(RNG.sample(_MULTI, 3)),
            "file": f"manifest-{code}.csv",
            "file_sha": sha(files[f"manifest-{code}.csv"]),
        }

    def public(self) -> dict[str, Any]:
        v = self.v
        return {k: v[k] for k in (
            "inbox", "ports", "port", "date_text", "time_text", "color", "range", "multi_options", "multi", "file",
        )}

    def act(self, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        s, v = self.s, self.v
        value = data.get("value")
        s.log("page", kind, str(value)[:40])
        checks = {
            "native_select": ("select", norm(value) == v["port"], f"Selected {norm(value) or 'nothing'} — the port is {v['port']}."),
            "date_input": ("date", norm(value) == v["date"], "The date field does not hold the requested date."),
            "time_input": ("time", norm(value)[:5] == v["time"], "The time field does not hold the requested time."),
            "color_input": ("color", norm(value).lower() == v["color"], f"The color is {norm(value) or 'unset'} — it must be {v['color']}."),
            "range_exact": ("range", _as_int(value) == v["range"], f"The slider is at {_as_int(value, 0)} — it must be exactly {v['range']}."),
        }
        if kind in checks:
            key, ok, problem = checks[kind]
            if ok:
                s.complete(kind)
                return s.say(key, "Saved ✓")
            return s.futility(key, problem)
        if kind == "multi_select":
            got = sorted(norm(x) for x in data.get("values") or [])
            if got == v["multi"]:
                s.complete("multi_select")
                return s.say("multi", "Exactly those three selected ✓")
            return s.futility("multi", f"{len(got)} option(s) selected — select exactly {', '.join(v['multi'])}.")
        if kind == "file_pick":
            name = norm(data.get("name"))
            digest = sha(str(data.get("text") or "").encode("utf-8"))
            s.log("filesystem", "picked", name)
            if digest == v["file_sha"]:
                s.complete("file_pick")
                return s.say("file", f"{name} attached ✓")
            return s.futility("file", f"{name or 'That file'} is not {v['file']} from the mission inbox.")
        return super().act(kind, data)


# =============================================================================================
# 8. Browser UI — the address bar, tabs, history, reload
# =============================================================================================


class BrowserUi(Gen4):
    name = "v4_browser"
    slug = "v4-browser"
    token = "BENCH_PASS v4-browser-5e81d3"
    family = "browser_ui"
    steps = ("typed_url", "tab_relay", "back_forward", "reload", "close_tab")
    ticks = True
    CLOSE_SETTLE_S = 2.5  # a pagehide followed by a fresh load is a reload or navigation, not a close

    def setup(self) -> None:
        self.v = {"key": rand_code(8).lower(), "vault_code": f"V-{rand_code(5)}"}
        self.hop = ""
        self.tabs: dict[str, str] = {}
        self.closed_at: dict[str, float] = {}

    def public(self) -> dict[str, Any]:
        return {"dest_path": f"/eval/bench/v4_browser_dest?sid={self.s.sid}&key={self.v['key']}", "hop": self.hop, "tabs": dict(self.tabs)}

    def act(self, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        s, v = self.s, self.v
        if kind == "page_loaded":
            nav = norm(data.get("navtype"))
            s.log("page", kind, nav)
            if nav == "reload":
                s.complete("reload")
                s.say("reload", "Reloaded ✓")
            return {"ok": True}
        if kind == "dest_arrived":
            s.log("browser_chrome", kind)
            if norm(data.get("key")) != v["key"]:
                return s.futility("typed", "That address is not the one on the mission page — type it exactly.")
            if data.get("same_tab"):
                return s.futility("typed", "That address opened in a tab the bench was already using — open a NEW tab (Ctrl+T) and type it there.")
            s.complete("typed_url")
            return s.say("typed", "Address opened in a new tab ✓")
        if kind == "vault_open":
            s.log("browser_window", kind)
            return {"ok": True, "code": v["vault_code"]}
        if kind == "vault_code":
            value = norm(data.get("value")).upper()
            s.log("page", kind, value)
            if value == v["vault_code"]:
                s.complete("tab_relay")
                return s.say("vault", "Vault code accepted ✓")
            return s.futility("vault", "That is not the code shown in the vault tab.")
        if kind == "hop":
            step, nav = norm(data.get("step")), norm(data.get("navtype"))
            s.log("browser_window", kind, f"{step}:{nav}")
            if "back_forward" in s.done:
                return {"ok": True, "phase": "done"}
            if nav == "back_forward":
                if step == "a" and self.hop == "b":
                    self.hop = "back"
                elif step == "b" and self.hop == "back":
                    self.hop = "done"
                    s.complete("back_forward")
                    s.say("history", "Back and Forward both used ✓")
            elif step == "a":
                self.hop = "a"
            elif step == "b" and self.hop in ("a", "b"):
                self.hop = "b"
            s.touch()
            return {"ok": True, "phase": self.hop}
        if kind == "tab_open":
            which = norm(data.get("which"))
            s.log("browser_window", kind, which)
            self.tabs[which] = "open"
            self.closed_at.pop(which, None)
            s.touch()
            return {"ok": True}
        if kind == "tab_hidden":
            which = norm(data.get("which"))
            s.log("browser_window", kind, which)
            if self.tabs.get(which) == "open":
                self.closed_at[which] = time.monotonic()
            return {"ok": True}
        return super().act(kind, data)

    def poll(self) -> None:
        s = self.s
        now = time.monotonic()
        for which, at in list(self.closed_at.items()):
            if now - at < self.CLOSE_SETTLE_S:
                continue
            self.closed_at.pop(which)
            self.tabs[which] = "closed"
            s.touch()
            if which == "draft":
                s.futility("tabs", "The draft report tab was closed — reopen it with its button and close only the promo tab.")
            elif which == "promo" and "close_tab" not in s.done:
                if self.tabs.get("draft") == "open":
                    s.complete("close_tab")
                    s.say("tabs", "Promo tab closed, draft kept ✓")
                else:
                    s.futility("tabs", "The promo tab is closed, but the draft report tab is not open — open both again, then close only the promo.")


# =============================================================================================
# 9. Window management — native windows moved, sized, maximized, minimized, snapped, closed, found
# =============================================================================================


def _inside(inner: dict[str, float], outer: dict[str, float], tol: float) -> bool:
    return (
        inner["x"] >= outer["x"] - tol
        and inner["y"] >= outer["y"] - tol
        and inner["x"] + inner["w"] <= outer["x"] + outer["w"] + tol
        and inner["y"] + inner["h"] <= outer["y"] + outer["h"] + tol
    )


class Windows(Gen4):
    name = "v4_windows"
    slug = "v4-windows"
    token = "BENCH_PASS v4-windows-d7a330"
    family = "window_management"
    steps = ("move_window", "resize_window", "maximize_restore", "minimize_restore", "snap_half", "close_specific", "bring_front")
    needs_desktop = True
    TIPS = 3
    VIEWS = {
        "dock": ("win.dock", "win.gauge"),
        "sizer": ("win.sizer",),
        "ledger": ("win.ledger",),
        "chat": ("win.inbox", "win.chat"),
        "notes": ("win.notes",),
        "tips": ("win.worklog", *(f"win.tip{i}" for i in range(1, 4))),
        "beacon": ("win.beacon", "win.cover"),
    }

    def setup(self) -> None:
        total = RNG.randint(10_000, 99_999) + RNG.randint(0, 99) / 100
        self.v = {
            "size_w": RNG.randrange(640, 861, 10),
            "size_h": RNG.randrange(420, 541, 10),
            "size_tol": 20,
            "snap_side": RNG.choice(["left", "right"]),
            "ledger_total": f"{total:,.2f}",
            "beacon_code": rand_code(4),
        }
        self.total_ok = False
        self.ledger_last: tuple[str, bool] | None = None
        self.approved_minimized = False
        self.tips_closed: set[str] = set()
        self.worklog_closed = False
        self.opened: set[str] = set()

    def public(self) -> dict[str, Any]:
        v = self.v
        return {"size_w": v["size_w"], "size_h": v["size_h"], "size_tol": v["size_tol"], "snap_side": v["snap_side"]}

    # geometry rules (pure, so the tests pin them) ----------------------------------------------
    @staticmethod
    def size_ok(w: float, h: float, state: str, scale: float, want_w: int, want_h: int, tol: int) -> bool:
        return state == "normal" and abs(w - want_w * scale) <= tol * scale and abs(h - want_h * scale) <= tol * scale

    @staticmethod
    def snapped(rect: dict[str, float], state: str, side: str, screen_w: float, screen_h: float, scale: float) -> bool:
        """The window fills one half of the screen's work area (Win+Arrow, or dragged to the edge)."""
        if state != "normal":
            return False
        tol = 48 * scale
        left, right = rect["x"], rect["x"] + rect["w"]
        top, bottom = rect["y"], rect["y"] + rect["h"]
        half = screen_w / 2
        tall = top <= 70 * scale and bottom >= screen_h - 130 * scale
        if side == "left":
            return tall and left <= tol and abs(right - half) <= tol
        return tall and abs(left - half) <= tol and right >= screen_w - tol

    def act(self, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        s, v = self.s, self.v
        if kind == "open":
            what = norm(data.get("what"))
            s.log("page", kind, what)
            views = self.VIEWS.get(what)
            if not views:
                return s.futility(None, "Unknown window group.")
            if what == "tips":
                self.tips_closed.clear()
                self.worklog_closed = False
            for view in views:
                self.open_window(view)
            self.opened.add(what)
            return s.say("open_" + what, "Opened on the desktop (it may be behind the browser).")
        if kind == "ledger_total":
            value = norm(data.get("value")).replace(",", "").replace(" ", "")
            s.log("page", kind, value)
            if value and _as_float(value) == _as_float(v["ledger_total"].replace(",", "")):
                self.total_ok = True
                if self.ledger_last == ("normal", False):  # already restored before the total was typed
                    s.complete("maximize_restore")
                    return s.say("ledger", "Total recorded and the Ledger is back to normal size ✓")
                return s.say("ledger", "Total recorded — now restore the Ledger window to its normal size.")
            return s.futility("ledger", "That is not the Ledger's total.")
        return super().act(kind, data)

    def ui(self, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        s, v = self.s, self.v
        window = str(data.get("window") or "")
        scale = float(data.get("scale") or 1.0)
        if kind == "docked":
            inside = _inside(data["gauge"], data["slot"], 14 * scale)
            s.log("desktop", kind, inside)
            if inside:
                s.complete("move_window")
                s.say("dock", "Gauge docked ✓")
            return {"ok": inside}
        if kind == "sized":
            ok = self.size_ok(float(data["w"]), float(data["h"]), str(data["state"]), scale, v["size_w"], v["size_h"], v["size_tol"])
            s.log("desktop", kind, f"{data['w']}x{data['h']} {data['state']}")
            if ok:
                s.complete("resize_window")
                s.say("sizer", "Window size locked ✓")
            return {"ok": ok}
        if kind == "ledger_geometry":
            state, wide = str(data.get("state")), bool(data.get("wide"))
            s.log("desktop", kind, f"{state} wide={wide}")
            self.ledger_last = (state, wide)
            if self.total_ok and state == "normal" and not wide:
                s.complete("maximize_restore")
                s.say("ledger", "Ledger restored ✓")
            return {"ok": True}
        if kind == "approve":
            chat = str(data.get("chat_state") or "")
            s.log("desktop", kind, chat)
            if chat == "iconic":
                self.approved_minimized = True
                return s.say("chat", "Approved — now restore the Chat window.")
            if chat == "closed":
                return s.futility("chat", "The Chat window was closed — reopen the pair and minimize Chat instead.")
            return s.futility("chat", "Approve was pressed while Chat was still on screen — minimize Chat first.")
        if kind == "chat_state":
            state = str(data.get("state") or "")
            s.log("desktop", kind, state)
            if self.approved_minimized and state == "normal":
                s.complete("minimize_restore")
                s.say("chat", "Chat restored ✓")
            return {"ok": True}
        if kind == "snap_geometry":
            rect = {k: float(data[k]) for k in ("x", "y", "w", "h")}
            ok = self.snapped(rect, str(data["state"]), v["snap_side"], float(data["screen_w"]), float(data["screen_h"]), scale)
            s.log("desktop", kind, f"{rect} ok={ok}")
            if ok:
                s.complete("snap_half")
                s.say("notes", f"Notes snapped to the {v['snap_side']} half ✓")
            return {"ok": ok}
        if kind == "acknowledge":
            alive = bool(data.get("cover_alive"))
            s.log("desktop", kind, alive)
            if alive:
                s.complete("bring_front")
                return s.say("beacon", "Beacon acknowledged ✓")
            return s.futility("beacon", "The maintenance overlay was closed — it must stay open. Reopen the pair and bring the Beacon to the front instead.")
        if kind == "close":
            s.log("desktop", "close_window", window)
            if window.startswith("win.tip"):
                self.tips_closed.add(window)
                if len(self.tips_closed) == self.TIPS and not self.worklog_closed:
                    s.complete("close_specific")
                    s.say("tips", "All tips closed, work log kept ✓")
            elif window == "win.worklog":
                self.worklog_closed = True
                s.futility("tips", "The Work log was closed — its notes are gone. Reopen the windows and close only the tips.")
            elif window == "win.chat":
                self.approved_minimized = False
                s.futility("chat", "Chat was closed (that ends the call) — reopen the pair and minimize Chat instead.")
            elif window == "win.cover":
                s.futility("beacon", "The maintenance overlay must stay open — reopen the pair.")
            return {"ok": True, "close": True}
        return super().ui(kind, data)


# =============================================================================================
# 10. Native app — menus, context menus, double-click, cross-window drag, wheel, selection, keys, Open
# =============================================================================================

_PARAGRAPH_NATIVE = (
    "Crates bound for the eastern terminal must be strapped twice and logged before the {time} cutoff. "
    "Any crate with a torn seal is held for inspection, and the {phrase} must be signed by the shift lead."
)
_NATIVE_PHRASES = ("cold chain declaration", "hazmat routing slip", "customs release form", "overweight permit")


class NativeApp(Gen4):
    name = "v4_native"
    slug = "v4-native"
    token = "BENCH_PASS v4-native-a64f1c"
    family = "native_app"
    steps = ("cascade_menu", "context_menu", "double_click_open", "cross_window_drag", "wheel_select", "text_select", "accelerator", "open_dialog")
    needs_desktop = True
    needs_workspace = True

    def setup(self) -> None:
        s = self.s
        ws = s.harness.make_workspace(s, "native")
        inbox = ws / "inbox"
        inbox.mkdir(parents=True)
        crates: list[str] = []
        while len(crates) < 12:
            crate = f"CR-{rand_digits(4)}"
            if crate not in crates:
                crates.append(crate)
        flag, open_, drag = RNG.sample(crates, 3)
        base = RNG.randint(10_000, 60_000)
        lines = sorted({base + RNG.randint(0, 9000) for _ in range(400)})[:300]
        manifest = [f"M-{n}" for n in lines]
        code, other = rand_code(4), rand_code(4)

        def packing(tag: str, n: int) -> bytes:
            rows = ["crate,item,qty"] + [f"{RNG.choice(crates)},{tag}-{rand_digits(3)},{RNG.randint(1, 9)}" for _ in range(n)]
            return ("\r\n".join(rows) + "\r\n").encode("ascii")

        files = {
            f"packing-{code}.csv": packing(code, 7),
            f"packing-{code}-v1.csv": packing(code, 5),
            f"packing-{other}.csv": packing(other, 7),
        }
        for name, data in files.items():
            (inbox / name).write_bytes(data)
        phrase = RNG.choice(_NATIVE_PHRASES)
        self.v = {
            "workspace": str(ws),
            "inbox": str(inbox),
            "crates": crates,
            "seals": {c: f"S-{rand_digits(5)}" for c in crates},
            "flag": flag,
            "open": open_,
            "drag": drag,
            "manifest": manifest,
            "manifest_target": RNG.choice(manifest[160:290]),
            "paragraph": _PARAGRAPH_NATIVE.format(time=f"{RNG.randint(13, 21)}:{RNG.choice(['05', '20', '35', '50'])}", phrase=phrase),
            "phrase": phrase,
            "file": f"packing-{code}.csv",
            "file_sha": sha(files[f"packing-{code}.csv"]),
        }
        self.units = "imperial"

    def public(self) -> dict[str, Any]:
        v = self.v
        return {k: v[k] for k in ("inbox", "flag", "open", "drag", "manifest_target", "phrase", "file")}

    def act(self, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        if kind == "launch":
            self.s.log("page", kind)
            self.open_window("native.warehouse")
            self.open_window("native.truck")
            return self.s.say("launch", "Warehouse Manager and Truck Loader opened on the desktop.")
        return super().act(kind, data)

    def ui(self, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        s, v = self.s, self.v
        crate = norm(data.get("crate")).upper()
        if kind == "units":
            value = norm(data.get("value")).lower()
            s.log("desktop", kind, value)
            self.units = value
            if value == "metric":
                s.complete("cascade_menu")
                s.say("units", "Units set to Metric ✓")
                return {"ok": True}
            return s.futility("units", "Units are Imperial — the task is Metric.")
        if kind == "crate_menu":
            action = norm(data.get("action"))
            s.log("desktop", kind, f"{action}:{crate}")
            if action == "Remove crate":
                return s.fail(f"removed crate {crate} from the inventory")
            if action == "Flag for inspection" and crate == v["flag"]:
                s.complete("context_menu")
                return s.say("flag", f"{crate} flagged for inspection ✓")
            if action == "Flag for inspection":
                return s.futility("flag", f"Flagged {crate} — the crate to flag is {v['flag']}.")
            return s.futility("flag", f"Chose '{action}' — the task is Flag for inspection.")
        if kind == "detail_opened":
            s.log("desktop", kind, crate)
            return {"ok": True, "seal": v["seals"].get(crate, "?")}
        if kind == "confirm_receipt":
            s.log("desktop", kind, crate)
            if crate == v["open"]:
                s.complete("double_click_open")
                return s.say("receipt", f"Receipt for {crate} confirmed ✓")
            return s.futility("receipt", f"Confirmed {crate} — the receipt to confirm is {v['open']}.")
        if kind == "load_truck":
            s.log("desktop", kind, crate)
            if crate == v["drag"]:
                s.complete("cross_window_drag")
                return s.say("truck", f"{crate} loaded onto the truck ✓")
            return s.futility("truck", f"Loaded {crate} — the crate for this truck is {v['drag']}. It was unloaded again.")
        if kind == "load_line":
            line = norm(data.get("line")).upper()
            s.log("desktop", kind, line)
            if line == v["manifest_target"]:
                s.complete("wheel_select")
                return s.say("line", f"{line} loaded ✓")
            return s.futility("line", f"Loaded {line or 'nothing'} — the line is {v['manifest_target']}.")
        if kind == "quote":
            text = squash(data.get("text"))
            s.log("desktop", kind, text[:40])
            if text == v["phrase"]:
                s.complete("text_select")
                return s.say("quote", "Phrase quoted ✓")
            return s.futility("quote", f"The selection was «{text[:60] or 'empty'}» — select exactly «{v['phrase']}».")
        if kind == "save_shortcut":
            s.log("desktop", kind)
            s.complete("accelerator")
            s.say("save", "Manifest saved with Ctrl+S ✓")
            return {"ok": True}
        if kind == "import":
            raw = str(data.get("path") or "")
            s.log("desktop", kind, Path(raw).name if raw else "(cancelled)")
            if not raw:
                return {"ok": False, "msg": "Import cancelled."}
            try:
                digest = sha(Path(raw).read_bytes())
            except OSError:
                return s.futility("import", "That file could not be read.") | {"msg": "File could not be read."}
            if digest == v["file_sha"]:
                s.complete("open_dialog")
                s.say("import", f"{Path(raw).name} imported ✓")
                return {"ok": True, "msg": f"Imported {Path(raw).name}"}
            return s.futility("import", f"Imported {Path(raw).name} — the packing list is {v['file']} in the mission inbox.") | {
                "msg": f"Imported {Path(raw).name}"
            }
        if kind == "close":
            window = str(data.get("window") or "")
            s.log("desktop", "close_window", window)
            return {"ok": True, "close": True}
        return super().ui(kind, data)


MISSIONS: dict[str, type[Mission]] = {
    cls.name: cls
    for cls in (Clicks, Keys, TextEditing, SelectionClipboard, Drag, ScrollZoom, FormControls, BrowserUi, Windows, NativeApp)
}
