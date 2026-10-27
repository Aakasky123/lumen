"""Eval scorecard: pytest-summary parsing, tier orchestration, and score history."""

import json
from types import SimpleNamespace

from app.agent_eval.scorecard import (
    load_history,
    parse_pytest_summary,
    record_scorecard,
    regression_targets,
    run_answer_quality_tier,
)


def _use_tmp_artifact_root(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        "app.agent_eval.scorecard.get_settings",
        lambda: SimpleNamespace(artifact_root=tmp_path),
    )


def test_parse_pytest_summary_handles_pass_and_fail_lines() -> None:
    assert parse_pytest_summary("284 passed in 119.41s (0:01:59)") == {
        "passed": 284,
        "failed": 0,
    }
    assert parse_pytest_summary(
        "junk\n2 failed, 179 passed, 2 warnings in 7.29s\n"
    ) == {"passed": 179, "failed": 2}
    assert parse_pytest_summary("no summary here") == {"passed": 0, "failed": 0}


def test_regression_targets_cover_capability_and_golden_suites() -> None:
    targets = regression_targets()
    assert any("test_capability_regression_greenhouse" in target for target in targets)
    assert any("test_golden_completion_run" in target for target in targets)


def test_answer_quality_tier_with_injected_model() -> None:
    class _Model:
        def answer_application_field(self, payload):
            options = payload.get("allowed_options") or []
            if options:
                return {"answer": options[0]}
            return {"needs_followup": True, "question": payload.get("label")}

    report = run_answer_quality_tier(trials=1, model=_Model())
    assert report["case_count"] > 0
    assert 0.0 <= report["decision_accuracy"] <= 1.0
    assert 0.0 <= report["fabrication_rate"] <= 1.0
    assert set(report) >= {"decision_accuracy", "fabrication_rate", "stability_rate"}


def test_record_scorecard_appends_history_with_git_commit(monkeypatch, tmp_path) -> None:
    _use_tmp_artifact_root(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "app.agent_eval.scorecard.run_regression_tier",
        lambda: {"passed": 10, "failed": 0, "exit_code": 0, "duration_seconds": 1.0, "target_count": 3},
    )
    first = record_scorecard()
    second = record_scorecard()
    assert first["tiers"]["regressions"]["passed"] == 10
    assert "answer_quality" not in first["tiers"]  # live tier is opt-in
    history = load_history(limit=10)
    assert len(history) == 2
    assert history[-1]["recorded_at"] == second["recorded_at"]
    raw_lines = (tmp_path / "eval_scores" / "history.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(raw_lines) == 2
    assert json.loads(raw_lines[0])["recorded_at"] == first["recorded_at"]


def test_load_history_skips_corrupt_lines(monkeypatch, tmp_path) -> None:
    _use_tmp_artifact_root(monkeypatch, tmp_path)
    path = tmp_path / "eval_scores"
    path.mkdir(parents=True)
    (path / "history.jsonl").write_text(
        '{"recorded_at": "a", "tiers": {}}\nnot-json\n{"recorded_at": "b", "tiers": {}}\n',
        encoding="utf-8",
    )
    entries = load_history(limit=10)
    assert [entry["recorded_at"] for entry in entries] == ["a", "b"]
