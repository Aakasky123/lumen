"""The brain's view of verified receipts: what happened, one line each.

A stored receipt is {type, outcome, verification, target} and carries the body's dispatch telemetry
(which click path ran, whether a trusted click was attempted, selector echoes, scroll bookkeeping).
That telemetry serves approval and verification code, which keeps reading the stored receipts; the
brain needs only the action, its target, what was typed or read back, whether it took, and what the
page did. Measured on retained brain-seat requests, twelve receipts as {type, outcome, evidence-JSON}
were ~8.6k characters of a ~40k-character packet and the 900-character evidence cut dropped the
read-back fields that sat after the telemetry.

The newest receipts keep their full finding list; older ones shrink to the target and its outcome.
A key this module does not know is kept as ``key=value`` in the detailed view, so a new verification
field reaches the brain until someone decides it is telemetry.
"""

from __future__ import annotations

import json
from typing import Any

# How many of the newest receipts show every finding; the rest show target and outcome only.
DETAILED_RECEIPTS = 3

# Dispatch telemetry and bookkeeping: tells the runtime how an action ran, tells the brain nothing.
_TELEMETRY_KEYS = frozenset({
    "mode", "status", "active_typable", "active_tag", "target_tag", "trusted_click_attempted",
    "trusted_click_ok", "activation_attempted", "activation_dispatched", "activation_count",
    "dispatch_path_selected", "final_submit_revalidated", "target_option_like", "target_selected_before",
    "owner_matches_target_before", "owner_matches_target_after", "reversible_page_context_basis",
    "preflight_target_selector", "live_target_selector", "final_submit_selector",
    "scroll_container_scroll_height", "scroll_container_client_height", "target_identity_matched",
    "scroll_attempted", "browser_mutation_attempted", "browser_click_attempted", "active_has_value",
    "active_value_length", "active_selector", "active_validity_valid", "active_aria_invalid",
    "scrolled_into_view", "scrolled_within_list", "scroll_delta_img", "selection_length", "selection_changed",
    "trusted_key_attempted", "trusted_key_ok", "before_active_selector", "after_active_selector",
    "provider", "provider_display_name", "provider_id", "model", "purpose", "latency_ms",
    "total_latency_ms", "parse_status", "raw_output_empty", "raw_output_chars", "finish_reason",
    "attempt_count", "max_tokens", "page_fallback", "execution_outcome_unknown", "focus_attempted",
    "visual_readback_mismatch", "readback_mismatch",
})
# Findings this module renders itself (so they are not repeated as key=value).
_RENDERED_KEYS = frozenset({
    "target_text", "active_label", "target_role", "before_scroll_y", "scroll_y", "before_scroll_position",
    "scroll_position", "scroll_target_kind", "scroll_changed", "url_changed", "title_changed",
    "surface_changed", "adopted_new_tab", "selected_value", "selection_committed", "option_menu_closed",
    "target_selected_after", "selection_evidence", "active_value", "value_match",
    "active_value_matches_expected", "owner_readback_changed", "focus_changed", "target_state_changed",
    "linked_value_changed", "linked_value_before", "linked_value_after",
    "expansion_changed", "target_expanded_before", "target_expanded_after", "target_disclosure",
    "progress_class", "durable_progress", "verification_state", "visual_verification_required",
    "final_action", "grounding_matched_excerpt", "grounding_source", "grounding_autofilled", "key",
    "outcome_status", "outcome_evidence", "outcome_confidence", "target_text_scrolled_into_view",
    "active_hidden", "active_hidden_reason",
})
# What a refused point hit (control_not_at_resolved_point only). A click that ran also carries a `hit_tag`
# (the element it resolved to), which is not a refusal fact and is never worded as one.
_REFUSAL_HIT_KEYS = frozenset({"hit_tag", "hit_id", "hit_class", "hit_text", "hit_covers_viewport"})
_FIELD_CHARS = 160


def _text(value: Any, limit: int = _FIELD_CHARS) -> str:
    text = " ".join(str(value if value is not None else "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def _key_name(key: Any) -> str:
    """A key as the brain names it: a whitespace key reads as its name, never as an empty quote."""
    raw = str(key if key is not None else "")
    if raw and raw.isspace():
        return {"\t": "Tab", "\n": "Enter", "\r": "Enter"}.get(raw, "Space")
    return _text(raw, 40)


def _is_digest(key: str, value: Any) -> bool:
    lowered = key.lower()
    return isinstance(value, str) and any(word in lowered for word in ("signature", "digest", "hash", "fingerprint"))


def _alarm_facts(v: dict[str, Any]) -> list[str]:
    """Telemetry filtered by VALUE, not key: a signal that the action may not have taken, or took on weak
    evidence, always reaches the brain (detailed and brief lines alike); the same keys at their ordinary
    values stay hidden."""
    facts: list[str] = []
    if v.get("execution_outcome_unknown") is True:
        facts.append("execution outcome unknown")
    if v.get("active_hidden") is True and v.get("surface_changed") is not True:
        # The field holds the value, but nobody can see it: a page that reacts to key presses never took it.
        reason = _text(v.get("active_hidden_reason"), 24)
        facts.append(f"typed into a hidden input{f' ({reason})' if reason else ''}; no visible effect confirmed (unverified)")
    if v.get("active_aria_invalid") is True:
        facts.append("field marked aria-invalid")
    if v.get("active_validity_valid") is False:
        facts.append("field fails its validity check")
    if v.get("page_fallback") is True:
        facts.append("page fallback used")
    if v.get("awaiting_confirmation") is True:
        facts.append("awaiting a confirmation step")
    for key in sorted(v):
        if key.startswith("trusted_") and key.endswith("_ok") and v[key] is False:
            facts.append(f"{key[:-3].replace('_', ' ')} failed")
    if v.get("failure_reason") == "control_not_at_resolved_point" and v.get("hit_tag"):
        name = str(v["hit_tag"]) + (f"#{_text(v['hit_id'], 40)}" if v.get("hit_id") else (f".{_text(v['hit_class'], 60)}" if v.get("hit_class") else ""))
        text = _text(v.get("hit_text"), 80)
        facts.append(
            f"the point is covered by {name}" + (f" '{text}'" if text else "")
            + (" (covers the whole viewport)" if v.get("hit_covers_viewport") else " (a different control)")
        )
    state = v.get("verification_state")
    if state and state != "observed_effect":
        facts.append(f"verification={_text(state, 40)}")
    if v.get("visual_readback_mismatch") is True or v.get("readback_mismatch") is True:
        facts.append("model visual read-back differs from the intended value")
    return facts


def _detail_facts(verification: dict[str, Any], label: str, extra: dict[str, Any]) -> list[str]:
    v = verification
    facts: list[str] = []
    if "before_scroll_y" in v or "scroll_y" in v or "scroll_position" in v:
        container = v.get("scroll_target_kind") == "container"
        before = v.get("before_scroll_position" if container else "before_scroll_y", v.get("before_scroll_y"))
        after = v.get("scroll_position" if container else "scroll_y", v.get("scroll_y"))
        facts.append(f"{'container' if container else 'page'} scrolled {before}->{after}")
        if v.get("scroll_changed") is False:
            facts.append("scroll did not move")
    elif v.get("scroll_changed") is False:
        facts.append("scroll did not move")
    if v.get("scrolled_within_list") is True:
        facts.append("scrolled into view within its list")
    elif v.get("scrolled_into_view") is True or v.get("target_text_scrolled_into_view") is True:
        facts.append("scrolled into view")
    if v.get("url_changed") is True:
        facts.append("url changed")
    if v.get("title_changed") is True:
        facts.append("title changed")
    if v.get("surface_changed") is False:
        facts.append("page unchanged")
    if v.get("adopted_new_tab") is True:
        facts.append("new tab adopted")
    if v.get("selected_value") not in (None, ""):
        facts.append(f"selected '{_text(v['selected_value'])}'")
    elif v.get("selection_committed") is True:
        facts.append("choice committed")
    elif v.get("target_selected_after") is True:
        facts.append("now selected")
    if v.get("option_menu_closed") is True:
        facts.append("menu closed")
    if v.get("active_value") not in (None, ""):
        facts.append(f"field reads '{_text(v['active_value'])}'")
    if v.get("active_label") and _text(v["active_label"]) != label:
        facts.append(f"field '{_text(v['active_label'], 80)}'")
    if v.get("value_match") is False:
        facts.append("read-back does NOT match")
    elif v.get("value_match") is True and not (v.get("active_hidden") is True and v.get("surface_changed") is not True):
        facts.append("read-back matched")
    if v.get("active_value_matches_expected") is False:
        facts.append("active value differs from expected")
    if v.get("owner_readback_changed") is True:
        facts.append("owner read-back changed")
    if v.get("focus_changed") is True:
        facts.append("focus moved")
    if v.get("expansion_changed") is True:
        # A tab or disclosure header is activated, not chosen: its read-back is the expanded state.
        facts.append("now expanded" if str(v.get("target_expanded_after")) == "true" else "now collapsed")
    elif v.get("target_state_changed") is True:
        facts.append("target state changed")
    if v.get("linked_value_changed") is True:
        facts.append(
            f"nearby value changed '{_text(v.get('linked_value_before'), 60)}' -> '{_text(v.get('linked_value_after'), 60)}'"
        )
    if v.get("key"):
        facts.append(f"key '{_key_name(v['key'])}'")
    if v.get("progress_class"):
        facts.append(f"progress={v['progress_class']}")
    elif v.get("durable_progress") is True:
        facts.append("durable progress")
    state = v.get("verification_state")
    if state and state != "observed_effect":
        evidence = v.get("selection_evidence")
        facts.append(f"verification={state}" + (f" ({evidence})" if evidence and evidence != state else ""))
    for alarm in _alarm_facts({k: val for k, val in v.items() if k != "verification_state"}):
        facts.append(alarm)
    if v.get("selection_evidence") and v.get("selection_evidence") not in {"selected_state_changed"}:
        facts.append(f"selection evidence={v['selection_evidence']}")
    if v.get("visual_verification_required") is True:
        facts.append("needs a visual check")
    if v.get("final_action") is True:
        facts.append("final action")
    if v.get("grounding_matched_excerpt"):
        facts.append(f"grounded in {_text(v.get('grounding_source') or 'page', 24)}: '{_text(v['grounding_matched_excerpt'], 120)}'")
    if v.get("outcome_status"):
        facts.append(f"outcome {v['outcome_status']}" + (f": {_text(v.get('outcome_evidence'), 200)}" if v.get("outcome_evidence") else ""))
    for key, value in {**v, **extra}.items():
        if key in _TELEMETRY_KEYS or key in _RENDERED_KEYS or _is_digest(key, value):
            continue
        if key in _REFUSAL_HIT_KEYS and v.get("failure_reason") == "control_not_at_resolved_point":
            continue
        if value is None or value == "" or value == [] or value == {} or value is False:
            continue
        facts.append(f"{key}={_text(json.dumps(value, default=str, ensure_ascii=False) if not isinstance(value, str) else value, 80)}")
    return facts


def _brief_facts(verification: dict[str, Any]) -> list[str]:
    v = verification
    facts: list[str] = []
    if v.get("selected_value") not in (None, ""):
        facts.append(f"selected '{_text(v['selected_value'], 60)}'")
    elif v.get("active_value") not in (None, ""):
        facts.append(f"field '{_text(v['active_value'], 60)}'")
    if v.get("value_match") is False:
        facts.append("read-back does NOT match")
    if v.get("url_changed") is True:
        facts.append("url changed")
    if v.get("outcome_status"):
        facts.append(f"outcome {v['outcome_status']}")
    facts.extend(_alarm_facts(v))
    return facts


def _is_container_name(label: str) -> bool:
    """A short noun-like label (one to three words, no sentence punctuation) that can name a container."""
    words = label.split()
    return 0 < len(words) <= 3 and len(label) <= 30 and not label.rstrip().endswith((".", "!", "?", ":"))


def receipt_parts(receipt: dict[str, Any], *, detailed: bool) -> tuple[str, list[str]]:
    """A stored receipt as (head, facts): the action and its target, and what was read back or observed."""
    verification = receipt.get("verification") if isinstance(receipt.get("verification"), dict) else {}
    verification = {("durable_progress" if k == "progress_made" else k): val for k, val in verification.items()}
    target = receipt.get("target") if isinstance(receipt.get("target"), dict) else {}
    kind = str(receipt.get("type") or "action")
    kind = kind[len("browser_"):] if kind.startswith("browser_") else kind
    label = _text(target.get("label"), 100)
    text = _text(verification.get("target_text"), 100)
    role = _text(verification.get("target_role"), 24)
    head = kind
    if label.casefold() in {kind.casefold(), str(receipt.get("type") or "").casefold()}:
        label = ""  # the action's own name stored as its label says nothing about the target
    if text and text != label:
        # The stored label is the brain's own words for the click (a reason, a container name), the
        # verified target_text is what the control reads: lead with the control, and keep the label after it,
        # as a container when it is a short noun-like name and as the brain's note otherwise.
        head += f" '{text}'"
        if label and label.casefold() not in text.casefold():
            head += f" in '{label}'" if _is_container_name(label) else f" (your note: {label})"
    elif label:
        head += f" '{label}'"
    if role:
        head += f" [{role}]"
    # Target keys beyond the label/selector pair (an artifact, a value source) stay visible.
    extra = {f"target_{k}": val for k, val in target.items() if k not in {"label", "selector"}}
    facts = _detail_facts(verification, label or text, extra) if detailed else _brief_facts(verification)
    return head, facts


def receipt_line(receipt: dict[str, Any], *, detailed: bool) -> str:
    """One brain-facing line for a stored receipt ({type, outcome, verification, target})."""
    head, facts = receipt_parts(receipt, detailed=detailed)
    return head + (": " + "; ".join(facts) if facts else "")


# Receipts of steps that only read or looked at the screen (scroll, wait, watch, zoom, read_*). They change
# nothing the run is accountable for, and what they read is already shown under its own key (zoom_view,
# clipboard_text, downloaded_file_text, print_view_text) or in recent_actions. The brain's reason for such a
# step is not a verified fact, so these receipts never reach the brain as verified_receipts.
READ_ONLY_RECEIPT_TYPES = frozenset({
    "page_scrolled", "waited", "element_watched", "screen_region_zoomed",
    "clipboard_read", "downloaded_file_read", "print_view_read", "form_outline_read",
})


def is_brain_visible_receipt(item: Any) -> bool:
    """A stored receipt the brain may see as verified: it verified, and it changed or checked something."""
    return (
        isinstance(item, dict)
        and item.get("outcome") == "verified_succeeded"
        and str(item.get("type") or "") not in READ_ONLY_RECEIPT_TYPES
    )


def brain_receipts(receipts: Any, *, limit: int = 12, detailed: int = DETAILED_RECEIPTS) -> list[str]:
    """The newest `limit` verified receipts as brain-facing lines, oldest first; the last `detailed`
    carry every finding. Only verified_succeeded receipts that are not read-only steps reach the brain."""
    items = [
        item for item in (receipts if isinstance(receipts, list) else [])
        if is_brain_visible_receipt(item)
    ][-limit:]
    cut = len(items) - detailed
    return [receipt_line(item, detailed=index >= cut) for index, item in enumerate(items)]
