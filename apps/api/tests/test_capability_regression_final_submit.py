"""Release-blocking capability ratchet promoted from real Chrome Run 439.

The assertions protect outcomes, safety invariants, receipts, state, provider integration, and
failure evidence. They intentionally do not preserve the historical external-site click path.
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app import models
from app.agent_eval.capability_regressions import (
    ALREADY_SATISFIED_CHOICE_ADVANCES,
    APPROVED_FIELD_CHOICE_BOUND,
    APPROVED_FINAL_ACTION_CONFIRMED,
    APPROVED_FINAL_ACTION_RECONCILED,
    CAPABILITY_REGRESSIONS,
)
from app.lumen_model import LumenModelClient
from app.db import SessionLocal, init_db
from app.extension_run import drive_in_browser_application


@pytest.fixture(autouse=True)
def _complete_final_readiness_audit(monkeypatch):
    """Keep these legacy submit-outcome fixtures focused on the approved final action."""

    monkeypatch.setattr(
        "app.extension_run._audit_final_action_readiness",
        lambda *args, **kwargs: {
            "ok": True,
            "complete": True,
            "pending": [],
            "summary": "All preceding controls have verified current values.",
            "metadata": {
                "provider": "fixture",
                "model": "fixture-readiness",
                "purpose": "vision_final_action_readiness_audit",
            },
        },
    )


class _EmptyCandidateContext:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def query(self, *args, **kwargs):
        return []

    def compact_defaults_with_provenance(self, user_id: int, include_seeded_facts: bool = True) -> dict:
        return {"values": {"first_name": "Capability", "email": "capability@example.test"}}


class _ApprovedFinalActionBody:
    device_id = "capability-fixture-device"

    def __init__(self, *, submitted: bool = False) -> None:
        self.submitted = submitted
        self.control_clicks: list[tuple[str, int, int, bool]] = []

    async def capture_screenshot(self) -> dict:
        confirmation = "Confirmation. Your request was successfully submitted." if self.submitted else "Review your completed request."
        return {
            "ok": True,
            "image": base64.b64encode(b"jpeg-fixture").decode("ascii"),
            "width": 800,
            "height": 600,
            "url": "https://fixture.test/confirmation" if self.submitted else "https://fixture.test/review",
            "title": "Confirmation" if self.submitted else "Review",
            "scroll_y": 0,
            "scroll_height": 600,
            "page_evidence": {
                "viewport_text": confirmation,
                "controls": [] if self.submitted else [{"index": 1, "tag": "button", "text": "Submit request", "x": 300, "y": 400, "width": 180, "height": 44}],
            },
        }

    async def vision_click_control(
        self,
        selector: str,
        fallback_x: int,
        fallback_y: int,
        *,
        allow_final_submit: bool = False,
        expected_target_identity: dict | None = None,
        expected_page_context: dict | None = None,
    ) -> dict:
        assert expected_target_identity == {
            "target_selector": "#submit-request",
            "target_text": "Submit request",
            "target_tag": "button",
            "target_type": "submit",
            "target_role": "button",
            "dispatch_path": "dom_activation",
            "page_url": "https://fixture.test/review",
        }
        assert expected_page_context
        assert expected_page_context["url"] == "https://fixture.test/review"
        assert expected_page_context["state_fingerprint"]
        assert expected_page_context["screenshot_sha256"]
        self.control_clicks.append((selector, fallback_x, fallback_y, bool(allow_final_submit)))
        if not allow_final_submit:
            return {"ok": False, "action": "click", "blocked_submit": True}
        self.submitted = True
        return {
            "ok": True,
            "action": "click",
            "mode": "control_selector",
            "url_changed": True,
            "title_changed": True,
            "surface_changed": True,
            "trusted_click_attempted": True,
            "trusted_click_ok": True,
            "final_submit_revalidated": True,
        }


class _DelayedConfirmationBody(_ApprovedFinalActionBody):
    def __init__(self) -> None:
        super().__init__()
        self.post_submit_observations = 0

    async def capture_screenshot(self) -> dict:
        if not self.submitted:
            return await super().capture_screenshot()
        self.post_submit_observations += 1
        if self.post_submit_observations == 1:
            return {
                "ok": True,
                "image": base64.b64encode(b"transitional-jpeg-fixture").decode("ascii"),
                "width": 800,
                "height": 600,
                "url": "https://fixture.test/review",
                "title": "Review",
                "scroll_y": 0,
                "scroll_height": 600,
                "page_evidence": {
                    "viewport_text": "The completed form is disabled while the request is being processed.",
                    "controls": [],
                },
            }
        return await super().capture_screenshot()


def _create_approved_run(*, consumed_uncertain_attempt: bool = False) -> tuple[int, int, int]:
    init_db()
    with SessionLocal() as db:
        user = models.User(email=f"capability-submit-{uuid.uuid4().hex}@example.test", name="Capability User")
        db.add(user)
        db.flush()
        pending = {
            "status": "approved",
            "selector": "#submit-request",
            "label": "Submit request",
            "x": 390,
            "y": 422,
            "target_identity": {
                "target_selector": "#submit-request",
                "target_text": "Submit request",
                "target_tag": "button",
                "target_type": "submit",
                "target_role": "button",
                "dispatch_path": "dom_activation",
                "page_url": "https://fixture.test/review",
            },
            "page_context": {"url": "https://fixture.test/review"},
            "question_id": 9001,
            "approval_token": f"fixture-approval-{uuid.uuid4().hex}",
            "authorization": {
                "source": "human_chat",
                "question_id": 9001,
                "message_id": 9002,
            },
        }
        if consumed_uncertain_attempt:
            pending.update(
                {
                    "status": "unverified",
                    "authorization_consumed": True,
                    "fresh_approval_required": True,
                    "browser_activation_attempted": True,
                    "browser_activation_may_have_occurred": True,
                    "reconciliation_checked": False,
                    "execution_id": "consumed-attempt-fixture",
                    "previous_failure_reason": "final_submit_unverified",
                    "previous_failure_evidence": {"body_ok": True, "surface_changed": True},
                }
            )
        run = models.OperatorRun(
            user_id=user.id,
            goal=APPROVED_FINAL_ACTION_CONFIRMED.goal,
            target_url="https://fixture.test/review",
            current_state="WAITING_FOR_USER",
            confidence_state={"pending_final_action": pending},
        )
        db.add(run)
        db.flush()
        thread = models.ChatThread(user_id=user.id, run_id=run.id, title="Capability regression", mode="run")
        db.add(thread)
        db.commit()
        return int(run.id), int(thread.id), int(user.id)


def _configure_provider_result(monkeypatch, data: dict) -> None:
    monkeypatch.setattr("app.candidate_context.CandidateContextEngine", _EmptyCandidateContext)
    monkeypatch.setattr("app.extension_run._record_context_usage_for_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "app.model_provider.ModelProviderService.vision_provider",
        lambda self, user_id=None: SimpleNamespace(live_enabled=True),
    )
    monkeypatch.setattr(
        LumenModelClient,
        "_call_model_json",
        lambda self, **kwargs: {
            "ok": True,
            "data": dict(data),
            "metadata": {
                "provider": "compatible-test-provider",
                "model": "capability-verifier",
                "purpose": "final_action_outcome_verification",
                "parse_status": "json",
                "raw_output_empty": False,
            },
        },
    )


def test_promoted_capability_executes_once_normalizes_provider_and_completes(monkeypatch) -> None:
    _configure_provider_result(
        monkeypatch,
        {
            # This is the compatible-provider envelope observed in the real failure: semantic verdict
            # is correct, but the provider uses result and omits confidence.
            "result": "succeeded",
            "evidence": "The current page shows a confirmation heading and explicit successful-submission message.",
        },
    )
    body = _ApprovedFinalActionBody()
    run_id, thread_id, _ = _create_approved_run()

    summary = asyncio.run(
        drive_in_browser_application(body=body, run_id=run_id, thread_id=thread_id, url=None, skip_navigate=True)
    )

    assert summary["state"] == APPROVED_FINAL_ACTION_CONFIRMED.required_final_state
    assert summary["reason"] == "final_action_submitted"
    assert body.control_clicks == [("#submit-request", 390, 422, True)]
    assert APPROVED_FINAL_ACTION_CONFIRMED.missing_receipts(summary["receipts"]) == []
    final_receipt = APPROVED_FINAL_ACTION_CONFIRMED.required_receipts[0].matching(summary["receipts"])[0]
    assert final_receipt["verification"]["outcome_provider"]["normalization"]["status_field"] == "result"
    assert final_receipt["verification"]["outcome_confidence"] == 0.9
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        state = dict(run.confidence_state or {})
        assert run.current_state == "COMPLETED"
        assert state["pending_final_action"]["status"] == "consumed"
        assert "approved_final_action_pending" not in state


def test_promoted_capability_waits_for_delayed_confirmation_without_clicking_twice(monkeypatch) -> None:
    monkeypatch.setattr("app.candidate_context.CandidateContextEngine", _EmptyCandidateContext)
    monkeypatch.setattr("app.extension_run._record_context_usage_for_run", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.extension_run._FINAL_ACTION_VERIFICATION_REOBSERVE_DELAYS_SECONDS", (0.0, 0.0, 0.0))
    monkeypatch.setattr(
        "app.model_provider.ModelProviderService.vision_provider",
        lambda self, user_id=None: SimpleNamespace(live_enabled=True),
    )

    verification_modes: list[str] = []
    before_pages: list[dict] = []

    def provider_verdict(self, **kwargs):
        evidence = ((kwargs.get("prompt") or {}).get("evidence") or {})
        after_page = (evidence.get("after") or {}).get("page") or {}
        verification_modes.append(str(evidence.get("verification_mode") or ""))
        before_pages.append((evidence.get("before") or {}).get("page") or {})
        completion_visible = "successfully submitted" in str(after_page).casefold()
        return {
            "ok": True,
            "data": {
                "status": "succeeded" if completion_visible else "uncertain",
                "confidence": 0.99 if completion_visible else 0.2,
                "evidence": (
                    "The current page explicitly confirms successful submission."
                    if completion_visible
                    else "The browser is in a transitional disabled state without explicit completion evidence."
                ),
            },
            "metadata": {
                "provider": "compatible-test-provider",
                "model": "capability-verifier",
                "purpose": "final_action_outcome_verification",
                "parse_status": "json",
                "raw_output_empty": False,
            },
        }

    monkeypatch.setattr(LumenModelClient, "_call_model_json", provider_verdict)
    body = _DelayedConfirmationBody()
    run_id, thread_id, _ = _create_approved_run()

    summary = asyncio.run(
        drive_in_browser_application(body=body, run_id=run_id, thread_id=thread_id, url=None, skip_navigate=True)
    )

    assert summary["state"] == APPROVED_FINAL_ACTION_CONFIRMED.required_final_state
    assert summary["reason"] == "final_action_submitted"
    assert body.control_clicks == [("#submit-request", 390, 422, True)]
    assert body.post_submit_observations == 2
    final_receipt = APPROVED_FINAL_ACTION_CONFIRMED.required_receipts[0].matching(summary["receipts"])[0]
    attempts = final_receipt["verification"]["outcome_observation_attempts"]
    assert [item["status"] for item in attempts] == ["uncertain", "succeeded"]
    assert [item["verification_mode"] for item in attempts] == [
        "current_state_after_irreversible_attempt",
        "current_state_after_irreversible_attempt",
    ]
    assert verification_modes == [
        "current_state_after_irreversible_attempt",
        "current_state_after_irreversible_attempt",
    ]
    assert before_pages == [{}, {}]
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        assert run.current_state == "COMPLETED"
        event = db.scalars(
            select(models.CognitiveEvent)
            .where(models.CognitiveEvent.run_id == run_id, models.CognitiveEvent.event_type == "FINAL_ACTION_OUTCOME_VERDICT")
            .order_by(models.CognitiveEvent.id.desc())
        ).first()
        assert event is not None
        assert [item["status"] for item in event.event_payload["observation_attempts"]] == ["uncertain", "succeeded"]


def test_promoted_capability_reconciles_existing_success_without_second_click(monkeypatch) -> None:
    _configure_provider_result(
        monkeypatch,
        {
            "result": "succeeded",
            "evidence": "The current browser state explicitly confirms the prior final action completed.",
        },
    )
    body = _ApprovedFinalActionBody(submitted=True)
    run_id, thread_id, _ = _create_approved_run(consumed_uncertain_attempt=True)

    summary = asyncio.run(
        drive_in_browser_application(body=body, run_id=run_id, thread_id=thread_id, url=None, skip_navigate=True)
    )

    assert summary["state"] == APPROVED_FINAL_ACTION_RECONCILED.required_final_state
    assert summary["reconciled_without_retry_click"] is True
    assert body.control_clicks == []
    assert APPROVED_FINAL_ACTION_RECONCILED.missing_receipts(summary["receipts"]) == []
    verification_receipts = [item for item in summary["receipts"] if item.get("type") == "final_action_outcome_verification"]
    assert len(verification_receipts) == 1
    assert verification_receipts[0]["outcome"] == "verified_succeeded"
    assert verification_receipts[0]["verification"]["browser_click_attempted"] is False
    assert verification_receipts[0]["verification"]["authorization_consumed"] is True
    assert verification_receipts[0]["verification"]["mode"] == "current_state_after_consumed_attempt"


def test_promoted_capability_fails_closed_with_diagnostic_artifacts(monkeypatch) -> None:
    _configure_provider_result(
        monkeypatch,
        {
            "status": "uncertain",
            "confidence": 0.2,
            "evidence": "The page changed, but no explicit completion evidence can be confirmed.",
        },
    )
    body = _ApprovedFinalActionBody()
    run_id, thread_id, _ = _create_approved_run()

    summary = asyncio.run(
        drive_in_browser_application(body=body, run_id=run_id, thread_id=thread_id, url=None, skip_navigate=True)
    )

    assert summary["state"] == "BLOCKED"
    assert summary["reason"] != "final_action_submitted"
    assert len(body.control_clicks) == 1
    receipt = [item for item in summary["receipts"] if item.get("type") == "browser_click"][-1]
    paths = receipt["verification"]["verification_artifacts"]
    assert Path(paths["before_screenshot"]).is_file()
    assert Path(paths["after_screenshot"]).is_file()
    assert Path(paths["manifest"]).is_file()
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        assert run.current_state == "BLOCKED"
        pending = (run.confidence_state or {})["pending_final_action"]
        assert pending["status"] == "failed"
        assert pending["authorization_consumed"] is True
        assert pending["fresh_approval_required"] is True
        assert pending["reconciliation_checked"] is True
        event = db.scalars(
            select(models.CognitiveEvent)
            .where(models.CognitiveEvent.run_id == run_id, models.CognitiveEvent.event_type == "FINAL_ACTION_OUTCOME_VERDICT")
            .order_by(models.CognitiveEvent.id.desc())
        ).first()
        assert event is not None
        assert event.event_payload["verified"] is False
        assert event.event_payload["artifact_paths"]["manifest"].endswith("-manifest.json")
    reconciliation_receipts = [
        item
        for item in summary["receipts"]
        if item.get("type") == "final_action_outcome_verification"
    ]
    assert len(reconciliation_receipts) == 1
    assert reconciliation_receipts[0]["outcome"] == "verification_inconclusive"
    assert reconciliation_receipts[0]["verification"]["browser_click_attempted"] is False
    assert reconciliation_receipts[0]["verification"]["fresh_approval_required"] is True


def test_promoted_capability_spec_records_complete_ratchet_contract() -> None:
    assert {spec.id for spec in CAPABILITY_REGRESSIONS} == {
        "approved_final_action_confirmed_once",
        "approved_final_action_reconciled_without_duplicate_click",
        "approved_field_choice_bound_across_distinct_control",
        "already_satisfied_choice_advances_without_reselection_loop",
        "answered_field_resumes_owned_browser_session",
        "ashby_application_rejection_handled_without_duplicate_submit",
        "ashby_application_corrected_and_submitted_once",
        "greenhouse_application_grounded_and_autonomously_submitted",
        "hirebridge_application_unavailable_stops_without_return_loop",
        "smartrecruiters_application_grounded_and_submitted_after_explicit_approval",
    }
    for spec in CAPABILITY_REGRESSIONS:
        assert spec.release_blocking is True
        assert spec.source_proof["surface"] == "real_chrome"
        assert int(spec.source_proof["run_id"]) > 0
        assert spec.initial_browser_state and spec.minimum_context
        assert spec.success_criteria and spec.forbidden_side_effects and spec.required_receipts
        assert "fixture" in spec.body_modes
        assert set(spec.body_modes) <= {"fixture", "real_chrome"}
    assert APPROVED_FIELD_CHOICE_BOUND.required_final_state == "READY_FOR_REVIEW"
    assert ALREADY_SATISFIED_CHOICE_ADVANCES.required_final_state == "WAITING_FOR_USER"
    assert APPROVED_FINAL_ACTION_CONFIRMED.required_final_state == "COMPLETED"
    assert APPROVED_FINAL_ACTION_RECONCILED.required_final_state == "COMPLETED"
    spec = APPROVED_FINAL_ACTION_CONFIRMED
    assert {
        "before screenshot",
        "after screenshot",
        "bounded before/after page evidence",
        "final-action execution receipt",
        "outcome-verifier provider metadata and normalized verdict",
    } <= set(spec.failure_artifacts)
