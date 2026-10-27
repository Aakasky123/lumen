"""Run Lumen's generic browser loop against WebArena task manifests.

This adapter is benchmark glue: it renders WebArena URL placeholders, drives the
existing LlmBrain through PlaywrightEvalBody, and writes WebArena's expected
`agent_response.json` plus `network.har` files. It does not encode routes,
site-specific decisions, or answers.
"""

from __future__ import annotations

import asyncio
import ast
import json
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.agent_eval.brain import LlmBrain
from app.agent_eval.contract import BrowserAction, EvalTask
from app.agent_eval.chrome_body import resolve_eval_body
from app.lumen_model import LumenModelClient
from app.db import SessionLocal, init_db

WEBARENA_LOCAL_URLS = {
    "__SHOPPING__": "http://localhost:7770",
    "__SHOPPING_ADMIN__": "http://localhost:7780/admin",
    "__REDDIT__": "http://localhost:9999",
    "__GITLAB__": "http://localhost:8023",
    "__MAP__": "http://localhost:3000",
    "__WIKIPEDIA__": "http://localhost:8888",
}

WEBARENA_SITE_KEYS = {
    "__SHOPPING__": "shopping",
    "__SHOPPING_ADMIN__": "shopping_admin",
    "__REDDIT__": "reddit",
    "__GITLAB__": "gitlab",
    "__MAP__": "map",
    "__WIKIPEDIA__": "wikipedia",
}

WEBARENA_SITE_HEADERS = {
    "shopping_admin": {"X-M2-Admin-Auto-Login": "admin:admin1234"},
}


@dataclass(frozen=True)
class WebArenaTaskRun:
    task_id: str
    start_url: str
    final_state: str
    steps: int
    agent_response: dict[str, Any]
    output_dir: str
    trace_file: str


def render_webarena_url(template: str, url_map: dict[str, str] | None = None) -> str:
    urls = url_map or WEBARENA_LOCAL_URLS
    for placeholder, base_url in sorted(urls.items(), key=lambda item: len(item[0]), reverse=True):
        if template.startswith(placeholder):
            return template.replace(placeholder, base_url.rstrip("/"), 1)
    return template


def webarena_run_config(url_map: dict[str, str] | None = None) -> dict[str, Any]:
    urls = url_map or WEBARENA_LOCAL_URLS
    environments = {}
    for placeholder, site_key in WEBARENA_SITE_KEYS.items():
        base_url = urls.get(placeholder)
        if not base_url:
            continue
        site_urls = [base_url.rstrip("/")]
        if placeholder == "__SHOPPING_ADMIN__" and site_urls[0].endswith("/admin"):
            site_urls.append(site_urls[0].removesuffix("/admin"))
        environments[site_key] = {"urls": site_urls, "active_url_idx": 0}
        if site_key in WEBARENA_SITE_HEADERS:
            environments[site_key]["use_header_login"] = True
    return {
        "environments": environments,
        "agent_response_file_name": "agent_response.json",
        "trace_file_name": "network.har",
        "eval_result_file_name": "eval_result.json",
        "storage_state_file_name": ".storage_state.json",
    }


def parse_webarena_base_url_overrides(values: list[str] | None) -> dict[str, str]:
    urls = dict(WEBARENA_LOCAL_URLS)
    for value in values or []:
        if "=" not in value:
            raise ValueError(f"base URL override must be PLACEHOLDER=URL, got {value!r}")
        key, url = value.split("=", 1)
        key = key.strip()
        if not key.startswith("__"):
            key = f"__{key.upper()}__"
        if key not in WEBARENA_LOCAL_URLS:
            raise ValueError(f"unknown WebArena placeholder {key!r}")
        urls[key] = url.strip().rstrip("/")
    return urls


def select_manifest_tasks(raw: Any, task_ids: str | None = None) -> list[dict[str, Any]]:
    rows = raw if isinstance(raw, list) else raw.get("tasks", []) if isinstance(raw, dict) else []
    tasks = [row for row in rows if isinstance(row, dict)]
    wanted = {item.strip() for item in (task_ids or "").split(",") if item.strip()}
    if wanted:
        tasks = [task for task in tasks if str(task.get("external_id") or task.get("task_id") or task.get("id")) in wanted]
    return tasks


def final_response_from_action(action: BrowserAction | None, *, task_type: str | None = None, error: str | None = None) -> dict[str, Any]:
    normalized_type = (task_type or "RETRIEVE").upper()
    if error is not None:
        detail = error or "task did not complete"
        return {
            "task_type": normalized_type,
            "status": "UNKNOWN_ERROR",
            "retrieved_data": None,
            "error_details": detail[:500],
        }
    value = (action.value if action else "") or ""
    parsed = _parse_jsonish(value)
    if isinstance(parsed, dict) and {"task_type", "status"} <= set(parsed):
        response = dict(parsed)
        response.setdefault("retrieved_data", None)
        response.setdefault("error_details", None)
        return response
    if normalized_type == "RETRIEVE":
        retrieved = parsed if isinstance(parsed, list) else ([parsed] if parsed not in (None, "") else [])
    else:
        retrieved = None
    return {
        "task_type": normalized_type,
        "status": "SUCCESS",
        "retrieved_data": retrieved,
        "error_details": None,
    }


def _parse_jsonish(value: str) -> Any:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            return ast.literal_eval(text)
        except (ValueError, SyntaxError):
            return text


async def run_webarena_manifest(
    *,
    manifest_path: Path,
    output_dir: Path,
    brain: LlmBrain,
    task_ids: str | None = None,
    max_steps: int = 25,
    headless: bool = True,
    url_map: dict[str, str] | None = None,
    reset_env_url: str | None = None,
    body_kind: str = "chrome",
) -> list[WebArenaTaskRun]:
    # utf-8-sig: Windows tooling routinely writes manifests with a BOM.
    raw = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    tasks = select_manifest_tasks(raw, task_ids=task_ids)
    if not tasks:
        raise ValueError("No WebArena manifest tasks matched the requested filter.")
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[WebArenaTaskRun] = []
    from app.agent_eval.scorecard import benchmark_run_context

    context = benchmark_run_context(
        benchmark="webarena",
        parameters={
            "body_kind": body_kind,
            "max_steps": max_steps,
            "task_ids": [str(t.get("external_id") or t.get("task_id") or t.get("id") or "") for t in tasks],
            "manifest": str(manifest_path),
        },
    )
    (output_dir / "run_context.json").write_text(json.dumps(context, indent=2), encoding="utf-8")
    for task in tasks:
        if reset_env_url:
            reset_webarena_environment(reset_env_url)
        results.append(
            await run_webarena_task(
                task,
                output_dir=output_dir,
                brain=brain,
                max_steps=max_steps,
                headless=headless,
                url_map=url_map,
                body_kind=body_kind,
            )
        )
    return results


async def run_webarena_task(
    task: dict[str, Any],
    *,
    output_dir: Path,
    brain: LlmBrain,
    max_steps: int,
    headless: bool,
    url_map: dict[str, str] | None = None,
    body_kind: str = "chrome",
) -> WebArenaTaskRun:
    task_id = str(task.get("external_id") or task.get("task_id") or task.get("id") or "")
    if not task_id:
        raise ValueError("WebArena task is missing external_id/task_id")
    start_urls = task.get("start_urls") or []
    if not start_urls:
        raise ValueError(f"WebArena task {task_id} has no start_urls")
    start_url = render_webarena_url(str(start_urls[0]), url_map=url_map)
    task_dir = output_dir / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    config = webarena_run_config(url_map=url_map)
    (task_dir / "run_config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")

    operation = str(task.get("operation") or "RETRIEVE").upper()
    goal = _benchmark_goal(str(task.get("goal") or ""), operation=operation)
    eval_task = EvalTask(
        id=f"webarena_{task_id}",
        goal=goal,
        initial_url=start_url,
        family=str(task.get("family") or "webarena"),
        max_steps=max_steps,
    )
    body = resolve_eval_body(
        body_kind,
        headless=headless,
        har_path=task_dir / "network.har",
        extra_http_headers=_headers_for_task(task),
    )
    receipts: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    final_action: BrowserAction | None = None
    final_state = "max_steps"
    try:
        obs = await body.reset(eval_task)
        observations.append(obs.raw)
        for _step in range(max_steps):
            action = brain(eval_task, obs, receipts)
            if action.kind == "screenshot":
                shot = await body.capture_screenshot()
                obs = await body.observe()
                if shot.get("image"):
                    obs.image = shot["image"]
                observations.append(obs.raw)
                continue
            if action.kind == "stop":
                final_action = action
                final_state = "success"
                break
            if action.kind in {"blocked", "takeover"}:
                final_action = action
                final_state = action.kind
                break
            receipt = await body.execute(action)
            receipts.append(receipt)
            obs = await body.observe()
            observations.append(obs.raw)
            receipt["progress"] = "advanced" if _observation_signature(obs) != _observation_signature_from_raw(observations[-2]) else "no_change"
        if final_state == "max_steps":
            final_action = BrowserAction(kind="blocked", reason=f"max steps {max_steps} reached")
        response = final_response_from_action(
            final_action,
            task_type=operation,
            error=None
            if final_state == "success"
            else ((final_action.reason or final_action.value or final_state) if final_action else final_state),
        )
    except Exception as exc:  # noqa: BLE001 - benchmark run failures must be serialized, not hidden.
        final_state = "error"
        response = final_response_from_action(None, task_type=operation, error=str(exc))
    finally:
        await body.cleanup()

    (task_dir / "agent_response.json").write_text(json.dumps(response, indent=2), encoding="utf-8")
    (task_dir / "lumen_run_log.json").write_text(
        json.dumps({"task": task, "final_state": final_state, "receipts": receipts, "observations": observations}, indent=2),
        encoding="utf-8",
    )
    return WebArenaTaskRun(
        task_id=task_id,
        start_url=start_url,
        final_state=final_state,
        steps=len(receipts),
        agent_response=response,
        output_dir=str(task_dir),
        trace_file=str(task_dir / "network.har"),
    )


def _benchmark_goal(goal: str, *, operation: str) -> str:
    return (
        f"{goal}\n\n"
        "When you have completed the task, choose kind='stop'. "
        "For retrieval tasks, set value to valid JSON in this exact shape: "
        f'{{"task_type":"{operation}","status":"SUCCESS","retrieved_data":[...],"error_details":null}}. '
        "Use numbers as numbers and objects/lists exactly as requested by the user. "
        "If you cannot complete it, use kind='blocked' with the reason."
    )


def _headers_for_task(task: dict[str, Any]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for site in task.get("sites") or []:
        headers.update(WEBARENA_SITE_HEADERS.get(str(site), {}))
    return headers


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


def run_webarena_manifest_sync(
    *,
    manifest_path: Path,
    output_dir: Path,
    task_ids: str | None,
    max_steps: int,
    headless: bool,
    url_map: dict[str, str] | None,
    reset_env_url: str | None = None,
    provider_id: int | None = None,
    body_kind: str = "chrome",
) -> list[dict[str, Any]]:
    init_db()
    with SessionLocal() as db:
        model = LumenModelClient(db)
        provider = model.provider_service.provider_by_id(provider_id) if provider_id else None
        brain = LlmBrain(model, provider=provider)
        runs = asyncio.run(
            run_webarena_manifest(
                manifest_path=manifest_path,
                output_dir=output_dir,
                brain=brain,
                task_ids=task_ids,
                max_steps=max_steps,
                headless=headless,
                url_map=url_map,
                reset_env_url=reset_env_url,
                body_kind=body_kind,
            )
        )
    return [asdict(run) for run in runs]


def reset_webarena_environment(env_ctrl_url: str, *, timeout: int = 120) -> None:
    url = env_ctrl_url.rstrip("/") + "/init"
    request = urllib.request.Request(url, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - local benchmark env URL.
        payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("success"):
        raise RuntimeError(f"WebArena environment reset failed: {payload}")
