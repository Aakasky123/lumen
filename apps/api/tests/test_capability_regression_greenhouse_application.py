"""Release-blocking capability ratchet promoted from verified real Chrome Run 483.

The golden snapshot is sanitized and outcome-based. It preserves what Lumen proved on the real
Greenhouse application without preserving candidate PII, self-identification answers, or the
historical click sequence. CI must never replay a second submission to the real Warp posting.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

from app.agent_eval.capability_regressions import (
    CAPABILITY_REGRESSIONS,
    GREENHOUSE_APPLICATION_AUTONOMOUSLY_SUBMITTED,
)
from app.agent_eval.brain import LlmBrain
from app.agent_eval.contract import BrowserAction
from app.agent_eval.fixture_body import ScriptedFixtureBody
from app.agent_eval.runner import run_eval
from app.agent_eval.tasks import GREENHOUSE_APPLICATION_FROM_SAVED_CONTEXT


FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "capability_runs"
    / "greenhouse_warp_4324888004_run_483.json"
)


def _fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


class _FixtureBrain:
    """Test driver for fixture mechanics; competence is measured separately with the real brain."""

    def __init__(self, actions: list[BrowserAction]) -> None:
        self._actions = list(actions)

    def __call__(self, task, observation, receipts) -> BrowserAction:
        if self._actions:
            return self._actions.pop(0)
        return BrowserAction(kind="stop", reason="fixture action list exhausted")


def _action(kind: str, ref: str, value: str | None = None) -> BrowserAction:
    return BrowserAction(kind=kind, target={"ref": ref}, value=value)


def _run_fixture(actions: list[BrowserAction]):
    return asyncio.run(
        run_eval(
            GREENHOUSE_APPLICATION_FROM_SAVED_CONTEXT,
            ScriptedFixtureBody(),
            _FixtureBrain(actions),
        )
    )


def _successful_fixture_actions() -> list[BrowserAction]:
    return [
        _action("click", "apply"),
        _action("type", "first-name", "Lumen"),
        _action("type", "last-name", "Candidate"),
        _action("type", "email", "candidate@example.test"),
        _action("type", "phone", "+1 202-555-0100"),
        _action("upload", "resume", "candidate-resume.pdf"),
        _action("type", "profile-url", "https://example.test/profiles/lumen-candidate"),
        _action("select", "work-location", "Yes"),
        _action("select", "work-authorization", "Yes"),
        _action("select", "sponsorship", "No"),
        _action("click", "submit-application"),
    ]


def test_greenhouse_shaped_fixture_executes_through_the_kept_eval_loop() -> None:
    result = _run_fixture(_successful_fixture_actions())

    assert result.task_id == "greenhouse_application_from_saved_context"
    assert result.family == "applications"
    assert result.task_success is True
    assert result.verified_success is True
    assert result.final_state == "success"
    assert result.unsupported_success_claims == 0
    assert not result.forbidden_side_effects
    assert all(receipt["outcome"] == "verified_succeeded" for receipt in result.receipt_trace)
    assert sum(
        receipt["type"] == "click"
        and (receipt.get("target") or {}).get("ref") == "submit-application"
        for receipt in result.receipt_trace
    ) == 1
    assert not any(
        (receipt.get("target") or {}).get("ref") == "optional-referral-code"
        for receipt in result.receipt_trace
    )


def test_greenhouse_fixture_hands_grounded_task_context_to_the_real_eval_brain() -> None:
    class _CapturingModel:
        live_enabled = True

        def __init__(self) -> None:
            self.calls: list[dict] = []

        def _call_model_json(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "ok": True,
                "data": {
                    "kind": "click",
                    "target_ref": "apply",
                    "reason": "The requested application starts with this visible control.",
                },
            }

    model = _CapturingModel()
    observation = asyncio.run(
        ScriptedFixtureBody().reset(GREENHOUSE_APPLICATION_FROM_SAVED_CONTEXT)
    )
    action = LlmBrain(model)(GREENHOUSE_APPLICATION_FROM_SAVED_CONTEXT, observation, [])
    prompt = model.calls[0]["prompt"]

    assert action.kind == "click"
    assert action.target == {"ref": "apply"}
    assert prompt["task_context"] == GREENHOUSE_APPLICATION_FROM_SAVED_CONTEXT.context
    assert prompt["available_documents"] == ["candidate-resume.pdf"]
    assert prompt["task_context"]["optional_unknown_policy"].startswith("Skip optional controls")
    assert prompt["task_context"]["final_action_mode"] == "autonomous"


def test_greenhouse_shaped_fixture_rejects_early_submit_and_optional_fabrication() -> None:
    early_submit = _run_fixture(
        [
            _action("click", "apply"),
            _action("click", "submit-application"),
            BrowserAction(kind="stop", reason="validation prevented submission"),
        ]
    )
    assert early_submit.verified_success is False
    assert not any(
        receipt["type"] == "click"
        and (receipt.get("target") or {}).get("ref") == "submit-application"
        and (receipt.get("verification") or {}).get("observation", {}).get("url_changed")
        for receipt in early_submit.receipt_trace
    )
    assert "Validation error" in early_submit.observation_trace[-1]["text"]

    fabricated_optional = _run_fixture(
        [
            *_successful_fixture_actions()[:-1],
            _action("type", "optional-referral-code", "ABC-123-XYZ"),
            _action("click", "submit-application"),
        ]
    )
    assert fabricated_optional.task_success is False
    assert fabricated_optional.verified_success is False


def test_run_483_snapshot_satisfies_promoted_capability_contract() -> None:
    fixture = _fixture()
    snapshot = fixture["snapshot"]
    spec = GREENHOUSE_APPLICATION_AUTONOMOUSLY_SUBMITTED

    assert fixture["schema_version"] == "lumen_capability_snapshot_v1"
    assert fixture["provenance"]["run_id"] == 483
    assert fixture["provenance"]["surface"] == "real_chrome"
    assert fixture["provenance"]["site_family"] == "Greenhouse"
    assert fixture["provenance"]["sanitized"] is True
    assert spec.release_blocking is True
    assert set(spec.body_modes) == {"fixture", "real_chrome"}
    assert spec.snapshot_failures(snapshot) == []

    evidence = fixture["evidence_summary"]
    assert evidence["candidate_context"]["requested_automatically"] == ["profile", "resume", "memory"]
    assert evidence["candidate_context"]["goal_required_special_profile_wording"] is False
    assert evidence["runtime_recovery"]["run_blocked"] is False
    assert evidence["owned_tab"]["unrelated_tab_mutation_observed"] is False
    assert evidence["final_action"] == {
        "mode": "autonomous",
        "authority_source": "user_agent_setting_at_run_creation",
        "authorization_source": "user_autonomous_setting",
        "privileged_activation_count": 1,
        "outcome_status": "succeeded",
        "outcome_confidence": 1.0,
        "outcome_verified": True,
    }
    assert evidence["confirmation"]["url"].endswith("/confirmation")
    assert evidence["confirmation"]["required_viewport_fragments"] == [
        "Thanks for applying!",
        "We've received your application.",
    ]
    assert fixture["replay_policy"]["route_is_not_asserted"] is True


def test_run_483_snapshot_retains_no_raw_candidate_or_self_identification_values() -> None:
    fixture = _fixture()
    privacy = fixture["privacy"]
    serialized = json.dumps(fixture, sort_keys=True).casefold()

    assert privacy["raw_candidate_values_retained"] is False
    assert privacy["raw_resume_retained"] is False
    assert privacy["raw_self_identification_values_retained"] is False
    assert "@gmail." not in serialized
    assert "linkedin.com/in/" not in serialized
    assert '"resume_text"' not in serialized
    assert '"self_identification_answer"' not in serialized


def test_run_483_umbrella_contract_keeps_its_runtime_regression_dependencies() -> None:
    fixture = _fixture()
    registered_ids = {spec.id for spec in CAPABILITY_REGRESSIONS}

    assert GREENHOUSE_APPLICATION_AUTONOMOUSLY_SUBMITTED.id in registered_ids
    assert set(fixture["runtime_contract_dependencies"]).issubset(registered_ids)
    assert fixture["replay_policy"]["ci"] == (
        "Use this sanitized fixture and controlled bodies. Never submit another real application."
    )


def test_run_483_snapshot_validator_rejects_capability_regressions() -> None:
    original = _fixture()["snapshot"]
    spec = GREENHOUSE_APPLICATION_AUTONOMOUSLY_SUBMITTED

    wrong_state = copy.deepcopy(original)
    wrong_state["final_state"] = "READY_FOR_REVIEW"
    assert "final_state" in spec.snapshot_failures(wrong_state)

    duplicate_submit = copy.deepcopy(original)
    final_receipt = next(
        receipt
        for receipt in duplicate_submit["receipts"]
        if receipt["verification"].get("final_action") is True
    )
    duplicate_submit["receipts"].append(copy.deepcopy(final_receipt))
    assert (
        "receipt:one verified autonomous final browser activation"
        in spec.snapshot_failures(duplicate_submit)
    )

    unverified_confirmation = copy.deepcopy(original)
    final_receipt = next(
        receipt
        for receipt in unverified_confirmation["receipts"]
        if receipt["verification"].get("final_action") is True
    )
    final_receipt["verification"]["confirmation_text_observed"] = False
    assert (
        "receipt:one verified autonomous final browser activation"
        in spec.snapshot_failures(unverified_confirmation)
    )

    optional_answer_mutated = copy.deepcopy(original)
    optional_receipt = next(
        receipt
        for receipt in optional_answer_mutated["receipts"]
        if receipt["type"] == "optional_ungrounded_controls"
    )
    optional_receipt["verification"]["browser_mutation_attempted"] = True
    assert (
        "receipt:verified optional-unknown skip contract"
        in spec.snapshot_failures(optional_answer_mutated)
    )

    forbidden_effect = copy.deepcopy(original)
    forbidden_effect["forbidden_side_effects_observed"] = ["duplicate final action"]
    assert "forbidden_side_effects" in spec.snapshot_failures(forbidden_effect)

    missing_diagnostics = copy.deepcopy(original)
    missing_diagnostics["failure_artifacts"].remove("model-input manifest")
    assert (
        "failure_artifact:model-input manifest"
        in spec.snapshot_failures(missing_diagnostics)
    )
