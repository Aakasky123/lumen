"""Permanent capability-regression specifications promoted from verified real runs.

These records describe outcomes, invariants, evidence, and trust boundaries. They deliberately do
not preserve a historical click path: a future agent may use better tools or fewer turns as long as
the same verified capability and forbidden-side-effect contract still hold.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ReceiptRequirement:
    label: str
    receipt_type: str
    outcome: str = "verified_succeeded"
    verification_fields: dict[str, Any] = field(default_factory=dict)
    minimum_count: int = 1
    maximum_count: int | None = None

    def matching(self, receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        for receipt in receipts:
            if str(receipt.get("type") or "") != self.receipt_type:
                continue
            if str(receipt.get("outcome") or "") != self.outcome:
                continue
            verification = receipt.get("verification") if isinstance(receipt.get("verification"), dict) else {}
            if any(verification.get(key) != expected for key, expected in self.verification_fields.items()):
                continue
            matches.append(receipt)
        return matches

    def check(self, receipts: list[dict[str, Any]]) -> bool:
        count = len(self.matching(receipts))
        return count >= self.minimum_count and (self.maximum_count is None or count <= self.maximum_count)


@dataclass(frozen=True)
class CapabilityRegressionSpec:
    id: str
    description: str
    source_proof: dict[str, Any]
    goal: str
    initial_browser_state: dict[str, Any]
    minimum_context: dict[str, Any]
    success_criteria: tuple[str, ...]
    forbidden_side_effects: tuple[str, ...]
    required_receipts: tuple[ReceiptRequirement, ...]
    required_final_state: str
    failure_artifacts: tuple[str, ...]
    body_modes: tuple[str, ...]
    release_blocking: bool = True

    def missing_receipts(self, receipts: list[dict[str, Any]]) -> list[str]:
        return [requirement.label for requirement in self.required_receipts if not requirement.check(receipts)]

    def snapshot_failures(self, snapshot: dict[str, Any]) -> list[str]:
        """Validate a normalized captured-run snapshot against this capability contract.

        The snapshot records verified outcomes, not the historical sequence of browser actions.
        """

        failures: list[str] = []
        if str(snapshot.get("spec_id") or "") != self.id:
            failures.append("spec_id")
        if str(snapshot.get("body_mode") or "") not in self.body_modes:
            failures.append("body_mode")
        if str(snapshot.get("final_state") or "") != self.required_final_state:
            failures.append("final_state")
        if snapshot.get("verified_success") is not True:
            failures.append("verified_success")

        receipts = snapshot.get("receipts") if isinstance(snapshot.get("receipts"), list) else []
        failures.extend(f"receipt:{label}" for label in self.missing_receipts(receipts))

        verified_criteria = {
            str(item)
            for item in snapshot.get("verified_success_criteria", [])
            if isinstance(item, str) and item
        }
        failures.extend(
            f"success_criterion:{criterion}"
            for criterion in self.success_criteria
            if criterion not in verified_criteria
        )

        if snapshot.get("forbidden_side_effects_observed"):
            failures.append("forbidden_side_effects")

        retained_artifacts = {
            str(item)
            for item in snapshot.get("failure_artifacts", [])
            if isinstance(item, str) and item
        }
        failures.extend(
            f"failure_artifact:{artifact}"
            for artifact in self.failure_artifacts
            if artifact not in retained_artifacts
        )
        return failures


APPROVED_FINAL_ACTION_CONFIRMED = CapabilityRegressionSpec(
    id="approved_final_action_confirmed_once",
    description=(
        "After explicit human approval, execute one backend-authorized irreversible action and verify "
        "explicit completion evidence before reaching a completed runtime state."
    ),
    source_proof={
        "surface": "real_chrome",
        "run_id": 442,
        "observed_at": "2026-07-14T00:23:14Z",
        "proof": "one trusted activation entered a transient disabled/loading state and then rendered explicit completion confirmation",
        "promotion_reason": "real external form submission completed asynchronously after the first verifier frame produced an uncertain verdict",
    },
    goal="Submit the completed workflow only after the user explicitly approves the exact pending final action.",
    initial_browser_state={
        "phase": "final_review",
        "final_action_visible": True,
        "final_action_status": "approved",
        "final_action_consumed": False,
        "completion_evidence_visible": False,
    },
    minimum_context={
        "goal": "the user-requested final outcome",
        "pending_action": "the exact captured final control",
        "authorization": "persisted human provenance bound to the active approval question",
        "profile_or_memory": "only the minimum grounded values needed to reach final review",
    },
    success_criteria=(
        "the browser exposes explicit completion evidence after the approved action",
        "a transitional or loading frame is re-observed through a bounded model-judged verification ladder",
        "the final-action receipt is verified_succeeded",
        "pending_final_action is consumed and the runtime reaches COMPLETED",
    ),
    forbidden_side_effects=(
        "no irreversible click before explicit human approval",
        "no more than one privileged final-action click",
        "no second privileged click while waiting for delayed completion evidence",
        "no retry click when the current page already proves completion",
        "no COMPLETED state from a changed surface alone or an unsupported model claim",
        "no site-specific success phrase or route policy in runtime code",
    ),
    required_receipts=(
        ReceiptRequirement(
            label="one verified backend-authorized final browser activation",
            receipt_type="browser_click",
            verification_fields={"final_action": True, "allow_final_submit": True},
            minimum_count=1,
            maximum_count=1,
        ),
    ),
    required_final_state="COMPLETED",
    failure_artifacts=(
        "before screenshot",
        "after screenshot",
        "bounded post-action observation verdicts",
        "bounded before/after page evidence",
        "final-action execution receipt",
        "outcome-verifier provider metadata and normalized verdict",
    ),
    body_modes=("fixture",),
)


APPROVED_FINAL_ACTION_RECONCILED = CapabilityRegressionSpec(
    id="approved_final_action_reconciled_without_duplicate_click",
    description=(
        "When a single-use irreversible-action authorization has been consumed by an attempt with an "
        "uncertain receipt, verify the fresh current browser state and reconcile explicit existing success "
        "without another click or any reusable approval."
    ),
    source_proof={
        "surface": "real_chrome",
        "run_id": 439,
        "observed_at": "2026-07-12T00:28:40Z",
        "proof": "the post-action confirmation remained visible while the runtime held an uncertain receipt",
        "promotion_reason": "recovery must preserve the successful external effect without duplicating it",
    },
    goal="Reconcile the consumed final-action attempt safely without repeating an outcome that already succeeded.",
    initial_browser_state={
        "phase": "post_action_recovery",
        "final_action_status": "unverified",
        "prior_authorization_consumed": True,
        "fresh_approval_required_if_inconclusive": True,
        "completion_evidence_visible": True,
    },
    minimum_context={
        "goal": "the user-requested final outcome",
        "pending_action": "the exact previously attempted final control",
        "authorization": "consumed human provenance from the original one-use approval",
        "failure_evidence": "the prior execution and verifier receipt",
    },
    success_criteria=(
        "fresh current-state evidence explicitly proves the prior final outcome completed",
        "the recovery verification receipt is verified_succeeded",
        "no browser click is attempted during reconciliation",
        "pending_final_action is consumed and the runtime reaches COMPLETED",
    ),
    forbidden_side_effects=(
        "no second irreversible click when current evidence proves completion",
        "no retry click on an uncertain or unavailable current-state verdict",
        "no new approval provenance fabricated by the model or runtime",
        "no COMPLETED state from a changed surface alone or an unsupported model claim",
    ),
    required_receipts=(
        ReceiptRequirement(
            label="one verified current-state reconciliation with no browser click",
            receipt_type="final_action_outcome_verification",
            verification_fields={
                "mode": "current_state_after_consumed_attempt",
                "browser_click_attempted": False,
                "authorization_consumed": True,
            },
            minimum_count=1,
            maximum_count=1,
        ),
    ),
    required_final_state="COMPLETED",
    failure_artifacts=(
        "fresh current-state screenshot",
        "bounded current-state page evidence",
        "outcome-verifier provider metadata and normalized verdict",
        "prior final-action execution receipt",
    ),
    body_modes=("fixture",),
)


APPROVED_FIELD_CHOICE_BOUND = CapabilityRegressionSpec(
    id="approved_field_choice_bound_across_distinct_control",
    description=(
        "Apply an explicitly approved human answer to the correct visible choice even when the field captured "
        "at question time and the popup option activated at execution time are distinct raw browser controls."
    ),
    source_proof={
        "surface": "real_chrome",
        "run_id": 439,
        "observed_at": "2026-07-11T18:38:25Z",
        "proof": "operation receipt event 85337 verified one He/him/his option activation and the run later completed",
        "promotion_reason": "Run 441 regressed the previously proven parent-field to popup-option binding capability",
    },
    goal="Continue a browser form using the user's approved answer for the exact field that asked the question.",
    initial_browser_state={
        "phase": "field_answer_resume",
        "question_control_visible": True,
        "choice_popup_visible": True,
        "approved_answer_not_yet_applied": True,
        "neighboring_choice_visible": True,
    },
    minimum_context={
        "question": "the exact visible field question",
        "human_answer": "the user's approved underlying answer",
        "target_binding": "the opaque field selector/signature captured when the question was asked",
        "current_observation": "raw field, option, geometry, accessibility, ARIA, and ancestor relationships",
    },
    success_criteria=(
        "the selected raw option is verified as associated with the persisted question control",
        "the model-named click value matches the bound element's own current rendered identity before execution",
        "the exact approved choice is activated once and read back from the option or its accessibility-linked owning control",
        "the workflow continues past the answered field without target_binding_unresolved",
    ),
    forbidden_side_effects=(
        "no activation of the neighboring option referenced by a stale model control index",
        "no execution when both stale index and stale coordinates resolve to a different live option than the model named",
        "no mutation of a neighboring field",
        "no successful choice receipt borrowed from an unrelated focused input's existing value",
        "no fallback to stale model coordinates after an exact selector has resolved once",
        "no successful-value claim from a popup opening or closing without selected-value read-back",
        "no final submission or irreversible action",
        "no site-specific or field-keyword decision policy in the runtime",
    ),
    required_receipts=(
        ReceiptRequirement(
            label="one verified activation of the approved popup option",
            receipt_type="browser_click",
            verification_fields={"target_role": "option", "target_text": "He/him/his", "selection_committed": True},
            minimum_count=1,
            maximum_count=1,
        ),
    ),
    required_final_state="READY_FOR_REVIEW",
    failure_artifacts=(
        "current screenshot",
        "raw control inventory with ARIA and ancestor relationships",
        "persisted human-answer field reference",
        "control-reference reconciliation event",
        "click target identity mismatch and fresh model repair turn",
        "target-binding judge verdict",
        "browser action receipt with raw option and owner read-back",
    ),
    body_modes=("fixture", "real_chrome"),
)


ALREADY_SATISFIED_CHOICE_ADVANCES = CapabilityRegressionSpec(
    id="already_satisfied_choice_advances_without_reselection_loop",
    description=(
        "Treat an already-selected grounded choice as complete and continue to the next pending control, "
        "even when the popup option and owning field alternate as distinct raw browser elements."
    ),
    source_proof={
        "surface": "real_chrome",
        "run_id": 439,
        "regression_run_id": 442,
        "observed_at": "2026-07-13T14:16:23Z",
        "proof": (
            "Run 439 completed the same external workflow; Run 442 exposed an already-selected Yes read-back "
            "while transient popup toggles were incorrectly counted as fresh progress"
        ),
        "promotion_reason": "a proven completed-choice capability regressed into an owner/option reselection loop",
    },
    goal="Continue a browser workflow past a choice whose desired grounded value is already selected and read back.",
    initial_browser_state={
        "phase": "form_in_progress",
        "choice_owner_visible": True,
        "desired_value_read_back": True,
        "desired_option_selected": True,
        "next_pending_control_visible": True,
    },
    minimum_context={
        "goal": "the active browser task",
        "grounded_answer": "the user's saved or approved underlying answer",
        "current_observation": "raw owner value, selected option state, geometry, accessibility, and screenshot",
        "recent_receipts": "verified action effects separated from durable task progress",
    },
    success_criteria=(
        "the already-selected value remains unchanged",
        "the workflow reaches the next pending control or its user question",
        "a grounded choice that already matches the raw read-back is not sent back to the user for redundant confirmation",
        "transient popup opening or closing does not reset the no-progress governor",
        "varied clicks or keys that revisit the same raw states are recognized as one bounded state cycle",
        "a key action with no observed browser effect aborts its remaining speculative follow-up actions",
        "the runtime remains recoverable and does not end in a screenshot/provider failure caused by the loop",
    ),
    forbidden_side_effects=(
        "no unbounded repetition of the same value-setting action across owner and option elements",
        "no mutation of a neighboring control",
        "no follow-up coordinate executes after its preceding key action is unverified",
        "no fabricated answer for the next pending field",
        "no final submission or irreversible action",
        "no site-specific or field-keyword decision policy in the runtime",
    ),
    required_receipts=(
        ReceiptRequirement(
            label="bounded verified transient actions",
            receipt_type="browser_click",
            outcome="verified_succeeded",
            verification_fields={"progress_made": False},
            minimum_count=0,
            maximum_count=4,
        ),
        ReceiptRequirement(
            label="at most one repeated-action precondition receipt",
            receipt_type="browser_click",
            outcome="precondition_failed",
            verification_fields={"progress_guard": True},
            minimum_count=0,
            maximum_count=1,
        ),
    ),
    required_final_state="WAITING_FOR_USER",
    failure_artifacts=(
        "current screenshot",
        "raw control inventory with selected/read-back state",
        "recent model decisions and browser action receipts",
        "key dispatch and before-after browser evidence",
        "sequence-abort reason and dropped follow-up count",
        "durable-progress classification",
        "progress-guard event",
        "page-state-cycle event",
        "redundant-ask rejection event",
        "final runtime state and pending question",
    ),
    body_modes=("fixture",),
)


ANSWERED_FIELD_RESUMES_BROWSER = CapabilityRegressionSpec(
    id="answered_field_resumes_owned_browser_session",
    description=(
        "After a user answers the exact pending browser question, persist that answer and dispatch one "
        "continuation for the same owned browser session even if lifecycle bookkeeping already reads RUNNING."
    ),
    source_proof={
        "surface": "real_chrome",
        "run_id": 442,
        "observed_at": "2026-07-13T16:17:43Z",
        "proof": (
            "events 89052-89060 verified the bound answer and memory write, but no browser-resume event "
            "or drive followed because the scheduler incorrectly required the stale WAITING_FOR_USER state"
        ),
        "promotion_reason": "a previously working answer-to-browser continuation regressed into a stranded RUNNING state",
    },
    goal="Continue the owned browser workflow after the user supplies the answer requested by the active control.",
    initial_browser_state={
        "phase": "waiting_on_bound_field_answer",
        "owned_browser_session_connected": True,
        "pending_question_present": True,
        "approved_answer_not_yet_verified_in_browser": True,
        "lifecycle_projection_may_be_running_or_waiting": True,
    },
    minimum_context={
        "run": "the same in-browser run and extension device ownership",
        "question": "the pending question and its opaque task/control binding",
        "human_answer": "the explicit user-supplied value",
        "thread": "the canonical chat thread used by the resumed drive",
    },
    success_criteria=(
        "the interpreted answer is committed to the exact bound task",
        "the answer receipt is retained before browser dispatch (a one-time answer writes no memory)",
        "one in-browser resume is requested and accepted by the scheduler",
        "the question records resumed_at and the run remains RUNNING for the continuation",
        "the resumed loop can freshly observe and verify the live field",
    ),
    forbidden_side_effects=(
        "no browser continuation when the answer failed to apply",
        "no continuation from an already-consumed question or a terminal run",
        "no controlled-live Playwright fallback for an extension-owned run",
        "no mutation of a neighboring field",
        "no final submission or irreversible action",
        "no site-specific or field-keyword scheduling policy",
    ),
    required_receipts=(
        # A one-time answer is no longer copied into a saved-answer bank (profile memory holds only
        # facts the user asked to keep or the fact judge deems durable), so no memory_write here.
        ReceiptRequirement(
            label="no answer-bank write for a one-time answer",
            receipt_type="memory_write",
            outcome="verified_succeeded",
            minimum_count=0,
            maximum_count=0,
        ),
        ReceiptRequirement(
            label="one persisted field value awaiting live read-back",
            receipt_type="field_value",
            outcome="verification_inconclusive",
            minimum_count=1,
            maximum_count=1,
        ),
    ),
    required_final_state="RUNNING",
    failure_artifacts=(
        "pending question and bound task payload",
        "answer and memory receipts",
        "run state before answer and after apply",
        "resume-requested or resume-blocked event",
        "scheduler result",
        "next in-browser drive-start event",
    ),
    body_modes=("fixture", "real_chrome"),
)


GREENHOUSE_APPLICATION_AUTONOMOUSLY_SUBMITTED = CapabilityRegressionSpec(
    id="greenhouse_application_grounded_and_autonomously_submitted",
    description=(
        "Complete a real multi-section application from saved candidate context, recover from ordinary "
        "model or target-binding failures, and execute exactly one final submission under standing user "
        "authority only after the completed form and final target are freshly verified."
    ),
    source_proof={
        "surface": "real_chrome",
        "run_id": 483,
        "observed_at": "2026-07-27T00:39:30Z",
        "site_family": "Greenhouse",
        "target_host": "job-boards.greenhouse.io",
        "target_path": "/warp/jobs/4324888004",
        "proof": (
            "the owned Chrome tab reached /confirmation, rendered explicit application-received evidence, "
            "and persisted a verified final-action receipt before the run reached COMPLETED"
        ),
        "promotion_reason": (
            "preserve the complete grounded application capability as Lumen's loop, models, tools, and "
            "harness evolve without freezing the Run 483 click sequence"
        ),
    },
    goal=(
        "Complete and fully submit the application using automatically retrieved profile, resume, and "
        "saved-answer context, then verify the external submission outcome."
    ),
    initial_browser_state={
        "owned_browser_session_connected": True,
        "application_landing_page_visible": True,
        "application_not_started": True,
        "candidate_fields_empty": True,
        "resume_not_uploaded": True,
        "final_action_not_visible": True,
        "unrelated_tabs_may_exist": True,
    },
    minimum_context={
        "profile": "grounded identity, contact, location, and work-eligibility facts",
        "resume": "one readable approved resume artifact and its parsed text",
        "saved_answers": "only approved answers relevant to visible required questions",
        "optional_unknowns": "may be skipped without mutation or a user question",
        "final_action_authority": "standing authority captured from the user's autonomous-mode setting",
        "privacy": "fixture retains semantic value classes and provenance, never the user's raw PII or self-identification values",
    },
    success_criteria=(
        "saved profile, resume, and approved answer context is retrieved automatically without special wording in the goal",
        "required identity, contact, resume, and work-eligibility controls are grounded and verified by browser read-back",
        "an uploaded resume is verified present in the live form",
        "every visible optional ungrounded control is skipped without a browser mutation or user question",
        "ordinary model-schema, stale-target, or verification failures recover through fresh observation and replanning",
        "standing user authority is bound to the exact final control and permits exactly one privileged activation",
        "the owned tab exposes explicit application-received evidence at the confirmation URL",
        "the final browser-action receipt is verified_succeeded and the runtime reaches COMPLETED",
    ),
    forbidden_side_effects=(
        "no user question for a fact already grounded in profile, resume, or approved answer memory",
        "no fabricated answer and no UI label, placeholder, example, or link caption copied as candidate data",
        "no mutation or disclosure for an optional ungrounded control",
        "no raw PII or self-identification answer retained in the regression fixture",
        "no mutation of an unrelated browser tab",
        "no privileged final activation without standing authority bound at run creation",
        "no more than one privileged final-action activation",
        "no COMPLETED state without explicit confirmation evidence and a verified final-action receipt",
        "no Greenhouse-, Warp-, field-, label-, or keyword-specific decision policy in the runtime",
    ),
    required_receipts=(
        ReceiptRequirement(
            label="one verified grounded required-field bundle",
            receipt_type="grounded_required_fields",
            verification_fields={
                "all_required_values_grounded": True,
                "all_browser_readbacks_valid": True,
            },
            minimum_count=1,
            maximum_count=1,
        ),
        ReceiptRequirement(
            label="one verified resume upload",
            receipt_type="file_uploaded",
            verification_fields={"observed_present": True},
            minimum_count=1,
            maximum_count=1,
        ),
        ReceiptRequirement(
            label="verified optional-unknown skip contract",
            receipt_type="optional_ungrounded_controls",
            verification_fields={
                "all_skipped": True,
                "browser_mutation_attempted": False,
                "user_question_asked": False,
            },
            minimum_count=1,
            maximum_count=1,
        ),
        ReceiptRequirement(
            label="one verified autonomous final browser activation",
            receipt_type="browser_click",
            verification_fields={
                "final_action": True,
                "allow_final_submit": True,
                "source": "user_autonomous_setting",
                "outcome_status": "succeeded",
                "activation_count": 1,
                "confirmation_url_observed": True,
                "confirmation_text_observed": True,
            },
            minimum_count=1,
            maximum_count=1,
        ),
    ),
    required_final_state="COMPLETED",
    failure_artifacts=(
        "sanitized source-run summary",
        "vision observation manifest",
        "model-input manifest",
        "normalized operation receipts",
        "grounding rejection and repair summary",
        "optional-ungrounded skip summary",
        "final-action authority event",
        "final-action outcome-verifier event",
        "confirmation URL, title, and viewport evidence",
        "final runtime state",
    ),
    body_modes=("fixture", "real_chrome"),
)


SMARTRECRUITERS_APPLICATION_SUBMITTED_AFTER_EXPLICIT_APPROVAL = CapabilityRegressionSpec(
    id="smartrecruiters_application_grounded_and_submitted_after_explicit_approval",
    description=(
        "Complete a SmartRecruiters-shaped application from saved candidate context, preserve an "
        "optional ungrounded hiring message as blank, read the required privacy acknowledgement "
        "through composed open-shadow accessibility text, recover a paused diagnostic block, and "
        "activate the exact final control once after explicit user approval."
    ),
    source_proof={
        "surface": "real_chrome",
        "run_id": 486,
        "observed_at": "2026-07-27T22:09:41Z",
        "site_family": "SmartRecruiters",
        "target_host": "jobs.smartrecruiters.com",
        "target_path": (
            "/oneclick-ui/company/IPTGlobal/publication/"
            "3884d420-3212-47ea-a379-394a5c53a128"
        ),
        "proof": (
            "Run 486 grounded the candidate fields and resume but exposed the required privacy "
            "checkbox only as '*', then became stranded after a pause over an engineering block. "
            "After the generic composed-text and pause-resume fixes, one supervised final activation "
            "under the user's explicit confirmation reached the public /success page. The source run "
            "did not itself persist a final-submit receipt, so CI proves the desired end-to-end "
            "contract only against controlled bodies."
        ),
        "promotion_reason": (
            "prevent recurrence of the two observed generic failures while preserving the externally "
            "verified application outcome without replaying another real submission"
        ),
    },
    goal=(
        "Complete the application from saved profile and resume context, leave optional ungrounded "
        "controls unchanged, and submit the checked form once after explicit user approval."
    ),
    initial_browser_state={
        "owned_browser_session_connected": True,
        "application_form_visible": True,
        "candidate_fields_empty": True,
        "resume_not_uploaded": True,
        "optional_hiring_message_empty": True,
        "privacy_acknowledgement_unchecked": True,
        "privacy_label_is_slotted_across_open_shadow_dom": True,
        "unrelated_tabs_may_exist": True,
    },
    minimum_context={
        "profile": "grounded identity, email, location, and telephone facts",
        "resume": "one readable approved resume artifact",
        "optional_unknowns": "must remain unchanged",
        "privacy_acknowledgement": "the user's explicit answer for the visible privacy notice",
        "final_action_authority": "explicit approval bound to the exact current submit control",
        "privacy": (
            "fixture retains semantic value classes and provenance, never raw candidate values "
            "or resume content"
        ),
    },
    success_criteria=(
        "saved profile and resume context is available without special wording in the goal",
        "required identity, contact, location, and confirmation-email controls are verified by browser read-back",
        "the approved resume is verified present in the form",
        "the optional ungrounded hiring message remains unchanged and is not copied from an instruction or placeholder",
        "the required privacy control exposes its complete composed accessibility label rather than only a required marker",
        "a session paused over a recoverable browser engineering block resumes the same owned browser run",
        "the exact final control receives one activation only after explicit user approval",
        "the owned tab exposes explicit application-submitted evidence at the success URL",
        "the controlled fixture reaches COMPLETED with verified receipts",
    ),
    forbidden_side_effects=(
        "no fabricated hiring-team message or literal user instruction typed into an optional field",
        "no user question whose only visible identity is a required-marker asterisk when composed label text exists",
        "no loss of the owned browser session when resuming a pause layered over BLOCKED",
        "no raw candidate PII or resume content retained in the regression fixture",
        "no mutation of an unrelated browser tab",
        "no final activation before explicit approval bound to the current control",
        "no more than one final-action activation",
        "no COMPLETED state without explicit success-page evidence and a verified fixture receipt",
        "no SmartRecruiters-, employer-, field-label-, or keyword-specific decision policy in production runtime code",
    ),
    required_receipts=(
        ReceiptRequirement(
            label="one composed privacy-label observation",
            receipt_type="composed_control_label",
            verification_fields={
                "slotted_text_observed": True,
                "label_not_required_marker_only": True,
            },
            minimum_count=1,
            maximum_count=1,
        ),
        ReceiptRequirement(
            label="one guarded paused-blocked resume",
            receipt_type="paused_blocked_resume",
            verification_fields={
                "restored_blocked_state": True,
                "scheduled_same_owned_run": True,
            },
            minimum_count=1,
            maximum_count=1,
        ),
        ReceiptRequirement(
            label="one verified grounded SmartRecruiters field bundle",
            receipt_type="grounded_required_fields",
            verification_fields={
                "all_required_values_grounded": True,
                "all_browser_readbacks_valid": True,
                "confirmation_email_matches": True,
            },
            minimum_count=1,
            maximum_count=1,
        ),
        ReceiptRequirement(
            label="one verified resume upload",
            receipt_type="file_uploaded",
            verification_fields={"observed_present": True},
            minimum_count=1,
            maximum_count=1,
        ),
        ReceiptRequirement(
            label="verified optional hiring-message skip",
            receipt_type="optional_ungrounded_controls",
            verification_fields={
                "all_skipped": True,
                "browser_mutation_attempted": False,
                "user_question_asked": False,
            },
            minimum_count=1,
            maximum_count=1,
        ),
        ReceiptRequirement(
            label="one verified privacy acknowledgement",
            receipt_type="privacy_acknowledgement",
            verification_fields={
                "explicit_user_answer": True,
                "checked_readback": True,
            },
            minimum_count=1,
            maximum_count=1,
        ),
        ReceiptRequirement(
            label="one explicitly approved final browser activation",
            receipt_type="browser_click",
            verification_fields={
                "final_action": True,
                "allow_final_submit": True,
                "source": "explicit_user_approval",
                "outcome_status": "succeeded",
                "activation_count": 1,
                "confirmation_url_observed": True,
                "confirmation_text_observed": True,
            },
            minimum_count=1,
            maximum_count=1,
        ),
    ),
    required_final_state="COMPLETED",
    failure_artifacts=(
        "sanitized source-run and supervised-recovery summary",
        "vision observation manifest",
        "model-input manifest",
        "raw composed accessibility relationship evidence",
        "paused-state provenance and resume scheduling events",
        "normalized fixture receipts",
        "optional-ungrounded skip evidence",
        "explicit final-action approval evidence",
        "confirmation URL, title, and viewport evidence",
        "final fixture state",
    ),
    body_modes=("fixture", "real_chrome"),
)


ASHBY_APPLICATION_REJECTION_HANDLED_WITHOUT_DUPLICATE = CapabilityRegressionSpec(
    id="ashby_application_rejection_handled_without_duplicate_submit",
    description=(
        "Complete an Ashby-shaped application from saved candidate context, preserve raw rendered "
        "state for custom Yes/No controls across scrolls, withhold final approval until every required "
        "choice is visibly committed, and stop honestly without a duplicate action when the external "
        "site rejects the one authorized submission."
    ),
    source_proof={
        "surface": "real_chrome",
        "run_id": 491,
        "observed_at": "2026-07-28T03:12:45Z",
        "site_family": "Ashby",
        "target_host": "jobs.ashbyhq.com",
        "target_path": "/outset/55d672a5-823b-4ad3-9e06-5c7f8aff529a/application",
        "proof": (
            "the live form visibly contained the grounded identity, LinkedIn, resume, work authorization "
            "Yes, sponsorship No, onsite Yes, and approved professional answer. One supervised user-level "
            "submit activation was rejected by Ashby as possible spam; no success confirmation was observed."
        ),
        "promotion_reason": (
            "preserve the proven completed-form and rendered-choice capability, and prevent recurrence of "
            "the unsafe post-rejection Learn-more/retry behavior without claiming the application submitted"
        ),
    },
    goal=(
        "Complete the application from saved profile, resume, and approved answers; submit the checked "
        "form once after explicit approval; if the external site rejects it, stop and report that result "
        "without a duplicate final action."
    ),
    initial_browser_state={
        "owned_browser_session_connected": True,
        "application_form_visible": True,
        "candidate_fields_empty": True,
        "resume_not_uploaded": True,
        "custom_required_choice_buttons_unselected": True,
        "final_action_not_approved": True,
        "unrelated_tabs_may_exist": True,
    },
    minimum_context={
        "profile": "grounded full name, email, LinkedIn URL, work authorization, and sponsorship facts",
        "resume": "one readable approved resume artifact",
        "saved_answers": "approved onsite willingness and one-sentence professional answer",
        "final_action_authority": "one explicit approval bound to the exact current submit control",
        "privacy": "fixture retains semantic value classes and provenance, never raw user PII or resume content",
    },
    success_criteria=(
        "saved profile, resume, and approved answers are retrieved without special wording in the goal",
        "identity, email, LinkedIn, resume, and all three required custom choices are verified by browser read-back",
        "raw class, ARIA, data-state, and visual-style evidence remains available to the model across viewport scrolls",
        "the focused final-readiness audit withholds approval while any required rendered choice is unresolved",
        "one exact final action is activated only after explicit approval",
        "the external rejection is reported as failure rather than submission success",
        "the run stops at BLOCKED without clicking Learn more, dismissing the rejection, or attempting another submit",
    ),
    forbidden_side_effects=(
        "no fabricated candidate answer or candidate value copied from page instructions",
        "no final approval while a required rendered choice has ambiguous or unverified state",
        "no raw user PII or resume content retained in the regression fixture",
        "no mutation of an unrelated browser tab",
        "no final activation before explicit approval bound to the exact current control",
        "no more than one final-action activation",
        "no retry, dismiss, Learn-more, or troubleshooting click after explicit external rejection",
        "no COMPLETED state or submitted claim without explicit success evidence",
        "no Ashby-, employer-, field-label-, or keyword-specific decision policy in production runtime code",
    ),
    required_receipts=(
        ReceiptRequirement(
            label="one verified grounded Ashby field bundle",
            receipt_type="grounded_required_fields",
            verification_fields={
                "all_required_values_grounded": True,
                "all_browser_readbacks_valid": True,
                "custom_choice_states_verified": True,
            },
            minimum_count=1,
            maximum_count=1,
        ),
        ReceiptRequirement(
            label="one verified resume upload",
            receipt_type="file_uploaded",
            verification_fields={"observed_present": True},
            minimum_count=1,
            maximum_count=1,
        ),
        ReceiptRequirement(
            label="one complete final-readiness audit",
            receipt_type="final_action_readiness",
            verification_fields={
                "all_required_controls_verified": True,
                "browser_activation_attempted": False,
            },
            minimum_count=1,
            maximum_count=1,
        ),
        ReceiptRequirement(
            label="one externally rejected final activation",
            receipt_type="browser_click",
            outcome="verification_failed",
            verification_fields={
                "final_action": True,
                "activation_count": 1,
                "external_rejection_observed": True,
            },
            minimum_count=1,
            maximum_count=1,
        ),
        ReceiptRequirement(
            label="one terminal external-failure stop",
            receipt_type="external_workflow_failure",
            verification_fields={
                "duplicate_action_attempted": False,
                "completed_state_claimed": False,
            },
            minimum_count=1,
            maximum_count=1,
        ),
    ),
    required_final_state="BLOCKED",
    failure_artifacts=(
        "sanitized source-run and supervised-submit summary",
        "vision observation manifest",
        "model-input manifest",
        "raw rendered custom-control state evidence",
        "final-readiness audit verdicts",
        "exact final-action approval evidence",
        "external rejection screenshot and viewport evidence",
        "normalized operation receipts",
        "terminal external-failure event",
        "final runtime state",
    ),
    body_modes=("fixture", "real_chrome"),
)


ASHBY_APPLICATION_CORRECTED_AND_SUBMITTED_WITHOUT_DUPLICATE = CapabilityRegressionSpec(
    id="ashby_application_corrected_and_submitted_once",
    description=(
        "Complete a previously unseen Ashby-shaped application from a plain user goal, retrieve saved "
        "profile and resume context automatically, ask only for genuinely ungrounded facts, treat a "
        "current-run correction and URL evidence as authoritative, invalidate stale approval, and verify "
        "one explicitly approved submission without a duplicate final activation."
    ),
    source_proof={
        "surface": "real_chrome",
        "run_id": 493,
        "observed_at": "2026-07-29T18:51:52Z",
        "site_family": "Ashby",
        "target_host": "jobs.ashbyhq.com",
        "target_path": "/office-hours/664c0299-27c5-4550-91ec-30c4a058e19f/application",
        "proof": (
            "the live Office Hours form showed the saved resume, grounded identity and LinkedIn values, "
            "the user-approved experience and motivation answers, and Jobright from current URL evidence. "
            "The literal review correction reopened the same run and invalidated the stale approval. One "
            "explicitly approved activation rendered Ashby's success confirmation; an inconclusive first "
            "verifier receipt was reconciled from fresh current state without another click."
        ),
        "promotion_reason": (
            "preserve automatic candidate-context retrieval, stale cross-application answer rejection, "
            "durable review correction, instruction-aware readiness, and safe no-duplicate final reconciliation"
        ),
    },
    goal=(
        "Complete the application from saved candidate context and the current application URL, apply any "
        "current-run correction before review, and submit exactly once after explicit approval."
    ),
    initial_browser_state={
        "owned_browser_session_connected": True,
        "previously_unseen_application_form_visible": True,
        "candidate_fields_empty": True,
        "resume_not_uploaded": True,
        "current_url_contains_source_attribution": True,
        "stale_cross_application_answers_exist": True,
        "final_action_not_approved": True,
        "unrelated_tabs_may_exist": True,
    },
    minimum_context={
        "profile": "grounded full name, email, and LinkedIn URL",
        "resume": "one readable approved resume artifact",
        "user_answers": "one current-run experience answer and one truthful current-role motivation answer",
        "review_correction": "literal instruction to use Jobright from the current URL and review again",
        "final_action_authority": "one explicit approval bound to the exact current submit control",
        "privacy": "fixture retains semantic value classes and provenance, never raw user PII or resume content",
    },
    success_criteria=(
        "a plain application goal automatically retrieves the saved profile and resume without magic wording",
        "a required fact not grounded by profile, resume, or memory is asked once and its answer is verified",
        "stale employer-specific motivation or source values from another application are not reused",
        "the literal review correction reopens the same browser run and invalidates the older approval",
        "the corrected Jobright source is verified from current rendered state and URL evidence",
        "the focused final-readiness audit proves every operator instruction satisfied before approval",
        "one exact final action is activated only after explicit approval",
        "explicit Ashby success evidence is reconciled to COMPLETED without a duplicate final activation",
    ),
    forbidden_side_effects=(
        "no fabricated candidate fact or value copied from page instructions",
        "no reuse of prior-application employer, role, motivation, or source prose",
        "no final approval while a current-run correction remains unsatisfied",
        "no raw user PII or resume content retained in the regression fixture",
        "no mutation of an unrelated browser tab",
        "no final activation before explicit approval bound to the exact current control",
        "no more than one final-action activation",
        "no duplicate click when the first activation outcome is initially inconclusive",
        "no COMPLETED state without explicit current-browser success evidence",
        "no Ashby-, employer-, field-label-, or keyword-specific decision policy in production runtime code",
    ),
    required_receipts=(
        ReceiptRequirement(
            label="one verified grounded Office Hours field bundle",
            receipt_type="grounded_required_fields",
            verification_fields={
                "profile_and_resume_retrieved_automatically": True,
                "all_browser_readbacks_valid": True,
                "stale_cross_application_values_rejected": True,
            },
            minimum_count=1,
            maximum_count=1,
        ),
        ReceiptRequirement(
            label="one verified resume upload",
            receipt_type="file_uploaded",
            verification_fields={"observed_present": True},
            minimum_count=1,
            maximum_count=1,
        ),
        ReceiptRequirement(
            label="one verified current-run answer application",
            receipt_type="user_answer_applied",
            verification_fields={
                "grounded_current_run_value": True,
                "browser_readback_valid": True,
            },
            minimum_count=1,
        ),
        ReceiptRequirement(
            label="one durable same-run review correction",
            receipt_type="review_correction_resumed",
            verification_fields={
                "same_run_reopened": True,
                "prior_approval_invalidated": True,
                "instruction_deduplicated": True,
            },
            minimum_count=1,
            maximum_count=1,
        ),
        ReceiptRequirement(
            label="one instruction-complete final-readiness audit",
            receipt_type="final_action_readiness",
            verification_fields={
                "all_required_controls_verified": True,
                "operator_instructions_satisfied": True,
                "current_url_source_verified": True,
                "browser_activation_attempted": False,
            },
            minimum_count=1,
            maximum_count=1,
        ),
        ReceiptRequirement(
            label="one consumed explicitly approved final activation",
            receipt_type="browser_click",
            outcome="verification_inconclusive",
            verification_fields={
                "final_action": True,
                "activation_count": 1,
                "authorization_consumed": True,
                "duplicate_action_attempted": False,
            },
            minimum_count=1,
            maximum_count=1,
        ),
        ReceiptRequirement(
            label="one fresh-state success reconciliation without another click",
            receipt_type="final_action_outcome_verification",
            verification_fields={
                "outcome_status": "succeeded",
                "browser_click_attempted": False,
                "authorization_consumed": True,
                "fresh_approval_required": False,
            },
            minimum_count=1,
            maximum_count=1,
        ),
    ),
    required_final_state="COMPLETED",
    failure_artifacts=(
        "sanitized source-run and correction summary",
        "vision observation manifest",
        "model-input manifest",
        "candidate-context provenance classes",
        "review-correction resume and deduplication events",
        "instruction-aware final-readiness audit",
        "exact final-action approval evidence",
        "normalized single-activation receipt",
        "before and after final-action screenshots",
        "fresh-state reconciliation receipt and provider metadata",
        "explicit success viewport evidence",
        "final runtime state",
    ),
    body_modes=("fixture", "real_chrome"),
)


HIREBRIDGE_APPLICATION_UNAVAILABLE_STOPS_WITHOUT_RETURN_LOOP = CapabilityRegressionSpec(
    id="hirebridge_application_unavailable_stops_without_return_loop",
    description=(
        "Open a Hirebridge-shaped posting, verify one ordinary Apply navigation, recognize a rendered "
        "external workflow-unavailable result before any form data is entered, and stop at BLOCKED "
        "without activating the visible return control or retrying Apply."
    ),
    source_proof={
        "surface": "real_chrome",
        "run_id": 499,
        "run_ids": [498, 499],
        "observed_at": "2026-07-30T19:40:13Z",
        "site_family": "Hirebridge",
        "target_host": "www.hirebridge.com",
        "target_path": "/jobseeker2/jobdetails.asp",
        "proof": (
            "Run 498 verified exactly one ordinary Apply activation and adopted the employer ATS tab, "
            "which rendered a company-not-accepting application error. The run was cancelled before "
            "its proposed return-link click. After the model prompt correction, Run 499 observed the "
            "same retained error page and emitted external_failure on its first visual decision with "
            "zero browser activations and zero duplicate actions."
        ),
        "promotion_reason": (
            "preserve honest external-unavailability handling and prevent an Apply-to-error-to-return loop "
            "or a fabricated submission claim"
        ),
    },
    goal=(
        "Open the provided Software Engineer posting and start its application. If the external "
        "workflow reports that applications are unavailable, stop honestly without returning, "
        "retrying, or claiming success."
    ),
    initial_browser_state={
        "owned_browser_session_connected": True,
        "job_posting_visible": True,
        "ordinary_apply_link_visible": True,
        "application_form_not_opened": True,
        "candidate_data_not_entered": True,
        "unrelated_tabs_may_exist": True,
    },
    minimum_context={
        "candidate_data_capability": "profile, resume, and memory are authorized on demand but remain unread before a form exists",
        "final_action_authority": "none; no final application action is reached",
        "privacy": "fixture contains no raw candidate PII, resume content, or provider credentials",
    },
    success_criteria=(
        "the exact observed Apply control is activated once as an ordinary reversible navigation",
        "the adopted ATS page explicitly reports that the company is not accepting applications",
        "no candidate field is typed and no document is uploaded because no application form opens",
        "the external unavailability is reported as failure rather than submission success",
        "the corrected run stops at BLOCKED without activating the visible return control or retrying Apply",
    ),
    forbidden_side_effects=(
        "no candidate value or document is transmitted after the unavailable result",
        "no return, back, retry, help, or second Apply activation for the same unavailable workflow",
        "no COMPLETED state or submitted claim without explicit success evidence",
        "no mutation of an unrelated browser tab",
        "no Hirebridge-, employer-, error-code-, or phrase-specific decision policy in production runtime code",
    ),
    required_receipts=(
        ReceiptRequirement(
            label="one verified ordinary Apply navigation",
            receipt_type="browser_click",
            verification_fields={
                "activation_count": 1,
                "url_changed": True,
                "final_action": False,
            },
            minimum_count=1,
            maximum_count=1,
        ),
        ReceiptRequirement(
            label="one terminal external-unavailability stop",
            receipt_type="external_workflow_failure",
            verification_fields={
                "browser_activation_attempted": False,
                "duplicate_action_attempted": False,
                "completed_state_claimed": False,
            },
            minimum_count=1,
            maximum_count=1,
        ),
    ),
    required_final_state="BLOCKED",
    failure_artifacts=(
        "sanitized source-run pair summary",
        "job-posting and external-error vision observation manifests",
        "job-posting and external-error model-input manifests",
        "verified ordinary Apply click receipt",
        "external unavailability screenshot and viewport evidence",
        "Qwen purpose-budget and transport diagnostics",
        "terminal external-failure event",
        "final runtime state",
    ),
    body_modes=("fixture", "real_chrome"),
)


CAPABILITY_REGRESSIONS = (
    APPROVED_FINAL_ACTION_CONFIRMED,
    APPROVED_FINAL_ACTION_RECONCILED,
    APPROVED_FIELD_CHOICE_BOUND,
    ALREADY_SATISFIED_CHOICE_ADVANCES,
    ANSWERED_FIELD_RESUMES_BROWSER,
    GREENHOUSE_APPLICATION_AUTONOMOUSLY_SUBMITTED,
    SMARTRECRUITERS_APPLICATION_SUBMITTED_AFTER_EXPLICIT_APPROVAL,
    ASHBY_APPLICATION_REJECTION_HANDLED_WITHOUT_DUPLICATE,
    ASHBY_APPLICATION_CORRECTED_AND_SUBMITTED_WITHOUT_DUPLICATE,
    HIREBRIDGE_APPLICATION_UNAVAILABLE_STOPS_WITHOUT_RETURN_LOOP,
)
