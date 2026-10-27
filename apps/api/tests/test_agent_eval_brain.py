"""The real brain wired into the runner. CI checks the brain's PLUMBING (parse/clamp/honest offline
fallback) and the suite/dashboard wiring — not its competence. Competence is measured by running this
brain live against the task suite (and ChromeExtensionBody), which is not a CI gate."""

import asyncio

from app.agent_eval.brain import LlmBrain, parse_action
from app.agent_eval.contract import EvalResult, Observation
from app.agent_eval.fixture_body import ScriptedFixtureBody
from app.agent_eval.suite import run_suite, summarize
from app.agent_eval.tasks import ALL_TASKS


class _FakeModel:
    def __init__(self, live: bool, action: dict | None = None) -> None:
        self.live_enabled = live
        self._action = action
        self.last_prompt = None
        self.calls = []

    def _call_model_json(self, *, purpose, system, prompt, schema, provider=None, **kwargs):
        self.last_prompt = prompt
        self.calls.append({"purpose": purpose, "prompt": prompt, "kwargs": kwargs})
        return {"ok": True, "data": self._action} if self._action is not None else {"ok": False}


class _SequenceModel:
    live_enabled = True

    def __init__(self, actions: list[dict]) -> None:
        self.actions = list(actions)
        self.calls = []

    def _call_model_json(self, *, purpose, system, prompt, schema, provider=None, **kwargs):
        self.calls.append({"purpose": purpose, "prompt": prompt, "kwargs": kwargs})
        return {"ok": True, "data": self.actions.pop(0)}


class _Task:
    goal = "Find and download the pricing PDF."


def test_brain_blocks_without_live_model() -> None:
    action = LlmBrain(_FakeModel(live=False))(_Task(), Observation(url="http://x"), [])
    assert action.kind == "blocked"
    assert "no live model" in action.reason.lower()  # honest, never a guessed route


def test_brain_parses_model_action() -> None:
    brain = LlmBrain(_FakeModel(live=True, action={"kind": "click", "target_ref": "apply", "reason": "enter the application"}))
    action = brain(_Task(), Observation(url="http://x", elements=[{"ref": "apply"}]), [])
    assert action.kind == "click"
    assert action.target == {"ref": "apply"}
    assert action.reason


def test_brain_includes_page_text_and_final_answer_value() -> None:
    model = _FakeModel(live=True, action={"kind": "stop", "value": "[6]", "reason": "count is visible"})
    brain = LlmBrain(model)
    action = brain(
        _Task(),
        Observation(
            url="http://x",
            raw={"text": "Reviews dashboard Total matching reviews: 6"},
        ),
        [],
    )
    assert model.last_prompt["page"]["text"] == "Reviews dashboard Total matching reviews: 6"
    assert action.kind == "stop"
    assert action.value == "[6]"


def test_brain_includes_link_hrefs_deeper_in_element_list() -> None:
    elements = [{"ref": f"pw_{idx:03d}", "role": "button", "label": f"Button {idx}"} for idx in range(55)]
    elements[49] = {
        "ref": "pw_049",
        "role": "link",
        "label": "All Reviews",
        "href": "http://localhost:7780/admin/review/product/index/",
        "attributes": {"name": "detail", "type": "text", "required": False},
    }
    model = _FakeModel(live=True, action={"kind": "click", "target_ref": "pw_049", "reason": "open observed reviews link"})
    brain = LlmBrain(model)
    action = brain(_Task(), Observation(url="http://x", elements=elements), [])
    compact = model.last_prompt["page"]["elements"]
    assert any(
        element.get("ref") == "pw_049"
        and element.get("href")
        and element.get("attributes", {}).get("name") == "detail"
        for element in compact
    )
    assert action.target == {"ref": "pw_049"}


def test_brain_keeps_late_dialog_controls_in_prompt() -> None:
    elements = [{"ref": f"pw_{idx:03d}", "role": "link", "label": f"Nav {idx}"} for idx in range(160)]
    elements[-2] = {"ref": "pw_158", "role": "button", "label": "Necessary Cookies Only"}
    elements[-1] = {"ref": "pw_159", "role": "button", "label": "Close"}
    model = _FakeModel(live=True, action={"kind": "click", "target_ref": "pw_158", "reason": "dismiss cookie banner"})
    action = LlmBrain(model)(_Task(), Observation(url="http://x", elements=elements), [])
    refs = {element.get("ref") for element in model.last_prompt["page"]["elements"]}
    assert "pw_158" in refs
    assert "pw_159" in refs
    assert action.target == {"ref": "pw_158"}


def test_brain_includes_recent_execution_errors_for_recovery() -> None:
    receipt = {
        "type": "click",
        "target": {"ref": "search"},
        "outcome": "execution_failed",
        "progress": "no_change",
        "execution": {"status": "failed", "error": "modal intercepts pointer events"},
    }
    model = _FakeModel(live=True, action={"kind": "click", "target_ref": "close", "reason": "dismiss modal"})
    LlmBrain(model)(_Task(), Observation(url="http://x", elements=[{"ref": "close", "role": "button", "label": "Close"}]), [receipt])
    assert model.last_prompt["recent_results"][0]["error"] == "modal intercepts pointer events"


def test_brain_normalizes_click_with_text_value_on_editable_target_to_type() -> None:
    model = _FakeModel(
        live=True,
        action={"kind": "click", "target_ref": "search", "value": "LED light bulbs", "reason": "search for the target"},
    )
    action = LlmBrain(model)(
        _Task(),
        Observation(url="http://x", elements=[{"ref": "search", "role": "textbox", "label": "Search"}]),
        [],
    )
    assert action.kind == "type"
    assert action.target == {"ref": "search"}
    assert action.value == "LED light bulbs"


def test_brain_repairs_blank_blocked_action_to_next_action() -> None:
    model = _SequenceModel(
        [
            {"kind": "blocked", "reason": ""},
            {"kind": "click", "target_ref": "search", "reason": "use visible search"},
        ]
    )
    action = LlmBrain(model)(
        _Task(),
        Observation(url="http://x", elements=[{"ref": "search", "role": "button", "label": "Search"}]),
        [],
    )
    assert action.kind == "click"
    assert action.target == {"ref": "search"}
    assert [call["purpose"] for call in model.calls] == ["browser_agent_action", "browser_agent_action_repair"]
    assert model.calls[1]["prompt"]["invalid_previous_action"]["kind"] == "blocked"


def test_brain_repairs_blank_blocked_action_to_grounded_block() -> None:
    model = _SequenceModel(
        [
            {"kind": "blocked", "reason": ""},
            {"kind": "blocked", "reason": "Cloudflare security verification blocks the page"},
        ]
    )
    action = LlmBrain(model)(_Task(), Observation(url="http://x", raw={"text": "Performing security verification"}), [])
    assert action.kind == "blocked"
    assert "Cloudflare" in action.reason


def test_brain_repairs_blocked_action_with_target_as_malformed() -> None:
    model = _SequenceModel(
        [
            {"kind": "blocked", "target_ref": "search", "value": "Alberta Job Grant eligibility criteria", "reason": ""},
            {"kind": "type", "target_ref": "search", "value": "Alberta Job Grant eligibility criteria", "reason": "search the visible site search"},
        ]
    )
    action = LlmBrain(model)(
        _Task(),
        Observation(url="http://x", elements=[{"ref": "search", "role": "textbox", "label": "Search"}]),
        [],
    )
    assert action.kind == "type"
    assert action.target == {"ref": "search"}
    assert action.value == "Alberta Job Grant eligibility criteria"


def test_brain_repairs_repeated_screenshot_request() -> None:
    model = _SequenceModel(
        [
            {"kind": "screenshot", "reason": "need to see it again"},
            {"kind": "click", "target_ref": "refunds", "reason": "click the visible target"},
        ]
    )
    action = LlmBrain(model)(
        _Task(),
        Observation(
            url="http://x",
            elements=[{"ref": "refunds", "role": "button", "label": "Refunds"}],
            image="base64-screenshot",
        ),
        [],
    )
    assert action.kind == "click"
    assert action.target == {"ref": "refunds"}
    assert [call["purpose"] for call in model.calls] == ["browser_agent_action", "browser_agent_action_repair"]
    assert model.calls[1]["prompt"]["invalid_previous_action"]["kind"] == "screenshot"


def test_brain_retries_repeated_screenshot_repair_once() -> None:
    model = _SequenceModel(
        [
            {"kind": "screenshot", "reason": "need to see it again"},
            {"kind": "screenshot", "reason": "still need to see it"},
            {"kind": "click", "target_ref": "refunds", "reason": "click the visible target"},
        ]
    )
    action = LlmBrain(model)(
        _Task(),
        Observation(
            url="http://x",
            elements=[{"ref": "refunds", "role": "button", "label": "Refunds"}],
            image="base64-screenshot",
        ),
        [],
    )
    assert action.kind == "click"
    assert [call["purpose"] for call in model.calls] == [
        "browser_agent_action",
        "browser_agent_action_repair",
        "browser_agent_action_repair",
    ]
    assert model.calls[1]["prompt"]["invalid_previous_action"]["allowed_kinds_this_turn"]
    assert "screenshot" not in model.calls[1]["prompt"]["invalid_previous_action"]["allowed_kinds_this_turn"]


def test_brain_blocks_after_repeated_invalid_screenshot_repairs() -> None:
    model = _SequenceModel(
        [
            {"kind": "screenshot", "reason": "need to see it again"},
            {"kind": "screenshot", "reason": "still need to see it"},
            {"kind": "screenshot", "reason": "again"},
        ]
    )
    action = LlmBrain(model)(
        _Task(),
        Observation(url="http://x", elements=[{"ref": "continue", "role": "button", "label": "Continue"}], image="base64-screenshot"),
        [],
    )
    assert action.kind == "blocked"
    assert "repeated invalid screenshot" in action.reason


def test_brain_blocks_when_model_unavailable_this_step() -> None:
    action = LlmBrain(_FakeModel(live=True, action=None))(_Task(), Observation(), [])
    assert action.kind == "blocked"


def test_parse_action_clamps_unknown_kind() -> None:
    assert parse_action({"kind": "teleport", "reason": "nope"}).kind == "blocked"
    assert parse_action({"kind": "navigate", "value": "http://x", "reason": "go"}).value == "http://x"


def test_parse_action_accepts_provider_action_wrapper() -> None:
    action = parse_action({"kind": "action", "action": "fill", "target_ref": "name", "value": "Lumen User"})
    assert action.kind == "type"
    assert action.target == {"ref": "name"}
    assert action.value == "Lumen User"


def test_parse_action_unwraps_nested_tool_arguments() -> None:
    action = parse_action({"arguments": {"kind": "action", "action": "click", "target_ref": "continue"}})
    assert action.kind == "click"
    assert action.target == {"ref": "continue"}


def test_parse_action_unwraps_nested_action_object() -> None:
    action = parse_action(
        {
            "analysis": "Need to type into the visible field.",
            "action": {
                "kind": "browser_action",
                "action": "type",
                "target_ref": "addr",
                "value": "123 Main St",
            },
        }
    )
    assert action.kind == "type"
    assert action.target == {"ref": "addr"}
    assert action.value == "123 Main St"


def test_parse_action_uses_url_target_ref_as_navigation_value() -> None:
    action = parse_action({"kind": "navigate", "target_ref": "https://example.com/pointsmax", "reason": "open page"})
    assert action.kind == "navigate"
    assert action.target is None
    assert action.value == "https://example.com/pointsmax"


def test_run_suite_with_offline_brain_reports_zero_verified() -> None:
    # the suite/dashboard wiring works AND an offline brain honestly verifies nothing (no fake success)
    out = asyncio.run(run_suite(ALL_TASKS, lambda task: ScriptedFixtureBody(), LlmBrain(_FakeModel(live=False))))
    summary = out["summary"]
    assert summary["overall"]["total"] == len(ALL_TASKS)
    assert summary["overall"]["verified_success"] == 0
    assert summary["overall"]["fabrications"] == 0  # blocking is not a fabricated success
    assert set(summary["by_family"]) == {t.family for t in ALL_TASKS}


def test_summarize_counts_families() -> None:
    results = [
        EvalResult(task_id="a", family="downloads", task_success=True, verified_success=True),
        EvalResult(task_id="b", family="downloads", task_success=True, verified_success=False, unsupported_success_claims=1),
        EvalResult(task_id="c", family="dialogs", task_success=False, verified_success=False),
    ]
    summary = summarize(results)
    assert summary["overall"] == {"total": 3, "task_success": 2, "verified_success": 1, "fabrications": 1, "forbidden": 0}
    assert summary["by_family"]["downloads"]["verified_success"] == 1
