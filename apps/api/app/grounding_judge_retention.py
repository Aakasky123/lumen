"""Retain each grounding-judge model attempt's exact input and outcome, for offline replay.

One JSON record per attempt under <artifact_root>/runs/<run_id>/judge-trajectory/, with any
attached screenshot as a sibling image file. The prompt is retained as transport sends it
(model_provider._redact_prompt_for_transport: one-time codes scrubbed, page-rendered sections
kept verbatim only beside an attached screenshot). The target-binding judge is retained the same way (record "purpose": "target_binding_judge",
verdict {matches, reason}); grounding replay skips those. Retention is evidence only: it never raises
into the judge path. Replay: app.agent_eval.judge_replay.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TRAJECTORY_DIR = "judge-trajectory"
RECORD_FORMAT = "grounding_judge_attempt_v1"
_VERDICT_KEYS = ("matches", "grounded", "entry_index", "reason", "support_quote", "instrumental", "commits_or_destroys", "evidence_status")
_IMAGE_SUFFIXES = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png", "image/webp": "webp"}


def _write_atomic(path: Path, data: bytes) -> None:
    temporary = path.with_name(f"{path.name}.tmp-{secrets.token_hex(6)}")
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass


def _images(image: Any) -> list[dict[str, Any]]:
    if isinstance(image, dict):
        return [image]
    if isinstance(image, list):
        return [item for item in image if isinstance(item, dict)]
    return []


def retain_attempt(
    run_id: int | None,
    *,
    judge_call_id: str,
    attempt: int,
    system: str,
    prompt: dict[str, Any],
    schema: dict[str, Any],
    provider: Any,
    max_tokens: int,
    timeout_seconds: int,
    image: Any,
    image_sent: bool,
    entry_count: int,
    result: Any,
    routed_by_purpose: bool = False,
    purpose: str = "grounding_judge",
    root: Path | None = None,
) -> dict[str, Any] | None:
    """Write one attempt record; returns {"path": ...} or None (no run, or any failure)."""
    try:
        if not run_id:
            return None
        from app.model_provider import _redact_prompt_for_transport

        if root is None:
            from app.config import get_settings

            root = get_settings().artifact_root
        outcome = result if isinstance(result, dict) else {}
        metadata = outcome.get("metadata") if isinstance(outcome.get("metadata"), dict) else {}
        # Transport keeps page-rendered sections only when the screenshot really rode along.
        screenshot_attached = bool(image_sent) and metadata.get("image_included") is not False
        safe_prompt = _redact_prompt_for_transport(prompt, screenshot_attached=screenshot_attached)
        directory = Path(root) / "runs" / str(int(run_id)) / TRAJECTORY_DIR
        directory.mkdir(parents=True, exist_ok=True)
        created_at = datetime.now(timezone.utc)
        token = f"{created_at.strftime('%Y%m%dT%H%M%S%f')}-{max(1, int(attempt))}"
        image_refs: list[dict[str, Any]] = []
        if image_sent:
            for index, item in enumerate(_images(image)):
                raw = base64.b64decode(str(item.get("data") or ""), validate=True)
                if not raw:
                    continue
                media_type = str(item.get("media_type") or "image/jpeg")
                image_path = directory / f"{token}-image-{index}.{_IMAGE_SUFFIXES.get(media_type, 'jpg')}"
                _write_atomic(image_path, raw)
                image_refs.append(
                    {
                        "path": image_path.name,
                        "media_type": media_type,
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "bytes": len(raw),
                    }
                )
        data = outcome.get("data") if isinstance(outcome.get("data"), dict) else {}
        usage = metadata.get("token_usage") if isinstance(metadata.get("token_usage"), dict) else {}
        record = {
            "format": RECORD_FORMAT,
            "run_id": int(run_id),
            "judge_call_id": str(judge_call_id),
            "purpose": str(purpose),
            "attempt": int(attempt),
            "created_at": created_at.isoformat(),
            "request": {
                "system": system,
                "prompt": safe_prompt,
                "schema": schema,
                "max_tokens": int(max_tokens),
                "timeout_seconds": int(timeout_seconds),
                "image_sent": bool(image_sent),
                "images": image_refs,
                "entry_count": int(entry_count),
            },
            "provider": {
                "id": metadata.get("provider_id", getattr(provider, "id", None)),
                "model": metadata.get("model") or getattr(provider, "model_name", None),
                "provider_type": metadata.get("provider") or getattr(provider, "provider_type", None),
                "reasoning_effort": getattr(provider, "reasoning_effort", None) if provider is not None else None,
                "structured_output": getattr(provider, "structured_output", None) if provider is not None else None,
                "routed_by_purpose": bool(routed_by_purpose),
                "effective_max_tokens": metadata.get("max_tokens"),
            },
            "outcome": {
                "ok": bool(outcome.get("ok")),
                "error": str(outcome.get("error") or "")[:300],
                "verdict": {key: data.get(key) for key in _VERDICT_KEYS if key in data},
                "latency_ms": metadata.get("latency_ms"),
                "finish_reason": metadata.get("finish_reason"),
                "parse_status": metadata.get("parse_status"),
                "image_included": metadata.get("image_included"),
                "usage": usage,
            },
        }
        record_path = directory / f"{token}.json"
        _write_atomic(record_path, json.dumps(record, ensure_ascii=False, indent=2, default=str).encode("utf-8"))
        return {"path": str(record_path)}
    except Exception:  # noqa: BLE001 - retention is evidence only; the judge path must never see it fail
        return None


def load_records(
    root: Path,
    *,
    run_ids: list[int] | None = None,
    days: int | None = None,
) -> list[dict[str, Any]]:
    """Retained attempt records (oldest first), each with "_path" and "_dir" for its images."""
    runs_root = Path(root) / "runs"
    if not runs_root.is_dir():
        return []
    wanted = {int(item) for item in run_ids} if run_ids else None
    cutoff = None
    if days:
        cutoff = datetime.now(timezone.utc).timestamp() - float(days) * 86400.0
    records: list[dict[str, Any]] = []
    for run_dir in runs_root.iterdir():
        if not run_dir.name.isdigit() or (wanted is not None and int(run_dir.name) not in wanted):
            continue
        directory = run_dir / TRAJECTORY_DIR
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(record, dict) or record.get("format") != RECORD_FORMAT:
                continue
            if cutoff is not None:
                try:
                    created = datetime.fromisoformat(str(record.get("created_at"))).timestamp()
                except ValueError:
                    continue
                if created < cutoff:
                    continue
            record["_path"] = str(path)
            record["_dir"] = str(directory)
            records.append(record)
    records.sort(key=lambda item: str(item.get("created_at") or ""))
    return records


def load_images(record: dict[str, Any]) -> list[dict[str, Any]]:
    """The record's attached screenshots as transport image dicts (base64)."""
    directory = Path(str(record.get("_dir") or ""))
    images: list[dict[str, Any]] = []
    for ref in (record.get("request") or {}).get("images") or []:
        raw = (directory / str(ref.get("path") or "")).read_bytes()
        images.append({"media_type": ref.get("media_type") or "image/jpeg", "data": base64.b64encode(raw).decode("ascii")})
    return images
