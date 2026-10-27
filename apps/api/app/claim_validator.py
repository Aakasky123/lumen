"""Grounded-response enforcement (deterministic SAFETY, not decision policy).

A conversational reply may only assert a run-lifecycle change (stopped / started / paused / resumed /
cancelled / submitted a run) if a matching `verified_succeeded` execution receipt exists for this
turn. This is the deterministic backstop behind the prompt-level instruction: when the model claims
an action anyway, the unsupported sentence is stripped before the reply is finalized — the system
reports reality, the model never narrates an action that didn't happen.

This is a verification gate on OUTPUT truthfulness; it does NOT choose the agent's strategy.
NOTE: heuristic + conservative (requires a run/application/session referent to avoid stripping benign
text like "let's get started"). It catches egregious fabrications; the prompt remains the first line,
and rendering claims directly from receipts (separate from prose) is the stronger future form.
"""

from __future__ import annotations

import re
from typing import Any

# A claimed-as-done lifecycle verb -> the receipt type that would have to back it.
_CLAIM_VERBS: tuple[tuple[str, str], ...] = (
    ("cancel_run", r"stop(?:ped|ping)?|end(?:ed|ing)?|cancell?(?:ed|ing)?|kill(?:ed|ing)?|abort(?:ed|ing)?|halt(?:ed|ing)?|shut\s+down"),
    ("start_run", r"start(?:ed|ing)?|launch(?:ed|ing)?|kick(?:ed|ing)?\s+off|begin|began|begun|created"),
    ("pause_run", r"paus(?:ed|ing)"),
    ("resume_run", r"resum(?:ed|ing)"),
    ("submit", r"submit(?:ted|ting)?|sent\s+your\s+application|applied\s+to"),
)
# A sentence only counts as a run-lifecycle CLAIM if it also references a run/application/session,
# so benign phrasing ("let's get you started", "I can help you apply") is not stripped.
_RUN_REFERENT = re.compile(r"\b(run|runs|application|session)\b|#\s*\d+", re.IGNORECASE)


def claimed_lifecycle_type(sentence: str) -> str | None:
    """The receipt type a sentence claims as DONE, or None if it makes no run-lifecycle claim."""
    if not _RUN_REFERENT.search(sentence):
        return None
    for receipt_type, verbs in _CLAIM_VERBS:
        if re.search(r"\b(?:" + verbs + r")\b", sentence, re.IGNORECASE):
            return receipt_type
    return None


def _verified_types(receipts: list[dict[str, Any]] | None) -> set[str]:
    return {str(r.get("type")) for r in (receipts or []) if r.get("outcome") == "verified_succeeded"}


def enforce_grounded_reply(text: str, receipts: list[dict[str, Any]] | None) -> tuple[str, list[str]]:
    """Strip any sentence that claims a run-lifecycle change with no backing verified receipt.
    Returns (grounded_text, stripped_sentences). If everything was an unsupported claim, returns a
    neutral grounded message instead of an empty string."""
    if not text or not text.strip():
        return text, []
    verified = _verified_types(receipts)
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    kept: list[str] = []
    stripped: list[str] = []
    for sentence in sentences:
        claim = claimed_lifecycle_type(sentence)
        if claim and claim not in verified:
            stripped.append(sentence)
        else:
            kept.append(sentence)
    if not stripped:
        return text, []
    grounded = " ".join(kept).strip()
    if not grounded:
        grounded = "I haven't started, stopped, or changed any run this turn. Tell me what you'd like me to do and I'll do it, then confirm what actually changed."
    return grounded, stripped
