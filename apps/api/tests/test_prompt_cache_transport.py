"""Prompt-cache prefix contract: stable system bytes cache across steps on every
provider; per-step dynamic directives never invalidate the cached rules block."""

import json

import pytest

from app.model_provider import PAGE_RENDERED_PROMPT_KEYS, ResolvedModelProvider, call_model_json
from app.prompt_cache import (
    PROMPT_CACHE_STABLE_KEYS,
    PROMPT_CACHE_VOLATILE_KEYS,
    SYSTEM_DYNAMIC_BOUNDARY,
    order_prompt_for_cache,
    prompt_wire_parts,
    split_system_cache_prefix,
    system_wire_text,
    user_wire_parts,
)
from app.vision_agent import AGENT_SYSTEM, prepare_model_input


class _FakeResponse:
    def __init__(self, body: dict):
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._body


def _provider(provider_type: str, model_name: str = "transport-test-model") -> ResolvedModelProvider:
    return ResolvedModelProvider(
        id=74,
        display_name=f"{provider_type} prompt cache test",
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


_RESPONSE_BODIES = {
    "anthropic": {"content": [{"type": "text", "text": "{}"}], "usage": {}},
    "openai": {"output_text": "{}", "usage": {}},
    "custom_openai_compatible": {"choices": [{"message": {"content": "{}"}}], "usage": {}},
    "gemini": {"candidates": [{"content": {"parts": [{"text": "{}"}]}}], "usageMetadata": {}},
}


def _call(monkeypatch, provider: ResolvedModelProvider, system: str, prompt: dict | None = None, image=None) -> dict:
    posted: dict = {}

    def fake_post(url, **kwargs):
        posted["json"] = kwargs["json"]
        return _FakeResponse(_RESPONSE_BODIES[provider.provider_type])

    monkeypatch.setattr("app.model_provider._http_post", fake_post)
    result = call_model_json(
        provider,
        purpose="prompt_cache_test",
        system=system,
        prompt=prompt if prompt is not None else {"goal": "Inspect the page."},
        image=image,
    )
    assert result["ok"] is True
    return posted["json"]


_IMAGE = {"media_type": "image/jpeg", "data": "aGVsbG8="}


def _packet(step: int) -> dict:
    """A packet as prepare_model_input orders it: run-stable keys, then per-step keys."""
    return order_prompt_for_cache(
        {
            "recent_actions": [{"step": step, "action": "click"}],
            "goal": "Reconcile the invoices.",
            "page_evidence": {"viewport_text": f"Invoice {step} of 9"},
            "your_notes": ["step 2: depot code DP-6463"],
            "desktop_tools": {"applications": [{"app_id": "app:1", "name": "Notepad"}]},
            "verified_receipts": [{"type": "click", "step": step}],
        }
    )


def _user_text(body: dict) -> str:
    content = body["messages"][-1]["content"]
    if isinstance(content, str):
        return content
    return "".join(part.get("text", "") for part in content if part.get("type") == "text")


def test_split_system_cache_prefix_round_trips_bytes() -> None:
    stable = "STABLE RULES"
    dynamic = "\n\nREPAIR: fix it"
    combined = stable + SYSTEM_DYNAMIC_BOUNDARY + dynamic
    assert split_system_cache_prefix(combined) == (stable, dynamic)
    assert split_system_cache_prefix(stable) == (stable, "")
    assert system_wire_text(combined) == stable + dynamic
    assert system_wire_text(stable) == stable


def test_anthropic_system_is_cached_block(monkeypatch) -> None:
    body = _call(monkeypatch, _provider("anthropic"), "STABLE RULES")
    assert body["system"] == [
        {"type": "text", "text": "STABLE RULES", "cache_control": {"type": "ephemeral"}}
    ]


def test_anthropic_breakpoints_sit_after_the_rules_and_after_the_stable_packet(monkeypatch) -> None:
    system = "STABLE RULES" + SYSTEM_DYNAMIC_BOUNDARY + "\n\nREPAIR: fix the tool call"
    body = _call(monkeypatch, _provider("anthropic"), system, _packet(3), image=_IMAGE)
    assert body["system"] == [{"type": "text", "text": "STABLE RULES", "cache_control": {"type": "ephemeral"}}]
    stable_block, tail_block, image_block = body["messages"][0]["content"]
    assert stable_block["cache_control"] == {"type": "ephemeral"}
    assert '"goal"' in stable_block["text"] and '"your_notes"' in stable_block["text"]
    assert '"recent_actions"' not in stable_block["text"]
    assert "cache_control" not in tail_block
    assert tail_block["text"].endswith("\n\nREPAIR: fix the tool call")
    assert image_block["type"] == "image"
    # The two text blocks read as the packet followed by the directives.
    assert json.loads(stable_block["text"] + tail_block["text"].split("\n\nREPAIR")[0]) == _packet(3)
    assert SYSTEM_DYNAMIC_BOUNDARY not in json.dumps(body)


@pytest.mark.parametrize("provider_type", ["openai", "gemini", "custom_openai_compatible"])
def test_dynamic_directives_ride_after_the_packet_not_in_the_system_message(monkeypatch, provider_type: str) -> None:
    system = "STABLE JSON RULES" + SYSTEM_DYNAMIC_BOUNDARY + "\n\nREPAIR: fix the tool call"
    body = _call(monkeypatch, _provider(provider_type), system, _packet(3))
    serialized = json.dumps(body)
    assert SYSTEM_DYNAMIC_BOUNDARY not in serialized
    if provider_type == "gemini":
        system_text = body["systemInstruction"]["parts"][0]["text"]
        user_text = body["contents"][0]["parts"][0]["text"]
    elif provider_type == "openai":
        system_text = body["input"][0]["content"]
        user_text = body["input"][1]["content"]
    else:
        assert [m["role"] for m in body["messages"]] == ["system", "user"]
        system_text = body["messages"][0]["content"]
        user_text = _user_text(body)
    assert system_text == "STABLE JSON RULES"
    assert user_text == json.dumps(_packet(3)) + "\n\nREPAIR: fix the tool call"


def test_consecutive_steps_share_the_system_and_the_stable_packet_as_one_prefix(monkeypatch) -> None:
    provider = _provider("custom_openai_compatible")
    first = _call(monkeypatch, provider, "STABLE RULES", _packet(3), image=_IMAGE)
    second = _call(
        monkeypatch,
        provider,
        "STABLE RULES" + SYSTEM_DYNAMIC_BOUNDARY + "\n\nLAST ACTION FEEDBACK: the page changed",
        _packet(4),
        image=_IMAGE,
    )
    assert first["messages"][0] == second["messages"][0]
    stable_text, _rest = prompt_wire_parts(_packet(4))
    assert _user_text(first).startswith(stable_text)
    assert _user_text(second).startswith(stable_text)
    assert '"your_notes"' in stable_text and '"page_evidence"' not in stable_text
    # The screenshot follows all of the text.
    assert second["messages"][-1]["content"][-1]["type"] == "image_url"


def test_chat_json_note_rides_at_the_end_of_the_user_text(monkeypatch) -> None:
    body = _call(monkeypatch, _provider("custom_openai_compatible"), "RULES", {"goal": "g"})
    assert body["messages"][0] == {"role": "system", "content": "RULES"}
    assert _user_text(body) == json.dumps({"goal": "g"}) + "\nReturn a single valid JSON object."


def test_prompt_wire_parts_concatenate_to_the_exact_serialization() -> None:
    for prompt in (
        _packet(1),
        {"goal": "g"},
        {"recent_actions": []},
        {},
        {"goal": "g", "page_evidence": {"a": 1}, "your_notes": ["late stable key after a volatile one"]},
    ):
        stable, rest = prompt_wire_parts(prompt)
        assert stable + rest == json.dumps(prompt, default=str)
    stable, rest = prompt_wire_parts({"goal": "g", "page_evidence": {"a": 1}, "your_notes": ["n"]})
    # Only the packet's leading run of stable keys is the cacheable part.
    assert stable == '{"goal": "g", ' and rest.startswith('"page_evidence"')
    assert prompt_wire_parts({"page_evidence": 1}) == ("", '{"page_evidence": 1}')
    assert user_wire_parts("S" + SYSTEM_DYNAMIC_BOUNDARY + "D", {"goal": "g"}, "!") == ('{"goal": "g"}', "D!")


def test_order_prompt_for_cache_reorders_without_losing_anything() -> None:
    prompt = {
        "recent_actions": [1],
        "a_new_key": "x",
        "control_inventory": [2],
        "your_plan": "p",
        "goal": "g",
        "desktop_tools": {"applications": []},
    }
    ordered = order_prompt_for_cache(prompt)
    assert ordered == prompt
    assert list(ordered) == ["goal", "desktop_tools", "your_plan", "a_new_key", "control_inventory", "recent_actions"]
    assert not set(PROMPT_CACHE_STABLE_KEYS) & set(PROMPT_CACHE_VOLATILE_KEYS)
    # Retention and transport receipts hash the packet with sorted keys, so the order is invisible to them.
    from app.model_provider import _canonical_payload_sha256

    assert _canonical_payload_sha256(ordered) == _canonical_payload_sha256(prompt)


def test_prepare_model_input_packet_leads_with_stable_keys_and_serializes_deterministically() -> None:
    context = {
        "goal": "g",
        "execution_surface": "desktop",
        "desktop_tools": {
            "execution_surface": "desktop",
            "windows": [{"window_id": "hwnd:1", "title": "Notepad"}],
            "applications": [{"app_id": "app:1", "name": "Notepad"}],
            "tool_targets": {"focus_window": "Use an exact window_id from windows."},
        },
        "your_notes": ["n1"],
        "recent_actions": [{"step": 1}],
        "verified_receipts": [{"type": "click"}],
    }
    first = prepare_model_input({"width": 100, "height": 100}, dict(context))["prompt"]
    again = prepare_model_input({"width": 100, "height": 100}, dict(context))["prompt"]
    assert json.dumps(first, default=str) == json.dumps(again, default=str)
    keys = list(first)
    stable_run = [key for key in keys if key in PROMPT_CACHE_STABLE_KEYS]
    assert keys[: len(stable_run)] == stable_run
    assert keys[0] == "goal" and keys[-1] == "recent_actions"
    # The window list, which changes whenever a window moves, is its own per-step key, not part of the manifest.
    assert "windows" not in first["desktop_tools"]
    assert first["desktop_windows"] == {"windows": [{"window_id": "hwnd:1", "title": "Notepad"}]}
    assert keys.index("desktop_windows") > keys.index("your_plan")
    assert first["desktop_tools"]["applications"] == [{"app_id": "app:1", "name": "Notepad"}]


def test_transport_scrubbing_still_applies_per_key_in_the_ordered_packet(monkeypatch) -> None:
    # The goal is not page-rendered: a code in it is scrubbed even with a screenshot attached; the
    # page evidence the screenshot shows keeps its code.
    assert "goal" not in PAGE_RENDERED_PROMPT_KEYS and "page_evidence" in PAGE_RENDERED_PROMPT_KEYS
    prompt = order_prompt_for_cache(
        {
            "page_evidence": {"viewport_text": "Your verification code is 482913"},
            "goal": "Enter verification code 482913 on the page.",
        }
    )
    body = _call(monkeypatch, _provider("custom_openai_compatible", "vision-model"), "RULES json", prompt, image=_IMAGE)
    user_text = _user_text(body)
    goal_text, page_text = user_text.split('"page_evidence"', 1)
    assert "482913" not in goal_text
    assert "482913" in page_text


def test_anthropic_temperature_kept_on_models_that_accept_it(monkeypatch) -> None:
    body = _call(monkeypatch, _provider("anthropic", "claude-sonnet-4-6"), "RULES")
    assert body["temperature"] == 0.0


@pytest.mark.parametrize(
    "model_name",
    ["claude-opus-4-7", "claude-opus-4-8", "claude-opus-5", "claude-sonnet-5", "claude-fable-5"],
)
def test_anthropic_temperature_omitted_where_rejected(monkeypatch, model_name: str) -> None:
    body = _call(monkeypatch, _provider("anthropic", model_name), "RULES")
    assert "temperature" not in body


def test_prepare_model_input_keeps_stable_system_without_directives() -> None:
    prepared = prepare_model_input({"width": 100, "height": 100}, {"goal": "g"})
    # The run-stable instruction block rides in the cacheable system prefix, not the per-step prompt.
    assert prepared["system"].startswith(AGENT_SYSTEM)
    assert "choose the SINGLE next action" in prepared["system"]
    assert "instruction" not in prepared["prompt"]
    assert SYSTEM_DYNAMIC_BOUNDARY not in prepared["system"]


def test_prepare_model_input_puts_repair_directives_after_boundary() -> None:
    prepared = prepare_model_input(
        {"width": 100, "height": 100},
        {
            "goal": "g",
            "required_action_repair": "return a schema-valid click",
            "recovery_constraints": [{"rule": "withhold_exact_fingerprint"}],
        },
    )
    stable, dynamic = split_system_cache_prefix(prepared["system"])
    assert stable.startswith(AGENT_SYSTEM)
    assert stable == prepare_model_input({"width": 100, "height": 100}, {"goal": "g"})["system"]
    assert "TOOL CALL REPAIR REQUIRED" in dynamic
    assert "MUTATION CONSTRAINT" in dynamic
    assert "TOOL CALL REPAIR REQUIRED" not in stable


def test_the_output_key_names_ride_in_the_cached_prefix_and_match_the_schema() -> None:
    # A json_object provider never receives the schema, so the stable prefix names its exact keys.
    prepared = prepare_model_input(
        {"width": 100, "height": 100},
        {"goal": "g", "required_action_repair": "return a schema-valid click"},
    )
    stable, _dynamic = split_system_cache_prefix(prepared["system"])
    properties = prepared["schema"]["schema"]["properties"]
    key_line = next(line for line in stable.splitlines() if line.startswith("Output one JSON object."))
    assert 'Required keys: "action"' in key_line
    for action in properties["action"]["enum"]:
        assert action in key_line
    named = key_line.split("named exactly as: ", 1)[1].split(". Omit", 1)[0].split(", ")
    assert set(named) == set(properties) - {"action", "reason"}


def test_openrouter_claude_gets_explicit_breakpoints_and_other_models_stay_plain(monkeypatch) -> None:
    monkeypatch.setitem(_RESPONSE_BODIES, "openrouter", _RESPONSE_BODIES["custom_openai_compatible"])
    monkeypatch.setattr("app.model_provider._openrouter_declines_temperature", lambda *_args: False)
    claude = _call(monkeypatch, _provider("openrouter", "anthropic/claude-sonnet-4.6"), "RULES json", _packet(3), image=_IMAGE)
    system_message, user_message = claude["messages"]
    assert system_message["content"] == [{"type": "text", "text": "RULES json", "cache_control": {"type": "ephemeral"}}]
    stable_part, tail_part, image_part = user_message["content"]
    assert stable_part["cache_control"] == {"type": "ephemeral"} and '"your_notes"' in stable_part["text"]
    assert "cache_control" not in tail_part and image_part["type"] == "image_url"
    deepseek = _call(monkeypatch, _provider("openrouter", "deepseek/deepseek-v4.1-flash"), "RULES json", _packet(3), image=_IMAGE)
    assert deepseek["messages"][0] == {"role": "system", "content": "RULES json"}
    assert "cache_control" not in json.dumps(deepseek)
    assert _user_text(deepseek) == json.dumps(_packet(3))
