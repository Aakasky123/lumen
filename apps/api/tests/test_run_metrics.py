"""Metrics across runs (app.run_metrics, `python -m app.agent_eval.metrics`, GET /operator/metrics):
per-run folding of timing/cost/refusal events, gauntlet trends and regressions from scorecards, a
read-only CLI, and a local-only endpoint."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import models, remote_access, run_metrics
from app.agent_eval import metrics as metrics_cli
from app.db import Base, SessionLocal, init_db
from app.runtime import OperatorRuntimeEngine

T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=timezone.utc)


def _run(db: Session, *, state: str, url: str = "http://127.0.0.1:9/eval/bench/v4_text") -> models.OperatorRun:
    user = OperatorRuntimeEngine(db).ensure_default_user()
    run = models.OperatorRun(user_id=user.id, goal="Complete the page", target_url=url, current_state=state)
    db.add(run)
    db.flush()
    return run


def _event(db: Session, run_id: int, event_type: str, payload: dict, seconds: float) -> None:
    db.add(
        models.CognitiveEvent(
            run_id=run_id,
            event_type=event_type,
            event_payload=payload,
            timestamp=T0 + timedelta(seconds=seconds),
        )
    )


def _step(outcome: str, **extra) -> dict:
    return {"step": 1, "observe_ms": 100, "decide_ms": 2000, "act_ms": 300, "verify_ms": 0, "outcome": outcome,
            "executed_subactions": 1 if outcome == "executed" else 0, **extra}


def _summary(steps: int, actions: int, decide_ms: int, judge_ms: int) -> dict:
    return {
        "totals_ms": {"observe_ms": 1000, "decide_ms": decide_ms, "act_ms": 500, "verify_ms": 0, "judge_ms": judge_ms},
        "waste_seconds": {"rejection_tax": 1.5, "empty_tax": 0.0},
        "decision_latency_seconds": {"p50": 2.0, "p95": 4.0, "count": steps},
        "steps": steps,
        "executed_actions": actions,
    }


def _seed_full_run(db: Session) -> int:
    run = _run(db, state="BLOCKED")
    rid = run.id
    _event(db, rid, "IN_BROWSER_DRIVE_STARTED", {}, 0)
    _event(db, rid, "STEP_TIMING", _step("executed"), 10)
    _event(db, rid, "STEP_TIMING", _step("sequence_invalid", rejection_reason="click_target_text_mismatch"), 20)
    _event(db, rid, "STEP_TIMING", _step("executed_without_durable_progress"), 30)
    _event(db, rid, "STEP_TIMING", _step("empty_length"), 40)
    _event(db, rid, "VISION_GROUNDING_CITATION_REJECTED", {"reason": "no_quote", "judge_reason": "free text"}, 41)
    _event(db, rid, "VISION_SEQUENCE_FOLLOWUP_DROPPED", {"reason": "fresh_observation_required_before_click"}, 42)
    _event(db, rid, "VISION_ASK_TARGET_BINDING_REJECTED", {"reason": "A long judge explanation"}, 43)
    _event(db, rid, "VISION_MODEL_SCHEMA_INVALID",
           {"invalid_reason": "provider_non_json", "attempt_record": {"finish_reason": "length"}}, 44)
    _event(db, rid, "VISION_MODEL_EMPTY_COMPLETION", {"attempt_record": {"failure_reason": "provider_empty_object"}}, 45)
    _event(db, rid, "VISION_BRAIN_ESCALATED", {"reason": "repeated_failures"}, 46)
    _event(db, rid, "VISION_MODEL_TRANSPORT_RECEIPT", {"model": "brain/a", "cost_usd": 0.01}, 47)
    _event(db, rid, "VISION_MODEL_TRANSPORT_RECEIPT", {"model": "brain/b", "cost_usd": 0.02}, 48)
    # Two loops (the run was resumed once): their summaries add up.
    _event(db, rid, "RUN_TIMING_SUMMARY", _summary(3, 2, 6000, 4000), 50)
    _event(db, rid, "RUN_TIMING_SUMMARY", _summary(1, 0, 2000, 1000), 90)
    _event(db, rid, "RUN_MODEL_CALLS", {"by_purpose": {
        "vision_agent_step": {"calls": 4, "failed": 0, "latency_ms": 8000, "cost_usd": 0.03},
        "vision_agent_step:hedge_extra": {"calls": 1, "failed": 0, "latency_ms": 2000, "cost_usd": 0.005},
        "grounding_judge": {"calls": 2, "failed": 0, "latency_ms": 3000, "cost_usd": 0.002},
    }}, 90)
    _event(db, rid, "EVAL_CANDIDATE_RECORDED", {"reason": "grounding_recovery_exhausted"}, 91)
    # Heartbeats after the loop ended do not stretch its wall time.
    _event(db, rid, "RUN_HEARTBEAT", {}, 5000)
    return rid


def test_collect_run_metrics_folds_timing_cost_and_failure_reasons() -> None:
    init_db()
    with SessionLocal() as db:
        rid = _seed_full_run(db)
        db.commit()
    with SessionLocal() as db:
        [run] = run_metrics.collect_run_metrics(db.connection(), run_ids=[rid])

    assert run["run_id"] == rid and run["state"] == "BLOCKED"
    assert run["reason"] == "grounding_recovery_exhausted"
    assert run["loop_end_outcome"] == "empty_length"
    assert (run["page"], run["page_source"]) == ("v4_text", "target_url")
    assert (run["loops"], run["steps"], run["executed_actions"]) == (2, 4, 2)
    assert run["time_source"] == "run_timing_summary"
    assert run["time_seconds"] == {"observe": 2.0, "decide": 8.0, "judge": 5.0, "act": 1.0, "verify": 0.0}
    assert run["waste_seconds"]["rejection_tax"] == 3.0
    assert run["wall_seconds"] == 90.0  # first event to the last loop summary
    assert run["cost_usd"] == {"brain": 0.035, "judges": 0.002, "total": 0.037}
    assert run["cost_source"] == "run_model_calls"
    assert run["decisions_by_model"] == {"brain/a": 1, "brain/b": 1}
    assert run["counts"] == {
        "refusals": 4,  # sequence_invalid step + grounding + follow-up dropped + ask binding
        "sequence_invalid": 1,
        "invalid_outputs": 2,
        "length_capped": 2,  # the empty_length step and the length-capped non-JSON reply
        "cut_off_replies": 0,
        "cut_off_recovered": 0,
        "no_progress": 1,
        "escalations": 1,
        "deescalations": 0,
    }
    assert run["step_outcomes"] == {
        "sequence_invalid:click_target_text_mismatch": 1,
        "executed_without_durable_progress": 1,
        "empty_length": 1,
    }
    assert run["refusal_reasons"] == {
        "grounding_rejected:no_quote": 1,
        "followup_dropped:fresh_observation_required_before_click": 1,
        "ask_target_binding_rejected": 1,
    }
    assert run["model_failures"] == {
        "schema_invalid:provider_non_json": 1,
        "empty_completion:provider_empty_object": 1,
    }
    assert run["escalation_reasons"] == {"repeated_failures": 1}


def test_run_without_summary_uses_its_steps_and_transport_receipts() -> None:
    init_db()
    with SessionLocal() as db:
        run = _run(db, state="CANCELLED", url="https://example.test/task")
        rid = run.id
        _event(db, rid, "STEP_TIMING", _step("executed"), 5)
        _event(db, rid, "STEP_TIMING", _step("executed"), 15)
        _event(db, rid, "VISION_MODEL_TRANSPORT_RECEIPT", {"model": "brain/a", "cost_usd": 0.004}, 14)
        _event(db, rid, "RUN_STATE_CHANGED", {"from_state": "RUNNING", "to_state": "CANCELLED", "reason": "Stopped by user"}, 20)
        db.commit()
    with SessionLocal() as db:
        [metrics] = run_metrics.collect_run_metrics(db.connection(), run_ids=[rid])

    assert metrics["time_source"] == "step_timing"
    assert (metrics["steps"], metrics["executed_actions"]) == (2, 2)
    assert metrics["time_seconds"]["decide"] == 4.0
    # The last step succeeded yet the run did not finish: it was stopped from outside.
    assert metrics["reason"] == "Stopped by user"
    assert metrics["cost_usd"] == {"brain": 0.004, "judges": None, "total": None}
    assert metrics["cost_source"] == "transport_receipts"
    assert metrics["page"] is None


def test_runs_that_never_ran_the_loop_are_outside_the_window() -> None:
    init_db()
    with SessionLocal() as db:
        idle = _run(db, state="CREATED").id
        ran = _run(db, state="READY_FOR_REVIEW").id
        _event(db, ran, "STEP_TIMING", _step("done"), 1)
        db.commit()
    with SessionLocal() as db:
        runs = run_metrics.collect_run_metrics(db.connection(), run_ids=[idle, ran])
    assert [run["run_id"] for run in runs] == [ran]
    assert runs[0]["reason"] == "done"


def _scorecard(root: Path, name: str, recorded_at: str, commit: str | None, pages: dict, *, benchmark: str = "lumen-interaction-gauntlet") -> None:
    folder = root / name
    folder.mkdir(parents=True)
    by_page = {
        page: {
            "passed": passed,
            "completed": completed,
            "total": 5,
            "run_id": run_id,
            "final_state": "READY_FOR_REVIEW" if passed else "BLOCKED",
            "model_usage": {"decision_cost_usd": 0.1, "hedge_extra_cost_usd": 0.0, "judge_cost_usd": 0.02},
        }
        for page, (passed, completed, run_id) in pages.items()
    }
    data = {"benchmark": benchmark, "recorded_at": recorded_at, "by_page": by_page}
    if commit:
        # c1 is an older scorecard that named the Brain seat "vision"; later ones name it "brain".
        role = {"vision": {"model_name": "brain/a"}} if commit == "c1" else {"brain": {"model_name": "brain/b"}}
        data["provenance"] = {"commit": commit, "model_roles": role}
    (folder / "scorecard.json").write_text(json.dumps(data), encoding="utf-8")


def _seed_scorecards(root: Path) -> None:
    _scorecard(root, "gauntlet-a", "2026-10-01T00:00:00+00:00", "c1", {"alpha": (True, 5, 9001), "beta": (True, 5, 9002)})
    _scorecard(root, "lanes-b", "2026-10-02T00:00:00+00:00", "c1", {"beta": (False, 3, 9003)})
    _scorecard(root, "gauntlet-c", "2026-10-03T00:00:00+00:00", "c2", {"alpha": (False, 4, 9004), "gamma": (True, 5, 9005)})
    _scorecard(root, "other-bench", "2026-10-04T00:00:00+00:00", "c3", {"alpha": (True, 5, 9006)}, benchmark="something-else")


def test_gauntlet_history_trends_and_regressions(tmp_path) -> None:
    _seed_scorecards(tmp_path)
    attempts = run_metrics.gauntlet_attempts(tmp_path)
    assert [attempt["dir"] for attempt in attempts] == ["gauntlet-a", "gauntlet-a", "lanes-b", "gauntlet-c", "gauntlet-c"]
    assert [attempt["brain_model"] for attempt in attempts] == ["brain/a", "brain/a", "brain/a", "brain/b", "brain/b"]
    history = run_metrics.gauntlet_history(attempts)

    alpha = history["pages"]["alpha"]
    assert (alpha["trend"], alpha["passes"], alpha["attempts"]) == ("PF", 1, 2)
    assert alpha["best"]["commit"] == "c1" and alpha["latest"]["commit"] == "c2"
    assert alpha["last_pass_commit"] == "c1"
    assert alpha["latest"]["cost_usd"] == 0.12
    assert history["pages"]["gamma"]["trend"] == "P"
    kinds = {item["page"]: item["kind"] for item in history["regressions"]}
    # alpha passed at c1 and fails at c2; beta passed and failed at the same commit.
    assert kinds == {"alpha": "regression", "beta": "flaky"}

    only_gamma = run_metrics.gauntlet_history(attempts, pages=["gamma"])
    assert list(only_gamma["pages"]) == ["gamma"] and only_gamma["regressions"] == []
    recent = run_metrics.gauntlet_history(attempts, since=datetime(2026, 10, 2, tzinfo=timezone.utc))
    # Passes before the window do not count: nothing in it passed and then failed.
    assert recent["pages"]["alpha"]["trend"] == "F" and recent["regressions"] == []


def test_gauntlet_history_folds_repeat_runs_into_their_page(tmp_path) -> None:
    # live_run --repeat names runs page@rN; a focus run (page#challenge) is a series of its own.
    _scorecard(tmp_path, "gauntlet-r", "2026-10-06T00:00:00+00:00", "c4",
               {"v6_mail@r1": (True, 5, 9101), "v6_mail@r2": (False, 4, 9102), "v6_mail#report_phish@r1": (True, 1, 9103)})
    attempts = run_metrics.gauntlet_attempts(tmp_path)
    assert sorted((a["page"], a["run_tag"]) for a in attempts) == [
        ("v6_mail", "r1"), ("v6_mail", "r2"), ("v6_mail#report_phish", "r1")]
    history = run_metrics.gauntlet_history(attempts, pages=["v6_mail"])
    assert list(history["pages"]) == ["v6_mail"] and history["pages"]["v6_mail"]["attempts"] == 2


def test_build_report_names_gauntlet_runs_and_filters_pages(tmp_path) -> None:
    init_db()
    with SessionLocal() as db:
        scored = _run(db, state="READY_FOR_REVIEW", url="https://example.test/no-bench-path").id
        other = _run(db, state="BLOCKED", url="http://127.0.0.1:9/eval/bench/v4_keys").id
        _event(db, scored, "STEP_TIMING", _step("done"), 1)
        _event(db, other, "STEP_TIMING", _step("invalid"), 1)
        db.commit()
    _scorecard(tmp_path, "gauntlet-x", "2026-10-05T00:00:00+00:00", "c9", {"delta": (True, 5, scored)})

    with SessionLocal() as db:
        report = run_metrics.build_report(db.connection(), tmp_path, run_ids=[scored, other])
        filtered = run_metrics.build_report(db.connection(), tmp_path, run_ids=[scored, other], pages=["delta"])

    by_id = {run["run_id"]: run for run in report["runs"]}
    assert by_id[scored]["page"] == "delta" and by_id[scored]["page_source"] == "gauntlet_scorecard"
    assert by_id[scored]["bench"] == {"passed": True, "completed": 5, "total": 5, "scorecard": "gauntlet-x", "commit": "c9"}
    assert by_id[other]["page"] == "v4_keys"
    assert report["totals"]["runs"] == 2
    assert report["totals"]["step_outcomes"] == {"invalid": 1}
    assert [run["run_id"] for run in filtered["runs"]] == [scored]
    assert list(filtered["gauntlet"]["pages"]) == ["delta"]
    json.dumps(report)  # JSON-ready


def test_cli_reads_a_database_file_without_changing_it(tmp_path, capsys) -> None:
    db_path = tmp_path / "copy.db"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    Base.metadata.create_all(bind=engine)
    with Session(engine) as db:
        user = models.User(email="metrics@example.test", name="Metrics")
        db.add(user)
        db.flush()
        run = models.OperatorRun(user_id=user.id, goal="g", target_url="http://127.0.0.1:9/eval/bench/v4_text",
                                 current_state="READY_FOR_REVIEW")
        db.add(run)
        db.flush()
        _event(db, run.id, "STEP_TIMING", _step("done"), 1)
        _event(db, run.id, "RUN_TIMING_SUMMARY", _summary(1, 0, 3000, 0), 2)
        db.commit()
    engine.dispose()
    eval_runs = tmp_path / "eval_runs"
    _seed_scorecards(eval_runs)
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()

    assert metrics_cli.main(["--db", str(db_path), "--eval-runs", str(eval_runs), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert [run["page"] for run in report["runs"]] == ["v4_text"]
    assert report["gauntlet"]["regressions"][0]["page"] == "alpha"

    assert metrics_cli.main(["--db", str(db_path), "--eval-runs", str(eval_runs), "--pages", "alpha,beta"]) == 0
    text_out = capsys.readouterr().out
    assert "REGRESSIONS (1 regression, 1 flaky)" in text_out and "gamma" not in text_out

    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before
    assert not Path(f"{db_path}-wal").exists()


def test_metrics_endpoint_is_local_only() -> None:
    from app.main import app

    client = TestClient(app)
    response = client.get("/operator/metrics", params={"limit": 3})
    assert response.status_code == 200
    body = response.json()
    assert {"runs", "totals", "gauntlet", "osworld", "window"} <= set(body)
    assert len(body["runs"]) <= 3
    assert client.get("/operator/metrics", params={"since": "not a date"}).status_code == 422
    assert remote_access.remote_route_policy("GET", "/operator/metrics") is None


def test_mac_bench_scorecards_are_reported_apart_from_the_gauntlet(tmp_path) -> None:
    _seed_scorecards(tmp_path)
    _scorecard(tmp_path, "mac-a", "2026-10-07T00:00:00+00:00", "m1", {"alpha": (False, 1, 9201)}, benchmark=run_metrics.MAC_BENCHMARK)
    _scorecard(tmp_path, "mac-b", "2026-10-08T00:00:00+00:00", "m2", {"alpha": (True, 5, 9202)}, benchmark=run_metrics.MAC_BENCHMARK)
    gauntlet = run_metrics.gauntlet_attempts(tmp_path)
    assert all(a["benchmark"] == run_metrics.GAUNTLET_BENCHMARK for a in gauntlet)
    assert [a["dir"] for a in run_metrics.gauntlet_attempts(tmp_path, run_metrics.MAC_BENCHMARK)] == ["mac-a", "mac-b"]
    with SessionLocal() as db:
        report = run_metrics.build_report(db.connection(), tmp_path)
    assert report["mac_bench"]["pages"]["alpha"]["trend"] == "FP"
    assert report["gauntlet"]["pages"]["alpha"]["trend"] == "PF"
    text = metrics_cli.render_text(report)
    assert "MAC BENCH TASKS" in text and "MAC BENCH REGRESSIONS" in text and "GAUNTLET PAGES" in text


def test_cache_hit_rate_per_run_and_in_the_aggregate() -> None:
    init_db()
    with SessionLocal() as db:
        rid = _seed_full_run(db)
        _event(db, rid, "RUN_MODEL_CALLS", {"by_purpose": {
            "vision_agent_step": {"calls": 2, "prompt_tokens": 1000, "cached_prompt_tokens": 750, "completion_tokens": 10, "cost_usd": 0.01},
            "grounding_judge": {"calls": 1, "prompt_tokens": 1000, "cached_prompt_tokens": 0, "completion_tokens": 10, "cost_usd": 0.01},
        }}, 95)
        db.commit()
    with SessionLocal() as db:
        [metrics] = run_metrics.collect_run_metrics(db.connection(), run_ids=[rid])
    assert metrics["cache_hit_rate"] == 0.375
    assert metrics["model_calls"]["vision_agent_step"]["cache_hit_rate"] == 0.75
    assert metrics["brain_cache_hit_rate"] == 0.75
    assert run_metrics._cache_rate(1, 0) is None
    assert run_metrics._window_totals([metrics])["cache_hit_rate"] == metrics["cache_hit_rate"]
