import json
from pathlib import Path

from app.db import init_db


init_db()


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "golden_runs" / "greenhouse_8164921002_run_272.json"


def _fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _event_types(section: dict) -> list[str]:
    return [event["event_type"] for event in section.get("events", [])]


def _all_events(fixture: dict) -> list[dict]:
    events_by_id: dict[int, dict] = {}
    for section_name in ("submit_approval", "otp", "completion"):
        for event in fixture[section_name].get("events", []):
            events_by_id[event["id"]] = event
    return [events_by_id[event_id] for event_id in sorted(events_by_id)]


def _assert_in_order(actual: list[str], expected: list[str]) -> None:
    cursor = 0
    for event_type in actual:
        if cursor < len(expected) and event_type == expected[cursor]:
            cursor += 1
    assert cursor == len(expected), f"missing ordered event sequence: {expected[cursor:]}"


def test_greenhouse_golden_approval_to_guarded_click_sequence() -> None:
    fixture = _fixture()
    questions = fixture["submit_approval"]["questions"]
    submit_questions = [question for question in questions if question["question_type"] == "submit_approval"]

    assert len(submit_questions) == 1
    assert submit_questions[0]["status"] == "applied"
    assert submit_questions[0]["answer_payload"]["resolved_by"] == "approve_run"
    assert submit_questions[0]["answer_payload"]["resolved_reason"] == "submit_approval_consumed"

    events = fixture["submit_approval"]["events"]
    _assert_in_order(
        _event_types(fixture["submit_approval"]),
        [
            "OPERATOR_QUESTION_ASKED",
            "SUBMIT_APPROVAL_RECEIVED",
            "SUBMIT_APPROVAL_SAVED",
            "SUBMISSION_RESUME_FROM_PENDING_ACTION",
            "SUBMIT_ATTEMPTED",
            "SUBMIT_BUTTON_CLICKED",
        ],
    )
    fingerprints = {
        event["payload"]["pending_submit_fingerprint"]
        for event in events
        if "pending_submit_fingerprint" in event["payload"]
    }
    assert len(fingerprints) == 1

    attempt = next(event for event in events if event["event_type"] == "SUBMIT_ATTEMPTED")
    clicked = next(event for event in events if event["event_type"] == "SUBMIT_BUTTON_CLICKED")
    assert attempt["payload"]["target"]["_allow_final_submit"] is True
    assert clicked["payload"]["target"]["_allow_final_submit"] is True

    final_submit_action = next(action for action in fixture["submit_approval"]["actions"] if action["step_number"] == 20)
    assert final_submit_action["primitive"] == "click_navigation_button"
    assert final_submit_action["status"] == "succeeded"
    assert final_submit_action["target"]["_allow_final_submit"] is True


def test_greenhouse_golden_ledger_end_state_snapshot() -> None:
    fixture = _fixture()
    summary = fixture["ready_for_review"]["review_summary"]
    diff = fixture["ready_for_review"]["review_diff"]

    assert summary["status"] == "READY_FOR_REVIEW"
    assert summary["field_count"] == 13
    assert summary["upload_count"] == 1
    assert summary["uploaded_documents"] == [{"label": "Resume/CV*", "status": "success"}]
    assert summary["unresolved_required_fields"] == []
    assert summary["final_submit_clicked"] is False
    assert len(summary["skipped_fields"]) == 3
    assert {field["reason"] for field in summary["skipped_fields"]} == {"skipped_optional_demographic_policy"}
    assert all("value_source" not in field for field in summary["skipped_fields"])
    all_labels = summary["filled_labels"] + [field["label"] for field in summary["skipped_fields"]]
    assert all(label and label != "this field" for label in all_labels)

    assert diff["extraction_empty"] is False
    assert diff["expected_field_count"] == 13
    assert diff["displayed_pair_count"] == 7
    assert diff["record_count"] == 20
    assert diff["summary"]["MATCHED_MISMATCH"] == 0
