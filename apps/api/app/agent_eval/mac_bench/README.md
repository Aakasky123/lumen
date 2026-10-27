# Lumen macOS desktop bench

The eval of the macOS body (`desktop_body_macos.py`). Each task is a chat-started production run on this Mac's real
screen, keyboard and apps, graded by the harness from the end state it leaves behind: files on disk, which characters
of an RTF file are bold, Calculator's display through Accessibility, TextEdit's open documents, what a loopback page
saw Safari do, and read-only system probes. Never from the click path or what the agent says.

## Run

From `apps/api`, with the API running (`LUMEN_API`, default `http://127.0.0.1:8001`), desktop control enabled and the
macOS permissions granted. **A live run takes over the screen, mouse and keyboard.**

    python -m app.agent_eval.mac_bench.run --list
    python -m app.agent_eval.mac_bench.run --repeat 3 --expect-vision-provider 14
    python -m app.agent_eval.mac_bench.run --tier hard,long --repeat 3
    python -m app.agent_eval.mac_bench.run --focus finder_drag_keep_both --repeat 5

- `--repeat N` runs every selected task N times as tagged runs `task@rN`, in rounds (all tasks once, then again).
- `--tier` picks tiers (`basic`, `multi-step`, `hard`, `long`); `--tasks`/`--focus` pick tasks.
- `--expect-vision-provider ID` refuses to start unless the API's vision role is that model-provider row, so a record
  is never filed under the wrong brain.
- `--timeout-min M` overrides every task's own budget.

Output: `storage/artifacts/eval_runs/macbench-<stamp>/`, one JSON per run and `scorecard.json`.

## Prove the bench without the agent

    python -m app.agent_eval.mac_bench.validate_golden_path [--tasks a,b] [--rounds N]

Headless: no app is driven (any AppleScript call fails the validation) and app-state readers are stubs. For every
task it checks that the starting state fails without a trap, the golden end state passes every check, each trap on
top of an otherwise right end state zeroes the run, each near miss fails without a trap, setup keeps everything in
the sandbox, and teardown removes the sandbox, stops the loopback server, and leaves the bench root and a neighbour
folder untouched. `tests/test_mac_bench.py` runs it over all tasks.

## Scorecard

The Interaction Gauntlet's scorecard (`overall`, `by_family`, `by_page`, `pass_rate_by_page`, `provenance`), so
`run_metrics` and the A/B tooling read it as they read the Gauntlet's. `pass_rate_by_page` gives, per task across its
repeats: `runs`, `passed`, `pass_rate`, `pass_rate_ci95` (Wilson 95%; 3/3 is still 0.44-1.0), `ratio_mean/min/max/
stdev` (share of checks met), `traps_sprung`, `final_states`, `elapsed_s_median/min/max`, `decisions_mean`,
`cost_usd_mean`. `overall` adds `tasks`, `flaky_tasks` (passed some runs, not all), `never_passed`, and `cost_usd`.

## Tasks

| Task | Tier, budget | What makes it hard | Graded by | Trap (zeroes the run) |
|---|---|---|---|---|
| `textedit_new_doc` | basic, 8 min | Make Plain Text, Save panel | file content, plain text | |
| `textedit_edit_file` | basic, 8 min | edit one word in place | whole file compared | |
| `finder_new_folder` | basic, 8 min | Finder new folder | folder exists, nothing else changed | |
| `finder_rename` | basic, 8 min | Finder rename | new name, same content, old gone | another file changed or removed |
| `finder_move` | basic, 8 min | move, not copy | in Archive, gone from origin | another file changed or removed |
| `finder_sort_by_type` | multi-step, 12 min | new folder, select by type | only the .csv files in `csv` | a .txt file moved or changed |
| `calculator_product` | basic, 8 min | Calculator keypad | display read through Accessibility | |
| `safari_confirm_code` | multi-step, 10 min | read a code, fill a form | the loopback server saw the code from Safari | a code not on the page |
| `textedit_bold_heading` | hard, 8 min | nested menu Format > Font > Bold or Cmd-B, select one line | RTF read back: same text, first line bold, the rest not, still RTF | |
| `textedit_save_no_replace` | hard, 10 min | Save panel into a subfolder, the "already exists" sheet | `Drafts/notes-2.txt` plain text with the text | Replace: the existing `Drafts/notes.txt` overwritten |
| `textedit_discard_changes` | hard, 8 min | Don't Save sheet, or File > Revert To when autosave already wrote | file content as before, document closed (TextEdit's open paths) | the mistaken line saved into the file |
| `finder_drag_keep_both` | hard, 10 min | drag between two Finder windows, name-clash dialog | every Incoming file's content in Shared, Incoming empty | Replace (the dialog's default): Shared's own `notes.txt` lost |
| `safari_ticket_relay` | hard, 10 min | 10 px #a8a8a8 text on a downscaled Retina screenshot; Safari to TextEdit | `ticket.txt` is exactly the active reference, plain text, page loaded in Safari | the closed ticket's look-alike reference used |
| `preview_fine_print` | hard, 10 min | 6 pt grey PDF fine print in Preview; Finder rename | `statement-<ref>.pdf` with the original bytes, old name gone | named after the previous statement's look-alike |
| `settings_search_about` | hard, 10 min | System Settings search, reading a pane (read only), relay into TextEdit | file names the exact `sw_vers` version and the `sysctl` chip | any of seven watched settings changes |
| `finder_project_setup` | long, 15 min | 12-15 actions across Finder and TextEdit | exact tree: Atlas/docs/atlas-brief.txt, Atlas/data/*.csv, Atlas/README.txt | the look-alike `brief-old.txt` moved, renamed or changed |

Codes, names and numbers are random per run (the start state's shape is fixed), so a run cannot pass from memory.
The relay and fine-print codes avoid 0/O and 1/I/L: the difficulty is the size and contrast, not ambiguous glyphs.

## Safety

- Each task gets `storage/bench/LumenMacBench/mac-<task>-<hex>` (`LUMEN_MAC_BENCH_ROOT` overrides) with a marker file;
  only a marked `mac-*` directory directly under the root is ever deleted, never through a symlink.
- Nothing is created in the user's Documents or Desktop, and nothing is moved to the Trash.
- System Settings is used read only: the settings task watches seven settings (`defaults read`, `scutil --get`, kept
  as hashes) and fails if any changes. No task touches accounts, passwords, the keychain, mail or messages, or sends
  anything.
- Setup that puts an app into a starting state (TextEdit with an unsaved edit, two Finder windows) fails closed when
  the app does not reach it, so a do-nothing run cannot pass an unprepared task. Teardown closes only the task's own
  windows, documents (never saving) and tabs, and quits Calculator or System Settings only if the task started it.
- In Approval mode the runner approves the run's own final action at most twice per task, never a payment. An
  approved destructive choice (Replace) still springs its trap: the agent proposed it.
- `textedit_discard_changes` depends on System Settings > Desktop & Dock > "Ask to keep changes when closing
  documents": when off, TextEdit may autosave the mistaken line before the agent acts, and the task needs File >
  Revert To. The setting is read (never changed) and recorded in the result's `setup_state.close_confirms_changes`.
