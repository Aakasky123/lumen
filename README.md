# Lumen

Lumen is an open-source computer-use agent for Windows and macOS. You give it a task in chat; it does the task on your machine by
looking at the screen and acting, one verified step at a time:

```text
observe -> decide -> check -> act -> verify
```

A server-side brain loop decides each step with a vision model and checks it against safety gates. Bodies carry
the steps out:

- the **Chrome extension** works in your real, signed-in Chrome;
- the **desktop bodies** work in native apps: UI Automation on Windows, Accessibility on macOS;
- the **OSWorld body** works in an Ubuntu VM, for benchmarking.

One run can move between the browser and the desktop. Job applications are one supported kind of task, with a
profile, résumé, and saved answers.

| Part | What it is |
|---|---|
| `apps/api` | FastAPI backend: chat, the run loop (`extension_run.py`, `vision_agent.py`), model calls, safety gates, the desktop body, storage |
| `apps/extension` | Chrome MV3 extension, the browser body |
| `apps/renderer` | Next.js cockpit: chat, run questions, settings |
| `apps/desktop` | Electron host: runs the backend, shows the cockpit and the on-screen overlays |

[`docs/architecture.md`](docs/architecture.md) maps the code. [`AGENTS.md`](AGENTS.md) is the working agreement.

## Run it

You need Python 3.12, Node.js (tested on 24), and Google Chrome.

**macOS**

```bash
git clone https://github.com/Aakasky123/lumen.git && cd lumen
python3.12 -m venv .venv && .venv/bin/pip install -r apps/api/requirements.txt
npm install && npm run extension:install
cp .env.example .env
npm run dev
```

**Windows** (PowerShell)

```powershell
git clone https://github.com/Aakasky123/lumen.git; cd lumen
py -3.12 -m venv .venv; .venv\Scripts\pip install -r apps/api/requirements.txt
npm install; npm run extension:install
copy .env.example .env
npm run dev
```

`npm run dev` starts the backend on `http://127.0.0.1:8001` and opens the desktop window. Then:

1. **Set an encryption key.** Put any long random string in `LUMEN_TOKEN_ENCRYPTION_KEY` in `.env`; Lumen
   encrypts stored provider keys and credentials with it.
2. **Add a model provider** in Settings. With no model, chat says so and nothing runs.
3. **Load the browser body.** In Chrome, open `chrome://extensions`, turn on Developer mode, and load
   `apps/extension` unpacked. It connects to `ws://127.0.0.1:8001/extension/ws`.
4. **Optional: let Lumen use desktop apps.** Set `LUMEN_DESKTOP_CONTROL=true` in `.env`. On macOS, also turn
   on the dev host (it shows as **Electron**) under System Settings > Privacy & Security > Accessibility and
   Screen Recording, then restart it with `npm run restart`.

Data lives in `storage/` (SQLite by default; `infra/docker-compose.yml` starts an optional Postgres).
`scripts/lumen.py` drives the backend from a terminal (`status`, `chat`, `runs`, `watch`, `answer`, and more).

## Check it

```bash
npm run api:test
npm --workspace apps/renderer exec tsc -- --noEmit
npm run capability-regressions
npm run desktop:test
```

Benchmarks (the Interaction Gauntlet, OSWorld, and the Mac bench) are described in
[`docs/architecture.md`](docs/architecture.md#13-evaluation-and-tests).

## Safety

- Lumen never types a fact it cannot ground in your goal, profile, documents, or answers. It asks for a required
  fact and skips an optional one.
- Passwords and one-time codes stay with you or the credential vault and never reach a model.
- Every click and activation key is checked before it runs. In the default approval mode, Lumen asks before a
  consequential final action (submit, send, book, pay) and acts only on the exact control you approved. Full
  autonomy acts on your standing setting, still on an exactly bound control, and never repeats an uncertain
  activation.
- What Lumen tells you it did comes only from verified receipts.

## Contributing

Issues and pull requests are welcome. [`CONTRIBUTING.md`](CONTRIBUTING.md) covers setup, checks, and what a
change needs; report security problems privately as described in [`SECURITY.md`](SECURITY.md).

## License

Apache License 2.0. See [`LICENSE`](LICENSE).
