"""Typed contract between the model's decision and the executor.

The loop has passed the model's normalized decision around as a flat dict of ~40 keys, where a
click could name its target three ways at once (a control index, a point, a label) and the runtime
reconciled or rejected the combination later. `ActionProposal` is the typed form of that same
decision: exactly one `ProposalTarget` saying which references the model gave, the gesture, the
action arguments, the evidence the model cited, and the follow-up actions.

Version 1 is a lossless wrapper: `from_normalized(d).to_normalized() == d` for every decision
`vision_agent.normalize_action` produces, so the executor is unchanged. What it adds is an explicit
target kind the loop records on every decision and a structured `ProposalRejection` for sequences
the runtime refuses, so the next step (one unambiguous target per action) can be measured first.

Classifying the target is structural only: which fields the model filled, never what the page means.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PROPOSAL_VERSION = 1

# Target kinds, from which references the model supplied.
TARGET_CONTROL_AND_POINT = "control_and_point"
TARGET_CONTROL = "control"
TARGET_LABEL = "label"
TARGET_CARET = "caret"
TARGET_POINT = "point"
TARGET_TOOL = "tool"
TARGET_NONE = "none"

# Keys of a top-level normalized decision (vision_agent._normalize_action_payload), in order.
_TOP_LEVEL_KEYS = (
    "_outcome_type", "action", "notes", "plan", "control", "x", "y", "text", "text_exact", "key", "direction", "amount", "button",
    "clicks", "hold_ms", "modifiers", "follow", "target_text", "at_caret", "path", "to_x", "to_y",
    "artifact_role", "target", "wait_ms", "credential_item_id", "credential_slot", "question",
    "options", "multi_select", "skipped_optional", "query", "source_scope", "page", "proposed_answer", "display_reason",
    "confirmation_required", "grounding_source", "grounding_quote", "derivation", "approval_context_summary",
    "approval_context_evidence", "terminal_outcome", "answer", "reason", "invalid_reason",
    "key_hint",
)
# Keys of a follow-up ("then") entry, in order.
_THEN_KEYS = (
    "action", "control", "x", "y", "text", "text_exact", "key", "direction", "amount", "artifact_role", "target",
    "wait_ms", "button", "clicks", "hold_ms", "modifiers", "path", "to_x", "to_y", "follow",
    "target_text", "at_caret", "grounding_source", "grounding_quote", "derivation", "invalid_reason",
)
# Actions whose target is a tab, window, app, file, or surface named in `target`, not a page location.
_TOOL_TARGET_ACTIONS = frozenset({"switch_tab", "focus_window", "launch_app", "open_file", "switch_surface", "navigate"})


@dataclass(frozen=True)
class ProposalTarget:
    kind: str
    control: int = 0
    x: int = 0
    y: int = 0
    label: str = ""
    at_caret: bool = False
    tool: str = ""

    @property
    def ambiguous(self) -> bool:
        """Two references the runtime must reconcile (and may find disagreeing)."""
        return self.kind == TARGET_CONTROL_AND_POINT


def classify_target(action: dict[str, Any]) -> ProposalTarget:
    kind_name = str(action.get("action") or "")
    control = int(action.get("control") or 0)
    x, y = int(action.get("x") or 0), int(action.get("y") or 0)
    label = str(action.get("target_text") or "")
    at_caret = bool(action.get("at_caret"))
    tool = str(action.get("target") or "")
    if control and (x or y):
        kind = TARGET_CONTROL_AND_POINT
    elif control:
        kind = TARGET_CONTROL
    elif label:
        kind = TARGET_LABEL
    elif at_caret:
        kind = TARGET_CARET
    elif x or y:
        kind = TARGET_POINT
    elif kind_name in _TOOL_TARGET_ACTIONS and tool:
        kind = TARGET_TOOL
    else:
        kind = TARGET_NONE
    return ProposalTarget(kind=kind, control=control, x=x, y=y, label=label, at_caret=at_caret, tool=tool)


@dataclass(frozen=True)
class ActionProposal:
    kind: str
    target: ProposalTarget
    fields: dict[str, Any]
    followups: tuple["ActionProposal", ...] = ()
    sequence_invalid_reason: str = ""
    then_dropped: tuple[dict[str, Any], ...] = ()
    has_then: bool = False
    is_followup: bool = False

    @classmethod
    def from_normalized(cls, action: dict[str, Any], *, is_followup: bool = False) -> "ActionProposal":
        keys = _THEN_KEYS if is_followup else _TOP_LEVEL_KEYS
        fields = {key: action[key] for key in keys if key in action}
        extra = sorted(set(action) - set(keys) - {"then", "sequence_invalid_reason", "then_dropped"})
        if extra:
            # A normalized decision grew a key this contract does not carry yet: fail loudly in
            # tests instead of silently dropping information at the executor boundary.
            raise ValueError(f"ActionProposal v{PROPOSAL_VERSION} does not carry keys: {extra}")
        raw_then = action.get("then")
        followups = tuple(
            cls.from_normalized(item, is_followup=True) for item in (raw_then if isinstance(raw_then, list) else [])
        )
        return cls(
            kind=str(action.get("action") or ""),
            target=classify_target(action),
            fields=fields,
            followups=followups,
            sequence_invalid_reason=str(action.get("sequence_invalid_reason") or ""),
            then_dropped=tuple(
                dict(item) for item in (action.get("then_dropped") or []) if isinstance(item, dict)
            ),
            has_then="then" in action,
            is_followup=is_followup,
        )

    def to_normalized(self) -> dict[str, Any]:
        keys = _THEN_KEYS if self.is_followup else _TOP_LEVEL_KEYS
        out = {key: self.fields[key] for key in keys if key in self.fields}
        if self.has_then:
            out["then"] = [item.to_normalized() for item in self.followups]
            out["sequence_invalid_reason"] = self.sequence_invalid_reason
            if self.then_dropped:
                out["then_dropped"] = [dict(item) for item in self.then_dropped]
        return out

    def summary(self) -> dict[str, Any]:
        """Value-free telemetry for the decision event: which references the model gave."""
        return {
            "proposal_version": PROPOSAL_VERSION,
            "target_kind": self.target.kind,
            "followup_target_kinds": [item.target.kind for item in self.followups],
        }


def decision_summary(action: dict[str, Any]) -> dict[str, Any]:
    """Target kinds of a decision as the loop holds it (with its private runtime keys), for the
    decision event. Tolerant where from_normalized is strict: telemetry never fails a step."""
    raw_then = action.get("then") if isinstance(action.get("then"), list) else []
    return {
        "proposal_version": PROPOSAL_VERSION,
        "target_kind": classify_target(action).kind,
        "followup_target_kinds": [classify_target(item).kind for item in raw_then if isinstance(item, dict)],
    }


@dataclass(frozen=True)
class ProposalRejection:
    """Why the runtime refused a proposal before anything executed."""

    reason: str
    label: str = "primary"
    action: str = ""
    target_kind: str = TARGET_NONE
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_sequence_invalid(cls, sequence_invalid: dict[str, Any]) -> "ProposalRejection":
        failed = sequence_invalid.get("action") if isinstance(sequence_invalid.get("action"), dict) else {}
        return cls(
            reason=str(sequence_invalid.get("reason") or "invalid"),
            label=str(sequence_invalid.get("label") or "primary"),
            action=str(failed.get("action") or ""),
            target_kind=classify_target(failed).kind,
        )

    def summary(self) -> dict[str, Any]:
        return {
            "rejection_reason": self.reason[:120],
            "rejection_label": self.label[:40],
            "rejection_action": self.action[:40],
            "rejection_target_kind": self.target_kind,
        }
