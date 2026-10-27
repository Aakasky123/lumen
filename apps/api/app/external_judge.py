"""External judge seat: a bench-only file mailbox that answers every non-brain model purpose.

When ``LUMEN_EXTERNAL_JUDGE_DIR`` is set (off by default; production is unaffected), every structured
model call that is not the brain's step decision (judges, classifiers, adjudicators, verifiers, the completion
grader, the safety judges, session summaries) is written to a judge mailbox and answered by a judge seat
instead of a provider. The brain keeps ``vision_agent_step`` only (``external_brain.py``); the safety purposes
(``external_brain.SAFETY_PURPOSES``) never reach the brain mailbox, only this one, so whoever writes the brain
mailbox never decides secrets, payments or readiness. Vision escalation is a different provider answering the
same ``vision_agent_step`` purpose, so it goes to the brain mailbox.

Independence: the judge root must not be inside, contain, or equal the brain root (a misconfigured pair makes
every judge call fail closed), requests carry only the harness's own system/prompt/schema/images (never the
brain's notes or mailbox paths), and the judge seat must be a different subagent from the brain.

    <judge_dir>/run-<id>/step-0007.request.json  {purpose, system, prompt, schema, images, deadline_seconds}
    <judge_dir>/run-<id>/step-0007.response.json written by the judge seat: the raw JSON verdict

Fail closed: no reply within the purpose's own deadline, a reply that is not a JSON object, or one that
violates the requested schema is returned as ``ok=False`` with ``blocking_reason`` ``external_judge_*``, exactly
like an unavailable provider; every gate already treats that as "no verdict".
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from app import external_brain, judge_limiter, model_call_ledger
from app.config import PROJECT_ROOT, get_settings

DEFAULT_DEADLINE_SECONDS = 60.0
_POLL_SECONDS = 0.05
_LOCK = threading.Lock()

# Purposes whose own deadline is an lumen_model constant (the one their provider call would have had).
_DEADLINE_CONSTANTS = {
    "grounding_judge": "GROUNDING_JUDGE_TIMEOUT_SECONDS",
    "payment_floor_judge": "PAYMENT_FLOOR_JUDGE_TIMEOUT_SECONDS",
    "target_binding_judge": "TARGET_BINDING_JUDGE_TIMEOUT_SECONDS",
    "control_reference_conflict": "TARGET_BINDING_JUDGE_TIMEOUT_SECONDS",
    "task_completion_verification": "TASK_COMPLETION_JUDGE_TIMEOUT_SECONDS",
    "blocked_click_semantics": "BLOCKED_CLICK_SEMANTICS_TIMEOUT_SECONDS",
    "secret_request_classification": "SECRET_REQUEST_CLASSIFIER_TIMEOUT_SECONDS",
    "secret_request_classification_deliberate": "SECRET_REQUEST_DELIBERATE_CLASSIFIER_TIMEOUT_SECONDS",
    "credential_target_classification": "CREDENTIAL_TARGET_CLASSIFIER_TIMEOUT_SECONDS",
    "claim_support_judge": "CLAIM_SUPPORT_JUDGE_TIMEOUT_SECONDS",
    "candidate_evidence_retrieval": "CANDIDATE_EVIDENCE_TIMEOUT_SECONDS",
    "application_proposal_repair": "APPLICATION_PROPOSAL_REPAIR_TIMEOUT_SECONDS",
    "session_context_compression": "SESSION_CONTEXT_COMPRESSION_TIMEOUT_SECONDS",
    "operator_chat_scope": "OPERATOR_CHAT_SCOPE_TIMEOUT_SECONDS",
}
_LITERAL_DEADLINES = {"pending_user_reply_interpretation": 25.0, "final_action_outcome_verification": 20.0}


# The brain's own decision, and a provider-health probe (it measures a provider, not a judgment).
_NOT_JUDGED = frozenset({"vision_agent_step", "model_provider_health"})


def directory() -> Path | None:
    raw = getattr(get_settings(), "external_judge_dir", None)
    # An empty LUMEN_EXTERNAL_JUDGE_DIR parses as Path("."): that is "unset", never "the working directory".
    if not raw or str(raw).strip() in ("", "."):
        return None
    path = Path(raw).expanduser()
    return (path if path.is_absolute() else PROJECT_ROOT / path).resolve()


def enabled() -> bool:
    return directory() is not None


def handles(purpose: str) -> bool:
    """Every purpose except the brain's own step decision goes to the judge seat when the flag is set."""
    return enabled() and purpose not in _NOT_JUDGED


def independence_violation() -> str | None:
    """Why the judge root is not independent of the brain root, or None."""
    root, brain = directory(), external_brain.directory()
    if root is None or brain is None:
        return None
    if root == brain or brain in root.parents or root in brain.parents:
        # Path-free on purpose: this text reaches gate reasons and the ledger, which the brain can see.
        return "judge mailbox is not independent of the brain mailbox"
    return None


def mailbox() -> Path | None:
    root = directory()
    run_id = model_call_ledger.current_run()
    return root if root is None or run_id is None else root / f"run-{run_id}"


def deadline_seconds(purpose: str, timeout_override: int | None = None) -> float:
    """The purpose's own deadline, floored by LUMEN_EXTERNAL_JUDGE_WAIT_SECONDS when set and capped
    by the calling judge's remaining budget (judge_limiter.call_deadline)."""
    base: float | None = None
    if timeout_override:
        try:
            base = float(timeout_override)
        except (TypeError, ValueError):
            base = None
    if base is None and purpose in _DEADLINE_CONSTANTS:
        from app import lumen_model

        base = float(getattr(lumen_model, _DEADLINE_CONSTANTS[purpose], DEFAULT_DEADLINE_SECONDS))
    if base is None:
        base = _LITERAL_DEADLINES.get(purpose, DEFAULT_DEADLINE_SECONDS)
    floor = getattr(get_settings(), "external_judge_wait_seconds", None)
    if floor and floor > 0:
        base = max(base, float(floor))
    outer = judge_limiter.call_deadline.get()
    if outer is not None:
        base = min(base, max(0.0, outer - time.monotonic()))
    # The seat's own pace sets the floor: it applies after the caller's budget cap, and the caller-side wall
    # timeouts stretch to match (stretch()).
    return max(base, min_deadline_seconds())


def min_deadline_seconds() -> float:
    """The least the mailbox waits for any reply (LUMEN_EXTERNAL_JUDGE_MIN_DEADLINE_SECONDS, default 90)."""
    try:
        return max(0.0, float(getattr(get_settings(), "external_judge_min_deadline_seconds", 90.0)))
    except (TypeError, ValueError):
        return 90.0


STRETCH_MARGIN_SECONDS = 15.0


def stretch(seconds: float) -> float:
    """A caller-side wall timeout around a judge call: unchanged in production; with the mailbox on, never
    shorter than the mailbox floor plus a margin, so the mailbox reports its own timeout first."""
    if not enabled():
        return seconds
    return max(float(seconds), min_deadline_seconds() + STRETCH_MARGIN_SECONDS)


# The latest outcome per (run, purpose): lets the run loop tell "the judge did not answer in time" from a
# provider outage when it words a refusal. Bounded by the number of purposes per run.
_OUTCOMES: dict[tuple[int, str], str] = {}


def _note_outcome(purpose: str, outcome: str) -> None:
    run_id = model_call_ledger.current_run()
    if run_id is not None:
        _OUTCOMES[(int(run_id), purpose)] = outcome


def timed_out(run_id: int | None, purpose_prefix: str) -> bool:
    """True when the latest external-judge call of a purpose starting with the prefix timed out."""
    if run_id is None:
        return False
    return any(
        outcome == "timeout"
        for (rid, purpose), outcome in list(_OUTCOMES.items())
        if rid == int(run_id) and purpose.startswith(purpose_prefix)
    )


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _next_step(root: Path) -> int:
    numbers = [int(p.name[5:9]) for p in root.glob("step-*.request.json") if p.name[5:9].isdigit()]
    return max(numbers, default=0) + 1


_JSON_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def schema_error(value: Any, schema: Any, path: str = "$") -> str | None:
    """A small structural check (type, required, enum, properties, items); None when the value fits."""
    if not isinstance(schema, dict):
        return None
    if isinstance(schema.get("schema"), dict) and "type" not in schema:  # {"name": ..., "schema": {...}} wrapper
        schema = schema["schema"]
    expected = schema.get("type")
    kinds = expected if isinstance(expected, list) else [expected] if expected else []

    def fits(kind: str) -> bool:
        if kind == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if kind == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        python = _JSON_TYPES.get(kind)
        return python is None or isinstance(value, python)

    if kinds and not any(fits(str(kind)) for kind in kinds):
        return f"{path}: expected {'/'.join(map(str, kinds))}"
    if isinstance(schema.get("enum"), list) and value not in schema["enum"]:
        return f"{path}: not one of the allowed values"
    if isinstance(value, dict):
        for key in schema.get("required") or []:
            if key not in value:
                return f"{path}: missing required {key!r}"
        for key, sub in (schema.get("properties") or {}).items():
            if key in value:
                problem = schema_error(value[key], sub, f"{path}.{key}")
                if problem:
                    return problem
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            problem = schema_error(item, schema["items"], f"{path}[{index}]")
            if problem:
                return problem
    return None


def _failure(reason: str, error: str, metadata: dict[str, Any]) -> dict[str, Any]:
    return {"ok": False, "blocking_reason": reason, "error": error, "data": {}, "metadata": metadata}


def call(
    *,
    purpose: str,
    system: str,
    prompt: dict[str, Any],
    schema: dict[str, Any] | None,
    image: dict[str, Any] | list[dict[str, Any]] | None,
    timeout_seconds_override: int | None = None,
) -> dict[str, Any]:
    from app.model_provider import _json_from_text_with_status, _redact_prompt_for_transport

    metadata: dict[str, Any] = {
        "provider": "external_judge",
        "provider_display_name": "External judge",
        "model": "external_judge",
        "purpose": purpose,
        "token_usage": {},
    }
    violation = independence_violation()
    if violation:
        return _failure("external_judge_misconfigured", violation, metadata)
    root = mailbox()
    assert root is not None, "external judge called while disabled"
    root.mkdir(parents=True, exist_ok=True)
    images = [item for item in (image if isinstance(image, list) else [image]) if isinstance(item, dict)]
    deadline_s = deadline_seconds(purpose, timeout_seconds_override)
    with _LOCK:
        step = _next_step(root)
        stem = f"step-{step:04d}"
        names: list[str] = []
        for index, item in enumerate(images):
            suffix = ".png" if str(item.get("media_type") or "").lower().endswith("png") else ".jpg"
            name = f"{stem}-{index}{suffix}"
            _write_atomic(root / name, base64.b64decode(str(item.get("data") or "")))
            names.append(name)
        request = {
            "purpose": purpose,
            "step": step,
            "reply_file": f"{stem}.response.json",
            "deadline_seconds": round(deadline_s, 1),
            "system": system,
            "prompt": _redact_prompt_for_transport(prompt, screenshot_attached=bool(names)),
            "schema": schema,
            "images": names,
        }
        _write_atomic(root / f"{stem}.request.json", json.dumps(request, indent=2, default=str).encode("utf-8"))
    metadata.update(image_requested=bool(names), image_included=bool(names), external_judge_step=step)
    response_path = root / f"{stem}.response.json"
    started = time.perf_counter()
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        if response_path.exists():
            try:
                raw_text = response_path.read_text(encoding="utf-8")
            except OSError:
                time.sleep(_POLL_SECONDS)
                continue
            data, parse_status = _json_from_text_with_status(raw_text)
            metadata.update(
                latency_ms=int((time.perf_counter() - started) * 1000),
                parse_status=parse_status,
                raw_output_chars=len(raw_text),
                raw_output_empty=not raw_text.strip(),
            )
            if not isinstance(data, dict) or not data:
                _note_outcome(purpose, "invalid")
                return _failure("external_judge_invalid_reply", f"{response_path.name} is not a JSON object ({parse_status}).", metadata)
            problem = schema_error(data, schema) if schema else None
            if problem:
                metadata["schema_error"] = problem
                _note_outcome(purpose, "invalid")
                return _failure("external_judge_schema_mismatch", f"{response_path.name} violates the schema: {problem}", metadata)
            _note_outcome(purpose, "answered")
            return {"ok": True, "data": data, "metadata": metadata}
        time.sleep(_POLL_SECONDS)
    metadata.update(latency_ms=int((time.perf_counter() - started) * 1000), error="external_judge_timeout")
    _note_outcome(purpose, "timeout")
    return _failure(
        "external_judge_timeout",
        f"The judge did not answer in time ({deadline_s:.0f}s, no reply at {response_path.name}); retrying is fine.",
        metadata,
    )
