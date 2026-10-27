"""External benchmark bridge for Lumen's model-agnostic browser loop.

This module keeps third-party benchmarks outside the runtime brain. It
normalizes task metadata into a small manifest that can feed Lumen's existing
eval contract, and it exposes prerequisite checks plus WebArena CLI wrappers
for local benchmark setup.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

WEBARENA_DOCKER_IMAGE = "ghcr.io/servicenow/webarena-verified:latest"
APP_ROOT = Path(__file__).resolve().parents[2]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))


@dataclass(frozen=True)
class BenchmarkTarget:
    name: str
    mode: str
    purpose: str
    first_gate: str
    lumen_use: str


@dataclass(frozen=True)
class BenchmarkReadiness:
    name: str
    available: bool
    detail: str


@dataclass(frozen=True)
class ExternalBenchmarkTask:
    benchmark: str
    external_id: str
    goal: str
    family: str
    sites: list[str]
    operation: str | None = None
    start_urls: list[str] | None = None
    source: dict[str, Any] | None = None


def benchmark_targets() -> list[BenchmarkTarget]:
    return [
        BenchmarkTarget(
            name="webarena-verified",
            mode="end_to_end_offline_environment",
            purpose="Measure observe-reason-act-verify behavior in replicated web apps with deterministic scoring.",
            first_gate="Run a 5-10 task smoke from the hard subset before scaling to the 258-task hard subset.",
            lumen_use="Adapter around EvalBody/BrowserAction plus WebArena agent response and HAR scoring.",
        ),
        BenchmarkTarget(
            name="mind2web",
            mode="static_next_action_dataset",
            purpose="Measure whether the brain chooses the grounded next element/action from cached DOM evidence.",
            first_gate="Run an action-selection sample; do not treat it as end-to-end browser success.",
            lumen_use="Prompt/evidence regression corpus for LlmBrain action selection, not an execution verifier.",
        ),
        BenchmarkTarget(
            name="online-mind2web",
            mode="live_web_task_benchmark",
            purpose="Measure live browser-task completion on real sites with trajectory/evaluator artifacts.",
            first_gate="Normalize a task or trajectory sample, then run only account-safe tasks with final-submit gating.",
            lumen_use="Live-web eval target around Lumen's generic loop; benchmark glue supplies tasks, not routes.",
        ),
        BenchmarkTarget(
            name="webbench",
            mode="live_read_write_web_benchmark",
            purpose="Measure broad READ information extraction and WRITE state-changing website workflows.",
            first_gate="Start with READ tasks; gate WRITE tasks behind account/safety review because they can mutate live state.",
            lumen_use="Manifest adapter for public CSV rows; live execution should use the same BrowserAction loop as WebArena.",
        ),
        BenchmarkTarget(
            name="webvoyager",
            mode="live_multisite_web_task_benchmark",
            purpose="Measure goal completion across real websites from start URLs and natural-language tasks.",
            first_gate="Run a small headful smoke on low-risk public browsing tasks before scaling.",
            lumen_use="Manifest adapter for JSONL rows; useful for navigation/recovery loop pressure.",
        ),
        BenchmarkTarget(
            name="browsecomp",
            mode="research_answer_benchmark",
            purpose="Measure persistent web research and final-answer quality for hard information-seeking questions.",
            first_gate="Use as a research-answer gate, not as proof of browser UI manipulation.",
            lumen_use="Final-answer/evidence quality eval for the brain; no browser-state verifier by default.",
        ),
        BenchmarkTarget(
            name="clawbench",
            mode="live_stateful_workflow_benchmark",
            purpose="Measure real-world web workflows such as ordering, scheduling, purchases, and applications.",
            first_gate="Normalize tasks first; run only disposable-account scenarios with explicit no-final-submit policy.",
            lumen_use="High-risk live-workflow target for the generic loop; benchmark glue imports instructions/eval schema only.",
        ),
    ]


def readiness(*, online: bool = False) -> list[BenchmarkReadiness]:
    checks = [
        BenchmarkReadiness(
            name="docker",
            available=bool(shutil.which("docker")),
            detail="required for WebArena-Verified containerized CLI and environments",
        ),
        BenchmarkReadiness(
            name="uvx",
            available=bool(shutil.which("uvx")),
            detail="optional no-install WebArena-Verified entrypoint",
        ),
        BenchmarkReadiness(
            name="webarena_verified_python_package",
            available=find_spec("webarena_verified") is not None,
            detail="optional Python API package; Docker path does not require it",
        ),
    ]
    if online and shutil.which("docker"):
        proc = subprocess.run(
            ["docker", "manifest", "inspect", WEBARENA_DOCKER_IMAGE],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        checks.append(
            BenchmarkReadiness(
                name="webarena_verified_docker_image",
                available=proc.returncode == 0,
                detail=WEBARENA_DOCKER_IMAGE if proc.returncode == 0 else (proc.stderr or proc.stdout).strip(),
            )
        )
    return checks


def webarena_docker_command(args: list[str], *, mounts: dict[Path, str] | None = None) -> list[str]:
    command = ["docker", "run", "--rm"]
    for host_path, container_path in (mounts or {}).items():
        command.extend(["-v", f"{host_path.resolve().as_posix()}:{container_path}"])
    return [*command, WEBARENA_DOCKER_IMAGE, *args]


def run_webarena_docker(args: list[str], *, timeout: int = 300, mounts: dict[Path, str] | None = None) -> Any:
    proc = subprocess.run(
        webarena_docker_command(args, mounts=mounts),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    output = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
    if proc.returncode != 0:
        raise RuntimeError(output.strip() or f"webarena-verified exited {proc.returncode}")
    return extract_json_payload(output)


def export_webarena_subset(name: str, output: Path, *, timeout: int = 300) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    container_output = f"/data/{output.name}"
    proc = subprocess.run(
        webarena_docker_command(
            ["subset-export", "--name", name, "--output", container_output],
            mounts={output.parent: "/data"},
        ),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError("\n".join(part for part in (proc.stdout, proc.stderr) if part).strip())
    if not output.is_file():
        raise RuntimeError(f"WebArena subset export did not create {output}")
    return output


def task_ids_from_webarena_subset(raw: Any, *, limit: int | None = None) -> list[str]:
    ids: list[str] = []
    for row in _as_rows(raw):
        task_id = row.get("task_id") or row.get("id")
        if task_id in (None, ""):
            continue
        ids.append(str(task_id))
        if limit is not None and len(ids) >= limit:
            break
    return ids


def write_webarena_hard_smoke_manifest(*, count: int, output: Path, subset_cache: Path | None = None) -> list[ExternalBenchmarkTask]:
    if count <= 0:
        raise ValueError("count must be positive")
    subset_path = subset_cache or output.with_name("webarena_verified_hard_subset.json")
    if not subset_path.is_file():
        export_webarena_subset("webarena-verified-hard", subset_path)
    task_ids = task_ids_from_webarena_subset(load_json(subset_path), limit=count)
    if not task_ids:
        raise RuntimeError(f"No task ids found in {subset_path}")
    raw = run_webarena_docker(["agent-input-get", "--task-ids", ",".join(task_ids)])
    tasks = normalize_webarena_agent_inputs(raw)
    write_manifest(tasks, output)
    return tasks


def extract_json_payload(text: str) -> Any:
    """Extract the first valid JSON object/array from CLI output with log lines."""
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            parsed, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        return parsed
    raise ValueError("No JSON payload found in benchmark output.")


def normalize_webarena_agent_inputs(raw: Any) -> list[ExternalBenchmarkTask]:
    rows = _as_rows(raw)
    tasks: list[ExternalBenchmarkTask] = []
    for row in rows:
        task_id = row.get("task_id") or row.get("id")
        if task_id in (None, ""):
            continue
        sites = _string_list(row.get("sites") or row.get("site"))
        tasks.append(
            ExternalBenchmarkTask(
                benchmark="webarena-verified",
                external_id=str(task_id),
                goal=str(row.get("intent") or row.get("goal") or "").strip(),
                family=f"webarena:{'+'.join(sites) if sites else 'unknown'}",
                sites=sites,
                operation=_optional_str(row.get("task_type") or row.get("operation") or row.get("main_objective")),
                start_urls=_string_list(row.get("start_urls") or row.get("start_url") or []),
                source={k: v for k, v in row.items() if k not in {"intent"}},
            )
        )
    return tasks


def normalize_mind2web_records(raw: Any) -> list[ExternalBenchmarkTask]:
    rows = _as_rows(raw)
    tasks: list[ExternalBenchmarkTask] = []
    for row in rows:
        task_id = row.get("annotation_id") or row.get("task_id") or row.get("id")
        goal = row.get("confirmed_task") or row.get("task") or row.get("goal") or row.get("intent")
        if task_id in (None, "") or not str(goal or "").strip():
            continue
        site = row.get("website") or row.get("domain") or row.get("site")
        sites = _string_list(site)
        action_count = len(row.get("actions") or row.get("action_reprs") or [])
        tasks.append(
            ExternalBenchmarkTask(
                benchmark="mind2web",
                external_id=str(task_id),
                goal=str(goal).strip(),
                family="mind2web:next_action",
                sites=sites,
                operation="next_action" if action_count <= 1 else "multi_step_action_sequence",
                start_urls=_string_list(row.get("url") or row.get("start_url") or []),
                source={"action_count": action_count, "website": site},
            )
        )
    return tasks


def normalize_online_mind2web_records(raw: Any) -> list[ExternalBenchmarkTask]:
    rows = _as_rows(raw)
    tasks: list[ExternalBenchmarkTask] = []
    for index, row in enumerate(rows):
        task_id = _first_present(row, "task_id", "annotation_id", "id") or f"row-{index}"
        goal = _first_present(row, "task", "confirmed_task", "goal", "intent", "instruction")
        if not str(goal or "").strip():
            continue
        start_url = (
            _first_present(row, "start_url", "url", "website_url", "web")
            or _first_url_from_history(row.get("action_history"))
        )
        sites = _string_list(_first_present(row, "website", "domain", "site") or _site_from_url(start_url))
        action_count = len(row.get("action_history") or row.get("actions") or [])
        tasks.append(
            ExternalBenchmarkTask(
                benchmark="online-mind2web",
                external_id=str(task_id),
                goal=str(goal).strip(),
                family="online-mind2web:live_task",
                sites=sites,
                operation="live_web_task",
                start_urls=_string_list(start_url),
                source={
                    "schema_version": _first_present(row, "schema_version"),
                    "reference_length": _first_present(row, "reference_length"),
                    "action_count": action_count,
                },
            )
        )
    return tasks


def normalize_webbench_records(raw: Any) -> list[ExternalBenchmarkTask]:
    rows = _as_rows(raw)
    tasks: list[ExternalBenchmarkTask] = []
    for index, row in enumerate(rows):
        task_id = _first_present(row, "ID", "id", "task_id") or f"row-{index}"
        goal = _first_present(row, "Task", "task", "goal", "instruction")
        if not str(goal or "").strip():
            continue
        start_url = _first_present(row, "Starting URL", "starting_url", "start_url", "url")
        category = str(_first_present(row, "Category", "category", "operation", "task_type") or "UNKNOWN").strip().upper()
        operation = "READ" if category == "READ" else "WRITE" if category else None
        tasks.append(
            ExternalBenchmarkTask(
                benchmark="webbench",
                external_id=str(task_id),
                goal=str(goal).strip(),
                family=f"webbench:{(operation or 'unknown').lower()}",
                sites=_string_list(_site_from_url(start_url)),
                operation=operation,
                start_urls=_string_list(start_url),
                source={"category": category},
            )
        )
    return tasks


def normalize_webvoyager_records(raw: Any) -> list[ExternalBenchmarkTask]:
    rows = _as_rows(raw)
    tasks: list[ExternalBenchmarkTask] = []
    for index, row in enumerate(rows):
        task_id = _first_present(row, "id", "task_id") or f"row-{index}"
        goal = _first_present(row, "ques", "question", "task", "goal", "instruction")
        if not str(goal or "").strip():
            continue
        start_url = _first_present(row, "web", "url", "start_url")
        web_name = _first_present(row, "web_name", "website", "site") or _site_from_url(start_url) or "unknown"
        tasks.append(
            ExternalBenchmarkTask(
                benchmark="webvoyager",
                external_id=str(task_id),
                goal=str(goal).strip(),
                family=f"webvoyager:{_slug(str(web_name))}",
                sites=_string_list(_site_from_url(start_url) or web_name),
                operation="live_web_task",
                start_urls=_string_list(start_url),
                source={"web_name": web_name},
            )
        )
    return tasks


def normalize_browsecomp_records(raw: Any) -> list[ExternalBenchmarkTask]:
    rows = _as_rows(raw)
    tasks: list[ExternalBenchmarkTask] = []
    for index, row in enumerate(rows):
        task_id = _first_present(row, "id", "task_id", "question_id") or f"row-{index}"
        goal = _first_present(row, "question", "prompt", "problem", "task", "goal")
        if not str(goal or "").strip():
            continue
        answer = _first_present(row, "answer", "target", "gold", "gold_answer", "ideal")
        tasks.append(
            ExternalBenchmarkTask(
                benchmark="browsecomp",
                external_id=str(task_id),
                goal=str(goal).strip(),
                family="browsecomp:research_qa",
                sites=[],
                operation="research_answer",
                start_urls=[],
                source={"has_reference_answer": bool(str(answer or "").strip())},
            )
        )
    return tasks


def normalize_clawbench_records(raw: Any) -> list[ExternalBenchmarkTask]:
    rows = _as_rows(raw)
    tasks: list[ExternalBenchmarkTask] = []
    for index, row in enumerate(rows):
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        eval_schema = row.get("eval_schema") if isinstance(row.get("eval_schema"), dict) else {}
        task_id = _first_present(row, "task_id", "id") or metadata.get("task_id") or f"row-{index}"
        goal = _first_present(row, "instruction", "task", "goal") or metadata.get("description")
        if not str(goal or "").strip():
            continue
        metaclass = metadata.get("metaclass") or "workflow"
        task_class = metadata.get("class") or metadata.get("platform") or "unknown"
        method = str(eval_schema.get("method") or "").strip().upper()
        tasks.append(
            ExternalBenchmarkTask(
                benchmark="clawbench",
                external_id=str(task_id),
                goal=str(goal).strip(),
                family=f"clawbench:{_slug(str(metaclass))}:{_slug(str(task_class))}",
                sites=_string_list(metadata.get("sites_involved") or metadata.get("site") or row.get("sites")),
                operation="state_change" if method and method != "GET" else "live_web_task",
                start_urls=_string_list(row.get("start_url") or row.get("url")),
                source={
                    "platform": metadata.get("platform"),
                    "eval_method": method or None,
                    "time_limit_minutes": row.get("time_limit"),
                },
            )
        )
    return tasks


NORMALIZERS = {
    "webarena-verified": normalize_webarena_agent_inputs,
    "mind2web": normalize_mind2web_records,
    "online-mind2web": normalize_online_mind2web_records,
    "webbench": normalize_webbench_records,
    "webvoyager": normalize_webvoyager_records,
    "browsecomp": normalize_browsecomp_records,
    "clawbench": normalize_clawbench_records,
}


def load_records(path: Path) -> Any:
    text = path.read_text(encoding="utf-8-sig")
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return list(csv.DictReader(text.splitlines()))
    if suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    if suffix == ".json":
        return json.loads(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return list(csv.DictReader(text.splitlines()))


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_manifest(tasks: list[ExternalBenchmarkTask], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(task) for task in tasks], indent=2), encoding="utf-8")


def _as_rows(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        for key in ("tasks", "data", "rows", "items"):
            value = raw.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [raw]
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _optional_str(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _first_present(row: dict[str, Any], *keys: str) -> Any:
    if not row:
        return None
    normalized = {str(key).casefold().replace("_", " ").strip(): value for key, value in row.items()}
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
        value = normalized.get(str(key).casefold().replace("_", " ").strip())
        if value not in (None, ""):
            return value
    return None


def _first_url_from_history(history: Any) -> str | None:
    if not isinstance(history, list):
        return None
    for item in history:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if isinstance(url, str) and url.strip():
            return url.strip()
    return None


def _site_from_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = urlparse(value.strip())
    host = parsed.netloc or parsed.path.split("/", 1)[0]
    return host.strip().lower() or None


def _slug(value: str) -> str:
    chars = [char.lower() if char.isalnum() else "-" for char in value.strip()]
    return "-".join(part for part in "".join(chars).split("-") if part) or "unknown"


def _print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, default=str))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Lumen external benchmark bridge")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("targets", help="List supported benchmark targets")

    doctor = subparsers.add_parser("doctor", help="Check local benchmark prerequisites")
    doctor.add_argument("--online", action="store_true", help="also check remote Docker image metadata")

    webarena_inputs = subparsers.add_parser("webarena-agent-inputs", help="Export WebArena task inputs via Docker")
    webarena_inputs.add_argument("--task-ids", help="comma-separated task ids")
    webarena_inputs.add_argument("--sites", help="comma-separated sites")
    webarena_inputs.add_argument("--task-type", help="task type filter")
    webarena_inputs.add_argument("--output", required=True, help="manifest output path")

    webarena_hard_smoke = subparsers.add_parser("webarena-hard-smoke", help="Export a small hard-subset WebArena manifest")
    webarena_hard_smoke.add_argument("--count", type=int, default=5, help="number of hard-subset tasks to include")
    webarena_hard_smoke.add_argument("--output", required=True, help="manifest output path")
    webarena_hard_smoke.add_argument("--subset-cache", help="optional host path for the hard-subset export cache")

    webarena_run = subparsers.add_parser("webarena-run", help="Run Lumen against a WebArena manifest and write evaluator files")
    webarena_run.add_argument("--manifest", required=True, help="normalized WebArena manifest path")
    webarena_run.add_argument("--output-dir", required=True, help="WebArena run output directory")
    webarena_run.add_argument("--task-ids", help="comma-separated task ids to run")
    webarena_run.add_argument("--max-steps", type=int, default=25)
    webarena_run.add_argument("--headful", action="store_true", help="show browser window")
    webarena_run.add_argument("--base-url", action="append", help="override placeholder URL, e.g. __SHOPPING_ADMIN__=http://localhost:7780")
    webarena_run.add_argument("--reset-env-url", help="optional WebArena env-control URL to POST /init before each task")
    webarena_run.add_argument("--provider", type=int, default=None, help="pin the brain to a model provider id")

    webbench_run = subparsers.add_parser("webbench-run", help="Run Lumen against a WebBench manifest")
    webbench_run.add_argument("--manifest", required=True, help="normalized WebBench manifest path")
    webbench_run.add_argument("--output-dir", required=True, help="run artifact output directory")
    webbench_run.add_argument("--task-ids", help="comma-separated task ids to run")
    webbench_run.add_argument("--limit", type=int, help="number of matching tasks to run")
    webbench_run.add_argument("--max-steps", type=int, default=10)
    webbench_run.add_argument("--timeout-seconds", type=int, default=180)
    webbench_run.add_argument("--headful", action="store_true", help="show browser window")
    webbench_run.add_argument("--allow-write", action="store_true", help="include WebBench WRITE rows; use only with disposable accounts")
    webbench_run.add_argument("--no-resume", action="store_true", help="rerun tasks even if output already exists")
    webbench_run.add_argument("--provider", type=int, default=None, help="pin the brain to a model provider id")

    normalize = subparsers.add_parser("normalize", help="Normalize benchmark CSV/JSON/JSONL into Lumen manifest shape")
    normalize.add_argument("--benchmark", choices=sorted(NORMALIZERS), required=True)
    normalize.add_argument("--input", required=True)
    normalize.add_argument("--output", required=True)

    args = parser.parse_args(argv)
    if args.command == "targets":
        _print_json([asdict(target) for target in benchmark_targets()])
        return
    if args.command == "doctor":
        _print_json([asdict(check) for check in readiness(online=args.online)])
        return
    if args.command == "webarena-agent-inputs":
        command = ["agent-input-get"]
        if args.task_ids:
            command.extend(["--task-ids", args.task_ids])
        if args.sites:
            command.extend(["--sites", args.sites])
        if args.task_type:
            command.extend(["--task-type", args.task_type])
        raw = run_webarena_docker(command)
        tasks = normalize_webarena_agent_inputs(raw)
        write_manifest(tasks, Path(args.output))
        _print_json({"written": args.output, "task_count": len(tasks)})
        return
    if args.command == "webarena-hard-smoke":
        tasks = write_webarena_hard_smoke_manifest(
            count=args.count,
            output=Path(args.output),
            subset_cache=Path(args.subset_cache) if args.subset_cache else None,
        )
        _print_json({"written": args.output, "task_count": len(tasks), "task_ids": [task.external_id for task in tasks]})
        return
    if args.command == "webarena-run":
        from app.agent_eval.webarena_runner import parse_webarena_base_url_overrides, run_webarena_manifest_sync

        runs = run_webarena_manifest_sync(
            manifest_path=Path(args.manifest),
            output_dir=Path(args.output_dir),
            task_ids=args.task_ids,
            max_steps=args.max_steps,
            headless=not args.headful,
            url_map=parse_webarena_base_url_overrides(args.base_url),
            reset_env_url=args.reset_env_url,
            provider_id=args.provider,
        )
        _print_json({"run_count": len(runs), "runs": runs})
        return
    if args.command == "webbench-run":
        from app.agent_eval.webbench_runner import run_webbench_manifest_sync

        result = run_webbench_manifest_sync(
            manifest_path=Path(args.manifest),
            output_dir=Path(args.output_dir),
            task_ids=args.task_ids,
            limit=args.limit,
            max_steps=args.max_steps,
            headless=not args.headful,
            allow_write=args.allow_write,
            timeout_seconds=args.timeout_seconds,
            resume=not args.no_resume,
            provider_id=args.provider,
        )
        _print_json(result["summary"])
        return
    if args.command == "normalize":
        raw = load_records(Path(args.input))
        tasks = NORMALIZERS[args.benchmark](raw)
        write_manifest(tasks, Path(args.output))
        _print_json({"written": args.output, "task_count": len(tasks)})


if __name__ == "__main__":
    main(sys.argv[1:])
