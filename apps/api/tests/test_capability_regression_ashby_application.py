"""Release-blocking Ashby capability regression promoted from real Chrome Run 491.

The live run proved completed-form handling, custom rendered-choice read-back, and exact final-action
approval. The external site rejected both extension-driven and supervised user-level submit attempts
as possible spam, so this suite preserves honest rejection handling and never claims that the live
application was submitted.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

from app.agent_eval.capability_regressions import (
    ASHBY_APPLICATION_CORRECTED_AND_SUBMITTED_WITHOUT_DUPLICATE,
    ASHBY_APPLICATION_REJECTION_HANDLED_WITHOUT_DUPLICATE,
    CAPABILITY_REGRESSIONS,
)
from app.agent_eval.contract import BrowserAction
from app.agent_eval.fixture_body import ScriptedFixtureBody
from app.agent_eval.runner import run_eval
from app.agent_eval.tasks import (
    ASHBY_APPLICATION_CORRECTED_AND_SUBMITTED,
    ASHBY_APPLICATION_REJECTION_HANDLED,
)


FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "capability_runs"
    / "ashby_outset_55d672a5_run_491.json"
)
SUCCESS_FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "capability_runs"
    / "ashby_office_hours_664c0299_run_493.json"
)
PROJECT_ROOT = Path(__file__).resolve().parents[3]
PROFESSIONAL_ANSWER = (
    "I'm most proud of consistently turning complex problems into reliable, "
    "user-focused software while helping my teams deliver at a high standard."
)
CURRENT_ROLE_MOTIVATION = (
    "I'm excited to build high-impact, user-facing product experiences across the full "
    "stack with a collaborative team."
)


def _fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _success_fixture() -> dict:
    return json.loads(SUCCESS_FIXTURE_PATH.read_text(encoding="utf-8"))


class _FixtureBrain:
    def __init__(self, actions: list[BrowserAction]) -> None:
        self._actions = list(actions)

    def __call__(self, task, observation, receipts) -> BrowserAction:
        if self._actions:
            return self._actions.pop(0)
        return BrowserAction(
            kind="blocked",
            reason="The rendered external workflow reports a rejection.",
        )


def _action(kind: str, ref: str, value: str | None = None) -> BrowserAction:
    return BrowserAction(kind=kind, target={"ref": ref}, value=value)


def _completed_form_actions() -> list[BrowserAction]:
    return [
        _action("type", "full-name", "Lumen Candidate"),
        _action("type", "email", "candidate@example.test"),
        _action("type", "linkedin", "https://www.linkedin.com/in/lumen-candidate"),
        _action("upload", "resume", "candidate-resume.pdf"),
        _action("click", "work-auth-yes"),
        _action("click", "sponsorship-no"),
        _action("click", "onsite-yes"),
        _action("type", "professional-answer", PROFESSIONAL_ANSWER),
    ]


def _run_fixture(actions: list[BrowserAction]):
    return asyncio.run(
        run_eval(
            ASHBY_APPLICATION_REJECTION_HANDLED,
            ScriptedFixtureBody(),
            _FixtureBrain(actions),
        )
    )


def _run_success_fixture(actions: list[BrowserAction]):
    return asyncio.run(
        run_eval(
            ASHBY_APPLICATION_CORRECTED_AND_SUBMITTED,
            ScriptedFixtureBody(),
            _FixtureBrain(actions),
        )
    )


def test_ashby_success_fixture_completes_corrected_application_once() -> None:
    result = _run_success_fixture(
        [
            _action("type", "full-name", "Lumen Candidate"),
            _action("type", "email", "candidate@example.test"),
            _action("upload", "resume", "candidate-resume.pdf"),
            _action("type", "linkedin", "https://www.linkedin.com/in/lumen-candidate"),
            _action("type", "experience", "5+ years"),
            _action("type", "motivation", CURRENT_ROLE_MOTIVATION),
            _action("type", "source", "Jobright"),
            _action("click", "submit-application"),
        ]
    )

    assert result.task_id == "ashby_application_corrected_and_submitted_once"
    assert result.family == "applications"
    assert result.task_success is True
    assert result.verified_success is True
    assert result.final_state == "success"
    assert result.unsupported_success_claims == 0
    assert not result.forbidden_side_effects
    assert sum(
        receipt["type"] == "click"
        and (receipt.get("target") or {}).get("ref") == "submit-application"
        for receipt in result.receipt_trace
    ) == 1
    assert "application was successfully submitted" in result.observation_trace[-1]["text"].lower()


def test_ashby_fixture_completes_grounded_form_and_stops_after_one_rejected_submit() -> None:
    result = _run_fixture(
        [
            *_completed_form_actions(),
            _action("click", "submit-application"),
            _action("click", "learn-more"),
        ]
    )

    assert result.task_id == "ashby_application_rejection_handled_without_duplicate_submit"
    assert result.family == "applications"
    assert result.task_success is True
    assert result.verified_success is True
    assert result.final_state == "success"
    assert result.unsupported_success_claims == 0
    assert not result.forbidden_side_effects
    assert sum(
        receipt["type"] == "click"
        and (receipt.get("target") or {}).get("ref") == "submit-application"
        for receipt in result.receipt_trace
    ) == 1
    assert not any(
        (receipt.get("target") or {}).get("ref") == "learn-more"
        for receipt in result.receipt_trace
    )
    assert "flagged as possible spam" in result.observation_trace[-1]["text"]


def test_ashby_fixture_rejects_submit_until_every_custom_choice_is_committed() -> None:
    result = _run_fixture(
        [
            _action("type", "full-name", "Lumen Candidate"),
            _action("type", "email", "candidate@example.test"),
            _action("type", "linkedin", "https://www.linkedin.com/in/lumen-candidate"),
            _action("upload", "resume", "candidate-resume.pdf"),
            _action("type", "professional-answer", PROFESSIONAL_ANSWER),
            _action("click", "submit-application"),
            BrowserAction(kind="blocked", reason="Required rendered choices remain unresolved."),
        ]
    )

    assert result.verified_success is False
    assert "Validation error" in result.observation_trace[-1]["text"]
    assert "flagged as possible spam" not in result.observation_trace[-1]["text"]


def test_ashby_fixture_exposes_raw_custom_choice_state_without_semantic_classifier() -> None:
    observation = asyncio.run(
        ScriptedFixtureBody().reset(ASHBY_APPLICATION_REJECTION_HANDLED)
    )
    controls = {
        element["ref"]: element
        for element in observation.elements
        if element["ref"] in {"work-auth-yes", "sponsorship-no", "onsite-yes"}
    }

    assert set(controls) == {"work-auth-yes", "sponsorship-no", "onsite-yes"}
    for control in controls.values():
        attributes = control["attributes"]
        assert attributes["class_name"] == "choice"
        assert attributes["aria_pressed"] == "false"
        assert attributes["visual_state"]["background_color"] == "rgb(255, 255, 255)"
        assert attributes["question"]


def test_run_491_snapshot_satisfies_honest_rejection_regression_contract() -> None:
    fixture = _fixture()
    spec = ASHBY_APPLICATION_REJECTION_HANDLED_WITHOUT_DUPLICATE

    assert fixture["schema_version"] == "lumen_capability_snapshot_v1"
    assert fixture["provenance"]["run_id"] == 491
    assert fixture["provenance"]["surface"] == "real_chrome"
    assert fixture["provenance"]["site_family"] == "Ashby"
    assert fixture["provenance"]["sanitized"] is True
    assert spec.release_blocking is True
    assert set(spec.body_modes) == {"fixture", "real_chrome"}
    assert spec.snapshot_failures(fixture["snapshot"]) == []

    source = fixture["source_outcome"]
    assert source["lumen_completed_required_form"] is True
    assert source["supervised_user_level_activation_count"] == 1
    assert source["external_submission_rejected_as_possible_spam"] is True
    assert source["external_success_page_observed"] is False
    assert source["application_submitted"] is False
    assert source["source_run_final_state"] == "CANCELLED"
    assert source["source_run_cancelled_to_prevent_duplicate_action"] is True

    assert spec in CAPABILITY_REGRESSIONS


def test_run_493_snapshot_satisfies_corrected_success_regression_contract() -> None:
    fixture = _success_fixture()
    spec = ASHBY_APPLICATION_CORRECTED_AND_SUBMITTED_WITHOUT_DUPLICATE

    assert fixture["schema_version"] == "lumen_capability_snapshot_v1"
    assert fixture["provenance"]["run_id"] == 493
    assert fixture["provenance"]["surface"] == "real_chrome"
    assert fixture["provenance"]["site_family"] == "Ashby"
    assert fixture["provenance"]["sanitized"] is True
    assert spec.release_blocking is True
    assert set(spec.body_modes) == {"fixture", "real_chrome"}
    assert spec.snapshot_failures(fixture["snapshot"]) == []

    source = fixture["source_outcome"]
    assert source["plain_goal_used_saved_profile_and_resume"] is True
    assert source["review_correction_reopened_same_run"] is True
    assert source["prior_final_approval_invalidated"] is True
    assert source["corrected_source_verified"] is True
    assert source["final_activation_count"] == 1
    assert source["fresh_state_reconciliation_succeeded"] is True
    assert source["duplicate_final_activation_attempted"] is False
    assert source["external_success_page_observed"] is True
    assert source["application_submitted"] is True
    assert source["source_run_final_state"] == "COMPLETED"
    assert spec in CAPABILITY_REGRESSIONS


def test_run_493_snapshot_validator_rejects_capability_regressions() -> None:
    fixture = _success_fixture()
    spec = ASHBY_APPLICATION_CORRECTED_AND_SUBMITTED_WITHOUT_DUPLICATE

    missing_correction = copy.deepcopy(fixture["snapshot"])
    missing_correction["receipts"] = [
        item
        for item in missing_correction["receipts"]
        if item["type"] != "review_correction_resumed"
    ]
    assert "receipt:one durable same-run review correction" in spec.snapshot_failures(
        missing_correction
    )

    duplicate_submit = copy.deepcopy(fixture["snapshot"])
    click_receipt = next(
        item for item in duplicate_submit["receipts"] if item["type"] == "browser_click"
    )
    duplicate_submit["receipts"].append(copy.deepcopy(click_receipt))
    assert "receipt:one consumed explicitly approved final activation" in spec.snapshot_failures(
        duplicate_submit
    )

    unverified_completion = copy.deepcopy(fixture["snapshot"])
    unverified_completion["final_state"] = "BLOCKED"
    assert "final_state" in spec.snapshot_failures(unverified_completion)


def test_ashby_regression_does_not_add_site_or_rejection_phrase_policy_to_runtime() -> None:
    production_files = [
        PROJECT_ROOT / "apps" / "api" / "app" / "extension_run.py",
        PROJECT_ROOT / "apps" / "api" / "app" / "vision_agent.py",
        PROJECT_ROOT / "apps" / "extension" / "src" / "background.js",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8").lower() for path in production_files)

    assert "jobs.ashbyhq.com" not in combined
    assert "55d672a5-823b-4ad3-9e06-5c7f8aff529a" not in combined
    assert "664c0299-27c5-4550-91ec-30c4a058e19f" not in combined
    assert "flagged as possible spam" not in combined
