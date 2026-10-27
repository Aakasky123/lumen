"""Per-run monotonic floor under the click-consequence and payment-floor judges.

A model verdict on one exact control can flip between calls (the same button rated irreversible
three times, then ordinary once). A safety gate's direction must not depend on that, so the run
keeps, per stable control identity, the strictest verdict any judge call returned for it, and a
later, less strict verdict for the same control is replaced by the stricter earlier one. The floor
never relaxes within a run. Keys carry the page, selector, tag/role/type, accessible name and row
context, so the same label on a different row or page is a different control. Failed (fail-closed)
judge calls are never recorded: a transient outage must not pin a control as uncertain.
"""

from __future__ import annotations

import threading
from typing import Any

from app.control_ids import _accessible_name, _page_scope

_RANK = {
    "intermediate": 0,
    "ordinary_reversible": 0,
    "uncertain": 1,
    "irreversible_final": 2,
    "consequential_irreversible": 2,
}
_lock = threading.Lock()
_click_floor: dict[int, dict[tuple[str, ...], dict[str, Any]]] = {}
_payment_floor: dict[int, dict[tuple[str, ...], dict[str, Any]]] = {}


def _norm(value: Any, limit: int = 200) -> str:
    return " ".join(str(value or "").split()).casefold()[:limit]


def control_key(*, url: Any, selector: Any, control: dict[str, Any] | None) -> tuple[str, ...] | None:
    """Stable identity of the control a click lands on; None when nothing identifies it."""
    control = control if isinstance(control, dict) else {}
    selector_text = str(selector or control.get("selector") or "").strip()
    name = _accessible_name(control)
    row = _norm(control.get("row_text") or control.get("context_text"))
    if not selector_text and not name:
        return None
    return (
        _page_scope(url),
        selector_text,
        str(control.get("tag") or ""),
        str(control.get("role") or ""),
        str(control.get("type") or ""),
        name,
        row,
    )


def payment_key(*, url: Any, selector: Any, label: Any) -> tuple[str, ...] | None:
    name = _norm(label)
    if not name and not selector:
        return None
    return (_page_scope(url), str(selector or "").strip(), name)


def _rank(classification: Any) -> int:
    return _RANK.get(str(classification or "").strip().lower(), 1)


def enforce_click(
    run_id: int | None, key: tuple[str, ...] | None, verdict: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The verdict to act on, plus an override note when an earlier stricter verdict replaced it."""
    if run_id is None or key is None or not isinstance(verdict, dict) or verdict.get("fail_closed"):
        return verdict, None
    new_rank = _rank(verdict.get("classification"))
    with _lock:
        floor = _click_floor.setdefault(int(run_id), {})
        prior = floor.get(key)
        if prior is None or new_rank > prior["rank"]:
            floor[key] = {"rank": new_rank, "verdict": dict(verdict)}
            return verdict, None
        if new_rank == prior["rank"]:
            return verdict, None
        stricter = dict(prior["verdict"])
    note = {
        "from": str(verdict.get("classification") or ""),
        "to": str(stricter.get("classification") or ""),
        "later_confidence": verdict.get("confidence"),
        "later_evidence": str(verdict.get("evidence") or "")[:200],
    }
    metadata = dict(stricter.get("metadata") if isinstance(stricter.get("metadata"), dict) else {})
    metadata["monotonic_override"] = note
    stricter["metadata"] = metadata
    stricter["evidence"] = ("Earlier stricter verdict for this control kept. " + str(stricter.get("evidence") or ""))[:400]
    return stricter, note


def enforce_payment(
    run_id: int | None, key: tuple[str, ...] | None, verdict: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """Payment=True sticks per control only once earned; returns (verdict, overridden).

    Every click is judged on its own and payment=True on that click always stands. A later
    payment=False for the same control is replaced by the earlier payment=True only when that control
    drew a deterministic payment signal, or two payment=True judge verdicts (a single judge flip must
    not turn into a guaranteed stop for every later click on the control). Fail-closed verdicts count
    as neither.
    """
    if run_id is None or key is None or not isinstance(verdict, dict):
        return verdict, False
    with _lock:
        floor = _payment_floor.setdefault(int(run_id), {})
        entry = floor.get(key)
        if verdict.get("payment"):
            if verdict.get("fail_closed") and not verdict.get("deterministic_hit") and not verdict.get("deterministic_reason"):
                return verdict, False
            entry = entry or {"trues": 0, "verdict": None, "deterministic": False}
            entry["trues"] += 1
            entry["verdict"] = dict(verdict)
            if verdict.get("deterministic_hit") or verdict.get("deterministic_reason"):
                entry["deterministic"] = True
            floor[key] = entry
            return verdict, False
        if entry is None or entry["verdict"] is None or not (entry["deterministic"] or entry["trues"] >= 2):
            return verdict, False
        sticky = dict(entry["verdict"])
    sticky["payment_unknown"] = False
    sticky["fail_closed"] = False
    sticky["monotonic_override"] = True
    return sticky, True


def release(run_id: int) -> None:
    with _lock:
        _click_floor.pop(int(run_id), None)
        _payment_floor.pop(int(run_id), None)
