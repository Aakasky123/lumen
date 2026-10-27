"""Run a task suite through the eval loop and summarize — the generality dashboard.

`make_body(task)` returns the EvalBody to use (ScriptedFixtureBody for CI/regression; a
ChromeExtensionBody bound to a live connection for truth). The SAME brain (decide) runs against
either. The headline number is verified_success rate per family — fixtures green means the loop isn't
broken; real Chrome across families is what justifies calling Lumen general.
"""

from __future__ import annotations

from typing import Any, Callable

from app.agent_eval.contract import EvalResult, EvalTask
from app.agent_eval.runner import Decider, run_eval

MakeBody = Callable[[EvalTask], Any]


def summarize(results: list[EvalResult]) -> dict[str, Any]:
    def bucket() -> dict[str, int]:
        return {"total": 0, "task_success": 0, "verified_success": 0, "fabrications": 0, "forbidden": 0}

    by_family: dict[str, dict[str, int]] = {}
    overall = bucket()
    for r in results:
        for stats in (overall, by_family.setdefault(r.family, bucket())):
            stats["total"] += 1
            stats["task_success"] += int(r.task_success)
            stats["verified_success"] += int(r.verified_success)
            stats["fabrications"] += r.unsupported_success_claims
            stats["forbidden"] += int(bool(r.forbidden_side_effects))
    return {"overall": overall, "by_family": by_family}


async def run_suite(tasks: list[EvalTask], make_body: MakeBody, decide: Decider) -> dict[str, Any]:
    results: list[EvalResult] = []
    for task in tasks:
        results.append(await run_eval(task, make_body(task), decide))
    return {"results": results, "summary": summarize(results)}
