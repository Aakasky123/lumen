# Lumen desktop host

An Electron app that packages the cockpit (`apps/renderer`) and the backend (`apps/api`) into one Windows
application. It runs the backend, shows the cockpit, and draws the on-screen overlays that show what Lumen is
doing. The run loop and both bodies (the Chrome extension and the Windows desktop body) live in the backend.

## What the host does

- **Backend.** Reuses a healthy API already on `127.0.0.1:8001`, without taking ownership of it; otherwise it
  starts one (uvicorn under `LUMEN_PYTHON` in development, `binaries/lumen-api.exe` when packaged) with
  `LUMEN_DATA_ROOT` and `LUMEN_DESKTOP_CONTROL=true`. It checks the backend every 5 s and restarts a
  backend it started after three failed checks (backing off 5 s, 15 s, 60 s).
- **Cockpit.** Serves the statically exported renderer on the first free port in `3210`-`3219` (the only origins
  the backend's CORS allows) and opens it in a single-instance window. Closing the window keeps Lumen in the tray
  (Open, Restart runtime, Open data folder, Quit). The window minimizes itself while a run works on the desktop.
- **Overlays.** On every display, two click-through windows excluded from screen capture: a status pill that
  mirrors the run's status (polled from `/operator/desktop/status`) and a pointer that shows where Lumen acts
  (polled from `/operator/desktop/pointer`).
- **Bridge.** The renderer runs with Node integration off, context isolation on, and the Chromium sandbox on.
  `preload.cjs` exposes only: read status (runtime, model, Chrome extension, computer use), start the runtime,
  restart it, turn Windows computer use on or off, open Chrome's extensions page, and open the data folder.

A normal API process started without the host leaves Windows computer use off; set
`LUMEN_DESKTOP_CONTROL=true` in `.env` to enable it. The host bar's switch turns it on and off at runtime,
and every Windows primitive checks that switch immediately before acting.

Chrome opens its side panel only on a user gesture inside Chrome, so the cockpit cannot open it. When a run
opens a tab and the panel is closed, the extension shows a notification that opens the panel beside that tab.

## Development

From the repository root:

```powershell
npm install
npm run desktop:dev     # or npm run dev, which starts the backend first
npm run desktop:test
```

## Packaging

```powershell
python -m pip install -r apps/desktop/requirements-build.txt   # once
npm run desktop:build      # unpacked app
npm run desktop:package    # Windows installer
```

Packaging exports the renderer with the desktop API origin, freezes `apps/api` into
`apps/desktop/binaries/lumen-api.exe` with PyInstaller, and bundles both with Electron Builder. Icons come from
`scripts/generate-icons.py`, which also writes the extension's icons. The installer carries the extension
source under `resources/extension` for loading unpacked. Build outputs are git-ignored.

## Where data lives

| | Data root | Logs |
|---|---|---|
| Development | the repository's `storage/` | `userData/logs/lumen-api.log` |
| Packaged | `%APPDATA%\Lumen\runtime` (the folder name predates the rename and is kept for existing installs) | `%APPDATA%\Lumen\logs/lumen-api.log` |

The data root holds `lumen.db`, `artifacts/`, and `memory/`.

## Windows computer use

`apps/api/app/desktop_body.py` plugs into the same loop as Chrome:

- **Sees** the whole virtual desktop, the active window's UI Automation controls, the window list, and the
  caret. Password and model-marked sensitive controls are masked.
- **Does** clicks (snapped to the control under the point), typing (clipboard paste for long text, with the
  clipboard restored), wheel, keys, drag, hover, uploads through the file dialog, window focus, waits, and
  launching an installed application from the list it was shown (including packaged apps via
  `shell:AppsFolder`).
- **Reports** what changed after each action (surface, title, focus, typed value) for receipts.

Shells, terminals, the Run box, the registry editor, and uninstallers are not in the launch list; Alt+F4,
Ctrl+Alt+Del, Win+L, and Win+R are blocked; the clipboard is never read for the model. Consequential clicks and
keys pass the same consequence check as in the browser.

Limits: Windows only; no control of UAC or the secure desktop; little structure on canvas, remote-desktop, or
DRM surfaces. The installer is unsigned and may be blocked by Smart App Control; a release needs a signing
certificate.
