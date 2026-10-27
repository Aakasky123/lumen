"""Opt-in brain message history (LUMEN_BRAIN_MESSAGE_HISTORY): earlier reply/outcome turns before each step's packet.

Off by default the request is the one the brain always sent. On, the turns are the last K steps (block-anchored
window), in order, with typed secrets projected away, capped by size, and byte-stable between consecutive steps.
"""

import json
from types import SimpleNamespace

import pytest

from app import extension_run, vision_agent
from app.config import Settings
from app.model_provider import ResolvedModelProvider, call_model_json
from app.vision_agent import (
    MESSAGE_HISTORY_HEADER,
    ReplyAction,
    message_history_turns,
    message_history_window_start,
    prepare_model_input,
)


class _FakeResponse:
    def __init__(self, body: dict):
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._body


_RESPONSE_BODIES = {
    "anthropic": {"content": [{"type": "text", "text": "{}"}], "usage": {}},
    "openai": {"output_text": "{}", "usage": {}},
    "custom_openai_compatible": {"choices": [{"message": {"content": "{}"}}], "usage": {}},
    "gemini": {"candidates": [{"content": {"parts": [{"text": "{}"}]}}], "usageMetadata": {}},
}


def _provider(provider_type: str, model_name: str = "transport-test-model") -> ResolvedModelProvider:
    return ResolvedModelProvider(
        id=75,
        display_name=f"{provider_type} message history test",
        provider_type=provider_type,
        model_name=model_name,
        base_url=f"https://{provider_type}.example.test/v1",
        api_key="test-only-key",
        temperature=0.0,
        max_tokens=128,
        timeout_seconds=5,
        enabled=True,
        is_active=True,
        source="database",
    )


@pytest.fixture
def history_on(monkeypatch):
    settings = SimpleNamespace(brain_message_history=True, brain_message_history_steps=8)
    monkeypatch.setattr(vision_agent, "get_settings", lambda: settings)
    return settings


@pytest.fixture
def history_off(monkeypatch):
    settings = SimpleNamespace(brain_message_history=False, brain_message_history_steps=8)
    monkeypatch.setattr(vision_agent, "get_settings", lambda: settings)
    return settings


def _records(steps: int, surface: str = "browser") -> list[dict]:
    return [
        {
            "step": number,
            "surface": surface,
            "reply": json.dumps({"action": "click", "reason": f"reason {number}"}),
            "outcome": f"primary click 'Next {number}' verified",
        }
        for number in range(1, steps + 1)
    ]


# --- settings ----------------------------------------------------------------------------------------------------


def test_setting_is_off_by_default_and_reads_the_lumen_name(monkeypatch) -> None:
    monkeypatch.delenv("LUMEN_BRAIN_MESSAGE_HISTORY", raising=False)
    monkeypatch.delenv("LUMEN_BRAIN_MESSAGE_HISTORY_STEPS", raising=False)
    settings = Settings(_env_file=None)
    assert settings.brain_message_history is False
    assert settings.brain_message_history_steps == 8

    monkeypatch.setenv("LUMEN_BRAIN_MESSAGE_HISTORY", "1")
    monkeypatch.setenv("LUMEN_BRAIN_MESSAGE_HISTORY_STEPS", "5")
    on = Settings(_env_file=None)
    assert on.brain_message_history is True
    assert on.brain_message_history_steps == 5


# --- off: the request is byte-identical to today ---------------------------------------------------------------


def test_off_envelope_ignores_history_and_matches_the_plain_envelope(history_off) -> None:
    view = {"width": 800, "height": 600}
    plain = prepare_model_input(view, {"goal": "Apply", "execution_surface": "browser"})
    with_turns = prepare_model_input(
        view,
        {
            "goal": "Apply",
            "execution_surface": "browser",
            "_message_history": message_history_turns(_records(3), step=4, surface="browser", steps=8),
        },
    )
    assert "history" not in with_turns
    assert json.dumps(with_turns, sort_keys=True) == json.dumps(plain, sort_keys=True)


def test_off_decide_calls_the_client_without_history_and_returns_a_plain_dict(history_off) -> None:
    captured: dict = {}

    class _Model:
        def _call_model_json(self, **kw: object) -> dict:
            captured.update(kw)
            return {"ok": True, "data": {"action": "click", "x": 1, "y": 2, "reason": "go"}, "metadata": {}}

    action = vision_agent.decide_next_action(
        _Model(), {"media_type": "image/jpeg", "data": "B64"}, {"width": 800, "height": 600}, {"goal": "Apply"}
    )
    assert "history" not in captured
    assert type(action) is dict
    assert action["action"] == "click"


@pytest.mark.parametrize("provider_type", sorted(_RESPONSE_BODIES))
def test_off_provider_request_is_unchanged_by_the_history_parameter(monkeypatch, provider_type: str) -> None:
    bodies: list[dict] = []

    def fake_post(url, **kwargs):
        bodies.append(kwargs["json"])
        return _FakeResponse(_RESPONSE_BODIES[provider_type])

    monkeypatch.setattr("app.model_provider._http_post", fake_post)
    kwargs = {"purpose": "message_history_off", "system": "Return JSON.", "prompt": {"goal": "Inspect."}}
    call_model_json(_provider(provider_type), **kwargs)
    call_model_json(_provider(provider_type), **kwargs, history=None)
    call_model_json(_provider(provider_type), **kwargs, history=[])
    assert bodies[0] == bodies[1] == bodies[2]


# --- window -----------------------------------------------------------------------------------------------------


def test_window_is_block_anchored_and_never_larger_than_the_step_count() -> None:
    starts = {step: message_history_window_start(step, 8) for step in range(1, 201)}
    for step in range(1, 201):
        assert 1 <= starts[step] <= step
        assert step - starts[step] <= 8  # at most 8 earlier steps
    for step in range(9, 201):
        assert step - starts[step] >= 4  # never fewer than K - K//2 once the window is full
    moves = [step for step in range(2, 201) if starts[step] != starts[step - 1]]
    assert moves == [step for step in range(10, 201) if (step - 9) % 4 == 1]


def test_window_of_one_step_is_the_plain_last_step() -> None:
    for step in range(2, 30):
        assert message_history_window_start(step, 1) == step - 1


# --- turns --------------------------------------------------------------------------------------------------------


def test_turns_are_header_then_reply_and_outcome_in_order() -> None:
    records = _records(10)
    turns = message_history_turns(records, step=11, surface="browser", steps=8)
    # window for step 11 is steps 5..10 (six pairs)
    assert turns[0] == {"role": "user", "text": MESSAGE_HISTORY_HEADER}
    assert len(turns) == 1 + 2 * 6
    for offset, number in enumerate(range(5, 11)):
        reply_turn = turns[1 + 2 * offset]
        outcome_turn = turns[2 + 2 * offset]
        assert reply_turn == {"role": "assistant", "text": records[number - 1]["reply"]}
        assert outcome_turn == {"role": "user", "text": records[number - 1]["outcome"]}
    assert turns[-1]["text"] == records[-1]["outcome"]


def test_turns_keep_only_the_current_surface() -> None:
    desktop = _records(3, surface="desktop")
    browser = [{**row, "step": 4, "outcome": "browser outcome"} for row in _records(1)]
    turns = message_history_turns(desktop + browser, step=5, surface="browser", steps=8)
    assert [turn["text"] for turn in turns[1:]] == [browser[0]["reply"], "browser outcome"]
    desktop_turns = message_history_turns(desktop + browser, step=5, surface="desktop", steps=8)
    assert len(desktop_turns) == 1 + 2 * 3


def test_no_earlier_reply_means_no_turns() -> None:
    assert message_history_turns([], step=1, surface="browser") == []
    records = [{**_records(1)[0], "reply": ""}]
    assert message_history_turns(records, step=2, surface="browser") == []


def test_missing_outcome_is_a_stated_placeholder_never_an_empty_turn() -> None:
    records = [{"step": 1, "surface": "browser", "reply": "{}", "outcome": ""}]
    turns = message_history_turns(records, step=2, surface="browser")
    assert turns[2] == {"role": "user", "text": vision_agent.MESSAGE_HISTORY_NO_OUTCOME}


def test_cap_drops_the_oldest_steps_past_six_thousand_tokens() -> None:
    records = [
        {"step": number, "surface": "browser", "reply": f"r{number}" + "x" * 2996, "outcome": "o" * 1000}
        for number in range(1, 9)
    ]
    turns = message_history_turns(records, step=9, surface="browser", steps=8, max_tokens=6000)
    total = sum(len(turn["text"]) // 4 for turn in turns)
    assert total <= 6000
    replies = [turn["text"] for turn in turns if turn["role"] == "assistant"]
    assert len(replies) == 5  # 8 steps at ~1,000 tokens each: the oldest three go
    assert replies[0].startswith("r4")
    assert replies[-1].startswith("r8")


# --- byte stability and wire shape ------------------------------------------------------------------------------


def _flatten(provider_type: str, body: dict) -> list[tuple[str, str]]:
    """(role, text) for every text the request carries, in wire order; gemini's "model" role reads as assistant."""
    flat: list[tuple[str, str]] = []
    if provider_type == "openai":
        for item in body["input"][1:]:
            content = item["content"]
            if isinstance(content, str):
                flat.append((item["role"], content))
            else:
                flat.extend((item["role"], part["text"]) for part in content if part.get("type") == "input_text")
    elif provider_type == "anthropic":
        for message in body["messages"]:
            content = message["content"]
            if isinstance(content, str):
                flat.append((message["role"], content))
            else:
                flat.extend((message["role"], block["text"]) for block in content if block.get("type") == "text")
    elif provider_type == "gemini":
        for content in body["contents"]:
            role = "assistant" if content["role"] == "model" else "user"
            flat.extend((role, part["text"]) for part in content["parts"] if "text" in part)
    else:
        for message in body["messages"][1:]:
            content = message["content"]
            if isinstance(content, str):
                flat.append((message["role"], content))
            else:
                flat.extend((message["role"], part["text"]) for part in content if part.get("type") == "text")
    return flat


def _post(monkeypatch, provider_type: str, history, prompt=None) -> dict:
    posted: dict = {}

    def fake_post(url, **kwargs):
        posted["json"] = kwargs["json"]
        return _FakeResponse(_RESPONSE_BODIES[provider_type])

    monkeypatch.setattr("app.model_provider._http_post", fake_post)
    result = call_model_json(
        _provider(provider_type),
        purpose="vision_agent_step",
        system="Return JSON.",
        prompt=prompt or {"goal": "Inspect the page."},
        history=history,
    )
    assert result["ok"] is True
    return {"body": posted["json"], "metadata": result["metadata"]}


_WIRE_PROVIDERS = ["openai", "anthropic", "gemini", "custom_openai_compatible"]


@pytest.mark.parametrize("provider_type", _WIRE_PROVIDERS)
def test_turns_go_before_the_packet_in_order(monkeypatch, provider_type: str) -> None:
    turns = message_history_turns(_records(3), step=4, surface="browser", steps=8)
    sent = _post(monkeypatch, provider_type, turns)
    flat = _flatten(provider_type, sent["body"])
    expected = [(turn["role"], turn["text"]) for turn in turns]
    assert flat[: len(turns)] == expected
    assert all(role == "user" for role, _ in flat[len(turns) :])
    assert "Inspect the page." in "".join(text for _, text in flat[len(turns) :])
    assert sent["metadata"]["message_history_turns"] == len(turns)


@pytest.mark.parametrize("provider_type", _WIRE_PROVIDERS)
def test_earlier_turns_are_byte_identical_between_consecutive_steps(monkeypatch, provider_type: str) -> None:
    records = _records(10)
    earlier_step = message_history_turns(records, step=10, surface="browser", steps=8)
    next_step = message_history_turns(records, step=11, surface="browser", steps=8)
    assert next_step[: len(earlier_step)] == earlier_step  # the window moved by nothing: the prefix is shared
    first = _flatten(provider_type, _post(monkeypatch, provider_type, earlier_step, {"goal": "Step ten"})["body"])
    second = _flatten(provider_type, _post(monkeypatch, provider_type, next_step, {"goal": "Step eleven"})["body"])
    assert second[: len(earlier_step)] == first[: len(earlier_step)]


def test_anthropic_cache_breakpoints_stay_within_four(monkeypatch) -> None:
    turns = message_history_turns(_records(3), step=4, surface="browser", steps=8)
    with_history = _post(monkeypatch, "anthropic", turns)["body"]
    without = _post(monkeypatch, "anthropic", None)["body"]
    assert json.dumps(with_history).count("cache_control") == 2  # rules block + last earlier turn
    assert json.dumps(without).count("cache_control") == 2  # rules block + stable packet keys
    last_earlier = with_history["messages"][len(turns) - 1]["content"][0]
    assert last_earlier.get("cache_control") == {"type": "ephemeral"}


def test_openrouter_claude_route_keeps_its_breakpoints_on_the_last_earlier_turn(monkeypatch) -> None:
    posted: dict = {}

    def fake_post(url, **kwargs):
        posted["json"] = kwargs["json"]
        return _FakeResponse(_RESPONSE_BODIES["custom_openai_compatible"])

    monkeypatch.setattr("app.model_provider._http_post", fake_post)
    turns = message_history_turns(_records(3), step=4, surface="browser", steps=8)
    result = call_model_json(
        _provider("openrouter", "anthropic/claude-test"),
        purpose="vision_agent_step",
        system="Return JSON.",
        prompt={"goal": "Inspect the page."},
        history=turns,
    )
    assert result["ok"] is True
    body = posted["json"]
    assert [(role, text) for role, text in _flatten("custom_openai_compatible", body)[: len(turns)]] == [
        (turn["role"], turn["text"]) for turn in turns
    ]
    assert json.dumps(body).count("cache_control") == 2  # rules block + last earlier turn
    last_earlier = body["messages"][len(turns)]["content"][0]
    assert last_earlier.get("cache_control") == {"type": "ephemeral"}


def test_gemini_keeps_roles_alternating_with_the_packet_joined_to_the_last_user_turn(monkeypatch) -> None:
    turns = message_history_turns(_records(2), step=3, surface="browser", steps=8)
    body = _post(monkeypatch, "gemini", turns)["body"]
    roles = [content["role"] for content in body["contents"]]
    assert roles == ["user", "model", "user", "model", "user"]
    # the last user turn (the outcome) and the packet share one content: its parts are the outcome, then the packet
    assert len(body["contents"][-1]["parts"]) == 2
    assert "Inspect the page." in body["contents"][-1]["parts"][1]["text"]


# --- redaction -----------------------------------------------------------------------------------------------------


def _type_prepared(sensitive: bool, *, label: str = "Password") -> dict:
    action = {"action": "type", "text": "", "control": 4}
    return {
        "action": action,
        "kind": "type",
        "bound_label": label,
        "bound_control": {"tag": "input", "type": "password"} if sensitive else {"tag": "input", "type": "text"},
        "sensitive_type": sensitive,
    }


def test_typed_value_in_a_sensitive_field_is_projected_out_of_the_reply() -> None:
    raw = {
        "action": "type",
        "text": "hunter2-Secret!",
        "control": 4,
        "reason": "Typing the password hunter2-Secret! into the field",
    }
    text, projected = extension_run._message_history_reply(raw, [_type_prepared(True)])
    parsed = json.loads(text)
    assert projected is True
    assert "hunter2" not in text
    assert parsed["text"] == "[REDACTED]"
    assert parsed["reason"] == "[REDACTED]"
    assert parsed["sensitive_value_redacted"] is True
    assert parsed["control"] == 4


def test_ordinary_typed_value_is_kept_as_the_model_wrote_it() -> None:
    raw = {"action": "type", "text": "Jane Doe", "control": 2, "reason": "Typing the name"}
    text, projected = extension_run._message_history_reply(raw, [_type_prepared(False, label="Name")])
    assert projected is False
    assert json.loads(text) == raw


def test_typed_item_with_no_prepared_entry_fails_closed() -> None:
    raw = {"action": "type", "text": "unconfirmed-value", "control": 3, "reason": "typing"}
    text, projected = extension_run._message_history_reply(raw, [])
    assert projected is True
    assert "unconfirmed-value" not in text


def test_click_with_no_prepared_entry_keeps_its_reason() -> None:
    raw = {"action": "click", "x": 10, "y": 20, "reason": "Opening the menu"}
    text, projected = extension_run._message_history_reply(raw, [])
    assert projected is False
    assert json.loads(text)["reason"] == "Opening the menu"


def test_then_items_are_matched_to_their_prepared_entries_by_position() -> None:
    raw = {
        "action": "click",
        "x": 1,
        "y": 1,
        "reason": "Focus the field",
        "then": [{"action": "type", "text": "hunter2-Secret!", "control": 4, "reason": "typing it"}],
    }
    prepared = [{"action": {"action": "click"}, "kind": "click"}, _type_prepared(True)]
    text, projected = extension_run._message_history_reply(raw, prepared)
    assert projected is True
    assert "hunter2" not in text
    assert json.loads(text)["reason"] == "Focus the field"


def test_otp_code_in_the_reply_is_redacted_as_on_every_transport() -> None:
    raw = {"action": "click", "x": 1, "y": 1, "reason": "Enter the verification code 482913 from the email"}
    text, _ = extension_run._message_history_reply(raw, [{"action": {"action": "click"}, "kind": "click"}])
    assert "482913" not in text
    assert "[REDACTED_OTP]" in text


def test_the_brains_notes_and_plan_stay_out_of_the_turn() -> None:
    raw = {
        "action": "click",
        "x": 1,
        "y": 1,
        "reason": "Open the code screen",
        "notes": ["step 2: the access code is 7731 keep it"],
        "plan": "[now] enter the code",
    }
    text, _ = extension_run._message_history_reply(raw, [{"action": {"action": "click"}, "kind": "click"}])
    parsed = json.loads(text)
    assert "notes" not in parsed and "plan" not in parsed
    assert "7731" not in text
    assert parsed["reason"] == "Open the code screen"


def test_outcome_of_a_projected_step_carries_no_typed_value_or_readback() -> None:
    trace = {
        "step": 3,
        "surface": "browser",
        "raw_reply": {"action": "type", "text": "hunter2-Secret!", "control": 4, "reason": "typing the password"},
        "actions": [{"action": "type", "text": "hunter2-Secret!", "control": 4}],
        "prepared": [_type_prepared(True)],
        "outcomes": {0: {"ok": True, "verified": True, "readback": "hunter2-Secret!"}},
        "primary_drop": "",
    }
    records: list[dict] = []
    extension_run._record_message_history(records, trace, last_reply_text="primary type 'Password' verified")
    assert len(records) == 1
    record = records[0]
    assert record["step"] == 3 and record["surface"] == "browser"
    assert "hunter2" not in record["reply"]
    assert "hunter2" not in record["outcome"]
    assert "verified" in record["outcome"]


def test_fallback_reply_without_a_kept_raw_reply_is_projected_too() -> None:
    trace = {
        "step": 2,
        "surface": "browser",
        "raw_reply": None,
        "actions": [{"action": "type", "text": "hunter2-Secret!", "control": 4, "_model_metadata": {}}],
        "prepared": [_type_prepared(True)],
        "outcomes": {0: {"ok": True, "verified": True}},
    }
    records: list[dict] = []
    extension_run._record_message_history(records, trace, last_reply_text="")
    assert len(records) == 1
    assert "hunter2" not in records[0]["reply"]
    assert "_model_metadata" not in records[0]["reply"]


def test_ordinary_step_outcome_is_the_last_reply_line_the_packet_shows() -> None:
    trace = {
        "step": 5,
        "surface": "browser",
        "raw_reply": {"action": "click", "x": 3, "y": 4, "reason": "Next"},
        "actions": [{"action": "click", "x": 3, "y": 4}],
        "prepared": [{"action": {"action": "click"}, "kind": "click"}],
        "outcomes": {0: {"ok": True, "verified": True}},
    }
    records: list[dict] = []
    extension_run._record_message_history(records, trace, last_reply_text="primary click verified")
    assert records[0]["outcome"] == "primary click verified"


# --- decide path -----------------------------------------------------------------------------------------------------


def test_on_decide_passes_the_turns_and_keeps_the_reply_off_the_action_keys(history_on) -> None:
    turns = message_history_turns(_records(2), step=3, surface="browser", steps=8)
    view = {"width": 800, "height": 600}
    context = {"goal": "Apply", "execution_surface": "browser", "_message_history": turns}
    context["_prepared_model_input"] = prepare_model_input(view, context)
    captured: dict = {}
    data = {"action": "click", "x": 5, "y": 6, "reason": "Next button"}

    class _Model:
        def _call_model_json(self, **kw: object) -> dict:
            captured.update(kw)
            return {"ok": True, "data": dict(data), "metadata": {}}

    action = vision_agent.decide_next_action(_Model(), {"media_type": "image/jpeg", "data": "B64"}, view, context)
    assert captured["history"] == context["_prepared_model_input"]["history"]
    assert [turn["role"] for turn in captured["history"]] == ["user", "assistant", "user", "assistant", "user"]
    assert isinstance(action, ReplyAction)
    assert action.raw_reply == data
    assert "raw_reply" not in action
    assert json.dumps(action, sort_keys=True).count("Next button") == 1


def test_on_decide_without_earlier_turns_sends_the_plain_request(history_on) -> None:
    view = {"width": 800, "height": 600}
    context = {"goal": "Apply", "execution_surface": "browser", "_message_history": []}
    context["_prepared_model_input"] = prepare_model_input(view, context)
    captured: dict = {}

    class _Model:
        def _call_model_json(self, **kw: object) -> dict:
            captured.update(kw)
            return {"ok": True, "data": {"action": "done", "reason": "ready"}, "metadata": {}}

    vision_agent.decide_next_action(_Model(), {"media_type": "image/jpeg", "data": "B64"}, view, context)
    assert "history" not in captured
    assert "history" not in context["_prepared_model_input"]


# --- the run loop -----------------------------------------------------------------------------------------------------


def test_loop_gives_each_step_its_earlier_turns_and_keeps_earlier_turns_identical(monkeypatch) -> None:
    import asyncio

    from app.db import init_db
    from app.extension_run import run_vision_loop

    from test_extension_run import _VisionBody
    from test_loop_budgets import _narrate, _stub_loop

    init_db()
    monkeypatch.setattr(vision_agent, "get_settings", lambda: SimpleNamespace(brain_message_history=True, brain_message_history_steps=8))
    replies = [
        {"action": "scroll", "direction": "down", "amount": 300, "reason": "First reply: look further down."},
        {"action": "scroll", "direction": "down", "amount": 300, "reason": "Second reply: keep looking."},
        {"action": "scroll", "direction": "down", "amount": 300, "reason": "Third reply: still looking."},
    ]
    seen: list = []

    class _Client:
        def _call_model_json(self, **kwargs: object) -> dict:
            seen.append(kwargs.get("history"))
            return {"ok": True, "data": dict(replies[min(len(seen), len(replies)) - 1]), "metadata": {}}

    client = _Client()
    _stub_loop(monkeypatch, lambda user_id, image, view, context: vision_agent.decide_next_action(client, image, view, context))
    monkeypatch.setattr(extension_run, "_db_event", lambda *args, **kwargs: None)

    asyncio.run(run_vision_loop(_VisionBody(), url=None, narrate=_narrate, run_id=0, user_id=0, skip_navigate=True, max_steps=3))

    assert len(seen) == 3
    assert seen[0] is None  # step 1: nothing earlier
    assert [turn["role"] for turn in seen[1]] == ["user", "assistant", "user"]
    assert "First reply: look further down." in seen[1][1]["text"]
    assert len(seen[2]) == 5
    assert seen[2][:3] == seen[1]  # the earlier turns did not change between consecutive steps
    assert "Second reply: keep looking." in seen[2][3]["text"]


def test_loop_sends_no_history_when_the_switch_is_off(monkeypatch) -> None:
    import asyncio

    from app.db import init_db
    from app.extension_run import run_vision_loop

    from test_extension_run import _VisionBody
    from test_loop_budgets import _narrate, _stub_loop

    init_db()
    monkeypatch.setattr(vision_agent, "get_settings", lambda: SimpleNamespace(brain_message_history=False, brain_message_history_steps=8))
    seen: list = []

    class _Client:
        def _call_model_json(self, **kwargs: object) -> dict:
            seen.append(("history" in kwargs, type(kwargs.get("prompt"))))
            return {"ok": True, "data": {"action": "scroll", "direction": "down", "amount": 300, "reason": "Keep reading."}, "metadata": {}}

    client = _Client()
    _stub_loop(monkeypatch, lambda user_id, image, view, context: vision_agent.decide_next_action(client, image, view, context))
    monkeypatch.setattr(extension_run, "_db_event", lambda *args, **kwargs: None)

    asyncio.run(run_vision_loop(_VisionBody(), url=None, narrate=_narrate, run_id=0, user_id=0, skip_navigate=True, max_steps=3))

    assert len(seen) == 3
    assert not any(has_history for has_history, _ in seen)


def test_retained_input_event_records_the_mode_and_turn_count() -> None:
    on = extension_run._model_input_event_payload(
        {"step": 4, "message_history_mode": "on", "message_history_turns": 5, "model_input_path": "x"}
    )
    assert on["message_history_mode"] == "on"
    assert on["message_history_turns"] == 5
    first_step = extension_run._model_input_event_payload({"step": 1, "message_history_mode": "on", "message_history_turns": 0})
    assert first_step["message_history_turns"] == 0  # on, and nothing earlier yet: still recorded
    off = extension_run._model_input_event_payload({"step": 2, "message_history_mode": "off", "message_history_turns": 0})
    assert off["message_history_mode"] == "off"


# --- a wrapped, unknown-kind, converted, or done reply is projected like the action the loop runs ----------------


def test_wrapped_typed_reply_is_projected_like_the_bare_action() -> None:
    raw = {"arguments": {"action": "type", "text": "hunter2-Secret!", "control": 4, "reason": "typing hunter2-Secret!"}}
    text, projected = extension_run._message_history_reply(raw, [_type_prepared(True)])
    assert projected is True
    assert "hunter2" not in text
    assert json.loads(text)["text"] == "[REDACTED]"


def test_wrapped_typed_reply_with_no_prepared_entry_fails_closed() -> None:
    raw = {"tool_input": {"action": "type", "text": "unconfirmed-value", "control": 3}}
    text, projected = extension_run._message_history_reply(raw, [])
    assert projected is True
    assert "unconfirmed-value" not in text


def test_item_whose_kind_cannot_be_determined_redacts_every_text_field() -> None:
    raw = {
        "action": "frobnicate",
        "text": "s1-secret",
        "text_exact": "s1-secret",
        "value": "s1-secret",
        "answer": "s1-secret",
        "content": "s1-secret",
        "key": "s1-secret",
        "reason": "s1-secret",
    }
    text, projected = extension_run._message_history_reply(raw, [])
    assert projected is True
    assert "s1-secret" not in text


def test_text_exact_of_a_sensitive_type_is_projected_out_of_the_reply() -> None:
    raw = {"action": "type", "text": "", "text_exact": "hunter2-Secret!", "control": 4, "reason": "typing it"}
    text, projected = extension_run._message_history_reply(raw, [_type_prepared(True)])
    assert projected is True
    assert "hunter2" not in text
    assert json.loads(text)["text_exact"] == "[REDACTED]"


def test_wrapped_then_item_is_projected_like_its_bare_form() -> None:
    raw = {
        "action": "click",
        "x": 1,
        "y": 1,
        "reason": "Focus the field",
        "then": [{"arguments": {"action": "type", "text": "hunter2-Secret!", "control": 4}}],
    }
    prepared = [{"action": {"action": "click"}, "kind": "click"}, _type_prepared(True)]
    text, projected = extension_run._message_history_reply(raw, prepared)
    assert projected is True
    assert "hunter2" not in text


def test_non_object_then_entry_is_withheld_from_the_turn() -> None:
    raw = {"action": "click", "x": 1, "y": 1, "reason": "Focus", "then": ["hunter2-Secret!"]}
    text, _ = extension_run._message_history_reply(raw, [{"action": {"action": "click"}, "kind": "click"}])
    assert "hunter2" not in text


def test_then_that_is_not_a_list_is_withheld_from_the_turn() -> None:
    raw = {"action": "click", "x": 1, "y": 1, "reason": "Focus", "then": "hunter2-Secret!"}
    text, projected = extension_run._message_history_reply(raw, [{"action": {"action": "click"}, "kind": "click"}])
    assert projected is True
    assert "hunter2" not in text


def test_click_converted_into_a_sensitive_select_option_is_projected_as_its_type() -> None:
    raw = {"action": "click", "x": 1, "y": 1, "text": "05", "reason": "Pick the expiry month"}
    converted = {
        "action": {"action": "type", "text": "05", "control": 4},
        "kind": "type",
        "converted_from": "click",
        "converted_option": "05",
        "sensitive_type": True,
        "bound_control": {"tag": "select", "autocomplete": "cc-exp-month"},
    }
    text, projected = extension_run._message_history_reply(raw, [converted])
    assert projected is True
    assert json.loads(text)["text"] == "[REDACTED]"


def test_click_converted_into_an_ordinary_select_option_keeps_its_text() -> None:
    raw = {"action": "click", "x": 1, "y": 1, "text": "No", "reason": "Answer no"}
    converted = {
        "action": {"action": "type", "text": "No", "control": 4},
        "kind": "type",
        "converted_from": "click",
        "converted_option": "No",
        "sensitive_type": False,
        "bound_control": {"tag": "select"},
    }
    text, projected = extension_run._message_history_reply(raw, [converted])
    assert projected is False
    assert json.loads(text)["text"] == "No"


def test_done_answer_matching_a_typed_secret_is_withheld() -> None:
    raw = {"action": "done", "answer": "hunter2-Secret!"}
    text, projected = extension_run._message_history_reply(
        raw, [{"action": {"action": "done"}, "kind": "done"}], secret_texts=["hunter2-Secret!"]
    )
    assert projected is True
    assert "hunter2" not in text


def test_done_answer_not_matching_a_typed_secret_is_kept() -> None:
    raw = {"action": "done", "answer": "Jane Doe"}
    text, projected = extension_run._message_history_reply(
        raw, [{"action": {"action": "done"}, "kind": "done"}], secret_texts=["hunter2-Secret!"]
    )
    assert projected is False
    assert json.loads(text)["answer"] == "Jane Doe"


def test_fallback_turn_withholds_text_exact_of_a_sensitive_type() -> None:
    trace = {
        "step": 4,
        "surface": "browser",
        "raw_reply": None,
        "actions": [{"action": "type", "text": "hunter2-Secret!", "text_exact": "hunter2-Secret!", "control": 4}],
        "prepared": [_type_prepared(True)],
        "outcomes": {0: {"ok": True, "verified": True}},
    }
    records: list[dict] = []
    extension_run._record_message_history(records, trace, last_reply_text="")
    assert len(records) == 1
    assert "hunter2" not in records[0]["reply"]
