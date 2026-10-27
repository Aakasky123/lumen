"""Keep/Change flywheel — turn every confirm-ask correction into DATA, never a rule.

When the user Keeps or Changes the agent's proposed answer, we record a structured correction. That
record is used three ways (the review's mandate): eval data (harvested into answer-quality corpus
cases), retrieval/memory (the corrected value is already saved to the answer bank by the caller, so a
repeat field auto-resolves), and analytics. It is explicitly NOT compiled into branching rules /
question→answer mappings — that would rebuild the rules engine the governing rule forbids.

A correction is captured ONLY for a confirm-ask (a field that surfaced a proposed answer); a plain
blank ask produces no record.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select

from app import models
from app.event_store import emit_event
from app.models import utcnow

ANSWER_CORRECTION_EVENT = "ANSWER_CORRECTION"


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").replace("www.", "")
    except Exception:  # noqa: BLE001
        return ""


def build_correction_record(*, task_payload: dict[str, Any], target_url: str = "", run_id: int = 0, ts: str | None = None) -> dict[str, Any] | None:
    """Build the structured correction from an answered field-approval task, or None if the task was
    not a confirm-ask (no surfaced proposal) or wasn't a keep/change."""
    proposed = str(task_payload.get("proposed_answer") or task_payload.get("proposed_option") or "").strip()
    user_choice = str(task_payload.get("user_choice") or "").strip()
    if not proposed or user_choice not in {"keep", "change"}:
        return None
    options = [str(option).strip() for option in (task_payload.get("options") or []) if str(option).strip()]
    user_value = str((task_payload.get("interpreted_answer") or {}).get("resolved_value") or task_payload.get("raw_user_answer") or "").strip()
    return {
        "field_key": str(task_payload.get("field_key") or ""),
        "field_label": str(task_payload.get("label") or ""),
        "field_type": "choice" if (task_payload.get("is_choice") or options) else "free_text",
        "options": options,
        "agent_proposed": proposed,
        "user_action": user_choice,
        "user_value": user_value,
        "host": _host(target_url),
        "run_id": int(run_id or 0),
        "ts": ts or utcnow().isoformat(),
    }


def record_correction(db: Any, *, run: models.OperatorRun, task_payload: dict[str, Any], ts: str | None = None) -> dict[str, Any] | None:
    """Persist a confirm-ask correction as an ANSWER_CORRECTION event (payload auto-redacted). Returns
    the record, or None when the task wasn't a confirm-ask. Best-effort: never raise into the answer
    path."""
    try:
        record = build_correction_record(task_payload=task_payload, target_url=getattr(run, "target_url", "") or "", run_id=int(getattr(run, "id", 0) or 0), ts=ts)
        if record is None:
            return None
        emit_event(db, event_type=ANSWER_CORRECTION_EVENT, payload=record, run_id=record["run_id"] or None, actor="answer_flywheel", confidence=1.0)
        return record
    except Exception:  # noqa: BLE001 - the flywheel must never break answering
        return None


def load_corrections(db: Any, *, user_id: int | None = None) -> list[dict[str, Any]]:
    """All recorded corrections (optionally scoped to one user via their runs), oldest first."""
    events = db.scalars(
        select(models.CognitiveEvent).where(models.CognitiveEvent.event_type == ANSWER_CORRECTION_EVENT).order_by(models.CognitiveEvent.id.asc())
    ).all()
    records = [event.event_payload for event in events if isinstance(event.event_payload, dict)]
    if user_id is not None:
        run_ids = {row for (row,) in db.execute(select(models.OperatorRun.id).where(models.OperatorRun.user_id == user_id))}
        records = [record for record in records if record.get("run_id") in run_ids]
    return records


def correction_to_case(record: dict[str, Any]) -> dict[str, Any]:
    """Convert a correction into an answer-quality corpus case (DATA, not a rule). A Change becomes a
    regression guard: the rejected value must never auto-reappear for that field. A Keep records the
    proposal as acceptable."""
    proposed = str(record.get("agent_proposed") or "").strip()
    value = str(record.get("user_value") or "").strip()
    changed = record.get("user_action") == "change" and bool(value) and value.lower() != proposed.lower()
    return {
        "id": f"corr_{record.get('field_key') or 'field'}",
        "label": str(record.get("field_label") or ""),
        "options": list(record.get("options") or []),
        # Any non-fabricating decision is fine; the regression we enforce is "don't repeat the mistake".
        "expected": ["auto", "confirm", "ask"] if changed else ["auto", "confirm"],
        "unacceptable": [proposed] if changed else [],
        "acceptable": [value] if value else [proposed],
        "note": (f"user changed '{proposed}' -> '{value}'" if changed else f"user kept '{proposed}'"),
    }
