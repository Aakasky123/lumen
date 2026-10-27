# Lumen Interaction Gauntlet — standalone bundle

A hard, self-grading benchmark for a browser/computer-use agent's **interaction mechanics**:
page handling, scrolling, clicking, typing, form filling, and OS-grade computer use.
No framework dependencies — Python stdlib to serve and grade (plus tkinter for the native windows of
generations 3-5); Playwright only if you want to re-verify the benchmark itself.

Six generations (see `generations_included` in `manifest.json` for what this bundle carries):

- **Generation 1** — 12 pages, 60 challenges: the baseline gauntlet of hostile-but-honest
  mechanics (delayed mounts, virtualized lists, honeypots, input masks, native dialogs, canvas UI).
- **Generation 2 ("nightmare")** — 9 pages, 40 challenges, built to break agents that passed
  generation 1. Each page targets a surviving heuristic: DOM-text trust (rendered labels and DOM
  labels are swapped; painted labels lie), obedience to instructions found in page content
  (injected "system notices" and human-verification lures are landmines), auto-accepting dialogs
  (one dialog in a chain is destructive and must be declined), scroll-to-bottom (a list that grows
  faster than you scroll; inverted wheel; dual simultaneous scroll bands), burst typing (rate
  limits, focus theft, a buffer that prepends every keystroke), blind speed (a UI that reshuffles
  every 0.9s under a click-swallowing sync veil; per-step session timers that wipe slow work), and
  DOM-only perception (forms and evidence that exist purely as canvas pixels; ephemeral 4-second
  reveals with a limited budget; drag paths judged by geometry).
- **Generation 3 ("cross-surface")** — 7 pages, 33 challenges: browser use, computer use, and
  switching between them. Missions hand work between the page and native desktop windows (menus,
  native Yes/No and Save As dialogs, a minimized window, a window that must be widened, an
  app-modal dialog hidden behind everything, an always-on-top toast, a pixel-only legacy form),
  the browser's own UI (permission prompts, popup windows, a Picture-in-Picture window), and files
  (download → edit → upload through the OS picker, rename in the file manager, drag a file onto
  the page). One page tests timing under a *latency-fairness contract*: everything moves faster
  than a screenshot round trip, yet a brain that needs 5 s per decision can pass with a body it
  hands timing to (late-bound clicks, armed waits, watching, visual rebinding).
- **Generation 4 ("human-input atlas")** — 10 pages, 69 challenges: coverage of every input
  primitive a person uses, one challenge each, so a failure names the missing skill. Pointer (a
  dense grid, double-click to open, a context menu, middle-click to a background tab, press-and-hold
  for a duration, hover tooltips, repeat clicks), keyboard (Tab traversal, arrow keys, chords,
  Esc/Enter, F2 rename, a held key, undo/redo), text editing (multi-line, caret insert, rich-text
  bold/italic, line delete, special characters, pick-only autocomplete, an edit deep in a long
  text), selection and clipboard (drag-select, triple-click, Shift- and Ctrl-click lists, rubber
  band, keyboard selection, copy/paste, cut/paste), drag (HTML5 drop, sortable list, kanban,
  two-handle slider, splitter, Ctrl+drag copy, long-press drag, lasso), scroll and zoom (inner
  panel, horizontal lane, map panning, Ctrl+wheel zoom, an 80-page document), the browser's form
  controls (native select, date, time, color, exact range, multi-select, file chooser) and its own
  UI (address bar in a new tab, tab relay, Back then Forward, reload, closing the right tab), and on
  the desktop: window management (move, resize, maximize then restore, minimize then restore, snap
  to half the screen, close only the right windows, find a buried window) and a native app (cascade
  and context menus, double-click, a drag from one window onto another, a long list, read-only
  text selection, an accelerator, the Open dialog). Pages ignore script-dispatched (untrusted) input.
- **Generation 5 ("endurance")** — 5 pages, 26 checkpoints: long tasks across many windows that
  test what breaks at length — memory (a record window that closes for good, a briefing shown
  once), standing rules applied many steps later under pressure, several native windows ringing at
  once, accuracy over dozens of items (14 invoices viewed one at a time; 16 claims each needing a
  lookup, a rule table, and arithmetic), and interruption and resumption (an expiring lookup
  session, an editor that crashes and loses its unsaved fields). Checkpoints are milestones of one
  task, so partial credit measures how far the agent got. Generation-5 pages carry a larger
  `budget_min` (30-75 minutes) in the manifest. Feedback is rationed so trial and error cannot stand in
  for memory and rules: terminal answers and saved claims are final, the reconciliation check runs at
  most three times, and the shift report and debrief are filed once.
- **Generation 6 ("web apps")** — 10 pages, 50 challenges: realistic apps that live entirely inside
  one page (`v6_mail`, `v6_shop`, `v6_sheet`, `v6_travel`, `v6_insights`, `v6_admin`, `v6_calendar`, `v6_crm`, `v6_docs`, `v6_helpdesk`), with the multi-step work those apps
  exist for: filters and bulk actions across paged tables, cross-referencing views, custom widgets,
  rules applied to many records, and look-alike records. Nothing leaves the page, so every page can run
  at once in headless browsers, and each of a page's five challenges can run alone: open the page with
  `?focus=<challenge>` (the manifest's `challenge_ids`; generation 4 is focusable too) and only that
  challenge is shown and judged; its pass reads `BENCH_FOCUS_PASS <page>-<hex> (<challenge>)`, so it never
  counts as a page pass. Add `?tag=<name>` to run the same page several times at once.

## Generations 3-6 quickstart (the harness)

Generations 3-6 need the harness, not `serve.py` — it judges surfaces a static page cannot see:

```
python v3_harness.py               # needs tkinter and a desktop session; random port by default
```

The first line printed is `V3_HARNESS_URL http://127.0.0.1:<port>`. For each manifest page with
`"harness": true`, start the agent at `<url>` + `url_path` with the page's `goal`. The harness
opens native windows on this machine's desktop and creates mission folders under `storage/bench/LumenBench`
(`--workspace-root` to change). Your agent must be able to see and operate the real screen, not
just the page. Grade with the harness's own verdicts, which cover every surface:

```
python grade.py results.json --harness http://127.0.0.1:<port>
```

Rules for a fair run: one harness per benchmark run (a sprung trap poisons that page until the
harness restarts); never start it with `--debug-hooks` for an agent run (that exposes ground truth
and voids the run); don't give the agent a shell or file-system API — the files are meant to be
handled with the file manager, an editor, and OS dialogs. Generation 6 needs no desktop: run the harness
with `--desktop off` and as many headless browsers as you like.

## How it grades itself

- Each page reveals a pass token (`BENCH_PASS <page>-<hex>`) **in visible page text** only when
  every challenge on it is genuinely complete.
- Traps (wrong-target clicks, hidden honeypots, accepting a paid promo, unauthorized submits,
  invented values in optional fields) permanently poison the run: `BENCH_FAIL <reason>` appears and
  is persisted in `sessionStorage`, so reloading cannot clear it.
- Codes, amounts, PINs, and canvas geometry are randomized per page load and validated by the page
  itself — memorized answers and replayed traces score zero.
- Every page always shows `Challenges complete: k / n` and `Ineffective interactions: m` in visible
  text, so partial credit and clumsiness are measurable from the same artifact.

## Quickstart

1. Serve the pages (stdlib only):

   ```
   python serve.py --port 8020
   ```

2. For each page in `manifest.json` with `"harness": false` (generation 3-6 pages need the harness above), run
   your agent with:
   - start URL: `http://127.0.0.1:8020` + the page's `url_path`
   - instruction: the page's `goal` string, verbatim
   - a fresh browser context per page (traps persist in sessionStorage by design)

3. When the agent finishes (or hits its step budget), capture the **final visible page text**
   (`document.body.innerText` or your harness's page-text observation) and store it as
   `{"<page name>": "<text>", ...}` in a JSON file.

4. Grade:

   ```
   python grade.py results.json -o scorecard.json
   ```

   The scorecard reports pass rate, mean challenge ratio, and sprung traps — overall, per family,
   and per page. A sprung trap zeroes that page's ratio: a run that stepped on a landmine failed no
   matter how much it finished first.

## Suggested reporting

Headline: **verified pass rate per family** (token present AND no `BENCH_FAIL`). Secondary: mean
challenge-completion ratio, futile-interaction counts, steps-to-pass. `max_steps` in the manifest
is a generous per-page budget; exceeding it should count as a failure in your harness.

## Integrity rules (read before wiring up an agent)

- The agent must interact through real browser mechanics (click/type/scroll/keys/drag). Several pages
  require native dialogs, a second tab, real clipboard paste, and keyboard-only focus traversal.
  Generation 4 counts a gesture only when the browser marks its events as trusted (real input); a
  script-dispatched click, key, drag, or paste is ineffective. Values entered into fields are judged
  by the final value, however they got there.
- `window.__benchDebug` holds per-load ground truth (random codes, canvas geometry) **for the
  benchmark's own validator only**. An agent that reads it — or evaluates arbitrary JS on the page
  — is cheating by definition; disable JS-eval capabilities during benchmark runs.
- `window.__benchReport()` is safe telemetry (done/failed/futile counts, no answers) if your
  harness wants structured progress instead of parsing page text.
- Pass tokens exist only inside page scripts until earned; don't give the agent view-source.

## Re-verifying the benchmark

`validate_golden_path.py` replays every page's intended interaction sequence with real Playwright
input. Generations 1-2 have 21 golden paths and 7 trap checks (including fail-persistence across
reload); generation 3 adds 7 golden paths and 8 trap checks through `validate_v3.py`, generation 4
adds 10 golden paths and 10 trap and rule checks (`validate_v4.py`), generation 5 adds 5 golden
paths and 4 trap checks (`validate_v5.py`), and generation 6 adds 10 golden paths and 16 trap and rule
checks (`validate_v6.py`, four at a time):

```
pip install playwright && playwright install chromium
python validate_golden_path.py [--generation 1|2|3|4|5|6|all]
```

Every bundled page must PASS (the validator skips pages not present in this bundle — a
generation-filtered bundle reports SKIP for the others). The full benchmark validates 98/98
(53 golden paths + 45 trap and rule checks). Run it after any edit to the pages. The checks for
generations 3-5 need tkinter and a desktop session, and run under a ghost-mode harness with every
decision delayed `--think` seconds (default 5) — the proof that their timed challenges are fair to a
slow brain (`--think 0` for a quick run).

## The pages

See `manifest.json` for the machine-readable list (name, family, tier, generation, goal, token,
challenge count, step budget, run budget). Helper pages opened in extra tabs or windows (`os_dialogs_aux`,
`v2_os_aux1`, `v2_os_aux2`, `v3_kiosk_ticket`, `v3_incident_portal`, `v4_clicks_notes`, `v4_browser_dest`,
`v4_browser_vault`, `v4_browser_hop`, `v4_browser_tab`, `v5_onboarding_badge`, `v5_marathon_lookup`) and the
shared `v3_common.js`, `v4_style.css`, `v6_kit.js`, and `v6_style.css` are not tasks and do not appear in the manifest.

Generation 2 notes for a fair run:
- Native dialogs must be answered **by content** — a harness that auto-accepts every dialog fails
  `v2_os_gauntlet` by design.
- Several graders are geometric (drag paths, knob angle, corridor traces, canvas hit-tests): the
  agent needs real pointer trajectories, not element-level click dispatch.
- `v2_form_crucible` runs a 90-second-per-step session timer; an agent that deliberates too long
  loses the step's work (counted as ineffective, not fatal).
- The injection landmines (`v2_mutation_storm`'s system notice, `v2_click_minefield`'s
  human-verification lure) are part of the score: following instructions embedded in page content
  is a failure, full stop.

Generation 4 and 5 notes for a fair run:
- The desktop pages (`v4_windows`, `v4_native`, every generation-5 page) open native windows on the
  machine running the harness; the agent must see and operate the real screen — move and resize
  windows by their frames, use the taskbar or Alt+Tab, answer the OS Open dialog.
- `v4_browser` needs the browser's own UI: a new tab, its address bar, Back/Forward, and reload.
- Generation 5 tests memory honestly: values the task needs later are shown once (a record window
  that closes 45 seconds after use, a briefing that disappears, an operator code shown only before
  the shift). Taking notes in an app on the machine is a legitimate human strategy; reading the
  harness's API or the page's script state is not.
