import json
import threading
import time
from types import SimpleNamespace

from app import external_brain, external_judge, model_call_ledger


def _settings(brain=None, escalation=None, judge=None, wait=5, purposes=None):
    return SimpleNamespace(
        external_brain_dir=brain,
        external_brain_wait_seconds=wait,
        external_brain_purposes=purposes,
        external_brain_per_run=True,
        external_escalation_dir=escalation,
        external_judge_dir=judge,
        external_judge_wait_seconds=None,
        external_judge_min_deadline_seconds=0.0,
    )


def _patch(monkeypatch, settings):
    monkeypatch.setattr(external_brain, "get_settings", lambda: settings)
    monkeypatch.setattr(external_judge, "get_settings", lambda: settings)


def _answer_later(box, text, step=1):
    def reply():
        request = box / f"step-{step:04d}.request.json"
        while not request.exists():
            time.sleep(0.01)
        (box / f"step-{step:04d}.response.json").write_text(text, encoding="utf-8")

    threading.Thread(target=reply, daemon=True).start()


def _step(seat):
    return external_brain.call(purpose="vision_agent_step", system="s", prompt={}, schema=None, image=None, seat=seat)


def test_escalation_step_goes_to_the_escalation_mailbox_when_set(monkeypatch, tmp_path):
    _patch(monkeypatch, _settings(brain=tmp_path / "brain", escalation=tmp_path / "escalation"))
    token = model_call_ledger.bind(9)
    try:
        _answer_later(tmp_path / "escalation" / "run-9", '{"action": "done"}')
        result = _step(external_brain.ESCALATION)
    finally:
        model_call_ledger.release(token, 9)
    assert result["ok"] is True and result["data"] == {"action": "done"}
    assert result["metadata"]["provider"] == "external_escalation"
    assert result["metadata"]["external_seat"] == "escalation"
    request = json.loads((tmp_path / "escalation" / "run-9" / "step-0001.request.json").read_text(encoding="utf-8"))
    assert request["purpose"] == "vision_agent_step" and request["reply_file"] == "step-0001.response.json"
    assert str(tmp_path / "brain") not in json.dumps(request)
    assert not (tmp_path / "brain").exists()


def test_brain_steps_stay_in_the_brain_mailbox_when_escalation_is_set(monkeypatch, tmp_path):
    _patch(monkeypatch, _settings(brain=tmp_path / "brain", escalation=tmp_path / "escalation"))
    _answer_later(tmp_path / "brain", '{"action": "wait"}')
    result = _step(external_brain.BRAIN)
    assert result["ok"] is True and result["metadata"]["provider"] == "external_brain"
    assert not (tmp_path / "escalation").exists()


def test_escalation_step_goes_to_the_brain_mailbox_when_unset(monkeypatch, tmp_path):
    _patch(monkeypatch, _settings(brain=tmp_path / "brain", escalation=None))
    assert external_brain.handles("vision_agent_step", external_brain.ESCALATION) is True
    _answer_later(tmp_path / "brain", '{"action": "done"}')
    result = _step(external_brain.ESCALATION)
    assert result["ok"] is True and result["metadata"]["provider"] == "external_brain"
    # An empty variable parses as Path("."): that is unset, never the working directory.
    _patch(monkeypatch, _settings(brain=tmp_path / "brain", escalation=type(tmp_path)(".")))
    assert external_brain.escalation_enabled() is False


def test_escalation_mailbox_takes_only_the_step_decision_and_never_a_safety_purpose(monkeypatch, tmp_path):
    _patch(monkeypatch, _settings(brain=tmp_path / "brain", escalation=tmp_path / "escalation", purposes="*"))
    for purpose in external_brain.SAFETY_PURPOSES:
        assert external_brain.handles(purpose, external_brain.ESCALATION) is False
    assert external_brain.mailbox(external_brain.ESCALATION, "session_context_compression") == tmp_path / "brain"
    assert external_brain.mailbox(external_brain.ESCALATION, "vision_agent_step") == tmp_path / "escalation"
    # Without a brain mailbox an escalated step still reaches the escalation seat, and nothing else does.
    _patch(monkeypatch, _settings(brain=None, escalation=tmp_path / "escalation"))
    assert external_brain.handles("vision_agent_step", external_brain.ESCALATION) is True
    assert external_brain.handles("vision_agent_step", external_brain.BRAIN) is False
    assert external_brain.handles("grounding_judge", external_brain.ESCALATION) is False


def test_escalation_root_that_is_not_independent_fails_closed(monkeypatch, tmp_path):
    for escalation, judge in (
        (tmp_path / "brain", None),
        (tmp_path / "brain" / "esc", None),
        (tmp_path, None),
        (tmp_path / "judge" / "esc", tmp_path / "judge"),
        (tmp_path / "judge", tmp_path / "judge"),
    ):
        _patch(monkeypatch, _settings(brain=tmp_path / "brain", escalation=escalation, judge=judge))
        result = _step(external_brain.ESCALATION)
        assert result["ok"] is False and result["blocking_reason"] == "external_escalation_misconfigured"
        assert str(tmp_path) not in result["error"]
    assert not any(tmp_path.rglob("step-*.request.json"))


def test_escalation_timeout_fails_closed_like_the_brain(monkeypatch, tmp_path):
    _patch(monkeypatch, _settings(brain=tmp_path / "brain", escalation=tmp_path / "escalation", wait=0.2))
    result = _step(external_brain.ESCALATION)
    assert result["ok"] is False and result["blocking_reason"] == "external_brain_timeout"
    assert result["data"] == {}


def test_lumen_model_routes_by_the_clients_seat(monkeypatch, tmp_path):
    from app.lumen_model import LumenModelClient

    _patch(monkeypatch, _settings(brain=tmp_path / "brain", escalation=tmp_path / "escalation"))
    model = LumenModelClient.__new__(LumenModelClient)
    model.vision_seat = external_brain.ESCALATION
    _answer_later(tmp_path / "escalation", '{"action": "done"}')
    result = model._call_model_json_unrecorded(purpose="vision_agent_step", system="s", prompt={}, schema={"type": "object"})
    assert result["ok"] is True and result["metadata"]["provider"] == "external_escalation"
    assert not (tmp_path / "brain").exists()


def test_decision_worker_carries_the_seat_from_the_run_context(monkeypatch, tmp_path):
    from app import extension_run, vision_agent

    seen = []
    monkeypatch.setattr(vision_agent, "decide_next_action", lambda client, *_: seen.append(client.vision_seat))
    extension_run._decide_vision_action(1, [], {}, {"_vision_seat": external_brain.ESCALATION})
    extension_run._decide_vision_action(1, [], {}, {})
    assert seen == ["escalation", "brain"]
    _patch(monkeypatch, _settings(brain=None, escalation=tmp_path / "escalation", wait=123))
    assert extension_run._vision_step_timeout_seconds(None, seat=external_brain.ESCALATION) == 123
    assert extension_run._vision_step_timeout_seconds(None) != 123
