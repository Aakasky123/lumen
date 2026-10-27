Lumen — AGENTS.md

Lumen is a computer-use agent. It completes real tasks on the user's machine through a Chrome extension and a desktop body for the host OS (Windows, or macOS in desktop_body_macos.py; plus an OSWorld VM body for benchmarking). The goal is a computer-use agent that works.

Priorities
When they conflict, in this order: 1. Performance (accuracy: Lumen passes every task, every test) 2. Cost 3. Speed. Safety rules below are not traded against any of them.

Safety
Never fill an ungrounded fact about the user: ask if required, skip if optional.
User-facing claims come only from verified receipts; anything entered unverified is reported as unverified.
Auto mode (Full autonomy) stops for the user only for payments and for facts about the user nothing holds (owner, 2026-10-06): a page- or goal-derived value the checks cannot verify is entered and reported, a flagged secret (or one the classifier could not judge) is typed unless a fail-closed check finds it would leave the task, and questions the agent can decide go back to it. Approval mode keeps every gate.
Facts
Run loop: run_vision_loop in apps/api/app/extension_run.py, plus apps/api/app/vision_agent.py. Bodies: extension_body.py, desktop_body.py (Windows), desktop_body_macos.py (macOS), desktop_platform.py (picks the host OS body), surface_body.py, agent_eval/osworld/body.py.
Implementation map: docs/architecture.md. Update it in the same change when behavior, API, storage, or config changes. Describe current behavior; history belongs in git log.
Checks: npm run api:test (runs pytest through scripts/py.mjs, which finds the .venv or python3; macOS has no bare `python`) · cd apps/renderer && npx tsc --noEmit · npm run capability-regressions · npm run desktop:test
The API suite runs on 8 xdist workers by default (~15s); one test, a few files, or -k run in-process. Each pytest process gets its own database under storage/test-runs, so concurrent runs in one checkout are safe. Test fakes settle instantly: a test that needs a real pause sets _VISION_STEP_SETTLE_SECONDS itself.
Eval: the production loop is measured by the Interaction Gauntlet (python -m app.agent_eval.interaction_bench.live_run), OSWorld (python -m app.agent_eval.osworld.run), and, for the macOS body, the Mac bench (python -m app.agent_eval.mac_bench.run [--focus task] [--repeat N]), run from apps/api. Success is the outcome, not the click path. Verified capabilities must not regress.
Add --lanes 5 to live_run to run in-page pages in parallel on headless bench lanes (manifest desktop: false); desktop pages still run one at a time on the real screen beside them.
Generation 6 (ten web apps, all in-page) runs whole on lanes: --generation 6 --lanes 10. To iterate on a failure, --focus page:challenge reruns one challenge alone and --repeat N measures how reliably it passes.
