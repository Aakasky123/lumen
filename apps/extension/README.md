# Lumen browser body

A Chrome MV3 extension that is Lumen's eyes and hands in the user's real, signed-in Chrome. It holds a
connection to the backend and carries out the commands the run loop sends. It never plans, chooses values, or
tells the user anything; the brain in `apps/api` does that.

```text
chat  ->  brain (run_vision_loop)  --ws-->  this extension  ->  the page
            ^                                     |
            +------ observations and results -----+
```

## Connection

`src/background.js` opens `ws://127.0.0.1:8001/extension/ws?device_id=…` (the device id is kept in
`chrome.storage`) and sends a `hello` with its build and capabilities. The backend accepts live actions only when
the hello lists every capability in `REQUIRED_LIVE_ACTION_CAPABILITIES` (`apps/api/app/extension_hub.py`);
`GET /operator/extension/status` reports `live_actions_ready`. A 20-second ping keeps the service worker alive;
the worker reconnects with backoff and a 30-second `chrome.alarms` tick. Because the backend addresses the
body by device id, a worker restart mid-action is picked up on the next command. After changing extension code,
reload it (`chrome://extensions`, or `POST /operator/extension/reload`) so the backend sees the new build.

## Commands

- **Observe:** `screenshot` (the visible tab plus `page_evidence`: redacted text, run tabs, and the control
  inventory), with sensitive regions masked; `ensure_sensitive_screenshot_control` binds a mask to a control the
  model judged sensitive.
- **Act:** `vision_click`, `vision_click_control`, `vision_type`, `vision_type_control`,
  `vision_type_credential_control`, `vision_scroll`, `vision_scroll_to_text`, `vision_wheel`, `vision_key`,
  `vision_drag`, `vision_hover`, `vision_wait`, `vision_watch`, `vision_upload`, `vision_upload_control`.
- **Navigate:** `navigate`, `navigate_back`, `navigate_forward`, `vision_navigate`, `vision_reload`,
  `vision_switch_tab`.
- **Other:** `set_control_indicator`, `reload_extension`, and `observe` / `execute`, which serve the evaluation
  body and the `GET /operator/extension/observe` grader only.

Clicks are trusted input through the Chrome debugger protocol. Before each action the body checks that the page
and document still match the observation and re-resolves the target; a coordinate or canvas click re-captures
and needs identical pixels. A submit-like target is refused unless the backend sends a one-time authorization
for that exact target. Clicks that would open a file dialog are refused; uploads attach files directly.

A run gets its own tabs: a new task always opens a new tab, and the body tracks the tabs the run opened
(a tab opened by the run's own click joins them). It never takes over the user's active tab.

## Files

| File | Role |
| --- | --- |
| `manifest.json` | Permissions: tabs, storage, scripting, alarms, downloads, debugger, notifications, sidePanel. Only `control_indicator.js` is a declared content script; the rest is injected on demand. |
| `src/background.js` | The body: connection, run tabs, capture and masking, command execution and its checks. |
| `src/observe.js` | Structured observation and fill/select/click executors for `observe` / `execute`. |
| `src/control_indicator.js` | Invisible marker on a run tab while Lumen controls it; hidden from captures. |
| `src/pointer_overlay.js` | The pointer showing where Lumen acts; hidden from captures. |
| `src/sidepanel.html` / `.js` / `.css` | Side panel: run activity and chat (it can send chat messages and answer the pending question). |
| `dev/sidepanel-preview.html` | The side panel with a stand-in runtime, for design work (`?s=question`, `choice`, `offline`, `empty`). Not packaged. |

## Use it

1. Start the backend (`npm run dev` from the repo root).
2. `chrome://extensions` → Developer mode → **Load unpacked** → `apps/extension`.
3. The toolbar badge shows **on** when connected; the icon opens the side panel.
4. Give Lumen a task in chat. When the side panel is closed, a notification offers to open it beside the run's tab.

Tests: `npm run extension:install` once, then `npm run extension:test` from the repo root (`node --test` with
jsdom, in `test/`).

Values typed into sensitive controls are never echoed in results or evidence.
