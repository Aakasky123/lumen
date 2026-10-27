from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from app import credential_vault, extension_run, models
from app.db import SessionLocal, init_db
from app.extension_run import drive_in_browser_application, run_vision_loop


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


class _MetadataBody:
    def __init__(self, url: str) -> None:
        self.url = url

    async def capture_screenshot(self) -> dict[str, Any]:
        return {
            "ok": True,
            "image": "jpeg",
            "width": 800,
            "height": 600,
            "url": self.url,
            "title": "Account",
            "surface_signature": "credential-metadata-surface-v1",
            "scroll_y": 0,
            "scroll_height": 600,
            "page_evidence": {
                "viewport_text": "The account page is visible.",
                "controls": [],
            },
        }


class _CredentialBody:
    def __init__(
        self,
        url: str,
        *,
        credential_result_overrides: dict[str, Any] | None = None,
    ) -> None:
        self.url = url
        self.typed = False
        self.credential_result_overrides = dict(
            credential_result_overrides or {}
        )
        self.credential_calls: list[tuple[str, str, dict[str, Any]]] = []
        self.ordinary_type_calls: list[tuple[str, str]] = []
        self.sensitive_mask_calls: list[tuple[str, dict[str, Any]]] = []

    async def capture_screenshot(self) -> dict[str, Any]:
        controls = (
            []
            if self.typed
            else [
                {
                    "index": 1,
                    "tag": "input",
                    "type": "email",
                    "role": "textbox",
                    "context_text": "Account email",
                    "selector": "#account-email",
                    "x": 100,
                    "y": 140,
                    "width": 360,
                    "height": 44,
                    "value": "",
                }
            ]
        )
        return {
            "ok": True,
            "image": "jpeg",
            "width": 800,
            "height": 600,
            "url": self.url,
            "title": "Account login",
            "surface_signature": "credential-login-surface-v1",
            "scroll_y": 0,
            "scroll_height": 600,
            "page_evidence": {
                "viewport_text": (
                    "Account is signed in."
                    if self.typed
                    else "Account email Sign in to your account."
                ),
                "controls": controls,
            },
        }

    async def mark_sensitive_screenshot_control(
        self,
        selector: str,
        *,
        expected_page_context: dict[str, Any],
    ) -> dict[str, Any]:
        self.sensitive_mask_calls.append(
            (selector, dict(expected_page_context))
        )
        return {
            "ok": True,
            "action": "mark_sensitive_screenshot_control",
            "selector_bound": True,
            "page_context_bound": True,
            "mask_surface_signature": "credential-mask-surface-v1",
            "marked_control_count": len(self.sensitive_mask_calls),
        }

    async def vision_type_credential_control(
        self,
        selector: str,
        text: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.typed = True
        self.credential_calls.append((selector, text, dict(kwargs)))
        # Return deliberately hostile plaintext read-back fields. The runtime must
        # strip these even if an older or compromised body fails to do so itself.
        return {
            "ok": True,
            "action": "type",
            "mode": "control_selector",
            "active_typable": True,
            "active_has_value": True,
            "active_value_matches_expected": True,
            "active_tag": "input",
            "active_selector": selector,
            "active_value": text,
            "active_value_ledger": text,
            "before_value": text,
            "owner_value_before": text,
            "owner_value_after": text,
            "selected_value": text,
            "target_text": text,
            **self.credential_result_overrides,
        }

    async def vision_type_control(
        self,
        selector: str,
        text: str,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        self.ordinary_type_calls.append((selector, text))
        raise AssertionError(
            "credential replay must not use ordinary vision_type_control"
        )


def _create_run(*, target_url: str) -> tuple[int, int]:
    init_db()
    with SessionLocal() as db:
        user = models.User(
            email=f"credential-runtime-{uuid.uuid4().hex}@example.test",
            name="Credential Runtime",
        )
        db.add(user)
        db.flush()
        run = models.OperatorRun(
            user_id=user.id,
            application_id=None,
            goal="Sign in to the visible account.",
            target_url=target_url,
            current_state="RUNNING",
        )
        db.add(run)
        db.commit()
        return int(run.id), int(user.id)


def _patch_loop_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    events: list[tuple[str, dict[str, Any]]],
) -> None:
    monkeypatch.setattr(
        "app.candidate_context.CandidateContextEngine",
        _ContextEngine,
    )
    monkeypatch.setattr(
        extension_run,
        "_record_context_usage_for_run",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        extension_run,
        "_db_event",
        lambda _run_id, event_type, payload: events.append(
            (event_type, payload)
        ),
    )


def _accepted_credential_classifier(
    *_args: Any,
    **kwargs: Any,
) -> dict[str, Any]:
    slot = str(
        (kwargs.get("action_evidence") or {}).get("credential_slot") or "email"
    )
    return {
        "slot": slot,
        "usage": "login",
        "confidence": 0.99,
        "reason": "The rendered exact control requests this reusable account credential.",
        "evidence": "The account sign-in surface and selected control agree.",
    }


def _use_credential_action(
    *,
    item_id: int,
    slot: str = "email",
) -> dict[str, Any]:
    return {
        "action": "use_credential",
        "control": 1,
        "x": 220,
        "y": 160,
        "text": "",
        "question": "Account email",
        "credential_item_id": item_id,
        "credential_slot": slot,
        "reason": "Use the selected exact-origin account credential.",
    }


async def _quiet_narrator(
    _kind: str,
    _data: dict[str, Any],
) -> None:
    return None


def test_runtime_context_contains_only_exact_origin_vault_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_db()
    unique = uuid.uuid4().hex
    origin = f"https://tenant-{unique}.example.test"
    other_origin = f"https://other-{unique}.example.test"
    browser_url = f"{origin}/account/login?return=%2Fbookings"
    exact_secret = f"exact-secret-{unique}"
    other_secret = f"other-secret-{unique}"

    with SessionLocal() as db:
        user = models.User(
            email=f"credential-context-{unique}@example.test",
            name="Credential Context",
        )
        db.add(user)
        db.flush()
        exact_item = credential_vault.create(
            db,
            user_id=user.id,
            origin=origin,
            account_label="Exact tenant",
            fields={"email": f"exact-{unique}@mail.test", "password": exact_secret},
            auto_use=True,
        )
        credential_vault.create(
            db,
            user_id=user.id,
            origin=other_origin,
            account_label="Other tenant",
            fields={"email": f"other-{unique}@mail.test", "password": other_secret},
            auto_use=True,
        )
        db.commit()
        user_id = int(user.id)
        exact_item_id = int(exact_item.id)

    contexts: list[dict[str, Any]] = []
    events: list[tuple[str, dict[str, Any]]] = []
    _patch_loop_dependencies(monkeypatch, events=events)

    def decide(
        _user_id: int,
        _image: dict[str, Any],
        _view: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        contexts.append(context)
        return {
            "action": "done",
            "reason": "The metadata context has been inspected.",
        }

    monkeypatch.setattr(extension_run, "_decide_vision_action", decide)

    asyncio.run(
        run_vision_loop(
            _MetadataBody(browser_url),
            url=None,
            narrate=_quiet_narrator,
            run_id=0,
            user_id=user_id,
            skip_navigate=True,
            max_steps=1,
        )
    )

    assert len(contexts) == 1
    metadata = contexts[0]["credential_vault"]
    assert [item["id"] for item in metadata] == [exact_item_id]
    assert metadata[0]["origin"] == origin
    assert set(metadata[0]) == {
        "id",
        "origin",
        "account_label",
        "available_slots",
        "auto_use",
        "created_at",
        "updated_at",
        "last_used_at",
        "revoked_at",
    }
    serialized = json.dumps(contexts, default=str)
    assert exact_secret not in serialized
    assert other_secret not in serialized
    assert other_origin not in serialized
    assert "encrypted_payload_json" not in serialized


def test_item_zero_creates_value_free_credential_vault_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin = f"https://handoff-{uuid.uuid4().hex}.example.test"
    browser_url = f"{origin}/login"
    run_id, user_id = _create_run(target_url=browser_url)
    body = _CredentialBody(browser_url)
    events: list[tuple[str, dict[str, Any]]] = []
    narrations: list[tuple[str, dict[str, Any]]] = []
    resolve_calls: list[dict[str, Any]] = []
    _patch_loop_dependencies(monkeypatch, events=events)
    monkeypatch.setattr(
        extension_run,
        "_credential_vault_metadata_for_url",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        extension_run,
        "_credential_vault_resolve_slot",
        lambda *args, **kwargs: resolve_calls.append(kwargs) or "forbidden",
    )
    monkeypatch.setattr(
        extension_run,
        "_call_credential_target_classifier",
        _accepted_credential_classifier,
    )
    monkeypatch.setattr(
        extension_run,
        "_decide_vision_action",
        lambda *args, **kwargs: _use_credential_action(item_id=0),
    )

    async def narrate(kind: str, data: dict[str, Any]) -> None:
        narrations.append((kind, data))

    summary = asyncio.run(
        run_vision_loop(
            body,
            url=None,
            narrate=narrate,
            run_id=run_id,
            user_id=user_id,
            skip_navigate=True,
            max_steps=1,
        )
    )

    assert summary["state"] == "WAITING_FOR_USER"
    assert summary["reason"] == "sensitive_browser_takeover"
    assert summary["ask"]["takeover_kind"] == "credential_vault"
    assert summary["ask"]["credential_origin"] == origin
    assert summary["ask"]["credential_slot"] == "email"
    assert summary["ask"]["credential_item_id"] == 0
    assert summary["ask"]["persist_answer"] is False
    assert "value" not in summary["ask"]
    assert "proposed_answer" not in summary["ask"]
    assert resolve_calls == []
    assert body.credential_calls == []
    assert body.ordinary_type_calls == []
    assert body.sensitive_mask_calls[0][0] == "#account-email"

    with SessionLocal() as db:
        questions = list(
            db.scalars(
                select(models.OperatorQuestion).where(
                    models.OperatorQuestion.run_id == run_id
                )
            ).all()
        )
    assert questions == []
    assert not any("value" in payload for _, payload in events)
    assert "credential_vault" in json.dumps(summary, default=str)
    assert "operator_question_created" not in json.dumps(summary, default=str)
    handoff_event = next(
        payload
        for event_type, payload in events
        if event_type == "VISION_CREDENTIAL_VAULT_HANDOFF_REQUIRED"
    )
    assert handoff_event["browser_mutation_attempted"] is False
    assert handoff_event["secret_question_persisted"] is False
    assert handoff_event["credential_origin"] == origin
    assert handoff_event["credential_slot"] == "email"
    assert narrations


def test_credential_handoff_publication_persists_vault_marker_without_question_or_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin = f"https://publish-{uuid.uuid4().hex}.example.test"
    browser_url = f"{origin}/account/login"
    run_id, user_id = _create_run(target_url=browser_url)
    credential_item_id = 24680
    expected_message = (
        "Lumen needs an account credential saved for this exact site. "
        "Add it in the secure Credentials card, then continue."
    )
    screenshot_mask_binding = {
        "mode": "model_bound_selector",
        "selector": "#account-password",
        "page_context": {
            "url": browser_url,
            "mask_surface_signature": "credential-publish-mask-v1",
        },
    }

    with SessionLocal() as db:
        thread = models.ChatThread(
            user_id=user_id,
            run_id=run_id,
            title="Credential publication",
            mode="run",
        )
        db.add(thread)
        db.commit()
        thread_id = int(thread.id)

    async def fake_loop(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "state": "WAITING_FOR_USER",
            "reason": "sensitive_browser_takeover",
            "ask": {
                # The publication layer must use its credential-specific message,
                # not trust arbitrary model/runtime label prose for the sidebar.
                "label": "Enter a secret into chat.",
                "options": [],
                "sensitive_browser_takeover": True,
                "persist_answer": False,
                "takeover_kind": "credential_vault",
                "credential_origin": origin,
                "credential_slot": "password",
                "credential_item_id": credential_item_id,
                "screenshot_mask_binding": screenshot_mask_binding,
            },
            "actions": 0,
            "receipts": [],
        }

    monkeypatch.setattr(extension_run, "run_vision_loop", fake_loop)

    summary = asyncio.run(
        drive_in_browser_application(
            body=object(),
            run_id=run_id,
            thread_id=thread_id,
            url=None,
            skip_navigate=True,
        )
    )

    assert summary["state"] == "WAITING_FOR_USER"
    assert summary["reason"] == "sensitive_browser_takeover"

    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        marker = dict(
            (run.confidence_state or {}).get("sensitive_browser_takeover")
            or {}
        )
        questions = list(
            db.scalars(
                select(models.OperatorQuestion).where(
                    models.OperatorQuestion.run_id == run_id
                )
            ).all()
        )
        tasks = list(
            db.scalars(
                select(models.RuntimeTask).where(
                    models.RuntimeTask.run_id == run_id
                )
            ).all()
        )
        messages = list(
            db.scalars(
                select(models.ChatMessage)
                .where(models.ChatMessage.thread_id == thread_id)
                .order_by(models.ChatMessage.id.asc())
            ).all()
        )
        presented = db.scalars(
            select(models.CognitiveEvent)
            .where(
                models.CognitiveEvent.run_id == run_id,
                models.CognitiveEvent.event_type
                == "IN_BROWSER_SENSITIVE_TAKEOVER_PRESENTED",
            )
            .order_by(models.CognitiveEvent.id.desc())
        ).first()

    assert run.current_state == "WAITING_FOR_USER"
    assert run.status_message == expected_message
    assert marker["active"] is True
    assert marker["persist_answer"] is False
    assert marker["continue_control"] == "sensitive_browser_continue"
    assert marker["kind"] == "credential_vault"
    assert marker["takeover_kind"] == "credential_vault"
    assert marker["credential_origin"] == origin
    assert marker["credential_slot"] == "password"
    assert marker["credential_item_id"] == credential_item_id
    assert marker["screenshot_mask_binding"] == screenshot_mask_binding
    assert questions == []
    assert tasks == []
    assert [message.content for message in messages] == [expected_message]
    assert messages[0].payload_json["takeover_kind"] == "credential_vault"
    assert messages[0].payload_json["credential_origin"] == origin
    assert messages[0].payload_json["credential_slot"] == "password"
    assert presented is not None
    assert presented.event_payload["operator_question_created"] is False
    assert presented.event_payload["runtime_task_created"] is False
    assert presented.event_payload["takeover_kind"] == "credential_vault"
    assert presented.event_payload["credential_origin"] == origin
    assert presented.event_payload["credential_slot"] == "password"
    assert "Enter a secret into chat." not in json.dumps(
        {
            "state": run.confidence_state,
            "messages": [
                {
                    "content": message.content,
                    "payload": message.payload_json,
                }
                for message in messages
            ],
        },
        default=str,
    )


def test_valid_slot_uses_dedicated_dispatch_without_plaintext_leakage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unique = uuid.uuid4().hex
    origin = f"https://replay-{unique}.example.test"
    browser_url = f"{origin}/account/login"
    secret = f"credential-email-{unique}@mail.test"
    item_id = 77123
    run_id, user_id = _create_run(target_url=browser_url)
    body = _CredentialBody(
        browser_url,
        credential_result_overrides={
            "active_label": secret,
            "active_name": secret,
            "active_id": secret,
            "active_selector": secret,
            "selector": secret,
            "page_control_identity": {
                "label": secret,
                "name": secret,
                "id": secret,
                "selector": secret,
            },
        },
    )
    contexts: list[dict[str, Any]] = []
    events: list[tuple[str, dict[str, Any]]] = []
    narrations: list[tuple[str, dict[str, Any]]] = []
    resolver_calls: list[dict[str, Any]] = []
    marked_used: list[dict[str, Any]] = []
    _patch_loop_dependencies(monkeypatch, events=events)
    metadata = [
        {
            "id": item_id,
            "origin": origin,
            "account_label": "Primary",
            "available_slots": ["email", "password"],
            "auto_use": True,
            "created_at": None,
            "updated_at": None,
            "last_used_at": None,
            "revoked_at": None,
        }
    ]
    monkeypatch.setattr(
        extension_run,
        "_credential_vault_metadata_for_url",
        lambda *_args, **_kwargs: [dict(metadata[0])],
    )

    def resolve(
        _user_id: int,
        **kwargs: Any,
    ) -> str:
        resolver_calls.append(dict(kwargs))
        return secret

    monkeypatch.setattr(
        extension_run,
        "_credential_vault_resolve_slot",
        resolve,
    )
    monkeypatch.setattr(
        extension_run,
        "_credential_vault_mark_used",
        lambda _user_id, **kwargs: marked_used.append(dict(kwargs)),
    )
    monkeypatch.setattr(
        extension_run,
        "_call_credential_target_classifier",
        _accepted_credential_classifier,
    )

    def decide(
        _user_id: int,
        _image: dict[str, Any],
        _view: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        contexts.append(context)
        if not body.typed:
            return _use_credential_action(item_id=item_id)
        return {
            "action": "done",
            "reason": "The account credential was verified in the browser.",
        }

    monkeypatch.setattr(extension_run, "_decide_vision_action", decide)

    async def narrate(kind: str, data: dict[str, Any]) -> None:
        narrations.append((kind, data))

    summary = asyncio.run(
        run_vision_loop(
            body,
            url=None,
            narrate=narrate,
            run_id=run_id,
            user_id=user_id,
            skip_navigate=True,
            max_steps=3,
        )
    )

    assert summary["state"] == "READY_FOR_REVIEW"
    assert len(body.credential_calls) == 1
    selector, dispatched_value, dispatch_kwargs = body.credential_calls[0]
    assert selector == "#account-email"
    assert dispatched_value == secret
    assert dispatch_kwargs["expected_page_context"]["url"] == browser_url
    assert body.ordinary_type_calls == []
    assert body.sensitive_mask_calls[0][0] == "#account-email"
    assert resolver_calls == [
        {"item_id": item_id, "slot": "email", "url": browser_url}
    ]
    assert marked_used == [{"item_id": item_id, "url": browser_url}]
    assert summary["receipts"][0]["outcome"] == "verified_succeeded"
    assert "value" not in summary["receipts"][0]["target"]
    assert secret not in json.dumps(summary["receipts"][0], default=str)
    assert (
        summary["receipts"][0]["verification"]["active_value_redacted"]
        is True
    )

    with SessionLocal() as db:
        persisted_state = dict(
            db.get(models.OperatorRun, run_id).confidence_state or {}
        )

    serialized_external_state = json.dumps(
        {
            "events": events,
            "narrations": narrations,
            "contexts": contexts,
            "summary": summary,
            "persisted_state": persisted_state,
        },
        default=str,
    )
    assert secret not in serialized_external_state
    assert "[REDACTED]" in serialized_external_state
    assert any(
        event_type == "VISION_CREDENTIAL_REPLAY_VERIFIED"
        and payload["value_redacted"] is True
        for event_type, payload in events
    )
    assert len(contexts) >= 2
    assert secret not in json.dumps(
        contexts[-1].get("recent_actions") or [],
        default=str,
    )
    assert secret not in json.dumps(
        contexts[-1].get("session_ledger") or {},
        default=str,
    )


@pytest.mark.parametrize(
    "metadata_factory",
    [
        lambda item_id, origin: {
            "id": item_id,
            "origin": f"https://wrong-{uuid.uuid4().hex}.example.test",
            "account_label": "Wrong origin",
            "available_slots": ["email"],
            "auto_use": True,
        },
        lambda item_id, origin: {
            "id": item_id,
            "origin": origin,
            "account_label": "Disabled",
            "available_slots": ["email"],
            "auto_use": False,
        },
    ],
    ids=["wrong-origin", "auto-use-disabled"],
)
def test_unavailable_item_metadata_is_rejected_before_vault_resolution(
    monkeypatch: pytest.MonkeyPatch,
    metadata_factory: Any,
) -> None:
    origin = f"https://reject-{uuid.uuid4().hex}.example.test"
    browser_url = f"{origin}/login"
    item_id = 99221
    run_id, user_id = _create_run(target_url=browser_url)
    body = _CredentialBody(browser_url)
    events: list[tuple[str, dict[str, Any]]] = []
    contexts: list[dict[str, Any]] = []
    resolver_calls: list[dict[str, Any]] = []
    _patch_loop_dependencies(monkeypatch, events=events)
    monkeypatch.setattr(
        extension_run,
        "_credential_vault_metadata_for_url",
        lambda *_args, **_kwargs: [
            metadata_factory(item_id, origin)
        ],
    )
    monkeypatch.setattr(
        extension_run,
        "_credential_vault_resolve_slot",
        lambda *args, **kwargs: resolver_calls.append(dict(kwargs))
        or "forbidden",
    )
    monkeypatch.setattr(
        extension_run,
        "_call_credential_target_classifier",
        _accepted_credential_classifier,
    )

    def decide(
        _user_id: int,
        _image: dict[str, Any],
        _view: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        contexts.append(context)
        return _use_credential_action(item_id=item_id)

    monkeypatch.setattr(extension_run, "_decide_vision_action", decide)

    summary = asyncio.run(
        run_vision_loop(
            body,
            url=None,
            narrate=_quiet_narrator,
            run_id=run_id,
            user_id=user_id,
            skip_navigate=True,
            max_steps=2,
        )
    )

    assert summary["state"] == "BLOCKED"
    assert summary["actions"] == 0
    assert resolver_calls == []
    assert body.credential_calls == []
    assert body.ordinary_type_calls == []
    assert body.sensitive_mask_calls == []
    assert len(contexts) == 2
    recent_feedback = json.dumps(
        contexts[-1].get("recent_actions") or [],
        default=str,
    )
    assert "credential_item_not_available_for_exact_origin" in recent_feedback


def test_credential_classifier_unavailable_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin = f"https://classifier-{uuid.uuid4().hex}.example.test"
    browser_url = f"{origin}/login"
    item_id = 88331
    run_id, user_id = _create_run(target_url=browser_url)
    body = _CredentialBody(browser_url)
    events: list[tuple[str, dict[str, Any]]] = []
    resolver_calls: list[dict[str, Any]] = []
    _patch_loop_dependencies(monkeypatch, events=events)
    monkeypatch.setattr(
        extension_run,
        "_credential_vault_metadata_for_url",
        lambda *_args, **_kwargs: [
            {
                "id": item_id,
                "origin": origin,
                "account_label": "Primary",
                "available_slots": ["email"],
                "auto_use": True,
            }
        ],
    )
    monkeypatch.setattr(
        extension_run,
        "_credential_vault_resolve_slot",
        lambda *args, **kwargs: resolver_calls.append(dict(kwargs))
        or "forbidden",
    )
    monkeypatch.setattr(
        extension_run,
        "_call_credential_target_classifier",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("classifier unavailable")
        ),
    )
    monkeypatch.setattr(
        extension_run,
        "_decide_vision_action",
        lambda *args, **kwargs: _use_credential_action(item_id=item_id),
    )

    summary = asyncio.run(
        run_vision_loop(
            body,
            url=None,
            narrate=_quiet_narrator,
            run_id=run_id,
            user_id=user_id,
            skip_navigate=True,
            max_steps=1,
        )
    )

    assert summary["state"] == "BLOCKED"
    assert summary["reason"] == "credential_target_classifier_unavailable"
    assert summary["block_kind"] == "awaiting_engineering"
    assert summary["system_block"] is True
    assert summary["actions"] == 0
    assert resolver_calls == []
    assert body.credential_calls == []
    assert body.ordinary_type_calls == []
    assert body.sensitive_mask_calls == []
    blocked_event = next(
        payload
        for event_type, payload in events
        if event_type == "VISION_CREDENTIAL_RUNTIME_BLOCKED"
    )
    assert blocked_event["reason"] == "credential_target_classifier_unavailable"
    assert blocked_event["browser_mutation_attempted"] is False
    assert blocked_event["vault_value_exposed"] is False
    assert blocked_event["operator_question_created"] is False


def test_credential_replay_mismatch_never_verifies_or_marks_item_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin = f"https://mismatch-{uuid.uuid4().hex}.example.test"
    browser_url = f"{origin}/login"
    secret = f"mismatch-{uuid.uuid4().hex}@mail.test"
    item_id = 91234
    run_id, user_id = _create_run(target_url=browser_url)
    body = _CredentialBody(
        browser_url,
        credential_result_overrides={
            "active_value_matches_expected": False,
        },
    )
    marked_used: list[dict[str, Any]] = []
    events: list[tuple[str, dict[str, Any]]] = []
    _patch_loop_dependencies(monkeypatch, events=events)
    monkeypatch.setattr(
        extension_run,
        "_credential_vault_metadata_for_url",
        lambda *_args, **_kwargs: [
            {
                "id": item_id,
                "origin": origin,
                "account_label": "Primary",
                "available_slots": ["email"],
                "auto_use": True,
            }
        ],
    )
    monkeypatch.setattr(
        extension_run,
        "_credential_vault_resolve_slot",
        lambda *_args, **_kwargs: secret,
    )
    monkeypatch.setattr(
        extension_run,
        "_credential_vault_mark_used",
        lambda _user_id, **kwargs: marked_used.append(dict(kwargs)),
    )
    monkeypatch.setattr(
        extension_run,
        "_call_credential_target_classifier",
        _accepted_credential_classifier,
    )

    def decide(
        _user_id: int,
        _image: dict[str, Any],
        _view: dict[str, Any],
        _context: dict[str, Any],
    ) -> dict[str, Any]:
        if not body.typed:
            return _use_credential_action(item_id=item_id)
        return {"action": "done", "reason": "Credential replay returned."}

    monkeypatch.setattr(extension_run, "_decide_vision_action", decide)
    summary = asyncio.run(
        run_vision_loop(
            body,
            url=None,
            narrate=_quiet_narrator,
            run_id=run_id,
            user_id=user_id,
            skip_navigate=True,
            max_steps=2,
        )
    )

    assert len(body.credential_calls) == 1
    assert marked_used == []
    assert summary["receipts"]
    receipt = summary["receipts"][0]
    assert receipt["outcome"] != "verified_succeeded"
    assert receipt["verification"]["active_value_matches_expected"] is False
    assert not any(
        event_type == "VISION_CREDENTIAL_REPLAY_VERIFIED"
        for event_type, _payload in events
    )
    assert secret not in json.dumps(
        {"summary": summary, "events": events},
        default=str,
    )


def test_ordinary_type_into_model_classified_credential_is_redirected_value_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin = f"https://ordinary-bypass-{uuid.uuid4().hex}.example.test"
    browser_url = f"{origin}/login"
    proposed_secret = f"bypass-{uuid.uuid4().hex}@mail.test"
    run_id, user_id = _create_run(target_url=browser_url)
    body = _CredentialBody(browser_url)
    contexts: list[dict[str, Any]] = []
    classifier_calls: list[dict[str, Any]] = []
    events: list[tuple[str, dict[str, Any]]] = []
    _patch_loop_dependencies(monkeypatch, events=events)
    monkeypatch.setattr(
        extension_run,
        "_credential_vault_metadata_for_url",
        lambda *_args, **_kwargs: [],
    )

    def classify(
        *_args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        classifier_calls.append(dict(kwargs))
        return {
            "slot": "email",
            "usage": "login",
            "confidence": 0.99,
            "reason": "The exact rendered control is an account login identifier.",
            "evidence": "The login surface and selected control agree.",
        }

    monkeypatch.setattr(
        extension_run,
        "_call_credential_target_classifier",
        classify,
    )
    monkeypatch.setattr(
        extension_run,
        "_call_secret_request_classifier",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError(
                "credential type must redirect before secret-value classification"
            )
        ),
    )

    def decide(
        _user_id: int,
        _image: dict[str, Any],
        _view: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        contexts.append(context)
        if len(contexts) == 1:
            return {
                "action": "type",
                "control": 1,
                "x": 220,
                "y": 160,
                "text": proposed_secret,
                "question": "Account email",
                "grounding_source": "profile",
                "grounding_quote": proposed_secret,
                "reason": "Type the account email.",
            }
        return {
            "action": "done",
            "reason": "The unsafe ordinary type was withheld.",
        }

    monkeypatch.setattr(extension_run, "_decide_vision_action", decide)
    summary = asyncio.run(
        run_vision_loop(
            body,
            url=None,
            narrate=_quiet_narrator,
            run_id=run_id,
            user_id=user_id,
            skip_navigate=True,
            max_steps=2,
        )
    )

    assert classifier_calls
    assert proposed_secret not in json.dumps(classifier_calls, default=str)
    assert body.ordinary_type_calls == []
    assert body.credential_calls == []
    assert body.sensitive_mask_calls == []
    assert summary["actions"] == 0
    assert len(contexts) >= 2
    repair = json.dumps(
        contexts[-1].get("recent_actions") or [],
        default=str,
    )
    assert "credential_type_requires_vault_action" in repair
    assert "use_credential" in repair
    assert proposed_secret not in json.dumps(
        {
            "events": events,
            "contexts": contexts,
            "summary": summary,
        },
        default=str,
    )


def test_multi_account_replay_requires_exact_run_local_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin = f"https://multi-account-{uuid.uuid4().hex}.example.test"
    browser_url = f"{origin}/login"
    secret = f"multi-{uuid.uuid4().hex}@mail.test"
    primary_id = 41001
    travel_id = 41002
    run_id, user_id = _create_run(target_url=browser_url)
    body = _CredentialBody(browser_url)
    events: list[tuple[str, dict[str, Any]]] = []
    resolve_calls: list[dict[str, Any]] = []
    marked_used: list[dict[str, Any]] = []
    _patch_loop_dependencies(monkeypatch, events=events)
    metadata = [
        {
            "id": primary_id,
            "origin": origin,
            "account_label": "Primary",
            "available_slots": ["email"],
            "auto_use": True,
        },
        {
            "id": travel_id,
            "origin": origin,
            "account_label": "Travel",
            "available_slots": ["email"],
            "auto_use": True,
        },
    ]
    monkeypatch.setattr(
        extension_run,
        "_credential_vault_metadata_for_url",
        lambda *_args, **_kwargs: [dict(item) for item in metadata],
    )

    def resolve(
        _user_id: int,
        **kwargs: Any,
    ) -> str:
        resolve_calls.append(dict(kwargs))
        return secret

    monkeypatch.setattr(
        extension_run,
        "_credential_vault_resolve_slot",
        resolve,
    )
    monkeypatch.setattr(
        extension_run,
        "_credential_vault_mark_used",
        lambda _user_id, **kwargs: marked_used.append(dict(kwargs)),
    )
    monkeypatch.setattr(
        extension_run,
        "_call_credential_target_classifier",
        _accepted_credential_classifier,
    )
    monkeypatch.setattr(
        extension_run,
        "_decide_vision_action",
        lambda *_args, **_kwargs: _use_credential_action(
            item_id=primary_id
        ),
    )
    # An unrelated answer with the same text must not authorize account replay.
    monkeypatch.setattr(
        extension_run,
        "_human_answers_for_run",
        lambda *_args, **_kwargs: [
            {"question": "Which theme?", "answer": "Primary"}
        ],
    )

    waiting = asyncio.run(
        run_vision_loop(
            body,
            url=None,
            narrate=_quiet_narrator,
            run_id=run_id,
            user_id=user_id,
            skip_navigate=True,
            max_steps=1,
        )
    )

    assert waiting["state"] == "WAITING_FOR_USER"
    assert waiting["reason"] == "needs_answer"
    assert waiting["ask"]["label"] == (
        extension_run._CREDENTIAL_ACCOUNT_CHOICE_QUESTION
    )
    assert waiting["ask"]["options"] == ["Primary", "Travel"]
    assert waiting["ask"]["persist_answer"] is False
    assert resolve_calls == []
    assert body.credential_calls == []
    assert any(
        event_type == "VISION_CREDENTIAL_ACCOUNT_CHOICE_REQUIRED"
        for event_type, _payload in events
    )

    monkeypatch.setattr(
        extension_run,
        "_human_answers_for_run",
        lambda *_args, **_kwargs: [
            {
                "question": extension_run._CREDENTIAL_ACCOUNT_CHOICE_QUESTION,
                "answer": "Primary",
            }
        ],
    )
    resumed = asyncio.run(
        run_vision_loop(
            body,
            url=None,
            narrate=_quiet_narrator,
            run_id=run_id,
            user_id=user_id,
            skip_navigate=True,
            max_steps=1,
        )
    )

    assert resolve_calls == [
        {"item_id": primary_id, "slot": "email", "url": browser_url}
    ]
    assert len(body.credential_calls) == 1
    assert marked_used == [{"item_id": primary_id, "url": browser_url}]
    assert resumed["receipts"][0]["outcome"] == "verified_succeeded"


def test_account_choice_persist_answer_false_uses_normal_question_task_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin = f"https://choice-publish-{uuid.uuid4().hex}.example.test"
    browser_url = f"{origin}/login"
    run_id, user_id = _create_run(target_url=browser_url)
    events: list[tuple[str, dict[str, Any]]] = []
    _patch_loop_dependencies(monkeypatch, events=events)

    with SessionLocal() as db:
        thread = models.ChatThread(
            user_id=user_id,
            run_id=run_id,
            title="Credential account choice",
            mode="run",
        )
        db.add(thread)
        db.commit()
        thread_id = int(thread.id)

    async def fake_loop(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "state": "WAITING_FOR_USER",
            "reason": "needs_answer",
            "ask": {
                "label": extension_run._CREDENTIAL_ACCOUNT_CHOICE_QUESTION,
                "options": ["Primary", "Travel"],
                "is_choice": True,
                "required": True,
                "is_unsure": True,
                "persist_answer": False,
                "credential_account_choice": True,
                "credential_origin": origin,
                "credential_slot": "email",
                "selector": "",
                "snapshot_id": None,
            },
            "actions": 0,
            "receipts": [],
        }

    monkeypatch.setattr(extension_run, "run_vision_loop", fake_loop)
    summary = asyncio.run(
        drive_in_browser_application(
            body=object(),
            run_id=run_id,
            thread_id=thread_id,
            url=None,
            skip_navigate=True,
        )
    )

    assert summary["state"] == "WAITING_FOR_USER"
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        questions = list(
            db.scalars(
                select(models.OperatorQuestion).where(
                    models.OperatorQuestion.run_id == run_id
                )
            ).all()
        )
        tasks = list(
            db.scalars(
                select(models.RuntimeTask).where(
                    models.RuntimeTask.run_id == run_id,
                    models.RuntimeTask.task_type == "field_approval",
                )
            ).all()
        )

    assert run.current_state == "WAITING_FOR_USER"
    assert len(questions) == 1
    assert len(tasks) == 1
    assert not (
        (run.confidence_state or {}).get("sensitive_browser_takeover")
    )
