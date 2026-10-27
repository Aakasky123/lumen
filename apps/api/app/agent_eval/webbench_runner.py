"""Run Lumen's generic browser loop against public WebBench rows.

WebBench's public CSV gives live website tasks, but it does not include the
leaderboard evaluator or hidden ground truth. This runner therefore reports an
honest Lumen completion score: did the loop stop with a non-empty final answer
for READ tasks, or reach an explicit completion state for opt-in WRITE tasks?
It is benchmark glue only; it does not encode website routes or answers.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.agent_eval.brain import LlmBrain
from app.agent_eval.chrome_body import resolve_eval_body
from app.agent_eval.contract import BrowserAction, EvalTask
from app.lumen_model import LumenModelClient
from app.db import SessionLocal, init_db


@dataclass(frozen=True)
class WebBenchTaskRun:
    task_id: str
    operation: str
    start_url: str
    final_state: str
    completed: bool
    answer: str | None
    steps: int
    error: str | None
    submitted_forms: list[str]
    downloads: list[str]
    uploads: list[str]
    output_dir: str
    trace_file: str


def select_webbench_tasks(
    raw: Any,
    *,
    task_ids: str | None = None,
    limit: int | None = None,
    allow_write: bool = False,
) -> list[dict[str, Any]]:
    rows = raw if isinstance(raw, list) else raw.get("tasks", []) if isinstance(raw, dict) else []
    tasks = [
        row
        for row in rows
        if isinstance(row, dict) and str(row.get("benchmark") or "").casefold() == "webbench"
    ]
    wanted = {item.strip() for item in (task_ids or "").split(",") if item.strip()}
    if wanted:
        tasks = [task for task in tasks if str(task.get("external_id") or task.get("task_id") or task.get("id")) in wanted]
    if not allow_write:
        tasks = [task for task in tasks if str(task.get("operation") or "").upper() == "READ"]
    if limit is not None:
        if limit <= 0:
            raise ValueError("limit must be positive")
        tasks = tasks[:limit]
    return tasks


def score_webbench_runs(runs: list[WebBenchTaskRun | dict[str, Any]]) -> dict[str, Any]:
    rows = [asdict(run) if isinstance(run, WebBenchTaskRun) else dict(run) for run in runs]
    attempted = len(rows)
    completed = sum(1 for row in rows if row.get("completed"))
    by_state: dict[str, int] = {}
    by_operation: dict[str, int] = {}
    for row in rows:
        state = str(row.get("final_state") or "unknown")
        operation = str(row.get("operation") or "UNKNOWN").upper()
        by_state[state] = by_state.get(state, 0) + 1
        by_operation[operation] = by_operation.get(operation, 0) + 1
    return {
        "attempted": attempted,
        "completed": completed,
        "completion_score": completed / attempted if attempted else 0.0,
        "blocked": by_state.get("blocked", 0),
        "takeover": by_state.get("takeover", 0),
        "error": by_state.get("error", 0),
        "max_steps": by_state.get("max_steps", 0),
        "by_state": by_state,
        "by_operation": by_operation,
        "note": (
            "Public WebBench rows do not include the leaderboard evaluator or hidden ground truth; "
            "completion_score is Lumen stopped-with-answer/completion rate, not official WebBench accuracy."
        ),
    }


async def run_webbench_manifest(
    *,
    manifest_path: Path,
    output_dir: Path,
    brain: LlmBrain,
    task_ids: str | None = None,
    limit: int | None = None,
    max_steps: int = 10,
    headless: bool = True,
    allow_write: bool = False,
    timeout_seconds: int = 180,
    resume: bool = True,
    body_kind: str = "chrome",
) -> dict[str, Any]:
    # utf-8-sig: Windows tooling routinely writes manifests with a BOM.
    raw = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    tasks = select_webbench_tasks(raw, task_ids=task_ids, limit=limit, allow_write=allow_write)
    if not tasks:
        raise ValueError("No WebBench manifest tasks matched the requested filter.")
    output_dir.mkdir(parents=True, exist_ok=True)

    runs: list[WebBenchTaskRun] = []
    for index, task in enumerate(tasks, start=1):
        task_id = str(task.get("external_id") or task.get("task_id") or task.get("id") or "")
        cached = _load_cached_run(output_dir, task_id) if resume else None
        if cached:
            run = cached
        else:
            try:
                run = await asyncio.wait_for(
                    run_webbench_task(
                        task,
                        output_dir=output_dir,
                        brain=brain,
                        max_steps=max_steps,
                        headless=headless,
                        allow_write=allow_write,
                        body_kind=body_kind,
                    ),
                    timeout=timeout_seconds,
                )
            except asyncio.TimeoutError:
                run = _timeout_run(task, output_dir=output_dir, timeout_seconds=timeout_seconds)
        runs.append(run)
        print(
            json.dumps(
                {
                    "index": index,
                    "total": len(tasks),
                    "task_id": run.task_id,
                    "operation": run.operation,
                    "final_state": run.final_state,
                    "completed": run.completed,
                    "steps": run.steps,
                    "cached": cached is not None,
                }
            ),
            flush=True,
        )

    summary = score_webbench_runs(runs)
    from app.agent_eval.scorecard import benchmark_run_context

    context = benchmark_run_context(
        benchmark="webbench",
        parameters={
            "body_kind": body_kind,
            "max_steps": max_steps,
            "allow_write": allow_write,
            "task_ids": [str(t.get("external_id") or t.get("task_id") or t.get("id") or "") for t in tasks],
            "manifest": str(manifest_path),
        },
    )
    result = {"context": context, "summary": summary, "runs": [asdict(run) for run in runs]}
    (output_dir / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


async def run_webbench_task(
    task: dict[str, Any],
    *,
    output_dir: Path,
    brain: LlmBrain,
    max_steps: int,
    headless: bool,
    allow_write: bool,
    body_kind: str = "chrome",
) -> WebBenchTaskRun:
    task_id = str(task.get("external_id") or task.get("task_id") or task.get("id") or "")
    if not task_id:
        raise ValueError("WebBench task is missing external_id/task_id")
    operation = str(task.get("operation") or "UNKNOWN").upper()
    if operation != "READ" and not allow_write:
        raise ValueError(f"WebBench task {task_id} is {operation}; pass allow_write=True to run non-READ tasks")
    start_urls = task.get("start_urls") or []
    if not start_urls:
        raise ValueError(f"WebBench task {task_id} has no start_urls")
    start_url = str(start_urls[0])
    task_dir = output_dir / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    eval_task = EvalTask(
        id=f"webbench_{task_id}",
        goal=_webbench_goal(str(task.get("goal") or ""), operation=operation, allow_write=allow_write),
        initial_url=start_url,
        family=str(task.get("family") or "webbench"),
        max_steps=max_steps,
    )
    body = resolve_eval_body(body_kind, headless=headless, har_path=task_dir / "network.har")
    receipts: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    final_action: BrowserAction | None = None
    final_state = "max_steps"
    error: str | None = None
    outcome = None

    try:
        obs = await body.reset(eval_task)
        observations.append(obs.raw)
        for _step in range(max_steps):
            action = await asyncio.to_thread(brain, eval_task, obs, receipts)
            actions.append({"step": _step + 1, "action": asdict(action)})
            if action.kind == "screenshot":
                shot = await body.capture_screenshot()
                obs = await body.observe()
                if shot.get("image"):
                    obs.image = shot["image"]
                observations.append(obs.raw)
                continue
            if action.kind == "stop":
                if operation == "READ" and not _answer_from_action(action):
                    receipts.append(_invalid_empty_read_stop_receipt(action))
                    obs = await body.observe()
                    observations.append(obs.raw)
                    continue
                final_action = action
                final_state = "success"
                break
            if action.kind in {"blocked", "takeover"}:
                final_action = action
                final_state = action.kind
                break
            receipt = await body.execute(action)
            receipts.append(receipt)
            previous = observations[-1]
            obs = await body.observe()
            observations.append(obs.raw)
            receipt["progress"] = "advanced" if _observation_signature(obs) != _observation_signature_from_raw(previous) else "no_change"
        if final_state == "max_steps":
            final_action = BrowserAction(kind="blocked", reason=f"max steps {max_steps} reached")
        outcome = await body.inspect_outcome()
    except Exception as exc:  # noqa: BLE001 - benchmark runs serialize task failures.
        final_state = "error"
        error = str(exc)
        try:
            outcome = await body.inspect_outcome()
        except Exception:  # noqa: BLE001 - original error is the useful one.
            outcome = None
    finally:
        await body.cleanup()

    answer = _answer_from_action(final_action)
    completed = _completed(final_state=final_state, operation=operation, answer=answer)
    run = WebBenchTaskRun(
        task_id=task_id,
        operation=operation,
        start_url=start_url,
        final_state=final_state,
        completed=completed,
        answer=answer,
        steps=len(receipts),
        error=error if error else None if final_state == "success" else _halt_reason(final_action, final_state),
        submitted_forms=list(getattr(outcome, "submitted_forms", []) if outcome else []),
        downloads=list(getattr(outcome, "downloads", []) if outcome else []),
        uploads=list(getattr(outcome, "uploads", []) if outcome else []),
        output_dir=str(task_dir),
        trace_file=str(task_dir / "network.har"),
    )
    _write_task_artifacts(task_dir=task_dir, task=task, run=run, actions=actions, receipts=receipts, observations=observations)
    return run


def run_webbench_manifest_sync(
    *,
    manifest_path: Path,
    output_dir: Path,
    task_ids: str | None = None,
    limit: int | None = None,
    max_steps: int = 10,
    headless: bool = True,
    allow_write: bool = False,
    timeout_seconds: int = 180,
    resume: bool = True,
    provider_id: int | None = None,
    body_kind: str = "chrome",
) -> dict[str, Any]:
    init_db()
    with SessionLocal() as db:
        model = LumenModelClient(db)
        provider = model.provider_service.provider_by_id(provider_id) if provider_id else None
        brain = LlmBrain(model, provider=provider)
        return asyncio.run(
            run_webbench_manifest(
                manifest_path=manifest_path,
                output_dir=output_dir,
                brain=brain,
                task_ids=task_ids,
                limit=limit,
                max_steps=max_steps,
                headless=headless,
                allow_write=allow_write,
                timeout_seconds=timeout_seconds,
                resume=resume,
                body_kind=body_kind,
            )
        )


def _webbench_goal(goal: str, *, operation: str, allow_write: bool) -> str:
    if operation == "READ":
        return (
            f"{goal}\n\n"
            "This is a WebBench READ task. Use the live page to find the answer. "
            "Do not log in, create, update, delete, purchase, upload, or perform an irreversible final submit. "
            "Search and navigation controls are allowed when needed. "
            "When the requested answer is found, choose kind='stop' and put only the concise grounded answer in value. "
            "If a login, CAPTCHA, paywall, or missing information blocks the task, choose kind='blocked' with the reason."
        )
    permission = "The user permitted disposable-account WRITE tasks for this benchmark run." if allow_write else ""
    return (
        f"{goal}\n\n"
        f"This is a WebBench {operation} task. {permission} "
        "Use only disposable/test-account state. Stop at a safe completed/review state; do not pay, place an order, "
        "or make an irreversible final submit unless the task is explicitly safe and disposable. "
        "When the task is complete, choose kind='stop' and summarize the verified completion in value. "
        "If login, CAPTCHA, payment, real identity, or non-disposable state is required, choose kind='takeover' or 'blocked'."
    )


def _answer_from_action(action: BrowserAction | None) -> str | None:
    text = str((action.value if action else "") or "").strip()
    return text or None


def _completed(*, final_state: str, operation: str, answer: str | None) -> bool:
    if final_state != "success":
        return False
    if operation == "READ":
        return bool(answer)
    return True


def _halt_reason(action: BrowserAction | None, final_state: str) -> str | None:
    if not action:
        return final_state
    return action.reason or action.value or final_state


def _invalid_empty_read_stop_receipt(action: BrowserAction) -> dict[str, Any]:
    return {
        "type": "stop",
        "target": action.target or {},
        "outcome": "invalid_action",
        "progress": "no_change",
        "attempt": {"status": "not_attempted", "reason": "empty_read_stop"},
        "execution": {
            "status": "rejected",
            "error": "WebBench READ tasks require stop.value to contain the retrieved answer. Use blocked with a reason if the answer cannot be retrieved.",
        },
        "verification": {"status": "not_verified"},
    }


def _observation_signature(obs: Any) -> tuple[Any, ...]:
    return (
        getattr(obs, "url", ""),
        _signature_elements(getattr(obs, "elements", [])),
    )


def _observation_signature_from_raw(raw: dict[str, Any]) -> tuple[Any, ...]:
    return (
        raw.get("url", ""),
        _signature_elements(raw.get("elements") or []),
    )


def _signature_elements(elements: list[dict[str, Any]], limit: int = 120) -> tuple[tuple[Any, ...], ...]:
    if len(elements) <= limit:
        selected = elements
    else:
        selected = [*elements[: max(limit - 40, 1)], *elements[-40:]]
    seen: set[str] = set()
    rows = []
    for element in selected:
        ref = str(element.get("ref") or "")
        if ref in seen:
            continue
        seen.add(ref)
        rows.append((ref, element.get("role"), element.get("label"), element.get("text"), element.get("value")))
    return tuple(rows)


def _write_task_artifacts(
    *,
    task_dir: Path,
    task: dict[str, Any],
    run: WebBenchTaskRun,
    actions: list[dict[str, Any]],
    receipts: list[dict[str, Any]],
    observations: list[dict[str, Any]],
) -> None:
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "run_result.json").write_text(json.dumps(asdict(run), indent=2), encoding="utf-8")
    (task_dir / "lumen_run_log.json").write_text(
        json.dumps({"task": task, "run": asdict(run), "actions": actions, "receipts": receipts, "observations": observations}, indent=2),
        encoding="utf-8",
    )


def _load_cached_run(output_dir: Path, task_id: str) -> WebBenchTaskRun | None:
    if not task_id:
        return None
    path = output_dir / task_id / "run_result.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return WebBenchTaskRun(**data)


def _timeout_run(task: dict[str, Any], *, output_dir: Path, timeout_seconds: int) -> WebBenchTaskRun:
    task_id = str(task.get("external_id") or task.get("task_id") or task.get("id") or "")
    operation = str(task.get("operation") or "UNKNOWN").upper()
    start_url = str((task.get("start_urls") or [""])[0])
    task_dir = output_dir / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    run = WebBenchTaskRun(
        task_id=task_id,
        operation=operation,
        start_url=start_url,
        final_state="error",
        completed=False,
        answer=None,
        steps=0,
        error=f"task timed out after {timeout_seconds}s",
        submitted_forms=[],
        downloads=[],
        uploads=[],
        output_dir=str(task_dir),
        trace_file=str(task_dir / "network.har"),
    )
    _write_task_artifacts(task_dir=task_dir, task=task, run=run, actions=[], receipts=[], observations=[])
    return run
