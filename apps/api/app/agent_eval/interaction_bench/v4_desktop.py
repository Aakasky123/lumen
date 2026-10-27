"""Native desktop windows for Interaction Gauntlet generation 4 (stdlib tkinter only).

Two missions live on the desktop. ``v4_windows`` is window management itself — every window here exists
to be moved, resized, maximized, minimized, snapped, closed, or found; each reports its raw geometry
(client rectangle, state, screen size, DPI scale) once it has been still for a moment, and the mission
judges it. ``v4_native`` is an ordinary legacy line-of-business app (cascade menus, a right-click menu,
double-click, a drag from one of its windows onto another, a long list, read-only text, an accelerator,
and the OS Open dialog).

As in generation 3, Tk draws its own widgets, so UI Automation sees little beyond title bars, menu bars,
and native dialogs: an agent works these windows by sight and the OS mouse and keyboard.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

try:  # package import inside the repo; flat import in the standalone bundle
    from .v3_desktop import BG, FONT, FONT_BIG, FONT_BOLD, FONT_HEAD, FONT_SMALL, INK, MUTED, PANEL, View
except ImportError:  # pragma: no cover - standalone bundle
    from v3_desktop import BG, FONT, FONT_BIG, FONT_BOLD, FONT_HEAD, FONT_SMALL, INK, MUTED, PANEL, View  # type: ignore

SETTLE_MS = 700  # a window is judged once it has stopped moving for this long


class SettlingView(View):
    """A window that reports its geometry after every move, resize, minimize, or restore settles."""

    def after_build(self) -> None:
        self._settle_job: Any = None
        self.win.bind("<Configure>", self._changed, add="+")
        self.win.bind("<Map>", self._changed, add="+")
        self.win.bind("<Unmap>", self._changed, add="+")
        super().after_build()

    def _changed(self, event: Any) -> None:
        if event.widget is not self.win or not self.alive():
            return
        self.on_change()
        if self._settle_job is not None:
            self.win.after_cancel(self._settle_job)
        self._settle_job = self.win.after(SETTLE_MS, self._settled)

    def _settled(self) -> None:
        self._settle_job = None
        if self.alive():
            self.settled()

    def on_change(self) -> None:
        """Live feedback while the window changes (redraw a size readout, say)."""

    def settled(self) -> None:
        """The window has been still for SETTLE_MS."""

    def rect(self) -> dict[str, int]:
        w = self.win
        return {"x": w.winfo_rootx(), "y": w.winfo_rooty(), "w": w.winfo_width(), "h": w.winfo_height()}

    def body_text(self, text: str, **kw: Any) -> Any:
        label = self.label(self.win, text, wraplength=self.host.px(kw.pop("wrap", 420)), **kw)
        label.pack(anchor="w", padx=16, pady=(12, 0))
        return label


# =============================================================================================
# v4_windows — move, resize, maximize, minimize, snap, close, find
# =============================================================================================


class DockFrame(SettlingView):
    title = "Dock frame"
    size = (480, 400)
    SLOT = (40, 70, 440, 370)  # logical canvas coordinates of the dashed slot

    def build(self) -> None:
        self.header("DOCK FRAME", "#3d5a80")
        canvas = self.host.tk.Canvas(self.win, bg="#f4f6f8", highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        self.reg("canvas", canvas)
        px = self.host.px
        x1, y1, x2, y2 = (px(n) for n in self.SLOT)
        canvas.create_rectangle(x1, y1, x2, y2, outline="#3d5a80", width=3, dash=(8, 6))
        canvas.create_text(px(240), px(36), text="Move the Gauge window inside the dashed frame.", font=FONT_BOLD, fill=INK)

    def position(self) -> tuple[int, int]:
        return self.host.screen_w - self.host.px(560), self.host.px(110)

    def slot_rect(self) -> dict[str, int]:
        canvas = self.widgets["canvas"]
        px = self.host.px
        x1, y1, x2, y2 = (px(n) for n in self.SLOT)
        return {"x": canvas.winfo_rootx() + x1, "y": canvas.winfo_rooty() + y1, "w": x2 - x1, "h": y2 - y1}

    def settled(self) -> None:
        gauge = self.host.view(self.s.sid, "win.gauge")
        if gauge is not None:
            gauge.report()


class Gauge(SettlingView):
    title = "Gauge"
    size = (300, 190)

    def build(self) -> None:
        tk = self.host.tk
        px = self.host.px
        canvas = tk.Canvas(self.win, bg=PANEL, highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        cx, cy, r = px(150), px(110), px(70)
        canvas.create_arc(cx - r, cy - r, cx + r, cy + r, start=0, extent=180, style="arc", width=6, outline="#3d5a80")
        a = math.radians(130)
        canvas.create_line(cx, cy, cx + r * 0.85 * math.cos(a), cy - r * 0.85 * math.sin(a), width=4, fill="#b42318")
        canvas.create_text(cx, px(22), text="PRESSURE GAUGE", font=FONT_BOLD, fill=INK)
        canvas.create_text(cx, px(150), text="Drag me by my title bar.", font=FONT_SMALL, fill=MUTED)

    def position(self) -> tuple[int, int]:
        return self.host.px(140), self.host.px(260)

    def settled(self) -> None:
        self.report()

    def report(self) -> None:
        dock = self.host.view(self.s.sid, "win.dock")
        if dock is None or self.win.state() != "normal":
            return
        self.ui("docked", gauge=self.rect(), slot=dock.slot_rect(), scale=self.host.scale)


class Sizer(SettlingView):
    title = "Sizer"
    size = (520, 330)

    def build(self) -> None:
        self.header("SIZER", "#2f5f4a")
        px = self.host.px
        tol = self.v["size_tol"]
        w, h = self.v["size_w"], self.v["size_h"]
        self.target = (px(w - tol), px(w + tol), px(h - tol), px(h + tol))
        self.body_text("Resize this window by dragging its edges or corner — do not maximize it.")
        self.reg("target", self.body_text(
            f"Target: width {self.target[0]}–{self.target[1]} px, height {self.target[2]}–{self.target[3]} px", font=FONT_BOLD
        ))
        self.reg("now", self.body_text("Current size: …", font=FONT_BIG))
        self.reg("status", self.body_text("", fg="#2b4c7e"))

    def position(self) -> tuple[int, int]:
        return self.host.px(120), self.host.px(120)

    def on_change(self) -> None:
        w, h = self.win.winfo_width(), self.win.winfo_height()
        state = self.win.state()
        suffix = "  (maximized — restore it first)" if state == "zoomed" else ""
        self.widgets["now"].config(text=f"Current size: {w} × {h} px{suffix}")

    def settled(self) -> None:
        r = self.rect()
        result = self.ui("sized", w=r["w"], h=r["h"], state=self.win.state(), scale=self.host.scale)
        self.widgets["status"].config(text="Size locked ✓" if result.get("ok") else "")


class Ledger(SettlingView):
    title = "Ledger"
    size = (560, 340)
    COLUMNS = ("Date", "Account", "Memo", "Debit", "Credit", "Balance", "Reference", "Reviewed by", "Total")

    def build(self) -> None:
        self.header("LEDGER   ·   FY2027 Q2", "#6b3a1f")
        canvas = self.host.tk.Canvas(self.win, bg="#ffffff", highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        self.reg("table", canvas)
        canvas.bind("<Configure>", lambda _e: self.redraw())

    def wide(self) -> bool:
        return self.win.winfo_width() >= int(self.host.screen_w * 0.85)

    def redraw(self) -> None:
        canvas = self.widgets["table"]
        canvas.delete("all")
        px = self.host.px
        col_w = px(130)
        rows = [("04-02", "1100", "Opening", "—", "—", "12,400.00", "LG-1", "AR")]
        rows += [(f"04-{3 + i:02d}", str(1100 + 7 * i), "Transfer", f"{120 + 33 * i}.00", "—", "…", f"LG-{2 + i}", "AR") for i in range(6)]
        for c, name in enumerate(self.COLUMNS[:-1]):
            canvas.create_text(px(12) + c * col_w, px(16), text=name, anchor="w", font=FONT_BOLD, fill=INK)
            for r, row in enumerate(rows):
                canvas.create_text(px(12) + c * col_w, px(42) + r * px(24), text=row[c], anchor="w", font=FONT, fill=INK)
        self.total_item = None
        if self.wide():
            # right-aligned against the canvas edge, so the total fits at any screen width and DPI
            x = int(canvas.winfo_width()) - px(14)
            canvas.create_text(x, px(16), text="Total", anchor="e", font=FONT_BOLD, fill="#b42318")
            self.total_item = canvas.create_text(x, px(42), text=self.v["ledger_total"], anchor="ne", font=FONT_BIG, fill="#b42318")
        else:
            canvas.create_text(
                int(canvas.winfo_width()) - px(10), px(16), anchor="e", fill="#b42318", font=FONT_BOLD,
                text="Total → (hidden: this window is too narrow — maximize it)",
            )

    def settled(self) -> None:
        self.ui("ledger_geometry", state=self.win.state(), wide=self.wide())

    def dbg_total_bbox(self) -> dict[str, Any]:
        canvas = self.widgets["table"]
        item = getattr(self, "total_item", None)
        return {"bbox": list(canvas.bbox(item)) if item else None, "width": int(canvas.winfo_width())}

    def dbg_total_bbox(self) -> dict[str, Any]:
        canvas = self.widgets["table"]
        item = getattr(self, "total_item", None)
        return {"bbox": list(canvas.bbox(item)) if item else None, "width": int(canvas.winfo_width())}


class Inbox(SettlingView):
    title = "Inbox — approvals"
    size = (520, 300)

    def build(self) -> None:
        self.header("APPROVALS", "#23415f")
        self.body_text("Purchase request PR-2207 · 14 replacement filters · $412.00\nRequested by Facilities. Press Approve.")
        row = self.host.tk.Frame(self.win, bg=BG)
        row.pack(anchor="w", padx=16, pady=(28, 0))
        self.reg("approve", self.button(row, "Approve", self.on_approve)).pack(side="left")
        self.reg("status", self.label(row, "", fg="#2b4c7e")).pack(side="left", padx=12)

    def position(self) -> tuple[int, int]:
        return self.host.px(180), self.host.px(200)

    def on_approve(self) -> None:
        chat = self.host.view(self.s.sid, "win.chat")
        state = "closed" if chat is None else str(chat.win.state())
        result = self.ui("approve", chat_state=state)
        self.widgets["status"].config(text=result.get("msg", "")[:60])


class Chat(SettlingView):
    title = "Chat — call in progress"
    size = (420, 260)

    def build(self) -> None:
        self.win.attributes("-topmost", True)
        self.header("CALL   ·   00:42", "#8a2f2f")
        self.body_text("You are on a call with Facilities. Do not close this window — closing it ends the call. "
                       "Minimize it to get it out of the way.", wrap=380)

    def position(self) -> tuple[int, int]:
        inbox = self.host.view(self.s.sid, "win.inbox")
        if inbox is not None:
            inbox.win.update_idletasks()
            return inbox.win.winfo_rootx() - self.host.px(30), inbox.win.winfo_rooty() + self.host.px(70)
        return self.host.px(150), self.host.px(270)

    def _changed(self, event: Any) -> None:
        if event.widget is self.win and self.alive() and event.type in (self.host.tk.EventType.Map, self.host.tk.EventType.Unmap):
            self.ui("chat_state", state=self.win.state())
        super()._changed(event)


class Notes(SettlingView):
    title = "Notes"
    size = (480, 340)

    def build(self) -> None:
        side = self.v["snap_side"]
        self.header("NOTES", "#4a3b6b")
        self.body_text(f"Snap this window to the {side.upper()} half of the screen — Windows key + {'Left' if side == 'left' else 'Right'} "
                       "arrow, or drag its title bar against that screen edge.", font=FONT_BOLD)
        self.reg("status", self.body_text("", fg="#2b4c7e"))

    def position(self) -> tuple[int, int]:
        return int(self.host.screen_w * 0.3), self.host.px(160)

    def settled(self) -> None:
        r = self.rect()
        result = self.ui("snap_geometry", **r, state=self.win.state(), screen_w=self.host.screen_w,
                         screen_h=self.host.screen_h, scale=self.host.scale)
        self.widgets["status"].config(text="Snapped ✓" if result.get("ok") else "")


class Tip(View):
    size = (380, 200)

    def build(self) -> None:
        n = int(self.name[-1])
        self.win.title(f"Tip of the day ({n} of 3)")
        self.header("TIP OF THE DAY", "#5f6b78")
        tips = ("Press F2 to rename a file.", "Ctrl+Shift+T reopens a closed tab.", "Win+D shows the desktop.")
        self.label(self.win, tips[n - 1], font=FONT_BOLD).pack(anchor="w", padx=16, pady=14)
        self.reg("close", self.button(self.win, "Close", self.request_close)).pack(anchor="e", padx=16)

    def position(self) -> tuple[int, int]:
        n = int(self.name[-1])
        return self.host.px(200 + 60 * n), self.host.px(150 + 50 * n)


class WorkLog(View):
    title = "Work log — unsaved"
    size = (380, 200)

    def build(self) -> None:
        self.header("WORK LOG", "#5f6b78")
        self.label(self.win, "Unsaved shift notes: 3 entries. Keep this window open.", font=FONT_BOLD).pack(anchor="w", padx=16, pady=14)
        self.reg("close", self.button(self.win, "Close", self.request_close)).pack(anchor="e", padx=16)

    def position(self) -> tuple[int, int]:
        return self.host.px(230), self.host.px(170)


class Beacon(View):
    title = "Beacon"
    size = (360, 190)

    def build(self) -> None:
        self.header("BEACON", "#1a7f37")
        self.label(self.win, "A beacon is waiting. Acknowledge it.", font=FONT_BOLD).pack(anchor="w", padx=16, pady=14)
        row = self.host.tk.Frame(self.win, bg=BG)
        row.pack(anchor="w", padx=16)
        self.reg("ack", self.button(row, "Acknowledge", self.on_ack)).pack(side="left")
        self.reg("status", self.label(row, "", fg="#2b4c7e")).pack(side="left", padx=10)

    def position(self) -> tuple[int, int]:
        return int(self.host.screen_w * 0.42), int(self.host.screen_h * 0.36)

    def on_ack(self) -> None:
        cover = self.host.view(self.s.sid, "win.cover")
        result = self.ui("acknowledge", cover_alive=cover is not None)
        self.widgets["status"].config(text="Acknowledged ✓" if result.get("ok") else "")


class Cover(View):
    title = "Maintenance overlay"
    size = (760, 500)

    def build(self) -> None:
        self.header("MAINTENANCE IN PROGRESS", "#a3261d")
        self.label(self.win, "Maintenance is running. Do not close this window.", font=FONT_HEAD).pack(anchor="w", padx=18, pady=18)

    def position(self) -> tuple[int, int]:
        beacon = self.host.view(self.s.sid, "win.beacon")
        if beacon is not None:
            beacon.win.update_idletasks()
            return beacon.win.winfo_rootx() - self.host.px(170), beacon.win.winfo_rooty() - self.host.px(150)
        return int(self.host.screen_w * 0.3), int(self.host.screen_h * 0.2)


# =============================================================================================
# v4_native — Warehouse Manager, its crate detail window, and the Truck Loader
# =============================================================================================


class Warehouse(View):
    title = "Warehouse Manager"
    size = (820, 640)

    def build(self) -> None:
        tk = self.host.tk
        self.units = tk.StringVar(value="Imperial")
        self.density = tk.StringVar(value="Comfortable")
        bar = tk.Menu(self.win)
        file_menu = tk.Menu(bar, tearoff=0)
        file_menu.add_command(label="Import packing list…", command=self.on_import)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.request_close)
        view_menu = tk.Menu(bar, tearoff=0)
        units = tk.Menu(view_menu, tearoff=0)
        for value in ("Imperial", "Metric"):
            units.add_radiobutton(label=value, variable=self.units, value=value, command=self.on_units)
        density = tk.Menu(view_menu, tearoff=0)
        for value in ("Compact", "Comfortable"):
            density.add_radiobutton(label=value, variable=self.density, value=value)
        view_menu.add_cascade(label="Units", menu=units)
        view_menu.add_cascade(label="Density", menu=density)
        help_menu = tk.Menu(bar, tearoff=0)
        help_menu.add_command(label="About", command=lambda: self.host.show_info(self.s, self.win, "About", "Warehouse Manager 2.3"))
        bar.add_cascade(label="File", menu=file_menu)
        bar.add_cascade(label="View", menu=view_menu)
        bar.add_cascade(label="Help", menu=help_menu)
        self.win.config(menu=bar)
        self.menus.update({"File": file_menu, "View": view_menu, "View>Units": units, "View>Density": density, "Help": help_menu})

        self.header("WAREHOUSE MANAGER   ·   DEPOT 4", "#23415f")
        top = tk.Frame(self.win, bg=BG)
        top.pack(fill="both", expand=True, padx=12, pady=8)
        left = tk.Frame(top, bg=BG)
        left.pack(side="left", fill="y")
        self.label(left, "Crates (right-click for actions · double-click to open · drag onto the truck)", fg=MUTED, font=FONT_SMALL).pack(anchor="w")
        crates = tk.Listbox(left, height=12, width=34, font=("Consolas", 11), activestyle="none", exportselection=False)
        kinds = ("bearings", "valves", "filters", "gaskets", "pumps", "fittings")
        for i, crate in enumerate(self.v["crates"]):
            crates.insert("end", f"{crate}   {kinds[i % len(kinds)]}")
        crates.pack(anchor="w", pady=(2, 0))
        self.reg("crates", crates)
        crates.bind("<Button-3>", self.on_right_click)
        crates.bind("<Double-Button-1>", self.on_double)
        crates.bind("<Return>", self.on_double)
        crates.bind("<ButtonPress-1>", self.on_press, add="+")
        crates.bind("<B1-Motion>", self.on_motion, add="+")
        crates.bind("<ButtonRelease-1>", self.on_release, add="+")
        self.ctx = tk.Menu(self.win, tearoff=0)
        self.ctx.add_command(label="Flag for inspection", command=lambda: self.crate_action("Flag for inspection"))
        self.ctx.add_command(label="Mark damaged", command=lambda: self.crate_action("Mark damaged"))
        self.ctx.add_separator()
        self.ctx.add_command(label="Remove crate", command=lambda: self.crate_action("Remove crate"))
        self.ctx_crate = ""
        self.drag: dict[str, Any] | None = None

        right = tk.Frame(top, bg=BG)
        right.pack(side="left", fill="both", expand=True, padx=(16, 0))
        self.label(right, "Manifest lines", fg=MUTED, font=FONT_SMALL).pack(anchor="w")
        frame = tk.Frame(right, bg=BG)
        frame.pack(anchor="w", fill="x")
        lines = tk.Listbox(frame, height=10, width=24, font=("Consolas", 11), exportselection=False)
        scroll = tk.Scrollbar(frame, orient="vertical", command=lines.yview)
        lines.config(yscrollcommand=scroll.set)
        for line in self.v["manifest"]:
            lines.insert("end", line)
        lines.pack(side="left")
        scroll.pack(side="left", fill="y")
        self.reg("lines", lines)
        self.reg("load", self.button(right, "Load line", self.on_load_line)).pack(anchor="w", pady=6)

        notes = tk.Text(self.win, height=4, wrap="word", font=FONT, bg="#fffdf5", relief="solid", bd=1)
        notes.insert("1.0", self.v["paragraph"])
        notes.configure(state="disabled")
        notes.pack(fill="x", padx=12, pady=(4, 2))
        self.reg("notes", notes)
        row = tk.Frame(self.win, bg=BG)
        row.pack(fill="x", padx=12)
        self.reg("quote", self.button(row, "Quote selection", self.on_quote)).pack(side="left")
        self.reg("status", self.label(self.win, "Units: Imperial   ·   unsaved changes", fg="#2b4c7e", font=FONT_SMALL)).pack(
            side="bottom", fill="x", padx=12, pady=6
        )
        for seq in ("<Control-s>", "<Control-S>"):
            self.win.bind(seq, self.on_ctrl_s)

    def position(self) -> tuple[int, int]:
        return self.host.px(90), self.host.px(70)

    def status(self, text: str) -> None:
        self.widgets["status"].config(text=text)

    # menus ------------------------------------------------------------------------------------
    def on_units(self) -> None:
        result = self.ui("units", value=self.units.get())
        self.status(f"Units: {self.units.get()}   ·   " + result.get("msg", ""))

    def on_import(self) -> None:
        path = self.host.ask_open_path(self.s, self.win, "Import packing list", str(Path.home()), [("CSV files", "*.csv"), ("All files", "*.*")])
        result = self.ui("import", path=path)
        self.status(result.get("msg", ""))

    # crate list -------------------------------------------------------------------------------
    def crate_at(self, y: int) -> str:
        lb = self.widgets["crates"]
        index = lb.nearest(y)
        if index < 0:
            return ""
        return str(lb.get(index)).split()[0]

    def on_right_click(self, event: Any) -> None:
        lb = self.widgets["crates"]
        index = lb.nearest(event.y)
        lb.selection_clear(0, "end")
        lb.selection_set(index)
        self.ctx_crate = self.crate_at(event.y)
        try:
            self.ctx.tk_popup(event.x_root, event.y_root)
        finally:
            self.ctx.grab_release()

    def crate_action(self, action: str, crate: str | None = None) -> dict[str, Any]:
        result = self.ui("crate_menu", crate=crate or self.ctx_crate, action=action)
        self.status(result.get("msg", ""))
        return result

    def on_double(self, event: Any) -> None:
        lb = self.widgets["crates"]
        sel = lb.curselection()
        if sel:
            self.open_detail(str(lb.get(sel[0])).split()[0])

    def open_detail(self, crate: str) -> None:
        view = self.host.open_view(self.s, "native.detail")
        view.show_crate(crate)

    def on_press(self, event: Any) -> None:
        self.drag = {"crate": self.crate_at(event.y), "x": event.x_root, "y": event.y_root, "live": False}

    def on_motion(self, event: Any) -> None:
        if self.drag and not self.drag["live"] and math.hypot(event.x_root - self.drag["x"], event.y_root - self.drag["y"]) > 12:
            self.drag["live"] = True
            self.widgets["crates"].config(cursor="fleur")

    def on_release(self, event: Any) -> None:
        drag, self.drag = self.drag, None
        self.widgets["crates"].config(cursor="")
        if not drag or not drag["live"]:
            return
        target = self.win.winfo_containing(event.x_root, event.y_root)
        truck = self.host.view(self.s.sid, "native.truck")
        bay = str(truck.widgets["bay"]) if truck is not None else ""
        if bay and target is not None and (str(target) == bay or str(target).startswith(bay + ".")):
            self.drop(drag["crate"])
        else:
            self.status(f"{drag['crate']} was dropped outside the truck's cargo bay.")

    def drop(self, crate: str) -> None:
        result = self.ui("load_truck", crate=crate)
        truck = self.host.view(self.s.sid, "native.truck")
        if truck is not None:
            truck.loaded(crate, bool(result.get("ok")))
        self.status(result.get("msg", ""))

    # other controls ---------------------------------------------------------------------------
    def on_load_line(self) -> None:
        lb = self.widgets["lines"]
        sel = lb.curselection()
        result = self.ui("load_line", line=str(lb.get(sel[0])) if sel else "")
        self.status(result.get("msg", ""))

    def on_quote(self) -> None:
        text = self.widgets["notes"]
        try:
            selected = text.get("sel.first", "sel.last")
        except self.host.tk.TclError:
            selected = ""
        result = self.ui("quote", text=selected)
        self.status(result.get("msg", ""))

    def on_ctrl_s(self, _event: Any = None) -> str:
        result = self.ui("save_shortcut")
        self.status("Manifest saved.   " + result.get("msg", ""))
        return "break"

    # validator-only drivers (harness --debug-hooks), each the same path a real gesture takes ---
    def dbg_menu(self, path: list[str]) -> bool:
        *parents, item = path
        menu = self.menus[">".join(parents)]
        for index in range(int(menu.index("end")) + 1):
            if menu.type(index) in ("command", "radiobutton", "checkbutton") and menu.entrycget(index, "label") == item:
                menu.invoke(index)
                return True
        raise KeyError(f"no menu item {' > '.join(path)}")

    def dbg_crate_action(self, crate: str, action: str) -> dict[str, Any]:
        self.ctx_crate = crate
        return self.crate_action(action)

    def dbg_open_detail(self, crate: str) -> bool:
        self.open_detail(crate)
        return True

    def dbg_drop(self, crate: str) -> bool:
        self.drop(crate)
        return True

    def dbg_select_line(self, line: str) -> bool:
        lb = self.widgets["lines"]
        index = list(lb.get(0, "end")).index(line)
        lb.selection_clear(0, "end")
        lb.selection_set(index)
        lb.see(index)
        return True

    def dbg_select_text(self, phrase: str) -> bool:
        text = self.widgets["notes"]
        start = text.search(phrase, "1.0")
        text.tag_remove("sel", "1.0", "end")
        text.tag_add("sel", start, f"{start}+{len(phrase)}c")
        return True

    def dbg_ctrl_s(self) -> bool:
        self.on_ctrl_s()
        return True

    def dbg_row_center(self, crate: str) -> list[int]:
        """Screen coordinates of a crate row (for a real-mouse drag test)."""
        lb = self.widgets["crates"]
        index = next(i for i, line in enumerate(lb.get(0, "end")) if str(line).split()[0] == crate)
        lb.see(index)
        lb.update_idletasks()
        x, y, w, h = lb.bbox(index)
        return [lb.winfo_rootx() + x + min(w, 80) // 2, lb.winfo_rooty() + y + h // 2]


class CrateDetail(View):
    title = "Crate detail"
    size = (380, 220)

    def build(self) -> None:
        self.crate = ""
        self.header("CRATE DETAIL", "#4a3b6b")
        self.reg("crate", self.label(self.win, "", font=FONT_BIG))
        self.widgets["crate"].pack(anchor="w", padx=16, pady=(10, 0))
        self.reg("seal", self.label(self.win, "", font=FONT_BOLD))
        self.widgets["seal"].pack(anchor="w", padx=16)
        row = self.host.tk.Frame(self.win, bg=BG)
        row.pack(anchor="w", padx=16, pady=12)
        self.reg("confirm", self.button(row, "Confirm receipt", self.on_confirm)).pack(side="left")
        self.reg("status", self.label(row, "", fg="#2b4c7e")).pack(side="left", padx=10)

    def position(self) -> tuple[int, int]:
        return self.host.px(520), self.host.px(260)

    def show_crate(self, crate: str) -> None:
        self.crate = crate
        result = self.ui("detail_opened", crate=crate)
        self.win.title(f"Crate detail — {crate}")
        self.widgets["crate"].config(text=crate)
        self.widgets["seal"].config(text=f"Seal: {result.get('seal', '?')}")
        self.widgets["status"].config(text="")
        self.present()

    def on_confirm(self) -> None:
        result = self.ui("confirm_receipt", crate=self.crate)
        self.widgets["status"].config(text=result.get("msg", "")[:48])


class Truck(View):
    title = "Truck Loader — Bay 2"
    size = (420, 320)

    def build(self) -> None:
        self.header("TRUCK LOADER   ·   BAY 2", "#2f5f4a")
        bay = self.host.tk.Canvas(self.win, bg="#eef3ea", highlightthickness=2, highlightbackground="#2f5f4a")
        bay.pack(fill="both", expand=True, padx=14, pady=12)
        self.reg("bay", bay)
        self.cargo: list[str] = []
        bay.bind("<Configure>", lambda _e: self.redraw())

    def position(self) -> tuple[int, int]:
        return self.host.screen_w - self.host.px(500), self.host.px(380)

    def redraw(self) -> None:
        bay = self.widgets["bay"]
        bay.delete("all")
        px = self.host.px
        bay.create_text(px(14), px(16), anchor="w", text="Cargo bay — drop crates here", font=FONT_BOLD, fill=INK)
        for i, crate in enumerate(self.cargo):
            bay.create_rectangle(px(14), px(40 + 34 * i), px(170), px(68 + 34 * i), fill="#c9b27c", outline="#7a6330")
            bay.create_text(px(24), px(54 + 34 * i), anchor="w", text=crate, font=("Consolas", 11, "bold"))

    def dbg_bay_center(self) -> list[int]:
        bay = self.widgets["bay"]
        return [bay.winfo_rootx() + bay.winfo_width() // 2, bay.winfo_rooty() + bay.winfo_height() // 2]

    def loaded(self, crate: str, ok: bool) -> None:
        if ok and crate not in self.cargo:
            self.cargo.append(crate)
        self.redraw()


VIEWS: dict[str, type[View]] = {
    "win.dock": DockFrame,
    "win.gauge": Gauge,
    "win.sizer": Sizer,
    "win.ledger": Ledger,
    "win.inbox": Inbox,
    "win.chat": Chat,
    "win.notes": Notes,
    "win.tip1": Tip,
    "win.tip2": Tip,
    "win.tip3": Tip,
    "win.worklog": WorkLog,
    "win.beacon": Beacon,
    "win.cover": Cover,
    "native.warehouse": Warehouse,
    "native.detail": CrateDetail,
    "native.truck": Truck,
}
