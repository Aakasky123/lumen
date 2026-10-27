# Contributing to Lumen

Thanks for helping. Lumen is a computer-use agent that acts on real machines, so changes are held to two
rules before anything else: it must still pass every task it passed before, and it must never type a fact
about the user that it cannot ground.

## Set up

```bash
python3 -m venv .venv && .venv/bin/pip install -r apps/api/requirements.txt
npm install
npm run extension:install
cp .env.example .env
npm run dev
```

Load `apps/extension` unpacked in Chrome (`chrome://extensions`, Developer mode). Add a model provider in
Settings before giving Lumen a task.

## Before you open a pull request

Run the checks that cover what you changed:

```bash
npm run api:test
cd apps/renderer && npx tsc --noEmit
npm run capability-regressions
npm run desktop:test
```

- Read [`AGENTS.md`](AGENTS.md) (the working agreement) and [`docs/architecture.md`](docs/architecture.md)
  (the implementation map).
- Update `docs/architecture.md` in the same change when behavior, API, storage, or config changes.
- Add or update tests with the change. A fix for a failed task should come with a test that fails without it.
- For changes to the run loop or a body, include benchmark results (the Interaction Gauntlet, OSWorld, or the
  Mac bench, as described in `docs/architecture.md`). Success is the task outcome, not the click path.
- Never commit real personal data, credentials, or API keys, including in tests and fixtures. Use
  `example.com` addresses and `555` phone numbers.

## License

By contributing, you agree that your contributions are licensed under the Apache License 2.0.
