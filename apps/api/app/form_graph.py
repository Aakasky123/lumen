"""Requiredness projection for question cards.

Records what a field's own evidence (its required flag, a visible marker, page text that
says it is optional) proves about whether it is required. It never decides what value a
field gets.
"""

from __future__ import annotations

import re
from typing import Any


REQUIREDNESS_STATES = frozenset(
    {
        "required_proven",
        "required_likely",
        "optional_proven",
        "optional_likely",
        "unknown_nonblocking",
        "unknown_blocking",
    }
)
BLOCKING_REQUIREDNESS_STATES = frozenset({"required_proven", "required_likely", "unknown_blocking"})

_VOLUNTARY_TEXT_RE = re.compile(
    r"\b(voluntary|voluntarily|optional|not required)\b"
    r"|\b(choose|choosing|decline|prefer|opt) (not )?to (answer|disclose|self[- ]?identif|provide|say)"
    r"|\byou (may|can) (choose|decline|skip)",
    re.IGNORECASE,
)


def _label(node: dict[str, Any]) -> str:
    return str(node.get("resolved_label") or node.get("label") or "")


def field_is_explicitly_optional(node: dict[str, Any]) -> bool:
    """Return True only when the page text itself says a field is optional."""
    label = _label(node)
    if "*" in label:
        return False
    return bool(_VOLUNTARY_TEXT_RE.search(f"{node.get('section_heading') or ''} {label}"))


def _policy_nonblocking_disclosure(node: dict[str, Any]) -> bool:
    options = " ".join(str(item or "") for item in (node.get("options") or [])).lower()
    return bool(re.search(r"\b(choose|prefer|decline)\s+not\s+to\s+(answer|disclose|say)|do\s+not\s+wish\s+to\s+answer|not\s+to\s+disclose\b", options))


def normalize_requiredness(value: Any, *, fallback_state: str = "unknown_blocking") -> dict[str, Any]:
    if isinstance(value, dict):
        state = str(value.get("state") or "").strip().lower()
        if state == "required":
            state = "required_proven"
        elif state == "optional":
            state = "optional_likely"
        elif state == "unknown":
            state = fallback_state
        if state not in REQUIREDNESS_STATES:
            state = fallback_state
        evidence = value.get("evidence") if isinstance(value.get("evidence"), list) else []
        return {
            "state": state,
            "confidence": float(value.get("confidence") if value.get("confidence") is not None else 0.3),
            "evidence": [str(item) for item in evidence if str(item).strip()],
        }
    state = str(value or "").strip().lower()
    if state == "required":
        state = "required_proven"
    elif state == "optional":
        state = "optional_likely"
    elif state == "unknown":
        state = fallback_state
    if state not in REQUIREDNESS_STATES:
        state = fallback_state
    return {"state": state, "confidence": 0.3, "evidence": ["normalized_legacy_requiredness"]}


def requiredness(node: dict[str, Any]) -> dict[str, Any]:
    """Evidence-backed requiredness object.

    Missing metadata never projects to required=true.
    """
    existing = node.get("requiredness")
    if isinstance(existing, dict) and str(existing.get("state") or "") in REQUIREDNESS_STATES:
        return normalize_requiredness(existing)
    visible_marker = "*" in f"{node.get('resolved_label') or ''} {node.get('label') or ''}"
    if field_is_explicitly_optional(node) and not visible_marker:
        return {"state": "optional_proven", "confidence": 0.9, "evidence": ["voluntary_or_optional_text"]}
    if _policy_nonblocking_disclosure(node) and not visible_marker:
        return {"state": "optional_likely", "confidence": 0.82, "evidence": ["policy_disclosure_nonblocking", "no_required_marker_near_label"]}
    if node.get("required"):
        return {"state": "required_proven", "confidence": 0.9, "evidence": ["required_flag"]}
    return {"state": "unknown_blocking", "confidence": 0.35, "evidence": ["no_required_evidence"]}


def legacy_required(requiredness_or_node: Any) -> bool:
    """Safe compatibility projection for old boolean consumers."""
    if isinstance(requiredness_or_node, dict) and "state" in requiredness_or_node:
        obj = normalize_requiredness(requiredness_or_node)
    elif isinstance(requiredness_or_node, dict):
        obj = requiredness(requiredness_or_node)
    else:
        obj = normalize_requiredness(requiredness_or_node)
    return str(obj.get("state") or "") in BLOCKING_REQUIREDNESS_STATES
