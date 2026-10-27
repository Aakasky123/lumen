"""A brain reply cut off at its output-token cap is recovered within the step.

Live (v5_memory runs 1721/1724): a hedge request routed to a throughput host ran 8,192 tokens of prose to
the cap in ~20 s and won the race against the pinned host; the cut-off reply then counted as a brain
failure and three of them escalated the task.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, replace

import pytest

from app import extension_run, model_hedge, model_provider, vision_agent
from app.extension_run import run_vision_loop
from app.model_hedge import hedged_call
from app.brain_escalation import SELF_RECOVERY_LIMIT
from app.model_provider import (
    _openrouter_routing,
    _salvage_cut_off_json,
    _salvaged_if_cut_off,
    call_model_json,
    reply_cut_off_without_json,
)
from app.vision_agent import SYSTEM_DYNAMIC_BOUNDARY
from tests.test_brain_escalation import _CLICK, _DONE, _resolved_vision_provider, _run_with_escalation
from tests.test_extension_run import (  # noqa: F401 - the autouse fixtures apply to this module too
    _ChoiceBody,
    _DummyContextEngine,
    _complete_final_action_readiness_audit_by_default,
    _ordinary_click_consequence_by_default,
    _ordinary_secret_semantics_by_default,
)
from tests.test_model_provider_transport_receipts import _provider

_ACTION = {"action": "click", "x": 20, "y": 100, "text": "", "reason": "Open the visible choice."}
_ACTION_TEXT = json.dumps(_ACTION)


# --- Salvage: only the one answer the cut-off text already determines ---


def test_a_leading_answer_repeated_until_the_cap_is_salvaged() -> None:
    text = _ACTION_TEXT + "\n" + _ACTION_TEXT + "\n" + _ACTION_TEXT[:30]
    assert _salvage_cut_off_json(text) == _ACTION
    assert _salvaged_if_cut_off({}, "invalid_json", text, "length") == (_ACTION, "json_salvaged")


def test_a_trailing_answer_after_reasoning_prose_is_salvaged() -> None:
    text = "The choice is visible near the top left, so open it.\n" + _ACTION_TEXT + "\nThat opens the li"
    assert _salvage_cut_off_json(text) == _ACTION
    assert _salvaged_if_cut_off({}, "non_json", text, "max_tokens")[1] == "json_salvaged"


@pytest.mark.parametrize(
    "text",
    [
        # Only reasoning prose ran to the cap (25 of the 33 live cut-off replies held no brace pair).
        "Let me think about where the field is. The window shows a list " * 40,
        # A draft and its revision: the text does not say which one is the answer.
        _ACTION_TEXT + " wait, better: " + json.dumps({**_ACTION, "x": 300}),
        # An answer followed by a different object being written when the cap hit.
        _ACTION_TEXT + ' {"action": "type", "text": "hel',
        # A stray brace in the prose.
        "the set {a, b} holds " + _ACTION_TEXT,
        "{}",
    ],
)
def test_nothing_is_salvaged_when_the_text_does_not_determine_one_answer(text: str) -> None:
    assert _salvage_cut_off_json(text) is None


def test_a_reply_that_was_not_cut_off_is_never_salvaged() -> None:
    text = _ACTION_TEXT + "\n" + _ACTION_TEXT
    assert _salvaged_if_cut_off({}, "invalid_json", text, "stop") == ({}, "invalid_json")


def test_a_salvaged_reply_reaches_the_brain_contract_as_an_ordinary_reply(monkeypatch) -> None:
    body = {
        "choices": [{"message": {"content": _ACTION_TEXT + _ACTION_TEXT + "{\"action\": \"cl"}, "finish_reason": "length"}],
        "usage": {"completion_tokens": 128},
    }
    monkeypatch.setattr(model_provider, "_post_openai_compatible_chat", lambda provider, **kwargs: body)

    result = call_model_json(
        _provider("custom_openai_compatible"), purpose="vision_agent_step", system="s", prompt={"goal": "g"}
    )

    assert result["ok"] and result["data"] == _ACTION
    assert result["metadata"]["parse_status"] == "json_salvaged"
    assert not reply_cut_off_without_json(result["metadata"])


class _ClientReturning:
    def __init__(self, data: dict) -> None:
        self.data = data

    def _call_model_json(self, **kwargs) -> dict:
        return {
            "ok": True,
            "data": self.data,
            "metadata": {"parse_status": "json_salvaged", "finish_reason": "length", "raw_output_chars": 900},
        }


def test_a_salvaged_action_goes_through_the_same_validation_as_any_action() -> None:
    view = {"width": 800, "height": 600, "url": "https://example.test", "title": "t", "scroll_y": 0}
    context = {"goal": "Open the choice", "page_evidence": {}}
    image = {"media_type": "image/jpeg", "data": "abc"}

    valid = vision_agent.decide_next_action(_ClientReturning(dict(_ACTION)), image, view, context)
    assert valid["action"] == "click" and not valid.get("invalid_reason")

    # A salvaged object with no usable action is rejected exactly like a whole reply would be.
    invalid = vision_agent.decide_next_action(_ClientReturning({"reason": "no action here"}), image, view, context)
    assert invalid.get("invalid_reason") or invalid.get("_outcome_type") == "schema_invalid"


# --- The hedge: a cut-off reply is not an answer ---


@dataclass(frozen=True)
class _HedgeProvider:
    provider_type: str = "openrouter"
    endpoint_preference: str | None = "wafer"
    endpoint_avoid: str | None = None
    endpoint_max_completion_price: float | None = None
    base_url: str = "https://openrouter.ai/api/v1"
    model_name: str = "deepseek/deepseek-v4.1-flash"


def _cut_off_reply() -> dict:
    return {
        "ok": True,
        "data": {},
        "metadata": {"finish_reason": "length", "parse_status": "non_json", "raw_output_chars": 34778, "token_usage": {}},
    }


@pytest.fixture
def _no_price_lookup(monkeypatch):
    monkeypatch.setattr(model_hedge, "openrouter_endpoint_completion_price", lambda base, model, host: None)


def test_a_cut_off_hedge_reply_does_not_beat_the_pinned_host_still_answering(_no_price_lookup) -> None:
    def call(provider, **kwargs):
        if provider.endpoint_preference == "wafer":
            time.sleep(0.3)
            return {"ok": True, "data": dict(_ACTION), "metadata": {"finish_reason": "stop", "parse_status": "json"}}
        return _cut_off_reply()

    result = hedged_call(call, _HedgeProvider(), hedge_after=0.05, purpose="vision_agent_step")

    assert result["data"] == _ACTION
    assert result["metadata"]["hedge"]["winner"] == "primary"


def test_when_every_reply_is_cut_off_or_failed_the_cut_off_reply_comes_back(_no_price_lookup) -> None:
    def call(provider, **kwargs):
        if provider.endpoint_preference == "wafer":
            time.sleep(0.2)
            return {"ok": False, "error": "upstream 502", "metadata": {}}
        return _cut_off_reply()

    result = hedged_call(call, _HedgeProvider(), hedge_after=0.05, purpose="vision_agent_step")

    # The loop recovers a cut-off reply within the step; a transport failure would only back off.
    assert result["ok"] is True and reply_cut_off_without_json(result["metadata"])
    assert result["metadata"]["hedge"]["winner"] == "hedge"


# --- Routing: the retry skips the host that cut the reply off ---


def test_a_skipped_host_is_ignored_in_every_routing_mode() -> None:
    pinned = replace(_provider("openrouter"), endpoint_preference="wafer", endpoint_skip=("Together",))
    assert _openrouter_routing(pinned) == {"order": ["wafer"], "ignore": ["Together"]}
    hedge = replace(pinned, endpoint_preference=None, endpoint_avoid="wafer")
    assert _openrouter_routing(hedge) == {"ignore": ["wafer", "Together"], "sort": "throughput"}
    # The pinned host itself cut off: it is skipped, not ordered first.
    assert _openrouter_routing(replace(pinned, endpoint_skip=("wafer",))) == {"ignore": ["wafer"]}
    assert _openrouter_routing(replace(_provider("custom_openai_compatible"), endpoint_skip=("Together",))) == {}


# --- The retry directive ---


def test_the_retry_is_told_its_reply_was_cut_off_after_the_cache_boundary() -> None:
    view = {"width": 800, "height": 600, "url": "https://example.test", "title": "t", "scroll_y": 0}
    plain = vision_agent.prepare_model_input(view, {"goal": "g"})
    retry = vision_agent.prepare_model_input(view, {"goal": "g", "reply_cut_off": {"max_tokens": 8192}})

    assert "CUT OFF" not in plain["system"]
    stable, dynamic = retry["system"].split(SYSTEM_DYNAMIC_BOUNDARY, 1)
    assert stable == plain["system"].split(SYSTEM_DYNAMIC_BOUNDARY, 1)[0]
    assert "YOUR PREVIOUS REPLY WAS CUT OFF" in dynamic and "8192 tokens" in dynamic
    assert "JSON action object only" in dynamic
    # The prompt itself is unchanged: the directive is not a page fact.
    assert retry["prompt"] == plain["prompt"]


# --- The loop: recovered within the step, accounted only when unrecovered ---


def _cut_off_decision(host: str = "Together") -> dict:
    return {
        "_outcome_type": "schema_invalid",
        "failure_reason": "provider_non_json",
        "_model_metadata": {
            "provider": "openrouter",
            "model": "deepseek-v4.1-flash",
            "parse_status": "non_json",
            "raw_output_empty": False,
            "raw_output_chars": 34778,
            "finish_reason": "length",
            "max_tokens": 8192,
            "upstream_provider": host,
            "token_usage": {"completion_tokens": 8192, "cost": 0.0134},
        },
    }


def _smart():
    return _resolved_vision_provider(
        502, display_name="GLM 5.3 Flash", provider_type="openrouter", model_name="z-ai/glm-5.3-flash"
    )


def test_a_recovered_cut_off_reply_acts_in_the_same_step_and_is_no_brain_failure(monkeypatch) -> None:
    contexts: list[dict] = []
    summary, decided_by, db_events, body = _run_with_escalation(
        monkeypatch,
        # Three cut-off replies in a row of steps, each recovered by its in-step retry.
        primary_actions=[_cut_off_decision(), _CLICK, _cut_off_decision(), _CLICK, _cut_off_decision(), _CLICK, _DONE],
        smart_actions=[],
        escalation=_smart(),
        contexts=contexts,
    )

    assert summary["state"] == "READY_FOR_REVIEW"
    assert set(decided_by) == {"primary"}
    assert not [event for event, _ in db_events if event == "VISION_BRAIN_ESCALATED"]
    assert not [event for event, _ in db_events if event == "VISION_MODEL_SCHEMA_INVALID"]
    assert len(body.clicks) == 3
    retry = contexts[1]
    assert retry["reply_cut_off"] == {"max_tokens": 8192, "finish_reason": "length"}
    assert retry["_vision_endpoint_skip"] == ["Together"]
    assert "YOUR PREVIOUS REPLY WAS CUT OFF" in retry["_prepared_model_input"]["system"]
    # The retry decides from the very same observation: no re-capture between the two calls.
    assert retry["page_evidence"] == contexts[0]["page_evidence"]
    cut_offs = [payload for event, payload in db_events if event == "VISION_MODEL_REPLY_CUT_OFF"]
    assert [payload["retry_outcome"] for payload in cut_offs] == ["recovered"] * 3
    assert cut_offs[0]["upstream_provider"] == "Together" and cut_offs[0]["raw_output_chars"] == 34778
    assert cut_offs[0]["completion_tokens"] == 8192
    decisions = [payload for event, payload in db_events if event == "VISION_AGENT_DECISION"]
    assert decisions[0]["action"] == "click"
    assert decisions[0]["model_metadata"]["cut_off_retry"]["outcome"] == "recovered"


def test_an_unrecovered_cut_off_still_counts_and_escalates(monkeypatch) -> None:
    summary, decided_by, db_events, body = _run_with_escalation(
        monkeypatch,
        primary_actions=[_cut_off_decision() for _ in range(6)],
        smart_actions=[_CLICK, _DONE],
        escalation=_smart(),
    )

    assert decided_by[:7] == ["primary"] * 6 + ["smart"]
    assert [event for event, _ in db_events].count("VISION_MODEL_SCHEMA_INVALID") == 3
    assert [event for event, _ in db_events if event == "VISION_BRAIN_ESCALATED"]
    cut_offs = [payload for event, payload in db_events if event == "VISION_MODEL_REPLY_CUT_OFF"]
    assert [payload["retry_outcome"] for payload in cut_offs] == ["schema_invalid"] * 3


def test_a_cut_off_retry_is_made_once_per_step_never_again_within_it(monkeypatch) -> None:
    calls: list[dict] = []
    summary, decided_by, db_events, body = _run_with_escalation(
        monkeypatch,
        primary_actions=[_cut_off_decision() for _ in range(20)],
        smart_actions=[],
        escalation=None,
        contexts=calls,
    )

    # Three steps reach the invalid-output ladder's end, then the stop recovers in-loop twice; each
    # step made exactly two calls, and the run ends at the third ladder's end.
    episodes = SELF_RECOVERY_LIMIT + 1
    assert summary["state"] == "BLOCKED" and summary["reason"] == "agent_confused"
    assert len(calls) == 6 * episodes
    assert ["reply_cut_off" in context for context in calls] == [False, True] * (3 * episodes)


def test_a_retry_that_never_answers_leaves_the_cut_off_reply_as_the_outcome(monkeypatch) -> None:
    attempts = {"count": 0}

    def decide(user_id, image, view, context):
        attempts["count"] += 1
        if context.get("reply_cut_off"):
            raise RuntimeError("transport broke")
        return _cut_off_decision()

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr("app.candidate_context.CandidateContextEngine", _DummyContextEngine)
    monkeypatch.setattr(extension_run, "_db_event", lambda run_id, event_type, payload: events.append((event_type, payload)))
    monkeypatch.setattr(extension_run, "_record_context_usage_for_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(extension_run, "_decide_vision_action", decide)

    async def narrate(kind: str, data: dict) -> None:
        pass

    summary = asyncio.run(
        run_vision_loop(_ChoiceBody(), url=None, narrate=narrate, run_id=0, user_id=0, skip_navigate=True, max_steps=12)
    )

    # Every ladder ends in three steps; the stop recovers in-loop twice before the run ends agent_confused.
    episodes = SELF_RECOVERY_LIMIT + 1
    assert summary["reason"] == "agent_confused"
    assert attempts["count"] == 6 * episodes
    assert [event for event, _ in events].count("VISION_MODEL_SCHEMA_INVALID") == 3 * episodes
    assert [payload["retry_outcome"] for event, payload in events if event == "VISION_MODEL_REPLY_CUT_OFF"] == ["error"] * 3 * episodes


def test_a_cut_off_reply_that_starts_a_second_object_is_ambiguous_for_every_caller() -> None:
    # Review: the extractor took the first draft of `{A} wait, that's wrong... {"verdict": "` on a cut-off
    # reply. Judges and gates read json the same way, so an ambiguous cut-off now fails closed.
    from app.model_provider import _json_from_text_with_status, _salvaged_if_cut_off

    draft = '{"verdict": "grounded"} wait, that is wrong. {"verdict": "'
    data, status = _json_from_text_with_status(draft)
    assert status == "json_extracted"
    assert _salvaged_if_cut_off(data, status, draft, "length") == ({}, "invalid_json")
    repeat = '{"verdict": "no"} {"verdict": "no"} {"verd'
    data, status = _json_from_text_with_status(repeat)
    assert _salvaged_if_cut_off(data, status, repeat, "length")[1] in {"json_salvaged", "json_extracted"}
    whole = '{"verdict": "no"} done'
    data, status = _json_from_text_with_status(whole)
    assert _salvaged_if_cut_off(data, status, whole, "length") == (data, "json_extracted"), "no second object: unchanged"
    assert _salvaged_if_cut_off(*_json_from_text_with_status(draft), draft, "stop")[1] == "json_extracted", "only a cut-off reply"
