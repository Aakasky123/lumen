import base64
import json
import threading
import time
from types import SimpleNamespace

from app import external_brain


def _settings(tmp_path, wait=None, purposes=None):
    return SimpleNamespace(external_brain_dir=tmp_path, external_brain_wait_seconds=wait, external_brain_purposes=purposes)


def test_disabled_without_directory(monkeypatch):
    monkeypatch.setattr(external_brain, "get_settings", lambda: _settings(None))
    assert external_brain.enabled() is False
    assert external_brain.handles("vision_agent_step") is False


def test_seats_default_to_the_browser_step(monkeypatch, tmp_path):
    monkeypatch.setattr(external_brain, "get_settings", lambda: _settings(tmp_path))
    assert external_brain.handles("vision_agent_step") is True
    assert external_brain.handles("session_context_compression") is False
    monkeypatch.setattr(external_brain, "get_settings", lambda: _settings(tmp_path, purposes="operator_chat_scope, session_context_compression"))
    assert external_brain.handles("session_context_compression") is True
    assert external_brain.handles("vision_agent_step") is False
    monkeypatch.setattr(external_brain, "get_settings", lambda: _settings(tmp_path, purposes="*"))
    assert external_brain.handles("session_context_compression") is True


def test_step_round_trip_writes_evidence_and_reads_reply(monkeypatch, tmp_path):
    monkeypatch.setattr(external_brain, "get_settings", lambda: _settings(tmp_path, wait=5))
    shot = base64.b64encode(b"\xff\xd8jpeg-bytes").decode()
    zoom = base64.b64encode(b"\x89PNG-bytes").decode()

    def reply():
        request = tmp_path / "step-0001.request.json"
        while not request.exists():
            time.sleep(0.02)
        (tmp_path / "step-0001.response.json").write_text('{"action": "click", "x": 10, "y": 20}', encoding="utf-8")

    threading.Thread(target=reply, daemon=True).start()
    result = external_brain.call(
        purpose="vision_agent_step",
        system="rules",
        prompt={"goal": "g"},
        schema={"type": "object"},
        image=[{"media_type": "image/jpeg", "data": shot}, {"media_type": "image/png", "data": zoom}],
    )

    assert result["ok"] is True
    assert result["data"] == {"action": "click", "x": 10, "y": 20}
    assert result["metadata"]["provider"] == "external_brain"
    assert result["metadata"]["parse_status"] == "json"
    request = json.loads((tmp_path / "step-0001.request.json").read_text(encoding="utf-8"))
    assert request["system"] == "rules"
    assert request["images"] == ["step-0001-0.jpg", "step-0001-1.png"]
    assert (tmp_path / "step-0001-0.jpg").read_bytes() == b"\xff\xd8jpeg-bytes"


def test_missing_reply_times_out_as_transport_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(external_brain, "get_settings", lambda: _settings(tmp_path, wait=0.3))
    (tmp_path / "step-0004.request.json").write_text("{}", encoding="utf-8")

    result = external_brain.call(purpose="vision_agent_step", system="s", prompt={}, schema=None, image={"media_type": "image/jpeg", "data": ""})

    assert result["ok"] is False
    assert result["blocking_reason"] == "external_brain_timeout"
    assert (tmp_path / "step-0005.request.json").exists()


def test_safety_judges_never_take_a_verdict_from_the_mailbox(monkeypatch, tmp_path):
    # Review finding 9: under "*" whoever writes the mailbox would decide secrets, consequences, payments.
    monkeypatch.setattr(external_brain, "get_settings", lambda: _settings(tmp_path, purposes="*"))
    for purpose in ("secret_request_classification", "blocked_click_semantics", "payment_floor_judge", "credential_target_classification"):
        assert external_brain.handles(purpose) is False
    assert external_brain.handles("vision_agent_step") is True
    # Closure review L9: judges of what the user said and what is remembered about them stay in-house too.
    for purpose in ("pending_user_reply_interpretation", "user_memory_reconciliation", "artifact_user_fact_extraction",
                    "page_quote_visual_check", "answer_skip_intent"):
        assert external_brain.handles(purpose) is False


def _answer_in(directory, results):
    def reply():
        request = directory / "step-0001.request.json"
        while not request.exists():
            time.sleep(0.02)
        (directory / "step-0001.response.json").write_text('{"action": "wait"}', encoding="utf-8")

    thread = threading.Thread(target=reply, daemon=True)
    thread.start()
    return thread


def _call():
    return external_brain.call(purpose="vision_agent_step", system="s", prompt={}, schema=None, image=None)


def test_calls_inside_a_run_use_that_runs_own_mailbox(monkeypatch, tmp_path):
    from app import model_call_ledger

    monkeypatch.setattr(external_brain, "get_settings", lambda: _settings(tmp_path, wait=5))
    token = model_call_ledger.bind(42)
    try:
        _answer_in(tmp_path / "run-42", None)
        assert _call()["ok"] is True
        # a second run numbers from 1 in its own directory
        token2 = model_call_ledger.bind(43)
        try:
            _answer_in(tmp_path / "run-43", None)
            assert _call()["ok"] is True
        finally:
            model_call_ledger.release(token2, 43)
    finally:
        model_call_ledger.release(token, 42)
    assert (tmp_path / "run-42" / "step-0001.request.json").exists()
    assert (tmp_path / "run-43" / "step-0001.request.json").exists()
    assert not (tmp_path / "step-0001.request.json").exists()


def test_call_without_run_context_uses_the_root(monkeypatch, tmp_path):
    monkeypatch.setattr(external_brain, "get_settings", lambda: _settings(tmp_path, wait=5))
    _answer_in(tmp_path, None)
    assert _call()["ok"] is True
    assert (tmp_path / "step-0001.request.json").exists()


def test_per_run_split_can_be_turned_off(monkeypatch, tmp_path):
    from app import model_call_ledger

    settings = _settings(tmp_path, wait=5)
    settings.external_brain_per_run = False
    monkeypatch.setattr(external_brain, "get_settings", lambda: settings)
    token = model_call_ledger.bind(7)
    try:
        _answer_in(tmp_path, None)
        assert _call()["ok"] is True
    finally:
        model_call_ledger.release(token, 7)
    assert (tmp_path / "step-0001.request.json").exists()


def test_reply_read_retries_while_the_reasoner_holds_the_file(monkeypatch, tmp_path):
    monkeypatch.setattr(external_brain, "get_settings", lambda: _settings(tmp_path, wait=5))
    real = type(tmp_path).read_text
    state = {"n": 0}

    def flaky(self, *a, **k):
        if self.name.endswith(".response.json") and state["n"] < 2:
            state["n"] += 1
            raise PermissionError(13, "Permission denied")
        return real(self, *a, **k)

    monkeypatch.setattr(type(tmp_path), "read_text", flaky)
    monkeypatch.setattr(external_brain, "_POLL_SECONDS", 0.01)

    def reply():
        request = tmp_path / "step-0001.request.json"
        while not request.exists():
            time.sleep(0.02)
        (tmp_path / "step-0001.response.json").write_text('{"action": "wait"}', encoding="utf-8")

    threading.Thread(target=reply, daemon=True).start()
    out = external_brain.call(purpose="vision_agent_step", system="s", prompt={}, schema=None, image=None)
    assert out["ok"] is True and out["data"] == {"action": "wait"} and state["n"] == 2


def test_the_brain_seat_helper_answers_a_live_request_end_to_end(monkeypatch, tmp_path, capsys):
    from app.agent_eval import brain_seat

    monkeypatch.setattr(external_brain, "get_settings", lambda: _settings(tmp_path, wait=5))
    monkeypatch.setattr(brain_seat, "MAILBOX", tmp_path)
    monkeypatch.setattr(brain_seat, "SEEN", tmp_path / ".seen_system_hashes")
    monkeypatch.setattr(brain_seat, "NOTES", tmp_path / "notes")
    monkeypatch.setenv("BRAIN_TAG", "lane-x")
    shot = base64.b64encode(b"\xff\xd8jpeg-bytes").decode()
    results = []
    caller = threading.Thread(
        target=lambda: results.append(external_brain.call(
            purpose="vision_agent_step", system="rules", prompt={"goal": "g"}, schema={"type": "object"},
            image=[{"media_type": "image/jpeg", "data": shot}],
        )),
        daemon=True,
    )
    caller.start()

    assert brain_seat.wait(5) == 0
    shown = capsys.readouterr().out
    assert "== STEP 1  purpose=vision_agent_step" in shown and "rules" in shown
    assert f"== IMAGE: {(tmp_path / 'step-0001-0.jpg').resolve()}" in shown
    brain_seat.answer(1, '{"action": "done", "reason": "ok"}')
    brain_seat.note(1, "[intent] finish  [good] clear page")
    started = time.monotonic()
    caller.join(5)

    assert results and results[0]["data"] == {"action": "done", "reason": "ok"}
    assert time.monotonic() - started < 1.0
    assert brain_seat.pending() == []
    assert (tmp_path / "notes" / "live.log").read_text(encoding="utf-8") == "[lane-x step 1] [intent] finish [good] clear page\n"
