from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import desktop_platform
from app import models, totp as totp_module, user_memory
from app.artifact_naming import normalize_artifact_role
from app.lumen_answer_interpreter import SKIP_ANSWER_TEXT, LumenAnswerInterpreter, InterpretedAnswer
from app.answer_normalization import classify_field, normalize_answer_key, normalize_answer_value
from app.lumen_model import LumenModelClient
from app.config import get_settings
from app.event_store import emit_event
from app.models import utcnow
from app.operation_receipt import make_receipt
from app.resume_selection import active_base_resume
from app.run_lifecycle import run_lifecycle_guard, serialize_run_lifecycle_row
from app.security import redact_otp_codes

logger = logging.getLogger(__name__)


TERMINAL_RUN_STATES = {"CANCELLED", "COMPLETED", "FAILED"}
STATE_EXCLUSIVE_QUESTION_TYPES = {"blocked_reason", "final_action_approval"}


class SensitiveBrowserTakeoverChatBlocked(ValueError):
    """Raised before persistence when chat is unavailable during direct secret entry."""


class OperatorChatService:
    def __init__(self, db: Session):
        self.db = db
        self.settings = get_settings()
        self.answer_interpreter = LumenAnswerInterpreter()
        self.lumen_model = LumenModelClient(db)

    def create_thread(self, *, user_id: int, run_id: int | None = None, title: str | None = None) -> models.ChatThread:
        if run_id:
            existing = self.db.scalars(
                select(models.ChatThread).where(models.ChatThread.run_id == run_id, models.ChatThread.status == "active").order_by(models.ChatThread.id.desc())
            ).first()
            if existing:
                return existing
        thread = models.ChatThread(
            user_id=user_id,
            run_id=run_id,
            title=title or ("Lumen run chat" if run_id else _DEFAULT_CHAT_TITLE),
            mode="run" if run_id else "global",
        )
        self.db.add(thread)
        self.db.flush()
        emit_event(self.db, event_type="OPERATOR_CHAT_THREAD_CREATED", payload={"thread_id": thread.id, "run_id": run_id, "mode": thread.mode}, run_id=run_id, actor="operator_chat", confidence=0.9)
        return thread

    def list_threads(self, *, user_id: int, run_id: int | None = None) -> list[models.ChatThread]:
        stmt = (
            select(models.ChatThread)
            .where(models.ChatThread.user_id == user_id, models.ChatThread.status == "active")
            .order_by(models.ChatThread.updated_at.desc(), models.ChatThread.id.desc())
            .limit(30)
        )
        if run_id is not None:
            stmt = (
                select(models.ChatThread)
                .where(models.ChatThread.user_id == user_id, models.ChatThread.run_id == run_id, models.ChatThread.status == "active")
                .order_by(models.ChatThread.updated_at.desc(), models.ChatThread.id.desc())
                .limit(30)
            )
        threads = list(self.db.scalars(stmt).all())
        # A thread can carry more than one run over time (its run_id is only the latest): its spend is
        # every run its messages name, plus the current one.
        thread_runs: dict[int, set[int]] = {thread.id: ({int(thread.run_id)} if thread.run_id else set()) for thread in threads}
        if thread_runs:
            for thread_id, payload in self.db.execute(
                select(models.ChatMessage.thread_id, models.ChatMessage.payload_json).where(
                    models.ChatMessage.thread_id.in_(list(thread_runs))
                )
            ).all():
                run_ref = payload.get("run_id") if isinstance(payload, dict) else None
                if str(run_ref or "").isdigit():
                    thread_runs[int(thread_id)].add(int(run_ref))
        run_ids = set().union(*thread_runs.values()) if thread_runs else set()
        costs = (
            dict(
                self.db.execute(
                    select(models.OperatorRun.id, models.OperatorRun.cost_estimate).where(models.OperatorRun.id.in_(run_ids))
                ).all()
            )
            if run_ids
            else {}
        )
        for thread in threads:
            # Not a mapped column: the thread's model spend, read by ChatThreadOut.
            runs = thread_runs.get(thread.id) or set()
            thread.cost_usd = round(sum(float(costs.get(run) or 0.0) for run in runs), 4) if runs else None
        return threads

    def list_messages(self, thread_id: int) -> list[models.ChatMessage]:
        self._thread(thread_id)
        return list(self.db.scalars(select(models.ChatMessage).where(models.ChatMessage.thread_id == thread_id).order_by(models.ChatMessage.id.asc())).all())

    def assert_chat_message_allowed(
        self,
        thread_id: int,
        *,
        thread: models.ChatThread | None = None,
    ) -> models.ChatThread:
        """Fail before ChatMessage creation while direct sensitive browser entry is active."""

        resolved_thread = thread or self._thread(thread_id)
        # The guard is user-wide, not thread-local. A user can open a new/global/history thread
        # while the same Chrome body is waiting, and that must not become an alternate persistence
        # path for a mistakenly pasted secret.
        user_runs = self.db.scalars(
            select(models.OperatorRun).where(models.OperatorRun.user_id == resolved_thread.user_id)
        ).all()
        for run in user_runs:
            if str(run.current_state or "").upper() in {"CANCELLED", "COMPLETED", "FAILED"}:
                continue
            state = dict(run.confidence_state or {})
            takeover = (
                dict(state.get("sensitive_browser_takeover") or {})
                if isinstance(state.get("sensitive_browser_takeover"), dict)
                else {}
            )
            if takeover.get("active"):
                raise SensitiveBrowserTakeoverChatBlocked(
                    "Chat is temporarily disabled while sensitive entry is active. Enter the value directly "
                    "in Chrome, then use Continue after entering in Chrome. Do not paste it into chat."
                )
        return resolved_thread

    def delete_thread(self, *, user_id: int, thread_id: int) -> models.ChatThread:
        thread = self.db.get(models.ChatThread, thread_id)
        if not thread or thread.user_id != user_id or thread.status != "active":
            raise ValueError("Chat thread not found")

        for question in self.db.scalars(select(models.OperatorQuestion).where(models.OperatorQuestion.thread_id == thread.id)).all():
            question.thread_id = None
            question.updated_at = utcnow()

        thread.status = "deleted"
        thread.updated_at = utcnow()
        emit_event(
            self.db,
            event_type="OPERATOR_CHAT_THREAD_DELETED",
            payload={"thread_id": thread.id, "run_id": thread.run_id},
            run_id=thread.run_id,
            actor="operator_chat",
            confidence=1,
            risk_level="low",
        )
        self.db.flush()
        return thread

    def add_run_runtime_message(self, *, run_id: int, content: str, reason: str, payload: dict[str, Any] | None = None) -> models.ChatMessage:
        run = self.db.get(models.OperatorRun, run_id)
        if not run:
            raise ValueError("Run not found")
        thread = self.create_thread(user_id=run.user_id, run_id=run.id)
        message = self._add_message(
            thread,
            role="assistant",
            content=content,
            payload={
                "source": "runtime_control",
                "reason": reason,
                "run_state": run.current_state,
                **(payload or {}),
            },
        )
        emit_event(
            self.db,
            event_type="LUMEN_CHAT_RUNTIME_MESSAGE_CREATED",
            payload={"thread_id": thread.id, "assistant_message_id": message.id, "reason": reason, "run_state": run.current_state},
            run_id=run.id,
            application_id=run.application_id,
            actor="runtime",
            confidence=0.95,
        )
        return message

    def stream_message(self, *, thread_id: int, content: str, payload: dict[str, Any] | None = None) -> Iterator[tuple[str, Any]]:
        """Run a chat turn and stream the reply.

        Yields ``("meta", {...})`` once (persisted user message, scope, and any action
        side-effects), then ``("token", str)`` for each reply chunk, then ``("done", {...})``
        with the persisted assistant message and finalized trace. The user message and any action
        side-effects are committed before tokens stream, so the turn survives a mid-stream
        disconnect.
        """
        # Records (run_id, thread_id) when this turn starts a deferred live application in
        # async mode. The streaming endpoint launches it as a post-response BackgroundTask
        # — never mid-stream — so the request releases its SQLite writes before the run.
        self._pending_deferred_launch: tuple[int, int] | None = None
        turn = self._prepare_chat_turn(thread_id=thread_id, content=content, payload=payload or {})
        deferred_run_id = int(turn.get("deferred_task_run_id") or 0)
        deferred_async = bool(deferred_run_id and get_settings().chat_application_async)
        if deferred_async:
            # Run creation is already durable at this point. Publish the handoff before the first
            # frame so a later transcript/serialization failure or client disconnect cannot leave
            # that accepted browser task stranded in CREATED.
            self._pending_deferred_launch = (
                deferred_run_id,
                int(turn["thread"].id),
            )
        yield (
            "meta",
            {
                "user_message": turn["user_message"],
                "chat_scope": turn["chat_scope"],
                "pending_question": turn["pending_question"],
                "task_request": turn["task_request"],
                "application_request": turn["application_request"],
                "run_control": turn["run_control"],
                "runtime_correction": turn["runtime_correction"],
                "question_answer": turn["question_answer"],
            },
        )
        if turn["mode"] == "static":
            assistant_text = turn["assistant_text"]
            chat_response = turn["chat_response"]
            if deferred_run_id:
                run_id = deferred_run_id
                request_model = str(turn.get("task_request_model") or "start_browser_task")
                if deferred_async:
                    # Acknowledge immediately and DEFER the live run to a post-response
                    # BackgroundTask (recorded here, launched by the streaming endpoint once
                    # this request has closed and released its SQLite writes). The chat then
                    # narrates start -> (activity feed shows per-step intent) -> outcome,
                    # instead of blocking the whole turn until the run finishes.
                    assistant_text = assistant_text or self._deferred_start_ack(run_id)
                    chat_response = {
                        "source": "action",
                        "provider": "operator_chat_service",
                        "model": request_model,
                        "deferred_task_async": True,
                        "deferred_application_async": request_model == "start_application",
                    }
                    if assistant_text:
                        yield ("token", assistant_text)
                else:
                    if assistant_text:
                        yield ("token", assistant_text)
                    self._continue_deferred_task_from_chat(turn)
                    assistant_text = turn["assistant_text"]
                    chat_response = turn["chat_response"]
            elif assistant_text:
                yield ("token", assistant_text)
        else:
            parts: list[str] = []
            assistant_text = turn["fallback_answer"]
            chat_response = {}
            for event in self.lumen_model.stream_operator_chat_reply(turn["stream_payload"], fallback_answer=turn["fallback_answer"]):
                if event.get("type") == "delta":
                    text = str(event.get("text") or "")
                    if text:
                        parts.append(text)
                        yield ("token", text)
                else:
                    assistant_text = str(event.get("answer") or "".join(parts) or turn["fallback_answer"])
                    chat_response = dict(event.get("metadata") or {})
            # Grounded-response enforcement: the free-text model may have claimed a run action.
            # The stream path carries no verified receipts, so strip any unsupported lifecycle claim
            # from the finalized reply — the system reports reality, the model never narrates an
            # action that did not happen. (Static action replies are already receipt-derived.)
            from app.claim_validator import enforce_grounded_reply

            receipts = (turn.get("stream_payload") or {}).get("execution_receipts") or []
            grounded_text, stripped = enforce_grounded_reply(assistant_text, receipts)
            if stripped:
                assistant_text = grounded_text
                chat_response = {**(chat_response or {}), "grounded_response_enforced": True, "stripped_claim_count": len(stripped)}
        result = self._finalize_chat_turn(turn, assistant_text=assistant_text, chat_response=chat_response)
        yield ("done", result)

    def _prepare_chat_turn(self, *, thread_id: int, content: str, payload: dict[str, Any]) -> dict[str, Any]:
        started_at = time.perf_counter()
        thread = self._thread(thread_id)
        # A TOTP seed in the user's message is a credential: it is replaced by a placeholder before the message is
        # stored, classified, or shown to any model, and kept encrypted with the run that uses it.
        self._turn_totp_seeds = []
        content = self._protect_user_text(content, run_id=thread.run_id)
        payload = self._protect_user_payload(payload, run_id=thread.run_id)
        # This guard deliberately runs before _add_message. The only resume authority during
        # sensitive browser entry is the separate text-free sensitive_browser_continue control;
        # free-form content is never interpreted, persisted, or used to schedule the browser.
        self.assert_chat_message_allowed(thread_id, thread=thread)
        pending_before = self._latest_pending_question(thread.run_id) if thread.run_id else None
        user_message = self._add_message(thread, role="user", content=content, payload=payload)
        if not thread.run_id and (thread.title or "").strip() in {"", _DEFAULT_CHAT_TITLE}:
            # A fresh chat is named after what the user first asked, so the chat list reads as a
            # history of tasks. Starting a run still retitles the thread after its target.
            thread.title = _chat_title_from_message(content) or _DEFAULT_CHAT_TITLE
        # A model-backed scope/correction decision can take substantially longer than a normal
        # database operation. Commit the accepted human turn before invoking the model so SQLite
        # never holds the ChatMessage write transaction (and its database-wide writer lock) across
        # provider latency. The message is intentionally durable even if classification or the
        # client stream later fails; subsequent action side-effects are committed separately below.
        self.db.commit()
        initial_context = self._lightweight_chat_context(thread)
        structured_pending_action = self._structured_pending_field_action(pending_before, payload)
        replayed_review_scope = (
            None
            if structured_pending_action
            else self._replayed_review_instruction_scope(thread, content)
        )
        if structured_pending_action:
            chat_scope = {
                "action": "answer_question",
                "primary_intent": "approval_or_otp",
                "in_scope": True,
                "run_related": True,
                "approval_related": True,
                "out_of_scope": False,
                "confidence": 1.0,
                "metadata": {"route": "structured_pending_field_action"},
            }
        elif replayed_review_scope:
            chat_scope = replayed_review_scope
        else:
            chat_scope = self._classify_chat_scope(thread, content, payload=payload, context=initial_context, pending_question=pending_before)
            if self._pending_question_answer_presumed(pending_before, content, chat_scope=chat_scope):
                chat_scope = self._as_pending_question_answer_scope(chat_scope, pending_before)
        scope_metadata = chat_scope.get("metadata") if isinstance(chat_scope.get("metadata"), dict) else {}
        scope_provider = str(scope_metadata.get("provider") or "")
        scope_model_unavailable = bool(
            not structured_pending_action
            and (
                scope_provider == "fallback_after_model_error"
                or (scope_provider == "model_unavailable" and chat_scope.get("out_of_scope"))
            )
        )
        out_of_scope = bool(chat_scope.get("out_of_scope"))
        run_control = (
            None
            if out_of_scope or structured_pending_action or scope_model_unavailable
            else self._try_run_control_from_chat(thread, chat_scope=chat_scope)
        )
        review_instruction = (
            None
            if run_control or structured_pending_action or scope_model_unavailable
            else self._try_apply_review_instruction_from_chat(
                thread,
                content,
                chat_scope=chat_scope,
                human_message_id=user_message.id,
            )
        )
        correction = (
            review_instruction
            if review_instruction
            else None
            if run_control or structured_pending_action or scope_model_unavailable
            else self._try_apply_runtime_correction(thread, content, payload=payload, chat_scope=chat_scope)
        )
        if run_control or correction or scope_model_unavailable:
            question_answer = None
        else:
            question_answer = self._try_answer_pending_question_from_chat(
                thread,
                content,
                payload=payload,
                chat_scope=chat_scope,
                human_message_id=user_message.id,
            )
        effective_out_of_scope = bool(
            (out_of_scope or scope_model_unavailable) and not correction and not question_answer
        )
        remembered_fact = (
            None
            if effective_out_of_scope or run_control or correction or question_answer
            else self._try_remember_user_fact_from_chat(thread, chat_scope=chat_scope)
        )
        if not effective_out_of_scope:
            self._learn_user_facts_in_passing(thread, chat_scope=chat_scope)
        browser_task_request = (
            None
            if effective_out_of_scope or run_control or correction or question_answer or remembered_fact
            else self._try_start_browser_task_from_chat(
                thread,
                content=content,
                payload=payload,
                chat_scope=chat_scope,
            )
        )
        application_request = (
            None
            if browser_task_request or effective_out_of_scope or run_control or correction or question_answer or remembered_fact
            else self._try_start_application_from_chat(thread, content, payload=payload, chat_scope=chat_scope)
        )
        task_request = browser_task_request or application_request
        pending_question = self.ensure_question_for_run(thread.run_id, thread_id=thread.id) if thread.run_id else None
        context = self._chat_response_context(
            thread,
            base_context=self._lightweight_chat_context(thread),
            payload=payload,
            pending_question=pending_question,
        )
        awaiting_state_reply = None
        if not effective_out_of_scope and not run_control and not correction and not question_answer and not task_request and not remembered_fact:
            awaiting_state_reply = self._awaiting_state_static_reply(
                thread,
                context=context,
                pending_question=pending_question,
                content=content,
                chat_scope=chat_scope,
            )
        turn: dict[str, Any] = {
            "thread": thread,
            "started_at": started_at,
            "payload": payload,
            "user_message": user_message,
            "context": context,
            "chat_scope": chat_scope,
            "pending_question": pending_question,
            "task_request": task_request,
            "application_request": application_request,
            "deferred_task_run_id": (
                int(task_request.get("run_id"))
                if isinstance(task_request, dict)
                and (task_request.get("deferred_task_start") or task_request.get("deferred_application_start"))
                and task_request.get("run_id")
                else None
            ),
            "task_request_model": (
                "start_application"
                if application_request and task_request is application_request
                else "start_browser_task"
            ),
            "run_control": run_control,
            "runtime_correction": correction,
            "question_answer": question_answer,
            "remembered_fact": remembered_fact,
        }
        if scope_model_unavailable:
            turn.update(
                mode="static",
                assistant_text=(
                    "Lumen's reasoning model is unavailable, so I did not interpret or run this task. "
                    "Configure and validate a model provider in Settings, then try again."
                ),
                chat_response={
                    "source": "runtime_guard",
                    "provider": "operator_chat_service",
                    "model": "model_unavailable",
                    "fallback_reason": "scope_model_unavailable",
                    "error": str(scope_metadata.get("error") or "")[:500],
                },
            )
        elif effective_out_of_scope:
            turn.update(
                mode="static",
                assistant_text=_out_of_scope_reply(content=content, recent_messages=context.get("recent_messages")),
                chat_response={"source": "scope_redirect", "provider": "scope_guard", "model": "scope_guard", "fallback_reason": "out_of_scope"},
            )
        elif task_request:
            turn.update(
                mode="static",
                assistant_text=task_request["assistant_text"],
                chat_response={
                    "source": "action",
                    "provider": "operator_chat_service",
                    "model": turn["task_request_model"],
                },
            )
        elif remembered_fact:
            turn.update(mode="static", assistant_text=remembered_fact["assistant_text"], chat_response={"source": "action", "provider": "operator_chat_service", "model": "user_memory"})
        elif run_control:
            turn.update(mode="static", assistant_text=run_control["assistant_text"], chat_response={"source": "action", "provider": "operator_chat_service", "model": "run_control"})
        elif correction:
            turn.update(mode="static", assistant_text=correction["assistant_text"], chat_response={"source": "action", "provider": "operator_chat_service", "model": "runtime_correction"})
        elif question_answer:
            turn.update(mode="static", assistant_text=question_answer["assistant_text"], chat_response={"source": "action", "provider": "operator_chat_service", "model": "pending_question_answer"})
        elif awaiting_state_reply:
            turn.update(
                mode="static",
                assistant_text=awaiting_state_reply,
                chat_response={"source": "fallback", "provider": "operator_chat_service", "model": "awaiting_state_redirect"},
            )
        else:
            turn.update(
                mode="stream",
                fallback_answer=_assistant_reply(context, pending_question, content=content, chat_scope=chat_scope),
                stream_payload={
                    "message": content,
                    "payload": payload,
                    "context": context,
                    "chat_scope": chat_scope,
                    "mode": payload.get("view_mode") or payload.get("source") or "unknown",
                    # The stream path is only reached when NO action fired this turn, so no run
                    # lifecycle changed — the reply is grounded on these (empty) execution receipts
                    # and is structurally forbidden from claiming a stop/start that didn't happen.
                    "execution_receipts": [],
                },
            )
        # Persist any action side-effects before tokens stream so the interpreted lifecycle change
        # is durable even if the client disconnects mid-stream. The user message itself was committed
        # before model inference above to avoid holding a database writer lock across provider latency.
        self.db.commit()
        return turn

    def _finalize_chat_turn(self, turn: dict[str, Any], *, assistant_text: str, chat_response: dict[str, Any]) -> dict[str, Any]:
        thread = turn["thread"]
        pending_question = turn["pending_question"]
        chat_response = self._finalize_chat_response_trace(
            chat_response,
            chat_scope=turn["chat_scope"],
            thread=thread,
            payload=turn["payload"],
            total_latency_ms=int((time.perf_counter() - turn["started_at"]) * 1000),
        )
        assistant = self._add_message(
            thread,
            role="assistant",
            content=assistant_text,
            payload={
                "context": turn["context"],
                "chat_scope": turn["chat_scope"],
                "pending_question_id": pending_question.id if pending_question else None,
                "task_request": turn["task_request"],
                "application_request": turn["application_request"],
                "run_control": turn["run_control"],
                "runtime_correction": turn["runtime_correction"],
                "question_answer": turn["question_answer"],
                "chat_response": chat_response,
            },
        )
        emit_event(
            self.db,
            event_type="OPERATOR_CHAT_MESSAGE_CREATED",
            payload={"thread_id": thread.id, "user_message_id": turn["user_message"].id, "assistant_message_id": assistant.id, "has_pending_question": bool(pending_question)},
            run_id=thread.run_id,
            actor="operator_chat",
            confidence=0.86,
        )
        emit_event(self.db, event_type="OPERATOR_CHAT_CONTEXT_BUILT", payload={"thread_id": thread.id, "context": turn["context"]}, run_id=thread.run_id, actor="operator_chat", confidence=0.82)
        emit_event(
            self.db,
            event_type="OPERATOR_CHAT_RESPONSE_GENERATED",
            payload={"thread_id": thread.id, "assistant_message_id": assistant.id, "chat_response": chat_response},
            run_id=thread.run_id,
            actor="operator_chat",
            confidence=0.84,
        )
        self.db.commit()
        return {
            "user_message": turn["user_message"],
            "assistant_message": assistant,
            "context": turn["context"],
            "pending_question": pending_question,
            "chat_scope": turn["chat_scope"],
            "task_request": turn["task_request"],
            "application_request": turn["application_request"],
            "run_control": turn["run_control"],
            "runtime_correction": turn["runtime_correction"],
            "question_answer": turn["question_answer"],
            "chat_response": chat_response,
        }

    def _lightweight_chat_context(self, thread: models.ChatThread) -> dict[str, Any]:
        run = self.db.get(models.OperatorRun, thread.run_id) if thread.run_id else None
        if not run:
            return {"scope": "global", "chat_mode": {"thread_mode": thread.mode}}
        workspace = dict((run.confidence_state or {}).get("end_to_end") or {})
        return redact_otp_codes(
            {
                "scope": "run",
                "run": {"id": run.id, "state": run.current_state, "status_message": run.status_message, "risk_level": run.risk_level, "target_url": run.target_url},
                "chat_mode": {"thread_mode": thread.mode},
                "goal_workspace": {
                    "phase": workspace.get("phase"),
                    "run_phase": workspace.get("run_phase"),
                    "pending_action": workspace.get("pending_action"),
                    "terminal_state": workspace.get("terminal_state"),
                    "resume_strategy": workspace.get("selected_submission_strategy") or workspace.get("submission_strategy"),
                },
            }
        )

    def _chat_response_context(
        self,
        thread: models.ChatThread,
        *,
        base_context: dict[str, Any],
        payload: dict[str, Any],
        pending_question: models.OperatorQuestion | None,
    ) -> dict[str, Any]:
        context = {
            **base_context,
            "view_mode": payload.get("view_mode") or payload.get("source") or "unknown",
            "recent_messages": self._recent_thread_history(thread.id),
            "pending_question": {
                "id": pending_question.id,
                "question_type": pending_question.question_type,
                "stuck_phase": pending_question.stuck_phase,
                "stuck_reason": pending_question.stuck_reason,
                "prompt": pending_question.prompt,
                "expected_answer_schema": pending_question.expected_answer_schema,
            }
            if pending_question
            else None,
        }
        return redact_otp_codes(context)

    def _finalize_chat_response_trace(
        self,
        chat_response: dict[str, Any],
        *,
        chat_scope: dict[str, Any],
        thread: models.ChatThread,
        payload: dict[str, Any],
        total_latency_ms: int,
    ) -> dict[str, Any]:
        trace = redact_otp_codes(dict(chat_response or {}))
        trace.setdefault("source", "unknown")
        trace.setdefault("provider", None)
        trace.setdefault("model", None)
        trace.setdefault("fallback_reason", None)
        trace.setdefault("error", None)
        trace.setdefault("latency_ms", trace.get("latency_ms") or total_latency_ms)
        trace["total_latency_ms"] = total_latency_ms
        trace["active_run_id"] = thread.run_id
        trace["mode"] = payload.get("view_mode") or payload.get("source") or "unknown"
        trace["scope_classification"] = {
            key: chat_scope.get(key)
            for key in (
                "in_scope",
                "action_request",
                "run_related",
                "profile_related",
                "resume_related",
                "approval_related",
                "out_of_scope",
                "primary_intent",
                "action",
                "confidence",
            )
        }
        scope_metadata = chat_scope.get("metadata") if isinstance(chat_scope.get("metadata"), dict) else {}
        trace["scope_provider"] = scope_metadata.get("provider")
        trace["scope_model"] = scope_metadata.get("model")
        return trace

    def _classify_chat_scope(
        self,
        thread: models.ChatThread,
        content: str,
        *,
        payload: dict[str, Any],
        context: dict[str, Any],
        pending_question: models.OperatorQuestion | None,
    ) -> dict[str, Any]:
        scope_context = {
            "thread_id": thread.id,
            "thread_mode": thread.mode,
            "view_mode": payload.get("view_mode") or payload.get("source") or "unknown",
            "active_run": bool(thread.run_id),
            "run": context.get("run"),
            "goal_workspace": context.get("goal_workspace"),
            "pending_question": {
                "id": pending_question.id,
                "question_type": pending_question.question_type,
                "stuck_phase": pending_question.stuck_phase,
                "stuck_reason": pending_question.stuck_reason,
                "prompt": pending_question.prompt,
                "expected_answer_schema": pending_question.expected_answer_schema,
            }
            if pending_question
            else None,
            "recent_messages": self._recent_thread_history(thread.id),
            "pending_task_clarification": self._pending_task_clarification(thread.id),
        }
        scope = self.lumen_model.classify_operator_chat_scope(
            {
                "message": content,
                "payload": payload,
                "context": scope_context,
            }
        )
        if self._pending_question_answer_presumed(pending_question, content, chat_scope=scope):
            scope = self._as_pending_question_answer_scope(scope, pending_question)
        if pending_question and scope.get("in_scope"):
            scope["approval_related"] = bool(scope.get("approval_related") or pending_question.question_type == "final_action_approval")
            scope["run_related"] = True
        scope["view_mode"] = scope_context["view_mode"]
        return scope

    def _pending_question_answer_presumed(self, question: models.OperatorQuestion | None, content: str, *, chat_scope: dict[str, Any]) -> bool:
        if not question or question.question_type != "field_approval":
            return False
        if self._pending_question_explicit_control_message(content, chat_scope=chat_scope):
            return False
        text = str(content or "").strip()
        if not text:
            return False
        lowered = text.lower()
        if lowered.endswith("?") or re.match(r"^(what|why|how|can you check|tell me|explain)\b", lowered):
            return False
        return True

    def _pending_question_explicit_control_message(self, content: str, *, chat_scope: dict[str, Any]) -> bool:
        action = str(chat_scope.get("action") or "none")
        if action in {"pause", "stop"}:
            return True
        text = re.sub(r"\s+", " ", str(content or "").strip().lower())
        return bool(
            re.fullmatch(r"(pause|hold)( this| run| application| session)?[.! ]*", text)
            or re.fullmatch(r"(stop|cancel)( this| run| application| session)?[.! ]*", text)
            or re.fullmatch(r"end( this| run| application| session)?[.! ]*", text)
        )

    def _as_pending_question_answer_scope(self, scope: dict[str, Any], question: models.OperatorQuestion | None) -> dict[str, Any]:
        metadata = dict(scope.get("metadata") or {})
        metadata["pending_question_route"] = "field_approval_answer_presumed"
        routed = {
            **scope,
            "in_scope": True,
            "out_of_scope": False,
            "run_related": True,
            "approval_related": True,
            "action_request": True,
            "primary_intent": "approval_or_otp",
            "action": "answer_question",
            "safe_to_answer": True,
            "metadata": metadata,
        }
        if question:
            routed["pending_question_id"] = question.id
            routed["pending_question_type"] = question.question_type
        return routed

    def _recent_thread_history(self, thread_id: int, *, limit: int = 8) -> list[dict[str, str]]:
        messages = list(
            self.db.scalars(
                select(models.ChatMessage).where(models.ChatMessage.thread_id == thread_id).order_by(models.ChatMessage.id.desc()).limit(limit)
            ).all()
        )
        return [{"role": message.role, "content": message.content[:500]} for message in reversed(messages)]

    def _last_detected_job_url(self, thread: models.ChatThread) -> str | None:
        messages = list(
            self.db.scalars(
                select(models.ChatMessage).where(models.ChatMessage.thread_id == thread.id).order_by(models.ChatMessage.id.desc()).limit(12)
            ).all()
        )
        for message in messages:
            payload = message.payload_json or {}
            application_request = payload.get("application_request") if isinstance(payload.get("application_request"), dict) else {}
            job_url = application_request.get("job_url") if isinstance(application_request, dict) else None
            if isinstance(job_url, str) and _is_http_url(job_url):
                return job_url
            extracted = _extract_job_url(message.content, payload if isinstance(payload, dict) else {})
            if extracted:
                return extracted
        return None

    def _run_state_snapshot(self, run_id: int) -> dict[str, Any]:
        """A fresh DB read of a run's lifecycle state — used for verified before/after receipts."""
        run = self.db.get(models.OperatorRun, run_id)
        return {"run_id": int(run_id), "state": str(run.current_state) if run and run.current_state else None}

    def _active_in_browser_run_ids(self) -> list[int]:
        """Runs currently occupying the real browser (RUNNING/WAITING_FOR_USER + in_browser tag)."""
        rows = self.db.scalars(select(models.OperatorRun).where(models.OperatorRun.current_state.in_(("RUNNING", "WAITING_FOR_USER")))).all()
        return [int(r.id) for r in rows if isinstance((r.confidence_state or {}).get("in_browser"), dict)]

    def _run_control_reply_from_receipt(self, action: str, receipt: dict[str, Any]) -> str:
        """The reply is rendered ONLY from the receipt — never from an assumed outcome. A failed /
        unverified action is reported accurately, not narrated as success."""
        rid = receipt.get("target", {}).get("resolved_id")
        verification = receipt.get("verification", {})
        before = (verification.get("before") or {}).get("state")
        after = (verification.get("after") or {}).get("state")
        if receipt.get("outcome") != "verified_succeeded":
            error = receipt.get("execution", {}).get("error")
            detail = f" (error: {error})" if error else ""
            if action == "stop":
                return f"I tried to stop run #{rid}, but it's still {after or 'in an unknown state'}{detail}. Nothing was changed — it may have already finished; check the run."
            return f"I tried to {action.replace('_', ' ')} run #{rid}, but it didn't take (still {after}){detail}."
        if action == "stop":
            return f"Stopped run #{rid} ({before} → cancelled) and freed your browser. Send a job link to start the next one."
        if action == "pause":
            return f"Paused run #{rid} (now {after}). Tell me to resume when you're ready."
        if action == "resume":
            return f"Resuming run #{rid} (now {after})."
        if action == "takeover":
            return f"Human takeover is active on run #{rid} (now {after})."
        if action == "end_takeover":
            return f"Ended human takeover on run #{rid} (now {after}). Tell me to resume when ready."
        return f"Run #{rid} is now {after}."

    def _try_remember_user_fact_from_chat(self, thread: models.ChatThread, *, chat_scope: dict[str, Any]) -> dict[str, Any] | None:
        """Store a volunteered durable user fact as one markdown bullet, honestly.

        The classifier only fills remember_request when the user states a durable
        personal fact or explicitly asks Lumen to remember something. Without this
        handler the model could SAY "I'll remember that" while nothing was stored -
        every remember reply must be backed by a verified write.
        """
        action = str(chat_scope.get("action") or "none")
        if action not in {"remember_user_fact", "forget_user_fact"}:
            return None
        request = chat_scope.get("remember_request") if isinstance(chat_scope.get("remember_request"), dict) else {}
        key = str(request.get("key") or "").strip()
        value = str(request.get("value") or "").strip()
        from app import user_memory
        from app.runtime import OperatorRuntimeEngine

        user = OperatorRuntimeEngine(self.db).ensure_default_user()
        if action == "forget_user_fact":
            if not key:
                return None
            forgotten = user_memory.forget(user.id, key)
            if forgotten:
                user_memory.mirror_to_profile(self.db, user.id, cleared=(user_memory.normalize_memory_key(key),))
            spoken = user_memory.normalize_memory_key(key).replace("_", " ")
            emit_event(
                self.db,
                event_type="USER_MEMORY_FORGOTTEN" if forgotten else "USER_MEMORY_FORGET_MISSED",
                payload={"key": user_memory.normalize_memory_key(key)},
                run_id=thread.run_id,
                actor="operator_chat",
                confidence=0.9,
                risk_level="low",
            )
            return {
                "remembered": False,
                "key": user_memory.normalize_memory_key(key),
                "value": "",
                "action": "forget",
                "assistant_text": (
                    f"Forgotten - removed {spoken} from your profile."
                    if forgotten
                    else f"There was nothing stored under {spoken}, so nothing changed."
                ),
            }
        if not key or not value:
            return None
        applied = user_memory.remember_all_reconciled(
            self.db,
            user.id,
            [{"key": key, "value": value, "section": str(request.get("section") or "")}],
            source_run_id=thread.run_id,
            source_note="you, in chat",
        )
        record = applied[0] if applied else {"key": key, "value": value, "action": "unstorable"}
        if record["action"] == "unstorable":
            return {
                "remembered": False,
                "assistant_text": "I couldn't store that as a memory - tell me the fact again in one short sentence.",
            }
        emit_event(
            self.db,
            event_type="USER_MEMORY_CONFLICT" if record["action"] == "conflict" else "USER_MEMORY_WRITTEN",
            payload={"key": record["key"], "action": record["action"], "value_length": len(record["value"])},
            run_id=thread.run_id,
            actor="operator_chat",
            confidence=0.9,
            risk_level="low",
        )
        spoken_key = record["key"].replace("_", " ")
        if record["action"] == "conflict":
            existing_value = str(record.get("existing_value") or "")
            return {
                "remembered": False,
                "key": record["key"],
                "value": record["value"],
                "action": "conflict",
                "assistant_text": (
                    f"That conflicts with what I have stored: {spoken_key} is currently "
                    f"“{existing_value}”, and you just gave me “{record['value']}”. "
                    "Which should I keep? You can also keep both under separate names "
                    "(for example personal email and school email) or tell me to forget one."
                ),
            }
        if record["action"] == "skip":
            assistant_text = (
                f"Already remembered - that's in your profile as {spoken_key}, so I didn't add a duplicate."
            )
        elif record["action"] == "update":
            assistant_text = f"Updated - {spoken_key} is now: {record['value']}."
        else:
            assistant_text = (
                f"Saved - I'll remember {spoken_key}: {record['value']}. "
                "Everything I know about you is in your profile, where you can change or delete it any time."
            )
        return {
            "remembered": True,
            "key": record["key"],
            "value": record["value"],
            "action": record["action"],
            "assistant_text": assistant_text,
        }

    def _learn_user_facts_in_passing(self, thread: models.ChatThread, *, chat_scope: dict[str, Any]) -> None:
        """Remember durable facts the user mentioned while asking for something else.

        The classifier lists them in user_facts (a model judgment: durable, about the user, not a
        secret). They are stored quietly and show up in the profile marked new; a fact that
        contradicts a stored one is not written (the user settles it in the profile).
        """
        learned = [item for item in (chat_scope.get("user_facts") or []) if isinstance(item, dict)]
        if not learned:
            return
        from app import user_memory
        from app.runtime import OperatorRuntimeEngine

        user = OperatorRuntimeEngine(self.db).ensure_default_user()
        savepoint = self.db.begin_nested()
        try:
            applied = user_memory.remember_all_reconciled(
                self.db, user.id, learned, source_run_id=thread.run_id, source_note="you, in chat", implicit=True
            )
            savepoint.commit()
        except Exception:  # noqa: BLE001 - learning in passing must never break the chat turn
            savepoint.rollback()
            return
        written = [record for record in applied if record.get("action") in {"add", "update"}]
        if written:
            emit_event(
                self.db,
                event_type="USER_MEMORY_LEARNED_IN_PASSING",
                payload={"keys": [record["key"] for record in written]},
                run_id=thread.run_id,
                actor="operator_chat",
                confidence=0.8,
                risk_level="low",
            )

    def _try_run_control_from_chat(self, thread: models.ChatThread, *, chat_scope: dict[str, Any]) -> dict[str, Any] | None:
        action = str(chat_scope.get("action") or "none")
        if action not in {"pause", "resume", "takeover", "end_takeover", "stop"}:
            return None
        # Resolve the target — FAIL CLOSED on no/ambiguous target rather than guessing which run.
        target_run_id = thread.run_id
        current = self.db.get(models.OperatorRun, target_run_id) if target_run_id else None
        if action == "stop" and (not target_run_id or (current and current.current_state in TERMINAL_RUN_STATES)):
            active = self._active_in_browser_run_ids()
            if len(active) == 1:
                target_run_id = active[0]
            elif not active:
                return {"action": action, "applied": False, "receipts": [], "assistant_text": "There's no active task to stop right now."}
            else:
                listed = ", ".join(f"#{rid}" for rid in active)
                return {"action": action, "applied": False, "receipts": [], "assistant_text": f"You have {len(active)} active runs ({listed}) — tell me which one to stop."}
        if not target_run_id:
            return {"action": action, "applied": False, "receipts": [], "assistant_text": "I can do that once there is an active application run. Send me a job link and tell me to apply, or open an existing run."}

        from app.runtime import OperatorRuntimeEngine

        engine = OperatorRuntimeEngine(self.db)
        before = self._run_state_snapshot(target_run_id)  # verified BEFORE
        error: str | None = None
        try:
            if action == "pause":
                engine.pause_run(target_run_id, reason="Paused from Lumen chat", notify_chat=False)
            elif action == "resume":
                engine.resume_run(target_run_id, notify_chat=False)
            elif action == "takeover":
                engine.start_human_takeover(target_run_id)
            elif action == "end_takeover":
                engine.end_human_takeover(target_run_id)
            else:
                engine.cancel_run(target_run_id, reason="Stopped from Lumen chat", notify_chat=False)
        except Exception as exc:  # noqa: BLE001 - attempted-but-failed becomes a 'failed' receipt, not a fabricated success
            error = str(exc) or exc.__class__.__name__
        after = self._run_state_snapshot(target_run_id)  # verified AFTER (read back from the DB)

        # Model attempt / execution / verification as INDEPENDENT axes; the runtime derives `outcome`.
        # planned != attempted != executed != VERIFIED. Stop must reach CANCELLED to verify; other
        # controls verify on an actual state change. "Executor returned but postcondition not met" is
        # verification_failed (not execution_failed) — exactly the "reported success but unverified" case.
        attempt = {"status": "attempted"}
        if error:
            execution = {"status": "failed", "error": error}
            verification: dict[str, Any] = {"status": "not_verified"}
        else:
            execution = {"status": "succeeded"}
            verified = (after["state"] == "CANCELLED") if action == "stop" else (after["state"] != before["state"])
            verification = {"status": "succeeded" if verified else "failed", "before": {"state": before["state"]}, "after": {"state": after["state"]}}
        receipt = make_receipt(
            operation_id=action,
            type={"stop": "cancel_run", "pause": "pause_run", "resume": "resume_run", "takeover": "start_takeover", "end_takeover": "end_takeover"}[action],
            target={"ref": "active_run", "resolved_id": target_run_id},
            attempt=attempt,
            execution=execution,
            verification=verification,
        )
        emit_event(
            self.db,
            event_type="LUMEN_CHAT_RUN_CONTROL_APPLIED",
            payload={"thread_id": thread.id, "action": action, "receipt": receipt},
            run_id=target_run_id,
            actor="lumen_chat",
            confidence=float(chat_scope.get("confidence") or 0.75),
            risk_level="medium" if action == "stop" else "low",
        )
        return {
            "action": action,
            "applied": receipt["outcome"] == "verified_succeeded",
            "run_id": target_run_id,
            "run_state": after["state"],
            "receipts": [receipt],
            "assistant_text": self._run_control_reply_from_receipt(action, receipt),
        }

    def _active_thread_id_for_run(self, run_id: int | None) -> int | None:
        if not run_id:
            return None
        thread_id = self.db.scalar(
            select(models.ChatThread.id)
            .where(models.ChatThread.run_id == run_id, models.ChatThread.status == "active")
            .order_by(models.ChatThread.updated_at.desc(), models.ChatThread.id.desc())
            .limit(1)
        )
        return int(thread_id) if thread_id else None

    def _question_thread_id(self, run_id: int | None, requested_thread_id: int | None = None) -> int | None:
        if requested_thread_id:
            return requested_thread_id
        return self._active_thread_id_for_run(run_id)

    def _latest_pending_question(self, run_id: int | None) -> models.OperatorQuestion | None:
        if not run_id:
            return None
        self._reconcile_stale_task_questions(run_id, preferred_thread_id=self._active_thread_id_for_run(run_id))
        return self.db.scalars(
            select(models.OperatorQuestion)
            .where(models.OperatorQuestion.run_id == run_id, models.OperatorQuestion.status == "pending")
            .order_by(models.OperatorQuestion.id.desc())
            .limit(1)
        ).first()

    def _structured_pending_field_action(
        self,
        question: models.OperatorQuestion | None,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Validate an explicit UI action against the pending question contract.

        This is state binding, not intent inference: the backend already supplied the proposal,
        choices, and task refs, and the user clicked one of those rendered controls. Free-form text
        remains model interpreted.
        """
        if not question or question.question_type != "field_approval":
            return None
        keep = payload.get("keep_proposed") is True
        skip = payload.get("skip") is True
        selected_option = str(payload.get("selected_option") or "").strip()
        raw_selected_options = payload.get("selected_options")
        selected_options = (
            [str(option).strip() for option in raw_selected_options]
            if isinstance(raw_selected_options, list)
            else []
        )
        explicit_actions = int(keep) + int(skip) + int(bool(selected_option)) + int(bool(selected_options))
        if explicit_actions != 1:
            return None
        if keep:
            return {"action": "keep_proposed"}
        if skip:
            return {"action": "skip"}

        schema = question.expected_answer_schema or {}
        fields = [field for field in (schema.get("fields") or []) if isinstance(field, dict)]
        field = fields[0] if fields else {}
        options = [
            str(option).strip()
            for option in (field.get("options") or field.get("visible_options") or schema.get("options") or [])
            if str(option).strip()
        ]
        if selected_options:
            # Several picks bind only on a field the contract marks multi-select, each an exact live option.
            if (
                not field.get("multiple")
                or len(set(selected_options)) != len(selected_options)
                or any(option not in options for option in selected_options)
            ):
                return None
            ordered = [option for option in options if option in selected_options]
            return {
                "action": "selected_options",
                "selected_options": ordered,
                "selected_option": multi_select_answer_text(ordered),
            }
        if selected_option not in options:
            return None
        return {"action": "selected_option", "selected_option": selected_option}

    def _try_answer_pending_question_from_chat(
        self,
        thread: models.ChatThread,
        content: str,
        *,
        payload: dict[str, Any],
        chat_scope: dict[str, Any],
        human_message_id: int | None = None,
    ) -> dict[str, Any] | None:
        if not thread.run_id:
            return None
        question = self._latest_pending_question(thread.run_id)
        if not question or not self._chat_message_should_answer_question(question, content, chat_scope=chat_scope):
            return None
        if question.question_type == "final_action_approval":
            run = self.db.get(models.OperatorRun, question.run_id)
            emit_event(
                self.db,
                event_type="FINAL_ACTION_CHAT_APPROVAL_REDIRECTED",
                payload={"question_id": question.id, "browser_activation_attempted": False},
                run_id=thread.run_id,
                actor="lumen_chat",
                confidence=1.0,
                risk_level="high",
            )
            return {
                "applied": False,
                "question_id": question.id,
                "question_type": question.question_type,
                "run_state": getattr(run, "current_state", None),
                "resolved_value": None,
                "confidence": 1.0,
                "rationale": "final_action_requires_structured_question_control",
                "save_scope": "one_time",
                "saved_common_answers": [],
                "assistant_text": (
                    "I did not treat that chat message as final-action authorization. "
                    "Use the exact approve or decline control on the active final-action question."
                ),
            }
        if question.question_type == "chrome_profile_choice":
            return self._answer_chrome_profile_choice_from_chat(thread, question, content)
        reply_interpretation: dict[str, Any] | None = None
        # A card click is already an explicit, structured human decision. Keep/skip buttons carry
        # their action and exact task binding separately from natural-language chat, so running the
        # reply interpreter again can only add latency or turn a valid click into a provider-shaped
        # failure. Free-form replies still go through the model-owned interpreter below.
        structured_field_action = self._structured_pending_field_action(question, payload)
        if structured_field_action:
            emit_event(
                self.db,
                event_type="PENDING_USER_REPLY_STRUCTURED_ACTION",
                payload={
                    "question_id": question.id,
                    "question_type": question.question_type,
                    "action": structured_field_action["action"],
                    "selected_option": structured_field_action.get("selected_option"),
                    **(
                        {"selected_options": structured_field_action["selected_options"]}
                        if structured_field_action.get("selected_options")
                        else {}
                    ),
                    "task_ids": [
                        int(task_id)
                        for task_id in (payload.get("task_ids") or [])
                        if str(task_id).isdigit()
                    ],
                    "save_scope": self._chat_answer_save_scope(question, content, payload=payload),
                },
                run_id=thread.run_id,
                actor="human",
                confidence=1.0,
                risk_level="low",
            )
        elif question.question_type in {"field_approval", "final_action_approval"}:
            reply_interpretation = self._interpret_pending_user_reply(question, content)
            raw_model_metadata = reply_interpretation.get("metadata") if isinstance(reply_interpretation.get("metadata"), dict) else {}
            reply_model_receipt = {
                key: raw_model_metadata.get(key)
                for key in (
                    "provider",
                    "provider_display_name",
                    "provider_id",
                    "model",
                    "latency_ms",
                    "total_latency_ms",
                    "attempt_count",
                    "parse_status",
                    "finish_reason",
                    "raw_output_empty",
                    "max_tokens",
                    "timeout_seconds",
                    "reasoning_config_included",
                    "reasoning_enabled_at_transport",
                )
                if raw_model_metadata.get(key) is not None
            }
            if not reply_interpretation.get("ok"):
                failure_kind = str(reply_interpretation.get("failure_kind") or "semantic_unclear").strip().lower()
                interpreter_system_failure = failure_kind in {
                    "interpreter_unavailable",
                    "provider_failure",
                    "provider_output_failure",
                    "schema_invalid",
                }
                emit_event(
                    self.db,
                    event_type=(
                        "PENDING_USER_REPLY_INTERPRETER_FAILED"
                        if interpreter_system_failure
                        else "PENDING_USER_REPLY_UNCLEAR"
                    ),
                    payload={
                        "question_id": question.id,
                        "question_type": question.question_type,
                        "route": reply_interpretation.get("route"),
                        "failure_kind": failure_kind,
                        "confidence": reply_interpretation.get("confidence"),
                        "rationale": reply_interpretation.get("rationale"),
                        "model_metadata": reply_model_receipt,
                    },
                    run_id=thread.run_id,
                    actor="lumen_answer_interpreter",
                    confidence=float(reply_interpretation.get("confidence") or 0.0),
                    risk_level="medium" if question.question_type == "final_action_approval" else "low",
                )
                return {
                    "applied": False,
                    "question_id": question.id,
                    "question_type": question.question_type,
                    "run_state": self.db.get(models.OperatorRun, question.run_id).current_state,
                    "resolved_value": None,
                    "confidence": reply_interpretation.get("confidence"),
                    "rationale": reply_interpretation.get("rationale") or "The reply could not be separated from its instructions safely.",
                    "failure_kind": failure_kind,
                    "save_scope": "one_time",
                    "saved_common_answers": [],
                    "assistant_text": (
                        (
                            "Lumen's answer interpreter failed before it could process your reply. Your message is recorded, "
                            "and I did not apply or save a field answer. This is a system failure, not a problem with how you answered."
                        )
                        if interpreter_system_failure
                        else (
                            "I could not verify that as authorization for the pending final action, so I did not submit. "
                            "Please confirm or decline the final-action question directly."
                            if question.question_type == "final_action_approval"
                            else "I could not separate the actual answer from the surrounding instruction safely, so I did not store or apply it. Please state the answer once more."
                        )
                    ),
                }
            emit_event(
                self.db,
                event_type="PENDING_USER_REPLY_INTERPRETED",
                payload={
                    "question_id": question.id,
                    "question_type": question.question_type,
                    "route": reply_interpretation.get("route"),
                    "memory_intent": reply_interpretation.get("memory_intent"),
                    "matched_option": reply_interpretation.get("matched_option"),
                    "confidence": reply_interpretation.get("confidence"),
                    "model_metadata": reply_model_receipt,
                },
                run_id=thread.run_id,
                actor="lumen_answer_interpreter",
                confidence=float(reply_interpretation.get("confidence") or 0.0),
                risk_level="medium" if question.question_type == "final_action_approval" else "low",
            )
            if reply_interpretation.get("route") == "instruction":
                return self._apply_pending_workflow_instruction_from_chat(
                    thread,
                    question,
                    content=content,
                    interpretation=reply_interpretation,
                    human_message_id=human_message_id,
                )
        normalized_answer = str(
            (structured_field_action or {}).get("selected_option")
            or (reply_interpretation or {}).get("matched_option")
            or (reply_interpretation or {}).get("answer_value")
            or content
        ).strip()
        answer_payload = {
            **{key: value for key, value in payload.items() if key != "selected_options"},
            "save_scope": (
                str((reply_interpretation or {}).get("save_scope") or "one_time")
                if reply_interpretation
                else self._chat_answer_save_scope(question, content, payload=payload)
            ),
        }
        if (structured_field_action or {}).get("selected_options"):
            answer_payload["selected_options"] = list(structured_field_action["selected_options"])
        if reply_interpretation:
            stored_interpretation = {
                key: reply_interpretation.get(key)
                for key in (
                    "ok",
                    "route",
                    "answer_value",
                    "matched_option",
                    "matched_options",
                    "workflow_instruction",
                    "memory_intent",
                    "save_scope",
                    "profile_key",
                    "profile_value",
                    "confidence",
                    "rationale",
                )
            }
            answer_payload.update(
                {
                    "reply_interpretation": stored_interpretation,
                    "raw_user_reply": content,
                }
            )
            if reply_interpretation.get("route") == "skip":
                answer_payload["skip"] = True
                answer_payload["skip_scope"] = "always" if reply_interpretation.get("save_scope") == "permanent" else "application"
        result = self.answer_question(
            question_id=question.id,
            answer=normalized_answer,
            payload=answer_payload,
            auto_resume=True,
            record_user_message=False,
        )
        run = result.get("run")
        state = getattr(run, "current_state", None)
        emit_event(
            self.db,
            event_type="LUMEN_CHAT_PENDING_QUESTION_ANSWERED",
            payload={"question_id": question.id, "question_type": question.question_type, "run_state": state, "can_resume": result.get("can_resume")},
            run_id=thread.run_id,
            actor="lumen_chat",
            confidence=0.86,
            risk_level="low",
        )
        applied_ok = bool(result.get("applied", {}).get("applied"))
        if question.question_type == "final_action_approval":
            applied_payload = result.get("applied", {})
            if applied_ok and applied_payload.get("declined"):
                assistant_text = "Okay, I left the application ready for your review and did not submit it."
            elif applied_ok and result.get("can_resume"):
                assistant_text = "Confirmed. Lumen is submitting the final action in the browser now."
            else:
                assistant_text = "I did not treat that as final-action confirmation, so Lumen is still waiting."
        elif not applied_ok:
            reason = result.get("rationale") or result.get("resume_error") or "the answer did not pass the apply path"
            if question.question_type == "field_approval" and result.get("applied", {}).get("choice_rehydrate_queued"):
                assistant_text = "I recorded your answer and am refreshing the live options before applying it."
            elif question.question_type == "field_approval":
                assistant_text = self._field_approval_clarification_text(question, reason=reason)
            else:
                assistant_text = f"I did not apply that answer, so Lumen is still waiting. Reason: {reason}."
        elif result.get("applied", {}).get("skipped"):
            labels = result.get("applied", {}).get("skipped_labels") or []
            label_txt = f" “{labels[0]}”" if len(labels) == 1 else ""
            skip_scope = str(result.get("applied", {}).get("scope") or "application")
            memory_text = "and I won't ask about it again" if skip_scope == "always" else "for this application"
            assistant_text = (
                f"Skipped{label_txt} {memory_text}"
                + (" — continuing now." if result.get("resumed") else ".")
                + " If the form later flags it as required, I'll bring it back to you."
            )
        else:
            resolved = result.get("resolved_value")
            # Only claim "saved to your profile" when a memory-write receipt VERIFIES it (read back),
            # never just because a save was attempted.
            memory_receipts = result.get("memory_receipts") or []
            saved_to_profile = any(
                r.get("type") == "memory_write" and r.get("outcome") == "verified_succeeded" and (r.get("target") or {}).get("scope") == "profile"
                for r in memory_receipts
            )
            saved_fragment = ", saved it to your profile" if saved_to_profile else ""
            field_receipts = result.get("field_receipts") or []
            live_field_verified = any(r.get("type") == "field_value" and r.get("outcome") == "verified_succeeded" for r in field_receipts)
            resume_error = result.get("resume_error")
            if result.get("can_resume") and result.get("resumed"):
                continuation_fragment = " and Runtime is continuing from the saved application state"
            elif result.get("can_resume") and resume_error:
                continuation_fragment = f", but Runtime could not resume automatically: {resume_error}"
            elif result.get("can_resume"):
                continuation_fragment = ", but Runtime has not resumed yet"
            else:
                continuation_fragment = ""
            if question.question_type == "field_approval" and result.get("can_resume") and not live_field_verified:
                if result.get("resumed"):
                    continuation_fragment = " and Lumen is resuming to apply and verify the live field"
                elif resume_error:
                    continuation_fragment = f", but Runtime could not resume automatically: {resume_error}"
                else:
                    continuation_fragment = ", and Lumen is waiting to resume before applying it in the browser"
                assistant_text = f"I recorded that answer{f' as {resolved}' if resolved else ''}{saved_fragment}{continuation_fragment}."
            elif (result.get("applied") or {}).get("target") == "operator_instruction":
                assistant_text = (
                    "I passed that to Lumen as an instruction for this run and it is continuing now."
                    if result.get("resumed")
                    else f"I saved that as an instruction for this run{continuation_fragment}."
                )
            elif result.get("can_resume"):
                assistant_text = f"I applied that answer{f' as {resolved}' if resolved else ''}{saved_fragment}{continuation_fragment}."
            else:
                target = str((result.get("applied") or {}).get("target") or "the current run")
                assistant_text = f"I recorded that answer for {target}, but it was not tied to a browser action, so Lumen is still waiting for an actionable field answer or correction."
        return {
            "applied": bool(result.get("applied", {}).get("applied")),
            "question_id": question.id,
            "question_type": question.question_type,
            "run_state": state,
            "resolved_value": result.get("resolved_value"),
            "confidence": result.get("confidence"),
            "rationale": result.get("rationale"),
            "memory_intent": (reply_interpretation or {}).get("memory_intent"),
            "save_scope": answer_payload.get("save_scope"),
            "saved_common_answers": result.get("saved_common_answers", []),
            "assistant_text": assistant_text,
        }

    def _field_approval_clarification_text(self, question: models.OperatorQuestion, *, reason: str) -> str:
        schema = question.expected_answer_schema or {}
        fields = [field for field in schema.get("fields") or [] if isinstance(field, dict)]
        label = str((fields[0] if fields else {}).get("label") or question.prompt or "this field").strip()
        options = [str(option).strip() for option in ((fields[0] if fields else {}).get("options") or []) if str(option).strip()]
        if options:
            return f"I still need a specific answer for: {label}. Choose one of: {', '.join(options)}. Reason: {reason}."
        return f"I still need a specific answer for: {label}. Reason: {reason}."

    def _chat_answer_save_scope(self, question: models.OperatorQuestion, content: str, *, payload: dict[str, Any]) -> str:
        explicit = str(payload.get("save_scope") or "").strip().lower()
        if explicit in {"one_time", "company", "platform", "permanent", "profile", "always", "save"}:
            return explicit
        # Natural-language save intent is interpreted by the model-owned pending-reply envelope.
        # This fallback deliberately does not infer intent from words or phrases.
        return "one_time"

    def _pending_reply_context(self, question: models.OperatorQuestion) -> dict[str, Any]:
        schema = dict(question.expected_answer_schema or {})
        fields = [field for field in (schema.get("fields") or []) if isinstance(field, dict)]
        field = fields[0] if fields else {}
        options = [str(option).strip() for option in (field.get("options") or field.get("visible_options") or schema.get("options") or []) if str(option).strip()]
        final_action = schema.get("final_action") if isinstance(schema.get("final_action"), dict) else {}
        return {
            "question_id": question.id,
            "question_type": question.question_type,
            "question_text": str(field.get("label") or field.get("display_title") or question.prompt or "").strip(),
            "visible_options": options,
            "answer_contract": {
                "field_key": field.get("field_key") or schema.get("field_key") or "",
                "is_choice": bool(field.get("is_choice") or options),
                "multiple": bool(field.get("multiple")),
                # The live control's raw kind (tag/type/role), so the interpreter can tell a text box
                # whose options are only suggestions from a closed set of choices.
                "control_kind": str(field.get("control_kind") or field.get("field_kind") or "").strip()[:120],
                "proposed_answer": str(field.get("proposed_answer") or field.get("proposed_option") or "").strip(),
                "final_action": {
                    "mode": final_action.get("mode"),
                    "payment_floor": final_action.get("payment_floor"),
                    "submit_label": final_action.get("submit_label"),
                }
                if final_action
                else None,
            },
        }

    def _apply_pending_workflow_instruction_from_chat(
        self,
        thread: models.ChatThread,
        question: models.OperatorQuestion,
        *,
        content: str,
        interpretation: dict[str, Any],
        human_message_id: int | None,
    ) -> dict[str, Any]:
        """Resume the browser brain with user guidance, without turning guidance into an answer."""
        from app.extension_run import schedule_resume_if_in_browser

        run = self.db.get(models.OperatorRun, question.run_id)
        if not run:
            raise ValueError("Operator run not found")
        instruction = str(interpretation.get("workflow_instruction") or "").strip()
        if not instruction:
            return {
                "applied": False,
                "question_id": question.id,
                "question_type": question.question_type,
                "run_state": run.current_state,
                "resolved_value": None,
                "confidence": interpretation.get("confidence"),
                "rationale": "The model did not return a usable workflow instruction.",
                "save_scope": "one_time",
                "saved_common_answers": [],
                "assistant_text": "I could not safely interpret that as either an answer or a browser instruction. Please rephrase it.",
            }

        schema = dict(question.expected_answer_schema or {})
        task_ids = [int(task_id) for task_id in (schema.get("task_ids") or [])]
        if task_ids:
            tasks = self.db.scalars(
                select(models.RuntimeTask).where(
                    models.RuntimeTask.run_id == run.id,
                    models.RuntimeTask.id.in_(task_ids),
                    models.RuntimeTask.task_type == "field_approval",
                )
            ).all()
            for task in tasks:
                payload = dict(task.payload or {})
                payload["superseded_reason"] = "operator_workflow_instruction"
                payload["operator_instruction"] = instruction
                task.payload = payload
                task.status = "superseded"
                task.updated_at = utcnow()

        question.answer_payload = {
            "route": "instruction",
            "workflow_instruction": instruction,
            "raw_user_reply": content,
            "source": "human_chat",
        }
        question.status = "superseded"
        question.updated_at = utcnow()
        state = dict(run.confidence_state or {})
        instructions = [dict(item) for item in (state.get("operator_instructions") or []) if isinstance(item, dict)]
        instructions.append(
            {
                "instruction": instruction,
                "source": "human_chat",
                "message_id": human_message_id,
                "question_id": question.id,
                "created_at": utcnow().isoformat(),
            }
        )
        state["operator_instructions"] = instructions[-8:]
        run.confidence_state = state
        run.current_state = "RUNNING"
        run.status_message = "Applying your instruction with fresh candidate evidence."
        run.updated_at = utcnow()
        emit_event(
            self.db,
            event_type="OPERATOR_WORKFLOW_INSTRUCTION_APPLIED",
            payload={
                "question_id": question.id,
                "task_ids": task_ids,
                "instruction": instruction,
                "memory_written": False,
            },
            run_id=run.id,
            actor="human",
            confidence=float(interpretation.get("confidence") or 0.0),
        )
        self.db.commit()

        resumed = False
        try:
            resumed = bool(schedule_resume_if_in_browser(run.id))
        except Exception:
            resumed = False
        if not resumed:
            run = self.db.get(models.OperatorRun, run.id)
            question = self.db.get(models.OperatorQuestion, question.id)
            if run:
                state = dict(run.confidence_state or {})
                state["in_browser_resume_retry"] = {
                    "pending": True,
                    "source": "operator_workflow_instruction",
                    "question_id": int(question.id) if question else None,
                    "reason": "scheduler_declined",
                    "status_message": "Your instruction is saved for this run, but the browser body is unavailable.",
                    "updated_at": utcnow().isoformat(),
                }
                run.confidence_state = state
                run.current_state = "WAITING_FOR_USER"
                run.status_message = "Your instruction is saved for this run, but the browser body is unavailable."
                run.updated_at = utcnow()
            if question:
                question.status = "pending"
                question.updated_at = utcnow()
            self.db.commit()

        return {
            "applied": resumed,
            "question_id": question.id if question else None,
            "question_type": "workflow_instruction",
            "run_state": "RUNNING" if resumed else "WAITING_FOR_USER",
            "resolved_value": None,
            "confidence": interpretation.get("confidence"),
            "rationale": interpretation.get("rationale"),
            "memory_intent": "current_answer",
            "save_scope": "one_time",
            "saved_common_answers": [],
            "assistant_text": (
                "I treated that as a browser instruction, not as the field answer. Lumen is retrieving relevant résumé evidence and retrying the visible question now."
                if resumed
                else "I treated that as a browser instruction, not as the field answer. The browser body is unavailable, so I have not applied or stored an answer."
            ),
        }

    def _interpret_pending_user_reply(self, question: models.OperatorQuestion, content: str) -> dict[str, Any]:
        context = self._pending_reply_context(question)
        interpreted = self.lumen_model.interpret_pending_user_reply({**context, "user_reply": content})
        if interpreted.get("ok"):
            return interpreted

        # Exact binding to a backend-created option is execution/state binding, not semantic
        # interpretation. It keeps button/card answers working if the model provider is temporarily
        # unavailable, while free-form replies still fail closed instead of poisoning memory.
        answer = str(content or "").strip()
        for option in context.get("visible_options") or []:
            if answer.casefold() != str(option).casefold():
                continue
            if question.question_type == "final_action_approval":
                choices = ((question.expected_answer_schema or {}).get("final_action") or {}).get("choices") or []
                choice = next((item for item in choices if isinstance(item, dict) and str(item.get("label") or "").casefold() == answer.casefold()), None)
                if not choice:
                    break
                route = str(choice.get("decision") or "unclear")
                if route not in {"approve", "decline"}:
                    break
                return {
                    "ok": True,
                    "route": route,
                    "answer_value": option,
                    "matched_option": option,
                    "memory_intent": "current_answer",
                    "save_scope": "one_time",
                    "profile_key": "",
                    "profile_value": "",
                    "confidence": 1.0,
                    "rationale": "The user selected an exact backend-bound final-action option.",
                }
            return {
                "ok": True,
                "route": "answer",
                "answer_value": option,
                "matched_option": option,
                "memory_intent": "current_answer",
                "save_scope": "one_time",
                "profile_key": "",
                "profile_value": "",
                "confidence": 1.0,
                "rationale": "The user selected an exact visible option.",
            }
        return interpreted

    def _user_wants_to_replace_active_run(self, content: str, active_host: str) -> bool:
        """Brain-interpret whether the user means 'stop the current run and start a new application
        instead' — so natural phrasing ('start a new one here', 'apply to this instead', 'scrap it
        and do this', 'switch to this') works WITHOUT any magic keyword. The model reads intent; it
        is not a regex. No live model -> False (ask a natural question, never guess from keywords)."""
        if not getattr(self.lumen_model, "live_enabled", False):
            return False
        schema = {
            "name": "replace_active_run_intent",
            "schema": {
                "type": "object",
                "properties": {"replace": {"type": "boolean"}, "reason": {"type": "string"}},
                "required": ["replace", "reason"],
                "additionalProperties": False,
            },
        }
        prompt = {
            "instruction": (
                f"Context: an application is already running in the user's browser on {active_host}, and only ONE can run "
                "at a time. The user's latest message has already been understood as wanting to start/apply to an "
                "application. So the real question is just: do they want to start it NOW (which necessarily means stopping "
                "the current run = replace=true), or do they instead want to KEEP the current run and not start now "
                "(replace=false)?\n"
                "DEFAULT to replace=true for any genuine intent to start / apply / do / switch to / work on an application "
                "now. Only replace=false if they clearly want to keep or continue the current run, wait, save it for later, "
                "or are merely asking a question. Infer from meaning, not specific words.\n"
                "Examples: 'start a new one here'->true; 'apply to this instead'->true; 'scrap it and apply here'->true; "
                "'forget that, switch to this'->true; 'do this one'->true; 'just start'->true; "
                "'keep the current one going'->false; 'what is it doing'->false; 'pause'->false; 'wait'->false."
            ),
            "user_message": content,
        }
        try:
            result = self.lumen_model._call_model_json(purpose="replace_active_run_intent", system="You interpret the user's run-control intent. Strict JSON only.", prompt=prompt, schema=schema)
        except Exception:  # noqa: BLE001 - on any failure, fall back to asking (never a keyword guess)
            return False
        return bool(result.get("ok") and (result.get("data") or {}).get("replace"))

    def _try_start_browser_task_from_chat(
        self,
        thread: models.ChatThread,
        *,
        content: str,
        payload: dict[str, Any],
        chat_scope: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Bind one model-authored generic browser-task request to durable runtime state.

        The scope model owns whether this is a task and supplies the goal plus its intended initial
        URL. Deterministic code validates that envelope, then gives navigation authority only to the
        sole raw URL enumerated from the current human turn. The model-provided URL can establish
        target identity, but it can never rewrite the user's query string or fragment. This method
        never infers intent from URL text.
        """
        if str(chat_scope.get("action") or "none") != "start_browser_task":
            return None
        interpreted = chat_scope.get("browser_task_request")
        request = dict(interpreted) if isinstance(interpreted, dict) else {}
        execution_surface = str(request.get("execution_surface") or "browser").strip().lower()
        if execution_surface not in {"browser", "desktop"}:
            execution_surface = "browser"
        decision = str(request.get("decision") or "none")
        goal = str(request.get("goal") or "").strip()
        target_url = str(request.get("target_url") or "").strip()
        if (
            execution_surface == "browser"
            and decision == "start"
            and desktop_platform.named_other_browser(content)
            and desktop_platform.desktop_surface_usable()
        ):
            # The browser surface is the Chrome extension: a user who names Safari (or another browser app)
            # wants that app driven on the desktop (only while the desktop body can run: otherwise the model's browser
            # decision stands). The goal keeps the address verbatim.
            execution_surface = "desktop"
            target_url = ""
            goal = goal or str(content or "").strip()
        if (
            execution_surface == "browser"
            and decision == "start"
            and _is_browser_internal_url(target_url)
            and desktop_platform.desktop_surface_usable()
        ):
            # The browser surface is the Chrome extension, which can neither open nor act on the browser's own pages
            # (chrome://extensions, settings, flags): the task drives the Chrome app on the desktop instead, with the
            # address kept in the goal.
            goal = goal or str(content or "").strip()
            if target_url not in goal:
                goal = f"{goal} Start at {target_url}."
            execution_surface = "desktop"
            target_url = ""
        clarification = str(request.get("clarification") or "").strip()
        candidate_data_access = str(request.get("candidate_data_access") or "none").strip().lower()
        raw_candidate_data_scopes = (
            request.get("candidate_data_scopes")
            if isinstance(request.get("candidate_data_scopes"), list)
            else []
        )
        candidate_data_scopes: list[str] = []
        for item in raw_candidate_data_scopes:
            scope = str(item or "").strip().lower()
            if scope in {"profile", "resume", "memory"} and scope not in candidate_data_scopes:
                candidate_data_scopes.append(scope)
        if candidate_data_access != "on_demand" or not candidate_data_scopes:
            candidate_data_access = "none"
            candidate_data_scopes = []
        if decision != "start":
            return {
                "action": "browser_task_clarification_required",
                "goal": goal or None,
                "target_url": target_url or None,
                "assistant_text": clarification
                or (
                    "I need a clear target application and outcome before I can begin."
                    if execution_surface == "desktop"
                    else "I need one exact starting URL and a clear browser outcome before I can begin."
                ),
            }

        if execution_surface == "desktop":
            if target_url:
                return {
                    "action": "browser_task_clarification_required",
                    "goal": goal or None,
                    "target_url": target_url or None,
                    "assistant_text": clarification
                    or desktop_platform.os_text("A Windows desktop task must not include a browser starting URL."),
                }
            bound_target_url = "desktop://windows"
        else:
            user_urls = _enumerate_user_http_urls(content, payload)
            target_key = self._normalized_url_for_routing(target_url)
            if len(user_urls) == 1 and self._normalized_url_for_routing(user_urls[0]) == target_key:
                # The current human turn is the URL authority. `_normalized_url_for_routing`
                # deliberately compares only target identity, so always carry the exact enumerated
                # human URL forward; otherwise a model-authored query or fragment could silently
                # alter where Chrome navigates.
                bound_target_url = user_urls[0]
            else:
                # Confirmation-turn authority: during a clarification exchange the URL was often
                # proposed BY Lumen ("should I use https://site?") and the user affirms without
                # re-pasting it. The affirmation judgment belongs to the model (decision=start);
                # the deterministic guarantee is that navigation may only bind to an exact URL
                # string already rendered in this thread's recent transcript — so a model-authored
                # rewrite (host, query, or fragment the user never saw) can never survive.
                seen_urls = [] if user_urls else self._recent_thread_http_urls(thread.id)
                matching = [u for u in seen_urls if self._normalized_url_for_routing(u) == target_key]
                if matching:
                    # A URL already rendered in this thread is the strongest binding:
                    # navigation goes to the exact string the user saw, so a model
                    # rewrite of its query/fragment never survives.
                    bound_target_url = next((u for u in matching if u == target_url), matching[0])
                elif not user_urls and _is_http_url(target_url):
                    # The user named the service without a link ("use atom site").
                    # Binding that name to its canonical URL is the model's judgment,
                    # and the user's own naming is the sanction — asking to confirm the
                    # obvious URL is friction (owner call 2026-08-19). The start
                    # acknowledgment always names the site, so the user can redirect
                    # or stop; a conflict with a user-pasted link still asks below.
                    bound_target_url = target_url
                elif _is_browser_internal_url(target_url):
                    # Reached only while the desktop surface is down (otherwise the task moved there above).
                    return {
                        "action": "browser_task_clarification_required",
                        "goal": goal or None,
                        "target_url": target_url or None,
                        "assistant_text": (
                            f"{target_url} is one of the browser's own pages, which my extension can't open or use. "
                            + _desktop_unusable_fix()
                        ),
                    }
                else:
                    return {
                        "action": "browser_task_clarification_required",
                        "goal": goal or None,
                        "target_url": target_url or None,
                        "assistant_text": (
                            clarification
                            or (
                                "Your message links a site that doesn't match the one I planned "
                                "to act on — paste the one link I should start from."
                                if user_urls
                                else "Which website should I do this on? Name the site or paste its link."
                            )
                        ),
                    }

        active_run = self.db.get(models.OperatorRun, thread.run_id) if thread.run_id else None
        # BLOCKED joins the detachable set: a blocked run is a dead session the user
        # should never have to End manually — an explicit NEW task request in the same
        # thread supersedes it (the blocked run stays in the DB for review).
        if active_run and str(active_run.current_state or "").upper() in {"COMPLETED", "CANCELLED", "FAILED", "BLOCKED"}:
            previous_terminal_run_id = int(active_run.id)
            previous_terminal_state = str(active_run.current_state or "").upper()
            thread.run_id = None
            active_run = None
            emit_event(
                self.db,
                event_type="TERMINAL_CHAT_RUN_DETACHED_FOR_NEW_BROWSER_TASK",
                payload={
                    "previous_run_id": previous_terminal_run_id,
                    "previous_state": previous_terminal_state,
                },
                run_id=previous_terminal_run_id,
                application_id=None,
                actor="runtime",
                confidence=1,
                risk_level="low",
            )
        if active_run:
            pending_confirmation = self._pending_task_clarification(thread.id)
            if (
                pending_confirmation
                and int(pending_confirmation.get("replace_run_id") or 0) == int(active_run.id)
            ):
                # The previous assistant turn asked EXACTLY this stop-and-replace
                # confirmation, and the model judged the current reply as the go-ahead
                # (a fresh decision=start reached this gate). Consume the answer: stop
                # the superseded run through the real lifecycle and continue into the
                # new task — a confirmation the runtime asked for must never dead-end.
                from app.runtime import OperatorRuntimeEngine as _RuntimeEngine

                superseded_run_id = int(active_run.id)
                try:
                    _RuntimeEngine(self.db).cancel_run(
                        superseded_run_id,
                        reason="Stopped — superseded by a user-confirmed new task in this chat.",
                        notify_chat=False,
                    )
                except ValueError as exc:
                    return {
                        "action": "new_browser_task_confirmation_required",
                        "goal": goal or None,
                        "target_url": bound_target_url,
                        "run_id": active_run.id,
                        "assistant_text": str(exc),
                    }
                thread.run_id = None
                active_run = None
                emit_event(
                    self.db,
                    event_type="CHAT_RUN_SUPERSEDED_BY_CONFIRMED_NEW_TASK",
                    payload={"previous_run_id": superseded_run_id},
                    run_id=superseded_run_id,
                    application_id=None,
                    actor="human",
                    confidence=1,
                    risk_level="low",
                )
            else:
                return {
                    "action": "new_browser_task_confirmation_required",
                    "goal": goal or None,
                    "target_url": bound_target_url,
                    "run_id": active_run.id,
                    "assistant_text": (
                        f"This chat is already working run #{active_run.id}. Say yes to stop that run "
                        "and start this new task instead, or tell me to leave it running."
                    ),
                }

        from app.runtime import OperatorRuntimeEngine

        try:
            run = OperatorRuntimeEngine(self.db).create_browser_task(
                user_id=int(thread.user_id),
                goal=goal,
                target_url=bound_target_url,
                execution_surface=execution_surface,
                candidate_data_access=candidate_data_access,
                candidate_data_scopes=candidate_data_scopes,
                extra_totp_seeds=list(getattr(self, "_turn_totp_seeds", None) or []),
            )
        except ValueError:
            return {
                "action": "browser_task_clarification_required",
                "goal": goal or None,
                "target_url": bound_target_url,
                "assistant_text": clarification
                or (
                    desktop_platform.os_text("I need a clear Windows application and outcome before I can begin.")
                    if execution_surface == "desktop"
                    else "I need one exact http(s) starting URL and a clear browser outcome before I can begin."
                ),
            }
        except Exception as exc:
            return {
                "action": "browser_task_start_failed",
                "goal": goal or None,
                "target_url": bound_target_url,
                "assistant_text": f"I could not create that browser task: {str(exc) or exc.__class__.__name__}",
            }

        previous_run_id = thread.run_id
        thread.run_id = run.id
        thread.mode = "run"
        thread.title = (
            desktop_platform.os_text("Computer task: Windows")
            if execution_surface == "desktop"
            else f"Browser task: {_host_label(bound_target_url)}"
        )
        thread.updated_at = utcnow()
        try:
            model_confidence = max(0.0, min(1.0, float(chat_scope.get("confidence") or 0.8)))
        except (TypeError, ValueError):
            model_confidence = 0.8
        emit_event(
            self.db,
            event_type="LUMEN_CHAT_BROWSER_TASK_STARTED",
            payload={
                "thread_id": thread.id,
                "goal": goal,
                "target_url": bound_target_url,
                "execution_surface": execution_surface,
                "run_id": run.id,
                "previous_run_id": previous_run_id,
                "candidate_data_access": candidate_data_access,
                "candidate_data_scopes": candidate_data_scopes,
            },
            run_id=run.id,
            application_id=None,
            actor="lumen_chat",
            confidence=model_confidence,
            risk_level=run.risk_level,
        )
        return {
            "action": "browser_task_started",
            "goal": goal,
            "target_url": bound_target_url,
            "execution_surface": execution_surface,
            "candidate_data_access": candidate_data_access,
            "candidate_data_scopes": candidate_data_scopes,
            "run_id": run.id,
            "previous_run_id": previous_run_id,
            "run_state": run.current_state,
            "deferred_task_start": True,
            "assistant_text": (
                (
                    desktop_platform.os_text("I created a live Windows computer task and will start it through Lumen Desktop. ")
                    if execution_surface == "desktop"
                    else f"I created a live browser task for {_host_label(bound_target_url)} and will start it in your connected Chrome. "
                )
                + _final_action_promise(run)
            ),
        }

    def _try_start_application_from_chat(self, thread: models.ChatThread, content: str, *, payload: dict[str, Any], chat_scope: dict[str, Any]) -> dict[str, Any] | None:
        action = str(chat_scope.get("action") or "none")
        job_url = _extract_job_url(content, payload) or (self._last_detected_job_url(thread) if action == "start_application" else None)
        active_run = self.db.get(models.OperatorRun, thread.run_id) if thread.run_id else None
        pending_question = self._latest_pending_question(thread.run_id) if thread.run_id else None
        if job_url and active_run and (pending_question or str(active_run.current_state or "") in {"WAITING_FOR_USER", "BLOCKED"}):
            return {
                "action": "new_application_confirmation_required",
                "job_url": job_url,
                "run_id": active_run.id,
                "assistant_text": (
                    "I am still waiting on the current application run. "
                    "I will not switch this thread to a different URL unless you explicitly confirm you want to start a new application."
                ),
            }
        if job_url and active_run and self._normalized_url_for_routing(str(active_run.target_url or "")) != self._normalized_url_for_routing(job_url):
            return {
                "action": "new_application_confirmation_required",
                "job_url": job_url,
                "run_id": active_run.id,
                "assistant_text": (
                    "This thread is already attached to an application run. "
                    "Please confirm in a new message if you want to start a different application from this thread."
                ),
            }
        if not job_url:
            if action == "start_application":
                return {
                    "action": "application_link_needed",
                    "assistant_text": "I can start that application, but I need the job link first. Send me the posting URL and I will take it from there.",
                }
            return None
        if action != "start_application":
            if action == "ingest_job_url":
                from app.job_ingestion import JobIngestionService

                result = JobIngestionService(self.db).ingest(job_url)
                posting = result.get("job_posting")
                analysis = result.get("job_analysis")
                title = getattr(posting, "title", "") or "the job"
                company = getattr(posting, "company", "") or _host_label(job_url)
                status = getattr(analysis, "ingestion_status", None) or "analyzed"
                return {
                    "action": "job_link_ingested",
                    "job_url": job_url,
                    "job_posting_id": getattr(posting, "id", None),
                    "job_analysis_id": getattr(analysis, "id", None),
                    "ingestion_status": status,
                    "assistant_text": (
                        f"I analyzed {title} at {company}. Ingestion status: {status}. "
                        "Tell me to apply when you want me to start a governed application run."
                    ),
                }
            return {
                "action": "job_link_detected",
                "job_url": job_url,
                "assistant_text": (
                    "I found that job link. Tell me to apply when you want me to start: "
                    "I can fill the application with your profile and base resume, and stop for review before final submit."
                ),
            }
        # The user's REAL browser runs one application at a time. If a body is connected and already
        # working another run, don't create a duplicate or open a second tab — decide BEFORE creating
        # the run so the reply is a single, correct message (not an "I started…" ack followed by a
        # "busy" contradiction). If the user signalled they want to drop the current one ("end that
        # and start here"), end the active run first and continue; otherwise tell them how to free it.
        from app.extension_hub import hub as _extension_hub
        from app.extension_run import end_active_in_browser_run, in_browser_body_busy_with

        _connection = _extension_hub.get_for_thread(int(thread.id))
        if _connection is not None:
            busy = in_browser_body_busy_with(_connection.device_id, db=self.db)
            if busy:
                # No keywords: the BRAIN reads the user's actual words to decide whether they mean
                # "stop the current run and start this instead" (replace/switch). If so, end the
                # active run and continue; otherwise ask a natural question — never demand a phrase.
                if self._user_wants_to_replace_active_run(content, str(busy["host"])):
                    end_active_in_browser_run(busy["id"], db=self.db, reason="User asked to start a different application instead.")
                else:
                    return {
                        "action": "application_body_busy",
                        "job_url": job_url,
                        "active_run_id": busy["id"],
                        "assistant_text": (
                            f"You've already got an application running on {busy['host']} (run #{busy['id']}). "
                            "Want me to stop that one and start this instead? Just say so — or tell me to keep it going."
                        ),
                    }

        previous_run_id = thread.run_id
        from app.end_to_end_goal import EndToEndGoalService

        try:
            run = EndToEndGoalService(self.db).start_deferred(job_url=job_url)["run"]
        except Exception as exc:
            return {
                "action": "application_start_failed",
                "job_url": job_url,
                "assistant_text": f"I tried to start that application, but the session could not start: {str(exc) or exc.__class__.__name__}",
            }

        thread.run_id = run.id
        thread.mode = "run"
        thread.title = f"Application: {_host_label(job_url)}"
        thread.updated_at = utcnow()
        workspace = dict((run.confidence_state or {}).get("end_to_end") or {})
        emit_event(
            self.db,
            event_type="LUMEN_CHAT_APPLICATION_STARTED",
            payload={
                "thread_id": thread.id,
                "job_url": job_url,
                "run_id": run.id,
                "previous_run_id": previous_run_id,
                "phase": workspace.get("phase") or run.current_state,
            },
            run_id=run.id,
            application_id=run.application_id,
            actor="lumen_chat",
            confidence=0.9,
            risk_level="medium",
        )
        state = run.current_state or "created"
        if state == "WAITING_FOR_USER":
            next_step = "I need your review before continuing."
        elif state == "BLOCKED":
            next_step = f"I hit a blocker: {run.status_message or 'review the run details for the reason.'}"
        else:
            next_step = "I will start working through the live form now."
        return {
            "action": "application_started",
            "job_url": job_url,
            "run_id": run.id,
            "previous_run_id": previous_run_id,
            "run_state": state,
            "phase": workspace.get("phase") or state,
            "deferred_application_start": True,
            "assistant_text": (
                f"Got it. I started a new application session for {_host_label(job_url)}. "
                "I will work through the live form and ask you for anything I cannot answer. "
                f"{next_step} I will not submit without the configured review gate."
            ),
        }

    def _recent_thread_http_urls(self, thread_id: int, *, limit_messages: int = 12) -> list[str]:
        """Exact http(s) URL strings already rendered in this thread, newest message first.

        Pure enumeration, no interpretation: this list only bounds which exact URL a
        model-judged confirmation turn may bind navigation to (a string the user has
        actually seen in the conversation)."""
        rows = self.db.scalars(
            select(models.ChatMessage)
            .where(models.ChatMessage.thread_id == int(thread_id))
            .order_by(models.ChatMessage.id.desc())
            .limit(max(1, int(limit_messages)))
        ).all()
        urls: list[str] = []
        for row in rows:
            for match in re.findall(r"https?://[^\s<>()\"']+", str(row.content or "")):
                candidate = match.rstrip(".,;:!?)]}\"'")
                if _is_http_url(candidate) and candidate not in urls:
                    urls.append(candidate)
        return urls

    def _pending_task_clarification(self, thread_id: int) -> dict[str, Any] | None:
        """Durable state for a clarification Lumen itself asked last turn.

        When the immediately preceding assistant message was a browser/desktop-task
        clarification, the classifier receives that fact explicitly (goal + question it
        asked) instead of re-inferring it from raw transcript text — a short follow-up
        like a site name or a yes must be understood as the answer, not fresh chatter."""
        last_assistant = self.db.scalars(
            select(models.ChatMessage)
            .where(
                models.ChatMessage.thread_id == int(thread_id),
                models.ChatMessage.role == "assistant",
            )
            .order_by(models.ChatMessage.id.desc())
            .limit(1)
        ).first()
        if not last_assistant:
            return None
        payload = last_assistant.payload_json
        if isinstance(payload, str):
            try:
                payload = json.loads(payload or "{}")
            except (TypeError, ValueError):
                return None
        scope = payload.get("chat_scope") if isinstance(payload, dict) else None
        if not isinstance(scope, dict):
            return None
        request = scope.get("browser_task_request") if isinstance(scope.get("browser_task_request"), dict) else {}
        task_request = (
            payload.get("task_request")
            if isinstance(payload, dict) and isinstance(payload.get("task_request"), dict)
            else {}
        )
        # Three shapes are a pending clarification: the model asked (decision=ask_user),
        # the runtime demoted a start to a rendered URL-confirmation
        # (browser_task_clarification_required), or the runtime asked whether to stop
        # the thread's attached run first (new_browser_task_confirmation_required).
        task_request_action = str(task_request.get("action") or "")
        if str(scope.get("action") or "") != "start_browser_task":
            return None
        if (
            str(request.get("decision") or "") != "ask_user"
            and task_request_action
            not in {"browser_task_clarification_required", "new_browser_task_confirmation_required"}
        ):
            return None
        pending = {
            "goal": str(request.get("goal") or task_request.get("goal") or "")[:2000],
            "question": str(request.get("clarification") or last_assistant.content or "")[:500],
            "execution_surface": str(request.get("execution_surface") or "browser"),
            "proposed_target_url": str(task_request.get("target_url") or request.get("target_url") or "")[:2048],
        }
        if task_request_action == "new_browser_task_confirmation_required":
            try:
                pending["replace_run_id"] = int(task_request.get("run_id") or 0)
            except (TypeError, ValueError):
                pending["replace_run_id"] = 0
        return pending

    def _normalized_url_for_routing(self, value: str) -> str:
        raw = str(value or "").strip()
        parsed = urlparse(raw)
        if not parsed.scheme or not parsed.netloc:
            return raw.rstrip("/")
        return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"

    def _deferred_start_ack(self, run_id: int) -> str:
        run = self.db.get(models.OperatorRun, run_id)
        host = _host_label(run.target_url) if run and run.target_url else "the target site"
        is_generic_task = bool(
            run
            and getattr(run, "application_id", None) is None
            and isinstance((getattr(run, "confidence_state", None) or {}).get("browser_task"), dict)
        )
        if is_generic_task:
            task_state = (run.confidence_state or {}).get("browser_task") or {}
            if str(task_state.get("execution_surface") or "browser") == "desktop":
                return desktop_platform.os_text(
                    "Got it - I started the Windows computer task. I am working through it now and "
                    "will post updates here as I go. "
                ) + _final_action_promise(run)
            return (
                f"Got it - I started the browser task on {host}. I'm working through it now and "
                "will post updates here as I go. "
            ) + _final_action_promise(run)
        return (
            f"Got it — I started a live application session on {host}. I'm working through the form now and "
            "will post updates here as I go (and you can watch each step in the activity panel). I won't "
            "submit without the configured review gate."
        )

    def _continue_deferred_task_from_chat(self, turn: dict[str, Any]) -> None:
        request = dict(turn.get("task_request") or {})
        run_id = int(request.get("run_id") or 0)
        if not run_id or not (request.get("deferred_task_start") or request.get("deferred_application_start")):
            return

        thread = turn["thread"]
        request_model = str(turn.get("task_request_model") or "start_browser_task")
        # Live browser execution only runs through the connected extension body in the user's
        # Chrome. Missing or busy bodies are surfaced directly to the user.
        from app.extension_run import route_deferred_chat_run

        routed = route_deferred_chat_run(run_id, int(thread.id), db=self.db)
        if routed["status"] in {"in_browser", "in_desktop"}:
            turn["assistant_text"] = self._deferred_start_ack(run_id)
            turn["chat_response"] = {
                "source": "action",
                "provider": "operator_chat_service",
                "model": request_model,
                "in_browser": routed["status"] == "in_browser",
                "in_desktop": routed["status"] == "in_desktop",
            }
            return
        if routed["status"] == "busy":
            turn["assistant_text"] = routed["message"]
            turn["chat_response"] = {
                "source": "action",
                "provider": "operator_chat_service",
                "model": request_model,
                "in_browser_busy": True,
            }
            return
        if routed["status"] == "launching_browser":
            # The runtime opened Chrome itself and will adopt this run once the
            # extension body connects; the user just watches it happen.
            turn["assistant_text"] = routed["message"]
            turn["chat_response"] = {
                "source": "action",
                "provider": "operator_chat_service",
                "model": request_model,
                "browser_launching": True,
            }
            return
        if routed["status"] == "awaiting_profile_choice":
            # Which Chrome profile to use is the user's decision; the runtime enumerated
            # the options and asked. The reply flows through the pending-question path.
            turn["assistant_text"] = routed["message"]
            turn["chat_response"] = {
                "source": "action",
                "provider": "operator_chat_service",
                "model": request_model,
                "awaiting_profile_choice": True,
            }
            return
        turn["assistant_text"] = routed.get("message") or "I cannot start this browser task until the Lumen Chrome extension body is connected."
        turn["chat_response"] = {
            "source": "action",
            "provider": "operator_chat_service",
            "model": request_model,
            "in_browser_required": True,
        }

    def _answer_chrome_profile_choice_from_chat(
        self,
        thread: models.ChatThread,
        question: models.OperatorQuestion,
        content: str,
    ) -> dict[str, Any]:
        """Bind the user's profile choice to one exact enumerated option, then continue the launch.

        The choice is the user's alone (never code's); this only exact-binds their reply to
        the options they were shown — full name/directory equality first, unique name
        containment second, anything ambiguous re-asks."""
        from app.agent_settings import update_agent_settings

        schema = dict(question.expected_answer_schema or {})
        options = [dict(item) for item in (schema.get("profile_options") or []) if isinstance(item, dict)]
        reply = " ".join(str(content or "").split()).strip().casefold()
        exact = [
            item
            for item in options
            if str(item.get("name") or "").casefold() == reply
            or str(item.get("directory") or "").casefold() == reply
        ]
        contained = [
            item
            for item in options
            if str(item.get("name") or "").casefold() and str(item.get("name") or "").casefold() in reply
        ]
        matched = exact or (contained if len(contained) == 1 else [])
        run = self.db.get(models.OperatorRun, question.run_id)
        if len(matched) != 1 or not str(matched[0].get("directory") or "").strip():
            names = ", ".join(str(item.get("name") or "") for item in options)
            return {
                "applied": False,
                "question_id": question.id,
                "question_type": question.question_type,
                "run_state": getattr(run, "current_state", None),
                "resolved_value": None,
                "confidence": 0.0,
                "rationale": "reply did not bind to exactly one enumerated profile",
                "save_scope": "one_time",
                "saved_common_answers": [],
                "assistant_text": f"I couldn't match that to one Chrome profile. Reply with one of: {names}.",
            }
        choice = matched[0]
        update_agent_settings(
            self.db,
            int(run.user_id) if run else int(thread.user_id),
            {"chrome_profile_directory": str(choice.get("directory"))},
        )
        question.status = "answered"
        question.answer_payload = {
            "directory": str(choice.get("directory")),
            "name": str(choice.get("name") or ""),
        }
        question.applied_at = models.utcnow()
        self.db.flush()
        from app.extension_run import route_deferred_chat_run

        routed = route_deferred_chat_run(int(question.run_id), int(thread.id), db=self.db)
        launch_note = str(routed.get("message") or "").strip()
        return {
            "applied": True,
            "question_id": question.id,
            "question_type": question.question_type,
            "run_state": getattr(run, "current_state", None),
            "resolved_value": str(choice.get("name") or choice.get("directory")),
            "confidence": 1.0,
            "rationale": "exact profile option binding",
            "save_scope": "durable",
            "saved_common_answers": [],
            "assistant_text": (
                f"Got it — I'll use your \"{choice.get('name')}\" Chrome profile from now on. {launch_note}"
            ).strip(),
        }

    def _awaiting_state_static_reply(
        self,
        thread: models.ChatThread,
        *,
        context: dict[str, Any],
        pending_question: models.OperatorQuestion | None,
        content: str,
        chat_scope: dict[str, Any],
    ) -> str | None:
        if not thread.run_id:
            return None
        run = self.db.get(models.OperatorRun, thread.run_id)
        state = str(getattr(run, "current_state", "") or "").upper()
        if pending_question and state in {"WAITING_FOR_USER", "BLOCKED", "READY_FOR_REVIEW"}:
            return _assistant_reply(context, pending_question, content=content, chat_scope=chat_scope)
        return None

    def _chat_message_should_answer_question(self, question: models.OperatorQuestion, content: str, *, chat_scope: dict[str, Any]) -> bool:
        text = str(content or "").strip()
        lowered = text.lower()
        if not text:
            return False
        action = str(chat_scope.get("action") or "none")
        if action == "answer_question":
            return True
        if question.question_type == "final_action_approval":
            return False
        if question.question_type == "field_approval":
            if lowered.endswith("?") or re.match(r"^(what|why|how|can you check|tell me|explain)\b", lowered):
                return False
            return True
        return False

    def _try_apply_review_instruction_from_chat(
        self,
        thread: models.ChatThread,
        content: str,
        *,
        chat_scope: dict[str, Any],
        human_message_id: int | None,
    ) -> dict[str, Any] | None:
        """Reopen declined review through a model-classified correction instruction.

        The chat model decides that the message is a run-related field correction. This method
        only performs lifecycle/safety bookkeeping, records the instruction verbatim for the
        browser brain, invalidates the stale final-action binding, and resumes fresh observation.
        """

        if not thread.run_id:
            return None
        run = self.db.get(models.OperatorRun, thread.run_id)
        run_state_now = str(getattr(run, "current_state", "") or "").strip().upper()
        if (
            not run
            # A live run takes the correction too (run 72: a school-city correction sent while the run filled the
            # form was answered "Got it" and dropped, because only a run awaiting review accepted one).
            or run_state_now not in {"READY_FOR_REVIEW", "RUNNING", "WAITING_FOR_USER", "BLOCKED"}
            or not _in_browser_run(run)
            or not chat_scope.get("run_related")
            or str(chat_scope.get("primary_intent") or "") != "correction"
            or str(chat_scope.get("action") or "") != "correct_field"
        ):
            return None
        instruction = str(content or "").strip()
        if not instruction:
            return None

        state = dict(run.confidence_state or {})
        pending_final = (
            dict(state.get("pending_final_action") or {})
            if isinstance(state.get("pending_final_action"), dict)
            else {}
        )
        instructions = [
            dict(item)
            for item in (state.get("operator_instructions") or [])
            if isinstance(item, dict)
        ]
        normalized_instruction = " ".join(instruction.split()).casefold()
        instructions = [
            item
            for item in instructions
            if " ".join(str(item.get("instruction") or "").split()).casefold()
            != normalized_instruction
        ]
        instructions.append(
            {
                "instruction": instruction,
                "source": "human_review_correction",
                "message_id": human_message_id,
                "created_at": utcnow().isoformat(),
            }
        )
        state["operator_instructions"] = instructions[-8:]
        if run_state_now == "RUNNING":
            # The live loop reads the run's instructions at its next step; nothing to reopen or resume (a second
            # drive must never start beside it).
            run.confidence_state = state
            run.updated_at = utcnow()
            emit_event(
                self.db,
                event_type="LIVE_CORRECTION_RECORDED",
                payload={"instruction": instruction, "message_id": human_message_id},
                run_id=run.id,
                actor="human",
                confidence=float(chat_scope.get("confidence") or 0.0),
                risk_level="medium",
            )
            self.db.commit()
            return {
                "applied": True,
                "run_state": "RUNNING",
                "confidence": float(chat_scope.get("confidence") or 0.0),
                "rationale": str(chat_scope.get("rationale") or ""),
                "assistant_text": "Saved your correction for the running session; Lumen applies it at its next step.",
            }
        state.pop("pending_final_action", None)
        state["requires_fresh_observation"] = {
            "reason": "human_review_correction",
            "requested_at": utcnow().isoformat(),
        }
        run.confidence_state = state
        run.current_state = "RUNNING"
        run.status_message = "Applying your review correction with fresh browser evidence."
        run.updated_at = utcnow()
        emit_event(
            self.db,
            event_type="REVIEW_CORRECTION_RESUME_REQUESTED",
            payload={
                "instruction": instruction,
                "message_id": human_message_id,
                "superseded_final_action": {
                    "label": str(pending_final.get("label") or ""),
                    "question_id": pending_final.get("question_id"),
                    "status": str(pending_final.get("status") or ""),
                },
            },
            run_id=run.id,
            actor="human",
            confidence=float(chat_scope.get("confidence") or 0.0),
            risk_level="medium",
        )
        self.db.commit()

        from app.extension_run import schedule_resume_if_in_browser

        resumed = False
        resume_error = ""
        try:
            resumed = bool(schedule_resume_if_in_browser(run.id))
        except Exception as exc:  # noqa: BLE001 - preserve the committed instruction for retry
            resume_error = str(exc) or exc.__class__.__name__
        if not resumed:
            run = self.db.get(models.OperatorRun, run.id)
            if run:
                retry_state = dict(run.confidence_state or {})
                retry_state["in_browser_resume_retry"] = {
                    "pending": True,
                    "source": "human_review_correction",
                    "question_id": None,
                    "reason": resume_error or "scheduler_declined",
                    "status_message": (
                        "Your review correction is saved, but the browser body is unavailable."
                    ),
                    "updated_at": utcnow().isoformat(),
                }
                run.confidence_state = retry_state
                run.current_state = "WAITING_FOR_USER"
                run.status_message = (
                    "Your review correction is saved, but the browser body is unavailable."
                )
                run.updated_at = utcnow()
                self.db.commit()

        return {
            "applied": resumed,
            "run_state": "RUNNING" if resumed else "WAITING_FOR_USER",
            "confidence": float(chat_scope.get("confidence") or 0.0),
            "rationale": str(chat_scope.get("rationale") or ""),
            "assistant_text": (
                "I reopened the same browser run with your review correction and invalidated the old final-action approval. "
                "Lumen is re-observing the page now and will ask again only after the corrected state is verified."
                if resumed
                else "I saved your review correction and invalidated the old final-action approval, but Chrome is unavailable. Use Resume when Chrome reconnects."
            ),
        }

    def _replayed_review_instruction_scope(
        self,
        thread: models.ChatThread,
        content: str,
    ) -> dict[str, Any] | None:
        """Reuse a prior model-owned correction classification for an exact durable retry.

        This does not interpret text. It is a lifecycle recovery path keyed by exact normalized
        equality with a correction the model already classified and the runtime already persisted.
        New or changed wording still goes through the normal model scope classifier.
        """

        if not thread.run_id:
            return None
        run = self.db.get(models.OperatorRun, thread.run_id)
        if (
            not run
            or str(run.current_state or "").strip().upper() != "READY_FOR_REVIEW"
            or not _in_browser_run(run)
        ):
            return None
        normalized = " ".join(str(content or "").split()).casefold()
        if not normalized:
            return None
        matched = next(
            (
                item
                for item in reversed((run.confidence_state or {}).get("operator_instructions") or [])
                if isinstance(item, dict)
                and str(item.get("source") or "") == "human_review_correction"
                and " ".join(str(item.get("instruction") or "").split()).casefold()
                == normalized
            ),
            None,
        )
        if not matched:
            return None
        return {
            "in_scope": True,
            "action_request": True,
            "run_related": True,
            "profile_related": False,
            "resume_related": False,
            "approval_related": True,
            "out_of_scope": False,
            "primary_intent": "correction",
            "action": "correct_field",
            "confidence": 1.0,
            "rationale": "Exact retry of a durable model-classified review correction.",
            "safe_to_answer": True,
            "expose_diagnostics": False,
            "browser_task_request": {
                "execution_surface": "browser",
                "decision": "none",
                "goal": "",
                "target_url": "",
                "clarification": "",
                "candidate_data_access": "none",
                "candidate_data_scopes": [],
            },
            "metadata": {
                "provider": "durable_review_instruction_replay",
                "model": "prior_model_classification",
                "route": "exact_durable_review_instruction_replay",
                "prior_message_id": matched.get("message_id"),
            },
        }

    def _try_apply_runtime_correction(self, thread: models.ChatThread, content: str, *, payload: dict[str, Any], chat_scope: dict[str, Any]) -> dict[str, Any] | None:
        if not thread.run_id:
            return None
        if not chat_scope.get("run_related") and not self._looks_like_runtime_correction(content):
            return None
        run = self.db.get(models.OperatorRun, thread.run_id)
        if not run:
            return None
        profile_updates = self._profile_link_values_from_correction_text(content.strip())
        if not profile_updates:
            return None
        self._apply_profile_correction_updates(run, profile_updates, raw_message=content)
        corrections = [
            {
                "field_key": field_key,
                "label": field_key,
                "value": value,
                "value_category": "url",
                "profile_field": field_key,
                "confidence": 0.96,
                "rationale": f"Lumen detected a profile-link correction for {field_key}.",
            }
            for field_key, value in profile_updates.items()
        ]
        emit_event(
            self.db,
            event_type="LUMEN_CHAT_RUNTIME_CORRECTION_REQUESTED",
            payload={
                "task_ids": [],
                "corrections": [
                    {
                        "field_key": correction["field_key"],
                        "label": correction["label"],
                        "resolved_value": correction["value"],
                        "profile_field": correction["profile_field"],
                    }
                    for correction in corrections
                ],
                "profile_updates": sorted(profile_updates),
            },
            run_id=run.id,
            application_id=run.application_id,
            actor="lumen_chat",
            confidence=0.96,
            risk_level="medium" if run.current_state == "READY_FOR_REVIEW" else "low",
        )
        return {
            "applied": True,
            "task_ids": [],
            "field_keys": list(profile_updates),
            "profile_updates": profile_updates,
            "corrections": corrections,
            "confidence": 0.96,
            "rationale": "Lumen detected a user correction for profile-backed link values.",
            "run_state": run.current_state,
            "assistant_text": f"I saved the corrected profile value{'s' if len(profile_updates) != 1 else ''} for {', '.join(sorted(profile_updates))}.",
        }

    def _looks_like_runtime_correction(self, content: str) -> bool:
        lowered = str(content or "").lower()
        return bool(
            re.search(r"\b(update|correct|fix|replace|wrong|got .* wrong|didn'?t fill|these are my correct)\b", lowered)
            or re.search(r"\b(linkedin|github|gitlab|portfolio|website)\s*[:=]", lowered)
        )

    def _profile_link_values_from_correction_text(self, text: str) -> dict[str, str]:
        patterns = {
            "linkedin": r"\b(?:linkedin|linked\s*in)\s*[:=]\s*(\S+)",
            "github": r"\b(?:github|git\s*hub|gitlab|git\s*lab)\s*[:=]\s*(\S+)",
            "portfolio": r"\b(?:portfolio|website|personal\s+site|web\s+site)\s*[:=]\s*(\S+)",
        }
        values: dict[str, str] = {}
        for field_key, pattern in patterns.items():
            match = re.search(pattern, text, re.IGNORECASE)
            if not match:
                continue
            value = self._normalize_profile_link_value(field_key, match.group(1))
            if value:
                values[field_key] = value
        return values

    def _normalize_profile_link_value(self, field_key: str, value: str) -> str:
        cleaned = str(value or "").strip().strip(".,;!)]}\"'")
        if not cleaned:
            return ""
        if not re.match(r"^[a-z][a-z0-9+.-]*://", cleaned, re.IGNORECASE):
            if cleaned.startswith("www.") or "." in cleaned or "/" in cleaned:
                cleaned = f"https://{cleaned}"
            elif field_key == "github":
                cleaned = f"https://github.com/{cleaned.lstrip('@')}"
            elif field_key == "linkedin":
                cleaned = f"https://www.linkedin.com/in/{cleaned.lstrip('@')}"
        return cleaned

    def _apply_profile_correction_updates(self, run: models.OperatorRun, updates: dict[str, str], *, raw_message: str) -> None:
        """A correction to a standard profile fact ("my LinkedIn is ...") updates the profile memory."""
        for key, value in updates.items():
            if key in user_memory.PROFILE_COLUMN_KEYS and str(value or "").strip():
                user_memory.remember(run.user_id, key, str(value), source_run_id=run.id, source_note="your correction")
        user_memory.mirror_to_profile(self.db, run.user_id)

    def ensure_question_for_run(self, run_id: int | None, *, thread_id: int | None = None, announce: bool = True) -> models.OperatorQuestion | None:
        if not run_id:
            return None
        with run_lifecycle_guard(run_id):
            return self._ensure_question_for_run_serialized(
                run_id,
                thread_id=thread_id,
                announce=announce,
            )

    def _claim_live_question_run(
        self,
        run_id: int,
        *,
        reconcile_terminal: bool = True,
    ) -> models.OperatorRun | None:
        if not serialize_run_lifecycle_row(
            self.db,
            run_id,
            excluded_states=TERMINAL_RUN_STATES,
        ):
            # A concurrent terminal transition won the CAS. Discard any stale
            # identity-map state before reconciling the already-terminal run.
            self.db.rollback()
            run = self.db.get(models.OperatorRun, run_id)
            if (
                reconcile_terminal
                and run
                and str(run.current_state or "").strip().upper() in TERMINAL_RUN_STATES
            ):
                self._reconcile_terminal_run_questions(run)
            return None
        run = self.db.get(models.OperatorRun, run_id)
        if not run:
            return None
        if str(run.current_state or "").strip().upper() in TERMINAL_RUN_STATES:
            self._reconcile_terminal_run_questions(run)
            return None
        return run

    def _require_live_question_run(self, run_id: int) -> models.OperatorRun:
        run = self._claim_live_question_run(run_id, reconcile_terminal=False)
        if run is None:
            raise ValueError("This question cannot be answered because the operator run is terminal.")
        return run

    def _ensure_question_for_run_serialized(
        self,
        run_id: int,
        *,
        thread_id: int | None = None,
        announce: bool = True,
    ) -> models.OperatorQuestion | None:
        run = self._claim_live_question_run(run_id)
        if run is None:
            return None
        resolved_thread_id = self._question_thread_id(run.id, thread_id)
        self._reconcile_stale_task_questions(run.id, preferred_thread_id=resolved_thread_id)
        existing = self.db.scalars(
            select(models.OperatorQuestion).where(models.OperatorQuestion.run_id == run.id, models.OperatorQuestion.status == "pending").order_by(models.OperatorQuestion.id.desc())
        ).first()
        if existing:
            if resolved_thread_id and not existing.thread_id:
                existing.thread_id = resolved_thread_id
            return existing
        spec = self._question_spec(run)
        if not spec:
            return None
        existing_for_spec = self._pending_question_for_spec(run.id, spec)
        if existing_for_spec:
            if resolved_thread_id and not existing_for_spec.thread_id:
                existing_for_spec.thread_id = resolved_thread_id
            return existing_for_spec
        question = models.OperatorQuestion(
            run_id=run.id,
            thread_id=resolved_thread_id,
            question_type=spec["question_type"],
            stuck_phase=spec["stuck_phase"],
            stuck_reason=spec["stuck_reason"],
            prompt=spec["prompt"],
            expected_answer_schema=spec["expected_answer_schema"],
        )
        self.db.add(question)
        self.db.flush()
        workspace = dict((run.confidence_state or {}).get("end_to_end") or {})
        workspace.update(
            {
                "pending_question_id": question.id,
                "pending_question_type": question.question_type,
                "pending_question_reason": question.stuck_reason,
            }
        )
        run.confidence_state = {**(run.confidence_state or {}), "end_to_end": workspace}
        emit_event(
            self.db,
            event_type="OPERATOR_QUESTION_ASKED",
            payload={"question_id": question.id, "question_type": question.question_type, "stuck_phase": question.stuck_phase, "stuck_reason": question.stuck_reason},
            run_id=run.id,
            actor="operator_chat",
            confidence=0.86,
            risk_level="medium" if run.current_state in {"WAITING_FOR_USER", "BLOCKED"} else "low",
        )
        # announce=False suppresses the duplicate "I am stuck…" chat message when
        # the caller already emits a primary assistant reply for this turn (the
        # deferred-application chat flow). The pending-question card is driven by
        # the question record, so it still surfaces; this only avoids a redundant
        # message that also lands out of order before the turn's own reply.
        if announce and resolved_thread_id:
            thread = self._thread(resolved_thread_id)
            self._add_message(thread, role="assistant", content=question.prompt, payload={"question_id": question.id, "question_type": question.question_type})
        self.db.flush()
        return question

    def list_questions(self, run_id: int) -> list[models.OperatorQuestion]:
        # GET/list polling should surface the pending-question record/card, not append a new chat
        # transcript message. The run outcome or the originating chat turn owns user-facing prose.
        run = self.db.get(models.OperatorRun, run_id)
        state = str(getattr(run, "current_state", "") or "").strip().upper()
        pending_ids = list(
            self.db.scalars(
                select(models.OperatorQuestion.id)
                .where(
                    models.OperatorQuestion.run_id == run_id,
                    models.OperatorQuestion.status == "pending",
                )
                .limit(2)
            ).all()
        )
        question_materialization_states = {"WAITING_FOR_USER", "BLOCKED", "READY_FOR_REVIEW"}
        if state not in TERMINAL_RUN_STATES and (
            len(pending_ids) == 1
            or (not pending_ids and state not in question_materialization_states)
        ):
            # The browser loop and answer endpoint own question transitions. Most UI polls are
            # therefore read-only; avoiding a synthetic UPDATE on every GET prevents a long model
            # turn from turning harmless polling into a SQLite writer pile-up.
            return list(
                self.db.scalars(
                    select(models.OperatorQuestion)
                    .where(models.OperatorQuestion.run_id == run_id)
                    .order_by(models.OperatorQuestion.id.desc())
                ).all()
            )
        with run_lifecycle_guard(run_id):
            self._ensure_question_for_run_serialized(
                run_id,
                thread_id=self._active_thread_id_for_run(run_id),
                announce=False,
            )
            self._reconcile_stale_task_questions(run_id, preferred_thread_id=self._active_thread_id_for_run(run_id))
            self.db.commit()
            return list(self.db.scalars(select(models.OperatorQuestion).where(models.OperatorQuestion.run_id == run_id).order_by(models.OperatorQuestion.id.desc())).all())

    def _reconcile_stale_task_questions(self, run_id: int, *, preferred_thread_id: int | None = None) -> None:
        run = self.db.get(models.OperatorRun, run_id)
        if run and str(run.current_state or "").strip().upper() in TERMINAL_RUN_STATES:
            self._reconcile_terminal_run_questions(run)
            return
        self._reconcile_invalid_applied_questions(run_id)
        run = self.db.get(models.OperatorRun, run_id)
        current_spec = self._question_spec(run) if run else None
        self._reconcile_moot_pending_questions(run_id, current_spec=current_spec, preferred_thread_id=preferred_thread_id)
        questions = list(
            self.db.scalars(
                select(models.OperatorQuestion).where(
                    models.OperatorQuestion.run_id == run_id,
                    models.OperatorQuestion.status == "pending",
                    models.OperatorQuestion.question_type.in_(["field_approval", "blocked_reason"]),
                )
            ).all()
        )
        self._dedupe_pending_task_questions(questions, preferred_thread_id=preferred_thread_id)
        referenced_task_ids = sorted(
            {
                int(task_id)
                for question in questions
                for task_id in ((question.expected_answer_schema or {}).get("task_ids") or [])
            }
        )
        tasks_by_id: dict[int, models.RuntimeTask] = {}
        if referenced_task_ids:
            tasks_by_id = {
                task.id: task
                for task in self.db.scalars(
                    select(models.RuntimeTask).where(
                        models.RuntimeTask.run_id == run_id,
                        models.RuntimeTask.id.in_(referenced_task_ids),
                    )
                ).all()
            }
        task_status_by_id: dict[int, str] = {task_id: task.status for task_id, task in tasks_by_id.items()}
        for question in questions:
            if question.status != "pending":
                continue
            if preferred_thread_id and not question.thread_id:
                question.thread_id = preferred_thread_id
            # Repair a parked field card: rebuild its answer schema from the task's CURRENT payload so a
            # question that was stored with a stale (e.g. reply_text-only) schema reflects the loop's later
            # enrichment — the cover-letter card gains its upload/generate/choose controls without a new run.
            if question.question_type == "field_approval":
                self._refresh_field_approval_question_schema(question, tasks_by_id=tasks_by_id)
                refreshed_fields = [field for field in ((question.expected_answer_schema or {}).get("fields") or []) if isinstance(field, dict)]
                refreshed_task_ids = (question.expected_answer_schema or {}).get("task_ids") or []
                refreshed_task = tasks_by_id.get(int(refreshed_task_ids[0])) if refreshed_task_ids and str(refreshed_task_ids[0]).isdigit() else None
                if refreshed_task and refreshed_fields and self._choice_field_needs_internal_refresh(refreshed_task, refreshed_fields[0]):
                    self._queue_choice_option_refresh(run, refreshed_task, refreshed_fields[0], reason="choice_options_missing_or_stale_existing_question")
                    question.status = "superseded"
                    question.updated_at = utcnow()
                    payload = dict(question.answer_payload or {})
                    payload.update({"resolved_by": "choice_option_refresh_queued", "resolved_at": question.updated_at.isoformat()})
                    question.answer_payload = payload
                    continue
            task_ids = (question.expected_answer_schema or {}).get("task_ids") or []
            if not task_ids:
                continue
            int_task_ids = [int(task_id) for task_id in task_ids]
            task_statuses = [task_status_by_id.get(task_id) for task_id in int_task_ids]
            if task_statuses and all(status is not None and status != "pending" for status in task_statuses):
                question.status = "superseded"
                question.updated_at = utcnow()
        # A GENERIC blocked_reason question (no backing task/field) only mirrors the run's
        # status_message at the moment it was raised. If the run's blocker has since moved on (a
        # different status_message), that question is STALE -> supersede it so the card reflects the
        # CURRENT blocker, never a resolved one. This is what left the answered "expected salary"
        # blocker on screen while the run had moved to the next field. (Field-backed blocked_reason
        # questions are reconciled by the task-status check above, not by this.)
        current_status = str((run.status_message if run else "") or "").strip()
        if current_status:
            for question in questions:
                if question.status != "pending" or question.question_type != "blocked_reason":
                    continue
                schema = question.expected_answer_schema or {}
                if (schema.get("task_ids")) or self._field_key_from_answer_schema(schema):
                    continue  # field-backed -> handled by task status, not status_message
                if str(question.stuck_reason or "").strip() != current_status:
                    question.status = "superseded"
                    question.updated_at = utcnow()
                    payload = dict(question.answer_payload or {})
                    payload.update({"resolved_by": "question_lifecycle_reconciliation", "resolved_reason": "run_blocker_moved_on"})
                    question.answer_payload = payload
        self.db.flush()

    def _reconcile_terminal_run_questions(self, run: models.OperatorRun) -> None:
        terminal_state = str(run.current_state or "").strip().upper()
        now = utcnow()
        for question in self.db.scalars(
            select(models.OperatorQuestion).where(
                models.OperatorQuestion.run_id == run.id,
                models.OperatorQuestion.status == "pending",
            )
        ).all():
            question.status = "superseded"
            question.updated_at = now
            payload = dict(question.answer_payload or {})
            payload.update(
                {
                    "resolved_by": "terminal_run_reconciliation",
                    "resolved_reason": "run_is_terminal",
                    "terminal_state": terminal_state,
                    "resolved_at": now.isoformat(),
                }
            )
            question.answer_payload = payload

        state = dict(run.confidence_state or {})
        raw_workspace = state.get("end_to_end")
        if isinstance(raw_workspace, dict):
            workspace = dict(raw_workspace)
            for key in ("pending_question_id", "pending_question_type", "pending_question_reason"):
                workspace.pop(key, None)
            state["end_to_end"] = workspace
            run.confidence_state = state
        self.db.flush()

    def _reconcile_moot_pending_questions(self, run_id: int, *, current_spec: dict[str, Any] | None, preferred_thread_id: int | None = None) -> None:
        if not current_spec:
            return
        current_type = str(current_spec.get("question_type") or "")
        if current_type not in STATE_EXCLUSIVE_QUESTION_TYPES:
            return
        current_key = self._question_spec_dedupe_key(current_spec)
        pending = list(
            self.db.scalars(
                select(models.OperatorQuestion).where(
                    models.OperatorQuestion.run_id == run_id,
                    models.OperatorQuestion.status == "pending",
                    models.OperatorQuestion.question_type.in_(sorted(STATE_EXCLUSIVE_QUESTION_TYPES)),
                )
            ).all()
        )
        for question in pending:
            question_key = self._question_instance_dedupe_key(question)
            if question.question_type == current_type and (current_key is None or question_key == current_key):
                continue
            if preferred_thread_id and not question.thread_id:
                question.thread_id = preferred_thread_id
            question.status = "superseded"
            question.updated_at = utcnow()
            payload = dict(question.answer_payload or {})
            payload.update(
                {
                    "resolved_by": "question_lifecycle_reconciliation",
                    "resolved_reason": "run_waiting_state_changed",
                    "mooted_by_question_type": current_type,
                    "resolved_at": question.updated_at.isoformat(),
                }
            )
            question.answer_payload = payload
            emit_event(
                self.db,
                event_type="OPERATOR_QUESTION_SUPERSEDED",
                payload={"question_id": question.id, "question_type": question.question_type, "mooted_by_question_type": current_type},
                run_id=run_id,
                actor="operator_chat",
                confidence=0.84,
                risk_level="low",
            )
        self.db.flush()

    def _supersede_other_pending_field_questions(self, run_id: int, *, answered_question_id: int) -> None:
        for question in self.db.scalars(
            select(models.OperatorQuestion).where(
                models.OperatorQuestion.run_id == run_id,
                models.OperatorQuestion.status == "pending",
                models.OperatorQuestion.question_type == "field_approval",
                models.OperatorQuestion.id != answered_question_id,
            )
        ).all():
            question.status = "superseded"
            question.updated_at = utcnow()
            payload = dict(question.answer_payload or {})
            payload.update(
                {
                    "resolved_by": "fresh_observation_required_after_field_answer",
                    "resolved_at": question.updated_at.isoformat(),
                }
            )
            question.answer_payload = payload
        self.db.flush()

    def _reconcile_invalid_applied_questions(self, run_id: int) -> None:
        questions = list(
            self.db.scalars(
                select(models.OperatorQuestion).where(
                    models.OperatorQuestion.run_id == run_id,
                    models.OperatorQuestion.status == "applied",
                )
            ).all()
        )
        for question in questions:
            if question.applied_at and question.answer_payload:
                continue
            question.status = "superseded"
            question.updated_at = utcnow()

    def _field_task_schema(self, task: models.RuntimeTask) -> dict[str, Any]:
        from app.form_graph import legacy_required, normalize_requiredness

        payload = dict(task.payload or {})
        legacy_required_value = payload.get("required") if "required" in payload else None
        legacy_state = "required_proven" if legacy_required_value is True else ("optional_likely" if legacy_required_value is False else "unknown_blocking")
        req = normalize_requiredness(payload.get("requiredness") or legacy_state)
        required_projection = legacy_required(req)
        field_binding = payload.get("field_binding") if isinstance(payload.get("field_binding"), dict) else {}
        visible_options = payload.get("visible_options") if isinstance(payload.get("visible_options"), list) else payload.get("options")
        visible_options = [str(option).strip() for option in (visible_options or []) if str(option).strip()]
        is_choice = bool(payload.get("is_choice"))
        answer_modes = payload.get("answer_modes") if isinstance(payload.get("answer_modes"), list) else []
        if not answer_modes:
            if is_choice:
                answer_modes = ["choose_option", "reply_text"] if visible_options else []
            else:
                answer_modes = ["reply_text"]
        answer_modes = [str(mode).strip() for mode in answer_modes if str(mode).strip()]
        document_capability = payload.get("document_capability") if isinstance(payload.get("document_capability"), dict) else None
        artifact_options: list[dict[str, Any]] = []
        if "choose_artifact" in answer_modes:
            run = self.db.get(models.OperatorRun, task.run_id)
            if run:
                stmt = select(models.ApplicationArtifact).where(
                    models.ApplicationArtifact.user_id == run.user_id,
                    models.ApplicationArtifact.status != "deleted",
                )
                artifact_kind = normalize_artifact_role((document_capability or {}).get("artifact_role") or (document_capability or {}).get("document_kind") or "", default="")
                if artifact_kind and artifact_kind != "none":
                    stmt = stmt.where(models.ApplicationArtifact.artifact_kind == artifact_kind)
                for artifact in self.db.scalars(stmt.order_by(models.ApplicationArtifact.id.desc()).limit(12)).all():
                    metadata = artifact.metadata_json or {}
                    artifact_options.append(
                        {
                            "id": artifact.id,
                            "display_name": metadata.get("display_name") or metadata.get("name") or artifact.filename,
                            "filename": artifact.filename,
                            "artifact_kind": artifact.artifact_kind,
                            "source": artifact.source,
                            "status": artifact.status,
                            "text_preview": artifact.text_preview,
                            "metadata_json": metadata,
                            "created_at": artifact.created_at.isoformat(),
                        }
                    )
        label = str(payload.get("observed_label") or payload.get("label") or "").strip()
        control_kind = str(payload.get("control_kind") or field_binding.get("control_kind") or "").strip()
        has_proposed_answer = bool(str(payload.get("proposed_answer") or payload.get("proposed_option") or "").strip())
        confirmation_required = bool(payload.get("confirmation_required"))
        evidence_coverage = (
            dict(payload.get("evidence_coverage") or {})
            if isinstance(payload.get("evidence_coverage"), dict)
            else None
        )
        supplied_display_reason = str(payload.get("display_reason") or "").strip()[:500]
        is_document = bool(document_capability and (document_capability.get("is_document_request") or document_capability.get("is_document_field")))
        if is_document:
            question_kind = "artifact"
            field_kind = str((document_capability or {}).get("artifact_role") or (document_capability or {}).get("document_kind") or "document").replace("_", " ").strip()
            reason = f"This {'required ' if required_projection else ''}file upload needs a {field_kind or 'document'} artifact."
        elif confirmation_required and has_proposed_answer:
            question_kind = "confirm"
            field_kind = control_kind or ("choice" if is_choice else "field")
            reason = "Lumen has a suggested answer and needs your confirmation before continuing."
        elif is_choice:
            question_kind = "choice"
            field_kind = control_kind or "choice"
            reason = (
                (
                    "Choose one or more of the live options observed from the form."
                    if payload.get("multiple")
                    else "Choose one of the live options observed from the form."
                )
                if visible_options
                else "Lumen needs a live option snapshot before it can safely ask this choice question."
            )
        elif has_proposed_answer:
            question_kind = "confirm"
            field_kind = control_kind or "field"
            reason = "Lumen has a suggested answer and needs your confirmation before continuing."
        else:
            question_kind = "text"
            field_kind = control_kind or "field"
            reason = "Lumen could not confidently answer this field from your saved profile or resume."
        display_title = label if label and label.lower() != "this field" else ""
        option_snapshot_id = str(payload.get("option_snapshot_id") or payload.get("binding_key") or field_binding.get("binding_key") or payload.get("snapshot_id") or field_binding.get("snapshot_id") or "").strip()
        display_subtitle = f"{'Required' if required_projection else 'Optional'} {field_kind}".strip()
        contract_error = ""
        if question_kind == "choice":
            if not display_title:
                contract_error = "Choice question is missing a user-facing field label."
            elif not visible_options:
                contract_error = "Choice question is missing live options."
            elif not option_snapshot_id:
                contract_error = "Choice question is missing an option snapshot id."
        return {
            "id": task.id,
            "run_id": task.run_id,
            "label": label,
            "field_key": payload.get("field_key"),
            "field_ref": payload.get("field_ref") or field_binding.get("field_ref") or {},
            "element_ref": payload.get("element_ref") or field_binding.get("element_ref") or {},
            "observed_label": label,
            "control_kind": control_kind,
            "field_kind": field_kind,
            "question_kind": question_kind,
            "display_title": display_title,
            "display_subtitle": display_subtitle,
            "display_reason": supplied_display_reason or reason,
            "ask_reason": supplied_display_reason or reason,
            "option_snapshot_id": option_snapshot_id if question_kind == "choice" else None,
            "choice_option_snapshot": payload.get("choice_option_snapshot") or field_binding.get("choice_option_snapshot"),
            "group_fingerprint": payload.get("group_fingerprint") or field_binding.get("group_fingerprint"),
            "field_id": payload.get("field_id") or field_binding.get("field_id"),
            "question_contract_valid": not bool(contract_error),
            "question_contract_error": contract_error or None,
            "visible_options": visible_options,
            "options": visible_options,
            "option_refs": payload.get("option_refs") or field_binding.get("option_refs") or [],
            "options_binding_status": payload.get("options_binding_status") or field_binding.get("options_binding_status"),
            "observation_id": payload.get("observation_id") or field_binding.get("observation_id"),
            "snapshot_id": payload.get("snapshot_id") or field_binding.get("snapshot_id"),
            "created_at": payload.get("created_at") or task.created_at.isoformat(),
            "binding_key": payload.get("binding_key") or field_binding.get("binding_key"),
            "is_choice": bool(payload.get("is_choice")),
            "multiple": bool(payload.get("is_choice") and payload.get("multiple")),
            "required": required_projection,
            "requiredness": req,
            "legacy_required_projection": required_projection,
            "answer_modes": answer_modes,
            "document_capability": document_capability,
            "artifact_options": artifact_options,
            "needs_option_observation": bool(payload.get("needs_option_observation")),
            "requires_fresh_observation": bool(payload.get("requires_fresh_observation")),
            "choice_question_state": payload.get("choice_question_state"),
            "accepted_file_extensions": payload.get("accepted_file_extensions") or [],
            "interpreted_answer_preview": payload.get("interpreted_answer"),
            # Confirm-ask: the agent's best guess, redacted on the way out so the
            # Keep/Change card never exposes an OTP/secret it happened to draft.
            "proposed_answer": redact_otp_codes({"v": payload.get("proposed_answer") or ""})["v"],
            "proposed_option": redact_otp_codes({"v": payload.get("proposed_option") or ""})["v"],
            "confirmation_required": confirmation_required,
            "evidence_coverage": evidence_coverage,
            "is_unsure": bool(payload.get("is_unsure")),
        }

    def _ensure_field_task_capability(self, task: models.RuntimeTask) -> None:
        """Re-classify a parked field task's document capability ONCE when its card would otherwise be a
        plain text box. Repairs legacy tasks created before the field carried enough control metadata for
        the classifier (e.g. a cover-letter ask parked as reply_text-only). Idempotent and at most one
        model call per task (guarded by document_capability_refreshed); no site/keyword rules — the same
        model-owned classifier the runtime uses."""
        payload = dict(task.payload or {})
        answer_modes = payload.get("answer_modes") or []
        already_document = any(mode in answer_modes for mode in ("upload_file", "generate_artifact", "choose_artifact"))
        capability = payload.get("document_capability") if isinstance(payload.get("document_capability"), dict) else {}
        document_without_modes = bool(capability.get("is_document_request") or capability.get("is_document_field")) and not already_document
        if already_document or (payload.get("document_capability_refreshed") and not document_without_modes):
            return
        run = self.db.get(models.OperatorRun, task.run_id)
        if not run:
            return
        try:
            from app.runtime import OperatorRuntimeEngine

            binding = payload.get("field_binding") if isinstance(payload.get("field_binding"), dict) else {}
            mode_payload = OperatorRuntimeEngine(self.db)._field_approval_answer_mode_payload(
                run,
                field=payload,
                binding=binding or payload,
                is_choice=bool(payload.get("is_choice")),
                visible_options=[str(option).strip() for option in (payload.get("visible_options") or payload.get("options") or []) if str(option).strip()],
            )
        except Exception:  # noqa: BLE001 - capability repair must never break the question read
            payload["document_capability_refreshed"] = True
            task.payload = payload
            task.updated_at = utcnow()
            return
        payload["answer_modes"] = mode_payload.get("answer_modes") or answer_modes or ["reply_text"]
        payload["document_capability"] = mode_payload.get("document_capability")
        payload["accepted_file_extensions"] = mode_payload.get("accepted_file_extensions") or payload.get("accepted_file_extensions") or []
        payload["document_capability_refreshed"] = True
        task.payload = payload
        task.updated_at = utcnow()

    def _refresh_field_approval_question_schema(self, question: models.OperatorQuestion, *, tasks_by_id: dict[int, models.RuntimeTask]) -> None:
        """Rebuild a pending field-approval question's expected_answer_schema from its CURRENT task payload.
        Keeps the rendered card in lockstep with the task the loop keeps enriching (answer_modes, document
        capability, options) instead of the snapshot frozen when the question was first created."""
        schema = question.expected_answer_schema or {}
        task_ids = [int(task_id) for task_id in (schema.get("task_ids") or [])]
        field_tasks = [tasks_by_id.get(task_id) for task_id in task_ids]
        field_tasks = [task for task in field_tasks if task is not None and task.task_type == "field_approval" and task.status == "pending"]
        if not field_tasks:
            return
        scoped = field_tasks[:1]
        for task in scoped:
            self._ensure_field_task_capability(task)
        schema_fields = [self._field_task_schema(task) for task in scoped]
        if not schema_fields:
            return
        display_contract = {
            key: schema_fields[0].get(key)
            for key in (
                "display_title",
                "display_subtitle",
                "display_reason",
                "question_kind",
                "field_kind",
                "option_snapshot_id",
                "question_contract_valid",
                "question_contract_error",
                "confirmation_required",
                "evidence_coverage",
            )
            if schema_fields[0].get(key) is not None
        }
        new_schema = {
            **schema,
            "task_ids": [task.id for task in scoped],
            "binding_key": schema_fields[0].get("binding_key"),
            "fields": schema_fields,
            **display_contract,
        }
        if new_schema != schema:
            question.expected_answer_schema = new_schema
            question.updated_at = utcnow()

    def _choice_field_needs_internal_refresh(self, task: models.RuntimeTask, field_schema: dict[str, Any]) -> bool:
        if str(field_schema.get("question_kind") or "") != "choice" and not bool(field_schema.get("is_choice")):
            return False
        options = [str(option).strip() for option in (field_schema.get("options") or []) if str(option).strip()]
        payload = dict(task.payload or {})
        if not options:
            return True
        return bool(
            payload.get("requires_fresh_observation")
            or payload.get("needs_option_observation")
            or payload.get("options_binding_status") == "mismatched_reobserve_required"
            or field_schema.get("options_binding_status") == "mismatched_reobserve_required"
        )

    def _queue_choice_option_refresh(
        self,
        run: models.OperatorRun,
        task: models.RuntimeTask,
        field_schema: dict[str, Any],
        *,
        raw_user_answer: str | None = None,
        reason: str = "choice_options_missing_or_stale",
    ) -> None:
        payload = dict(task.payload or {})
        if raw_user_answer is not None and str(raw_user_answer).strip():
            payload["pending_raw_user_answer"] = str(raw_user_answer).strip()
            payload["pending_answer_status"] = "awaiting_choice_option_refresh"
        payload.update(
            {
                "requires_fresh_observation": True,
                "needs_option_observation": True,
                "choice_question_state": "reobserve_or_hydrate_required",
                "choice_option_refresh_reason": reason,
                "choice_option_refresh_queued_at": utcnow().isoformat(),
                "answer_modes": [],
            }
        )
        task.payload = payload
        task.updated_at = utcnow()
        workspace = dict((run.confidence_state or {}).get("end_to_end") or {})
        workspace.update(
            {
                "requires_fresh_observation": True,
                "requires_fresh_observation_reason": reason,
            }
        )
        run.confidence_state = {**(run.confidence_state or {}), "end_to_end": workspace}
        run.status_message = "Refreshing live options before asking this choice question."
        emit_event(
            self.db,
            event_type="CHOICE_OPTION_REFRESH_QUEUED",
            payload={
                "task_id": task.id,
                "field_key": payload.get("field_key"),
                "label": payload.get("label"),
                "reason": reason,
                "has_pending_raw_user_answer": bool(payload.get("pending_raw_user_answer")),
            },
            run_id=run.id,
            application_id=run.application_id,
            actor="operator_chat",
            confidence=0.86,
            risk_level="low",
        )
        try:
            from app.extension_run import schedule_resume_if_in_browser

            schedule_resume_if_in_browser(run.id)
        except Exception:  # noqa: BLE001 - reobserve scheduling is best-effort from chat reads
            pass

    def _ground_choice_text_to_visible_option(self, raw_answer: str, options: list[str]) -> str | None:
        answer = re.sub(r"\s+", " ", str(raw_answer or "")).strip()
        visible_options = [re.sub(r"\s+", " ", str(option or "")).strip() for option in options if str(option or "").strip()]
        if not answer or not visible_options:
            return None
        answer_key = answer.casefold()
        for option in visible_options:
            if option.casefold() == answer_key:
                return option
        yes_no = ""
        if re.search(r"\b(y|yes|yeah|yep|true|affirmative)\b", answer_key):
            yes_no = "yes"
        elif re.search(r"\b(n|no|nope|false)\b", answer_key):
            yes_no = "no"
        if yes_no:
            candidates = [
                option
                for option in visible_options
                if re.match(rf"^\s*{yes_no}\b", option, flags=re.IGNORECASE)
                or option.casefold() == yes_no
            ]
            if len(candidates) == 1:
                return candidates[0]
        normalized, error = normalize_answer_value(field_key="", label="", value=answer, options=visible_options)
        if normalized and not error:
            for option in visible_options:
                if option.casefold() == normalized.casefold():
                    return option
        containing = [option for option in visible_options if option.casefold() in answer_key]
        if len(containing) == 1:
            return containing[0]
        return None

    def _try_apply_pending_choice_answer(self, run: models.OperatorRun, task: models.RuntimeTask, field_schema: dict[str, Any]) -> bool:
        payload = dict(task.payload or {})
        raw_answer = str(payload.get("pending_raw_user_answer") or "").strip()
        if not raw_answer:
            return False
        if str(field_schema.get("question_kind") or "") != "choice" and not bool(field_schema.get("is_choice")):
            return False
        options = [str(option).strip() for option in (field_schema.get("options") or payload.get("options") or []) if str(option).strip()]
        grounded = self._ground_choice_text_to_visible_option(raw_answer, options)
        if not grounded:
            return False
        interpreted = {
            "resolved_value": grounded,
            "confidence": 1.0,
            "rationale": "User's pending answer was grounded to a refreshed live choice option.",
            "source": "pending_choice_answer_grounded_after_refresh",
            "needs_followup": False,
            "memory_update": {},
        }
        payload.update(
            {
                "approved_value": grounded,
                "approved_at": utcnow().isoformat(),
                "raw_user_answer": raw_answer,
                "interpreted_answer": interpreted,
                "user_choice": "change",
                "pending_answer_status": "grounded_after_choice_option_refresh",
                "choice_question_state": "ready",
                "requires_fresh_observation": False,
                "needs_option_observation": False,
            }
        )
        task.status = "approved"
        task.payload = payload
        task.updated_at = utcnow()
        workspace = dict((run.confidence_state or {}).get("end_to_end") or {})
        workspace["pending_question_id"] = None
        workspace["pending_question_type"] = None
        workspace["pending_question_reason"] = None
        workspace["requires_fresh_observation"] = True
        workspace["requires_fresh_observation_reason"] = "choice_answer_grounded_after_option_refresh"
        workspace.setdefault("auto_resolved_field_approvals", []).append({"task_ids": [task.id], "timestamp": utcnow().isoformat(), "source": "pending_choice_answer"})
        run.confidence_state = {**(run.confidence_state or {}), "end_to_end": workspace}
        run.status_message = "Field approvals updated. Lumen grounded the pending choice answer from refreshed live options. Use Resume to continue."
        emit_event(
            self.db,
            event_type="CHOICE_PENDING_ANSWER_GROUNDED",
            payload={"task_id": task.id, "field_key": payload.get("field_key"), "label": payload.get("label"), "grounded_option": grounded},
            run_id=run.id,
            application_id=run.application_id,
            actor="operator_chat",
            confidence=1.0,
            risk_level="low",
        )
        return True

    def _field_question_binding_key_from_schema(self, schema: dict[str, Any]) -> str:
        if not isinstance(schema, dict):
            return ""
        value = str(schema.get("binding_key") or "").strip()
        if value:
            return value
        fields = schema.get("fields")
        if isinstance(fields, list) and fields and isinstance(fields[0], dict):
            return str(fields[0].get("binding_key") or "").strip()
        return ""

    def _task_binding_key(self, task: models.RuntimeTask) -> str:
        payload = dict(task.payload or {})
        field_binding = payload.get("field_binding") if isinstance(payload.get("field_binding"), dict) else {}
        return str(payload.get("binding_key") or field_binding.get("binding_key") or "").strip()

    def _question_binding_stale_reason(self, question: models.OperatorQuestion, tasks: list[models.RuntimeTask]) -> str | None:
        schema = question.expected_answer_schema or {}
        fields = [field for field in schema.get("fields") or [] if isinstance(field, dict)]
        field_by_id = {int(field["id"]): field for field in fields if str(field.get("id") or "").isdigit()}
        for task in tasks:
            payload = dict(task.payload or {})
            if payload.get("requires_fresh_observation") or payload.get("options_binding_status") == "mismatched_reobserve_required":
                return "The pending question's options were not observed from the same live field. Lumen must re-observe before using your answer."
            schema_field = field_by_id.get(int(task.id))
            if not schema_field:
                continue
            schema_key = str(schema_field.get("binding_key") or "").strip()
            task_key = self._task_binding_key(task)
            if schema_key and task_key and schema_key != task_key:
                return "The pending question was created from an older field observation. Lumen must re-observe before using your answer."
        return None

    def _verbatim_visible_option_from_instruction(self, raw_answer: str, options: list[str]) -> str | None:
        answer = str(raw_answer or "").strip()
        if not answer:
            return None
        lowered = answer.lower()
        for option in options:
            clean_option = str(option).strip()
            if clean_option and clean_option.lower() == lowered:
                return clean_option
        for option in options:
            clean_option = str(option).strip()
            if not clean_option:
                continue
            pattern = rf"(?<![A-Za-z0-9]){re.escape(clean_option.lower())}(?![A-Za-z0-9])"
            if re.search(pattern, lowered):
                return clean_option
        compact_answer = re.sub(r"\s+", "", lowered)
        for option in options:
            clean_option = str(option).strip()
            compact_option = re.sub(r"\s+", "", clean_option.lower())
            if len(compact_option) >= 2 and not compact_option.isdigit() and compact_option in compact_answer:
                return clean_option
        return None

    def _pending_question_for_spec(self, run_id: int, spec: dict[str, Any]) -> models.OperatorQuestion | None:
        spec_key = self._question_spec_dedupe_key(spec)
        if spec_key is None:
            return None
        for question in self.db.scalars(
            select(models.OperatorQuestion)
            .where(
                models.OperatorQuestion.run_id == run_id,
                models.OperatorQuestion.question_type == spec.get("question_type"),
                models.OperatorQuestion.status == "pending",
            )
            .order_by(models.OperatorQuestion.id.desc())
        ).all():
            if self._question_instance_dedupe_key(question) == spec_key:
                return question
        return None

    def _dedupe_pending_task_questions(self, questions: list[models.OperatorQuestion], *, preferred_thread_id: int | None = None) -> None:
        buckets: dict[tuple[str, tuple[str, ...]], list[models.OperatorQuestion]] = {}
        for question in questions:
            if question.status != "pending":
                continue
            key = self._question_instance_dedupe_key(question)
            if key is None:
                continue
            buckets.setdefault(key, []).append(question)
        for scoped in buckets.values():
            if len(scoped) <= 1:
                continue
            keep = max(
                scoped,
                key=lambda item: (
                    1 if preferred_thread_id and item.thread_id == preferred_thread_id else 0,
                    1 if item.thread_id else 0,
                    item.id,
                ),
            )
            if preferred_thread_id and not keep.thread_id:
                keep.thread_id = preferred_thread_id
            for question in scoped:
                if question is keep:
                    continue
                question.status = "superseded"
                question.updated_at = utcnow()

    def _question_spec_dedupe_key(self, spec: dict[str, Any]) -> tuple[str, tuple[str, ...]] | None:
        question_type = str(spec.get("question_type") or "")
        schema = spec.get("expected_answer_schema") or {}
        if question_type == "field_approval":
            binding_key = self._field_question_binding_key_from_schema(schema)
            if binding_key:
                return (question_type, (f"binding:{binding_key}",))
        task_ids = tuple(sorted(str(task_id) for task_id in (schema.get("task_ids") or [])))
        if task_ids:
            return (question_type, task_ids)
        if question_type == "final_action_approval":
            return (question_type, ("run",))
        if question_type == "blocked_reason":
            field_key = self._field_key_from_answer_schema(schema)
            if field_key:
                return (question_type, (f"field:{field_key}",))
            reason_key = self._blocked_reason_dedupe_key(spec.get("stuck_reason"))
            if reason_key:
                return (question_type, (f"reason:{reason_key}",))
        return None

    def _question_instance_dedupe_key(self, question: models.OperatorQuestion) -> tuple[str, tuple[str, ...]] | None:
        schema = question.expected_answer_schema or {}
        if question.question_type == "field_approval":
            binding_key = self._field_question_binding_key_from_schema(schema)
            if binding_key:
                return (question.question_type, (f"binding:{binding_key}",))
        task_ids = tuple(sorted(str(task_id) for task_id in (schema.get("task_ids") or [])))
        if task_ids:
            return (question.question_type, task_ids)
        if question.question_type == "final_action_approval":
            return (question.question_type, ("run",))
        if question.question_type == "blocked_reason":
            field_key = self._field_key_from_answer_schema(schema)
            if field_key:
                return (question.question_type, (f"field:{field_key}",))
            reason_key = self._blocked_reason_dedupe_key(question.stuck_reason)
            if reason_key:
                return (question.question_type, (f"reason:{reason_key}",))
        return None

    def _blocked_reason_dedupe_key(self, value: Any) -> str:
        text = re.sub(r"\s+", " ", str(value or "").strip().lower())
        return text[:240]

    def _field_key_from_answer_schema(self, schema: dict[str, Any]) -> str:
        for key in ("field_key", "node_id"):
            value = str(schema.get(key) or "").strip()
            if value:
                return value
        fields = schema.get("fields")
        if isinstance(fields, list) and fields:
            field = fields[0]
            if isinstance(field, dict):
                return str(field.get("field_key") or field.get("node_id") or field.get("id") or "").strip()
        return ""

    def answer_question(
        self,
        *,
        question_id: int,
        answer: str,
        payload: dict[str, Any] | None = None,
        auto_resume: bool = True,
        record_user_message: bool = True,
        structured_final_action: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        question = self.db.get(models.OperatorQuestion, question_id)
        if not question:
            raise ValueError("Operator question not found")
        with run_lifecycle_guard(question.run_id):
            return self._answer_question_serialized(
                question_id=question_id,
                answer=answer,
                payload=payload,
                auto_resume=auto_resume,
                record_user_message=record_user_message,
                structured_final_action=structured_final_action,
            )

    def _answer_question_serialized(
        self,
        *,
        question_id: int,
        answer: str,
        payload: dict[str, Any] | None = None,
        auto_resume: bool = True,
        record_user_message: bool = True,
        structured_final_action: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        question = self.db.get(models.OperatorQuestion, question_id)
        if not question:
            raise ValueError("Operator question not found")
        run = self._require_live_question_run(question.run_id)
        self.db.refresh(question)
        # An answer may carry a TOTP seed too: scrubbed before it reaches the question row, events, or the thread.
        answer = self._protect_user_text(answer, run_id=question.run_id)
        payload = self._protect_user_payload(payload or {}, run_id=question.run_id) if payload else payload
        if structured_final_action is not None and question.question_type != "final_action_approval":
            raise ValueError("A final-action token cannot answer a different question type.")
        if question.question_type == "final_action_approval" and str(question.status or "").strip().lower() != "pending":
            raise ValueError("This final-action approval question is no longer active.")
        # Capture the handoff origin before applying the answer. Applying a field approval can
        # legitimately advance other runtime bookkeeping (including the visible run lifecycle),
        # but it must not erase the fact that this pending human turn owns one browser resume.
        # This is generic orchestration state, not interpretation of the field or answer.
        question_status_before = str(question.status or "").strip().lower()
        run_state_before = str(run.current_state or "").strip().upper()
        raw_answer_payload = {"answer": answer, **(payload or {})}
        if question.question_type == "final_action_approval":
            raw_answer_payload = self._prepare_final_action_answer_payload(
                question,
                raw_answer_payload,
                structured_final_action=structured_final_action,
            )
        answer_payload = redact_otp_codes(raw_answer_payload)
        question.answer_payload = answer_payload
        question.status = "answered"
        question.updated_at = utcnow()
        thread = self._thread(question.thread_id) if question.thread_id else None
        if thread and record_user_message:
            self._add_message(
                thread,
                role="user",
                content=answer,
                payload={"question_id": question.id, "answer_payload": answer_payload},
            )
        applied = self._apply_answer(run, question, answer_payload)
        # Some retained answer handlers commit their own durable task update. Reclaim
        # the live row before applying any later question/run mutation so a terminal
        # cancellation that won between those transactions cannot be overwritten.
        run = self._require_live_question_run(run.id)
        # No per-answer memory blob: the answer already lives on the question row and in
        # the event stream, and durable user facts flow through user_memory (markdown)
        # via _save_common_answer. Duplicating every operator answer into memory_items
        # produced hundreds of write-only rows.
        if applied.get("applied"):
            question.status = "applied"
            question.applied_at = utcnow()
        elif question.question_type == "final_action_approval":
            question.status = "pending"
            question.applied_at = None
            question.answer_payload = {}
        else:
            question.status = "answered"
            question.applied_at = None
        resumed = None
        resume_error = None
        # In-browser runs (driving the user's real Chrome via the extension) resume through
        # schedule_resume_if_in_browser only after the answer and memory transaction commits. They must NOT take
        # the controlled-live runtime path below, which uses the Playwright adapter and is blocked by
        # the external-execution mode guard (external_site_not_allowlisted, real_ingested_job_required).
        in_browser_run = _in_browser_run(run)
        run_state_after_apply = str(run.current_state or "").strip().upper()
        resumable_human_turn = question_status_before in {"pending", "answered"}
        terminal_after_apply = run_state_after_apply in {"CANCELLED", "COMPLETED", "FAILED"}
        in_browser_resume_requested = bool(
            auto_resume
            and applied.get("can_resume")
            and applied.get("resume_in_browser_requested")
            and in_browser_run
            and resumable_human_turn
            and not terminal_after_apply
        )
        resume_requested = bool(auto_resume and applied.get("can_resume") and run.current_state == "WAITING_FOR_USER" and not in_browser_run)
        if in_browser_resume_requested:
            run.current_state = "RUNNING"
            run.status_message = "Applying your answer in the browser."
            run.updated_at = utcnow()
            emit_event(
                self.db,
                event_type="OPERATOR_CHAT_RESUME_REQUESTED",
                payload={
                    "question_id": question.id,
                    "runtime": "in_browser",
                    "run_state_before_answer": run_state_before,
                    "run_state_after_apply": run_state_after_apply,
                },
                run_id=run.id,
                actor="operator_chat",
                confidence=0.9,
            )
            self.db.commit()
            run = self._require_live_question_run(run.id)
            # Commit before scheduling: the live-run claim above holds the run row's write lock on this
            # session, and the resume may write the rebuilt body binding from its own session.
            self.db.commit()
            try:
                from app.extension_run import schedule_resume_if_in_browser

                applied["resumed"] = bool(schedule_resume_if_in_browser(run.id))
            except Exception as exc:  # noqa: BLE001 - keep the committed answer and report scheduling failure
                applied["resumed"] = False
                resume_error = str(exc) or exc.__class__.__name__
            if not applied.get("resumed"):
                run = self.db.get(models.OperatorRun, run.id)
                if run:
                    state = dict(run.confidence_state or {})
                    state["in_browser_resume_retry"] = {
                        "pending": True,
                        "source": "operator_answer",
                        "question_id": int(question.id),
                        "reason": resume_error or "scheduler_declined",
                        "status_message": "Your answer was saved, but the browser body was unavailable to resume.",
                        "updated_at": utcnow().isoformat(),
                    }
                    run.confidence_state = state
                    run.current_state = "WAITING_FOR_USER"
                    run.status_message = "Your answer was saved, but the browser body was unavailable to resume."
                    run.updated_at = utcnow()
                emit_event(
                    self.db,
                    event_type="OPERATOR_CHAT_RESUME_BLOCKED",
                    payload={
                        "question_id": question.id,
                        "runtime": "in_browser",
                        "reason": resume_error or "scheduler_declined",
                        "run_state_before_answer": run_state_before,
                        "run_state_after_apply": run_state_after_apply,
                    },
                    run_id=question.run_id,
                    actor="operator_chat",
                    confidence=0.7,
                    risk_level="medium",
                )
                self.db.commit()
                run = self._require_live_question_run(run.id)
        if resume_requested:
            emit_event(self.db, event_type="OPERATOR_CHAT_RESUME_REQUESTED", payload={"question_id": question.id}, run_id=run.id, actor="operator_chat", confidence=0.82)
            self.db.commit()
            run = self._require_live_question_run(run.id)
        if resume_requested:
            from app.runtime import OperatorRuntimeEngine

            try:
                resumed = OperatorRuntimeEngine(self.db).resume_run(run.id)
                run = self._require_live_question_run(run.id)
                question.resumed_at = utcnow()
            except Exception as exc:
                resume_error = str(exc) or exc.__class__.__name__
                emit_event(self.db, event_type="OPERATOR_CHAT_RESUME_BLOCKED", payload={"question_id": question.id, "error": str(exc)}, run_id=run.id, actor="operator_chat", confidence=0.55, risk_level="medium")
        emit_event(
            self.db,
            event_type="OPERATOR_USER_ANSWER_RECEIVED",
            payload={"question_id": question.id, "question_type": question.question_type},
            run_id=run.id,
            actor="human",
            confidence=1,
        )
        # In-browser runs resume via schedule_resume_if_in_browser (the controlled-live resume_run is
        # skipped for them), so the real "did it resume" signal is applied["resumed"].
        resumed_flag = bool(resumed) or bool(applied.get("resumed"))
        if resumed_flag and question.resumed_at is None:
            question.resumed_at = utcnow()
        if resumed_flag:
            state = dict(run.confidence_state or {})
            retry_state = (
                dict(state.get("in_browser_resume_retry") or {})
                if isinstance(state.get("in_browser_resume_retry"), dict)
                else {}
            )
            if retry_state:
                retry_state.update(
                    {
                        "pending": False,
                        "resumed_at": utcnow().isoformat(),
                    }
                )
                state["in_browser_resume_retry"] = retry_state
                run.confidence_state = state
        from app.extension_run import run_surface

        routing_state = dict(run.confidence_state or {})
        emit_event(
            self.db,
            event_type="OPERATOR_ANSWER_APPLIED",
            payload={
                "question_id": question.id,
                "applied": applied,
                "resumed": resumed_flag,
                # What the reply routing could see: the transient marker and the durable surface.
                "routing": {
                    "in_browser_marker": isinstance(routing_state.get("in_browser"), dict),
                    "durable_surface": run_surface(routing_state),
                    "run_state_before_answer": run_state_before,
                },
            },
            run_id=run.id,
            actor="operator_chat",
            confidence=0.84 if applied.get("applied") else 0.55,
        )
        self._require_live_question_run(run.id)
        self.db.commit()
        first_interpretation = (applied.get("interpretations") or [{}])[0]
        return {
            "question": question,
            "applied": applied,
            "run": resumed or run,
            "resolved_value": first_interpretation.get("resolved_value"),
            "confidence": first_interpretation.get("confidence"),
            "rationale": first_interpretation.get("rationale"),
            "can_resume": bool(applied.get("can_resume")),
            "resumed": resumed_flag,
            "resume_error": resume_error,
            "saved_common_answers": applied.get("saved_common_answers", []),
            "memory_receipts": applied.get("memory_receipts", []),
            "field_receipts": applied.get("field_receipts", []),
        }

    def _question_spec(self, run: models.OperatorRun) -> dict[str, Any] | None:
        if str(run.current_state or "").strip().upper() in TERMINAL_RUN_STATES:
            return None
        run_state = dict(run.confidence_state or {})
        sensitive_takeover = (
            dict(run_state.get("sensitive_browser_takeover") or {})
            if isinstance(run_state.get("sensitive_browser_takeover"), dict)
            else {}
        )
        if run.current_state == "WAITING_FOR_USER" and sensitive_takeover.get("active"):
            # Direct browser entry is an informational handoff, not an answerable question.
            # Polling/listing questions must not manufacture a blocked-reason question whose
            # reply could route a secret into field tasks or answer memory.
            return None
        resume_retry = (
            dict(run_state.get("in_browser_resume_retry") or {})
            if isinstance(run_state.get("in_browser_resume_retry"), dict)
            else {}
        )
        if run.current_state == "WAITING_FOR_USER" and resume_retry.get("pending"):
            # The human answer/instruction already committed. This is a reconnectable body
            # lifecycle state, not a request for another fact and not a new semantic question.
            return None
        workspace = dict((run.confidence_state or {}).get("end_to_end") or {})
        pending_final_action = dict((run.confidence_state or {}).get("pending_final_action") or {})
        if str(pending_final_action.get("status") or "") in {"approved", "executing"}:
            return None
        if str(pending_final_action.get("status") or "") in {"awaiting_submit_approval", "awaiting_payment_confirmation"}:
            payment = bool(pending_final_action.get("payment"))
            options = ["Confirm and submit", "Leave it for my review"] if payment else ["Submit", "Leave it for my review"]
            target_label = re.sub(r"\s+", " ", str(pending_final_action.get("label") or "final action")).strip()[:120]
            prompt = (
                f"{str(pending_final_action.get('payment_summary') or 'This final action may involve a payment or booking.').strip()} "
                "Should Lumen submit using your default payment method?"
                if payment
                else str(pending_final_action.get("approval_prompt") or "").strip()
                or f'Lumen reached the final action and paused for your approval. Should I execute "{target_label}" now?'
            )
            return {
                "question_type": "final_action_approval",
                "stuck_phase": "FINAL_ACTION",
                "stuck_reason": "Lumen needs explicit final-action confirmation.",
                "prompt": prompt,
                "expected_answer_schema": {
                    "options": options,
                    "question_type": "final_action_approval",
                    "final_action": {
                        "mode": pending_final_action.get("mode") or "approval",
                        "configured_mode": pending_final_action.get("configured_mode") or pending_final_action.get("mode") or "approval",
                        "autonomy_downgraded": bool(pending_final_action.get("autonomy_downgraded")),
                        "payment_floor": payment,
                        "summary": pending_final_action.get("payment_summary") or "",
                        "submit_label": pending_final_action.get("label") or "",
                        "approval_token": pending_final_action.get("approval_token") or "",
                        "choices": [
                            {"label": label, "decision": "approve" if index == 0 else "decline"}
                            for index, label in enumerate(options)
                        ],
                    },
                },
            }
        tasks = list(
            self.db.scalars(select(models.RuntimeTask).where(models.RuntimeTask.run_id == run.id, models.RuntimeTask.status == "pending").order_by(models.RuntimeTask.id.asc())).all()
        )
        field_tasks = [task for task in tasks if task.task_type == "field_approval"]
        if field_tasks:
            scoped_field_tasks = field_tasks[:1]
            schema_fields = [self._field_task_schema(task) for task in scoped_field_tasks]
            if schema_fields and self._try_apply_pending_choice_answer(run, scoped_field_tasks[0], schema_fields[0]):
                return None
            if schema_fields and self._choice_field_needs_internal_refresh(scoped_field_tasks[0], schema_fields[0]):
                self._queue_choice_option_refresh(run, scoped_field_tasks[0], schema_fields[0], reason="choice_options_missing_or_stale_before_question")
                return None
            display_contract = {
                key: schema_fields[0].get(key)
                for key in (
                    "display_title",
                    "display_subtitle",
                    "display_reason",
                    "question_kind",
                    "field_kind",
                    "option_snapshot_id",
                    "question_contract_valid",
                    "question_contract_error",
                    "confirmation_required",
                    "evidence_coverage",
                )
                if schema_fields and schema_fields[0].get(key) is not None
            }
            return {
                "question_type": "field_approval",
                "stuck_phase": "APPLICATION_EXECUTION",
                "stuck_reason": "Low-confidence field mappings require your answer or approval.",
                "prompt": "I found application fields that need your confirmation before I continue. Please provide the correct answer or approve the suggested values.",
                "expected_answer_schema": {
                    "task_ids": [task.id for task in scoped_field_tasks],
                    "binding_key": schema_fields[0].get("binding_key") if schema_fields else None,
                    "fields": schema_fields,
                    **display_contract,
                },
            }
        if run.current_state == "WAITING_FOR_USER" and (
            str(run.status_message or "").startswith("Field approvals updated.")
            or "lumen is waiting for your answer before execution can resume" in str(run.status_message or "").lower()
        ):
            return None
        if run.current_state == "BLOCKED" and workspace.get("system_block"):
            return None
        if run.current_state == "BLOCKED" and str(workspace.get("block_kind") or "") == "awaiting_engineering":
            return None
        pause_state = (run.confidence_state or {}).get("user_pause") if isinstance(run.confidence_state, dict) else None
        if run.current_state == "WAITING_FOR_USER" and isinstance(pause_state, dict) and pause_state.get("active"):
            # A user pause is session control, not a request for a missing fact. Specific field,
            # approval, OTP, and recovery questions above remain visible; only the generic fallback
            # card is suppressed.
            return None
        if run.current_state in {"WAITING_FOR_USER", "BLOCKED"}:
            return {
                "question_type": "blocked_reason",
                "stuck_phase": workspace.get("phase") or run.current_state,
                "stuck_reason": run.status_message,
                "prompt": f"I am stuck because: {run.status_message}. Tell me the missing fact, correction, or instruction to store before I continue.",
                "expected_answer_schema": {"answer": "string", "memory_update": "optional object"},
            }
        return None

    def _prepare_final_action_answer_payload(
        self,
        question: models.OperatorQuestion,
        answer_payload: dict[str, Any],
        *,
        structured_final_action: dict[str, Any] | None,
    ) -> dict[str, Any]:
        prepared = dict(answer_payload or {})
        schema_final = ((question.expected_answer_schema or {}).get("final_action") or {})
        prepared.pop("final_action_decision", None)
        prepared.pop("approval_token", None)
        prepared.pop("human_authorization", None)
        if not isinstance(structured_final_action, dict):
            raise ValueError(
                "Final actions must be answered with the exact rendered approval control; free-form chat cannot authorize them."
            )
        decision = str(structured_final_action.get("decision") or "").strip().lower()
        approval_token = str(structured_final_action.get("approval_token") or "").strip()
        if decision not in {"approve", "decline"} or not approval_token:
            raise ValueError("The structured final-action decision is incomplete.")
        answer = str(prepared.get("answer") or "").strip()
        choices = [item for item in (schema_final.get("choices") or []) if isinstance(item, dict)]
        choice = next((item for item in choices if str(item.get("label") or "").strip().casefold() == answer.casefold()), None)
        bound_decision = str((choice or {}).get("decision") or "").strip().lower()
        if bound_decision != decision:
            raise ValueError("The selected final-action choice does not match the rendered decision.")
        prepared["final_action_decision"] = decision
        prepared["approval_token"] = approval_token
        prepared["human_authorization"] = {
            "source": "question_answer_endpoint",
            "question_id": question.id,
            "thread_id": question.thread_id,
        }
        return prepared

    def _apply_final_action_approval_answer(self, run: models.OperatorRun, question: models.OperatorQuestion, answer_payload: dict[str, Any]) -> dict[str, Any]:
        state = dict(run.confidence_state or {})
        pending = dict(state.get("pending_final_action") or {})
        if not pending:
            return {"applied": False, "target": "final_action", "can_resume": False, "reason": "No pending final action was recorded."}
        schema_final = ((question.expected_answer_schema or {}).get("final_action") or {})
        pending_status = str(pending.get("status") or "")
        if pending_status not in {"awaiting_submit_approval", "awaiting_payment_confirmation"}:
            return {
                "applied": False,
                "target": "final_action",
                "can_resume": False,
                "reason": f"The pending final action is not awaiting approval (status={pending_status or 'missing'}).",
            }
        expected_question_id = pending.get("question_id")
        expected_token = str(pending.get("approval_token") or "")
        schema_token = str(schema_final.get("approval_token") or "")
        request_token = str(answer_payload.get("approval_token") or "")
        if (
            expected_question_id != question.id
            or not expected_token
            or request_token != expected_token
            or schema_token != expected_token
        ):
            return {"applied": False, "target": "final_action", "can_resume": False, "reason": "The approval is stale or does not match the exact pending final action."}
        decision = str(answer_payload.get("final_action_decision") or "").strip().lower()
        authorization = answer_payload.get("human_authorization") if isinstance(answer_payload.get("human_authorization"), dict) else {}
        if (
            decision not in {"approve", "decline"}
            or str(authorization.get("source") or "") != "question_answer_endpoint"
            or authorization.get("question_id") != question.id
        ):
            return {"applied": False, "target": "final_action", "can_resume": False, "reason": "No backend-bound human final-action decision was recorded."}
        authorization_record = {
            "source": str(authorization.get("source") or ""),
            "question_id": question.id,
            "thread_id": authorization.get("thread_id") or question.thread_id,
            "message_id": authorization.get("message_id"),
            "recorded_at": utcnow().isoformat(),
        }
        if decision == "decline":
            pending.update(
                {
                    "status": "declined",
                    "declined_at": utcnow().isoformat(),
                    "authorization": authorization_record,
                }
            )
            state["pending_final_action"] = pending
            run.confidence_state = state
            run.current_state = "READY_FOR_REVIEW"
            run.status_message = "The task is ready for your review. Lumen did not perform the final action."
            run.updated_at = utcnow()
            emit_event(
                self.db,
                event_type="FINAL_ACTION_CONFIRMATION_RECORDED",
                payload={"decision": "declined", "question_id": question.id, "payment": bool(pending.get("payment"))},
                run_id=run.id,
                actor="operator_chat",
                confidence=0.93,
            )
            return {"applied": True, "target": "final_action", "can_resume": False, "declined": True}
        pending.update(
            {
                "status": "approved",
                "approved_at": utcnow().isoformat(),
                "authorization": authorization_record,
            }
        )
        state["pending_final_action"] = pending
        run.confidence_state = state
        run.current_state = "WAITING_FOR_USER"
        run.status_message = "Final action approved. Lumen is executing the exact approved browser action."
        run.updated_at = utcnow()
        emit_event(
            self.db,
            event_type="FINAL_ACTION_CONFIRMATION_RECORDED",
            payload={"decision": "approved", "question_id": question.id, "payment": bool(pending.get("payment"))},
            run_id=run.id,
            actor="operator_chat",
            confidence=0.93,
        )
        from app.extension_run import schedule_resume_if_in_browser

        resumed = schedule_resume_if_in_browser(run.id)
        return {"applied": True, "target": "final_action", "can_resume": True, "resumed": resumed}

    def _apply_answer(self, run: models.OperatorRun, question: models.OperatorQuestion, answer_payload: dict[str, Any]) -> dict[str, Any]:
        if question.question_type == "final_action_approval":
            return self._apply_final_action_approval_answer(run, question, answer_payload)
        if question.question_type == "field_approval":
            return self._apply_field_approval_answer(run, question, answer_payload)
        if question.question_type == "blocked_reason":
            field_answer = self._apply_field_backed_blocked_reason_answer(run, question, answer_payload)
            if field_answer:
                return field_answer
        workspace = dict((run.confidence_state or {}).get("end_to_end") or {})
        workspace.setdefault("chat_answers", []).append({"question_id": question.id, "question_type": question.question_type, "answer": answer_payload, "timestamp": utcnow().isoformat()})
        run.confidence_state = {**(run.confidence_state or {}), "end_to_end": workspace}
        answer_text = str(answer_payload.get("answer") or "").strip()
        if (
            question.question_type == "blocked_reason"
            and answer_text
            and _in_browser_run(run)
            and str(run.current_state or "").strip().upper() == "WAITING_FOR_USER"
        ):
            # A reply to a stuck in-browser run is a human turn for the brain: it reads the reply as an
            # operator instruction and the loop resumes. Stored only in the workspace, a reply such as
            # "continue" left the run waiting forever (run 1791). The body is checked where the resume
            # is scheduled, so a reply that lands while the body is away is kept for retry, never filed
            # as a workspace note.
            state = dict(run.confidence_state or {})
            instructions = [dict(item) for item in (state.get("operator_instructions") or []) if isinstance(item, dict)]
            instructions.append(
                {
                    "instruction": answer_text[:500],
                    "source": "human_chat",
                    "question_id": question.id,
                    "created_at": utcnow().isoformat(),
                }
            )
            state["operator_instructions"] = instructions[-8:]
            run.confidence_state = state
            return {
                "applied": True,
                "target": "operator_instruction",
                "can_resume": True,
                "resume_in_browser_requested": True,
            }
        return {"applied": True, "target": "goal_workspace", "can_resume": False}

    def _skip_field_approval(
        self,
        run: models.OperatorRun,
        question: models.OperatorQuestion,
        tasks: list[models.RuntimeTask],
        task_ids: list[Any],
        *,
        scope: str = "application",
        raw_answer: str = "",
        rationale: str = "",
    ) -> dict[str, Any]:
        """User chose to skip the field(s): resolve the question without filling.
        Plain skip is current-application only; always-skip persists for future runs. The form's own validation can still bring it back (a flagged-
        required field re-surfaces on the next observation). We never submit to discover that — we read
        the validation error when it appears."""
        from app.runtime import OperatorRuntimeEngine

        scope = "always" if str(scope or "").lower() == "always" else "application"
        skipped_labels: list[str] = []
        saved_common_answers: list[dict[str, Any]] = []
        memory_receipts: list[dict[str, Any]] = []
        interpretations: list[dict[str, Any]] = []
        for task in tasks:
            payload = dict(task.payload or {})
            label = str(payload.get("label") or task.name or "this field").strip()
            interpreted_payload = {
                "resolved_value": SKIP_ANSWER_TEXT,
                "confidence": 1.0,
                "rationale": rationale or "User asked Lumen to skip this field.",
                "source": "user_skip_preference",
                "needs_followup": False,
                "memory_update": {},
                "answer_kind": "skip",
                "scope": scope,
            }
            payload["skipped"] = True
            payload["user_choice"] = "skip"
            payload["answer_kind"] = "skip"
            payload["skip_scope"] = scope
            payload["raw_user_answer"] = raw_answer
            payload["interpreted_answer"] = interpreted_payload
            task.payload = payload
            task.updated_at = utcnow()
            if label:
                skipped_labels.append(label)
            if scope == "always":
                saved = self._save_skip_answer_memory(run, task, question_id=question.id)
                if saved:
                    saved_common_answers.append(saved)
                    memory_receipts.append(self._skip_memory_write_receipt(run, task))
            interpretations.append({**interpreted_payload, "task_id": task.id, "field_key": payload.get("field_key")})
        # Mark the tasks resolved + advance the epoch so the loop re-observes fresh.
        OperatorRuntimeEngine(self.db).approve_fields(run_id=run.id, task_ids=[int(t) for t in task_ids], approved=True, values={})
        self._supersede_other_pending_field_questions(run.id, answered_question_id=question.id)
        return {
            "applied": True,
            "skipped": True,
            "skipped_labels": skipped_labels,
            "scope": scope,
            "target": "field_approval_tasks",
            "task_ids": task_ids,
            "can_resume": True,
            "resumed": False,
            "resume_in_browser_requested": True,
            "interpretations": interpretations,
            "saved_common_answers": saved_common_answers,
            "memory_receipts": memory_receipts,
            "field_receipts": [],
        }

    @staticmethod
    def _picked_multi_select_options(
        task_payload: dict[str, Any],
        visible_options: list[str],
        answer_payload: dict[str, Any],
    ) -> list[str]:
        """The options a multi-select answer picked, in the field's option order: from a card's
        selected_options or the reply interpreter's matched_options. Each must be one of the field's
        live options; anything else (or a single-choice field) yields [] and the ordinary path."""
        if not (task_payload.get("is_choice") and task_payload.get("multiple")) or not visible_options:
            return []
        raw = answer_payload.get("selected_options")
        reply = answer_payload.get("reply_interpretation")
        if not isinstance(raw, list) and isinstance(reply, dict) and reply.get("route") == "answer":
            raw = reply.get("matched_options")
        if not isinstance(raw, list):
            return []
        by_fold = {option.casefold(): option for option in visible_options}
        picked = {by_fold.get(str(option or "").strip().casefold()) for option in raw}
        if not raw or None in picked:
            return []
        return [option for option in visible_options if option in picked]

    def _apply_field_approval_answer(self, run: models.OperatorRun, question: models.OperatorQuestion, answer_payload: dict[str, Any]) -> dict[str, Any]:
        from app.runtime import OperatorRuntimeEngine

        task_ids = answer_payload.get("task_ids") or (question.expected_answer_schema or {}).get("task_ids") or []
        int_task_ids = [int(task_id) for task_id in task_ids]
        tasks = list(
            self.db.scalars(
                select(models.RuntimeTask).where(
                    models.RuntimeTask.run_id == run.id,
                    models.RuntimeTask.task_type == "field_approval",
                    models.RuntimeTask.id.in_(int_task_ids),
                )
            ).all()
        )
        stale_reason = self._question_binding_stale_reason(question, tasks)
        if stale_reason:
            choice_rehydrate_queued = False
            for task in tasks:
                task_payload = dict(task.payload or {})
                field_schema = self._field_task_schema(task)
                if self._choice_field_needs_internal_refresh(task, field_schema):
                    choice_rehydrate_queued = True
                    self._queue_choice_option_refresh(
                        run,
                        task,
                        field_schema,
                        raw_user_answer=str(answer_payload.get("answer") or ""),
                        reason="choice_answer_requires_fresh_options",
                    )
                    continue
                task_payload["stale_reobserve_required"] = True
                task_payload["stale_reason"] = stale_reason
                task.payload = task_payload
                task.updated_at = utcnow()
            question.status = "superseded"
            question.updated_at = utcnow()
            try:
                from app.extension_run import schedule_resume_if_in_browser

                schedule_resume_if_in_browser(run.id)
            except Exception:  # noqa: BLE001 - best-effort re-observe request
                pass
            self.db.flush()
            return {
                "applied": False,
                "target": "field_approval_tasks",
                "task_ids": task_ids,
                "can_resume": choice_rehydrate_queued,
                "choice_rehydrate_queued": choice_rehydrate_queued,
                "interpretations": [],
                "saved_common_answers": [],
                "memory_receipts": [],
                "field_receipts": [],
                "reason": stale_reason,
            }
        if answer_payload.get("skip"):
            skip_scope = "always" if str(answer_payload.get("skip_scope") or answer_payload.get("scope") or "").lower() == "always" else "application"
            return self._skip_field_approval(
                run,
                question,
                tasks,
                task_ids,
                scope=skip_scope,
                raw_answer=str(answer_payload.get("answer") or ""),
                rationale="User selected skip for this field.",
            )
        values: dict[str, str] = {}
        interpretations: list[dict[str, Any]] = []
        saved_common_answers: list[dict[str, Any]] = []
        field_receipts: list[dict[str, Any]] = []
        memory_receipts: list[dict[str, Any]] = []  # one per memory write — verified by reading it back
        learn_later: list[dict[str, Any]] = []
        save_scope = str(answer_payload.get("save_scope") or "one_time").lower()
        permanent = save_scope in {"permanent", "profile", "always", "save"}
        reply_contract = answer_payload.get("reply_interpretation") if isinstance(answer_payload.get("reply_interpretation"), dict) else None
        if permanent and reply_contract and reply_contract.get("route") == "answer":
            profile_key = re.sub(r"[^a-z0-9_]+", "_", str(reply_contract.get("profile_key") or "").strip().lower()).strip("_")[:80]
            profile_value = str(reply_contract.get("profile_value") or "").strip()
            if not profile_key or not profile_value:
                return {
                    "applied": False,
                    "target": "field_approval_tasks",
                    "task_ids": task_ids,
                    "can_resume": False,
                    "interpretations": [
                        {
                            "resolved_value": str(reply_contract.get("answer_value") or answer_payload.get("answer") or "").strip(),
                            "confidence": float(reply_contract.get("confidence") or 0.0),
                            "rationale": "The requested profile-memory save did not include a stable model-chosen key and canonical value.",
                            "source": "model_interpreted_human_reply",
                            "needs_followup": True,
                            "memory_update": {},
                            "answer_kind": "value",
                            "scope": "profile_memory",
                            "profile_key": profile_key,
                            "profile_value": profile_value,
                        }
                    ],
                    "saved_common_answers": [],
                    "memory_receipts": [],
                    "field_receipts": [],
                    "reason": "profile_memory_contract_incomplete",
                }
        company = self._run_company(run)
        profile = self._profile_payload(run.user_id)
        resume_facts = self._resume_profile_facts(run.user_id)
        keep_proposed = bool(answer_payload.get("keep_proposed"))
        answer_text = str(answer_payload.get("answer", "")).strip()
        skip_model_client = LumenModelClient(self.db, user_id=run.user_id)
        for task in tasks:
            payload = dict(task.payload or {})
            proposed = str(payload.get("proposed_answer") or payload.get("proposed_option") or "").strip()
            visible_options = [str(option).strip() for option in (payload.get("visible_options") or payload.get("options") or []) if str(option).strip()]
            picked_options = self._picked_multi_select_options(payload, visible_options, answer_payload)
            skip_probe = None
            if not picked_options and not isinstance(answer_payload.get("reply_interpretation"), dict):
                skip_probe = self.answer_interpreter.interpret(
                    raw_answer=answer_text,
                    field_key=str(payload.get("field_key") or ""),
                    label=str(payload.get("label") or ""),
                    options=visible_options,
                    profile=profile,
                    resume_facts=resume_facts,
                    company=company,
                    model_client=skip_model_client,
                )
            if skip_probe is not None and skip_probe.answer_kind == "skip":
                return self._skip_field_approval(
                    run,
                    question,
                    tasks,
                    task_ids,
                    scope=skip_probe.scope or "application",
                    raw_answer=answer_text,
                    rationale=skip_probe.rationale,
                )
            # If the user picked one of the field's OWN options, apply it verbatim — it's already a
            # valid value for the control, so re-interpreting it (e.g. mangling "5+" into "5 years"
            # so the select can't match it) is wrong.
            picked_option = self._verbatim_visible_option_from_instruction(answer_text, visible_options)
            if picked_options:
                # A multi-select field: the user's set of live options, applied verbatim as a set.
                interpreted = InterpretedAnswer(
                    resolved_value=multi_select_answer_text(picked_options),
                    confidence=1.0,
                    rationale=f"User picked {len(picked_options)} of the field's visible options.",
                    source="user_picked_options",
                    needs_followup=False,
                    memory_update={},
                )
                user_choice, raw_user_answer = "change", interpreted.resolved_value
            elif keep_proposed and proposed:
                # User clicked "Keep" -> apply the agent's proposed value verbatim, skipping the
                # interpreter (it's already the answer the user confirmed). Idempotent; never
                # re-triggers an ask. user_choice is logged for transparency.
                interpreted = InterpretedAnswer(
                    resolved_value=proposed,
                    confidence=1.0,
                    rationale="User kept the agent's proposed answer.",
                    source="user_kept_proposed",
                    needs_followup=False,
                    memory_update={},
                )
                user_choice, raw_user_answer = "keep", proposed
            elif isinstance(answer_payload.get("reply_interpretation"), dict) and answer_payload["reply_interpretation"].get("route") == "answer":
                reply = dict(answer_payload["reply_interpretation"])
                interpreted = InterpretedAnswer(
                    resolved_value=str(reply.get("matched_option") or reply.get("answer_value") or answer_text).strip(),
                    confidence=float(reply.get("confidence") or 0.8),
                    rationale=str(reply.get("rationale") or "Lumen separated the user's answer from its operational instructions."),
                    source="model_interpreted_human_reply",
                    needs_followup=False,
                    memory_update={},
                    profile_key=str(reply.get("profile_key") or ""),
                    profile_value=str(reply.get("profile_value") or ""),
                )
                user_choice, raw_user_answer = "change", interpreted.resolved_value
            elif picked_option:
                interpreted = InterpretedAnswer(
                    resolved_value=picked_option,
                    confidence=1.0,
                    rationale=f"User picked the visible option {picked_option!r} verbatim.",
                    source="user_picked_option",
                    needs_followup=False,
                    memory_update={},
                )
                user_choice, raw_user_answer = "change", picked_option
            else:
                interpreted = self.answer_interpreter.interpret(
                    raw_answer=str(answer_payload.get("answer", "")),
                    field_key=str(payload.get("field_key") or ""),
                    label=str(payload.get("label") or ""),
                    options=visible_options,
                    profile=profile,
                    resume_facts=resume_facts,
                    company=company,
                )
                user_choice, raw_user_answer = "change", answer_payload.get("answer", "")
            interpreted_payload = {
                "resolved_value": interpreted.resolved_value,
                "confidence": interpreted.confidence,
                "rationale": interpreted.rationale,
                "source": interpreted.source,
                "needs_followup": interpreted.needs_followup,
                "memory_update": interpreted.memory_update,
                "answer_kind": interpreted.answer_kind,
                "scope": interpreted.scope,
                "profile_key": interpreted.profile_key,
                "profile_value": interpreted.profile_value,
                **({"selected_options": picked_options} if picked_options else {}),
            }
            payload.update(
                {
                    "raw_user_answer": raw_user_answer,
                    "original_user_reply": str(answer_payload.get("raw_user_reply") or raw_user_answer),
                    "interpreted_answer": interpreted_payload,
                    "user_choice": user_choice,
                }
            )
            task.payload = payload
            task.updated_at = utcnow()
            interpretations.append({**interpreted_payload, "task_id": task.id, "field_key": payload.get("field_key")})
            if interpreted.needs_followup:
                return {"applied": False, "target": "field_approval_tasks", "task_ids": task_ids, "can_resume": False, "interpretations": interpretations, "saved_common_answers": saved_common_answers, "memory_receipts": memory_receipts}
            values[str(task.id)] = interpreted.resolved_value
            # Flywheel: a confirm-ask Keep/Change becomes a structured correction (eval data +
            # analytics). No-op for plain blank asks. Best-effort — never breaks answering.
            from app.answer_corrections import record_correction

            record_correction(self.db, run=run, task_payload=task.payload)
            if permanent:
                self._apply_interpreter_memory(run, interpreted.memory_update)
            # Memory write WITH a receipt: persist, then verify by reading it back. The "saved it"
            # claim is rendered only from a verified receipt — never asserted.
            # Never POISON the answer bank: only remember a value that's actually a valid answer for the
            # field. For a choice field, a reply that maps to NO option (e.g. an instruction like "if its
            # optional avoid it") must not be saved — it would be reused every run, match nothing, and
            # re-ask forever (the bug the user hit). Verbatim picks / kept proposals are valid by
            # construction; free-text fields have no options to violate.
            answer_value = str(interpreted.resolved_value or "").strip()
            verbatim_choice = interpreted.source in {"user_picked_option", "user_kept_proposed"}
            maps_to_option = (not visible_options) or verbatim_choice or any(answer_value.lower() == opt.lower() for opt in visible_options)
            should_remember = bool(answer_value) and maps_to_option
            try:
                if should_remember and permanent:
                    saved = self._save_common_answer(run, task, interpreted, question_id=question.id)
                    saved_common_answers.append(saved)
                    memory_receipts.append(self._memory_write_receipt(run, task=task, written=saved.get("facts") or []))
                elif should_remember:
                    # Not asked to remember: a model decides afterwards whether the answer is a
                    # durable fact about the user (a salary expectation, not a showtime).
                    learn_later.append(
                        {
                            "question": str((task.payload or {}).get("label") or task.name or ""),
                            "answer": answer_value,
                            "options": list(visible_options or []),
                        }
                    )
            except Exception as exc:  # noqa: BLE001 - a failed save must not be claimed as saved
                memory_receipts.append(make_receipt(
                    operation_id=f"memory_write:{task.id}", type="memory_write",
                    target={"field": str((task.payload or {}).get("field_key") or ""), "scope": "profile" if permanent else "run"},
                    attempt={"status": "attempted"}, execution={"status": "failed", "error": str(exc)}, verification={"status": "not_verified"},
                ))
            field_receipts.append(
                make_receipt(
                    operation_id=f"field_value:{task.id}",
                    type="field_value",
                    target={
                        "field": str((task.payload or {}).get("field_key") or ""),
                        "label": str((task.payload or {}).get("label") or ""),
                        "expected_value": interpreted.resolved_value,
                        "snapshot_id": str((task.payload or {}).get("snapshot_id") or ""),
                    },
                    attempt={"status": "attempted"},
                    execution={"status": "succeeded", "mode": "approved_value_stored_for_fresh_browser_observation"},
                    verification={"status": "inconclusive", "reason": "live_browser_field_verification_requires_resume_observation"},
                )
            )
        if not tasks:
            return {"applied": False, "target": "field_approval_tasks", "task_ids": task_ids, "can_resume": False, "interpretations": interpretations, "saved_common_answers": saved_common_answers, "memory_receipts": memory_receipts, "field_receipts": field_receipts, "reason": "No pending field task was found."}
        OperatorRuntimeEngine(self.db).approve_fields(run_id=run.id, task_ids=[int(task_id) for task_id in task_ids], approved=True, values=values)
        # If this is an in-browser run, auto-resume the loop now that the answer is in the bank —
        # it re-observes, the just-answered field resolves, and it continues (or asks the next).
        # Capture whether the resume was actually scheduled so the reply can say so truthfully (the
        # controlled-live resume_run path is skipped for in-browser runs, so this is the real signal).
        resumed_in_browser = False
        for receipt in memory_receipts:
            emit_event(self.db, event_type="OPERATION_RECEIPT", payload=receipt, run_id=run.id, actor="memory_write", confidence=1.0)
        self._learn_from_answers_later(run, learn_later)
        for receipt in field_receipts:
            emit_event(self.db, event_type="OPERATION_RECEIPT", payload=receipt, run_id=run.id, actor="field_approval", confidence=1.0)
        self._supersede_other_pending_field_questions(run.id, answered_question_id=question.id)
        return {"applied": True, "target": "field_approval_tasks", "task_ids": task_ids, "can_resume": True, "resumed": resumed_in_browser, "resume_in_browser_requested": True, "interpretations": interpretations, "saved_common_answers": saved_common_answers, "memory_receipts": memory_receipts, "field_receipts": field_receipts}

    def _apply_field_backed_blocked_reason_answer(self, run: models.OperatorRun, question: models.OperatorQuestion, answer_payload: dict[str, Any]) -> dict[str, Any] | None:
        task = self._ensure_blocked_reason_field_task(run, question)
        if not task:
            return None
        schema = dict(question.expected_answer_schema or {})
        schema["task_ids"] = [task.id]
        schema_field = self._field_task_schema(task)
        schema["binding_key"] = schema_field.get("binding_key")
        schema["fields"] = [schema_field]
        schema["blocked_reason_field_answer"] = True
        question.expected_answer_schema = schema
        result = self._apply_field_approval_answer(run, question, {**answer_payload, "task_ids": [task.id]})
        if result.get("applied"):
            self._supersede_duplicate_field_blocked_questions(run.id, keep_question=question, field_key=str((task.payload or {}).get("field_key") or ""))
        return result

    def _ensure_blocked_reason_field_task(self, run: models.OperatorRun, question: models.OperatorQuestion) -> models.RuntimeTask | None:
        schema = dict(question.expected_answer_schema or {})
        task_ids = [int(task_id) for task_id in schema.get("task_ids") or []]
        if task_ids:
            task = self.db.scalars(
                select(models.RuntimeTask).where(
                    models.RuntimeTask.run_id == run.id,
                    models.RuntimeTask.task_type == "field_approval",
                    models.RuntimeTask.id.in_(task_ids),
                )
            ).first()
            if task:
                return task
        field = self._blocked_reason_field_from_schema(schema) or self._blocked_reason_field_from_artifact(run) or self._blocked_reason_field_from_text(run, question)
        if not field:
            return None
        field_key = str(field.get("field_key") or field.get("node_id") or normalize_answer_key("", str(field.get("label") or ""))).strip()
        label = re.sub(r"\s+", " ", str(field.get("label") or field_key or "this field")).strip()
        if not field_key or not label or label.lower() in {"this field", "field"}:
            return None
        existing = self.db.scalars(
            select(models.RuntimeTask).where(
                models.RuntimeTask.run_id == run.id,
                models.RuntimeTask.task_type == "field_approval",
                models.RuntimeTask.status.in_(["pending", "approved"]),
            )
        ).all()
        for task in existing:
            if str((task.payload or {}).get("field_key") or "") == field_key:
                return task
        options = [str(option).strip() for option in field.get("options") or [] if str(option).strip()]
        if not options and self._blocked_reason_label_looks_yes_no(label):
            options = ["Yes", "No"]
        from app.form_graph import legacy_required, requiredness

        req = requiredness(field)
        required_projection = legacy_required(req)
        task = models.RuntimeTask(
            run_id=run.id,
            name=f"Answer blocked field: {label}",
            status="pending",
            task_type="field_approval",
            payload={
                "field_key": field_key,
                "label": label,
                "target": {"node_id": field.get("node_id"), "snapshot_id": field.get("snapshot_id")},
                "value_category": "option" if options else "field_value",
                "confidence": 0.0,
                "source": "blocked_reason_widget_failure",
                "options": options,
                "stored_answer": "",
                "value_source": None,
                "question_text": f"This posting asks: {label}. I don't have a stored answer for it.",
                "attempted_source": field.get("attempted_source"),
                "required": required_projection,
                "requiredness": req,
                "legacy_required_projection": required_projection,
            },
            max_attempts=1,
        )
        self.db.add(task)
        self.db.flush()
        return task

    def _blocked_reason_field_from_schema(self, schema: dict[str, Any]) -> dict[str, Any] | None:
        fields = schema.get("fields") if isinstance(schema, dict) else None
        if isinstance(fields, list) and fields:
            field = fields[0]
            if isinstance(field, dict):
                return {
                    "field_key": field.get("field_key") or field.get("id"),
                    "node_id": field.get("node_id") or field.get("field_key"),
                    "snapshot_id": field.get("snapshot_id"),
                    "label": field.get("label") or field.get("question"),
                    "options": field.get("options") or [],
                    "option_refs": field.get("option_refs") or [],
                    "field_ref": field.get("field_ref") or {},
                    "element_ref": field.get("element_ref") or {},
                    "control_kind": field.get("control_kind"),
                    "observation_id": field.get("observation_id"),
                    "required": field.get("required") if "required" in field else None,
                    "requiredness": field.get("requiredness"),
                }
        return None

    def _blocked_reason_field_from_artifact(self, run: models.OperatorRun) -> dict[str, Any] | None:
        workspace = dict((run.confidence_state or {}).get("end_to_end") or {})
        path_text = str(workspace.get("phase2_blocker_artifact_path") or "").strip()
        if not path_text:
            return None
        path = Path(path_text)
        if not path.exists():
            return None
        try:
            artifact = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        evidence = artifact.get("evidence") if isinstance(artifact.get("evidence"), dict) else {}
        failure = evidence.get("failed_action") if isinstance(evidence.get("failed_action"), dict) else {}
        if not failure and isinstance(evidence.get("batch"), dict):
            batch = evidence["batch"]
            failure = batch.get("result") if isinstance(batch.get("result"), dict) else {}
            if not failure:
                failure = next((item for item in batch.get("results") or [] if isinstance(item, dict) and not item.get("success")), {})
        if not failure:
            return None
        observation = failure.get("observation") if isinstance(failure.get("observation"), dict) else {}
        target = observation.get("target") if isinstance(observation.get("target"), dict) else {}
        label = re.sub(
            r"\s+",
            " ",
            str(target.get("label") or target.get("resolved_label") or failure.get("label") or failure.get("node_id") or ""),
        ).strip()
        node_id = str(failure.get("node_id") or target.get("node_id") or "").strip()
        options = self._candidate_options_from_failure_payload(failure)
        return {
            "field_key": node_id or normalize_answer_key("", label),
            "node_id": node_id,
            "snapshot_id": failure.get("snapshot_id"),
            "label": label,
            "options": options,
            "attempted_source": failure.get("value_source") or failure.get("answer_source"),
            "required": True,
        }

    def _blocked_reason_field_from_text(self, run: models.OperatorRun, question: models.OperatorQuestion) -> dict[str, Any] | None:
        text = " ".join(str(value or "") for value in (question.prompt, question.stuck_reason, run.status_message))
        match = re.search(r"(?:complete|on)\s+'([^']{4,240})'", text)
        if not match:
            return None
        label = re.sub(r"\s+", " ", match.group(1)).strip()
        return {"field_key": normalize_answer_key("", label), "label": label, "options": ["Yes", "No"] if self._blocked_reason_label_looks_yes_no(label) else [], "required": True}

    def _candidate_options_from_failure_payload(self, failure: dict[str, Any]) -> list[str]:
        candidates: list[str] = []
        observation = failure.get("observation") if isinstance(failure, dict) and isinstance(failure.get("observation"), dict) else {}
        sources = [
            failure.get("candidate_options") if isinstance(failure, dict) else None,
            observation.get("candidate_options") if isinstance(observation, dict) else None,
            (observation.get("smart_select_trace") or {}).get("candidate_options") if isinstance(observation.get("smart_select_trace"), dict) else None,
            (observation.get("smart_select_trace") or {}).get("options") if isinstance(observation.get("smart_select_trace"), dict) else None,
        ]
        for source in sources:
            if not isinstance(source, list):
                continue
            for item in source:
                text = str((item or {}).get("text") or (item or {}).get("value") or (item or {}).get("label") if isinstance(item, dict) else item or "").strip()
                if text and text not in candidates:
                    candidates.append(text)
        return candidates

    def _blocked_reason_label_looks_yes_no(self, label: str) -> bool:
        return classify_field("", label, []) == "yes_no"

    def _blocked_reason_field_key_for_question(self, run: models.OperatorRun, question: models.OperatorQuestion) -> str:
        task_ids = [int(task_id) for task_id in ((question.expected_answer_schema or {}).get("task_ids") or [])]
        if task_ids:
            task = self.db.scalars(select(models.RuntimeTask).where(models.RuntimeTask.run_id == run.id, models.RuntimeTask.id.in_(task_ids))).first()
            if task:
                return str((task.payload or {}).get("field_key") or "")
        field = self._blocked_reason_field_from_schema(question.expected_answer_schema or {}) or self._blocked_reason_field_from_artifact(run) or self._blocked_reason_field_from_text(run, question)
        return str((field or {}).get("field_key") or (field or {}).get("node_id") or "").strip()

    def _supersede_duplicate_field_blocked_questions(self, run_id: int, *, keep_question: models.OperatorQuestion, field_key: str) -> None:
        if not field_key:
            return
        run = self.db.get(models.OperatorRun, run_id)
        if not run:
            return
        for question in self.db.scalars(
            select(models.OperatorQuestion).where(
                models.OperatorQuestion.run_id == run_id,
                models.OperatorQuestion.status == "pending",
                models.OperatorQuestion.question_type == "blocked_reason",
                models.OperatorQuestion.id != keep_question.id,
            )
        ).all():
            if self._blocked_reason_field_key_for_question(run, question) != field_key:
                continue
            question.status = "superseded"
            question.updated_at = utcnow()
        self.db.flush()

    def _answer_memory_pattern(self, task: models.RuntimeTask) -> str:
        """The STABLE answer-bank key for a field — keyed by question text, not the volatile node_id.
        Shared by the save and the read-back verifier so they always agree (else the 'saved it' receipt
        reads inconclusive)."""
        from app.agent_settings import normalize_question_key

        payload = dict(task.payload or {})
        field_key = str(payload.get("field_key") or task.name or "common_answer")
        label = str(payload.get("label") or task.name or field_key)
        stable_key = normalize_question_key(label) or field_key
        return f"{stable_key}: {label}".strip()

    def _memory_write_receipt(self, run: models.OperatorRun, *, task: models.RuntimeTask, written: list[dict[str, Any]]) -> dict[str, Any]:
        """A receipt for a profile write, VERIFIED by reading the facts back from the memory file -
        so the 'saved it' claim is grounded in reality, never asserted. Outcome is runtime-derived."""
        field_key = str((task.payload or {}).get("field_key") or task.name or "")
        try:
            stored = {fact.key: fact.value for fact in user_memory.facts(run.user_id)}
            verified = bool(written) and all(
                record["key"] in stored
                and (record.get("action") == "skip" or stored[record["key"]] == " ".join(str(record.get("value") or "").split()))
                for record in written
            )
        except Exception:  # noqa: BLE001 - verification failure -> inconclusive, never crash
            verified = False
        return make_receipt(
            operation_id=f"memory_write:{field_key}",
            type="memory_write",
            target={"field": field_key, "scope": "profile", "read_back": verified, "keys": [record.get("key") for record in written]},
            attempt={"status": "attempted"},
            execution={"status": "succeeded"},
            verification={"status": "succeeded" if verified else "inconclusive"},
        )

    def _skip_memory_write_receipt(self, run: models.OperatorRun, task: models.RuntimeTask) -> dict[str, Any]:
        field_key = str((task.payload or {}).get("field_key") or task.name or "")
        pattern = self._answer_memory_pattern(task)
        try:
            row = self.db.scalars(
                select(models.AnswerMemory).where(models.AnswerMemory.user_id == run.user_id, models.AnswerMemory.question_pattern == pattern)
            ).first()
            verified = bool(row and str(row.approved_answer) == SKIP_ANSWER_TEXT)
        except Exception:  # noqa: BLE001
            verified = False
        return make_receipt(
            operation_id=f"memory_write_skip:{field_key}",
            type="memory_write",
            target={"field": field_key, "scope": "always", "answer_kind": "skip", "read_back": verified},
            attempt={"status": "attempted"},
            execution={"status": "succeeded"},
            verification={"status": "succeeded" if verified else "inconclusive"},
        )

    def _save_skip_answer_memory(self, run: models.OperatorRun, task: models.RuntimeTask, *, question_id: int | None = None) -> dict[str, Any]:
        interpreted = InterpretedAnswer(
            resolved_value=SKIP_ANSWER_TEXT,
            confidence=1.0,
            rationale="User asked Lumen to always skip this field.",
            source="user_skip_preference",
            needs_followup=False,
            memory_update={},
            answer_kind="skip",
            scope="always",
        )
        saved = self._save_answer_memory(run, task, interpreted, question_id=question_id)
        saved["answer_kind"] = "skip"
        saved["scope"] = "always"
        return saved

    def _save_answer_memory(self, run: models.OperatorRun, task: models.RuntimeTask, interpreted, *, question_id: int | None = None) -> dict[str, Any]:
        payload = dict(task.payload or {})
        field_key = str(payload.get("field_key") or task.name or "common_answer")
        label = str(payload.get("label") or task.name or field_key)
        # Key the memory by the STABLE question text (shared helper), not the volatile node_id — which
        # changed every render and piled up duplicate, sometimes poisoned, rows for the same question.
        # One row per question now upserts; reuse still matches by the label after the colon.
        normalized_pattern = self._answer_memory_pattern(task)
        memory = self.db.scalars(
            select(models.AnswerMemory).where(
                models.AnswerMemory.user_id == run.user_id,
                models.AnswerMemory.question_pattern == normalized_pattern,
            )
        ).first()
        if not memory:
            memory = models.AnswerMemory(
                user_id=run.user_id,
                question_pattern=normalized_pattern,
                approved_answer=interpreted.resolved_value,
                confidence=float(interpreted.confidence or 0.8),
            )
            self.db.add(memory)
            self.db.flush()
        else:
            memory.approved_answer = interpreted.resolved_value
            memory.confidence = max(float(memory.confidence or 0), float(interpreted.confidence or 0.8))
        memory.last_used_at = utcnow()
        self.db.flush()
        saved = {
            "answer_memory_id": memory.id,
            "field_key": field_key,
            "normalized_key": normalize_answer_key(field_key, label),
            "question": label,
            "question_pattern": normalized_pattern,
            "answer": interpreted.resolved_value,
            "question_id": question_id,
        }
        answer_kind = str(getattr(interpreted, "answer_kind", "") or "").strip()
        if answer_kind:
            saved["answer_kind"] = answer_kind
        scope = str(getattr(interpreted, "scope", "") or "").strip()
        if scope:
            saved["scope"] = scope
        emit_event(
            self.db,
            event_type="ANSWER_MEMORY_SAVED",
            payload=saved,
            run_id=run.id,
            actor="lumen_chat",
            confidence=float(interpreted.confidence or 0.8),
            risk_level="low",
        )
        return saved

    def _save_common_answer(self, run: models.OperatorRun, task: models.RuntimeTask, interpreted, *, question_id: int | None = None) -> dict[str, Any]:
        """Remember an answer the user asked Lumen to keep, as fact(s) in their profile.

        A model chose the key: the reply interpreter (memory_intent=profile_memory with a
        profile_key) or, for a structured "Use & remember", the answer-fact judge with
        explicit=True. Code never invents keys from field labels.
        """
        payload = dict(task.payload or {})
        field_key = str(payload.get("field_key") or task.name or "common_answer")
        label = str(payload.get("label") or task.name or field_key)
        model_profile_key = user_memory.normalize_memory_key(str(getattr(interpreted, "profile_key", "") or ""))
        profile_value = str(getattr(interpreted, "profile_value", "") or interpreted.resolved_value).strip()
        if interpreted.source == "model_interpreted_human_reply" and not model_profile_key:
            raise ValueError("Model-interpreted profile memory requires a stable model-chosen profile key")
        if model_profile_key:
            candidates = [{"key": model_profile_key, "value": profile_value}]
        else:
            judged = self.lumen_model.extract_user_facts_from_answer(
                {
                    "question": label,
                    "answer": profile_value,
                    "options": list(payload.get("options") or []),
                    "task": run.goal,
                    "explicit": True,
                }
            )
            candidates = judged.get("facts") or [] if judged.get("ok") else []
            if not candidates:
                raise ValueError("Lumen couldn't turn that answer into a profile fact")
        # The user asked to keep this value, so it replaces a conflicting older one.
        applied = user_memory.remember_all_reconciled(
            self.db, run.user_id, candidates, source_run_id=run.id, source_note="you, answering a question", overwrite_conflicts=True
        )
        written = [record for record in applied if record.get("action") in {"add", "update", "skip"}]
        saved = {
            "field_key": field_key,
            "question": label,
            "answer": interpreted.resolved_value,
            "memory_intent": "profile_memory",
            "facts": written,
        }
        emit_event(
            self.db,
            event_type="COMMON_ANSWER_SAVED",
            payload=saved,
            run_id=run.id,
            actor="lumen_chat",
            confidence=float(interpreted.confidence or 0.8),
            risk_level="low",
        )
        return saved

    def _learn_from_answers_later(self, run: models.OperatorRun, answers: list[dict[str, Any]]) -> None:
        """After the user answers questions, remember the durable ones (model-judged) off the hot path."""
        if not answers:
            return
        user_id, run_id, goal = run.user_id, run.id, run.goal

        def work() -> None:
            from app.db import SessionLocal

            with SessionLocal() as db:
                client = LumenModelClient(db)
                for item in answers:
                    try:
                        judged = client.extract_user_facts_from_answer({**item, "task": goal, "explicit": False})
                        facts = judged.get("facts") or [] if judged.get("ok") else []
                        if facts:
                            user_memory.remember_all_reconciled(
                                db, user_id, facts, source_run_id=run_id, source_note="you, answering a question", implicit=True
                            )
                        # Commit per answer: never hold the write lock across the next model call.
                        db.commit()
                    except Exception:  # noqa: BLE001 - learning is enrichment; it never affects the run
                        db.rollback()
                        logger.exception("Learning from an answer failed (run %s)", run_id)

        threading.Thread(target=work, name=f"lumen-learn-{run_id}", daemon=True).start()

    def _apply_interpreter_memory(self, run: models.OperatorRun, memory_update: dict[str, Any]) -> None:
        """Standard profile facts the reply interpreter extracted go to the user's profile memory."""
        if not memory_update:
            return
        candidates = [
            {"key": key, "value": str(value)}
            for key, value in (memory_update.get("profile") or {}).items()
            if key in user_memory.PROFILE_COLUMN_KEYS and str(value or "").strip()
        ]
        if candidates:
            user_memory.remember_all_reconciled(
                self.db, run.user_id, candidates, source_run_id=run.id, source_note="you, answering a question"
            )

    def _profile_payload(self, user_id: int) -> dict[str, Any]:
        profile = self.db.scalars(select(models.UserProfile).where(models.UserProfile.user_id == user_id)).first()
        if not profile:
            return {}
        return {
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

    def _resume_profile_facts(self, user_id: int) -> dict[str, Any]:
        profile = self.db.scalars(select(models.UserProfile).where(models.UserProfile.user_id == user_id)).first()
        facts = dict((profile.profile_json or {}) if profile else {})
        known = list(facts.get("known_employers") or [])
        resume = active_base_resume(self.db, user_id)
        text = (resume.base_resume_text if resume else "").lower()
        for employer in ["Spectrum Capital", "New Jersey Institute of Technology", "Cognier Insights", "Kommuri Pratap Reddy Institute Of Technology", "Affirm"]:
            if employer.lower() in text and employer not in known:
                known.append(employer)
        facts["known_employers"] = known
        return facts

    def _run_company(self, run: models.OperatorRun) -> str | None:
        application = self.db.get(models.Application, run.application_id)
        posting = self.db.get(models.JobPosting, application.job_posting_id) if application and application.job_posting_id else None
        return posting.company if posting else None

    def _protect_user_text(self, text: str, *, run_id: int | None) -> str:
        """The one place user-written text loses a TOTP seed: it becomes a placeholder, and the seed is kept
        encrypted with the run (or, for a chat with no run yet, with the turn, for the run it starts)."""
        cleaned, encrypted = totp_module.protect_goal(str(text or ""), get_settings().token_encryption_key)
        if encrypted:
            self._turn_totp_seeds = [*(getattr(self, "_turn_totp_seeds", None) or []), *encrypted]
            if run_id:
                self._attach_totp_seeds(int(run_id), encrypted)
        return cleaned if str(text or "") else text

    def _protect_user_payload(self, value: Any, *, run_id: int | None, depth: int = 0) -> Any:
        """Every string inside a user-supplied payload goes through _protect_user_text."""
        if depth > 6:
            return value
        if isinstance(value, str):
            return self._protect_user_text(value, run_id=run_id)
        if isinstance(value, dict):
            return {key: self._protect_user_payload(item, run_id=run_id, depth=depth + 1) for key, item in value.items()}
        if isinstance(value, list):
            return [self._protect_user_payload(item, run_id=run_id, depth=depth + 1) for item in value]
        return value

    def _attach_totp_seeds(self, run_id: int, encrypted: list[str]) -> None:
        """Keep a seed the user sent mid-run with that run (encrypted), next to the ones its goal carried."""
        run = self.db.get(models.OperatorRun, run_id)
        if run is None:
            return
        state = dict(run.confidence_state or {})
        state["totp_seeds"] = [*(state.get("totp_seeds") or []), *encrypted][:8]
        run.confidence_state = state

    def _add_message(self, thread: models.ChatThread, *, role: str, content: str, payload: dict[str, Any] | None = None) -> models.ChatMessage:
        if role == "user":
            content = self._protect_user_text(content, run_id=thread.run_id)
            payload = self._protect_user_payload(payload or {}, run_id=thread.run_id)
        message = models.ChatMessage(thread_id=thread.id, role=role, content=content, payload_json=redact_otp_codes(payload or {}))
        self.db.add(message)
        thread.updated_at = utcnow()
        self.db.flush()
        return message

    def _thread(self, thread_id: int | None) -> models.ChatThread:
        if not thread_id:
            raise ValueError("Chat thread not found")
        thread = self.db.get(models.ChatThread, thread_id)
        if not thread or thread.status != "active":
            raise ValueError("Chat thread not found")
        return thread


def multi_select_answer_text(options: list[str]) -> str:
    """One answer string for a multi-select pick: the chosen options, in order."""
    return "; ".join(options)


def _in_browser_run(run: Any) -> bool:
    """Whether a reply to this run is a reply to a live browser run. Decided by the run's durable surface,
    never by the transient in_browser marker, and never by whether a body is connected right now."""
    from app.extension_run import run_is_in_browser

    return run_is_in_browser(getattr(run, "confidence_state", None))


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _assistant_reply(context: dict[str, Any], pending_question: models.OperatorQuestion | None, *, content: str | None = None, chat_scope: dict[str, Any] | None = None) -> str:
    chat_scope = chat_scope or {}
    primary_intent = str(chat_scope.get("primary_intent") or "domain_chat")
    action = str(chat_scope.get("action") or "none")
    dev_mode = "dev" in str(chat_scope.get("view_mode") or "").lower() or "developer" in str(chat_scope.get("view_mode") or "").lower()
    run = context.get("run", {}) if isinstance(context.get("run"), dict) else {}
    workspace = context.get("goal_workspace", {}) if isinstance(context.get("goal_workspace"), dict) else {}
    profile = context.get("profile_summary", {}) if isinstance(context.get("profile_summary"), dict) else {}
    resume = context.get("resume_summary", {}) if isinstance(context.get("resume_summary"), dict) else {}
    if pending_question:
        if action in {"explain_status", "explain_submit_policy"} or primary_intent in {"run_status", "explain_activity", "submit_policy", "approval_or_otp"}:
            return (
                f"I need your input before I can continue. I'm at {pending_question.stuck_phase or 'the current step'}: "
                f"{pending_question.stuck_reason or 'Lumen needs a decision.'} {pending_question.prompt}"
            )
        return f"I'm waiting for your answer: {pending_question.prompt}"
    if chat_scope.get("out_of_scope"):
        return _out_of_scope_reply(content=content, recent_messages=context.get("recent_messages"))
    if primary_intent == "greeting":
        if context.get("scope") == "global":
            return desktop_platform.os_text("Hi, I'm Lumen. What should I work on? I can do tasks in your browser and in Windows apps.")
        return "Hi, I'm Lumen. I'm on this task. You can ask what I'm doing, answer my questions, or give me a correction."
    if primary_intent in {"run_status", "explain_activity"} or action == "explain_status":
        if not run:
            return "There's no task running in this chat yet. Tell me what to do and I'll start."
        phase = workspace.get("phase") or workspace.get("run_phase") or "current workflow"
        status = run.get("status_message") or "No blocker reported."
        if dev_mode and chat_scope.get("expose_diagnostics"):
            return f"Run {run.get('id')} is in {run.get('state')} during {phase}. Status: {status}"
        return f"I'm working on it ({str(phase).replace('_', ' ').lower()}). Current status: {status}"
    if chat_scope.get("resume_related") and chat_scope.get("profile_related"):
        saved = int(profile.get("common_answer_count") or 0)
        resume_status = "I have your base resume available" if resume.get("base_resume_uploaded") else "I do not see a base resume uploaded yet"
        return (
            f"{resume_status}. I can also use your stored profile variables and {saved} saved common answer(s) while filling forms. "
            "For a job run, I compare the posting to your resume, prepare a recommendation, and ask before using or submitting anything."
        )
    if primary_intent in {"profile_question", "saved_answers_question"} or chat_scope.get("profile_related"):
        saved = int(profile.get("common_answer_count") or 0)
        missing = [
            label
            for key, label in [
                ("name_set", "name"),
                ("email_set", "email"),
                ("phone_set", "phone"),
                ("location_set", "location"),
                ("work_authorization_set", "work authorization"),
                ("linkedin_set", "LinkedIn"),
            ]
            if not profile.get(key)
        ]
        if missing:
            return f"I can use your stored variables and common answers while filling forms. You have {saved} saved common answer(s). Missing profile fields: {', '.join(missing[:4])}."
        return f"Your core stored variables are ready, and I have {saved} saved common answer(s) available for application forms."
    if primary_intent == "resume_question" or chat_scope.get("resume_related"):
        if resume.get("base_resume_uploaded"):
            resume_name = resume.get("base_resume_name")
            return (
                f"I have your base resume{f' ({resume_name})' if resume_name else ''} and I'll attach it when I apply to a job for you. "
                "I can't tailor or rewrite resumes yet — for now I use your base resume as-is."
            )
        return (
            "I don't see a base resume yet, so I have none to attach when I apply. "
            "Note: I can't tailor resumes yet — I use your base resume as-is."
        )
    if primary_intent in {"approval_or_otp", "submit_policy"} or chat_scope.get("approval_related") or action == "explain_submit_policy":
        return (
            "Lumen will ask before sensitive actions like OTP handling, human approvals, and final submit. "
            "For final review, I verify the selected resume, filled fields, attachments, and submit policy before any submission attempt."
        )
    if primary_intent == "application_form_help":
        return (
            "I can help with application forms by reading field labels, using your stored profile and common answers, "
            "asking when a field is ambiguous, and applying your corrections during the run."
        )
    if primary_intent == "correction":
        if run:
            return "Tell me the field and the corrected value. I will apply it to the current run and ask runtime to continue from the saved state."
        return "I can apply corrections once this chat is attached to an active application run."
    if primary_intent == "run_control":
        return "I can pause, resume, start takeover, end takeover, or stop an active run. Tell me which control you want to apply."
    if context.get("scope") == "global":
        return (
            "I'm Lumen, your application agent. Send me a job link and tell me to apply, "
            "or ask me about your profile, resume, saved answers, current runs, or anything you want to change."
        )
    run = context.get("run", {})
    return f"I'm tracking run {run.get('id')} in state {run.get('state')}. Current status: {run.get('status_message') or 'no blocker reported'}."


_SCOPE_REDIRECT_TEMPLATES = {
    "greeting": [
        "Doing well - ready to work. Give me a task: a site to act on, a job link, or something on your computer.",
        "I'm here and ready to help - browsing tasks, job applications, your files and apps. What should I do?",
    ],
    "tone_feedback": [
        "Sorry - I did not mean to come across that way. Tell me the task or the correction and I'll get to it.",
        "You're right, that was too abrupt. Give me the task again, or tell me what to change, and I'll take care of it.",
    ],
    "off_topic": [
        "I'm a doing agent rather than a chat companion: I act in your browser and on your computer - booking, ordering, searching, filling forms, applying to jobs. Give me a task and I'll get to work.",
        "I can't help with that one, but give me something to DO - a website task, a job application, or work in a Windows app - and I'm on it.",
        "That's outside what I can do. What I'm good at is acting for you: browsing tasks, applications, forms, files, and apps. Name the task and I'll start.",
    ],
}


def _out_of_scope_reply(*, content: str | None = None, recent_messages: Any | None = None) -> str:
    intent = _scope_redirect_intent(content)
    previous = _last_assistant_text(recent_messages)
    templates = [desktop_platform.os_text(template) for template in _SCOPE_REDIRECT_TEMPLATES[intent]]
    for template in templates:
        if template != previous:
            return template
    return templates[0]


def _scope_redirect_intent(content: str | None) -> str:
    text = re.sub(r"\s+", " ", str(content or "").strip().lower())
    if not text:
        return "off_topic"
    if re.search(r"\b(rude|mean|cold|not helpful|unhelpful|annoying|frustrating|dismissive)\b", text) or re.search(r"\b(don'?t|do not)\s+have\s+to\s+be\s+rude\b", text):
        return "tone_feedback"
    if re.fullmatch(r"(hi|hello|hey|hey lumen|hi lumen|hello lumen|good morning|good afternoon|good evening)[!. ]*", text):
        return "greeting"
    if re.search(r"\b(how are you|how'?s it going|how are things|you good|are you there)\b", text):
        return "greeting"
    return "off_topic"


def _last_assistant_text(recent_messages: Any | None) -> str | None:
    if not isinstance(recent_messages, list):
        return None
    for item in reversed(recent_messages):
        if not isinstance(item, dict):
            continue
        if item.get("role") == "assistant":
            content = item.get("content")
            return str(content) if content is not None else None
    return None


def _extract_job_url(content: str, payload: dict[str, Any] | None = None) -> str | None:
    payload = payload or {}
    payload_url = payload.get("job_url")
    if isinstance(payload_url, str) and _is_http_url(payload_url.strip()):
        return payload_url.strip()
    for match in re.findall(r"https?://[^\s<>()\"']+", content or ""):
        candidate = match.rstrip(".,;:!?)]}\"'")
        if _is_http_url(candidate):
            return candidate
    return None


def _enumerate_user_http_urls(content: str, payload: dict[str, Any] | None = None) -> list[str]:
    """Enumerate, but never interpret, exact http(s) URL candidates from one human turn."""

    values: list[str] = []
    raw_payload = payload if isinstance(payload, dict) else {}
    for key in ("target_url", "url", "job_url"):
        raw = raw_payload.get(key)
        candidate = str(raw or "").strip()
        if candidate and _is_http_url(candidate) and candidate not in values:
            values.append(candidate)
    for match in re.findall(r"https?://[^\s<>()\"']+", content or ""):
        candidate = match.rstrip(".,;:!?)]}\"'")
        if _is_http_url(candidate) and candidate not in values:
            values.append(candidate)
    return values


def _final_action_promise(run: models.OperatorRun | None) -> str:
    """What the start reply promises about the final step, read from the policy the run snapshotted."""
    policy = (getattr(run, "confidence_state", None) or {}).get("final_action_policy") if run else None
    if isinstance(policy, dict) and policy.get("standing_authority_granted") is True:
        return (
            "Full autonomy is on, so I'll take the final step (submit, send, book) myself "
            "and stop only for a payment or a fact about you I don't have."
        )
    return "I will pause before any consequential final action for your explicit approval."


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _desktop_unusable_fix() -> str:
    """The one step that makes the desktop surface usable, by why it is not (as the run-start block words it)."""
    try:
        status = desktop_platform.desktop_control_status()
    except Exception:  # noqa: BLE001
        status = {}
    if status.get("permissions_missing"):
        return desktop_platform.permission_message()
    then = " so I can work in the browser app itself, or do this step yourself."
    if status.get("available") and not status.get("enabled"):
        return "Turn computer use back on from the desktop status bar" + then
    return desktop_platform.os_text("Start Lumen from the packaged Windows desktop app with computer use on") + then


# A browser's own pages: the extension body can neither navigate to nor act on them.
_BROWSER_INTERNAL_SCHEMES = frozenset(
    {"chrome", "chrome-extension", "chrome-untrusted", "chrome-search", "devtools", "about", "edge", "brave", "opera", "vivaldi"}
)


def _is_browser_internal_url(value: str) -> bool:
    return urlparse(str(value or "").strip()).scheme.lower() in _BROWSER_INTERNAL_SCHEMES


def _host_label(url: str) -> str:
    host = urlparse(url).netloc or url
    return host.removeprefix("www.")


_DEFAULT_CHAT_TITLE = "Lumen chat"
_CHAT_TITLE_LIMIT = 60


def _chat_title_from_message(content: str) -> str:
    """The first line of a message as a one-line chat title, with codes redacted."""
    first_line = next((line.strip() for line in str(content or "").splitlines() if line.strip()), "")
    title = " ".join(redact_otp_codes(first_line).split()).replace("[REDACTED_OTP]", "••••")
    if len(title) <= _CHAT_TITLE_LIMIT:
        return title
    return title[: _CHAT_TITLE_LIMIT - 1].rsplit(" ", 1)[0].rstrip(",.;:") + "…"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:80] or "unknown"
