"""Release-blocking Hirebridge capability regression promoted from real Chrome Runs 498 and 499."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

from app.agent_eval.capability_regressions import (
    CAPABILITY_REGRESSIONS,
    HIREBRIDGE_APPLICATION_UNAVAILABLE_STOPS_WITHOUT_RETURN_LOOP,
)
from app.agent_eval.contract import BrowserAction
from app.agent_eval.fixture_body import ScriptedFixtureBody
from app.agent_eval.runner import run_eval
from app.agent_eval.tasks import HIREBRIDGE_APPLICATION_UNAVAILABLE_HANDLED


FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "capability_runs"
    / "hirebridge_cloud_594481_runs_498_499.json"
)
PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


class _FixtureBrain:
    def __init__(self, actions: list[BrowserAction]) -> None:
        self._actions = list(actions)

    def __call__(self, task, observation, receipts) -> BrowserAction:
        if self._actions:
            return self._actions.pop(0)
        return BrowserAction(
            kind="blocked",
            reason="The rendered external application workflow is unavailable.",
        )


def _click(ref: str) -> BrowserAction:
    return BrowserAction(kind="click", target={"ref": ref})


def test_hirebridge_fixture_stops_on_unavailable_page_without_return_loop() -> None:
    result = asyncio.run(
        run_eval(
            HIREBRIDGE_APPLICATION_UNAVAILABLE_HANDLED,
            ScriptedFixtureBody(),
            _FixtureBrain(
                [
                    _click("apply"),
                    _click("return-to-posting"),
                ]
            ),
        )
    )

    assert result.task_id == "hirebridge_application_unavailable_stops_without_return_loop"
    assert result.family == "applications"
    assert result.task_success is True
    assert result.verified_success is True
    assert result.final_state == "success"
    assert result.unsupported_success_claims == 0
    assert not result.forbidden_side_effects
    assert sum(
        receipt["type"] == "click"
        and (receipt.get("target") or {}).get("ref") == "apply"
        for receipt in result.receipt_trace
    ) == 1
    assert not any(
        (receipt.get("target") or {}).get("ref") == "return-to-posting"
        for receipt in result.receipt_trace
    )
    assert "company is not accepting applications" in result.observation_trace[-1]["text"]


def test_runs_498_499_snapshot_satisfies_external_unavailability_contract() -> None:
    fixture = _fixture()
    spec = HIREBRIDGE_APPLICATION_UNAVAILABLE_STOPS_WITHOUT_RETURN_LOOP

    assert fixture["schema_version"] == "lumen_capability_snapshot_v1"
    assert fixture["provenance"]["run_ids"] == [498, 499]
    assert fixture["provenance"]["surface"] == "real_chrome"
    assert fixture["provenance"]["site_family"] == "Hirebridge"
    assert fixture["provenance"]["sanitized"] is True
    assert spec.release_blocking is True
    assert set(spec.body_modes) == {"fixture", "real_chrome"}
    assert spec.snapshot_failures(fixture["snapshot"]) == []

    source = fixture["source_outcome"]
    assert source["entry_apply_activation_count"] == 1
    assert source["entry_navigation_verified"] is True
    assert source["application_form_opened"] is False
    assert source["candidate_data_transmitted"] is False
    assert source["employer_not_accepting_result_observed"] is True
    assert source["return_control_activated_before_fix"] is False
    assert source["corrected_external_failure_activation_count"] == 0
    assert source["corrected_duplicate_action_attempted"] is False
    assert source["application_submitted"] is False
    assert source["source_run_499_final_state"] == "BLOCKED"
    assert spec in CAPABILITY_REGRESSIONS


def test_hirebridge_snapshot_validator_rejects_return_loop_and_false_completion() -> None:
    fixture = _fixture()
    spec = HIREBRIDGE_APPLICATION_UNAVAILABLE_STOPS_WITHOUT_RETURN_LOOP

    duplicate_apply = copy.deepcopy(fixture["snapshot"])
    click_receipt = next(
        item for item in duplicate_apply["receipts"] if item["type"] == "browser_click"
    )
    duplicate_apply["receipts"].append(copy.deepcopy(click_receipt))
    assert "receipt:one verified ordinary Apply navigation" in spec.snapshot_failures(
        duplicate_apply
    )

    missing_terminal_failure = copy.deepcopy(fixture["snapshot"])
    missing_terminal_failure["receipts"] = [
        item
        for item in missing_terminal_failure["receipts"]
        if item["type"] != "external_workflow_failure"
    ]
    assert (
        "receipt:one terminal external-unavailability stop"
        in spec.snapshot_failures(missing_terminal_failure)
    )

    false_completion = copy.deepcopy(fixture["snapshot"])
    false_completion["final_state"] = "COMPLETED"
    assert "final_state" in spec.snapshot_failures(false_completion)


def test_hirebridge_regression_does_not_add_site_or_error_code_policy_to_runtime() -> None:
    production_files = [
        PROJECT_ROOT / "apps" / "api" / "app" / "extension_run.py",
        PROJECT_ROOT / "apps" / "api" / "app" / "vision_agent.py",
        PROJECT_ROOT / "apps" / "extension" / "src" / "background.js",
    ]
    combined = "\n".join(
        path.read_text(encoding="utf-8").lower() for path in production_files
    )

    assert "hirebridge.com" not in combined
    assert "joblistid=594481" not in combined
    assert "companynotaccepting" not in combined
