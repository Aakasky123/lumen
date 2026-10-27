from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models
from app.config import get_settings


VISION_LOOP_SUPPORT_EVENT_TYPES = frozenset(
    {
        "ACTION_ATTEMPTED",
        "ACTION_EXECUTED",
        "ACTION_OUTCOME_VERIFIED",
        "ACTION_RECEIPT",
        "APPLICATION_EXECUTION_FAILED",
        "LUMEN_PLANNER_STEP",
        "CLICK_CONSEQUENCE_PREFLIGHT_EVALUATED",
        "CONTRADICTION_DETECTED",
        "ESCALATION_STEP",
        "FINAL_ACTION_MODE",
        "FINAL_ACTION_OUTCOME_VERDICT",
        "IN_BROWSER_DRIVE_STARTED",
        "IN_BROWSER_RUN_EXCEPTION",
        "OPERATION_RECEIPT",
        "OPTIONAL_UNGROUNDED_SKIPPED",
        "RUN_HEARTBEAT",
        "RUN_TIMING_SUMMARY",
        "STEP_TIMING",
        "SYSTEM_BLOCK",
        "TRANSIENT_UI_SETTLED",
        "VISION_CONTEXT_POLICY_APPLIED",
    }
)


def run_failure_evidence(db: Session, run: models.OperatorRun) -> dict[str, Any]:
    """The run evidence an eval-flywheel failure candidate records."""
    vision_loop_events = _vision_loop_events(db, run.id)
    return {
        "run_state": run.current_state,
        "status_message": run.status_message,
        "latest_observation_path": _latest_intact_observation_path(run.id, vision_loop_events),
        "vision_action_receipts": [
            event for event in vision_loop_events if event.get("event_type") == "ACTION_RECEIPT"
        ],
        "vision_loop_event_counts": _event_type_counts(vision_loop_events),
        "vision_timings": _vision_timings(vision_loop_events),
    }


def _vision_loop_events(db: Session, run_id: int) -> list[dict[str, Any]]:
    events = list(
        db.scalars(
            select(models.CognitiveEvent)
            .where(models.CognitiveEvent.run_id == run_id)
            .order_by(models.CognitiveEvent.id.asc())
        ).all()
    )
    return [
        _event_to_dict(event)
        for event in events
        if (
            str(event.event_type or "").startswith("VISION_")
            or event.event_type in VISION_LOOP_SUPPORT_EVENT_TYPES
            or event.actor == "lumen_in_browser"
        )
    ]


def _vision_timings(events: list[dict[str, Any]]) -> dict[str, Any]:
    step_timings = [event for event in events if event.get("event_type") == "STEP_TIMING"]
    run_summaries = [event for event in events if event.get("event_type") == "RUN_TIMING_SUMMARY"]
    latest_summary = run_summaries[-1].get("payload") if run_summaries else {}
    return {
        "step_timings": step_timings,
        "run_summaries": run_summaries,
        "latest_run_summary": latest_summary if isinstance(latest_summary, dict) else {},
    }


def _latest_intact_observation_path(run_id: int, events: list[dict[str, Any]]) -> str | None:
    """The newest retained observation file that still matches the hash its event recorded."""
    trajectory_root = (get_settings().artifact_root / "runs" / str(run_id) / "vision-trajectory").resolve()
    for event in reversed(events):
        if event.get("event_type") != "VISION_OBSERVATION_RETAINED":
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        path = Path(str(payload.get("observation_path") or "")).resolve()
        if not path.is_relative_to(trajectory_root) or not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        expected_sha256 = str(payload.get("observation_file_sha256") or "").strip().lower()
        if expected_sha256 and hashlib.sha256(data).hexdigest() != expected_sha256:
            continue
        return str(path)
    return None


def _event_type_counts(events: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        event_type = str(event.get("event_type") or "")
        if event_type:
            counts[event_type] = counts.get(event_type, 0) + 1
    return counts


def _event_to_dict(event: models.CognitiveEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "event_type": event.event_type,
        "actor": event.actor,
        "confidence": event.confidence,
        "risk_level": event.risk_level,
        "timestamp": event.timestamp.isoformat(),
        "payload": event.event_payload,
    }
