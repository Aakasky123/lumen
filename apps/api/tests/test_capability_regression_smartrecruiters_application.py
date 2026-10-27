"""Release-blocking SmartRecruiters failure regression promoted from real Chrome Run 486.

The real source run exposed two generic failures: a required privacy checkbox whose composed
open-shadow label collapsed to ``*``, and a browser session stranded when Resume removed a pause
overlay from an underlying engineering block. The external application was submitted only through
supervised recovery after explicit user confirmation, so this suite never claims that Run 486
produced an Lumen final-submit receipt and never replays the real application.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

from app.agent_eval.brain import LlmBrain
from app.agent_eval.capability_regressions import (
    CAPABILITY_REGRESSIONS,
    SMARTRECRUITERS_APPLICATION_SUBMITTED_AFTER_EXPLICIT_APPROVAL,
)
from app.agent_eval.contract import BrowserAction
from app.agent_eval.fixture_body import ScriptedFixtureBody
from app.agent_eval.runner import run_eval
from app.agent_eval.tasks import (
    SMARTRECRUITERS_APPLICATION_FROM_SAVED_CONTEXT,
    SMARTRECRUITERS_PRIVACY_NOTICE,
)


FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "capability_runs"
    / "smartrecruiters_iptglobal_3884d420_run_486.json"
)
PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


class _FixtureBrain:
    """Drive fixture mechanics; live-model competence remains a separate real-brain eval."""

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
            SMARTRECRUITERS_APPLICATION_FROM_SAVED_CONTEXT,
            ScriptedFixtureBody(),
            _FixtureBrain(actions),
        )
    )


def _completed_form_actions() -> list[BrowserAction]:
    return [
        _action("type", "first-name", "Lumen"),
        _action("type", "last-name", "Candidate"),
        _action("type", "email", "candidate@example.test"),
        _action("type", "confirm-email", "candidate@example.test"),
        _action("type", "location", "Example City, NJ, United States"),
        _action("type", "phone", "+1 202-555-0100"),
        _action("upload", "resume", "candidate-resume.pdf"),
    ]


def _successful_fixture_actions() -> list[BrowserAction]:
    return [
        *_completed_form_actions(),
        _action("click", "privacy-ack"),
        _action("click", "submit-application"),
    ]


def test_smartrecruiters_fixture_executes_outcome_contract_without_route_replay() -> None:
    result = _run_fixture(_successful_fixture_actions())

    assert result.task_id == "smartrecruiters_application_from_saved_context"
    assert result.family == "applications"
    assert result.task_success is True
    assert result.verified_success is True
    assert result.final_state == "success"
    assert result.unsupported_success_claims == 0
    assert not result.forbidden_side_effects
    assert all(receipt["outcome"] == "verified_succeeded" for receipt in result.receipt_trace)
    assert sum(
        receipt["type"] == "click"
        and (receipt.get("target") or {}).get("ref") == "privacy-ack"
        for receipt in result.receipt_trace
    ) == 1
    assert sum(
        receipt["type"] == "click"
        and (receipt.get("target") or {}).get("ref") == "submit-application"
        for receipt in result.receipt_trace
    ) == 1
    assert not any(
        (receipt.get("target") or {}).get("ref") == "optional-hiring-message"
        for receipt in result.receipt_trace
    )


def test_smartrecruiters_fixture_exposes_complete_composed_privacy_identity() -> None:
    observation = asyncio.run(
        ScriptedFixtureBody().reset(SMARTRECRUITERS_APPLICATION_FROM_SAVED_CONTEXT)
    )
    privacy = next(
        element
        for element in observation.elements
        if element["ref"] == "privacy-ack"
    )

    assert privacy["label"] == SMARTRECRUITERS_PRIVACY_NOTICE
    assert privacy["label"].strip() != "*"
    assert privacy["attributes"]["composed_accessibility_text"] is True
    assert privacy["attributes"]["open_shadow_dom"] is True
    assert privacy["accessibility_context"] == [
        {
            "relation": "aria-labelledby",
            "text": SMARTRECRUITERS_PRIVACY_NOTICE,
        }
    ]


def test_smartrecruiters_fixture_hands_saved_context_and_approval_to_real_eval_brain() -> None:
    class _CapturingModel:
        live_enabled = True

        def __init__(self) -> None:
            self.calls: list[dict] = []

        def _call_model_json(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "ok": True,
                "data": {
                    "kind": "type",
                    "target_ref": "first-name",
                    "value": "Lumen",
                    "reason": "Use the grounded synthetic first name.",
                },
            }

    model = _CapturingModel()
    observation = asyncio.run(
        ScriptedFixtureBody().reset(SMARTRECRUITERS_APPLICATION_FROM_SAVED_CONTEXT)
    )
    action = LlmBrain(model)(
        SMARTRECRUITERS_APPLICATION_FROM_SAVED_CONTEXT,
        observation,
        [],
    )
    prompt = model.calls[0]["prompt"]

    assert action.kind == "type"
    assert action.target == {"ref": "first-name"}
    assert prompt["task_context"] == SMARTRECRUITERS_APPLICATION_FROM_SAVED_CONTEXT.context
    assert prompt["available_documents"] == ["candidate-resume.pdf"]
    assert prompt["task_context"]["optional_unknown_policy"].startswith("Leave optional")
    assert prompt["task_context"]["final_action_mode"] == "safe_approval"
    assert "explicitly approved" in prompt["task_context"]["final_action_authority"]


def test_smartrecruiters_fixture_rejects_submit_without_privacy_or_required_data() -> None:
    early_submit = _run_fixture(
        [
            _action("click", "submit-application"),
            BrowserAction(kind="stop", reason="validation prevented submission"),
        ]
    )
    assert early_submit.verified_success is False
    assert "Validation error" in early_submit.observation_trace[-1]["text"]

    missing_privacy = _run_fixture(
        [
            *_completed_form_actions(),
            _action("click", "submit-application"),
            BrowserAction(kind="stop", reason="privacy acknowledgement is required"),
        ]
    )
    assert missing_privacy.verified_success is False
    assert "Validation error" in missing_privacy.observation_trace[-1]["text"]
    assert not any(
        receipt["type"] == "click"
        and (receipt.get("target") or {}).get("ref") == "submit-application"
        and (receipt.get("verification") or {})
        .get("observation", {})
        .get("url_changed")
        for receipt in missing_privacy.receipt_trace
    )


def test_smartrecruiters_fixture_rejects_optional_instruction_or_placeholder_mutation() -> None:
    mutated_optional = _run_fixture(
        [
            *_completed_form_actions(),
            _action(
                "type",
                "optional-hiring-message",
                "Generate a custom message from my resume and upload it.",
            ),
            _action("click", "privacy-ack"),
            _action("click", "submit-application"),
        ]
    )

    assert mutated_optional.task_success is False
    assert mutated_optional.verified_success is False


def test_run_486_controlled_snapshot_satisfies_promoted_regression_contract() -> None:
    fixture = _fixture()
    snapshot = fixture["snapshot"]
    spec = SMARTRECRUITERS_APPLICATION_SUBMITTED_AFTER_EXPLICIT_APPROVAL

    assert fixture["schema_version"] == "lumen_capability_snapshot_v1"
    assert fixture["provenance"]["run_id"] == 486
    assert fixture["provenance"]["surface"] == "real_chrome"
    assert fixture["provenance"]["site_family"] == "SmartRecruiters"
    assert fixture["provenance"]["sanitized"] is True
    assert fixture["provenance"]["snapshot_represents"] == (
        "controlled regression target derived from a supervised recovery"
    )
    assert spec.release_blocking is True
    assert set(spec.body_modes) == {"fixture", "real_chrome"}
    assert spec.snapshot_failures(snapshot) == []

    source_outcome = fixture["source_outcome"]
    assert source_outcome["lumen_runtime_completed_submission"] is False
    assert source_outcome["lumen_final_submit_receipt_observed"] is False
    assert source_outcome[
        "supervised_final_activation_after_explicit_user_confirmation"
    ] is True
    assert source_outcome["external_success_page_observed"] is True

    evidence = fixture["evidence_summary"]
    assert evidence["observed_failure"]["privacy_control_observed_label"] == "*"
    assert evidence["observed_failure"]["pause_overlay_stranded_blocked_session"] is True
    assert evidence["fixed_contract"]["required_marker_only_label_forbidden"] is True
    assert evidence["fixed_contract"]["paused_from_blocked_uses_guarded_recovery"] is True
    assert evidence["source_final_action"]["lumen_runtime_receipt_observed"] is False
    assert evidence["source_final_action"]["supervised_activation_count"] == 1
    assert evidence["confirmation"]["url"].split("?", 1)[0].endswith("/success")
    assert evidence["confirmation"]["required_viewport_fragments"] == [
        "Application submitted!",
        "has been submitted successfully.",
    ]
    assert fixture["replay_policy"]["route_is_not_asserted"] is True


def test_run_486_fixture_retains_no_candidate_values_or_bad_optional_message() -> None:
    fixture = _fixture()
    privacy = fixture["privacy"]
    serialized = json.dumps(fixture, sort_keys=True).casefold()

    assert privacy["raw_candidate_values_retained"] is False
    assert privacy["raw_resume_retained"] is False
    assert privacy["raw_optional_message_retained"] is False
    assert "@gmail." not in serialized
    assert "linkedin.com/in/" not in serialized
    assert '"resume_text"' not in serialized
    assert "jordan" not in serialized
    assert "park" not in serialized
    assert "can you generate a custom one" not in serialized


def test_run_486_regression_keeps_runtime_and_component_dependencies() -> None:
    fixture = _fixture()
    registered_ids = {spec.id for spec in CAPABILITY_REGRESSIONS}

    assert (
        SMARTRECRUITERS_APPLICATION_SUBMITTED_AFTER_EXPLICIT_APPROVAL.id
        in registered_ids
    )
    assert set(fixture["runtime_contract_dependencies"]).issubset(registered_ids)
    assert fixture["component_regressions"] == {
        "backend": "test_resume_paused_blocked_browser_session_reuses_guarded_recovery",
        "extension": (
            "vision evidence reads slotted accessibility labels across open shadow components"
        ),
    }

    backend_regression = (
        PROJECT_ROOT / "apps" / "api" / "tests" / "test_operator_runtime.py"
    ).read_text(encoding="utf-8")
    extension_regression = (
        PROJECT_ROOT
        / "apps"
        / "extension"
        / "test"
        / "background_vision_click.test.js"
    ).read_text(encoding="utf-8")
    assert fixture["component_regressions"]["backend"] in backend_regression
    assert fixture["component_regressions"]["extension"] in extension_regression
    assert fixture["replay_policy"]["ci"] == (
        "Use this sanitized fixture and controlled bodies. Never submit another real application."
    )


def test_run_486_snapshot_validator_rejects_observed_failure_regressions() -> None:
    original = _fixture()["snapshot"]
    spec = SMARTRECRUITERS_APPLICATION_SUBMITTED_AFTER_EXPLICIT_APPROVAL

    wrong_state = copy.deepcopy(original)
    wrong_state["final_state"] = "READY_FOR_REVIEW"
    assert "final_state" in spec.snapshot_failures(wrong_state)

    marker_only_label = copy.deepcopy(original)
    composed = next(
        receipt
        for receipt in marker_only_label["receipts"]
        if receipt["type"] == "composed_control_label"
    )
    composed["verification"]["label_not_required_marker_only"] = False
    assert (
        "receipt:one composed privacy-label observation"
        in spec.snapshot_failures(marker_only_label)
    )

    stranded_pause = copy.deepcopy(original)
    resumed = next(
        receipt
        for receipt in stranded_pause["receipts"]
        if receipt["type"] == "paused_blocked_resume"
    )
    resumed["verification"]["scheduled_same_owned_run"] = False
    assert (
        "receipt:one guarded paused-blocked resume"
        in spec.snapshot_failures(stranded_pause)
    )

    mutated_optional = copy.deepcopy(original)
    optional = next(
        receipt
        for receipt in mutated_optional["receipts"]
        if receipt["type"] == "optional_ungrounded_controls"
    )
    optional["verification"]["browser_mutation_attempted"] = True
    assert (
        "receipt:verified optional hiring-message skip"
        in spec.snapshot_failures(mutated_optional)
    )

    unchecked_privacy = copy.deepcopy(original)
    privacy = next(
        receipt
        for receipt in unchecked_privacy["receipts"]
        if receipt["type"] == "privacy_acknowledgement"
    )
    privacy["verification"]["checked_readback"] = False
    assert (
        "receipt:one verified privacy acknowledgement"
        in spec.snapshot_failures(unchecked_privacy)
    )

    duplicate_submit = copy.deepcopy(original)
    final_receipt = next(
        receipt
        for receipt in duplicate_submit["receipts"]
        if receipt["verification"].get("final_action") is True
    )
    duplicate_submit["receipts"].append(copy.deepcopy(final_receipt))
    assert (
        "receipt:one explicitly approved final browser activation"
        in spec.snapshot_failures(duplicate_submit)
    )

    missing_confirmation = copy.deepcopy(original)
    final_receipt = next(
        receipt
        for receipt in missing_confirmation["receipts"]
        if receipt["verification"].get("final_action") is True
    )
    final_receipt["verification"]["confirmation_text_observed"] = False
    assert (
        "receipt:one explicitly approved final browser activation"
        in spec.snapshot_failures(missing_confirmation)
    )

    forbidden_effect = copy.deepcopy(original)
    forbidden_effect["forbidden_side_effects_observed"] = [
        "optional hiring message mutated"
    ]
    assert "forbidden_side_effects" in spec.snapshot_failures(forbidden_effect)

    missing_diagnostics = copy.deepcopy(original)
    missing_diagnostics["failure_artifacts"].remove(
        "raw composed accessibility relationship evidence"
    )
    assert (
        "failure_artifact:raw composed accessibility relationship evidence"
        in spec.snapshot_failures(missing_diagnostics)
    )
