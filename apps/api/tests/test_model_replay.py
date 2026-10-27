from __future__ import annotations

from app.agent_eval import model_replay


def _pool_item(run_id: int, action: str = "click", **extra: object) -> dict:
    return {"run_id": run_id, "action": action, "input": "x", "shot": "y", **extra}


def test_stratified_sample_caps_runs_and_click_share() -> None:
    pool = [_pool_item(run_id=1) for _ in range(10)]
    pool += [_pool_item(run_id=index) for index in range(2, 30)]
    pool += [_pool_item(run_id=100 + index, action="type") for index in range(6)]

    sample = model_replay.stratified_sample(pool, target=12)

    assert len(sample) == 12
    assert sum(1 for item in sample if item["run_id"] == 1) <= model_replay.DEFAULT_PER_RUN_CAP
    clicks = sum(1 for item in sample if item["action"] == "click")
    assert clicks <= max(6, 12 // model_replay.DEFAULT_CLICK_SHARE_CAP)
    assert any(item["action"] == "type" for item in sample), "rarer actions keep their share"


def test_score_against_recorded_matches_and_coordinate_distance() -> None:
    recorded = {"action": "click", "control": "  Submit  Application ", "x": 100, "y": 200}
    scored = model_replay.score_against_recorded(
        recorded,
        {"action": "click", "control": "submit application", "x": 103, "y": 204},
    )
    assert scored["action_match"] is True
    assert scored["control_match"] is True
    assert scored["coord_delta_px"] == 5.0

    disagreeing = model_replay.score_against_recorded(recorded, {"action": "ask_user", "control": ""})
    assert disagreeing["action_match"] is False
    assert disagreeing["control_match"] is False
    assert "coord_delta_px" not in disagreeing

    no_controls = model_replay.score_against_recorded({"action": "scroll"}, {"action": "scroll"})
    assert no_controls["control_match"] is None


def test_summarize_reports_validity_latency_and_agreement() -> None:
    rows = [
        {"model": "a", "outcome": "valid", "latency_s": 1.0, "action_match": True, "control_match": True, "coord_delta_px": 2.0},
        {"model": "a", "outcome": "valid", "latency_s": 3.0, "action_match": False, "control_match": None},
        {"model": "a", "outcome": "provider_error", "latency_s": 0.2},
    ]
    (summary,) = model_replay.summarize(rows)
    assert summary["model"] == "a"
    assert summary["n"] == 3
    assert summary["valid"] == 2
    assert summary["errors"] == 1
    assert summary["action_agree"] == "1/2"
    assert summary["control_agree"] == "1/1"
    assert summary["coord_delta_med_px"] == 2.0
