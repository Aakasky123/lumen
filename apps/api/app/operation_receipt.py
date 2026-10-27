"""Execution/accountability layer for operator-level operations (control-plane: cancel_run,
start_run, …) AND, later, browser actions (browser_click, …). This is NOT the agent's reasoning —
it only records what actually happened, so replies are rendered from reality, never from a planner's
claim. The receipt models four independent axes plus a runtime-computed outcome:

    intended → attempted → executed → observed → verified

    attempt.status:      "attempted" | "not_attempted"   (+ reason when not attempted)
    execution.status:    "succeeded" | "failed" | "rejected" | "not_executed"
    verification.status: "succeeded" | "failed" | "inconclusive" | "not_verified"  (+ before/after)
    outcome (DERIVED, runtime-only, NEVER planner-supplied): one of OUTCOMES.

An operation may be: not attempted because a precondition failed; skipped because a dependency
failed; attempted but rejected by the executor; reported succeeded by the executor yet fail
postcondition verification; executed but impossible to verify; or verified succeeded.
"""

from __future__ import annotations

from typing import Any


def compute_outcome(*, attempt: dict[str, Any], execution: dict[str, Any], verification: dict[str, Any]) -> str:
    """Derive the single outcome from the three independent axes. Computed by the runtime ONLY —
    the planner/model never supplies it."""
    if str(attempt.get("status")) != "attempted":
        reason = str(attempt.get("reason") or "")
        if reason == "dependency_failed":
            return "dependency_skipped"
        return "precondition_failed"  # not attempted for any other reason (precondition not met)
    if str(execution.get("status")) in {"failed", "rejected", "not_executed"}:
        return "execution_failed"
    verification_status = str(verification.get("status"))
    if verification_status == "succeeded":
        return "verified_succeeded"
    if verification_status == "failed":
        return "verification_failed"
    return "verification_inconclusive"  # inconclusive / not_verified — executed but unconfirmed


def make_receipt(
    *,
    operation_id: str,
    type: str,
    target: dict[str, Any] | None = None,
    attempt: dict[str, Any],
    execution: dict[str, Any],
    verification: dict[str, Any],
) -> dict[str, Any]:
    return {
        "operation_id": operation_id,
        "type": type,
        "target": target or {},
        "attempt": attempt,
        "execution": execution,
        "verification": verification,
        "outcome": compute_outcome(attempt=attempt, execution=execution, verification=verification),
    }
