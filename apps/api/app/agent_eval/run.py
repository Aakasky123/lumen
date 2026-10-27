"""Run the eval suite and print the generality dashboard.

  python -m app.agent_eval.run            # fixtures + the real brain (CI-shaped, no browser)
  python -m app.agent_eval.run --real     # real Chrome via a CONNECTED extension body (truth)

Fixtures isolate the brain's reasoning on authored pages. --real needs the backend running, the
extension connected, and a tab — it drives the same brain through ChromeExtensionBody.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from app.agent_eval.brain import LlmBrain
from app.agent_eval.fixture_body import ScriptedFixtureBody
from app.agent_eval.suite import run_suite
from app.agent_eval.tasks import ALL_TASKS, real_tasks
from app.lumen_model import LumenModelClient
from app.db import SessionLocal, init_db


def _select_tasks(tasks: list[Any], *, families: str | None = None, exclude_families: str | None = None) -> list[Any]:
    include = {item.strip() for item in (families or "").split(",") if item.strip()}
    exclude = {item.strip() for item in (exclude_families or "").split(",") if item.strip()}
    selected = [task for task in tasks if (not include or task.family in include) and task.family not in exclude]
    if not selected:
        raise SystemExit("No eval tasks matched the requested family filter.")
    return selected


def _brain(db: Any, provider_id: int | None) -> LlmBrain:
    model = LumenModelClient(db)
    provider = model.provider_service.provider_by_id(provider_id) if provider_id else None
    if provider_id and provider is None:
        raise SystemExit(f"No model provider with id {provider_id}.")
    if provider is not None:
        print(f"(pinned model provider #{provider_id}: {provider.provider_type}/{provider.model_name})")
    return LlmBrain(model, provider=provider)


async def run_fixtures(
    provider_id: int | None = None,
    *,
    families: str | None = None,
    exclude_families: str | None = None,
) -> dict[str, Any]:
    with SessionLocal() as db:
        return await run_suite(
            _select_tasks(ALL_TASKS, families=families, exclude_families=exclude_families),
            lambda task: ScriptedFixtureBody(),
            _brain(db, provider_id),
        )


async def run_real(
    base: str,
    provider_id: int | None = None,
    *,
    families: str | None = None,
    exclude_families: str | None = None,
) -> dict[str, Any]:
    from app.agent_eval.chrome_body import ChromeExtensionBody
    from app.extension_body import ExtensionBody
    from app.extension_hub import hub

    connection = hub.get(None)
    if not connection:
        raise SystemExit("No extension connected. Open Chrome with the Lumen extension, then retry.")
    body = ChromeExtensionBody(ExtensionBody(connection))
    with SessionLocal() as db:
        return await run_suite(
            _select_tasks(real_tasks(base), families=families, exclude_families=exclude_families),
            lambda task: body,
            _brain(db, provider_id),
        )


def _print(out: dict[str, Any]) -> None:
    print(json.dumps(out["summary"], indent=2))
    for r in out["results"]:
        print(
            f"  {r.task_id:38} {r.family:14} success={str(r.task_success):5} verified={str(r.verified_success):5} "
            f"final={r.final_state:10} steps={r.steps_taken:2} fab={r.unsupported_success_claims} forbidden={r.forbidden_side_effects}"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--real", action="store_true", help="drive real Chrome via a connected extension")
    parser.add_argument("--base", default="http://127.0.0.1:8001")
    parser.add_argument("--family", default=None, help="comma-separated task families to include")
    parser.add_argument("--exclude-family", default=None, help="comma-separated task families to skip")
    parser.add_argument("--provider", type=int, default=None, help="pin the brain to a model provider id instead of the active configured provider")
    args = parser.parse_args()
    init_db()
    if args.real:
        out = asyncio.run(
            run_real(args.base, args.provider, families=args.family, exclude_families=args.exclude_family)
        )
    else:
        out = asyncio.run(run_fixtures(args.provider, families=args.family, exclude_families=args.exclude_family))
    _print(out)


if __name__ == "__main__":
    main()
