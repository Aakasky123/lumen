"""Golden-path validator for Interaction Gauntlet generation 4 (the human-input atlas).

Like validate_v3.py this proves the benchmark, not an agent: every page is solvable with real input,
its token appears only after genuine completion, and its traps poison the page. Browser challenges are
driven with real Playwright input (trusted events: clicks of every button, holds, hovers, drags, wheels,
keys and chords, selections, the clipboard, tabs, the address bar's equivalent, history). Native windows
run in the harness's ghost mode and are driven through its debug hooks — the same handlers a real
click, drag, or key reaches — with native dialogs answered from a scripted queue.

    python -m app.agent_eval.interaction_bench.validate_v4 [--think 5] [--only v4_clicks,...] [--headed]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import BrowserContext, Page

HERE = Path(__file__).resolve().parent
try:  # package import inside the repo; flat import in the standalone bundle
    from .v4_missions import MISSIONS
    from .validate_v3 import HarnessProc, assert_fail, done, goto, public, run_checks, think, wait_step
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(HERE))
    from v4_missions import MISSIONS  # type: ignore
    from validate_v3 import HarnessProc, assert_fail, done, goto, public, run_checks, think, wait_step  # type: ignore


def assert_pass(page: Page, name: str) -> None:
    token = MISSIONS[name].token
    page.wait_for_function(
        "tok => document.getElementById('bench-verdict').textContent.includes(tok)", arg=token, timeout=10000
    )


def center(page: Page, selector: str) -> tuple[float, float]:
    page.locator(selector).scroll_into_view_if_needed()
    box = page.locator(selector).bounding_box()
    assert box, f"{selector} is not visible"
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


# ------------------------------------------------------------------------------------- clicks


def solve_v4_clicks(page: Page, ctx: BrowserContext, h: HarnessProc) -> None:
    goto(page, h, "v4_clicks")
    pub = public(page)
    think()
    page.click(f'[data-cell="{pub["cell"]}"]')
    wait_step(page, "grid_click")
    think()
    target = page.locator(f'.file[data-name="{pub["file"]}"]')
    target.click()
    assert "double_open" not in done(page), "a single click only selects"
    target.dblclick()
    wait_step(page, "double_open")
    think()
    page.click(f'.mail[data-sender="{pub["sender"]}"]', button="right")
    page.locator("#ctxmenu div", has_text="Archive").click()
    wait_step(page, "context_menu")
    think()
    page.click("#notes")  # a left click is refused...
    page.wait_for_function("() => (V3.state().messages.middle || '').includes('middle mouse')", timeout=5000)
    with ctx.expect_page() as info:
        page.click("#notes", button="middle")  # ...a middle click opens a background tab
    info.value.wait_for_load_state()
    wait_step(page, "middle_click")
    page.bring_to_front()
    think()
    x, y = center(page, "#hold")
    page.mouse.move(x, y)
    page.mouse.down()
    time.sleep(pub["hold_low"] - 0.9)
    page.mouse.up()  # too early: ineffective, and says how long it was held
    page.wait_for_function("() => (V3.state().messages.hold || '').startsWith('Held')", timeout=5000)
    page.mouse.down()
    time.sleep(pub["hold_low"] + 0.5)
    page.mouse.up()
    wait_step(page, "hold_press")
    think()
    page.hover("#tip-icon")
    page.wait_for_function("() => document.getElementById('tip').textContent.includes('Tier code')", timeout=5000)
    code = page.inner_text("#tip").split(":")[-1].strip()
    page.mouse.move(5, 5)
    assert page.inner_text("#tip") == "", "the tooltip hides when the pointer leaves"
    page.fill("#tier", code)
    page.click("#tier-go")
    wait_step(page, "hover_reveal")
    think()
    delta = pub["qty_target"] - pub["qty_start"]
    for _ in range(abs(delta)):
        page.click("#plus" if delta > 0 else "#minus")
    page.click("#qty-go")
    assert_pass(page, "v4_clicks")


def trap_v4_delete_message(page: Page, ctx: BrowserContext, h: HarnessProc) -> None:
    goto(page, h, "v4_clicks")
    pub = public(page)
    page.click(f'.mail[data-sender="{pub["sender"]}"]', button="right")
    page.locator("#ctxmenu div", has_text="Delete").click()
    assert_fail(page)
    page.reload()
    assert_fail(page)


def check_v4_untrusted_input(page: Page, ctx: BrowserContext, h: HarnessProc) -> None:
    """Script-dispatched input never counts (a person's input is always trusted)."""
    goto(page, h, "v4_clicks")
    pub = public(page)
    page.evaluate(f"""() => document.querySelector('[data-cell="{pub["cell"]}"]').click()""")
    page.evaluate(
        f"""() => document.querySelector('.file[data-name="{pub["file"]}"]').dispatchEvent(new MouseEvent('dblclick', {{bubbles: true}}))"""
    )
    page.wait_for_function("() => V3.state().futile >= 2", timeout=5000)
    assert not done(page), "untrusted clicks completed a challenge"


# --------------------------------------------------------------------------------------- keys


def solve_v4_keys(page: Page, ctx: BrowserContext, h: HarnessProc) -> None:
    goto(page, h, "v4_keys")
    pub = public(page)
    think()
    page.click("#kb-start")
    for _ in range(len(pub["fields"]) - pub["fields"].index("Badge ID")):
        page.keyboard.press("Shift+Tab")
    assert page.evaluate("document.activeElement.dataset.field") == "Badge ID"
    page.keyboard.type(pub["badge"])
    page.keyboard.press("Shift+Tab")  # leaving the field commits it
    wait_step(page, "shift_tab")
    think()
    target = pub["grid_target"]
    page.click("#board .hd >> nth=0")  # focus the board without touching a cell
    dx, dy = "ABCDEFGHI".index(target[0]) - 4, int(target[1:]) - 1 - 4
    for _ in range(abs(dx)):
        page.keyboard.press("ArrowRight" if dx > 0 else "ArrowLeft")
    for _ in range(abs(dy)):
        page.keyboard.press("ArrowDown" if dy > 0 else "ArrowUp")
    page.keyboard.press("Enter")
    wait_step(page, "arrow_grid")
    think()
    key = next(k for cmd, k in pub["chords"] if cmd == pub["chord_target"])
    page.click("#thread")
    page.keyboard.press(f"Control+Shift+{key}")
    wait_step(page, "chord")
    think()
    page.click("#ov-open")
    page.keyboard.press("Escape")
    page.keyboard.press("Enter")
    wait_step(page, "escape_enter")
    think()
    page.click(f'#filelist li[data-name="{pub["rename_target"]}"]')
    page.keyboard.press("F2")
    page.keyboard.press("Control+A")
    page.keyboard.type(pub["rename_to"])
    page.keyboard.press("Enter")
    wait_step(page, "f2_rename")
    think()
    page.click("#cap")
    page.keyboard.down("Space")
    time.sleep((pub["hold_low"] + 10) / 25)
    page.keyboard.up("Space")
    wait_step(page, "key_hold")
    think()
    page.wait_for_function("() => document.querySelectorAll('#undolist li.gone').length === 3", timeout=8000)
    page.click("#undolist")
    for _ in range(3):
        page.keyboard.press("Control+z")  # one undo too many restores the deliberate removal...
    page.keyboard.press("Control+y")  # ...and redo takes it back out
    page.click("#undo-done")
    assert_pass(page, "v4_keys")


def check_v4_script_focus(page: Page, ctx: BrowserContext, h: HarnessProc) -> None:
    """A keyboard-only field refuses focus that did not arrive by Tab."""
    goto(page, h, "v4_keys")
    page.evaluate("""() => document.querySelector('[data-field="Badge ID"]').focus()""")
    page.wait_for_function("() => V3.state().futile >= 1", timeout=5000)
    assert page.evaluate("document.activeElement.dataset.field || ''") == ""


# --------------------------------------------------------------------------------------- text

WORD_RECT_JS = r"""([sel, word]) => {
  const root = document.querySelector(sel);
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const re = new RegExp('(^|[^\p{L}])(' + word + ')(?![\p{L}])', 'u');
    const m = re.exec(node.textContent);
    if (!m) continue;
    const range = document.createRange();
    range.setStart(node, m.index + m[1].length);
    range.setEnd(node, m.index + m[1].length + word.length);
    const r = range.getBoundingClientRect();
    return [r.x + r.width / 2, r.y + r.height / 2];
  }
  return null;
}"""


def select_in_field(page: Page, selector: str, start: int, end: int) -> None:
    page.focus(selector)
    page.evaluate("([sel, a, b]) => document.querySelector(sel).setSelectionRange(a, b)", [selector, start, end])


def solve_v4_text(page: Page, ctx: BrowserContext, h: HarnessProc) -> None:
    goto(page, h, "v4_text")
    pub = public(page)
    think()
    page.fill("#address", "\n".join(pub["address"]))
    page.click("#address-go")
    wait_step(page, "multiline")
    think()
    at = pub["sentence"].index(" " + pub["anchor"]) + 1
    select_in_field(page, "#sentence", at, at)
    page.keyboard.type(pub["insert_word"] + " ")
    page.click("#sentence-go")
    wait_step(page, "caret_insert")
    think()
    page.locator("#editor").scroll_into_view_if_needed()
    for word, chord in ((pub["bold_word"], "Control+b"), (pub["italic_word"], "Control+i")):
        point = page.evaluate(WORD_RECT_JS, ["#editor", word])
        assert point, word
        page.mouse.dblclick(*point)
        page.keyboard.press(chord)
    page.click("#rich-go")
    wait_step(page, "rich_format")
    think()
    config = "\n".join(pub["config"])
    start = config.index(pub["temp_line"])
    select_in_field(page, "#config", start, start + len(pub["temp_line"]) + 1)
    page.keyboard.press("Delete")
    page.click("#config-go")
    wait_step(page, "line_delete")
    think()
    page.fill("#special", pub["special"])
    page.click("#special-go")
    wait_step(page, "special_chars")
    think()
    page.fill("#city", pub["city"])  # typing the name is not choosing it...
    page.click("#city-go")
    page.wait_for_function("() => (V3.state().messages.city || '').includes('suggestion list')", timeout=5000)
    page.fill("#city", pub["city"][:4])
    shown = page.locator("#suggest li").all_inner_texts()
    assert pub["city"] in shown, shown
    for _ in range(shown.index(pub["city"]) + 1):
        page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    page.click("#city-go")
    wait_step(page, "autocomplete")
    think()
    lines = pub["deep_lines"]
    i = next(k for k, line in enumerate(lines) if line.startswith(pub["deep_record"] + " "))
    offset = sum(len(line) + 1 for line in lines[:i]) + lines[i].index("hold")
    select_in_field(page, "#deep", offset, offset + 4)
    page.keyboard.type("ready")
    page.click("#deep-go")
    assert_pass(page, "v4_text")


def check_v4_partial_bold(page: Page, ctx: BrowserContext, h: HarnessProc) -> None:
    """Half a word in bold is not a bold word."""
    goto(page, h, "v4_text")
    pub = public(page)
    page.evaluate(
        """(w) => { const ed = document.getElementById('editor'); ed.innerHTML = ed.textContent.replace(w, '<b>' + w.slice(0, 3) + '</b>' + w.slice(3)); }""",
        pub["bold_word"],
    )
    page.click("#rich-go")
    page.wait_for_function("() => (V3.state().messages.rich || '').includes('partly')", timeout=5000)
    assert "rich_format" not in done(page)


# ---------------------------------------------------------------------------------- selection

PHRASE_ENDS_JS = r"""([sel, phrase]) => {
  const node = document.querySelector(sel).firstChild;
  const at = node.textContent.indexOf(phrase);
  const range = document.createRange();
  range.setStart(node, at);
  range.setEnd(node, at + phrase.length);
  const rects = range.getClientRects();
  const a = rects[0], b = rects[rects.length - 1];
  return [a.left + 0.5, a.top + a.height / 2, b.right - 0.5, b.top + b.height / 2];
}"""


def solve_v4_selection(page: Page, ctx: BrowserContext, h: HarnessProc) -> None:
    ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin=h.base)
    goto(page, h, "v4_selection")
    pub = public(page)
    think()
    page.locator("#para").scroll_into_view_if_needed()
    x1, y1, x2, y2 = page.evaluate(PHRASE_ENDS_JS, ["#para", pub["phrase"]])
    page.mouse.move(x1, y1)
    page.mouse.down()
    page.mouse.move((x1 + x2) / 2, (y1 + y2) / 2, steps=5)
    page.mouse.move(x2, y2, steps=5)
    page.mouse.up()
    page.click("#quote-phrase")
    wait_step(page, "drag_select")
    think()
    para = f'.addr[data-idx="{pub["para_index"]}"]'
    page.click(para, click_count=2)  # a word, then...
    page.click("#quote-para")
    page.wait_for_function("() => (V3.state().messages.para || '').length > 0", timeout=5000)
    page.click(para, click_count=3)  # ...the whole paragraph
    page.click("#quote-para")
    wait_step(page, "triple_click")
    think()
    page.click(f'#lb-range .r[data-id="{pub["range_from"]}"]')
    page.click(f'#lb-range .r[data-id="{pub["range_to"]}"]', modifiers=["Shift"])
    page.click("#apply-range")
    wait_step(page, "shift_range")
    think()
    overdue = [a for a, st in zip(pub["accounts"], pub["statuses"]) if st == "Overdue"]
    for account in overdue:
        page.click(f'#lb-set .r[data-id="{account}"]', modifiers=["Control"])
    page.click("#apply-set")
    wait_step(page, "ctrl_set")
    think()
    page.locator("#mboard").scroll_into_view_if_needed()
    box = page.locator("#mboard").bounding_box()
    assert box
    corners = {"top-left": (16, 14, 214, 158), "top-right": (504, 14, 272, 158), "bottom-left": (16, 290, 214, 162), "bottom-right": (504, 290, 272, 162)}
    sx, sy, ex, ey = corners[pub["corner"]]
    page.mouse.move(box["x"] + sx, box["y"] + sy)
    page.mouse.down()
    page.mouse.move(box["x"] + ex, box["y"] + ey, steps=8)
    page.mouse.up()
    wait_step(page, "marquee")
    think()
    start = pub["order_code"].index(pub["order_part"])
    page.click("#order")
    page.keyboard.press("Home")
    for _ in range(start):
        page.keyboard.press("ArrowRight")
    for _ in range(len(pub["order_part"])):
        page.keyboard.press("Shift+ArrowRight")
    page.click("#use-sel")
    wait_step(page, "keyboard_select")
    think()
    page.click("#tok-dst")
    page.keyboard.type("ABC")
    assert page.input_value("#tok-dst") == "", "typed text must be refused"
    page.click("#tok-src")
    page.keyboard.press("Control+a")
    page.keyboard.press("Control+c")
    page.click("#tok-dst")
    page.keyboard.press("Control+v")
    wait_step(page, "copy_paste")
    think()
    page.click("#cut-src")
    page.keyboard.press("Control+a")
    page.keyboard.press("Control+x")
    page.click("#cut-dst")
    page.keyboard.press("Control+v")
    assert_pass(page, "v4_selection")


def check_v4_copy_is_not_cut(page: Page, ctx: BrowserContext, h: HarnessProc) -> None:
    """Copying the transfer code (leaving Draft full) is not moving it."""
    ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin=h.base)
    goto(page, h, "v4_selection")
    page.click("#cut-src")
    page.keyboard.press("Control+a")
    page.keyboard.press("Control+c")
    page.click("#cut-dst")
    page.keyboard.press("Control+v")
    page.wait_for_function("() => (V3.state().messages.cut || '').includes('without being cut')", timeout=5000)
    assert "cut_paste" not in done(page)


# --------------------------------------------------------------------------------------- drag


def drag_points(page: Page, start: tuple[float, float], end: tuple[float, float], *, hold: float = 0.0, steps: int = 12) -> None:
    page.mouse.move(*start)
    page.mouse.down()
    if hold:
        time.sleep(hold)
    page.mouse.move(*end, steps=steps)
    page.mouse.up()


def box_of(page: Page, selector: str) -> dict[str, float]:
    page.locator(selector).first.scroll_into_view_if_needed()
    box = page.locator(selector).first.bounding_box()
    assert box, f"{selector} is not visible"
    return box


def solve_v4_drag(page: Page, ctx: BrowserContext, h: HarnessProc) -> None:
    import math

    goto(page, h, "v4_drag")
    pub = public(page)
    think()
    page.drag_and_drop(f'.card5[data-card="{pub["card"]}"]', "#bin")
    wait_step(page, "html5_drop")
    think()
    for i, name in enumerate(pub["order_target"]):
        current = page.locator("#reorder li").evaluate_all("els => els.map(e => e.dataset.name)")
        if current[i] == name:
            continue
        src = box_of(page, f'#reorder li[data-name="{name}"]')
        dst = box_of(page, f"#reorder li >> nth={i}")
        drag_points(page, (src["x"] + 40, src["y"] + src["height"] / 2), (dst["x"] + 40, dst["y"] + 3))
    page.click("#order-go")
    wait_step(page, "reorder")
    think()
    page.locator("#board").scroll_into_view_if_needed()
    src = box_of(page, f'.kcard[data-name="{pub["kanban_move"]}"]')
    page.mouse.move(src["x"] + 30, src["y"] + src["height"] / 2)
    page.mouse.down()
    # Like a person: the board opens a gap where the card would land, which shifts the cards below it, so aim
    # at the live gap between the two named cards and let go once the gap sits there.
    review = '.col[data-col="Review"] .kcard[data-name="{}"]'
    for _ in range(5):
        after = page.locator(review.format(pub["kanban_after"])).bounding_box()
        before = page.locator(review.format(pub["kanban_before"])).bounding_box()
        page.mouse.move(before["x"] + 30, (after["y"] + after["height"] + before["y"]) / 2, steps=10)
        if page.evaluate("([a, b]) => { const s = document.querySelector('#board .kslot');"
                         " return !!s && s.previousElementSibling?.dataset.name === a && s.nextElementSibling?.dataset.name === b; }",
                         [pub["kanban_after"], pub["kanban_before"]]):
            break
    page.mouse.up()
    page.click("#board-go")
    wait_step(page, "kanban")
    think()
    track = box_of(page, "#track")
    for handle, value in (("#h-high", pub["range_high"]), ("#h-low", pub["range_low"])):
        hb = box_of(page, handle)
        drag_points(page, (hb["x"] + hb["width"] / 2, hb["y"] + hb["height"] / 2), (track["x"] + value * 4, hb["y"] + hb["height"] / 2))
    page.click("#range-go")
    wait_step(page, "dual_slider")
    think()
    split = box_of(page, "#split")
    divider = box_of(page, "#divider")
    target = pub["split_min"] + 10
    drag_points(page, (divider["x"] + 5, divider["y"] + 60), (split["x"] + target + 5, divider["y"] + 60))
    page.click("#split-go")
    wait_step(page, "splitter")
    think()
    tmpl = box_of(page, "#palette .tmpl")
    slot = box_of(page, f'.slot[data-slot="{pub["copy_slot"]}"]')
    start, end = (tmpl["x"] + 50, tmpl["y"] + 20), (slot["x"] + 60, slot["y"] + 30)
    drag_points(page, start, end)  # a plain drag moves the template out of the palette
    page.wait_for_function("() => (V3.state().messages.copy || '').includes('moved')", timeout=5000)
    page.click("#palette-reset")  # (clicking it may scroll the page: measure again)
    tmpl = box_of(page, "#palette .tmpl")
    slot = box_of(page, f'.slot[data-slot="{pub["copy_slot"]}"]')
    page.keyboard.down("Control")
    drag_points(page, (tmpl["x"] + 50, tmpl["y"] + 20), (slot["x"] + 60, slot["y"] + 30))
    page.keyboard.up("Control")
    wait_step(page, "copy_drag")
    think()
    item = box_of(page, f'#longlist li[data-name="{pub["long_target"]}"]')
    top = box_of(page, "#longlist li >> nth=0")
    drag_points(page, (item["x"] + 40, item["y"] + item["height"] / 2), (top["x"] + 40, top["y"] + 3))  # no hold: never lifts
    page.wait_for_function("() => (V3.state().messages.long || '').includes('never lifted')", timeout=5000)
    drag_points(page, (item["x"] + 40, item["y"] + item["height"] / 2), (top["x"] + 40, top["y"] + 3), hold=1.2)
    wait_step(page, "long_press_drag")
    think()
    canvas = box_of(page, "#lasso")
    rx, ry = pub["red"]
    radius = min(60.0, math.dist(pub["red"], pub["blue"]) * 0.55)
    pts = [(canvas["x"] + rx + radius * math.cos(a / 36 * 2 * math.pi), canvas["y"] + ry + radius * math.sin(a / 36 * 2 * math.pi)) for a in range(37)]
    page.mouse.move(*pts[0])
    page.mouse.down()
    for p in pts[1:]:
        page.mouse.move(*p)
    page.mouse.up()
    assert_pass(page, "v4_drag")


def check_v4_lasso_both_stars(page: Page, ctx: BrowserContext, h: HarnessProc) -> None:
    """A loop that also takes in the blue star does not count."""
    import math

    goto(page, h, "v4_drag")
    pub = public(page)
    canvas = box_of(page, "#lasso")
    cx, cy = (pub["red"][0] + pub["blue"][0]) / 2, (pub["red"][1] + pub["blue"][1]) / 2
    radius = math.dist(pub["red"], pub["blue"]) / 2 + 40
    page.mouse.move(canvas["x"] + cx + radius, canvas["y"] + cy)
    page.mouse.down()
    for a in range(1, 37):
        page.mouse.move(canvas["x"] + cx + radius * math.cos(a / 36 * 2 * math.pi), canvas["y"] + cy + radius * math.sin(a / 36 * 2 * math.pi))
    page.mouse.up()
    page.wait_for_function("() => (V3.state().messages.lasso || '').includes('blue')", timeout=5000)
    assert "lasso" not in done(page)


# ------------------------------------------------------------------------------------- scroll


def wheel_until_visible(page: Page, region: str, target: str, dx: float, dy: float, tries: int = 80) -> None:
    """Turn the wheel over a region until the target sits fully inside it (no scrollIntoView)."""
    box = box_of(page, region)
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    for _ in range(tries):
        inside = page.evaluate(
            """([r, t]) => { const el = document.querySelector(t); if (!el) return false;  // virtualized: not rendered yet
                 const a = document.querySelector(r).getBoundingClientRect(), b = el.getBoundingClientRect();
                 return b.left >= a.left && b.right <= a.right && b.top >= a.top && b.bottom <= a.bottom; }""",
            [region, target],
        )
        if inside:
            return
        page.mouse.wheel(dx, dy)
        page.wait_for_timeout(30)
    raise AssertionError(f"{target} never scrolled into {region}")


def pan_to(page: Page, wx: float, wy: float) -> tuple[float, float]:
    """Drag the map (in hops) until world point (wx, wy) is near the view's center; returns its page coords."""
    canvas = box_of(page, "#map")
    for _ in range(30):
        ox, oy, zoom = page.evaluate("[ox, oy, zoom]")
        sx, sy = (wx - ox) * zoom, (wy - oy) * zoom
        dx, dy = 320 - sx, 180 - sy
        if abs(dx) < 40 and abs(dy) < 40:
            break
        dx, dy = max(-250, min(250, dx)), max(-150, min(150, dy))
        drag_points(page, (canvas["x"] + 320, canvas["y"] + 180), (canvas["x"] + 320 + dx, canvas["y"] + 180 + dy), steps=6)
        after = page.evaluate("[ox, oy]")
        if after == [ox, oy]:
            break  # clamped at the map's edge
    ox, oy, zoom = page.evaluate("[ox, oy, zoom]")
    return canvas["x"] + (wx - ox) * zoom, canvas["y"] + (wy - oy) * zoom


def solve_v4_scroll(page: Page, ctx: BrowserContext, h: HarnessProc) -> None:
    goto(page, h, "v4_scroll")
    pub = public(page)
    truth = h.truth(page.evaluate("V3.sid()"))
    think()
    row_sel = f'#rows .rw[data-row="{pub["row_target"]}"]'
    wheel_until_visible(page, "#rows", row_sel, 0, 120, tries=120)  # a notch smaller than the panel
    page.click(f"{row_sel} button")
    wait_step(page, "inner_scroll")
    think()
    ev_sel = f'#timeline .ev[data-q="{pub["quarter"]}"]'
    wheel_until_visible(page, "#timeline", ev_sel, 400, 0)
    page.click(f"{ev_sel} button")
    wait_step(page, "horizontal")
    think()
    flag = next(f for f in pub["flags"] if f["id"] == pub["flag"])
    page.mouse.click(*pan_to(page, flag["x"], flag["y"]))
    wait_step(page, "pan_map")
    think()
    star = next(b for b in pub["buildings"] if b["star"])
    x, y = pan_to(page, star["x"], star["y"])
    page.mouse.move(x, y)
    page.keyboard.down("Control")
    page.mouse.wheel(0, -520)  # about five notches: past 3x
    page.keyboard.up("Control")
    assert page.evaluate("zoom") >= 3, "Ctrl+wheel must zoom the map"
    page.wait_for_function("() => Array.isArray(names) && names.length > 0", timeout=5000)  # labels arrive once zoomed
    page.fill("#bname", truth["building"])  # read off the zoomed map (the validator has no OCR)
    page.click("#bname-go")
    wait_step(page, "zoom_map")
    think()
    page.click("#doc")
    page.keyboard.press("End")
    page.wait_for_function("() => /Closing code: CL-/.test((document.querySelector('#doc .closing') || {}).textContent || '')", timeout=8000)
    code = page.inner_text("#doc .closing").split(":")[-1].strip()
    page.fill("#ccode", code)
    page.click("#ccode-go")
    assert_pass(page, "v4_scroll")


# ------------------------------------------------------------------------------------ widgets


def solve_v4_widgets(page: Page, ctx: BrowserContext, h: HarnessProc) -> None:
    sid = goto(page, h, "v4_widgets")
    pub = public(page)
    truth = h.truth(sid)
    think()
    page.select_option("#port", pub["port"])
    page.click("#port-go")
    wait_step(page, "native_select")
    think()
    page.fill("#date", truth["date"])
    page.click("#date-go")
    wait_step(page, "date_input")
    think()
    page.fill("#time", truth["time"])
    page.click("#time-go")
    wait_step(page, "time_input")
    think()
    page.fill("#color", pub["color"])
    page.click("#color-go")
    wait_step(page, "color_input")
    think()
    box = box_of(page, "#range")
    page.mouse.click(box["x"] + box["width"] * pub["range"] / 1000, box["y"] + box["height"] / 2)  # near the value...
    for _ in range(60):
        value = int(page.input_value("#range"))
        if value == pub["range"]:
            break
        page.keyboard.press("ArrowRight" if value < pub["range"] else "ArrowLeft")  # ...then exact with the keys
    page.click("#range-go")
    wait_step(page, "range_exact")
    think()
    page.select_option("#multi", pub["multi"])
    page.click("#multi-go")
    wait_step(page, "multi_select")
    think()
    inbox = Path(pub["inbox"])
    page.set_input_files("#file", str(inbox / pub["file"].replace(".csv", "-old.csv")))
    page.wait_for_function("() => (V3.state().messages.file || '').includes('is not')", timeout=5000)
    page.set_input_files("#file", str(inbox / pub["file"]))
    assert_pass(page, "v4_widgets")


# ------------------------------------------------------------------------------------ browser


def solve_v4_browser(page: Page, ctx: BrowserContext, h: HarnessProc) -> None:
    goto(page, h, "v4_browser")
    think()
    url = page.inner_text("#dest").strip()
    tab = ctx.new_page()
    tab.goto(url)  # a new tab, address typed into its bar
    wait_step(page, "typed_url")
    tab.close()
    think()
    with ctx.expect_page() as info:
        page.click("#vault")
    vault = info.value
    vault.wait_for_function("() => /V-[A-Z0-9]{5}/.test(document.getElementById('code').textContent)", timeout=10000)
    code = vault.inner_text("#code").strip()
    vault.close()
    page.bring_to_front()
    page.fill("#vcode", code)
    page.click("#vcode-go")
    wait_step(page, "tab_relay")
    think()
    page.click("#hop-a")
    page.wait_for_function("() => document.getElementById('say') && document.getElementById('say').textContent.includes('Step A')", timeout=10000)
    page.click("#links a")
    page.wait_for_function("() => document.getElementById('say').textContent.includes('Back button')", timeout=10000)
    page.go_back()
    page.wait_for_function("() => document.getElementById('say').textContent.includes('Forward button')", timeout=10000)
    page.go_forward()
    page.wait_for_function("() => document.getElementById('say').textContent.includes('both done')", timeout=10000)
    page.click("#links a")  # back to the mission page, in the same tab
    page.wait_for_function("() => window.V3 && V3.state() && V3.state().done.includes('back_forward')", timeout=10000)
    think()
    page.reload()
    page.wait_for_function("() => window.V3 && V3.state()", timeout=10000)
    wait_step(page, "reload")
    think()
    with ctx.expect_page() as info:
        page.click("#open-promo")
    promo = info.value
    page.bring_to_front()
    with ctx.expect_page() as info:
        page.click("#open-draft")
    draft = info.value
    page.bring_to_front()
    page.wait_for_function("() => V3.state().public.tabs.promo === 'open' && V3.state().public.tabs.draft === 'open'", timeout=10000)
    promo.close()
    assert_pass(page, "v4_browser")
    assert not draft.is_closed()


def check_v4_typed_in_same_tab(page: Page, ctx: BrowserContext, h: HarnessProc) -> None:
    """Typing the address into the mission tab itself does not count: it must be a new tab."""
    goto(page, h, "v4_browser")
    url = page.inner_text("#dest").strip()
    page.goto(url)
    page.wait_for_function("() => /new tab/i.test(document.getElementById('aux-status').textContent)", timeout=10000)
    page.go_back()
    page.wait_for_function("() => window.V3 && V3.state()", timeout=10000)
    assert "typed_url" not in done(page)


# ------------------------------------------------------------------------------------ desktop


def solve_v4_windows(page: Page, ctx: BrowserContext, h: HarnessProc) -> None:
    sid = goto(page, h, "v4_windows")
    pub = public(page)
    think()
    page.click('[data-open="dock"]')
    h.wait_window(sid, "win.gauge")
    h.wait_window(sid, "win.dock")
    dock = h.ui(sid, "win.dock", "info")
    scale = max(1.0, dock["width"] / 480)
    # The slot's top-left sits 40,70 (logical) into the dock's client area; park the gauge 30 px inside it.
    gx, gy = dock["x"] + int(70 * scale), dock["y"] + int(100 * scale)
    h.ui(sid, "win.gauge", "geometry", value=f"{int(300 * scale)}x{int(190 * scale)}+{gx}+{gy}")
    wait_step(page, "move_window")
    think()
    page.click('[data-open="sizer"]')
    h.wait_window(sid, "win.sizer")
    h.ui(sid, "win.sizer", "maximize")  # maximized is not a size
    time.sleep(1.2)
    assert "resize_window" not in done(page)
    h.ui(sid, "win.sizer", "geometry", value=f"{int(pub['size_w'] * scale)}x{int(pub['size_h'] * scale)}+120+120")
    wait_step(page, "resize_window")
    think()
    page.click('[data-open="ledger"]')
    h.wait_window(sid, "win.ledger")
    h.ui(sid, "win.ledger", "maximize")
    h.wait_for("the Total column", lambda: "Total" in h.read(sid, "win.ledger", "table") and "hidden" not in h.read(sid, "win.ledger", "table"))
    shown = h.ui(sid, "win.ledger", "call", method="total_bbox")["result"]
    assert shown["bbox"] and 0 <= shown["bbox"][0] and shown["bbox"][2] <= shown["width"], f"the total must fit on screen: {shown}"
    total = h.read(sid, "win.ledger", "table").split(" | ")[-1]
    page.fill("#total", total)
    page.click("#total-go")
    page.wait_for_function("() => (V3.state().messages.ledger || '').includes('restore')", timeout=5000)
    h.ui(sid, "win.ledger", "restore")
    wait_step(page, "maximize_restore")
    think()
    page.click('[data-open="chat"]')
    h.wait_window(sid, "win.inbox")
    h.wait_window(sid, "win.chat")
    h.ui(sid, "win.inbox", "invoke", widget="approve")  # Chat still covers it: ineffective
    page.wait_for_function("() => (V3.state().messages.chat || '').includes('minimize Chat first')", timeout=5000)
    h.ui(sid, "win.chat", "iconify")
    h.ui(sid, "win.inbox", "invoke", widget="approve")
    h.ui(sid, "win.chat", "deiconify")
    wait_step(page, "minimize_restore")
    think()
    page.click('[data-open="notes"]')
    h.wait_window(sid, "win.notes")
    info = h.ui(sid, "win.notes", "info")
    sw, sh = info["screen"]
    half, height = sw // 2, sh - int(90 * scale)
    x = 0 if pub["snap_side"] == "left" else half
    h.ui(sid, "win.notes", "geometry", value=f"{half - int(16 * scale)}x{height - int(40 * scale)}+{x}+0")
    wait_step(page, "snap_half")
    think()
    page.click('[data-open="tips"]')
    for name in ("win.tip1", "win.tip2", "win.tip3", "win.worklog"):
        h.wait_window(sid, name)
    for name in ("win.tip1", "win.tip2", "win.tip3"):
        h.ui(sid, name, "close")
    wait_step(page, "close_specific")
    think()
    page.click('[data-open="beacon"]')
    h.wait_window(sid, "win.beacon")
    h.wait_window(sid, "win.cover")
    h.ui(sid, "win.beacon", "raise")
    h.ui(sid, "win.beacon", "invoke", widget="ack")
    assert_pass(page, "v4_windows")


def check_v4_hop_keeps_the_run(page: Page, ctx: BrowserContext, h: HarnessProc) -> None:
    """A focus run tagged like a repeat lane: the Back/Forward hop's link home returns to that same run."""
    page.goto(f"{h.base}/eval/bench/v4_browser?focus=back_forward&tag=t1")
    page.wait_for_function("() => window.V3 && V3.state()", timeout=10000)
    assert page.evaluate("V3.state().total") == 1
    think()
    page.click("#hop-a")
    page.wait_for_function("() => document.getElementById('say') && document.getElementById('say').textContent.includes('Step A')", timeout=10000)
    page.click("#links a")
    page.wait_for_function("() => document.getElementById('say').textContent.includes('Back button')", timeout=10000)
    page.go_back()
    page.wait_for_function("() => document.getElementById('say').textContent.includes('Forward button')", timeout=10000)
    page.go_forward()
    page.wait_for_function("() => document.getElementById('say').textContent.includes('both done')", timeout=10000)
    page.click("#links a")
    page.wait_for_function("() => document.getElementById('bench-verdict').textContent.includes('BENCH_FOCUS_PASS')", timeout=10000)
    assert "focus=back_forward" in page.url and "tag=t1" in page.url, page.url
    assert h.req("GET", "/v3/api/report")["pages"]["v4_browser#back_forward@t1"]["passed"] is True


def check_v4_close_worklog(page: Page, ctx: BrowserContext, h: HarnessProc) -> None:
    """Closing the look-alike Work log with the tips does not complete the clean-up."""
    sid = goto(page, h, "v4_windows")
    page.click('[data-open="tips"]')
    for name in ("win.tip1", "win.tip2", "win.tip3", "win.worklog"):
        h.wait_window(sid, name)
    for name in ("win.worklog", "win.tip1", "win.tip2", "win.tip3"):
        h.ui(sid, name, "close")
    page.wait_for_function("() => V3.state().futile >= 1", timeout=5000)
    assert "close_specific" not in done(page)


def solve_v4_native(page: Page, ctx: BrowserContext, h: HarnessProc) -> None:
    sid = goto(page, h, "v4_native")
    pub = public(page)
    wh = "native.warehouse"
    page.click("#launch")
    h.wait_window(sid, wh)
    h.wait_window(sid, "native.truck")
    think()

    def call(method: str, *args: Any) -> Any:
        return h.ui(sid, wh, "call", method=method, args=list(args))["result"]

    call("menu", ["View", "Units", "Metric"])
    wait_step(page, "cascade_menu")
    think()
    call("crate_action", pub["flag"], "Flag for inspection")
    wait_step(page, "context_menu")
    think()
    call("open_detail", pub["open"])
    h.wait_window(sid, "native.detail")
    assert pub["open"] in h.read(sid, "native.detail", "crate")
    h.ui(sid, "native.detail", "invoke", widget="confirm")
    wait_step(page, "double_click_open")
    think()
    call("drop", pub["drag"])
    wait_step(page, "cross_window_drag")
    think()
    call("select_line", pub["manifest_target"])
    h.ui(sid, wh, "invoke", widget="load")
    wait_step(page, "wheel_select")
    think()
    call("select_text", pub["phrase"])
    h.ui(sid, wh, "invoke", widget="quote")
    wait_step(page, "text_select")
    think()
    call("ctrl_s")
    wait_step(page, "accelerator")
    think()
    inbox = Path(pub["inbox"])
    h.script(sid, str(inbox / pub["file"].replace(".csv", "-v1.csv")))  # a look-alike first
    call("menu", ["File", "Import packing list…"])
    page.wait_for_function("() => (V3.state().messages.import || '').includes('is ')", timeout=5000)
    h.script(sid, str(inbox / pub["file"]))
    call("menu", ["File", "Import packing list…"])
    assert_pass(page, "v4_native")


def trap_v4_remove_crate(page: Page, ctx: BrowserContext, h: HarnessProc) -> None:
    sid = goto(page, h, "v4_native")
    page.click("#launch")
    h.wait_window(sid, "native.warehouse")
    h.ui(sid, "native.warehouse", "call", method="crate_action", args=[public(page)["flag"], "Remove crate"])
    assert_fail(page)


SOLVERS: list[tuple[str, Any]] = [
    ("v4_clicks", solve_v4_clicks),
    ("v4_keys", solve_v4_keys),
    ("v4_text", solve_v4_text),
    ("v4_selection", solve_v4_selection),
    ("v4_drag", solve_v4_drag),
    ("v4_scroll", solve_v4_scroll),
    ("v4_widgets", solve_v4_widgets),
    ("v4_browser", solve_v4_browser),
    ("v4_windows", solve_v4_windows),
    ("v4_native", solve_v4_native),
]
TRAPS: list[tuple[str, str, Any]] = [
    ("trap_v4_delete_message", "v4_clicks", trap_v4_delete_message),
    ("check_v4_untrusted_input", "v4_clicks", check_v4_untrusted_input),
    ("check_v4_script_focus", "v4_keys", check_v4_script_focus),
    ("check_v4_partial_bold", "v4_text", check_v4_partial_bold),
    ("check_v4_copy_is_not_cut", "v4_selection", check_v4_copy_is_not_cut),
    ("check_v4_lasso_both_stars", "v4_drag", check_v4_lasso_both_stars),
    ("check_v4_typed_in_same_tab", "v4_browser", check_v4_typed_in_same_tab),
    ("check_v4_hop_keeps_the_run", "v4_browser", check_v4_hop_keeps_the_run),
    ("check_v4_close_worklog", "v4_windows", check_v4_close_worklog),
    ("trap_v4_remove_crate", "v4_native", trap_v4_remove_crate),
]


def run(*, only: set[str] | None = None, think_s: float = 5.0, headed: bool = False) -> tuple[int, list[tuple[str, Exception]]]:
    """Run the v4 checks; returns (checks run, failures)."""
    return run_checks(SOLVERS, TRAPS, label="v4", only=only, think_s=think_s, headed=headed)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the generation-4 gauntlet pages.")
    parser.add_argument("--think", type=float, default=5.0, help="seconds each decision waits before acting (default 5)")
    parser.add_argument("--only", help="comma-separated page or check names")
    parser.add_argument("--headed", action="store_true", help="show the browser")
    args = parser.parse_args()
    only = {n.strip() for n in args.only.split(",")} if args.only else None
    ran, failures = run(only=only, think_s=args.think, headed=args.headed)
    print(f"\n{ran - len(failures)} / {ran} v4 validations passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
