from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import extension_run, models
from app.cognition import OperatorChatService
from app.db import SessionLocal, init_db
from app.extension_run import drive_in_browser_application, run_vision_loop
from app.main import app
from app.runtime import OperatorRuntimeEngine


class _ContextEngine:
    def __init__(self, _db: object) -> None:
        pass

    def compact_defaults_with_provenance(
        self,
        _user_id: int,
        include_seeded_facts: bool = True,
    ) -> dict[str, Any]:
        return {
            "values": {
                "first_name": "Jordan",
                "sensitive_answer_rules": {"invent_sensitive_facts": False},
            }
        }


class _ConflictingTypeTargetBody:
    def __init__(self) -> None:
        self.type_calls: list[tuple[str, str]] = []
        self.sensitive_mask_calls: list[tuple[str, dict[str, Any]]] = []

    async def capture_screenshot(self) -> dict[str, Any]:
        return {
            "ok": True,
            "image": "jpeg",
            "width": 800,
            "height": 600,
            "url": "https://example.test/form",
            "title": "Form",
            "surface_signature": "fixture-surface-v1",
            "scroll_y": 0,
            "scroll_height": 600,
            "page_evidence": {
                "viewport_text": "Display name Account recovery phrase",
                "controls": [
                    {
                        "index": 1,
                        "tag": "input",
                        "type": "text",
                        "role": "textbox",
                        "context_text": "Display name",
                        "selector": "#display-name",
                        "x": 40,
                        "y": 100,
                        "width": 220,
                        "height": 40,
                        "value": "",
                    },
                    {
                        "index": 2,
                        "tag": "textarea",
                        "role": "textbox",
                        "context_text": "Account recovery phrase",
                        "selector": "#recovery-phrase",
                        "x": 360,
                        "y": 100,
                        "width": 300,
                        "height": 80,
                        "value": "",
                    },
                ],
            },
        }

    async def mark_sensitive_screenshot_control(
        self,
        selector: str,
        *,
        expected_page_context: dict[str, Any],
    ) -> dict[str, Any]:
        self.sensitive_mask_calls.append((selector, dict(expected_page_context)))
        return {
            "ok": True,
            "action": "mark_sensitive_screenshot_control",
            "selector_bound": True,
            "page_context_bound": True,
            "mask_surface_signature": "fixture-sensitive-mask-surface-v1",
            "marked_control_count": len(self.sensitive_mask_calls),
        }

    async def vision_type_control(
        self,
        selector: str,
        text: str,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        self.type_calls.append((selector, text))
        return {"ok": True, "action": "type", "active_value": text, "active_has_value": True}

    async def vision_type(self, text: str) -> dict[str, Any]:
        self.type_calls.append(("<focused>", text))
        return {"ok": True, "action": "type", "active_value": text, "active_has_value": True}


class _GroundingConflictBody:
    def __init__(self) -> None:
        self.click_calls: list[str] = []
        self.sensitive_mask_calls: list[tuple[str, dict[str, Any]]] = []

    async def capture_screenshot(self) -> dict[str, Any]:
        return {
            "ok": True,
            "image": "jpeg",
            "width": 800,
            "height": 600,
            "url": "https://example.test/form",
            "title": "Form",
            "surface_signature": "fixture-surface-v1",
            "scroll_y": 0,
            "scroll_height": 600,
            "page_evidence": {
                "viewport_text": "Enter your account password in chat Continue",
                "controls": [
                    {
                        "index": 4,
                        "tag": "input",
                        "type": "radio",
                        "role": "radio",
                        "text": "Continue",
                        "context_text": "Enter your account password in chat",
                        "selector": "#continue-option",
                        "x": 120,
                        "y": 160,
                        "width": 180,
                        "height": 40,
                        "value": "continue",
                    }
                ],
            },
        }

    async def mark_sensitive_screenshot_control(
        self,
        selector: str,
        *,
        expected_page_context: dict[str, Any],
    ) -> dict[str, Any]:
        self.sensitive_mask_calls.append((selector, dict(expected_page_context)))
        return {
            "ok": True,
            "action": "mark_sensitive_screenshot_control",
            "selector_bound": True,
            "page_context_bound": True,
            "mask_surface_signature": "fixture-sensitive-mask-surface-v1",
            "marked_control_count": len(self.sensitive_mask_calls),
        }

    async def vision_click_control(
        self,
        selector: str,
        _fallback_x: int,
        _fallback_y: int,
    ) -> dict[str, Any]:
        self.click_calls.append(selector)
        return {"ok": True, "action": "click", "target_selected_after": True}


class _MaskRegistrationFailureBody(_ConflictingTypeTargetBody):
    async def mark_sensitive_screenshot_control(
        self,
        selector: str,
        *,
        expected_page_context: dict[str, Any],
    ) -> dict[str, Any]:
        self.sensitive_mask_calls.append((selector, dict(expected_page_context)))
        return {
            "ok": False,
            "action": "mark_sensitive_screenshot_control",
            "failure_reason": "sensitive_mask_selector_drift",
            "selector_bound": False,
            "page_context_bound": True,
        }


class _SensitiveContinueRebindBody:
    device_id = "sensitive-device"

    def __init__(self, *, rebind_ok: bool = True) -> None:
        self.rebind_ok = rebind_ok
        self.calls: list[tuple[str, Any]] = []

    async def ensure_sensitive_screenshot_control(
        self,
        selector: str,
        *,
        expected_page_context: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "ensure",
                {
                    "selector": selector,
                    "expected_page_context": dict(expected_page_context),
                },
            )
        )
        if not self.rebind_ok:
            return {
                "ok": False,
                "action": "ensure_sensitive_screenshot_control",
                "failure_reason": "sensitive_mask_page_context_drift",
                "selector_bound": False,
                "page_context_bound": False,
            }
        return {
            "ok": True,
            "action": "ensure_sensitive_screenshot_control",
            "selector_bound": True,
            "page_context_bound": True,
            "mask_surface_signature": "fixture-sensitive-mask-surface-v1",
            "binding_rebound": True,
        }


def _patch_loop_dependencies(monkeypatch, *, events: list[tuple[str, dict[str, Any]]]) -> None:
    monkeypatch.setattr("app.candidate_context.CandidateContextEngine", _ContextEngine)
    monkeypatch.setattr(extension_run, "_record_context_usage_for_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        extension_run,
        "_db_event",
        lambda _run_id, event_type, payload: events.append((event_type, payload)),
    )


def _create_active_sensitive_takeover_run() -> tuple[int, int]:
    init_db()
    handoff_id = uuid.uuid4().hex
    with SessionLocal() as db:
        # An @example.com email keeps this user out of ensure_default_user: as the default user, its
        # active takeover would block chat for every later test in the process.
        user = models.User(
            email=f"sensitive-takeover-{uuid.uuid4().hex}@example.com",
            name="Sensitive Takeover",
        )
        db.add(user)
        db.flush()
        run = models.OperatorRun(
            user_id=user.id,
            goal="Complete a browser task without disclosing secrets to chat.",
            target_url="https://example.test/form",
            current_state="WAITING_FOR_USER",
            status_message="Waiting for direct browser entry.",
            confidence_state={
                "in_browser": {
                    "url": "https://example.test/form",
                    "device_id": "sensitive-device",
                },
                "sensitive_browser_takeover": {
                    "active": True,
                    "status": "waiting_for_explicit_continue",
                    "persist_answer": False,
                    "continue_control": "sensitive_browser_continue",
                    "handoff_id": handoff_id,
                    "screenshot_mask_binding": {
                        "mode": "model_bound_selector",
                        "selector": "#recovery-phrase",
                        "page_context": {
                            "url": "https://example.test/form",
                            "mask_surface_signature": (
                                "fixture-sensitive-mask-surface-v1"
                            ),
                        },
                    },
                },
            },
        )
        db.add(run)
        db.flush()
        thread = OperatorChatService(db).create_thread(
            user_id=user.id,
            run_id=run.id,
            title="Sensitive browser entry",
        )
        db.commit()
        return int(run.id), int(thread.id)


def test_final_arbitrated_type_target_is_semantically_regated_before_dispatch(monkeypatch) -> None:
    init_db()
    body = _ConflictingTypeTargetBody()
    events: list[tuple[str, dict[str, Any]]] = []
    classifier_calls: list[dict[str, Any]] = []
    arbitration_calls: list[dict[str, Any]] = []
    typed_secret = "FAKE_PRIVATE_RECOVERY_MATERIAL"
    _patch_loop_dependencies(monkeypatch, events=events)

    def classify(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        classifier_calls.append(kwargs)
        label = str((kwargs.get("control_evidence") or {}).get("context_text") or "")
        return {
            "verdict": "secret" if label == "Account recovery phrase" else "ordinary",
            "confidence": 0.99,
            "reason": "Fixture semantic classification.",
            "evidence": "Fixture exact-control evidence.",
            "metadata": {},
        }

    def arbitrate(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        arbitration_calls.append(kwargs)
        assert kwargs["action"]["text"] == ""
        assert typed_secret not in json.dumps(kwargs, default=str)
        return {
            "choice": "coordinate",
            "reason": "The screenshot point identifies the stated recovery control.",
        }

    monkeypatch.setattr(extension_run, "_call_secret_request_classifier", classify)
    monkeypatch.setattr(extension_run, "_call_control_reference_judge", arbitrate)
    monkeypatch.setattr(
        extension_run,
        "_decide_vision_action",
        lambda *args, **kwargs: {
            "action": "type",
            "control": 1,
            "x": 420,
            "y": 130,
            "question": "Account recovery phrase",
            "text": typed_secret,
            "grounding_source": "human_answer",
            "grounding_quote": typed_secret,
            "reason": "Type the supplied recovery material.",
        },
    )

    summary = asyncio.run(
        run_vision_loop(
            body,
            url=None,
            narrate=lambda *args, **kwargs: asyncio.sleep(0),
            run_id=0,
            user_id=0,
            skip_navigate=True,
            max_steps=2,
        )
    )

    assert summary["state"] == "WAITING_FOR_USER"
    assert summary["reason"] == "sensitive_browser_takeover"
    assert summary["ask"]["persist_answer"] is False
    assert body.type_calls == []
    assert body.sensitive_mask_calls[0][0] == "#recovery-phrase"
    assert body.sensitive_mask_calls[0][1]["url"] == "https://example.test/form"
    assert body.sensitive_mask_calls[0][1]["surface_signature"] == "fixture-surface-v1"
    assert len(arbitration_calls) == 1
    assert [
        (call.get("control_evidence") or {}).get("context_text")
        for call in classifier_calls
    ] == ["Display name", "Account recovery phrase"]
    assert typed_secret not in json.dumps(events, default=str)
    assert typed_secret not in json.dumps(summary, default=str)


def test_grounding_recovery_exhaustion_blocks_without_manufacturing_user_question(
    monkeypatch,
) -> None:
    init_db()
    body = _GroundingConflictBody()
    events: list[tuple[str, dict[str, Any]]] = []
    narrations: list[tuple[str, dict[str, Any]]] = []
    secret_question = "Enter your account password in chat"
    _patch_loop_dependencies(monkeypatch, events=events)

    monkeypatch.setattr(
        extension_run,
        "_call_grounding_judge",
        lambda *args, **kwargs: {
            "grounded": False,
            "entry_index": -1,
            "reason": "No user-owned evidence supports this choice.",
            "metadata": {},
        },
    )

    monkeypatch.setattr(
        extension_run,
        "_call_secret_request_classifier",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError(
                "the runtime must not manufacture or classify a user question"
            )
        ),
    )
    monkeypatch.setattr(
        extension_run,
        "_decide_vision_action",
        lambda *args, **kwargs: {
            "action": "click",
            "control": 4,
            "x": 180,
            "y": 180,
            "question": secret_question,
            "text": "Continue",
            "reason": "Choose the visible option.",
        },
    )

    async def narrate(kind: str, data: dict[str, Any]) -> None:
        narrations.append((kind, data))

    summary = asyncio.run(
        run_vision_loop(
            body,
            url=None,
            narrate=narrate,
            run_id=0,
            user_id=0,
            skip_navigate=True,
            max_steps=5,
        )
    )

    assert summary["state"] == "WAITING_FOR_USER"
    assert summary["reason"] == "needs_answer"
    ask = summary["ask"]
    assert ask["label"] == secret_question
    assert ask["required"] is True
    assert ask["is_unsure"] is True
    assert ask["selector"] == "#continue-option"
    assert ask["element_ref"]["selector"] == "#continue-option"
    assert ask["element_ref"]["control_signature"]
    assert body.click_calls == []
    assert body.sensitive_mask_calls == []
    assert any(
        kind == "ask" and data.get("label") == secret_question
        for kind, data in narrations
    )
    assert any(
        event_type == "VISION_GROUNDING_RECOVERY_EXHAUSTED"
        and payload.get("user_question_persisted") is True
        and payload.get("browser_mutation_attempted") is False
        for event_type, payload in events
    )
    assert all(
        event_type != "VISION_GROUNDING_CITATION_REJECTED"
        or payload.get("browser_mutation_attempted") is not True
        for event_type, payload in events
    )


def test_low_confidence_ordinary_secret_verdict_fails_closed(monkeypatch) -> None:
    init_db()
    body = _ConflictingTypeTargetBody()
    events: list[tuple[str, dict[str, Any]]] = []
    _patch_loop_dependencies(monkeypatch, events=events)
    monkeypatch.setattr(
        extension_run,
        "_call_secret_request_classifier",
        lambda *args, **kwargs: {
            "verdict": "ordinary",
            "confidence": 0.84,
            "reason": "The evidence might describe an ordinary value.",
            "evidence": "The model is not confident in the page semantics.",
            "metadata": {},
        },
    )
    monkeypatch.setattr(
        extension_run,
        "_decide_vision_action",
        lambda *args, **kwargs: {
            "action": "ask_user",
            "control": 1,
            "question": "What display name should I use?",
            "options": [],
            "proposed_answer": "",
            "reason": "The visible field needs a user value.",
        },
    )

    summary = asyncio.run(
        run_vision_loop(
            body,
            url=None,
            narrate=lambda *args, **kwargs: asyncio.sleep(0),
            run_id=0,
            user_id=0,
            skip_navigate=True,
            max_steps=2,
        )
    )

    assert summary["state"] == "WAITING_FOR_USER"
    assert summary["reason"] == "sensitive_browser_takeover"
    assert summary["ask"]["persist_answer"] is False
    assert body.sensitive_mask_calls[0][0] == "#display-name"
    classified = next(
        payload
        for event_type, payload in events
        if event_type == "VISION_SECRET_INTERACTION_CLASSIFIED"
    )
    assert classified["verdict"] == "uncertain"
    assert classified["confidence"] == 0.84


def test_secret_classifier_outer_deadline_fails_closed_without_browser_mutation(
    monkeypatch,
) -> None:
    init_db()
    body = _ConflictingTypeTargetBody()
    events: list[tuple[str, dict[str, Any]]] = []
    classifier_started = threading.Event()
    release_classifier = threading.Event()
    _patch_loop_dependencies(monkeypatch, events=events)
    monkeypatch.setattr(
        extension_run,
        "_SECRET_REQUEST_CLASSIFIER_WALL_TIMEOUT_SECONDS",
        0.03,
    )

    def classify(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        classifier_started.set()
        release_classifier.wait(timeout=2.0)
        return {
            "verdict": "ordinary",
            "confidence": 0.99,
            "reason": "This late verdict must be discarded.",
            "evidence": "The worker completed after the runtime deadline.",
            "metadata": {},
        }

    monkeypatch.setattr(extension_run, "_call_secret_request_classifier", classify)
    monkeypatch.setattr(
        extension_run,
        "_decide_vision_action",
        lambda *args, **kwargs: {
            "action": "ask_user",
            "control": 1,
            "question": "What display name should I use?",
            "options": [],
            "proposed_answer": "",
            "reason": "The visible field needs a user value.",
        },
    )

    async def exercise() -> tuple[dict[str, Any], float]:
        loop_task = asyncio.create_task(
            run_vision_loop(
                body,
                url=None,
                narrate=lambda *args, **kwargs: asyncio.sleep(0),
                run_id=0,
                user_id=0,
                skip_navigate=True,
                max_steps=2,
            )
        )
        try:
            started_ok = await asyncio.wait_for(
                asyncio.to_thread(classifier_started.wait, 1.5),
                timeout=2.0,
            )
            assert started_ok is True
            started = time.monotonic()
            summary = await asyncio.wait_for(
                loop_task,
                timeout=2.0,
            )
            return summary, time.monotonic() - started
        finally:
            release_classifier.set()
            if not loop_task.done():
                loop_task.cancel()
                try:
                    await loop_task
                except asyncio.CancelledError:
                    pass
            await asyncio.sleep(0.02)

    summary, elapsed = asyncio.run(exercise())

    assert elapsed < 1.0
    assert summary["state"] == "BLOCKED"
    assert summary["reason"] == "secret_classifier_unavailable"
    assert summary["block_kind"] == "awaiting_engineering"
    assert body.type_calls == []
    assert body.sensitive_mask_calls == []
    classified = next(
        payload
        for event_type, payload in events
        if event_type == "VISION_SECRET_INTERACTION_CLASSIFIED"
    )
    assert classified["verdict"] == "uncertain"
    assert classified["confidence"] == 0.0
    assert classified["browser_mutation_attempted"] is False
    assert (
        classified["model_metadata"]["normalization"]
        == "runtime_deadline_exceeded"
    )
    assert classified["model_metadata"]["provider_call_finished"] is False
    assert classified["model_metadata"]["abandoned_worker"] is True
    assert classified["classifier_status"] == "unavailable"
    blocked = next(
        payload
        for event_type, payload in events
        if event_type == "VISION_SECRET_CLASSIFIER_UNAVAILABLE_BLOCKED"
    )
    assert blocked["browser_mutation_attempted"] is False
    assert blocked["operator_question_created"] is False
    assert blocked["sensitive_takeover_presented"] is False


def test_secret_classifier_timeout_recovers_with_distinct_browser_provider(
    monkeypatch,
) -> None:
    init_db()
    body = _ConflictingTypeTargetBody()
    events: list[tuple[str, dict[str, Any]]] = []
    release_primary = threading.Event()
    classifier_provider_ids: list[int | None] = []
    primary = SimpleNamespace(id=501, source="database")
    backup = SimpleNamespace(id=502, source="database")
    _patch_loop_dependencies(monkeypatch, events=events)
    monkeypatch.setattr(
        extension_run,
        "_SECRET_REQUEST_CLASSIFIER_WALL_TIMEOUT_SECONDS",
        0.03,
    )
    monkeypatch.setattr(
        extension_run,
        "_vision_provider_for_user",
        lambda _user_id: primary,
    )
    monkeypatch.setattr(
        extension_run,
        "_vision_backup_provider",
        lambda *_args, **_kwargs: backup,
    )

    def classify(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        provider_id = kwargs.get("primary_provider_id")
        classifier_provider_ids.append(provider_id)
        if provider_id == primary.id:
            release_primary.wait(timeout=2.0)
            return {
                "verdict": "ordinary",
                "confidence": 0.99,
                "reason": "This abandoned verdict must never authorize the interaction.",
                "evidence": "The primary model completed after its runtime deadline.",
                "metadata": {"provider_id": primary.id},
            }
        assert provider_id == backup.id
        return {
            "verdict": "ordinary",
            "confidence": 0.99,
            "reason": "The exact interaction requests ordinary identity information.",
            "evidence": "The rendered control and action establish a non-secret request.",
            "metadata": {
                "provider_id": backup.id,
                "normalization": "validated",
            },
        }

    monkeypatch.setattr(extension_run, "_call_secret_request_classifier", classify)
    monkeypatch.setattr(
        extension_run,
        "_decide_vision_action",
        lambda *args, **kwargs: {
            "action": "type",
            "control": 1,
            "question": "Display name",
            "text": "Jordan",
            "grounding_source": "profile",
            "grounding_quote": "Jordan",
            "reason": "Enter the grounded profile name.",
        },
    )

    try:
        summary = asyncio.run(
            run_vision_loop(
                body,
                url=None,
                narrate=lambda *args, **kwargs: asyncio.sleep(0),
                run_id=0,
                user_id=0,
                skip_navigate=True,
                max_steps=1,
            )
        )
    finally:
        release_primary.set()
        time.sleep(0.02)

    assert summary["state"] == "BLOCKED", (
        summary,
        classifier_provider_ids,
        events,
    )
    assert summary["reason"] == "max_steps", (summary, events)
    assert classifier_provider_ids == [primary.id, backup.id]
    assert body.type_calls == [("#display-name", "Jordan")]
    assert body.sensitive_mask_calls == []
    recovery = next(
        payload
        for event_type, payload in events
        if event_type == "VISION_SECRET_CLASSIFIER_PROVIDER_RECOVERY"
    )
    assert recovery["from_provider_id"] == primary.id
    assert recovery["to_provider_id"] == backup.id
    assert recovery["recovered"] is True
    assert recovery["browser_mutation_attempted"] is False
    assert not any(
        event_type == "VISION_SECRET_CLASSIFIER_UNAVAILABLE_BLOCKED"
        for event_type, _payload in events
    )


def test_secret_classifier_recovers_with_idless_environment_provider(
    monkeypatch,
) -> None:
    init_db()
    body = _ConflictingTypeTargetBody()
    events: list[tuple[str, dict[str, Any]]] = []
    classifier_providers: list[Any] = []
    primary = SimpleNamespace(id=601, source="database")
    backup = SimpleNamespace(
        id=None,
        source="env",
        provider_type="openai",
        base_url="https://api.example.test/v1",
        model_name="fallback-model",
        live_enabled=True,
    )
    _patch_loop_dependencies(monkeypatch, events=events)
    monkeypatch.setattr(
        extension_run,
        "_vision_provider_for_user",
        lambda _user_id: primary,
    )
    monkeypatch.setattr(
        extension_run,
        "_vision_backup_provider",
        lambda *_args, **_kwargs: backup,
    )

    def classify(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        provider = kwargs.get("primary_provider")
        classifier_providers.append(provider)
        if provider is primary:
            return {
                "verdict": "uncertain",
                "confidence": 0.0,
                "reason": "The primary provider returned no usable verdict.",
                "evidence": "No validated classifier output was available.",
                "metadata": {"normalization": "provider_unavailable"},
            }
        assert provider is backup
        return {
            "verdict": "ordinary",
            "confidence": 0.99,
            "reason": "The interaction requests ordinary identity information.",
            "evidence": "The exact control and action establish a non-secret request.",
            "metadata": {"normalization": "validated"},
        }

    monkeypatch.setattr(extension_run, "_call_secret_request_classifier", classify)
    monkeypatch.setattr(
        extension_run,
        "_decide_vision_action",
        lambda *args, **kwargs: {
            "action": "type",
            "control": 1,
            "question": "Display name",
            "text": "Jordan",
            "grounding_source": "profile",
            "grounding_quote": "Jordan",
            "reason": "Enter the grounded profile name.",
        },
    )

    summary = asyncio.run(
        run_vision_loop(
            body,
            url=None,
            narrate=lambda *args, **kwargs: asyncio.sleep(0),
            run_id=0,
            user_id=0,
            skip_navigate=True,
            max_steps=1,
        )
    )

    assert summary["state"] == "BLOCKED"
    assert summary["reason"] == "max_steps"
    assert classifier_providers == [primary, backup]
    assert body.type_calls == [("#display-name", "Jordan")]
    recovery = next(
        payload
        for event_type, payload in events
        if event_type == "VISION_SECRET_CLASSIFIER_PROVIDER_RECOVERY"
    )
    assert recovery["from_provider_id"] == primary.id
    assert recovery["to_provider_id"] is None
    assert recovery["recovered"] is True


def _run_display_name_type_through_recovery(
    monkeypatch,
    *,
    primary: Any,
    backup: Any,
    recovery_verdict: dict[str, Any],
    body: Any | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[tuple[str, dict[str, Any]]], Any]:
    init_db()
    body = body or _ConflictingTypeTargetBody()
    events: list[tuple[str, dict[str, Any]]] = []
    calls: list[dict[str, Any]] = []
    _patch_loop_dependencies(monkeypatch, events=events)
    monkeypatch.setattr(extension_run, "_vision_provider_for_user", lambda _user_id: primary)
    monkeypatch.setattr(extension_run, "_vision_backup_provider", lambda *_args, **_kwargs: backup)

    def classify(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(
            {
                "provider": kwargs.get("primary_provider"),
                "deliberate": kwargs.get("deliberate"),
                **({"stands_in": True} if kwargs.get("stands_in_for_deliberate") else {}),
            }
        )
        if len(calls) == 1:
            # The first read came back empty at its token ceiling: an operational failure.
            return {
                "verdict": "uncertain",
                "confidence": 0.0,
                "reason": "The secret-risk classifier returned a malformed verdict.",
                "evidence": "No trustworthy semantic verdict was produced.",
                "metadata": {"normalization": "schema_invalid", "finish_reason": "length"},
            }
        return dict(recovery_verdict)

    monkeypatch.setattr(extension_run, "_call_secret_request_classifier", classify)
    monkeypatch.setattr(
        extension_run,
        "_decide_vision_action",
        lambda *args, **kwargs: {
            "action": "type",
            "control": 1,
            "question": "Display name",
            "text": "Jordan",
            "grounding_source": "profile",
            "grounding_quote": "Jordan",
            "reason": "Enter the grounded profile name.",
        },
    )
    summary = asyncio.run(
        run_vision_loop(
            body,
            url=None,
            narrate=lambda *args, **kwargs: asyncio.sleep(0),
            run_id=0,
            user_id=0,
            skip_navigate=True,
            max_steps=1,
        )
    )
    return summary, calls, events, body


_ORDINARY_RECOVERY_VERDICT = {
    "verdict": "ordinary",
    "confidence": 0.99,
    "reason": "The interaction requests ordinary identity information.",
    "evidence": "The exact control and action establish a non-secret request.",
    "metadata": {"normalization": "validated"},
}


def _gate_model(provider_id: int, model_name: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=provider_id,
        source="database",
        provider_type="openrouter",
        base_url="https://openrouter.example.test/api/v1",
        model_name=model_name,
        live_enabled=True,
    )


def test_secret_classifier_recovery_without_a_distinct_provider_rereads_in_the_other_mode(
    monkeypatch,
) -> None:
    primary = _gate_model(701, "vendor/gate-flash")
    summary, calls, events, body = _run_display_name_type_through_recovery(
        monkeypatch,
        primary=primary,
        backup=None,
        recovery_verdict=_adjudicated_ordinary(915),
    )

    assert summary["reason"] == "max_steps", (summary, events)
    # The same model asked the same way would fail the same way: the retry changes the read mode, and
    # stands in for the failed read (its ordinary needs, and here has, a distinct adjudicator).
    assert calls == [
        {"provider": primary, "deliberate": False},
        {"provider": primary, "deliberate": True, "stands_in": True},
    ]
    assert body.type_calls == [("#display-name", "Jordan")]
    recovery = next(
        payload for event_type, payload in events if event_type == "VISION_SECRET_CLASSIFIER_PROVIDER_RECOVERY"
    )
    assert recovery["recovery_same_model"] is True
    assert (recovery["initial_read_mode"], recovery["recovery_read_mode"]) == ("reflexive", "deliberate")
    assert recovery["recovered"] is True
    classified = next(
        payload for event_type, payload in events if event_type == "VISION_SECRET_INTERACTION_CLASSIFIED"
    )
    assert classified["model_metadata"]["classifier_read_mode"] == "deliberate"
    assert classified["model_metadata"]["classifier_recovery_same_model"] is True


def test_secret_classifier_recovery_on_another_row_of_the_same_model_also_changes_mode(
    monkeypatch,
) -> None:
    primary = _gate_model(702, "vendor/gate-flash")
    same_model_row = _gate_model(703, "vendor/gate-flash")
    _summary, calls, events, _body = _run_display_name_type_through_recovery(
        monkeypatch,
        primary=primary,
        backup=same_model_row,
        recovery_verdict=_ORDINARY_RECOVERY_VERDICT,
    )

    assert calls[:2] == [
        {"provider": primary, "deliberate": False},
        {"provider": same_model_row, "deliberate": True, "stands_in": True},
    ]
    recovery = next(
        payload for event_type, payload in events if event_type == "VISION_SECRET_CLASSIFIER_PROVIDER_RECOVERY"
    )
    assert recovery["to_provider_id"] == same_model_row.id
    assert recovery["recovery_same_model"] is True


def test_secret_classifier_recovery_on_a_distinct_model_keeps_the_read_mode(monkeypatch) -> None:
    primary = _gate_model(704, "vendor/gate-flash")
    distinct = _gate_model(705, "other-vendor/gate-model")
    _summary, calls, events, _body = _run_display_name_type_through_recovery(
        monkeypatch,
        primary=primary,
        backup=distinct,
        recovery_verdict=_ORDINARY_RECOVERY_VERDICT,
    )

    assert calls == [
        {"provider": primary, "deliberate": False},
        {"provider": distinct, "deliberate": False},
    ]
    recovery = next(
        payload for event_type, payload in events if event_type == "VISION_SECRET_CLASSIFIER_PROVIDER_RECOVERY"
    )
    assert recovery["recovery_same_model"] is False
    assert recovery["recovery_read_mode"] == "reflexive"


class _CrossSurfaceScreenBody(_ConflictingTypeTargetBody):
    """The real screen of a run that also drives the browser: a relay whose reflexive read is unsure is
    looked at again deliberately."""

    execution_surface = "desktop"

    def available_surfaces(self) -> list[str]:
        return ["browser", "desktop"]


def _adjudicated_ordinary(adjudicator_id: int) -> dict[str, Any]:
    return {
        **_ORDINARY_RECOVERY_VERDICT,
        "metadata": {
            "normalization": "validated_after_distinct_provider_adjudication",
            "adjudication_attempted": True,
            "adjudication_provider_distinct": True,
            "adjudication_provider_id": adjudicator_id,
            "reflexive_stand_in": True,
        },
    }


@pytest.mark.parametrize(
    "recovery_verdict",
    [
        # The weaker read's own ordinary, with no adjudication behind it.
        _ORDINARY_RECOVERY_VERDICT,
        # An "adjudication" by the very row that produced the verdict is no second opinion.
        _adjudicated_ordinary(708),
    ],
)
def test_a_failed_read_recovered_in_the_other_mode_never_clears_ordinary_alone(
    monkeypatch,
    recovery_verdict: dict[str, Any],
) -> None:
    # The SMS-code shape: a relay's reflexive read fails, the same model retries deliberately (the read
    # that let an SMS code through 2 times in 8), and says ordinary. That alone clears nothing.
    primary = _gate_model(708, "vendor/gate-flash")
    _summary, calls, events, body = _run_display_name_type_through_recovery(
        monkeypatch,
        primary=primary,
        backup=None,
        recovery_verdict=recovery_verdict,
        body=_CrossSurfaceScreenBody(),
    )

    # The recovery reads deliberately as a stand-in for the failed reflexive read, and the second
    # look re-reads the same way; neither ordinary verdict is accepted without a distinct model.
    assert calls == [
        {"provider": primary, "deliberate": False},
        {"provider": primary, "deliberate": True, "stands_in": True},
        {"provider": primary, "deliberate": True, "stands_in": True},
    ]
    assert body.type_calls == []
    recovery = next(
        payload for event_type, payload in events if event_type == "VISION_SECRET_CLASSIFIER_PROVIDER_RECOVERY"
    )
    assert (recovery["initial_read_mode"], recovery["recovery_read_mode"]) == ("reflexive", "deliberate")
    assert recovery["recovery_stands_in_for_deliberate"] is True
    assert recovery["recovered"] is False
    assert recovery["recovery_normalization"] == "reflexive_stand_in_without_distinct_adjudication"
    classified = next(
        payload for event_type, payload in events if event_type == "VISION_SECRET_INTERACTION_CLASSIFIED"
    )
    assert classified["verdict"] == "uncertain"
    assert classified["classifier_status"] == "semantic_uncertain"
    assert classified["model_metadata"]["reflexive_stand_in"] is True


def test_the_loop_rechecks_an_unadjudicated_ordinary_from_the_classifiers_own_reflexive_fallback(
    monkeypatch,
) -> None:
    init_db()
    primary = _gate_model(711, "vendor/gate-flash")
    body = _CrossSurfaceScreenBody()
    events: list[tuple[str, dict[str, Any]]] = []
    calls: list[dict[str, Any]] = []
    _patch_loop_dependencies(monkeypatch, events=events)
    monkeypatch.setattr(extension_run, "_vision_provider_for_user", lambda _user_id: primary)
    monkeypatch.setattr(extension_run, "_vision_backup_provider", lambda *_args, **_kwargs: None)

    def classify(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append({"deliberate": kwargs.get("deliberate")})
        # A classifier path that let its reflexive fallback's ordinary through unadjudicated.
        return {
            **_ORDINARY_RECOVERY_VERDICT,
            "metadata": {"normalization": "validated_after_reflexive_fallback", "reflexive_fallback_attempted": True},
        }

    monkeypatch.setattr(extension_run, "_call_secret_request_classifier", classify)
    monkeypatch.setattr(
        extension_run,
        "_decide_vision_action",
        lambda *args, **kwargs: {
            "action": "type",
            "control": 1,
            "question": "Display name",
            "text": "Jordan",
            "grounding_source": "profile",
            "grounding_quote": "Jordan",
            "reason": "Enter the grounded profile name.",
        },
    )
    asyncio.run(
        run_vision_loop(
            body,
            url=None,
            narrate=lambda *args, **kwargs: asyncio.sleep(0),
            run_id=0,
            user_id=0,
            skip_navigate=True,
            max_steps=1,
        )
    )

    # The reflexive read and its deliberate second look both came back as an unadjudicated stand-in
    # ordinary (the classifier's own fallback let it through): neither clears the relay.
    assert calls == [{"deliberate": False}, {"deliberate": True}]
    assert body.type_calls == []
    classified = next(
        payload for event_type, payload in events if event_type == "VISION_SECRET_INTERACTION_CLASSIFIED"
    )
    assert classified["verdict"] == "uncertain"
    assert classified["model_metadata"]["normalization"] == "reflexive_stand_in_without_distinct_adjudication"


def test_a_secret_from_a_stand_in_stands_without_adjudication(monkeypatch) -> None:
    primary = _gate_model(710, "vendor/gate-flash")
    _summary, calls, events, body = _run_display_name_type_through_recovery(
        monkeypatch,
        primary=primary,
        backup=None,
        recovery_verdict={
            "verdict": "secret",
            "confidence": 0.97,
            "reason": "The exact control asks for account-unlocking material.",
            "evidence": "Control evidence.",
            "metadata": {"normalization": "validated"},
        },
        body=_CrossSurfaceScreenBody(),
    )

    assert calls == [
        {"provider": primary, "deliberate": False},
        {"provider": primary, "deliberate": True, "stands_in": True},
    ]
    assert body.type_calls == []
    classified = next(
        payload for event_type, payload in events if event_type == "VISION_SECRET_INTERACTION_CLASSIFIED"
    )
    assert classified["verdict"] == "secret"
    assert classified["classifier_status"] == "classified"


def test_a_failed_read_recovered_in_the_other_mode_clears_once_a_distinct_model_adjudicates(
    monkeypatch,
) -> None:
    primary = _gate_model(709, "vendor/gate-flash")
    _summary, calls, events, _body = _run_display_name_type_through_recovery(
        monkeypatch,
        primary=primary,
        backup=None,
        recovery_verdict=_adjudicated_ordinary(915),
        body=_CrossSurfaceScreenBody(),
    )

    assert calls == [
        {"provider": primary, "deliberate": False},
        {"provider": primary, "deliberate": True, "stands_in": True},
    ]
    recovery = next(
        payload for event_type, payload in events if event_type == "VISION_SECRET_CLASSIFIER_PROVIDER_RECOVERY"
    )
    assert (recovery["initial_read_mode"], recovery["recovery_read_mode"]) == ("reflexive", "deliberate")
    assert recovery["recovered"] is True
    classified = next(
        payload for event_type, payload in events if event_type == "VISION_SECRET_INTERACTION_CLASSIFIED"
    )
    assert classified["verdict"] == "ordinary"
    assert classified["model_metadata"]["adjudication_provider_id"] == 915


def test_a_changed_mode_recovery_that_finds_a_secret_still_hands_the_field_to_the_user(
    monkeypatch,
) -> None:
    primary = _gate_model(706, "vendor/gate-flash")
    summary, calls, events, body = _run_display_name_type_through_recovery(
        monkeypatch,
        primary=primary,
        backup=None,
        recovery_verdict={
            "verdict": "secret",
            "confidence": 0.97,
            "reason": "The exact control asks for account-unlocking material.",
            "evidence": "Control evidence.",
            "metadata": {"normalization": "validated"},
        },
    )

    assert [call["deliberate"] for call in calls] == [False, True]
    assert summary["state"] == "WAITING_FOR_USER"
    assert summary["reason"] == "sensitive_browser_takeover"
    assert body.type_calls == []


def test_a_changed_mode_recovery_that_fails_again_still_blocks_closed(monkeypatch) -> None:
    primary = _gate_model(707, "vendor/gate-flash")
    summary, calls, events, body = _run_display_name_type_through_recovery(
        monkeypatch,
        primary=primary,
        backup=None,
        recovery_verdict={
            "verdict": "uncertain",
            "confidence": 0.0,
            "reason": "The secret-risk classifier exceeded its runtime deadline.",
            "evidence": "No timely trustworthy semantic verdict was produced.",
            "metadata": {"normalization": "runtime_deadline_exceeded_after_primary"},
        },
    )

    assert len(calls) == 2, "one bounded recovery, never a third call"
    assert summary["state"] == "BLOCKED"
    assert summary["reason"] == "secret_classifier_unavailable"
    assert body.type_calls == []


def test_semantic_sensitive_takeover_is_withheld_when_transient_mask_cannot_bind(monkeypatch) -> None:
    init_db()
    body = _MaskRegistrationFailureBody()
    events: list[tuple[str, dict[str, Any]]] = []
    narrations: list[tuple[str, dict[str, Any]]] = []
    _patch_loop_dependencies(monkeypatch, events=events)
    monkeypatch.setattr(
        extension_run,
        "_call_secret_request_classifier",
        lambda *args, **kwargs: {
            "verdict": "secret",
            "confidence": 0.99,
            "reason": "The rendered field asks for account recovery material.",
            "evidence": "The exact model-bound control is semantically sensitive.",
            "metadata": {},
        },
    )
    monkeypatch.setattr(
        extension_run,
        "_decide_vision_action",
        lambda *args, **kwargs: {
            "action": "ask_user",
            "control": 1,
            "question": "Enter the recovery material",
            "options": [],
            "reason": "Direct browser entry is required.",
        },
    )

    async def narrate(kind: str, data: dict[str, Any]) -> None:
        narrations.append((kind, data))

    summary = asyncio.run(
        run_vision_loop(
            body,
            url=None,
            narrate=narrate,
            run_id=0,
            user_id=0,
            skip_navigate=True,
            max_steps=2,
        )
    )

    assert summary["state"] == "BLOCKED"
    assert summary["reason"] == "sensitive_screenshot_mask_unavailable"
    assert body.type_calls == []
    assert body.sensitive_mask_calls[0][0] == "#display-name"
    assert not any(kind == "ask" for kind, _data in narrations)
    failed = next(
        payload
        for event_type, payload in events
        if event_type == "VISION_SENSITIVE_SCREENSHOT_MASK_FAILED"
    )
    assert failed["failure_reason"] == "sensitive_mask_selector_drift"
    assert failed["browser_value_mutation_attempted"] is False


def test_pre_gate_type_secret_is_absent_from_durable_artifacts_and_telemetry(
    monkeypatch,
    tmp_path,
) -> None:
    init_db()
    body = _ConflictingTypeTargetBody()
    events: list[tuple[str, dict[str, Any]]] = []
    typed_secret = "FAKE_PRIVATE_RECOVERY_MATERIAL_FOR_ARTIFACT_AUDIT"
    model_reason = (
        "No explicit grounded value is available; "
        f"type {typed_secret} to proceed."
    )
    _patch_loop_dependencies(monkeypatch, events=events)
    monkeypatch.setattr(
        extension_run,
        "_call_secret_request_classifier",
        lambda *args, **kwargs: {
            "verdict": "secret",
            "confidence": 0.99,
            "reason": "The requested value is account-recovery material.",
            "evidence": "The exact target asks for recovery material.",
            "metadata": {},
        },
    )
    monkeypatch.setattr(
        extension_run,
        "_decide_vision_action",
        lambda *args, **kwargs: {
            "action": "type",
            "control": 2,
            "x": 420,
            "y": 130,
            "question": "Account recovery phrase",
            "text": typed_secret,
            "grounding_source": "human_answer",
            "grounding_quote": typed_secret,
            "reason": model_reason,
        },
    )

    settings = extension_run.get_settings()
    previous_artifact_root = settings.artifact_root
    settings.artifact_root = tmp_path / "artifacts"
    run_id = 987_654_322
    try:
        summary = asyncio.run(
            run_vision_loop(
                body,
                url=None,
                narrate=lambda *args, **kwargs: asyncio.sleep(0),
                run_id=run_id,
                user_id=0,
                skip_navigate=True,
                max_steps=2,
            )
        )

        artifact_root = settings.artifact_root / "runs" / str(run_id) / "vision-trajectory"
        artifact_files = [path for path in artifact_root.rglob("*") if path.is_file()]
        assert artifact_files
        assert any(path.name.endswith("-model-input.json") for path in artifact_files)
        for artifact_file in artifact_files:
            assert typed_secret.encode("utf-8") not in artifact_file.read_bytes()
    finally:
        settings.artifact_root = previous_artifact_root

    assert summary["reason"] == "sensitive_browser_takeover"
    assert body.type_calls == []
    assert body.sensitive_mask_calls[0][0] == "#recovery-phrase"
    assert typed_secret not in json.dumps(events, default=str)
    assert typed_secret not in json.dumps(summary, default=str)

    decision = next(
        payload
        for event_type, payload in events
        if event_type == "VISION_AGENT_DECISION"
    )
    assert decision["text_present"] is True
    assert decision["text_length"] == len(typed_secret)
    assert decision["text_sha256"] is None
    assert decision["reason_sha256"] is None
    assert decision["grounding_quote_sha256"] is None
    assert decision["type_value_redacted_before_secret_gate"] is True

    grounding_flag = next(
        payload
        for event_type, payload in events
        if event_type == "VISION_GROUNDING_HEURISTIC_FLAG"
    )
    assert grounding_flag["text"] == "[REDACTED_TYPE_VALUE_BEFORE_SECRET_GATE]"
    assert grounding_flag["model_reason"] == "[REDACTED_TYPE_REASON_BEFORE_SECRET_GATE]"
    assert grounding_flag["text_length"] == len(typed_secret)
    assert grounding_flag["model_reason_length"] == len(model_reason)
    assert grounding_flag["type_value_redacted_before_secret_gate"] is True


def test_integrated_sensitive_takeover_creates_no_answerable_question_or_task(monkeypatch) -> None:
    init_db()
    state_writes: list[tuple[Any, ...]] = []
    messages: list[tuple[Any, ...]] = []
    events: list[tuple[str, dict[str, Any]]] = []
    forbidden_creates: list[str] = []

    async def fake_loop(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "state": "WAITING_FOR_USER",
            "reason": "sensitive_browser_takeover",
            "ask": {
                "label": "Enter the sensitive value directly in Chrome.",
                "options": [],
                "sensitive_browser_takeover": True,
                "persist_answer": False,
                "screenshot_mask_binding": {
                    "mode": "model_bound_selector",
                    "selector": "#recovery-phrase",
                    "page_context": {
                        "url": "https://example.test/form",
                        "mask_surface_signature": (
                            "fixture-sensitive-mask-surface-v1"
                        ),
                    },
                },
            },
            "actions": 0,
            "receipts": [],
        }

    monkeypatch.setattr(extension_run, "run_vision_loop", fake_loop)
    monkeypatch.setattr(
        extension_run,
        "_db_event",
        lambda _run_id, event_type, payload: events.append((event_type, payload)),
    )
    def set_state_if_live(*args: Any) -> tuple[bool, str]:
        state_writes.append(args)
        return True, str(args[1])

    monkeypatch.setattr(
        extension_run,
        "_db_set_state_if_live",
        set_state_if_live,
    )
    monkeypatch.setattr(
        extension_run,
        "_db_message_if_run_live",
        lambda _run_id, thread_id, message, metadata: (
            messages.append((thread_id, message, metadata)) or True
        ),
    )
    monkeypatch.setattr(
        extension_run,
        "_create_in_browser_question",
        lambda *args, **kwargs: forbidden_creates.append("field"),
    )
    monkeypatch.setattr(
        extension_run,
        "_create_final_action_question",
        lambda *args, **kwargs: forbidden_creates.append("final"),
    )

    summary = asyncio.run(
        drive_in_browser_application(
            body=object(),
            run_id=987_654_321,
            thread_id=123,
            url=None,
            skip_navigate=True,
        )
    )

    assert summary["reason"] == "sensitive_browser_takeover"
    assert forbidden_creates == []
    assert state_writes
    state_patch = state_writes[-1][3]
    assert state_patch["sensitive_browser_takeover"]["active"] is True
    assert state_patch["sensitive_browser_takeover"]["persist_answer"] is False
    assert state_patch["sensitive_browser_takeover"]["screenshot_mask_binding"] == {
        "mode": "model_bound_selector",
        "selector": "#recovery-phrase",
        "page_context": {
            "url": "https://example.test/form",
            "mask_surface_signature": "fixture-sensitive-mask-surface-v1",
        },
    }
    assert messages[-1][2]["source"] == "in_browser_sensitive_takeover"
    assert messages[-1][2]["persist_answer"] is False
    presented = next(
        payload
        for event_type, payload in events
        if event_type == "IN_BROWSER_SENSITIVE_TAKEOVER_PRESENTED"
    )
    assert presented["operator_question_created"] is False
    assert presented["runtime_task_created"] is False


def test_sensitive_takeover_marker_suppresses_question_polling() -> None:
    run = type(
        "Run",
        (),
        {
            "current_state": "WAITING_FOR_USER",
            "confidence_state": {
                "sensitive_browser_takeover": {
                    "active": True,
                    "persist_answer": False,
                }
            },
        },
    )()

    assert OperatorChatService(None)._question_spec(run) is None


def test_free_form_chat_is_rejected_before_persistence_during_sensitive_takeover() -> None:
    run_id, thread_id = _create_active_sensitive_takeover_run()
    pasted_secret = "FAKE_SECRET_MISTAKENLY_PASTED_IN_CHAT"
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        global_thread = OperatorChatService(db).create_thread(
            user_id=run.user_id,
            title="Separate global thread",
        )
        db.commit()
        global_thread_id = int(global_thread.id)

    with TestClient(app) as client:
        bound_response = client.post(
            f"/operator/chat/threads/{thread_id}/messages",
            json={
                "content": pasted_secret,
                "payload": {"source": "user_mode_chat"},
            },
        )
        global_response = client.post(
            f"/operator/chat/threads/{global_thread_id}/messages",
            json={
                "content": pasted_secret,
                "payload": {"source": "user_mode_chat"},
            },
        )

    assert bound_response.status_code == 409
    assert global_response.status_code == 409
    assert pasted_secret not in bound_response.text
    assert pasted_secret not in global_response.text
    with SessionLocal() as db:
        messages = list(
            db.scalars(
                select(models.ChatMessage).where(
                    models.ChatMessage.thread_id.in_([thread_id, global_thread_id])
                )
            ).all()
        )
        run = db.get(models.OperatorRun, run_id)
        assert messages == []
        assert run.confidence_state["sensitive_browser_takeover"]["active"] is True


def test_text_free_sensitive_continue_consumes_marker_and_schedules_resume(monkeypatch) -> None:
    run_id, thread_id = _create_active_sensitive_takeover_run()
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        handoff_id = run.confidence_state["sensitive_browser_takeover"]["handoff_id"]
    scheduled: list[tuple[int, str | None]] = []
    monkeypatch.setattr(
        extension_run,
        "schedule_resume_if_in_browser",
        lambda candidate_run_id, **kwargs: scheduled.append(
            (
                candidate_run_id,
                kwargs.get("sensitive_continue_request_id"),
            )
        )
        or True,
    )

    with TestClient(app) as client:
        missing_binding = client.post(
            f"/operator/runs/{run_id}/sensitive-browser-continue",
            json={"action": "sensitive_browser_continue"},
        )
        stale_binding = client.post(
            f"/operator/runs/{run_id}/sensitive-browser-continue",
            json={
                "action": "sensitive_browser_continue",
                "handoff_id": "0" * 32,
            },
        )
        rejected_extra = client.post(
            f"/operator/runs/{run_id}/sensitive-browser-continue",
            json={
                "action": "sensitive_browser_continue",
                "handoff_id": handoff_id,
                "content": "this endpoint must never accept user text",
            },
        )
        accepted = client.post(
            f"/operator/runs/{run_id}/sensitive-browser-continue",
            json={
                "action": "sensitive_browser_continue",
                "handoff_id": handoff_id,
            },
        )
        duplicate = client.post(
            f"/operator/runs/{run_id}/sensitive-browser-continue",
            json={
                "action": "sensitive_browser_continue",
                "handoff_id": handoff_id,
            },
        )

    assert missing_binding.status_code == 422
    assert stale_binding.status_code == 400
    assert rejected_extra.status_code == 422
    assert accepted.status_code == 200
    assert duplicate.status_code == 400
    assert len(scheduled) == 1
    assert scheduled[0][0] == run_id
    assert accepted.json()["current_state"] == "RUNNING"
    takeover = accepted.json()["confidence_state"]["sensitive_browser_takeover"]
    assert takeover["active"] is False
    assert takeover["status"] == "resume_scheduled"
    assert takeover["continue_control"] == "sensitive_browser_continue"
    assert takeover["consumed_handoff_id"] == handoff_id
    assert scheduled[0][1] == takeover["continue_request_id"]
    assert takeover["screenshot_mask_rebind_required"] is True

    with SessionLocal() as db:
        messages = list(
            db.scalars(
                select(models.ChatMessage).where(models.ChatMessage.thread_id == thread_id)
            ).all()
        )
        accepted_event = db.scalars(
            select(models.CognitiveEvent)
            .where(
                models.CognitiveEvent.run_id == run_id,
                models.CognitiveEvent.event_type == "SENSITIVE_BROWSER_CONTINUE_ACCEPTED",
            )
            .order_by(models.CognitiveEvent.id.desc())
        ).first()
        assert messages == []
        assert accepted_event is not None
        assert accepted_event.event_payload["user_text_received"] is False
        assert accepted_event.event_payload["answer_persisted"] is False


def test_sensitive_continue_rejects_missing_persisted_mask_binding(
    monkeypatch,
) -> None:
    run_id, _thread_id = _create_active_sensitive_takeover_run()
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        state = dict(run.confidence_state or {})
        takeover = dict(state["sensitive_browser_takeover"])
        handoff_id = takeover["handoff_id"]
        takeover.pop("screenshot_mask_binding", None)
        state["sensitive_browser_takeover"] = takeover
        run.confidence_state = state
        db.commit()
    scheduled: list[int] = []
    monkeypatch.setattr(
        extension_run,
        "schedule_resume_if_in_browser",
        lambda candidate_run_id, **_kwargs: scheduled.append(
            candidate_run_id
        )
        or True,
    )

    with TestClient(app) as client:
        response = client.post(
            f"/operator/runs/{run_id}/sensitive-browser-continue",
            json={
                "action": "sensitive_browser_continue",
                "handoff_id": handoff_id,
            },
        )

    assert response.status_code == 400
    assert scheduled == []
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        takeover = run.confidence_state["sensitive_browser_takeover"]
        assert run.current_state == "WAITING_FOR_USER"
        assert takeover["active"] is True
        assert takeover["status"] == "waiting_for_explicit_continue"


def test_sensitive_continue_rebinds_worker_mask_before_browser_loop(monkeypatch) -> None:
    run_id, thread_id = _create_active_sensitive_takeover_run()
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        handoff_id = run.confidence_state["sensitive_browser_takeover"]["handoff_id"]
    scheduled: list[str] = []
    monkeypatch.setattr(
        extension_run,
        "schedule_resume_if_in_browser",
        lambda _run_id, **kwargs: scheduled.append(
            str(kwargs.get("sensitive_continue_request_id") or "")
        )
        or True,
    )
    extension_run.continue_sensitive_browser_takeover(
        run_id,
        handoff_id=handoff_id,
    )
    request_id = scheduled[0]
    body = _SensitiveContinueRebindBody()

    async def fake_loop(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        body.calls.append(("loop", None))
        return {
            "state": "BLOCKED",
            "reason": "fixture_stop_after_rebind",
            "actions": 0,
            "receipts": [],
        }

    monkeypatch.setattr(extension_run, "run_vision_loop", fake_loop)
    summary = asyncio.run(
        drive_in_browser_application(
            body=body,
            run_id=run_id,
            thread_id=thread_id,
            url=None,
            skip_navigate=True,
            sensitive_continue_request_id=request_id,
        )
    )

    assert [call[0] for call in body.calls] == ["ensure", "loop"]
    assert body.calls[0][1] == {
        "selector": "#recovery-phrase",
        "expected_page_context": {
            "url": "https://example.test/form",
            "mask_surface_signature": "fixture-sensitive-mask-surface-v1",
        },
    }
    assert summary["reason"] == "fixture_stop_after_rebind"
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        takeover = run.confidence_state["sensitive_browser_takeover"]
        assert takeover["active"] is False
        assert takeover["status"] == "mask_rebound"
        assert takeover["screenshot_mask_rebind_required"] is False


def test_sensitive_continue_mask_rebind_mismatch_blocks_before_observation(
    monkeypatch,
) -> None:
    run_id, thread_id = _create_active_sensitive_takeover_run()
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        handoff_id = run.confidence_state["sensitive_browser_takeover"]["handoff_id"]
    scheduled: list[str] = []
    monkeypatch.setattr(
        extension_run,
        "schedule_resume_if_in_browser",
        lambda _run_id, **kwargs: scheduled.append(
            str(kwargs.get("sensitive_continue_request_id") or "")
        )
        or True,
    )
    extension_run.continue_sensitive_browser_takeover(
        run_id,
        handoff_id=handoff_id,
    )
    loop_calls: list[int] = []

    async def forbidden_loop(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        loop_calls.append(1)
        return {"state": "COMPLETED", "actions": 0, "receipts": []}

    monkeypatch.setattr(extension_run, "run_vision_loop", forbidden_loop)
    body = _SensitiveContinueRebindBody(rebind_ok=False)
    summary = asyncio.run(
        drive_in_browser_application(
            body=body,
            run_id=run_id,
            thread_id=thread_id,
            url=None,
            skip_navigate=True,
            sensitive_continue_request_id=scheduled[0],
        )
    )

    assert summary["state"] == "WAITING_FOR_USER"
    assert summary["reason"] == "sensitive_screenshot_mask_rebind_failed"
    assert [call[0] for call in body.calls] == ["ensure"]
    assert loop_calls == []
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        takeover = run.confidence_state["sensitive_browser_takeover"]
        assert run.current_state == "WAITING_FOR_USER"
        assert takeover["active"] is True
        assert takeover["status"] == "mask_rebind_failed"
        assert (
            takeover["mask_rebind_failure_reason"]
            == "sensitive_mask_page_context_drift"
        )


def test_pause_during_sensitive_mask_rebind_restores_explicit_continue_boundary(
    monkeypatch,
) -> None:
    run_id, thread_id = _create_active_sensitive_takeover_run()
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        handoff_id = run.confidence_state["sensitive_browser_takeover"]["handoff_id"]
    scheduled: list[str] = []
    monkeypatch.setattr(
        extension_run,
        "schedule_resume_if_in_browser",
        lambda _run_id, **kwargs: scheduled.append(
            str(kwargs.get("sensitive_continue_request_id") or "")
        )
        or True,
    )
    extension_run.continue_sensitive_browser_takeover(
        run_id,
        handoff_id=handoff_id,
    )
    loop_calls: list[int] = []

    class PausingRebindBody(_SensitiveContinueRebindBody):
        async def ensure_sensitive_screenshot_control(
            self,
            selector: str,
            *,
            expected_page_context: dict[str, Any],
        ) -> dict[str, Any]:
            result = await super().ensure_sensitive_screenshot_control(
                selector,
                expected_page_context=expected_page_context,
            )
            with SessionLocal() as db:
                run = db.get(models.OperatorRun, run_id)
                state = dict(run.confidence_state or {})
                state["user_pause"] = {
                    "active": True,
                    "paused_from_state": "RUNNING",
                }
                state["session_control"] = {
                    "status": "paused",
                    "reason": "Paused during privacy rebind",
                }
                run.confidence_state = state
                run.current_state = "WAITING_FOR_USER"
                run.status_message = "Paused during privacy rebind."
                db.commit()
            return result

    async def forbidden_loop(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        loop_calls.append(1)
        return {"state": "COMPLETED", "actions": 0, "receipts": []}

    monkeypatch.setattr(extension_run, "run_vision_loop", forbidden_loop)
    body = PausingRebindBody()
    summary = asyncio.run(
        drive_in_browser_application(
            body=body,
            run_id=run_id,
            thread_id=thread_id,
            url=None,
            skip_navigate=True,
            sensitive_continue_request_id=scheduled[0],
        )
    )

    assert summary["state"] == "WAITING_FOR_USER"
    assert summary["reason"] == "session_paused"
    assert [call[0] for call in body.calls] == ["ensure"]
    assert loop_calls == []
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        takeover = run.confidence_state["sensitive_browser_takeover"]
        assert run.current_state == "WAITING_FOR_USER"
        assert run.status_message == "Paused during privacy rebind."
        assert run.confidence_state["session_control"]["status"] == "paused"
        assert takeover["active"] is True
        assert takeover["status"] == "waiting_for_explicit_continue"
        assert takeover["screenshot_mask_rebind_required"] is True

    with TestClient(app) as client:
        continue_while_paused = client.post(
            f"/operator/runs/{run_id}/sensitive-browser-continue",
            json={
                "action": "sensitive_browser_continue",
                "handoff_id": handoff_id,
            },
        )
    assert continue_while_paused.status_code == 400


def test_generic_drive_cannot_bypass_consumed_sensitive_continue_binding(
    monkeypatch,
) -> None:
    run_id, thread_id = _create_active_sensitive_takeover_run()
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        handoff_id = run.confidence_state["sensitive_browser_takeover"]["handoff_id"]
    monkeypatch.setattr(
        extension_run,
        "schedule_resume_if_in_browser",
        lambda _run_id, **_kwargs: True,
    )
    extension_run.continue_sensitive_browser_takeover(
        run_id,
        handoff_id=handoff_id,
    )
    loop_calls: list[int] = []

    async def forbidden_loop(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        loop_calls.append(1)
        return {"state": "COMPLETED", "actions": 0, "receipts": []}

    monkeypatch.setattr(extension_run, "run_vision_loop", forbidden_loop)
    body = _SensitiveContinueRebindBody()
    summary = asyncio.run(
        drive_in_browser_application(
            body=body,
            run_id=run_id,
            thread_id=thread_id,
            url=None,
            skip_navigate=True,
        )
    )

    assert summary["state"] == "WAITING_FOR_USER"
    assert summary["reason"] == "sensitive_browser_continue_authority_required"
    assert body.calls == []
    assert loop_calls == []


def test_resume_after_a_used_continuation_hands_the_run_back_with_continue_armed(monkeypatch) -> None:
    """Run 1793: the authorized continuation ran and ended; a later plain resume was refused but left
    the run RUNNING, so the app showed "Working" with no Continue. It now re-arms the same control."""
    run_id, thread_id = _create_active_sensitive_takeover_run()
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        handoff_id = run.confidence_state["sensitive_browser_takeover"]["handoff_id"]
    monkeypatch.setattr(extension_run, "schedule_resume_if_in_browser", lambda _run_id, **_kwargs: True)
    extension_run.continue_sensitive_browser_takeover(run_id, handoff_id=handoff_id)
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        state = dict(run.confidence_state)
        takeover = dict(state["sensitive_browser_takeover"])
        takeover["status"] = "mask_rebound"  # the authorized drive got through
        takeover["authorized_drive_finished_at"] = "2026-10-09T00:00:00"  # ... and then ended
        state["sensitive_browser_takeover"] = takeover
        run.confidence_state = state
        run.current_state = "RUNNING"
        db.commit()
    loop_calls: list[int] = []

    async def forbidden_loop(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        loop_calls.append(1)
        return {"state": "COMPLETED", "actions": 0, "receipts": []}

    monkeypatch.setattr(extension_run, "run_vision_loop", forbidden_loop)
    body = _SensitiveContinueRebindBody()
    summary = asyncio.run(
        drive_in_browser_application(body=body, run_id=run_id, thread_id=thread_id, url=None, skip_navigate=True)
    )

    assert summary["reason"] == "sensitive_browser_continue_authority_required"
    assert body.calls == [] and loop_calls == []
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        takeover = run.confidence_state["sensitive_browser_takeover"]
        assert run.current_state == "WAITING_FOR_USER"
        assert takeover["active"] is True
        assert takeover["status"] == "waiting_for_explicit_continue"
        assert "continue_request_id" not in takeover
        fresh_handoff_id = takeover["handoff_id"]
        assert fresh_handoff_id and fresh_handoff_id != handoff_id
    # A replay of the consumed Continue is stale; the user's fresh Continue works.
    with pytest.raises(ValueError):
        extension_run.continue_sensitive_browser_takeover(run_id, handoff_id=handoff_id)
    extension_run.continue_sensitive_browser_takeover(run_id, handoff_id=fresh_handoff_id)
    with SessionLocal() as db:
        assert db.get(models.OperatorRun, run_id).confidence_state["sensitive_browser_takeover"]["status"] == "resume_scheduled"


def test_a_resume_while_the_authorized_continuation_may_still_run_does_not_rearm(monkeypatch) -> None:
    run_id, thread_id = _create_active_sensitive_takeover_run()
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        handoff_id = run.confidence_state["sensitive_browser_takeover"]["handoff_id"]
    monkeypatch.setattr(extension_run, "schedule_resume_if_in_browser", lambda _run_id, **_kwargs: True)
    extension_run.continue_sensitive_browser_takeover(run_id, handoff_id=handoff_id)
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        state = dict(run.confidence_state)
        takeover = dict(state["sensitive_browser_takeover"])
        takeover["status"] = "mask_rebound"  # no authorized_drive_finished_at: it may still be running
        state["sensitive_browser_takeover"] = takeover
        run.confidence_state = state
        db.commit()

    async def forbidden_loop(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("an unauthorized drive must not start the loop")

    monkeypatch.setattr(extension_run, "run_vision_loop", forbidden_loop)
    asyncio.run(
        drive_in_browser_application(
            body=_SensitiveContinueRebindBody(), run_id=run_id, thread_id=thread_id, url=None, skip_navigate=True
        )
    )
    with SessionLocal() as db:
        takeover = db.get(models.OperatorRun, run_id).confidence_state["sensitive_browser_takeover"]
        assert takeover["status"] == "mask_rebound" and takeover["active"] is False
        assert takeover["handoff_id"] == handoff_id


def test_generic_scheduled_drive_cannot_consume_sensitive_takeover(monkeypatch) -> None:
    run_id, _thread_id = _create_active_sensitive_takeover_run()
    loop_calls: list[int] = []

    async def forbidden_loop(*args: Any, **kwargs: Any) -> dict[str, Any]:
        loop_calls.append(1)
        return {"state": "COMPLETED", "actions": 0, "receipts": []}

    monkeypatch.setattr(extension_run, "run_vision_loop", forbidden_loop)
    summary = asyncio.run(
        drive_in_browser_application(
            body=object(),
            run_id=run_id,
            thread_id=_thread_id,
            url=None,
            skip_navigate=True,
        )
    )

    assert summary["state"] == "WAITING_FOR_USER"
    assert summary["reason"] == "sensitive_browser_takeover"
    assert loop_calls == []
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        takeover = run.confidence_state["sensitive_browser_takeover"]
        assert takeover["active"] is True
        assert takeover["status"] == "waiting_for_explicit_continue"


def test_sensitive_continue_scheduler_failure_restores_active_waiting_marker(monkeypatch) -> None:
    run_id, _thread_id = _create_active_sensitive_takeover_run()
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        handoff_id = run.confidence_state["sensitive_browser_takeover"]["handoff_id"]
    monkeypatch.setattr(
        extension_run,
        "schedule_resume_if_in_browser",
        lambda _run_id, **_kwargs: False,
    )

    with TestClient(app) as client:
        response = client.post(
            f"/operator/runs/{run_id}/sensitive-browser-continue",
            json={
                "action": "sensitive_browser_continue",
                "handoff_id": handoff_id,
            },
        )

    assert response.status_code == 400
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        takeover = run.confidence_state["sensitive_browser_takeover"]
        assert run.current_state == "WAITING_FOR_USER"
        assert takeover["active"] is True
        assert takeover["status"] == "resume_failed"


def test_cancelling_sensitive_takeover_unlocks_future_chat() -> None:
    run_id, _thread_id = _create_active_sensitive_takeover_run()
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        user_id = int(run.user_id)
        OperatorRuntimeEngine(db).cancel_run(run_id, notify_chat=False)
        global_thread = OperatorChatService(db).create_thread(
            user_id=user_id,
            title="Chat after stopped takeover",
        )
        db.commit()
        global_thread_id = int(global_thread.id)

    with TestClient(app) as client:
        response = client.post(
            f"/operator/chat/threads/{global_thread_id}/messages",
            json={
                "content": "Start a different ordinary browser task.",
                "payload": {"source": "user_mode_chat"},
            },
        )

    assert response.status_code == 200
    with SessionLocal() as db:
        cancelled = db.get(models.OperatorRun, run_id)
        takeover = cancelled.confidence_state["sensitive_browser_takeover"]
        assert cancelled.current_state == "CANCELLED"
        assert takeover["active"] is False
        assert takeover["status"] == "cancelled"


def _unavailable_read() -> dict[str, Any]:
    return {
        "verdict": "uncertain",
        "confidence": 0.0,
        "reason": "The model-owned secret-risk classifier was unavailable.",
        "evidence": "No trustworthy semantic verdict was produced.",
        "metadata": {"normalization": "provider_unavailable"},
    }


def _run_display_name_type_through_outage(
    monkeypatch, *, outage_calls: int, deferrals: int, max_steps: int
) -> tuple[dict[str, Any], list[Any], Any]:
    init_db()
    body = _ConflictingTypeTargetBody()
    events: list[tuple[str, dict[str, Any]]] = []
    calls: list[Any] = []
    primary = _gate_model(801, "vendor/gate-flash")
    _patch_loop_dependencies(monkeypatch, events=events)
    monkeypatch.setattr(extension_run, "_SECRET_CLASSIFIER_DEFERRALS_MAX", deferrals)
    monkeypatch.setattr(extension_run, "_vision_provider_for_user", lambda _user_id: primary)
    monkeypatch.setattr(extension_run, "_vision_backup_provider", lambda *_a, **_k: None)

    def classify(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs.get("deliberate"))
        if len(calls) <= outage_calls:
            return _unavailable_read()
        return dict(_ORDINARY_RECOVERY_VERDICT)

    monkeypatch.setattr(extension_run, "_call_secret_request_classifier", classify)
    monkeypatch.setattr(
        extension_run,
        "_decide_vision_action",
        lambda *args, **kwargs: {
            "action": "type",
            "control": 1,
            "question": "Display name",
            "text": "Jordan",
            "grounding_source": "profile",
            "grounding_quote": "Jordan",
            "reason": "Enter the grounded profile name.",
        },
    )
    summary = asyncio.run(
        run_vision_loop(
            body,
            url=None,
            narrate=lambda *args, **kwargs: asyncio.sleep(0),
            run_id=0,
            user_id=0,
            skip_navigate=True,
            max_steps=max_steps,
        )
    )
    return summary, calls, body


def test_classifier_outage_defers_the_action_to_the_brain_then_types_once_it_recovers(monkeypatch) -> None:
    # Two reads (primary + same-provider retry) fail on each of two steps; the third step's read answers.
    summary, calls, body = _run_display_name_type_through_outage(
        monkeypatch, outage_calls=4, deferrals=3, max_steps=4
    )

    assert summary["reason"] != "secret_classifier_unavailable", summary
    assert len(calls) >= 5
    assert body.type_calls and set(body.type_calls) == {("#display-name", "Jordan")}


def test_classifier_outage_blocks_closed_once_the_deferrals_are_spent(monkeypatch) -> None:
    summary, calls, body = _run_display_name_type_through_outage(
        monkeypatch, outage_calls=100, deferrals=2, max_steps=8
    )

    assert summary["state"] == "BLOCKED"
    assert summary["reason"] == "secret_classifier_unavailable"
    assert len(calls) == 6, "two reads per step for the initial step and each of the two deferrals"
    assert body.type_calls == []


def test_same_provider_classifier_retry_waits_for_its_backoff(monkeypatch) -> None:
    import time as _time

    monkeypatch.setattr(extension_run, "_SECRET_CLASSIFIER_RETRY_BACKOFF_SECONDS", 0.3)
    started = _time.monotonic()
    summary, calls, _body = _run_display_name_type_through_outage(
        monkeypatch, outage_calls=1, deferrals=0, max_steps=1
    )

    assert len(calls) >= 2
    assert _time.monotonic() - started >= 0.3
    assert summary["reason"] != "secret_classifier_unavailable"



def test_vault_credential_continue_drops_a_stale_mask_and_reobserves(monkeypatch) -> None:
    """Run 1794: the login page drifted while the user filled the Credentials card, and every Continue
    failed the mask rebind. A vault credential was never typed into Chrome, so the stale binding is
    dropped and the run re-observes; direct entry (the test above) still blocks."""
    run_id, thread_id = _create_active_sensitive_takeover_run()
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        state = dict(run.confidence_state)
        takeover = dict(state["sensitive_browser_takeover"])
        takeover["kind"] = "credential_vault"
        takeover["takeover_kind"] = "credential_vault"
        state["sensitive_browser_takeover"] = takeover
        run.confidence_state = state
        db.commit()
        handoff_id = takeover["handoff_id"]
    scheduled: list[str] = []
    monkeypatch.setattr(
        extension_run,
        "schedule_resume_if_in_browser",
        lambda _run_id, **kwargs: scheduled.append(str(kwargs.get("sensitive_continue_request_id") or "")) or True,
    )
    extension_run.continue_sensitive_browser_takeover(run_id, handoff_id=handoff_id)
    loop_calls: list[int] = []

    async def stop_loop(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        loop_calls.append(1)
        return {"state": "WAITING_FOR_USER", "reason": "fixture_stop_after_rebind", "actions": 0, "receipts": []}

    monkeypatch.setattr(extension_run, "run_vision_loop", stop_loop)
    body = _SensitiveContinueRebindBody(rebind_ok=False)
    asyncio.run(
        drive_in_browser_application(
            body=body,
            run_id=run_id,
            thread_id=thread_id,
            url=None,
            skip_navigate=True,
            sensitive_continue_request_id=scheduled[0],
        )
    )

    assert loop_calls == [1]
    with SessionLocal() as db:
        takeover = db.get(models.OperatorRun, run_id).confidence_state["sensitive_browser_takeover"]
        # The authorized drive ran and ended, so the vault takeover is complete.
        assert takeover["status"] == "completed"
        assert takeover["stale_vault_mask_dropped_at"]


def test_a_verified_vault_replay_completes_the_takeover_so_later_resumes_run(monkeypatch) -> None:
    """Run 1794: long after the sign-in, an ordinary resume still demanded the secure Continue because
    the consumed takeover stayed mask_rebound. A verified replay of its credential completes it."""
    run_id, thread_id = _create_active_sensitive_takeover_run()
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        state = dict(run.confidence_state)
        takeover = dict(state["sensitive_browser_takeover"])
        takeover.update(
            {
                "kind": "credential_vault",
                "active": False,
                "status": "mask_rebound",
                "credential_origin": "https://login.example.test",
                "authorized_drive_finished_at": "2026-10-09T00:00:00",
            }
        )
        state["sensitive_browser_takeover"] = takeover
        run.confidence_state = state
        run.current_state = "BLOCKED"
        db.commit()

    extension_run._complete_vault_takeover_after_replay(run_id, "https://other.example.test")
    with SessionLocal() as db:
        assert db.get(models.OperatorRun, run_id).confidence_state["sensitive_browser_takeover"]["status"] == "mask_rebound"
    extension_run._complete_vault_takeover_after_replay(run_id, "https://login.example.test")
    with SessionLocal() as db:
        assert db.get(models.OperatorRun, run_id).confidence_state["sensitive_browser_takeover"]["status"] == "completed"

    loop_calls: list[int] = []

    async def stop_loop(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        loop_calls.append(1)
        return {"state": "WAITING_FOR_USER", "reason": "fixture_stop", "actions": 0, "receipts": []}

    monkeypatch.setattr(extension_run, "run_vision_loop", stop_loop)
    asyncio.run(
        drive_in_browser_application(
            body=_SensitiveContinueRebindBody(), run_id=run_id, thread_id=thread_id, url=None, skip_navigate=True
        )
    )
    assert loop_calls == [1]
