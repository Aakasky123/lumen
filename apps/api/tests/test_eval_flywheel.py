"""Failure -> eval flywheel: blocked runs become reviewable candidates shaped like
capability-regression snapshots; corrections export as answer-quality case specs."""

import json
from types import SimpleNamespace

from app import models
from app.db import SessionLocal, init_db
from app.eval_flywheel import (
    CANDIDATE_SCHEMA_VERSION,
    capture_failure_candidate,
    export_correction_cases,
    list_candidates,
)
from app.event_store import emit_event
from app.runtime import OperatorRuntimeEngine


def _make_run(db) -> models.OperatorRun:
    user = OperatorRuntimeEngine(db).ensure_default_user()
    run = models.OperatorRun(
        user_id=user.id,
        goal="Apply to the example role",
        target_url="https://www.jobs.example.com/postings/42",
        current_state="BLOCKED",
    )
    db.add(run)
    db.flush()
    return run


def _use_tmp_artifact_root(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "app.eval_flywheel.get_settings",
        lambda: SimpleNamespace(artifact_root=tmp_path),
    )


def test_capture_failure_candidate_writes_regression_shaped_snapshot(monkeypatch, tmp_path) -> None:
    init_db()
    _use_tmp_artifact_root(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "app.eval_flywheel.run_failure_evidence",
        lambda db, run: {
            "vision_action_receipts": [
                {
                    "id": 9,
                    "event_type": "ACTION_RECEIPT",
                    "payload": {
                        "type": "click_verified",
                        "outcome": "verified_succeeded",
                        "verification": {"kind": "dom_readback"},
                    },
                }
            ],
            "latest_observation_path": None,
            "status_message": "Blocked after repeated non-progress.",
            "run_state": "BLOCKED",
            "vision_loop_event_counts": {"ACTION_RECEIPT": 1},
            "vision_timings": [{"step": 1, "decide_ms": 1200}],
        },
    )
    with SessionLocal() as db:
        run = _make_run(db)
        result = capture_failure_candidate(
            db,
            run.id,
            {
                "state": "BLOCKED",
                "reason": "no_progress",
                "block_kind": "awaiting_engineering",
                "model_failure_reason": "",
                "actions": 7,
                "timing_summary": {"steps": 7},
            },
        )
        db.commit()
        run_id = run.id

    assert result is not None
    path = tmp_path / "eval_candidates" / f"run_{run_id}.json"
    assert path.exists()
    candidate = json.loads(path.read_text(encoding="utf-8"))
    assert candidate["schema_version"] == CANDIDATE_SCHEMA_VERSION
    assert candidate["candidate_kind"] == "blocked_run_failure"
    assert candidate["provenance"]["run_id"] == run_id
    assert candidate["provenance"]["host"] == "jobs.example.com"
    snapshot = candidate["snapshot"]
    # The exact block CapabilityRegressionSpec.snapshot_failures validates.
    assert set(snapshot) == {
        "spec_id",
        "body_mode",
        "final_state",
        "verified_success",
        "verified_success_criteria",
        "forbidden_side_effects_observed",
        "receipts",
        "failure_artifacts",
    }
    assert snapshot["final_state"] == "BLOCKED"
    assert snapshot["verified_success"] is False
    assert snapshot["failure_artifacts"] == ["no_progress"]
    # Receipt envelopes are unwrapped to the payloads the spec matcher reads.
    assert snapshot["receipts"] == [
        {
            "type": "click_verified",
            "outcome": "verified_succeeded",
            "verification": {"kind": "dom_readback"},
        }
    ]
    assert candidate["privacy"]["sanitized"] is False

    with SessionLocal() as db:
        events = [
            event
            for event in db.query(models.CognitiveEvent)
            .filter(models.CognitiveEvent.run_id == run_id)
            .all()
            if event.event_type == "EVAL_CANDIDATE_RECORDED"
        ]
        assert len(events) == 1
        assert events[0].event_payload["reason"] == "no_progress"

    entries = list_candidates()
    ours = [entry for entry in entries if entry.get("run_id") == run_id]
    assert ours and ours[0]["final_state"] == "BLOCKED"


def test_capture_failure_candidate_survives_bare_run_with_real_export(monkeypatch, tmp_path) -> None:
    init_db()
    _use_tmp_artifact_root(monkeypatch, tmp_path)
    with SessionLocal() as db:
        run = _make_run(db)
        result = capture_failure_candidate(
            db,
            run.id,
            {"state": "BLOCKED", "reason": "vision_model_unavailable"},
        )
        db.commit()
        run_id = run.id
    assert result is not None
    candidate = json.loads(
        (tmp_path / "eval_candidates" / f"run_{run_id}.json").read_text(encoding="utf-8")
    )
    assert candidate["snapshot"]["receipts"] == []
    assert candidate["failure"]["reason"] == "vision_model_unavailable"


def test_capture_failure_candidate_returns_none_for_missing_run(monkeypatch, tmp_path) -> None:
    init_db()
    _use_tmp_artifact_root(monkeypatch, tmp_path)
    with SessionLocal() as db:
        assert capture_failure_candidate(db, 99_999_999, {"state": "BLOCKED"}) is None


def test_export_correction_cases_keeps_latest_correction_per_field(monkeypatch, tmp_path) -> None:
    init_db()
    _use_tmp_artifact_root(monkeypatch, tmp_path)
    field_key = "eval flywheel export test field"

    def _correction(value: str) -> dict:
        return {
            "field_key": field_key,
            "field_label": "Eval flywheel export test field",
            "field_type": "choice",
            "options": ["Yes", "No"],
            "agent_proposed": "Yes",
            "user_action": "change",
            "user_value": value,
            "host": "jobs.example.com",
            "run_id": 1,
            "ts": "2026-08-01T00:00:00+00:00",
        }

    with SessionLocal() as db:
        run = _make_run(db)
        for value in ("No", "No thanks"):
            emit_event(
                db,
                event_type="ANSWER_CORRECTION",
                run_id=run.id,
                actor="answer_flywheel",
                payload=_correction(value),
            )
        db.commit()
        result = export_correction_cases(db)

    assert result["case_count"] >= 1
    exported = json.loads(
        (tmp_path / "eval_candidates" / "correction_cases.json").read_text(encoding="utf-8")
    )
    ours = [case for case in exported["cases"] if case.get("id") == f"corr_{field_key}"]
    assert len(ours) == 1
    case = ours[0]
    # A changed answer means the agent's proposal was wrong: ask becomes acceptable and
    # the proposed value becomes unacceptable.
    assert "ask" in case["expected"]
    assert case["unacceptable"] == ["Yes"]
    # Latest correction wins the dedupe.
    assert case["acceptable"] == ["No thanks"]
