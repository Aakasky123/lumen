"""Per-task brain escalation: a cheap primary brain hands a stuck part of a task to a smarter model.

The run loop drives the user's primary vision brain. When that brain keeps failing on the same part
of a task (the runtime rejects its actions, or its actions run without durable progress), the next
decisions go to the user-designated escalation model; the first durable progress that model makes
hands the task back to the primary brain. Failures are the runtime's own deterministic step outcome
codes, never an interpretation of the page. Provider errors are not failures here: the provider
failover ladder owns those.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

# Brain failures on one part of a task before the next decision goes to the escalation model.
ESCALATION_FAILURE_THRESHOLD = 3

# Step outcomes that mean the brain's decision failed: the runtime rejected or withheld the action,
# or the action ran and the workflow did not advance.
BRAIN_FAILURE_OUTCOMES = frozenset(
    {
        "invalid",
        "sequence_invalid",
        "target_unresolved",
        "blind_click_retry",
        "target_binding_rejected",
        "ask_target_binding_rejected",
        "rejected_citation",
        "proposed_answer_rejected",
        "proposed_answer_missing",
        "optional_skip_binding_rejected",
        "redundant_grounded_ask_rejected",
        "grounded_proposal_replan",
        "done_rejected",
        "executed_without_durable_progress",
        "repeated_pre_action_revalidation_failure",
        "precondition_blocked",
        "grounding_recovery_exhausted",
        "page_state_cycle_exhausted",
        "candidate_query_exhausted",
        "candidate_query_duplicate",
        "web_tool_duplicate",
        "web_tool_exhausted",
        "final_approval_context_ungrounded",
        "final_action_readiness_unverified_controls",
        "final_action_readiness_audit_pending",
        # The brain reasoned past its output budget or its deadline without deciding: on a geometric or
        # dense desktop part it does so again, so that part goes to the escalation brain.
        "empty_length",
        "timeout_inflight",
    }
)


# Withheld-action reasons that are the body's own doing (its binding of the control to the point went
# stale or the page geometry disagrees with it), not a decision the brain made: a stronger model cannot
# fix them, so a streak of them never escalates the brain.
HARNESS_REFUSAL_REASONS = frozenset(
    {
        "click_target_drift",
        "click_target_drift_at_dispatch",
        "click_target_identity_unavailable",
        "control_not_found",
        "control_selector_ambiguous",
        "control_not_at_resolved_point",
        "click_detached",
    }
)

# Terminal stops that mean the brain could not get past a part of the task. Before the loop ends a
# run this way, the escalation brain gets that part of the task.
BRAIN_FAILURE_STOP_REASONS = frozenset({"no_progress", "agent_confused", "target_binding_unresolved"})

# Step outcomes where the runtime itself withheld the brain's action before it ran (a stale target, a
# repeated mutation with no verified change, an action sequence or citation it could not validate).
# The stop message names these as the cause when they are the last thing that happened.
RUNTIME_REFUSAL_OUTCOMES = frozenset(
    {
        "pre_action_revalidation_failed",
        "repeated_pre_action_revalidation_failure",
        "repeated_harness_refusal",
        "precondition_blocked",
        "sequence_invalid",
        "rejected_citation",
        "target_binding_rejected",
        "ask_target_binding_rejected",
        "optional_skip_binding_rejected",
        "proposed_answer_rejected",
        "redundant_grounded_ask_rejected",
    }
)

# Step outcomes where the page returned to a state it was already in and nothing advanced. The stop message
# says the page did not change when this is the last thing that happened.
PAGE_UNCHANGED_OUTCOMES = frozenset({"page_state_cycle_exhausted"})
# Step outcomes where the action ran but no progress was verified. This is not the same claim as an unchanged
# page (the page may have changed without the task advancing), so the stop message words it apart.
NO_VERIFIED_PROGRESS_OUTCOMES = frozenset({"executed_without_durable_progress"})

# Engineering stops the loop recovers from in-loop before a run ends BLOCKED. Each one is a stuck episode
# (the brain's actions ran without progress, its replies were refused, or the body could not confirm a
# scroll), not a question for the user. Only the call sites that tag a stop as a stuck episode use this set.
SELF_RECOVERABLE_STOP_REASONS = frozenset({"no_progress", "agent_confused", "scroll_execution_outcome_unknown"})

# Recoveries per run. The count rides in the run journal, so a resumed or restarted loop keeps it.
SELF_RECOVERY_LIMIT = 2

# Stops that are never recovered from, whatever the budget: a payment, an approval, a sensitive entry or
# credential takeover, a secret or grounding refusal that needs a user fact, a mask or binding safety stop,
# a user cancel or pause, the step cap, a rejected external workflow, and a runtime stall. A stop whose
# original reason is one of these ends BLOCKED at once.
SELF_RECOVERY_NEVER_REASONS = frozenset(
    {
        "payment_floor_confirmation",
        "final_submit_approval_required",
        "final_submit_outcome_unknown",
        "needs_answer",
        "user_only_question_judge_unavailable",
        # Final-action and ask gates whose no-progress stops are not tagged as recoverable.
        "final_submit_page_context_drift",
        "final_approval_context_ungrounded",
        "final_action_repeat_refused",
        "redundant_grounded_ask",
        "recovery_constraint_ignored",
        "credential_type_requires_vault_action",
        "keyed_text_sensitive_requires_type",
        "sensitive_type_requires_bound_control",
        "sensitive_browser_takeover",
        "sensitive_browser_continue_authority_required",
        "watch_target_sensitive",
        "page_value_about_user",
        "secret_classifier_unavailable",
        "secret_destination_judge_unavailable",
        "grounding_check_unavailable",
        "grounding_recovery_exhausted",
        "sensitive_mask_binding_unavailable",
        "sensitive_screenshot_mask_unavailable",
        "sensitive_screenshot_mask_rebind_failed",
        "user_cancelled",
        "session_paused",
        "max_steps",
        "external_workflow_rejected",
        "runtime_stall",
    }
)

# Outcome codes the repair line names as the last refusals of a stuck episode.
SELF_RECOVERY_CODE_OUTCOMES = frozenset({"repeated_harness_refusal", "scroll_execution_outcome_unknown"})

# What a recovery tells the brain was stuck, per stop reason.
SELF_RECOVERY_STUCK_TEXT = {
    "no_progress": "its actions ran without moving the task forward",
    "agent_confused": "its replies were refused as invalid or out of sequence",
    "scroll_execution_outcome_unknown": "a scroll's result was lost in transit and the page position is unknown",
}


def can_self_recover(
    reason: str,
    recoveries_used: int,
    original_reason: str = "",
    codes: Iterable[str] = (),
) -> bool:
    """Whether an engineering stop gets another in-loop recovery instead of ending the run BLOCKED. A refusal
    code the run recorded for the stuck episode is checked the same way as the stop reason: any code in
    SELF_RECOVERY_NEVER_REASONS (a sensitive entry, a credential takeover, an unavailable check) blocks it."""
    if reason not in SELF_RECOVERABLE_STOP_REASONS or reason in SELF_RECOVERY_NEVER_REASONS:
        return False
    if original_reason and original_reason in SELF_RECOVERY_NEVER_REASONS:
        return False
    if any(str(code or "") in SELF_RECOVERY_NEVER_REASONS for code in codes):
        return False
    return int(recoveries_used) < SELF_RECOVERY_LIMIT


def is_brain_failure_stop(summary: object) -> bool:
    return (
        isinstance(summary, dict)
        and summary.get("state") == "BLOCKED"
        and summary.get("reason") in BRAIN_FAILURE_STOP_REASONS
        and not summary.get("needs_user")
        and not summary.get("harness_refusal")
    )


@dataclass
class BrainEscalation:
    """Failure/progress bookkeeping for one run. The loop owns the provider switch itself."""

    enabled: bool = False
    threshold: int = ESCALATION_FAILURE_THRESHOLD
    failures: int = 0
    active: bool = False
    progressed_while_active: bool = False
    escalations: int = 0
    # The last recorded step ran without durable progress, which the next observation may yet show
    # was not a failure.
    no_progress_pending: bool = False

    def record_outcome(self, outcome: str) -> None:
        self.no_progress_pending = outcome == "executed_without_durable_progress"
        if outcome in BRAIN_FAILURE_OUTCOMES:
            self.failures += 1

    def settle_no_progress(self, *, reached_new_state: bool) -> None:
        """The observation after a step that ran without durable progress: when it shows a screen never
        seen before, the step did something (paged a one-at-a-time viewer to its next record) and is
        not a brain failure."""
        if self.no_progress_pending and reached_new_state and self.failures > 0:
            self.failures -= 1
        self.no_progress_pending = False

    def record_progress(self) -> None:
        self.failures = 0
        if self.active:
            self.progressed_while_active = True

    def should_escalate(self) -> bool:
        return self.enabled and not self.active and self.failures >= self.threshold

    def should_deescalate(self) -> bool:
        return self.active and self.progressed_while_active

    def mark_escalated(self) -> None:
        self.active = True
        self.progressed_while_active = False
        self.failures = 0
        self.escalations += 1

    def mark_deescalated(self) -> None:
        self.active = False
        self.progressed_while_active = False
        self.failures = 0

    def to_state(self) -> dict[str, Any]:
        """The bookkeeping a later loop of the same run restores (the run journal's "escalation")."""
        return {
            "active": self.active,
            "failures": self.failures,
            "escalations": self.escalations,
            "progressed_while_active": self.progressed_while_active,
        }

    def restore(self, saved: object) -> bool:
        """Take the bookkeeping a previous loop of this run saved. Returns True when the task stays on
        the escalation brain: the saved state was escalated and this loop has an escalation brain to
        hand it to (`enabled`). Otherwise the loop starts on the primary brain with a zero failure
        count, as a new run does. The escalation count always carries over."""
        if not isinstance(saved, dict):
            return False
        self.escalations = _saved_count(saved.get("escalations"))
        if saved.get("active") is True and self.enabled:
            self.active = True
            self.progressed_while_active = saved.get("progressed_while_active") is True
            self.failures = _saved_count(saved.get("failures"))
            return True
        self.failures = 0 if saved.get("active") is True else _saved_count(saved.get("failures"))
        return False


def _saved_count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0
