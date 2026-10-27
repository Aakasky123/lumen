"""Run-scoped compressed session context: the body's bookkeeping, the brain's compression.

The vision loop keeps only bounded recent windows (page_memory[-8:], recent_actions[-10:]);
anything older silently falls out of the prompt, so long multi-page runs lose their own
history. This module gives the loop a durable, compressed narrative of the whole run:

  body (deterministic): appends one compact record per executed action, decides when to
      compress, stores and injects the summary — it never interprets page meaning.
  brain (model): folds pending records into the running summary — the only semantic step,
      served by the designated fast judge provider when one is configured.

The summary is fail-open CONTEXT, never a guarantee: on any model failure the previous
summary stays and pending records keep accumulating up to a hard cap. Step records carry
labels, outcomes, and hosts only — never typed values, credentials, or OTP codes.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

SESSION_CONTEXT_VERSION = "vision_session_context_v1"
SUMMARY_MAX_CHARS = 1_500
RECORD_FIELD_MAX_CHARS = 120
PENDING_COMPRESS_THRESHOLD = 5
PENDING_HARD_CAP = 40


def new_state() -> dict[str, Any]:
    return {
        "version": SESSION_CONTEXT_VERSION,
        "summary": "",
        "folded_records": 0,
        "dropped_records": 0,
        "pending": [],
    }


def coerce_state(raw: Any) -> dict[str, Any]:
    """Rehydrate a persisted state defensively; anything malformed becomes a fresh state."""
    state = new_state()
    if not isinstance(raw, dict):
        return state
    state["summary"] = _clean_text(raw.get("summary"), SUMMARY_MAX_CHARS)
    for counter in ("folded_records", "dropped_records"):
        try:
            state[counter] = max(0, int(raw.get(counter) or 0))
        except (TypeError, ValueError):
            state[counter] = 0
    pending = raw.get("pending")
    if isinstance(pending, list):
        state["pending"] = [dict(record) for record in pending if isinstance(record, dict)][
            -PENDING_HARD_CAP:
        ]
    # The record id must stay monotonic across a resume: restarting it at 0 made a compression
    # whose snapshot predates the resume drop every record appended after it.
    seqs = [record["seq"] for record in state["pending"] if isinstance(record.get("seq"), int)]
    try:
        persisted_next = max(0, int(raw.get("next_seq") or 0))
    except (TypeError, ValueError):
        persisted_next = 0
    next_seq = max([persisted_next, *(seq + 1 for seq in seqs)])
    if next_seq:
        state["next_seq"] = next_seq
    return state


def host_of(url: Any) -> str:
    try:
        host = urlparse(str(url or "")).hostname or ""
    except ValueError:
        host = ""
    return host.removeprefix("www.")


def _clean_text(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[: max(0, int(limit))]


def append_step_record(
    state: dict[str, Any],
    *,
    step: int,
    kind: str,
    label: str = "",
    ok: bool | None = None,
    verified: bool | None = None,
    result_hint: str = "",
    url_host: str = "",
    note: str = "",
) -> None:
    """Append one compact deterministic record of an executed action (no raw values)."""
    record: dict[str, Any] = {"step": max(0, int(step or 0)), "kind": _clean_text(kind, 40)}
    if label:
        record["label"] = _clean_text(label, RECORD_FIELD_MAX_CHARS)
    if ok is not None:
        record["ok"] = bool(ok)
    if verified is not None:
        record["verified"] = bool(verified)
    if result_hint:
        record["result_hint"] = _clean_text(result_hint, RECORD_FIELD_MAX_CHARS)
    if url_host:
        record["host"] = _clean_text(url_host, 80)
    if note:
        record["note"] = _clean_text(note, RECORD_FIELD_MAX_CHARS)
    # A monotonic id, so a compression that ran while older records were evicted folds exactly
    # the records it summarized (see adopt_compression). Never shown to a model.
    record["seq"] = int(state.get("next_seq") or 0)
    state["next_seq"] = record["seq"] + 1
    pending = state.setdefault("pending", [])
    pending.append(record)
    overflow = len(pending) - PENDING_HARD_CAP
    if overflow > 0:
        del pending[:overflow]
        state["dropped_records"] = int(state.get("dropped_records") or 0) + overflow


def should_compress(state: dict[str, Any]) -> bool:
    return len(state.get("pending") or []) >= PENDING_COMPRESS_THRESHOLD


def snapshot_for_compression(state: dict[str, Any]) -> dict[str, Any]:
    """Freeze the current summary + pending records for one compression attempt."""
    pending = list(state.get("pending") or [])
    records = [{key: value for key, value in record.items() if key != "seq"} for record in pending]
    seqs = [int(record["seq"]) for record in pending if isinstance(record.get("seq"), int)]
    return {
        "summary": str(state.get("summary") or ""),
        "records": records,
        "record_count": len(records),
        "last_seq": max(seqs) if seqs else None,
    }


def adopt_compression(
    state: dict[str, Any],
    snapshot: dict[str, Any],
    new_summary: Any,
) -> bool:
    """Fold a completed compression back in. Records appended while the compression ran
    stay pending; returns False (state unchanged) for empty/invalid summaries."""
    summary = _clean_text(new_summary, SUMMARY_MAX_CHARS)
    if not summary:
        return False
    folded = max(0, int(snapshot.get("record_count") or 0))
    state["summary"] = summary
    state["folded_records"] = int(state.get("folded_records") or 0) + folded
    pending = state.setdefault("pending", [])
    last_seq = snapshot.get("last_seq")
    # Drop exactly the records the summary saw. Deleting the first record_count records dropped
    # new ones instead whenever the hard cap evicted old records while the compression ran.
    # Records without a seq predate it and were all pending at snapshot time.
    pending[:] = [
        record
        for record in pending
        if isinstance(record.get("seq"), int) and (last_seq is None or record["seq"] > last_seq)
    ]
    return True


def prompt_view(state: dict[str, Any], current_step: int | None = None) -> dict[str, Any]:
    """The bounded view injected into the per-step prompt. Empty dict until the first
    compression so early steps carry no dead weight. Its age is the count of actions recorded after it
    (stored with the state, so it survives a paused or resumed loop); ``current_step`` is unused."""
    summary = str(state.get("summary") or "")
    if not summary:
        return {}
    since = len(state.get("pending") or [])
    view = {
        "summary": summary,
        "basis": (
            f"Older-actions recollection; {since} action(s) happened after it. Built from action labels only, "
            "never values or page state. For the current state of the page, fields or results use "
            "session_ledger, verified_receipts, last_reply and the screenshot; where they differ, they win."
        ),
        "folded_action_records": int(state.get("folded_records") or 0),
        "unfolded_recent_actions": len(state.get("pending") or []),
    }
    dropped = int(state.get("dropped_records") or 0)
    if dropped:
        view["dropped_unfolded_records"] = dropped
    return view
