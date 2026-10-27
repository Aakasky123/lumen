import hashlib
import logging
import re
import uuid
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models, remote_access, totp as totp_module
from app.agents.safety import SafetyGovernor
from app.artifact_naming import normalize_artifact_role
from app.agent_settings import get_agent_settings
from app.candidate_seed import CANDIDATE_SEED_PROVENANCE_KEY, neutral_onboarding_profile_json
from app.config import get_settings
from app.event_store import emit_event
from app.models import utcnow
from app.lumen_model import LumenModelClient
from app.resume_selection import active_base_resume
from app.run_lifecycle import run_lifecycle_guard, serialize_run_lifecycle_row
from app.state_machine import OperatorState, assert_transition


logger = logging.getLogger("lumen.runtime")


def _final_action_safety_policy(db: Session, user_id: int) -> dict[str, Any]:
    mode = str(
        get_agent_settings(db, int(user_id)).get("final_action_mode") or "approval"
    ).strip().lower()
    if mode not in {"approval", "autonomous"}:
        mode = "approval"
    held_for_device = mode == "autonomous" and remote_access.autonomy_held()
    if held_for_device:
        mode = "approval"
    standing_authority = mode == "autonomous"
    authority = "standing_user_authority" if standing_authority else "human_approval_required"
    return {
        "policy_version": 2,
        "final_action_mode": mode,
        "consequential_actions": authority,
        "payments": authority,
        "external_commitments": authority,
        "approval_granted": False,
        "standing_authority_granted": standing_authority,
        "authority_source": "paired_device_without_step_up" if held_for_device else "user_agent_setting_at_run_creation",
    }


def default_runtime_path_visibility() -> dict:
    return {
        "controlled_live_used": False,
        "legacy_filling_used": False,
        "legacy_filling_bypassed": False,
        "build_application_actions_called": False,
        "canonical_v2_routing_reason": None,
        "live_model_required": False,
        "live_model_available": False,
        "live_model_probe_status": None,
        "live_model_provider_type": None,
        "live_model_provider_id": None,
        "fallback_attempted": False,
        "fallback_blocked": False,
        "legacy_planner_touched": False,
    }


class OperatorRuntimeEngine:
    """Run lifecycle, state transitions, field-approval tasks, and final-action bookkeeping.
    Brain output is treated as intent, not authority to mutate run state."""

    def __init__(self, db: Session):
        self.db = db
        self.settings = get_settings()
        self.safety = SafetyGovernor()
        self.lumen_model = LumenModelClient(db)

    def ensure_default_user(self) -> models.User:
        users = list(self.db.scalars(select(models.User).order_by(models.User.id.asc())).all())
        user = next((item for item in users if not self._is_placeholder_email(item.email)), None)
        if not user:
            user = next((item for item in users if str(item.email or "").lower().startswith("onboarding-") and str(item.email or "").lower().endswith("@example.com")), None)
        if not user:
            user = next((item for item in users if item.email in {"jordan@example.com", "candidate@example.com"}), None)
        if user:
            self._ensure_default_profile_details(user)
            return user
        user = models.User(email=f"onboarding-{uuid.uuid4().hex[:12]}@example.com", name="Candidate")
        self.db.add(user)
        self.db.flush()
        self.db.add(models.UserProfile(user_id=user.id, profile_json=neutral_onboarding_profile_json()))
        self.db.commit()
        return user

    def _ensure_default_profile_details(self, user: models.User) -> None:
        profile = self.db.scalars(select(models.UserProfile).where(models.UserProfile.user_id == user.id)).first()
        if not profile:
            profile = models.UserProfile(user_id=user.id, profile_json=neutral_onboarding_profile_json() if self._is_placeholder_email(user.email) else {})
            self.db.add(profile)
            self.db.flush()
            return
        if self._is_placeholder_email(user.email):
            profile_json = dict(profile.profile_json or {})
            if CANDIDATE_SEED_PROVENANCE_KEY not in profile_json:
                profile_json.update(neutral_onboarding_profile_json())
                profile.profile_json = profile_json
                profile.updated_at = utcnow()
                self.db.flush()

    def create_goal(self, *, job_url: str, goal: str) -> models.OperatorRun:
        user = self.ensure_default_user()
        application = models.Application(user_id=user.id, status="draft", current_step="created")
        self.db.add(application)
        self.db.flush()
        goal, totp_seeds = totp_module.protect_goal(goal, get_settings().token_encryption_key)
        run = models.OperatorRun(
            user_id=user.id,
            application_id=application.id,
            goal=goal,
            target_url=job_url,
            current_state=OperatorState.CREATED.value,
            confidence_state={
                "final_action_policy": _final_action_safety_policy(self.db, int(user.id)),
                **({"totp_seeds": totp_seeds} if totp_seeds else {}),
            },
            risk_level=self.safety.evaluate_goal(job_url)["risk_level"],
            status_message="Goal created. Runtime is ready to plan.",
        )
        self.db.add(run)
        self.db.flush()
        emit_event(
            self.db,
            event_type="OPERATOR_GOAL_CREATED",
            payload={"goal": run.goal, "target_url": job_url},
            run_id=run.id,
            application_id=application.id,
            actor="human",
            confidence=1,
            risk_level=run.risk_level,
        )
        self.db.commit()
        return run

    def create_browser_task(
        self,
        *,
        user_id: int,
        goal: str,
        target_url: str,
        execution_surface: str = "browser",
        candidate_data_access: str = "none",
        candidate_data_scopes: list[str] | None = None,
        extra_totp_seeds: list[str] | None = None,
    ) -> models.OperatorRun:
        """Create a generic connected-Chrome task without application-domain state.

        The model owns the requested goal and initial URL. This boundary only validates that exact
        structured output, creates durable run state, and installs a fixed human-approval floor. It
        never infers workflow type or expands the user's authority from the goal text.
        """
        normalized_goal = str(goal or "").strip()
        normalized_url = str(target_url or "").strip()
        normalized_surface = str(execution_surface or "browser").strip().lower()
        normalized_candidate_data_access = str(candidate_data_access or "none").strip().lower()
        normalized_candidate_data_scopes: list[str] = []
        for item in candidate_data_scopes or []:
            scope = str(item or "").strip().lower()
            if scope not in {"profile", "resume", "memory"}:
                raise ValueError("A browser task candidate-data scope must be profile, resume, or memory.")
            if scope not in normalized_candidate_data_scopes:
                normalized_candidate_data_scopes.append(scope)
        parsed = urlparse(normalized_url)
        if not normalized_goal or len(normalized_goal) > 4000:
            raise ValueError("A browser task requires a non-empty goal of at most 4000 characters.")
        if normalized_surface not in {"browser", "desktop"}:
            raise ValueError("A digital task execution surface must be browser or desktop.")
        if normalized_surface == "desktop":
            normalized_url = "desktop://windows"
        elif (
            len(normalized_url) > 2048
            or bool(re.search(r"\s", normalized_url))
            or parsed.scheme.lower() not in {"http", "https"}
            or not parsed.netloc
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("A browser task requires one valid http(s) target URL without embedded credentials.")
        if normalized_candidate_data_access not in {"none", "on_demand"}:
            raise ValueError("A browser task candidate-data capability must be none or on_demand.")
        if normalized_candidate_data_access == "on_demand" and not normalized_candidate_data_scopes:
            raise ValueError("An on-demand candidate-data capability requires at least one explicit source scope.")
        if normalized_candidate_data_access == "none" and normalized_candidate_data_scopes:
            raise ValueError("Candidate-data source scopes cannot be granted when candidate-data access is none.")

        user = self.db.get(models.User, int(user_id or 0))
        if user is None:
            raise ValueError("A browser task requires an existing owning user.")
        safety_policy = _final_action_safety_policy(self.db, int(user.id))
        # A TOTP seed in the goal is a credential: stored encrypted with the run, replaced by a placeholder everywhere else.
        normalized_goal, totp_seeds = totp_module.protect_goal(normalized_goal, get_settings().token_encryption_key)
        totp_seeds = [*(extra_totp_seeds or []), *totp_seeds][:8]
        run = models.OperatorRun(
            user_id=int(user.id),
            application_id=None,
            goal=normalized_goal,
            target_url=normalized_url,
            current_state=OperatorState.CREATED.value,
            confidence_state={
                "final_action_policy": safety_policy,
                **({"totp_seeds": totp_seeds} if totp_seeds else {}),
                "browser_task": {
                    "schema_version": 4,
                    "goal": normalized_goal,
                    "target_url": normalized_url,
                    "execution_surface": normalized_surface,
                    "candidate_data_access": normalized_candidate_data_access,
                    "candidate_data_scopes": normalized_candidate_data_scopes,
                    "safety_policy": safety_policy,
                },
            },
            risk_level=self.safety.evaluate_goal(normalized_url)["risk_level"],
            status_message="Lumen is starting this live browser task.",
        )
        self.db.add(run)
        self.db.flush()
        emit_event(
            self.db,
            event_type="OPERATOR_BROWSER_TASK_CREATED",
            payload={
                "goal": normalized_goal,
                "target_url": normalized_url,
                "execution_surface": normalized_surface,
                "application_id": None,
                "candidate_data_access": normalized_candidate_data_access,
                "candidate_data_scopes": normalized_candidate_data_scopes,
                "safety_policy": safety_policy,
            },
            run_id=run.id,
            application_id=None,
            actor="runtime",
            confidence=1,
            risk_level=run.risk_level,
        )
        self.db.commit()
        return run

    def run_to_review(self, run_id: int) -> models.OperatorRun:
        """Browser work runs only through the connected-body vision loop; this entry blocks."""
        run = self._get_run(run_id)
        if run.current_state == OperatorState.READY_FOR_REVIEW:
            return run
        self._transition(
            run,
            OperatorState.BLOCKED,
            reason="Start or resume browser work through the connected Chrome extension vision loop.",
            actor="runtime",
            confidence=1,
        )
        self.db.commit()
        return self._get_run(run_id)

    def _block_legacy_direct_runtime(self, run: models.OperatorRun, *, phase: str) -> None:
        state = dict(run.confidence_state or {})
        visibility = dict(state.get("runtime_path_visibility") or default_runtime_path_visibility())
        visibility.update(
            {
                "legacy_filling_used": False,
                "legacy_filling_bypassed": True,
                "build_application_actions_called": False,
                "canonical_v2_routing_reason": "legacy_direct_runtime_removed",
            }
        )
        state["runtime_path_visibility"] = visibility
        run.confidence_state = state
        self._transition(
            run,
            OperatorState.BLOCKED,
            reason="The legacy deterministic browser-fill runtime has been removed. Start or resume browser work through the connected Chrome extension vision loop.",
            actor="runtime",
            confidence=1,
        )
        emit_event(
            self.db,
            event_type="LEGACY_DIRECT_RUNTIME_REMOVED",
            payload={
                "phase": phase,
                "live_path": "chat -> start_deferred -> route_deferred_chat_run -> run_vision_loop",
            },
            run_id=run.id,
            application_id=run.application_id,
            actor="runtime",
            confidence=1,
            risk_level="medium",
        )

    def cancel_run(self, run_id: int, reason: str = "Stopped by user", *, notify_chat: bool = True) -> models.OperatorRun:
        # Final-action claim and cancellation share one lifecycle guard. Cancellation wins if it
        # commits first; once the irreversible click is marked executing, cancellation is rejected
        # until verification settles so the owned browser body cannot be reused underneath it.
        from app.extension_run import final_action_execution_in_progress, final_action_lifecycle_guard

        with final_action_lifecycle_guard(), run_lifecycle_guard(run_id):
            # Serialize at the database row as well as in-process. If a question
            # transaction already owns the row, cancellation waits and then becomes
            # the last committed lifecycle mutation; if cancellation owns it first,
            # the question-side live-state CAS fails closed.
            if not serialize_run_lifecycle_row(self.db, run_id):
                return self._get_run(run_id)
            run = self._get_run(run_id)
            self.db.refresh(run)
            if final_action_execution_in_progress(run):
                raise ValueError("The approved final action is currently executing; wait for its outcome before stopping this run.")
            already_cancelled = run.current_state == OperatorState.CANCELLED.value
            if not already_cancelled:
                self._transition(run, OperatorState.CANCELLED, reason=reason, actor="human", confidence=1)
            now = utcnow()
            for task in self.db.scalars(
                select(models.RuntimeTask).where(
                    models.RuntimeTask.run_id == run.id,
                    models.RuntimeTask.status.in_(["pending", "approved"]),
                )
            ).all():
                task.status = "superseded"
                task.updated_at = now
            for question in self.db.scalars(
                select(models.OperatorQuestion).where(
                    models.OperatorQuestion.run_id == run.id,
                    models.OperatorQuestion.status == "pending",
                )
            ).all():
                question.status = "superseded"
                question.updated_at = now
            state = dict(run.confidence_state or {})
            raw_workspace = state.get("end_to_end")
            if isinstance(raw_workspace, dict):
                workspace = dict(raw_workspace)
                for key in ("pending_question_id", "pending_question_type", "pending_question_reason"):
                    workspace.pop(key, None)
                state["end_to_end"] = workspace
            sensitive_takeover = (
                dict(state.get("sensitive_browser_takeover") or {})
                if isinstance(state.get("sensitive_browser_takeover"), dict)
                else {}
            )
            if sensitive_takeover:
                sensitive_takeover.update(
                    {
                        "active": False,
                        "status": "cancelled",
                        "stopped_at": utcnow().isoformat(),
                    }
                )
                state["sensitive_browser_takeover"] = sensitive_takeover
            run.confidence_state = {
                **state,
                "user_pause": {"active": False, "cleared_by": "stop", "stopped_at": utcnow().isoformat()},
                "session_control": {"status": "stopped", "reason": reason, "updated_at": utcnow().isoformat()},
            }
            run.status_message = "Session stopped. Start a new session to continue this application."
            if not already_cancelled:
                emit_event(self.db, event_type="APPLICATION_EXECUTION_STOPPED", payload={"reason": reason}, run_id=run.id, application_id=run.application_id, actor="human", confidence=1)
            if notify_chat and not already_cancelled:
                self._add_runtime_chat_message(
                    run,
                    "Stopped this session. I will not continue it. Start a new session when you want Lumen to work on another application.",
                    reason="session_stopped",
                )
            self.db.commit()
            return run

    def pause_run(self, run_id: int, reason: str = "Paused by user", *, notify_chat: bool = True) -> models.OperatorRun:
        run = self._get_run(run_id)
        previous_pause = (run.confidence_state or {}).get("user_pause") or {}
        paused_from_state = str(previous_pause.get("paused_from_state") or run.current_state)
        if run.current_state not in {OperatorState.WAITING_FOR_USER.value, OperatorState.CANCELLED.value, OperatorState.COMPLETED.value}:
            self._transition(run, OperatorState.WAITING_FOR_USER, reason=reason, actor="human", confidence=1)
        run.confidence_state = {
            **(run.confidence_state or {}),
            "user_pause": {
                "active": True,
                "paused_from_state": paused_from_state,
                "paused_at": utcnow().isoformat(),
                "reason": reason,
            },
            "session_control": {"status": "paused", "reason": reason, "updated_at": utcnow().isoformat()},
        }
        emit_event(
            self.db,
            event_type="APPLICATION_EXECUTION_PAUSED",
            payload={"reason": reason, "paused_from_state": paused_from_state},
            run_id=run.id,
            application_id=run.application_id,
            actor="human",
            confidence=1,
        )
        run.status_message = "Paused. Lumen will not continue this session until you resume it."
        if notify_chat:
            self._add_runtime_chat_message(
                run,
                "Paused this session. I stopped runtime progress and will wait here until you click Resume or tell me to resume.",
                reason="session_paused",
                payload={"paused_from_state": paused_from_state},
            )
        self.db.commit()
        return run

    def resume_run(self, run_id: int, *, notify_chat: bool = True) -> models.OperatorRun:
        run = self._get_run(run_id)
        if run.current_state == OperatorState.READY_FOR_REVIEW.value and self._review_has_field_correction(run):
            self._reopen_review_for_field_correction(run)
        elif (
            run.current_state == OperatorState.READY_FOR_REVIEW.value
            and self._review_has_unverified_browser_actions(run)
        ):
            self._reopen_review_for_browser_verification(run)
        elif (
            run.current_state == OperatorState.READY_FOR_REVIEW.value
            and self._review_can_renew_exact_final_action(run)
        ):
            self._reopen_review_for_exact_final_action(run)
        if run.current_state == OperatorState.BLOCKED.value:
            if self._blocked_run_can_resume(run):
                self._reopen_blocked_run_for_recovery(run)
            else:
                emit_event(
                    self.db,
                    event_type="APPLICATION_EXECUTION_RESUME_REQUESTED",
                    payload={
                        "state": run.current_state,
                        "recoverable": False,
                        "recovery_gate": self._blocked_run_resume_block_reason(run),
                    },
                    run_id=run.id,
                    actor="human",
                    confidence=0.8,
                )
                self.db.commit()
                return self._get_run(run_id)
        in_browser_state = (run.confidence_state or {}).get("in_browser") if isinstance(run.confidence_state, dict) else None
        retry_state = (
            dict((run.confidence_state or {}).get("in_browser_resume_retry") or {})
            if isinstance((run.confidence_state or {}).get("in_browser_resume_retry"), dict)
            else {}
        )
        retrying_committed_handoff = bool(
            run.current_state == OperatorState.WAITING_FOR_USER.value
            and isinstance(in_browser_state, dict)
            and retry_state.get("pending")
        )
        if retrying_committed_handoff:
            self._supersede_in_browser_resume_retry_questions(run, retry_state=retry_state)
            run.current_state = OperatorState.RUNNING.value
            run.status_message = "Reconnecting the saved answer to the live browser session."
            run.updated_at = utcnow()
            self.db.flush()
        if run.current_state == OperatorState.RUNNING.value and isinstance(in_browser_state, dict):
            pending_question = self._pending_operator_question(run.id)
            if pending_question:
                run.status_message = "Lumen is waiting for your answer before the live browser session can resume."
                emit_event(
                    self.db,
                    event_type="IN_BROWSER_RESUME_BLOCKED",
                    payload={"reason": "pending_operator_question", "question_id": pending_question.id, "question_type": pending_question.question_type},
                    run_id=run.id,
                    application_id=run.application_id,
                    actor="runtime",
                    confidence=0.9,
                    risk_level="low",
                )
                self.db.commit()
                return self._get_run(run_id)
            try:
                from app.extension_run import schedule_resume_if_in_browser

                resumed = bool(schedule_resume_if_in_browser(run.id))
            except Exception as exc:
                resumed = False
                emit_event(
                    self.db,
                    event_type="IN_BROWSER_RESUME_FAILED",
                    payload={"state": run.current_state, "error": str(exc)},
                    run_id=run.id,
                    application_id=run.application_id,
                    actor="runtime",
                    confidence=0.35,
                    risk_level="medium",
                )
            if resumed:
                state = dict(run.confidence_state or {})
                state["session_control"] = {"status": "running", "reason": "Resumed live browser session", "updated_at": utcnow().isoformat()}
                if retrying_committed_handoff:
                    completed_retry = dict(state.get("in_browser_resume_retry") or {})
                    completed_retry.update(
                        {
                            "pending": False,
                            "resumed_at": utcnow().isoformat(),
                        }
                    )
                    state["in_browser_resume_retry"] = completed_retry
                run.confidence_state = state
                run.status_message = "Resuming the live browser session in Chrome."
                emit_event(
                    self.db,
                    event_type="IN_BROWSER_RESUME_SCHEDULED",
                    payload={"device_id": in_browser_state.get("device_id"), "skip_navigate": True},
                    run_id=run.id,
                    application_id=run.application_id,
                    actor="runtime",
                    confidence=0.9,
                    risk_level="low",
                )
                if notify_chat:
                    self._add_runtime_chat_message(
                        run,
                        "Resuming the live browser session in Chrome from the current page.",
                        reason="in_browser_session_resumed",
                        payload={"run_id": run.id},
                    )
            elif retrying_committed_handoff:
                state = dict(run.confidence_state or {})
                pending_retry = dict(state.get("in_browser_resume_retry") or {})
                pending_retry.update(
                    {
                        "pending": True,
                        "last_attempt_at": utcnow().isoformat(),
                        "attempt_count": int(pending_retry.get("attempt_count") or 0) + 1,
                    }
                )
                state["in_browser_resume_retry"] = pending_retry
                run.confidence_state = state
                run.current_state = OperatorState.WAITING_FOR_USER.value
                run.status_message = "Chrome is still unavailable. Reconnect it, then use Resume again."
                emit_event(
                    self.db,
                    event_type="IN_BROWSER_RESUME_RETRY_UNAVAILABLE",
                    payload={
                        "device_id": in_browser_state.get("device_id"),
                        "retry_source": pending_retry.get("source"),
                        "question_id": pending_retry.get("question_id"),
                    },
                    run_id=run.id,
                    application_id=run.application_id,
                    actor="runtime",
                    confidence=0.9,
                    risk_level="medium",
                )
            else:
                run.current_state = OperatorState.BLOCKED.value
                run.status_message = "The live browser session could not be resumed because no connected extension body was available."
                emit_event(
                    self.db,
                    event_type="IN_BROWSER_RESUME_UNAVAILABLE",
                    payload={"device_id": in_browser_state.get("device_id")},
                    run_id=run.id,
                    application_id=run.application_id,
                    actor="runtime",
                    confidence=0.7,
                    risk_level="medium",
                )
            self.db.commit()
            return self._get_run(run_id)
        pause_state = dict((run.confidence_state or {}).get("user_pause") or {})
        if run.current_state == OperatorState.WAITING_FOR_USER.value and pause_state.get("active"):
            paused_from_state = str(pause_state.get("paused_from_state") or OperatorState.FILLING_APPLICATION.value)
            if paused_from_state == OperatorState.RUNNING.value and isinstance(in_browser_state, dict):
                self._supersede_pause_diagnostic_questions(run)
            run.confidence_state = {
                **(run.confidence_state or {}),
                "user_pause": {
                    **pause_state,
                    "active": False,
                    "resumed_at": utcnow().isoformat(),
                },
                "session_control": {"status": "running", "reason": "Resumed by user", "updated_at": utcnow().isoformat()},
            }
            if notify_chat:
                self._add_runtime_chat_message(
                    run,
                    "Resuming this session now. I will continue from the paused state and stop again if I need your input.",
                    reason="session_resumed",
                    payload={"paused_from_state": paused_from_state},
                )
            if paused_from_state == OperatorState.RUNNING.value and isinstance(in_browser_state, dict):
                # Restore the lifecycle state first, then reuse the normal in-browser scheduler path.
                # This is session state recovery only; it does not choose a browser strategy.
                self._transition(run, OperatorState.RUNNING, reason="User resumed paused live browser session.", actor="human", confidence=1)
                self.db.flush()
                return self.resume_run(run.id, notify_chat=False)
            if paused_from_state == OperatorState.BLOCKED.value and isinstance(in_browser_state, dict):
                # A manual pause must not hide the durable recovery state that existed underneath
                # it. Restore BLOCKED, then reuse the normal guarded browser-recovery path so
                # pending field questions, armed final actions, and submission locks still gate
                # resume exactly as they do for an unpaused blocked run.
                self._transition(
                    run,
                    OperatorState.BLOCKED,
                    reason="User resumed a paused browser recovery session.",
                    actor="human",
                    confidence=1,
                )
                self.db.flush()
                return self.resume_run(run.id, notify_chat=False)
            if paused_from_state == OperatorState.CREATED.value:
                self._transition(run, OperatorState.CREATED, reason="User resumed paused session.", actor="human", confidence=1)
                self.db.flush()
                return self.run_to_review(run.id)
            if paused_from_state == OperatorState.READY_FOR_REVIEW.value:
                self._transition(run, OperatorState.READY_FOR_REVIEW, reason="User resumed paused review session.", actor="human", confidence=1)
                run.status_message = "Review session resumed."
                self.db.commit()
                return self._get_run(run_id)
        if run.current_state in {OperatorState.WAITING_FOR_USER.value, OperatorState.FILLING_APPLICATION.value}:
            pending_question = self._pending_operator_question(run.id)
            if pending_question:
                emit_event(
                    self.db,
                    event_type="APPLICATION_EXECUTION_RESUME_BLOCKED",
                    payload={
                        "reason": "pending_operator_question",
                        "question_id": pending_question.id,
                        "question_type": pending_question.question_type,
                    },
                    run_id=run.id,
                    application_id=run.application_id,
                    actor="runtime",
                    confidence=0.92,
                    risk_level="low",
                )
                run.status_message = "Lumen is waiting for your answer before execution can resume."
                self.db.commit()
                return self._get_run(run_id)
            if isinstance(in_browser_state, dict):
                # An in-browser run resumes exactly like a BLOCKED one: the same state gate, then the
                # same in-browser scheduler below. The legacy runtime is gone and must never take it.
                block_reason = self._blocked_run_resume_block_reason(run)
                if block_reason is not None:
                    emit_event(
                        self.db,
                        event_type="APPLICATION_EXECUTION_RESUME_REQUESTED",
                        payload={"state": run.current_state, "recoverable": False, "recovery_gate": block_reason},
                        run_id=run.id,
                        application_id=run.application_id,
                        actor="human",
                        confidence=0.8,
                    )
                    self.db.commit()
                    return self._get_run(run_id)
                if run.current_state == OperatorState.FILLING_APPLICATION.value:
                    self._transition(run, OperatorState.WAITING_FOR_USER, reason="Resume requested for a live in-browser run.", actor="human", confidence=1)
                if self._sensitive_browser_takeover_active(run):
                    # The credential or sign-in handoff needs the explicit, text-free Continue control,
                    # not a generic resume. Stay parked on the handoff instead of driving the loop.
                    run.status_message = "Lumen is waiting for you to press Continue on the secure step in Chrome before it resumes."
                    emit_event(
                        self.db,
                        event_type="IN_BROWSER_RESUME_AWAITING_SENSITIVE_CONTINUE",
                        payload={"state": run.current_state},
                        run_id=run.id,
                        application_id=run.application_id,
                        actor="human",
                        confidence=0.9,
                        risk_level="low",
                    )
                    self.db.commit()
                    return self._get_run(run_id)
                self._transition(run, OperatorState.RUNNING, reason="Resume requested for a live in-browser run.", actor="human", confidence=1)
                self.db.flush()
                return self.resume_run(run.id, notify_chat=notify_chat)
            self._block_legacy_direct_runtime(run, phase="resume_run")
        else:
            emit_event(self.db, event_type="APPLICATION_EXECUTION_RESUME_REQUESTED", payload={"state": run.current_state}, run_id=run.id, actor="human", confidence=0.8)
        self.db.commit()
        return self._get_run(run_id)

    def _add_runtime_chat_message(
        self,
        run: models.OperatorRun,
        content: str,
        *,
        reason: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        try:
            from app.cognition import OperatorChatService

            OperatorChatService(self.db).add_run_runtime_message(
                run_id=run.id,
                content=content,
                reason=reason,
                payload=payload,
            )
        except Exception as exc:
            emit_event(
                self.db,
                event_type="LUMEN_CHAT_RUNTIME_MESSAGE_FAILED",
                payload={"reason": reason, "error": str(exc)},
                run_id=run.id,
                application_id=run.application_id,
                actor="runtime",
                confidence=0.35,
                risk_level="low",
            )

    def _pending_operator_question(self, run_id: int) -> models.OperatorQuestion | None:
        questions = list(self.db.scalars(
            select(models.OperatorQuestion)
            .where(models.OperatorQuestion.run_id == run_id, models.OperatorQuestion.status == "pending")
            .order_by(models.OperatorQuestion.id.asc())
        ).all())
        for question in questions:
            if question.question_type == "blocked_reason" and self._blocked_reason_question_is_stale(run=self._get_run(run_id), question=question):
                question.status = "applied"
                question.updated_at = utcnow()
                self.db.flush()
                continue
            if self._question_still_waits_on_pending_task(question):
                return question
            question.status = "applied"
            question.updated_at = utcnow()
            self.db.flush()
        return None

    def _supersede_pause_diagnostic_questions(self, run: models.OperatorRun) -> list[int]:
        """Retire only generic diagnostic cards created while a running browser loop was paused."""
        superseded_ids: list[int] = []
        pause_status_message = str(run.status_message or "").strip()
        questions = list(
            self.db.scalars(
                select(models.OperatorQuestion).where(
                    models.OperatorQuestion.run_id == run.id,
                    models.OperatorQuestion.status == "pending",
                    models.OperatorQuestion.question_type == "blocked_reason",
                )
            ).all()
        )
        for question in questions:
            schema = question.expected_answer_schema if isinstance(question.expected_answer_schema, dict) else {}
            if (
                str(question.stuck_reason or "").strip() != pause_status_message
                or schema != {"answer": "string", "memory_update": "optional object"}
            ):
                continue
            question.status = "superseded"
            question.updated_at = utcnow()
            payload = dict(question.answer_payload or {})
            payload.update(
                {
                    "resolved_by": "session_resume",
                    "resolved_reason": "pause_diagnostic_no_longer_active",
                }
            )
            question.answer_payload = payload
            superseded_ids.append(int(question.id))

        if superseded_ids:
            state = dict(run.confidence_state or {})
            workspace = dict(state.get("end_to_end") or {})
            if workspace.get("pending_question_id") in superseded_ids:
                workspace.pop("pending_question_id", None)
                workspace.pop("pending_question_type", None)
                workspace.pop("pending_question_reason", None)
                state["end_to_end"] = workspace
                run.confidence_state = state
            emit_event(
                self.db,
                event_type="PAUSE_DIAGNOSTIC_QUESTIONS_SUPERSEDED",
                payload={"question_ids": superseded_ids, "reason": "session_resumed"},
                run_id=run.id,
                application_id=run.application_id,
                actor="runtime",
                confidence=1,
                risk_level="low",
            )
            self.db.flush()
        return superseded_ids

    def _supersede_in_browser_resume_retry_questions(
        self,
        run: models.OperatorRun,
        *,
        retry_state: dict[str, Any],
    ) -> list[int]:
        """Retire only generic cards synthesized after a committed browser-resume handoff failed."""

        superseded_ids: list[int] = []
        retry_status_message = str(retry_state.get("status_message") or "").strip()
        questions = list(
            self.db.scalars(
                select(models.OperatorQuestion).where(
                    models.OperatorQuestion.run_id == run.id,
                    models.OperatorQuestion.status == "pending",
                    models.OperatorQuestion.question_type == "blocked_reason",
                )
            ).all()
        )
        for question in questions:
            schema = (
                question.expected_answer_schema
                if isinstance(question.expected_answer_schema, dict)
                else {}
            )
            if (
                not retry_status_message
                or str(question.stuck_reason or "").strip() != retry_status_message
                or schema != {"answer": "string", "memory_update": "optional object"}
            ):
                continue
            question.status = "superseded"
            question.updated_at = utcnow()
            payload = dict(question.answer_payload or {})
            payload.update(
                {
                    "resolved_by": "in_browser_resume_retry",
                    "resolved_reason": "committed_answer_reconnected",
                }
            )
            question.answer_payload = payload
            superseded_ids.append(int(question.id))

        if superseded_ids:
            state = dict(run.confidence_state or {})
            workspace = dict(state.get("end_to_end") or {})
            if workspace.get("pending_question_id") in superseded_ids:
                workspace.pop("pending_question_id", None)
                workspace.pop("pending_question_type", None)
                workspace.pop("pending_question_reason", None)
                state["end_to_end"] = workspace
                run.confidence_state = state
            emit_event(
                self.db,
                event_type="IN_BROWSER_RESUME_RETRY_QUESTIONS_SUPERSEDED",
                payload={
                    "question_ids": superseded_ids,
                    "retry_source": retry_state.get("source"),
                },
                run_id=run.id,
                application_id=run.application_id,
                actor="runtime",
                confidence=1,
                risk_level="low",
            )
            self.db.flush()
        return superseded_ids

    def _blocked_reason_question_is_stale(self, *, run: models.OperatorRun, question: models.OperatorQuestion) -> bool:
        text = f"{run.status_message or ''} {question.stuck_reason or ''}".lower()
        return any(
            phrase in text
            for phrase in (
                "ready to resume",
                "browser session restart requested",
                "site browser session data was cleared",
                "selected user-provided pdf",
                "lumen is waiting for your answer before execution can resume",
                "parked browser session is not available",
                "target page, context or browser has been closed",
                "browser has been closed",
                "replay budget fix applied",
                "pre-submit validation failed before approval prompt",
                "auto-resolved repeated field approvals",
            )
        )

    def reconcile_orphaned_runs_after_restart(self) -> list[int]:
        """Convert runs stranded RUNNING by a process restart into resumable blocks.

        At process startup no drive coroutine exists, so every RUNNING row is
        definitionally orphaned: its loop died with the previous process while the DB
        row kept claiming live execution. Left alone, such a run occupies its body's
        busy slot forever and renders a live status for work that is not happening.

        Reconciliation makes the discontinuity explicit and safe to resume:
        - the run becomes BLOCKED with a status message that says what happened;
        - any unconsumed pending final action is superseded so stale authorization is
          never replayed (fresh approval required);
        - a runtime_discontinuity marker is stored for the drive loop to surface to
          the model on resume, so the first decision after the gap knows the surface
          may have changed while unobserved.

        The existing resume flow then handles the rest: a fresh observation, never a
        replayed action.
        """
        orphans = self.db.scalars(
            select(models.OperatorRun).where(
                models.OperatorRun.current_state == OperatorState.RUNNING.value
            )
        ).all()
        reconciled: list[int] = []
        now_iso = utcnow().isoformat()
        for run in orphans:
            state = dict(run.confidence_state or {})
            pending = (
                dict(state.get("pending_final_action"))
                if isinstance(state.get("pending_final_action"), dict)
                else {}
            )
            superseded_final_action = False
            if str(pending.get("status") or "").strip().lower() in {"proposed", "approved", "executing"}:
                pending.update(
                    {
                        "status": "superseded",
                        "fresh_approval_required": True,
                        "authorization_consumed": False,
                        "superseded_at": now_iso,
                        "superseded_reason": "runtime_restarted",
                    }
                )
                state["pending_final_action"] = pending
                superseded_final_action = True
            state["runtime_discontinuity"] = {
                "reason": "runtime_restarted",
                "at": now_iso,
                "previous_status_message": str(run.status_message or "")[:300],
            }
            state["system_block"] = True
            state["system_block_reason"] = "runtime_restarted"
            state["block_kind"] = "runtime_restart"
            run.confidence_state = state
            run.status_message = (
                "The Lumen runtime restarted while this run was executing. Nothing was "
                "replayed. Use Resume to reattach with a completely fresh observation."
                + (
                    " The pending final action was superseded and needs fresh approval."
                    if superseded_final_action
                    else ""
                )
            )
            emit_event(
                self.db,
                event_type="RUN_ORPHANED_BY_RUNTIME_RESTART",
                payload={
                    "previous_state": OperatorState.RUNNING.value,
                    "final_action_superseded": superseded_final_action,
                    "in_browser": bool(isinstance(state.get("in_browser"), dict)),
                },
                run_id=run.id,
                application_id=run.application_id,
                actor="runtime",
                confidence=1,
                risk_level="low",
            )
            self._transition(
                run,
                OperatorState.BLOCKED,
                reason="Runtime restarted mid-run; reconciled to a resumable block.",
                actor="runtime",
                confidence=1,
            )
            reconciled.append(run.id)
        if reconciled:
            self.db.commit()
        return reconciled

    def _blocked_run_can_resume(self, run: models.OperatorRun) -> bool:
        return self._blocked_run_resume_block_reason(run) is None

    def _sensitive_browser_takeover_active(self, run: models.OperatorRun) -> bool:
        takeover = (run.confidence_state or {}).get("sensitive_browser_takeover")
        return isinstance(takeover, dict) and bool(takeover.get("active"))

    def _blocked_run_resume_block_reason(self, run: models.OperatorRun) -> str | None:
        """Return the state gate preventing a fresh browser observation, if any.

        Recovery is orchestration, not semantic interpretation. A connected, non-terminal browser
        run may be re-observed when its only pending question is the diagnostic blocker card. Factual
        questions, approvals, and armed final actions remain hard gates. This deliberately relies on
        durable runtime state instead of status-message wording.
        """
        workspace = self._end_to_end_workspace(run)
        if workspace.get("terminal_state") == "application_submitted":
            return "terminal_submission"
        state = dict(run.confidence_state or {})
        in_browser = state.get("in_browser")
        if not isinstance(in_browser, dict) or not in_browser.get("device_id"):
            return "browser_body_not_owned"

        pending_final_action = state.get("pending_final_action") if isinstance(state.get("pending_final_action"), dict) else {}
        pending_final_status = str(pending_final_action.get("status") or "").strip().lower()
        final_action_failed_conclusively = False
        if pending_final_status == "failed":
            try:
                outcome_confidence = float(
                    pending_final_action.get("outcome_confidence") or 0.0
                )
            except (TypeError, ValueError):
                outcome_confidence = 0.0
            final_action_failed_conclusively = (
                str(pending_final_action.get("outcome_status") or "").strip().lower()
                == "failed"
                and outcome_confidence >= 0.75
                and pending_final_action.get("authorization_consumed") is True
            )
        if (
            pending_final_status
            # A superseded action is disarmed by definition: its authorization was never
            # consumed and fresh approval is required before any final action, so it
            # must not bar the fresh observation that recovery needs.
            and pending_final_status not in {"consumed", "declined", "cancelled", "superseded"}
            and not final_action_failed_conclusively
        ):
            return "pending_final_action"
        if workspace.get("submission_lock"):
            return "submission_lock"
        if str(workspace.get("submit_approval_status") or "").strip().lower() in {"pending", "approved"}:
            return "submit_approval_pending"
        if workspace.get("pending_submit_fingerprint") or workspace.get("pending_submit_target"):
            return "pending_submit_target"

        pending_questions = list(
            self.db.scalars(
                select(models.OperatorQuestion).where(
                    models.OperatorQuestion.run_id == run.id,
                    models.OperatorQuestion.status == "pending",
                )
            ).all()
        )
        for question in pending_questions:
            if question.question_type != "blocked_reason":
                return "pending_operator_question"
            schema = question.expected_answer_schema if isinstance(question.expected_answer_schema, dict) else {}
            if schema.get("task_ids") or schema.get("fields"):
                return "pending_field_question"
        return None

    def _reopen_blocked_run_for_recovery(self, run: models.OperatorRun) -> None:
        now = utcnow().isoformat()
        workspace = self._end_to_end_workspace(run)
        workspace.update(
            {
                "phase": "APPLICATION_EXECUTION",
                "run_phase": "filling_application",
                "resume_mode": "rehydrated_verify_existing_fields",
                "pending_action": None,
                "pending_question_id": None,
                "pending_question_type": None,
                "pending_question_reason": None,
                "submit_approval_status": "not_requested",
                "submit_approval_expires_at": None,
                "pending_submit_fingerprint": None,
                "pending_submit_target": None,
                "submission_lock": False,
                "terminal_state": None,
                "terminal_event": None,
                "terminal_payload": None,
                "terminal_reopened_at": now,
                "blocked_recovery_resumed_at": now,
                "blocked_recovery_reason": run.status_message,
                # A reopened run is no longer system-blocked. Stale markers otherwise
                # keep dead diagnostic banners alive in clients until the next finalize.
                "system_block": False,
                "system_block_reason": None,
                "needs_user": False,
                "block_kind": None,
                "phase2_model_failure_reason": None,
            }
        )
        workspace["blocked_recovery_count"] = int(workspace.get("blocked_recovery_count") or 0) + 1
        self._set_end_to_end_workspace(run, workspace)
        run.risk_level = "low"
        run.confidence_state = {
            **(run.confidence_state or {}),
            "mutations_used": 0,
            "run_risk_score": 0.12,
            "run_risk_level": "LOW_RISK",
            "system_block": None,
            "system_block_reason": None,
            "block_kind": None,
            "phase2_model_failure_reason": None,
        }
        for question in self.db.scalars(
            select(models.OperatorQuestion).where(
                models.OperatorQuestion.run_id == run.id,
                models.OperatorQuestion.status == "pending",
                models.OperatorQuestion.question_type.in_(["blocked_reason", "submit_approval"]),
            )
        ).all():
            question.status = "applied"
            question.updated_at = utcnow()
        emit_event(
            self.db,
            event_type="APPLICATION_EXECUTION_RECOVERY_RESUME_STARTED",
            payload={"from_state": "BLOCKED", "resume_mode": workspace["resume_mode"], "reason": workspace.get("blocked_recovery_reason")},
            run_id=run.id,
            application_id=run.application_id,
            actor="runtime",
            confidence=0.82,
            risk_level="medium",
        )
        self._transition(
            run,
            OperatorState.WAITING_FOR_USER,
            reason="Recoverable blocked run reopened for application execution.",
            actor="runtime",
            confidence=0.82,
        )
        in_browser = (run.confidence_state or {}).get("in_browser") if isinstance(run.confidence_state, dict) else None
        if isinstance(in_browser, dict) and in_browser.get("device_id"):
            self._transition(
                run,
                OperatorState.RUNNING,
                reason="Reopened browser recovery is ready for a fresh observation.",
                actor="runtime",
                confidence=0.82,
            )

    def _review_has_field_correction(self, run: models.OperatorRun) -> bool:
        tasks = self.db.scalars(
            select(models.RuntimeTask).where(
                models.RuntimeTask.run_id == run.id,
                models.RuntimeTask.task_type == "field_approval",
                models.RuntimeTask.status == "approved",
            )
        ).all()
        return any(
            bool((task.payload or {}).get("force_reapply") or (task.payload or {}).get("source") == "operator_correction")
            for task in tasks
        )

    def _review_has_unverified_browser_actions(self, run: models.OperatorRun) -> bool:
        state = dict(run.confidence_state or {})
        if not isinstance(state.get("in_browser"), dict):
            return False
        ledger = state.get("vision_session_ledger")
        if not isinstance(ledger, dict):
            return False
        return any(
            isinstance(entry, dict)
            and str(entry.get("last_action") or "") in {"click", "type", "upload"}
            and int(entry.get("attempts") or 0) > 0
            and entry.get("verified_value") is None
            for entry in ledger.values()
        )

    def _review_can_renew_exact_final_action(self, run: models.OperatorRun) -> bool:
        """Allow an explicit resume only when the prior final action provably never activated."""
        state = dict(run.confidence_state or {})
        if not isinstance(state.get("in_browser"), dict):
            return False
        pending = state.get("pending_final_action")
        if not isinstance(pending, dict):
            return False
        if str(pending.get("status") or "") not in {"declined", "failed", "cancelled"}:
            return False
        return (
            pending.get("browser_activation_attempted") is not True
            and pending.get("browser_activation_may_have_occurred") is not True
        )

    def _reopen_review_for_exact_final_action(self, run: models.OperatorRun) -> None:
        """Re-observe a reviewed live page and request a new exact-action approval."""
        workspace = self._end_to_end_workspace(run)
        now = utcnow().isoformat()
        workspace.update(
            {
                "phase": "APPLICATION_EXECUTION",
                "run_phase": "filling_application",
                "resume_mode": "fresh_exact_final_action",
                "pending_action": None,
                "pending_question_id": None,
                "pending_question_type": None,
                "pending_question_reason": None,
                "submit_approval_status": "not_requested",
                "submit_approval_expires_at": None,
                "pending_submit_fingerprint": None,
                "pending_submit_target": None,
                "submission_lock": False,
                "review_reopened_at": now,
                "review_reopen_reason": "fresh_exact_final_action",
            }
        )
        self._set_end_to_end_workspace(run, workspace)
        state = dict(run.confidence_state or {})
        pending = dict(state.get("pending_final_action") or {})
        pending.update(
            {
                "status": "cancelled",
                "cancelled_at": now,
                "cancel_reason": "review_reopened_for_fresh_exact_final_action",
                "fresh_approval_required": True,
            }
        )
        state["pending_final_action"] = pending
        run.confidence_state = state
        for question in self.db.scalars(
            select(models.OperatorQuestion).where(
                models.OperatorQuestion.run_id == run.id,
                models.OperatorQuestion.status == "pending",
                models.OperatorQuestion.question_type.in_(
                    ["submit_approval", "final_action_approval"]
                ),
            )
        ).all():
            question.status = "superseded"
            question.updated_at = utcnow()
        emit_event(
            self.db,
            event_type="APPLICATION_REVIEW_REOPENED_FOR_FRESH_FINAL_ACTION",
            payload={
                "reason": "fresh_exact_final_action",
                "browser_activation_attempted": False,
            },
            run_id=run.id,
            application_id=run.application_id,
            actor="runtime",
            confidence=0.98,
            risk_level="medium",
        )
        self._transition(
            run,
            OperatorState.WAITING_FOR_USER,
            reason="Final review reopened for a fresh exact-action browser observation.",
            actor="human",
            confidence=1,
        )
        self._transition(
            run,
            OperatorState.RUNNING,
            reason="Fresh exact-action approval renewal is ready to re-observe the browser.",
            actor="runtime",
            confidence=0.98,
        )

    def _reopen_review_for_browser_verification(self, run: models.OperatorRun) -> None:
        workspace = self._end_to_end_workspace(run)
        now = utcnow().isoformat()
        workspace.update(
            {
                "phase": "APPLICATION_EXECUTION",
                "run_phase": "filling_application",
                "resume_mode": "rehydrated_verify_existing_fields",
                "pending_action": None,
                "pending_question_id": None,
                "pending_question_type": None,
                "pending_question_reason": None,
                "submit_approval_status": "not_requested",
                "submit_approval_expires_at": None,
                "pending_submit_fingerprint": None,
                "pending_submit_target": None,
                "submission_lock": False,
                "review_reopened_at": now,
                "review_reopen_reason": "unverified_browser_actions",
            }
        )
        self._set_end_to_end_workspace(run, workspace)
        state = dict(run.confidence_state or {})
        pending_final_action = (
            dict(state.get("pending_final_action") or {})
            if isinstance(state.get("pending_final_action"), dict)
            else {}
        )
        if pending_final_action:
            pending_final_action.update(
                {
                    "status": "cancelled",
                    "cancelled_at": now,
                    "cancel_reason": "review_reopened_for_browser_verification",
                }
            )
            state["pending_final_action"] = pending_final_action
            run.confidence_state = state
        for question in self.db.scalars(
            select(models.OperatorQuestion).where(
                models.OperatorQuestion.run_id == run.id,
                models.OperatorQuestion.status == "pending",
                models.OperatorQuestion.question_type.in_(["submit_approval", "final_action_approval"]),
            )
        ).all():
            question.status = "superseded"
            question.updated_at = utcnow()
        emit_event(
            self.db,
            event_type="APPLICATION_REVIEW_REOPENED_FOR_BROWSER_VERIFICATION",
            payload={"reason": "unverified_browser_actions"},
            run_id=run.id,
            application_id=run.application_id,
            actor="runtime",
            confidence=0.95,
            risk_level="medium",
        )
        self._transition(
            run,
            OperatorState.WAITING_FOR_USER,
            reason="Final review reopened to verify attempted browser values.",
            actor="runtime",
            confidence=0.95,
        )
        self._transition(
            run,
            OperatorState.RUNNING,
            reason="Reopened browser verification is ready for a fresh observation.",
            actor="runtime",
            confidence=0.95,
        )

    def _reopen_review_for_field_correction(self, run: models.OperatorRun) -> None:
        workspace = self._end_to_end_workspace(run)
        now = utcnow().isoformat()
        workspace.update(
            {
                "phase": "APPLICATION_EXECUTION",
                "run_phase": "filling_application",
                "resume_mode": "parked_field_recovery",
                "pending_action": None,
                "pending_question_type": None,
                "pending_question_reason": None,
                "submit_approval_status": "not_requested",
                "submit_approval_expires_at": None,
                "pending_submit_fingerprint": None,
                "pending_submit_target": None,
                "submission_lock": False,
                "review_reopened_at": now,
                "review_reopen_reason": "field_correction",
            }
        )
        self._set_end_to_end_workspace(run, workspace)
        for question in self.db.scalars(
            select(models.OperatorQuestion).where(
                models.OperatorQuestion.run_id == run.id,
                models.OperatorQuestion.status == "pending",
                models.OperatorQuestion.question_type == "submit_approval",
            )
        ).all():
            question.status = "applied"
            question.updated_at = utcnow()
        emit_event(
            self.db,
            event_type="APPLICATION_REVIEW_REOPENED_FOR_FIELD_CORRECTION",
            payload={"reason": "field_correction"},
            run_id=run.id,
            application_id=run.application_id,
            actor="runtime",
            confidence=0.9,
            risk_level="medium",
        )
        self._transition(run, OperatorState.FILLING_APPLICATION, reason="Final review reopened to apply corrected field answer.", actor="runtime", confidence=0.9)

    def _question_still_waits_on_pending_task(self, question: models.OperatorQuestion) -> bool:
        task_ids = (question.expected_answer_schema or {}).get("task_ids") or []
        if not task_ids:
            return True
        tasks = list(
            self.db.scalars(
                select(models.RuntimeTask).where(
                    models.RuntimeTask.run_id == question.run_id,
                    models.RuntimeTask.id.in_([int(task_id) for task_id in task_ids]),
                )
            ).all()
        )
        return any(task.status == "pending" for task in tasks)

    def start_human_takeover(self, run_id: int) -> models.OperatorRun:
        run = self._get_run(run_id)
        if run.current_state == OperatorState.READY_FOR_REVIEW.value:
            self._transition(run, OperatorState.FILLING_APPLICATION, reason="Human takeover requested from review.", actor="human", confidence=0.8)
        if run.current_state == OperatorState.FILLING_APPLICATION.value:
            self._transition(run, OperatorState.WAITING_FOR_USER, reason="Human takeover mode started.", actor="human", confidence=0.95)
        run.confidence_state = {**(run.confidence_state or {}), "human_takeover_active": True}
        emit_event(self.db, event_type="HUMAN_TAKEOVER_STARTED", payload={"mode": "observe_only"}, run_id=run.id, actor="human", confidence=1)
        run.status_message = "Human takeover is active. Runtime is paused and observing only."
        self.db.commit()
        return run

    def end_human_takeover(self, run_id: int) -> models.OperatorRun:
        run = self._get_run(run_id)
        run.confidence_state = {**(run.confidence_state or {}), "human_takeover_active": False}
        emit_event(self.db, event_type="HUMAN_TAKEOVER_ENDED", payload={"resume_available": True}, run_id=run.id, actor="human", confidence=1)
        run.status_message = "Human takeover ended. Use Resume to continue controlled execution."
        self.db.commit()
        return run

    def approve_fields(self, *, run_id: int, task_ids: list[int], approved: bool, values: dict[str, str] | None = None) -> models.OperatorRun:
        run = self._get_run(run_id)
        values = values or {}
        stmt = select(models.RuntimeTask).where(models.RuntimeTask.run_id == run.id, models.RuntimeTask.task_type == "field_approval")
        if task_ids:
            stmt = stmt.where(models.RuntimeTask.id.in_(task_ids))
        tasks = list(self.db.scalars(stmt).all())
        for task in tasks:
            task.status = "approved" if approved else "rejected"
            task.payload = {**(task.payload or {}), "approved_value": values.get(str(task.id)), "approved_at": utcnow().isoformat()}
            task.updated_at = utcnow()
        if approved and tasks:
            self._advance_execution_epoch(run, reason="field_approval_answered", task_ids=[task.id for task in tasks])
        emit_event(
            self.db,
            event_type="FIELD_APPROVALS_UPDATED",
            payload={"approved": approved, "task_ids": [task.id for task in tasks]},
            run_id=run.id,
            actor="human",
            confidence=1,
        )
        run.status_message = "Field approvals updated. Use Resume to continue controlled execution."
        self.db.commit()
        return run

    def _field_approval_binding_payload(
        self,
        run: models.OperatorRun,
        *,
        field: dict[str, Any],
        field_key: str,
        label: str,
        is_choice: bool,
        options: list[str],
        option_refs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        target = field.get("target") if isinstance(field.get("target"), dict) else {}
        raw_field_ref = field.get("field_ref") if isinstance(field.get("field_ref"), dict) else {}
        raw_element_ref = field.get("element_ref") if isinstance(field.get("element_ref"), dict) else {}
        locator_ref = field.get("locator_ref") if isinstance(field.get("locator_ref"), dict) else {}
        snapshot_id = str(field.get("snapshot_id") or target.get("snapshot_id") or raw_field_ref.get("snapshot_id") or raw_element_ref.get("snapshot_id") or "").strip()
        node_id = str(field.get("node_id") or target.get("node_id") or raw_field_ref.get("node_id") or raw_element_ref.get("node_id") or field_key).strip()
        selector = str(
            field.get("selector")
            or target.get("selector")
            or raw_element_ref.get("selector")
            or raw_field_ref.get("selector")
            or locator_ref.get("selector")
            or ""
        ).strip()
        observation_id = str(field.get("observation_id") or snapshot_id).strip()
        control_kind = str(field.get("control_kind") or field.get("role") or field.get("input_type") or ("choice" if is_choice else "field")).strip()
        field_ref = {
            "run_id": run.id,
            "field_key": field_key,
            "node_id": node_id,
            "snapshot_id": snapshot_id,
            "selector": selector,
            "label": label,
        }
        element_ref = {
            **({key: value for key, value in raw_element_ref.items() if value} if raw_element_ref else {}),
            "node_id": node_id,
            "snapshot_id": snapshot_id,
            "selector": selector,
            "field_key": field_key,
            "label": label,
            "role": control_kind,
        }
        visible_options, matched_option_refs, options_status = self._field_bound_visible_options(
            options,
            option_refs,
            field_ref=field_ref,
            element_ref=element_ref,
        )
        group_fingerprint = self._choice_group_fingerprint(
            field_ref=field_ref,
            element_ref=element_ref,
            control_kind=control_kind,
            options=visible_options,
        )
        binding_key_parts = [
            str(run.id),
            snapshot_id,
            node_id,
            selector,
            field_key,
            label,
            control_kind,
            "|".join(visible_options),
        ]
        binding_key = "field:" + hashlib.sha1("\u241f".join(binding_key_parts).encode("utf-8")).hexdigest()[:16]
        choice_snapshot = self._choice_option_snapshot(
            field_ref=field_ref,
            element_ref=element_ref,
            control_kind=control_kind,
            options=visible_options,
            option_refs=matched_option_refs,
            group_fingerprint=group_fingerprint,
            graph_version=snapshot_id or observation_id,
            source=str(field.get("choice_snapshot_source") or field.get("source") or "field_approval_binding"),
        )
        return {
            "run_id": run.id,
            "field_ref": field_ref,
            "element_ref": element_ref,
            "observed_label": label,
            "control_kind": control_kind,
            "visible_options": visible_options,
            "option_refs": matched_option_refs,
            "options_binding_status": options_status,
            "observation_id": observation_id,
            "snapshot_id": snapshot_id,
            "field_id": field_key,
            "group_fingerprint": group_fingerprint,
            "choice_option_snapshot": choice_snapshot,
            "option_snapshot_id": choice_snapshot.get("snapshot_id") if choice_snapshot else None,
            "created_at": utcnow().isoformat(),
            "binding_key": binding_key,
            "requires_fresh_observation": options_status == "mismatched_reobserve_required",
        }

    def _field_approval_answer_mode_payload(
        self,
        run: models.OperatorRun,
        *,
        field: dict[str, Any],
        binding: dict[str, Any],
        is_choice: bool,
        visible_options: list[str],
    ) -> dict[str, Any]:
        from app.form_graph import legacy_required, requiredness

        control_kind = str(binding.get("control_kind") or field.get("control_kind") or field.get("role") or field.get("input_type") or "").strip().lower()
        req = requiredness(field)
        required_projection = legacy_required(req)
        modes: list[str] = []
        if is_choice and visible_options:
            modes.append("choose_option")
        input_constraints = field.get("input_constraints") if isinstance(field.get("input_constraints"), dict) else {}
        accepted = field.get("accepted_file_extensions") or input_constraints.get("accepted_file_extensions") or input_constraints.get("accept") or []
        if isinstance(accepted, str):
            accepted_extensions = [item.strip().lower().lstrip(".") for item in re.split(r"[,;\s]+", accepted) if item.strip()]
        else:
            accepted_extensions = [str(item).strip().lower().lstrip(".") for item in accepted if str(item).strip()]
        classifier_payload = {
            "run_id": run.id,
            "field_ref": binding.get("field_ref") or {},
            "element_ref": binding.get("element_ref") or {},
            "observed_label": binding.get("observed_label") or field.get("label") or "",
            "control_kind": control_kind or "unknown",
            "role": field.get("role"),
            "input_type": field.get("input_type") or field.get("type"),
            "required": required_projection,
            "requiredness": req,
            "legacy_required_projection": required_projection,
            "is_choice": bool(is_choice),
            "visible_options": visible_options[:12],
            "options_binding_status": binding.get("options_binding_status"),
            "option_snapshot_id": binding.get("option_snapshot_id"),
            "input_constraints": input_constraints,
            "accepted_file_extensions": accepted_extensions,
            "upload_file_type": field.get("upload_file_type"),
            "document_kind": field.get("document_kind"),
            "artifact_policy": {"cover_letter": "ask_before_generate_or_upload"},
        }
        try:
            capability = self.lumen_model.classify_document_field_capability(classifier_payload)
        except Exception:  # noqa: BLE001 - capability hints must not break pending questions
            capability = {}
        capability = capability if isinstance(capability, dict) else {}
        document_role = self._document_artifact_role(capability, field=field)
        is_document = bool(capability.get("is_document_request") or capability.get("is_document_field")) and document_role != "none" and float(capability.get("confidence") or 0.0) >= 0.5
        if is_document:
            document_modes = self._build_document_answer_modes(control_kind=control_kind, artifact_role=document_role, requiredness=req, run=run, capability=capability)
            modes.extend(document_modes)
        if not (is_document and control_kind in {"file_input", "upload_zone", "file"}) and not (is_choice and not visible_options):
            modes.append("reply_text")
        if is_document:
            capability = {
                **capability,
                "is_document_request": True,
                "document_kind": document_role,
                "artifact_role": document_role,
                "allow_upload": "upload_file" in modes,
                "allow_generate": "generate_artifact" in modes,
                "allow_existing_artifact": "choose_artifact" in modes,
            }
            emit_event(
                self.db,
                event_type="DOCUMENT_CLASSIFICATION",
                payload={"label": classifier_payload.get("observed_label"), "control_kind": control_kind, "artifact_role": document_role, "confidence": capability.get("confidence"), "evidence": capability.get("evidence") or capability.get("rationale")},
                run_id=run.id,
                actor="runtime",
                confidence=float(capability.get("confidence") or 0.5),
            )
            emit_event(
                self.db,
                event_type="DOCUMENT_CAPABILITY",
                payload={
                    "label": classifier_payload.get("observed_label"),
                    "control_kind": control_kind,
                    "artifact_role": document_role,
                    "accepted_file_extensions": accepted_extensions,
                    "allow_upload": capability.get("allow_upload"),
                    "allow_generate": capability.get("allow_generate"),
                    "allow_existing_artifact": capability.get("allow_existing_artifact"),
                    "artifact_policy": classifier_payload.get("artifact_policy"),
                },
                run_id=run.id,
                actor="runtime",
                confidence=float(capability.get("confidence") or 0.5),
            )
        constrained_control_without_options = bool(
            is_choice
            and not visible_options
            and control_kind in {"combobox", "select", "select_option", "dropdown", "choice", "radio", "radio_group", "checkbox", "checkbox_group"}
        )
        # Diagnostic: WHY a document field did/didn't get upload/generate/choose modes. Surfaces the
        # classifier inputs (label + the control_kind the body reported) and its verdict, so a cover
        # letter that stayed a plain text box is traceable from the backend log instead of guessed at.
        logger.info(
            "field_answer_modes label=%r control_kind=%r is_document=%s doc_kind=%s confidence=%.2f modes=%s",
            classifier_payload.get("observed_label"),
            control_kind or "unknown",
            is_document,
            (capability or {}).get("document_kind"),
            float((capability or {}).get("confidence") or 0.0),
            list(dict.fromkeys(modes)),
        )
        deduped_modes = list(dict.fromkeys(modes))
        emit_event(
            self.db,
            event_type="ANSWER_MODES",
            payload={
                "label": classifier_payload.get("observed_label"),
                "control_kind": control_kind,
                "is_document": is_document,
                "artifact_role": document_role if is_document else None,
                "answer_modes": deduped_modes,
                "requiredness": req,
                "capability_error": "DOCUMENT_FIELD_HAS_NO_ARTIFACT_MODE" if is_document and required_projection and not deduped_modes else None,
            },
            run_id=run.id,
            actor="runtime",
            confidence=0.86 if is_document else 0.7,
        )
        return {
            "answer_modes": deduped_modes,
            "document_capability": capability if is_document else None,
            "accepted_file_extensions": accepted_extensions,
            "needs_option_observation": constrained_control_without_options,
            "requires_fresh_observation": constrained_control_without_options,
        }

    def _document_artifact_role(self, capability: dict[str, Any], *, field: dict[str, Any]) -> str:
        role = normalize_artifact_role(capability.get("artifact_role") or capability.get("document_kind") or field.get("upload_file_type") or field.get("document_kind") or "", default="")
        return role or "none"

    def _has_active_artifact(self, *, run: models.OperatorRun, artifact_role: str) -> bool:
        if not artifact_role or artifact_role == "none":
            return False
        return bool(
            self.db.scalars(
                select(models.ApplicationArtifact.id)
                .where(
                    models.ApplicationArtifact.user_id == run.user_id,
                    models.ApplicationArtifact.status != "deleted",
                    models.ApplicationArtifact.artifact_kind == artifact_role,
                )
                .limit(1)
            ).first()
        )

    def _build_document_answer_modes(
        self,
        *,
        control_kind: str,
        artifact_role: str,
        requiredness: dict[str, Any],
        run: models.OperatorRun,
        capability: dict[str, Any],
    ) -> list[str]:
        modes: list[str] = []
        is_file_control = control_kind in {"file_input", "upload_zone", "file"}
        if is_file_control or capability.get("allow_upload"):
            modes.append("upload_file")
        if artifact_role == "cover_letter":
            modes.append("generate_artifact")
        if self._has_active_artifact(run=run, artifact_role=artifact_role):
            modes.append("choose_artifact")
        state = str((requiredness or {}).get("state") or "")
        if state.startswith("optional"):
            modes.append("skip_optional")
        return list(dict.fromkeys(modes))

    def _field_bound_visible_options(
        self,
        options: list[str],
        option_refs: list[dict[str, Any]],
        *,
        field_ref: dict[str, Any],
        element_ref: dict[str, Any],
    ) -> tuple[list[str], list[dict[str, Any]], str]:
        cleaned_options = [str(option).strip() for option in options if str(option).strip()]
        refs = [ref for ref in option_refs if isinstance(ref, dict)]
        if not refs:
            return cleaned_options, [], "legacy_unowned" if cleaned_options else "none"
        matched_refs: list[dict[str, Any]] = []
        matched_options: list[str] = []
        explicit_mismatch = False
        for ref in refs:
            label = str(ref.get("label") or ref.get("text") or ref.get("value") or "").strip()
            if not label:
                continue
            matches, explicit_owner = self._option_ref_matches_field(ref, field_ref=field_ref, element_ref=element_ref)
            if matches:
                normalized_ref = {**ref, "label": label}
                matched_refs.append(normalized_ref)
                if label not in matched_options:
                    matched_options.append(label)
            elif explicit_owner:
                explicit_mismatch = True
        if matched_options:
            return matched_options[:40], matched_refs[:40], "matched"
        if explicit_mismatch:
            return [], [], "mismatched_reobserve_required"
        return cleaned_options[:40], refs[:40], "unowned_refs"

    def _choice_group_fingerprint(
        self,
        *,
        field_ref: dict[str, Any],
        element_ref: dict[str, Any],
        control_kind: str,
        options: list[str],
    ) -> str:
        identity_parts = [
            str(field_ref.get("field_key") or ""),
            str(field_ref.get("node_id") or element_ref.get("node_id") or ""),
            str(field_ref.get("selector") or element_ref.get("selector") or ""),
            str(field_ref.get("label") or element_ref.get("label") or ""),
            str(element_ref.get("name") or ""),
            str(control_kind or ""),
            "|".join(str(option).strip().casefold() for option in options if str(option).strip()),
        ]
        return "choice_group:" + hashlib.sha1("\u241f".join(identity_parts).encode("utf-8")).hexdigest()[:16]

    def _choice_option_snapshot(
        self,
        *,
        field_ref: dict[str, Any],
        element_ref: dict[str, Any],
        control_kind: str,
        options: list[str],
        option_refs: list[dict[str, Any]],
        group_fingerprint: str,
        graph_version: str,
        source: str,
    ) -> dict[str, Any] | None:
        cleaned_options = [str(option).strip() for option in options if str(option).strip()]
        if not cleaned_options:
            return None
        refs_by_label = {str(ref.get("label") or ref.get("text") or ref.get("value") or "").strip().casefold(): ref for ref in option_refs if isinstance(ref, dict)}
        field_id = str(field_ref.get("field_key") or field_ref.get("node_id") or element_ref.get("node_id") or "").strip()
        option_payloads: list[dict[str, Any]] = []
        for index, label in enumerate(cleaned_options, start=1):
            ref = refs_by_label.get(label.casefold()) or {}
            raw_option_id = str(ref.get("option_id") or ref.get("node_id") or ref.get("id") or ref.get("value") or "").strip()
            option_id = raw_option_id or f"{group_fingerprint}:option:{index}"
            option_payloads.append(
                {
                    "option_id": option_id,
                    "label": label,
                    "checked": bool(ref.get("checked")) if "checked" in ref else None,
                    "disabled": bool(ref.get("disabled")) if "disabled" in ref else None,
                }
            )
        snapshot_basis = [
            field_id,
            group_fingerprint,
            str(control_kind or ""),
            str(graph_version or ""),
            "|".join(item["label"] for item in option_payloads),
        ]
        snapshot_id = "choice_snapshot:" + hashlib.sha1("\u241f".join(snapshot_basis).encode("utf-8")).hexdigest()[:16]
        ownership_evidence = [
            f"field_id:{field_id}" if field_id else "",
            f"group_fingerprint:{group_fingerprint}",
            f"control_kind:{control_kind}" if control_kind else "",
            f"graph_version:{graph_version}" if graph_version else "",
        ]
        return {
            "snapshot_id": snapshot_id,
            "field_id": field_id,
            "group_fingerprint": group_fingerprint,
            "control_kind": str(control_kind or "choice"),
            "options": option_payloads,
            "ownership_evidence": [item for item in ownership_evidence if item],
            "source": source or "field_approval_binding",
            "graph_version": graph_version or "",
        }

    def _option_ref_matches_field(
        self,
        ref: dict[str, Any],
        *,
        field_ref: dict[str, Any],
        element_ref: dict[str, Any],
    ) -> tuple[bool, bool]:
        owner_refs = []
        for key in ("owner_ref", "owner_field_ref", "field_ref", "control_ref", "element_ref"):
            value = ref.get(key)
            if isinstance(value, dict):
                owner_refs.append(value)
        flat_owner = {
            key: ref.get(key)
            for key in ("node_id", "owner_node_id", "control_node_id", "field_key", "selector", "snapshot_id", "aria_controls", "aria_owns", "list")
            if ref.get(key)
        }
        if flat_owner:
            owner_refs.append(flat_owner)
        if not owner_refs:
            ref_snapshot = str(ref.get("snapshot_id") or "").strip()
            field_snapshot = str(field_ref.get("snapshot_id") or element_ref.get("snapshot_id") or "").strip()
            if ref_snapshot and field_snapshot and ref_snapshot != field_snapshot:
                return False, True
            return True, False
        for owner in owner_refs:
            if self._field_refs_overlap(owner, field_ref, element_ref):
                return True, True
        return False, True

    def _field_refs_overlap(self, owner: dict[str, Any], field_ref: dict[str, Any], element_ref: dict[str, Any]) -> bool:
        snapshot = str(owner.get("snapshot_id") or "").strip()
        field_snapshot = str(field_ref.get("snapshot_id") or element_ref.get("snapshot_id") or "").strip()
        if snapshot and field_snapshot and snapshot != field_snapshot:
            return False
        comparable_pairs = (
            ("node_id", "node_id"),
            ("owner_node_id", "node_id"),
            ("control_node_id", "node_id"),
            ("field_key", "field_key"),
            ("selector", "selector"),
            ("aria_controls", "aria_controls"),
            ("aria_owns", "aria_owns"),
            ("list", "list"),
            ("name", "name"),
        )
        candidates = {**field_ref, **element_ref}
        for owner_key, field_key in comparable_pairs:
            owner_value = str(owner.get(owner_key) or "").strip()
            field_value = str(candidates.get(field_key) or "").strip()
            if owner_value and field_value and owner_value == field_value:
                return True
        field_node = str(candidates.get("node_id") or "").strip()
        owner_nodes = [
            str(owner.get(key) or "").strip()
            for key in ("node_id", "owner_node_id", "control_node_id")
            if str(owner.get(key) or "").strip()
        ]
        if field_node.startswith(("radiogroup:", "checkboxgroup:")):
            member_ids = {part for part in field_node.split(":", 1)[1].split("|") if part}
            if any(owner_node in member_ids for owner_node in owner_nodes):
                return True
        return False

    def _phase2_upsert_required_field_approval_task(self, run: models.OperatorRun, *, field: dict[str, Any], source: str) -> int:
        from app.form_graph import legacy_required, requiredness

        is_choice = bool(field.get("is_choice"))
        req = requiredness(field)
        required = legacy_required(req)
        options = [str(option).strip() for option in field.get("options") or [] if str(option).strip()]
        label = str(field.get("label") or field.get("node_label") or "this field")
        field_key = str(field.get("node_id") or label or source)
        binding = self._field_approval_binding_payload(
            run,
            field=field,
            field_key=field_key,
            label=label,
            is_choice=is_choice,
            options=options,
            option_refs=list(field.get("option_refs") or []),
        )
        visible_options = list(binding.get("visible_options") or [])
        mode_payload = self._field_approval_answer_mode_payload(
            run,
            field=field,
            binding=binding,
            is_choice=is_choice,
            visible_options=visible_options,
        )
        multiple = is_choice and bool(field.get("multiple"))
        question_text = self._field_approval_question_text(
            label, options=visible_options, required=required, is_choice=is_choice, multiple=multiple
        )
        payload = {
            "field_key": field_key,
            "label": label,
            "target": {"node_id": field.get("node_id"), "snapshot_id": field.get("snapshot_id")},
            "value_category": "option" if is_choice else "field_value",
            "confidence": 0.0,
            "source": source,
            "options": visible_options,
            "visible_options": visible_options,
            "option_refs": list(binding.get("option_refs") or []),
            "field_ref": binding.get("field_ref") or {},
            "element_ref": binding.get("element_ref") or {},
            "observed_label": binding.get("observed_label") or label,
            "control_kind": binding.get("control_kind") or "",
            "observation_id": binding.get("observation_id") or "",
            "snapshot_id": binding.get("snapshot_id") or "",
            "field_binding": binding,
            "binding_key": binding.get("binding_key"),
            "options_binding_status": binding.get("options_binding_status"),
            "choice_option_snapshot": binding.get("choice_option_snapshot"),
            "option_snapshot_id": binding.get("option_snapshot_id"),
            "group_fingerprint": binding.get("group_fingerprint"),
            "field_id": binding.get("field_id"),
            "requires_fresh_observation": bool(binding.get("requires_fresh_observation") or mode_payload.get("requires_fresh_observation")),
            "needs_option_observation": bool(mode_payload.get("needs_option_observation")),
            "answer_modes": mode_payload.get("answer_modes") or ([] if is_choice and not visible_options else ["reply_text"]),
            "choice_question_state": "ready" if not is_choice or visible_options else "reobserve_or_hydrate_required",
            "document_capability": mode_payload.get("document_capability"),
            "accepted_file_extensions": mode_payload.get("accepted_file_extensions") or [],
            "is_choice": is_choice,
            "multiple": multiple,
            "stored_answer": "",
            "value_source": None,
            "question_text": question_text,
            "allowed_sources": list(field.get("allowed_sources") or []),
            "attempted_source": field.get("attempted_source"),
            "required": required,
            "requiredness": req,
            "legacy_required_projection": required,
            "legacy_required_projection_source": "requiredness_object",
            # Confirm-ask: the agent's best guess to surface for Keep/Change when it's unsure.
            "proposed_answer": str(field.get("proposed_answer") or ""),
            "proposed_option": str(field.get("proposed_option") or ""),
            "display_reason": str(field.get("display_reason") or "")[:500],
            "confirmation_required": bool(field.get("confirmation_required")),
            "evidence_coverage": (
                dict(field.get("evidence_coverage") or {})
                if isinstance(field.get("evidence_coverage"), dict)
                else None
            ),
            "is_unsure": bool(field.get("is_unsure")),
        }
        existing = self.db.scalars(
            select(models.RuntimeTask).where(
                models.RuntimeTask.run_id == run.id,
                models.RuntimeTask.status.in_(["pending", "approved"]),
                models.RuntimeTask.task_type == "field_approval",
            )
        ).all()
        same_field = [task for task in existing if str((task.payload or {}).get("field_key") or "") == field_key]
        pending = next((task for task in same_field if task.status == "pending"), None)
        if pending is not None:
            pending.payload = {**(pending.payload or {}), **payload, "updated_at": utcnow().isoformat()}
            pending.updated_at = utcnow()
            return int(pending.id)
        # An approved task for this field answered an earlier ask. The brain asking again means the field
        # still needs an answer (run 1791: one pick of a "Select 3" group), so this ask gets its own pending
        # card with its own options. The approved task keeps its status: human_answers reads approved tasks,
        # and the answer it holds must keep reaching the brain.
        approved = next((task for task in same_field if task.status == "approved"), None)
        if approved is not None:
            payload["supersedes_task_id"] = int(approved.id)
        task = models.RuntimeTask(
            run_id=run.id,
            name=f"Answer required field: {label}",
            status="pending",
            task_type="field_approval",
            payload=payload,
            max_attempts=1,
        )
        self.db.add(task)
        self.db.flush()
        return int(task.id)

    def _field_approval_question_text(
        self,
        label: str,
        *,
        options: list[str] | None = None,
        required: bool = True,
        is_choice: bool = False,
        multiple: bool = False,
    ) -> str:
        cleaned = re.sub(r"\s+", " ", str(label or "")).strip()
        opts = [re.sub(r"\s+", " ", str(o)).strip() for o in (options or []) if str(o).strip()]
        if is_choice and opts:
            opt_text = "; ".join(f"[{o}]" for o in opts)
            kind = "multi-select" if multiple else "single-choice"
            head = f'The field "{cleaned}"' if cleaned and cleaned.lower() != "this field" else f"This application has a {kind} field that"
            ask = "Which options should I select? Pick one or more." if multiple else "Which option should I select?"
            if required:
                return f"{head} is required to continue. Options: {opt_text}. {ask}"
            return f"{head} is optional. Options: {opt_text}. {ask} You can also tell me to skip it."
        cleaned = cleaned or "this field"
        if not required:
            return f"This posting asks: {cleaned} (optional). I don't have a stored answer. Provide one, or tell me to skip it."
        return f"This posting asks: {cleaned}. I don't have a stored answer for it."

    def _end_to_end_workspace(self, run: models.OperatorRun) -> dict:
        return dict((run.confidence_state or {}).get("end_to_end") or {})

    def _set_end_to_end_workspace(self, run: models.OperatorRun, workspace: dict) -> None:
        run.confidence_state = {**(run.confidence_state or {}), "end_to_end": workspace}
        run.updated_at = utcnow()
        self.db.flush()

    def _advance_execution_epoch(self, run: models.OperatorRun, *, reason: str, task_ids: list[int] | None = None) -> int:
        workspace = self._end_to_end_workspace(run)
        current_epoch = int(workspace.get("execution_epoch") or 0)
        next_epoch = current_epoch + 1
        workspace.update(
            {
                "execution_epoch": next_epoch,
                "last_execution_epoch_reason": reason,
                "last_execution_epoch_task_ids": task_ids or [],
                "last_execution_epoch_at": utcnow().isoformat(),
            }
        )
        if reason == "field_approval_answered":
            workspace.pop("pending_question_id", None)
            workspace.pop("pending_question_type", None)
            workspace.pop("pending_question_reason", None)
            workspace.update(
                {
                    "requires_fresh_observation": True,
                    "requires_fresh_observation_reason": reason,
                    "fresh_observation_after_task_ids": task_ids or [],
                    "fresh_observation_requested_at": utcnow().isoformat(),
                }
            )
        self._set_end_to_end_workspace(run, workspace)
        run.confidence_state = {
            **(run.confidence_state or {}),
            "mutations_used": 0,
            "run_risk_score": 0.12,
            "run_risk_level": "LOW_RISK",
        }
        run.risk_level = "low"
        emit_event(
            self.db,
            event_type="EXECUTION_EPOCH_ADVANCED",
            payload={
                "from": current_epoch,
                "to": next_epoch,
                "reason": reason,
                "task_ids": task_ids or [],
                "mutation_budget_reset": True,
                "requires_fresh_observation": reason == "field_approval_answered",
            },
            run_id=run.id,
            application_id=run.application_id,
            actor="runtime",
            confidence=0.9,
            risk_level="low",
        )
        return next_epoch

    def _is_placeholder_email(self, email: str | None) -> bool:
        normalized = str(email or "").strip().lower()
        return not normalized or normalized in {"jordan@example.com", "test@example.com", "user@example.com"} or normalized.endswith("@example.com")

    def _transition(self, run: models.OperatorRun, to_state: OperatorState, *, reason: str, actor: str, confidence: float) -> None:
        from_state = run.current_state
        assert_transition(from_state, to_state.value)
        run.current_state = to_state.value
        if to_state == OperatorState.READY_FOR_REVIEW:
            run.status_message = "Application is ready for final review. Lumen has not clicked final submit."
        run.updated_at = utcnow()
        emit_event(
            self.db,
            event_type="RUN_STATE_CHANGED",
            payload={"from_state": from_state, "to_state": to_state.value, "reason": reason},
            run_id=run.id,
            application_id=run.application_id,
            actor=actor,
            confidence=confidence,
            risk_level=run.risk_level,
        )
        self.db.flush()

    def _get_run(self, run_id: int) -> models.OperatorRun:
        run = self.db.get(models.OperatorRun, run_id)
        if not run:
            raise ValueError(f"Operator run {run_id} was not found")
        return run

    def _profile_payload(self, user_id: int) -> dict:
        profile = self.db.scalars(select(models.UserProfile).where(models.UserProfile.user_id == user_id)).first()
        if not profile:
            return {}
        payload = {
            **(profile.profile_json or {}),
            "phone": profile.phone,
            "location": profile.location,
            "linkedin": profile.linkedin,
            "github": profile.github,
            "portfolio": profile.portfolio,
            "work_authorization": profile.work_authorization,
            "sponsorship_status": profile.sponsorship_status,
            "salary_expectation": profile.salary_expectation,
            "relocation_preference": profile.relocation_preference,
        }
        payload.update(self._profile_facts(user_id))
        return payload

    def _profile_facts(self, user_id: int) -> dict:
        profile = self.db.scalars(select(models.UserProfile).where(models.UserProfile.user_id == user_id)).first()
        profile_json = dict(profile.profile_json or {}) if profile else {}
        known_employers = list(profile_json.get("known_employers") or [])
        known_titles = list(profile_json.get("known_titles") or [])
        base_resume = active_base_resume(self.db, user_id)
        resume_text = base_resume.base_resume_text if base_resume else ""
        if resume_text:
            for employer in self._extract_known_employers(resume_text):
                if employer not in known_employers:
                    known_employers.append(employer)
        return {
            "known_employers": known_employers,
            "known_titles": known_titles,
            "work_authorization_facts": {
                "work_authorization": profile.work_authorization if profile else None,
                "sponsorship_status": profile.sponsorship_status if profile else None,
            },
        }

    def _extract_known_employers(self, resume_text: str) -> list[str]:
        candidates = [
            "Spectrum Capital",
            "New Jersey Institute of Technology",
            "Cognier Insights",
            "Kommuri Pratap Reddy Institute Of Technology",
            "Affirm",
        ]
        text = resume_text.lower()
        return [candidate for candidate in candidates if candidate.lower() in text]
