"""External brain: a dev seam that seats an outside reasoner in the browser brain's chair.

When ``LUMEN_EXTERNAL_BRAIN_DIR`` is set, every ``vision_agent_step`` decision (or every purpose
named in ``LUMEN_EXTERNAL_BRAIN_PURPOSES``; ``*`` = all) is written to that directory exactly as
the model would receive it (system text, transport-redacted prompt, schema, screenshots) and the
caller waits for a reply file instead of calling a provider:

    step-0007.request.json      {purpose, system, prompt, schema, images: [file names]}
    step-0007-0.jpg             the screenshot (then any zoom crop as -1, -2 ...)
    step-0007.response.json     written by the outside reasoner: the raw action object

Inside a run (the run loop binds its id in ``model_call_ledger``, which worker threads inherit) the
mailbox is ``<dir>/run-<run_id>/`` with its own step numbering, so one brain per run can serve
concurrent runs; a call with no run context uses ``<dir>`` itself. ``LUMEN_EXTERNAL_BRAIN_PER_RUN=0``
turns the split off.

Escalation seat: when the run loop has handed a stuck part of the task to the escalation brain
(``brain_escalation``), the step decision carries ``seat="escalation"``. With ``LUMEN_EXTERNAL_ESCALATION_DIR``
set, those calls go to that mailbox instead (same files, same per-run layout, same wait and fail-closed handling),
so a different outside reasoner answers them; unset, they go to the brain mailbox as before. Only
``vision_agent_step`` ever reaches the escalation mailbox. Its root must not equal, contain or sit inside the brain or
judge root: a misconfigured root makes every escalation call fail closed (``external_escalation_misconfigured``).

By default only the step decision is rerouted; judges, classifiers, gates and verifiers keep their
own providers, so the reasoner sees the harness from the inside: the same evidence, the same action
contract, the same receipts and refusals a model gets. The reply is parsed by the same JSON reader
the provider transports use, so a malformed reply fails the way a malformed completion would.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from app import model_call_ledger
from app.config import PROJECT_ROOT, get_settings

DEFAULT_WAIT_SECONDS = 900.0
_POLL_SECONDS = 0.05
_LOCK = threading.Lock()


# Which brain a step decision is made for: the primary brain, or the escalation brain holding a stuck part.
BRAIN = "brain"
ESCALATION = "escalation"


def _resolve(raw: Any) -> Path | None:
    # An empty variable parses as Path("."): that is "unset", never "the working directory".
    if not raw or str(raw).strip() in ("", "."):
        return None
    path = Path(raw).expanduser()
    return (path if path.is_absolute() else PROJECT_ROOT / path).resolve()


def directory() -> Path | None:
    raw = get_settings().external_brain_dir
    if not raw:
        return None
    path = Path(raw).expanduser()
    return (path if path.is_absolute() else PROJECT_ROOT / path).resolve()


def escalation_directory() -> Path | None:
    """The escalation mailbox root (LUMEN_EXTERNAL_ESCALATION_DIR), or None when unset."""
    return _resolve(getattr(get_settings(), "external_escalation_dir", None))


def escalation_enabled() -> bool:
    return escalation_directory() is not None


def _routes_to_escalation(purpose: str, seat: str) -> bool:
    return seat == ESCALATION and purpose == "vision_agent_step" and escalation_enabled()


def escalation_independence_violation() -> str | None:
    """Why the escalation root is not independent of the brain or judge root, or None."""
    root = escalation_directory()
    if root is None:
        return None
    others = [directory(), _resolve(getattr(get_settings(), "external_judge_dir", None))]
    for other in others:
        if other is not None and (root == other or other in root.parents or root in other.parents):
            # Path-free on purpose: this text reaches the ledger and the brain's failure feedback.
            return "escalation mailbox is not independent of the brain or judge mailbox"
    return None


def mailbox(seat: str = BRAIN, purpose: str = "vision_agent_step") -> Path | None:
    """The directory this call writes to: the run's own subdirectory when called inside a run."""
    root = escalation_directory() if _routes_to_escalation(purpose, seat) else directory()
    run_id = model_call_ledger.current_run()
    if root is None or run_id is None or not getattr(get_settings(), "external_brain_per_run", True):
        return root
    return root / f"run-{run_id}"


def enabled() -> bool:
    return directory() is not None


# Judges that gate a safety floor never take their verdict from a reply file, even under ``*``: whoever
# can write the mailbox would decide secrets, credentials, consequences, payments and readiness.
SAFETY_PURPOSES = frozenset(
    {
        "secret_request_classification",
        "secret_request_classification_deliberate",
        "credential_target_classification",
        "blocked_click_semantics",
        "payment_floor_judge",
        "target_binding_judge",
        "control_reference_conflict",
        "grounding_judge",
        "claim_support_judge",
        "final_action_outcome_verification",
        "vision_final_action_readiness_audit",
        # It decides what the completion reply may claim.
        "task_completion_verification",
        # They decide what the user said and what the run may remember about them.
        "pending_user_reply_interpretation",
        "user_memory_reconciliation",
        "artifact_user_fact_extraction",
        "answer_user_fact_extraction",
        # A fail-closed check of the model's own page citations, and whether a question may be skipped.
        "page_quote_visual_check",
        # The second reader of a screen-text proof: never the brain whose reading it checks.
        "screen_text_transcription",
        "answer_skip_intent",
    }
)


def handles(purpose: str, seat: str = BRAIN) -> bool:
    """Which model seats the outside reasoner fills: the browser step by default, a comma list of
    purposes, or ``*`` for every structured model call except the safety judges. An escalation-seat step
    is handled whenever the escalation mailbox is set, even without a brain mailbox."""
    if purpose in SAFETY_PURPOSES:
        return False
    if _routes_to_escalation(purpose, seat):
        return True
    if not enabled():
        return False
    raw = get_settings().external_brain_purposes or "vision_agent_step"
    purposes = {item.strip() for item in raw.split(",") if item.strip()}
    return "*" in purposes or purpose in purposes


def wait_seconds() -> float:
    value = get_settings().external_brain_wait_seconds
    return float(value) if value and value > 0 else DEFAULT_WAIT_SECONDS


def _next_step(root: Path) -> int:
    numbers = [
        int(path.name[5:9])
        for path in root.glob("step-*.request.json")
        if path.name[5:9].isdigit()
    ]
    return max(numbers, default=0) + 1


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _image_suffix(item: dict[str, Any]) -> str:
    media_type = str(item.get("media_type") or "").lower()
    return ".png" if media_type.endswith("png") else ".jpg"


def call(
    *,
    purpose: str,
    system: str,
    prompt: dict[str, Any],
    schema: dict[str, Any] | None,
    image: dict[str, Any] | list[dict[str, Any]] | None,
    seat: str = BRAIN,
) -> dict[str, Any]:
    from app.model_provider import _json_from_text_with_status, _redact_prompt_for_transport

    escalation = _routes_to_escalation(purpose, seat)
    if escalation:
        violation = escalation_independence_violation()
        if violation:
            return {
                "ok": False,
                "blocking_reason": "external_escalation_misconfigured",
                "error": violation,
                "data": {},
                "metadata": {
                    "provider": "external_escalation",
                    "provider_display_name": "External escalation",
                    "model": "external",
                    "purpose": purpose,
                    "external_seat": ESCALATION,
                    "error": "external_escalation_misconfigured",
                },
            }
    root = mailbox(seat, purpose)
    assert root is not None, "external brain called while disabled"
    root.mkdir(parents=True, exist_ok=True)
    images = [item for item in (image if isinstance(image, list) else [image]) if isinstance(item, dict)]
    with _LOCK:
        step = _next_step(root)
        stem = f"step-{step:04d}"
        names: list[str] = []
        for index, item in enumerate(images):
            name = f"{stem}-{index}{_image_suffix(item)}"
            _write_atomic(root / name, base64.b64decode(str(item.get("data") or "")))
            names.append(name)
        request = {
            "purpose": purpose,
            "step": step,
            # The mailbox number counts every request, re-asks included, so it differs from the run's own step.
            "reply_file": f"{stem}.response.json",
            "system": system,
            "prompt": _redact_prompt_for_transport(prompt, screenshot_attached=bool(names)),
            "schema": schema,
            "images": names,
        }
        _write_atomic(root / f"{stem}.request.json", json.dumps(request, indent=2, default=str).encode("utf-8"))
    response_path = root / f"{stem}.response.json"
    started = time.perf_counter()
    deadline = time.monotonic() + wait_seconds()
    metadata: dict[str, Any] = {
        "provider": "external_escalation" if escalation else "external_brain",
        "provider_display_name": "External escalation" if escalation else "External brain",
        "model": "external",
        "purpose": purpose,
        "image_requested": bool(names),
        "image_included": bool(names),
        "external_brain_step": step,
        "external_seat": ESCALATION if escalation else BRAIN,
    }
    while time.monotonic() < deadline:
        if response_path.exists():
            try:
                raw_text = response_path.read_text(encoding="utf-8")
            except OSError:
                # The reasoner may still hold the file (Windows PermissionError while it writes); retry.
                time.sleep(_POLL_SECONDS)
                continue
            data, parse_status = _json_from_text_with_status(raw_text)
            metadata.update(
                latency_ms=int((time.perf_counter() - started) * 1000),
                parse_status=parse_status,
                raw_output_chars=len(raw_text),
                raw_output_empty=not raw_text.strip(),
                token_usage={},
            )
            return {"ok": True, "data": data if isinstance(data, dict) else {}, "metadata": metadata}
        time.sleep(_POLL_SECONDS)
    metadata.update(latency_ms=int((time.perf_counter() - started) * 1000), error="external_brain_timeout")
    return {
        "ok": False,
        "blocking_reason": "external_brain_timeout",
        "error": f"No reply at {response_path.name} within {wait_seconds():.0f}s.",
        "data": {},
        "metadata": metadata,
    }
