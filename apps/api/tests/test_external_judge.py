import json
import threading
import time
from types import SimpleNamespace

from app import external_brain, external_judge, model_call_ledger
from app.agent_eval import judge_seat


def _settings(judge=None, brain=None, floor=None, purposes=None, min_deadline=0.0):
    return SimpleNamespace(
        external_judge_dir=judge,
        external_judge_wait_seconds=floor,
        external_judge_min_deadline_seconds=min_deadline,
        external_brain_dir=brain,
        external_brain_wait_seconds=None,
        external_brain_purposes=purposes,
        external_brain_per_run=True,
    )


def _patch(monkeypatch, settings):
    monkeypatch.setattr(external_judge, "get_settings", lambda: settings)
    monkeypatch.setattr(external_brain, "get_settings", lambda: settings)


def _answer_later(box, text, step=1):
    def reply():
        request = box / f"step-{step:04d}.request.json"
        while not request.exists():
            time.sleep(0.01)
        (box / f"step-{step:04d}.response.json").write_text(text, encoding="utf-8")

    threading.Thread(target=reply, daemon=True).start()


def test_flag_off_changes_nothing(monkeypatch, tmp_path):
    _patch(monkeypatch, _settings(judge=None, brain=tmp_path / "brain"))
    assert external_judge.handles("grounding_judge") is False
    assert external_brain.handles("vision_agent_step") is True


def test_purposes_route_to_the_right_mailbox(monkeypatch, tmp_path):
    _patch(monkeypatch, _settings(judge=tmp_path / "judge", brain=tmp_path / "brain", purposes="*"))
    for purpose in ("grounding_judge", "payment_floor_judge", "task_completion_verification",
                    "session_context_compression", "secret_request_classification", "page_quote_visual_check"):
        assert external_judge.handles(purpose) is True
    assert external_judge.handles("vision_agent_step") is False
    assert external_judge.handles("model_provider_health") is False
    assert external_brain.handles("vision_agent_step") is True


def test_safety_purposes_are_never_in_the_brain_mailbox(monkeypatch, tmp_path):
    _patch(monkeypatch, _settings(judge=tmp_path / "judge", brain=tmp_path / "brain", purposes="*"))
    for purpose in external_brain.SAFETY_PURPOSES:
        assert external_brain.handles(purpose) is False
        assert external_judge.handles(purpose) is True


def test_lumen_model_sends_judges_to_the_judge_mailbox(monkeypatch, tmp_path):
    from app.lumen_model import LumenModelClient

    _patch(monkeypatch, _settings(judge=tmp_path / "judge", brain=tmp_path / "brain", purposes="*"))
    _answer_later(tmp_path / "judge", '{"ok": true}')
    model = LumenModelClient.__new__(LumenModelClient)
    result = model._call_model_json_unrecorded(purpose="grounding_judge", system="s", prompt={}, schema={"type": "object"})
    assert result["ok"] is True and result["metadata"]["provider"] == "external_judge"
    assert not (tmp_path / "brain").exists()


def test_round_trip_records_latency_in_the_ledger(monkeypatch, tmp_path):
    _patch(monkeypatch, _settings(judge=tmp_path, brain=tmp_path.parent / "brain-x"))
    box = tmp_path / "run-7"
    token = model_call_ledger.bind(7)
    try:
        _answer_later(box, '{"verdict": "yes"}')
        schema = {"type": "object", "required": ["verdict"], "properties": {"verdict": {"type": "string"}}}
        result = external_judge.call(purpose="grounding_judge", system="rules", prompt={"q": 1}, schema=schema, image=None)
        model_call_ledger.record("grounding_judge", result)
    finally:
        ledger = model_call_ledger.release(token, 7)
    assert result["ok"] is True and result["data"] == {"verdict": "yes"}
    assert result["metadata"]["latency_ms"] >= 0
    assert ledger["grounding_judge"]["calls"] == 1 and ledger["grounding_judge"]["models"] == ["external_judge"]
    request = json.loads((box / "step-0001.request.json").read_text(encoding="utf-8"))
    assert request["system"] == "rules" and request["schema"] == schema and request["deadline_seconds"] > 0
    assert str(tmp_path.parent / "brain-x") not in json.dumps(request)


def test_missing_reply_fails_closed_at_the_purpose_deadline(monkeypatch, tmp_path):
    _patch(monkeypatch, _settings(judge=tmp_path))
    started = time.monotonic()
    result = external_judge.call(purpose="payment_floor_judge", system="s", prompt={}, schema=None, image=None, timeout_seconds_override=1)
    assert result["ok"] is False and result["blocking_reason"] == "external_judge_timeout"
    assert time.monotonic() - started < 5


def test_each_purpose_keeps_its_own_deadline(monkeypatch, tmp_path):
    _patch(monkeypatch, _settings(judge=tmp_path))
    assert external_judge.deadline_seconds("payment_floor_judge") == 15
    assert external_judge.deadline_seconds("grounding_judge") == 100
    assert external_judge.deadline_seconds("final_action_outcome_verification") == 20
    assert external_judge.deadline_seconds("unknown_purpose") == external_judge.DEFAULT_DEADLINE_SECONDS
    assert external_judge.deadline_seconds("payment_floor_judge", 9) == 9
    _patch(monkeypatch, _settings(judge=tmp_path, floor=40))
    assert external_judge.deadline_seconds("payment_floor_judge") == 40


def test_invalid_and_mismatched_replies_fail_closed(monkeypatch, tmp_path):
    _patch(monkeypatch, _settings(judge=tmp_path))
    schema = {"type": "object", "required": ["verdict"], "properties": {"verdict": {"enum": ["yes", "no"]}}}
    _answer_later(tmp_path, "not json at all")
    bad = external_judge.call(purpose="grounding_judge", system="s", prompt={}, schema=schema, image=None)
    assert bad["ok"] is False and bad["blocking_reason"] == "external_judge_invalid_reply"
    _answer_later(tmp_path, '{"verdict": "maybe"}', step=2)
    mismatch = external_judge.call(purpose="grounding_judge", system="s", prompt={}, schema=schema, image=None)
    assert mismatch["ok"] is False and mismatch["blocking_reason"] == "external_judge_schema_mismatch"
    _answer_later(tmp_path, '{"other": 1}', step=3)
    missing = external_judge.call(purpose="grounding_judge", system="s", prompt={}, schema={"name": "x", "schema": schema}, image=None)
    assert missing["ok"] is False and missing["blocking_reason"] == "external_judge_schema_mismatch"


def test_overlapping_roots_fail_closed(monkeypatch, tmp_path):
    _patch(monkeypatch, _settings(judge=tmp_path / "brain" / "judge", brain=tmp_path / "brain"))
    result = external_judge.call(purpose="grounding_judge", system="s", prompt={}, schema=None, image=None)
    assert result["ok"] is False and result["blocking_reason"] == "external_judge_misconfigured"
    assert not (tmp_path / "brain" / "judge").exists()


def test_seat_lists_every_pending_request_and_answers_any(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(judge_seat, "ROOT", tmp_path)
    monkeypatch.setattr(judge_seat, "SEEN", tmp_path / ".seen")
    for run, step in (("3", 1), ("3", 2), ("4", 1)):
        box = tmp_path / f"run-{run}"
        box.mkdir(exist_ok=True)
        (box / f"step-{step:04d}.request.json").write_text(
            json.dumps({"purpose": f"p{run}{step}", "system": "sys", "prompt": {"a": 1}, "schema": {"type": "object"}, "images": [], "deadline_seconds": 30}),
            encoding="utf-8",
        )
    assert [ident for ident, _, _ in judge_seat.pending()] == ["3/0001", "3/0002", "4/0001"]
    assert judge_seat.wait(1) == 0
    assert "3 pending" in capsys.readouterr().out
    judge_seat.answer("3/0002", '{"ok": true}')
    assert [ident for ident, _, _ in judge_seat.pending()] == ["3/0001", "4/0001"]
    assert json.loads((tmp_path / "run-3" / "step-0002.response.json").read_text()) == {"ok": True}


def test_empty_judge_dir_env_is_off(monkeypatch):
    from app import external_judge

    monkeypatch.setenv("LUMEN_EXTERNAL_JUDGE_DIR", "")
    assert not external_judge.enabled()
    assert not external_judge.handles("payment_floor_judge")


def test_min_deadline_floor_lifts_every_purpose_and_shows_in_the_request(monkeypatch, tmp_path):
    _patch(monkeypatch, _settings(judge=tmp_path, min_deadline=90))
    assert external_judge.deadline_seconds("payment_floor_judge") == 90
    assert external_judge.deadline_seconds("payment_floor_judge", 9) == 90
    assert external_judge.deadline_seconds("grounding_judge") == 100  # already above the floor
    # The calling judge's own budget cap does not push the wait under the floor.
    from app import judge_limiter

    token = judge_limiter.call_deadline.set(time.monotonic() + 5)
    try:
        assert external_judge.deadline_seconds("grounding_judge") == 90
    finally:
        judge_limiter.call_deadline.reset(token)
    _answer_later(tmp_path / "run-3", '{"ok": true}')
    run = model_call_ledger.bind(3)
    try:
        external_judge.call(purpose="payment_floor_judge", system="s", prompt={}, schema=None, image=None, timeout_seconds_override=5)
    finally:
        model_call_ledger.release(run, 3)
    request = json.loads((tmp_path / "run-3" / "step-0001.request.json").read_text(encoding="utf-8"))
    assert request["deadline_seconds"] == 90


def test_default_floor_is_90_and_off_without_the_mailbox(monkeypatch, tmp_path):
    from app.config import Settings

    assert Settings.model_fields["external_judge_min_deadline_seconds"].default == 90.0
    _patch(monkeypatch, _settings(judge=None, min_deadline=90))
    assert external_judge.stretch(30) == 30
    _patch(monkeypatch, _settings(judge=tmp_path, min_deadline=90))
    assert external_judge.stretch(30) == 90 + external_judge.STRETCH_MARGIN_SECONDS
    assert external_judge.stretch(240) == 240


def test_timeout_is_worded_as_a_slow_judge_and_remembered_per_run(monkeypatch, tmp_path):
    _patch(monkeypatch, _settings(judge=tmp_path))
    run = model_call_ledger.bind(5)
    try:
        result = external_judge.call(
            purpose="secret_request_classification", system="s", prompt={}, schema=None, image=None, timeout_seconds_override=1
        )
        assert result["blocking_reason"] == "external_judge_timeout"
        assert "did not answer in time" in result["error"] and "provider" not in result["error"]
        assert external_judge.timed_out(5, "secret_request_classification") is True
        assert external_judge.timed_out(6, "secret_request_classification") is False
        assert external_judge.timed_out(5, "grounding_judge") is False
        _answer_later(tmp_path / "run-5", '{"ok": true}', step=2)
        assert external_judge.call(
            purpose="secret_request_classification", system="s", prompt={}, schema=None, image=None, timeout_seconds_override=5
        )["ok"]
        assert external_judge.timed_out(5, "secret_request_classification") is False
    finally:
        model_call_ledger.release(run, 5)
