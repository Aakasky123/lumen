import pytest

from app.agent_eval.contract import BrowserAction
from app.agent_eval.webbench_runner import (
    WebBenchTaskRun,
    _invalid_empty_read_stop_receipt,
    score_webbench_runs,
    select_webbench_tasks,
)


def test_select_webbench_tasks_defaults_to_read_rows_and_limit() -> None:
    tasks = select_webbench_tasks(
        [
            {
                "benchmark": "webbench",
                "external_id": "1",
                "operation": "READ",
                "goal": "Find the price",
                "start_urls": ["https://example.com"],
            },
            {
                "benchmark": "webbench",
                "external_id": "2",
                "operation": "WRITE",
                "goal": "Create a list",
                "start_urls": ["https://example.com"],
            },
            {
                "benchmark": "webvoyager",
                "external_id": "3",
                "operation": "live_web_task",
                "goal": "Find a recipe",
                "start_urls": ["https://example.org"],
            },
            {
                "benchmark": "webbench",
                "external_id": "4",
                "operation": "READ",
                "goal": "Find the hours",
                "start_urls": ["https://example.net"],
            },
        ],
        limit=1,
    )
    assert [task["external_id"] for task in tasks] == ["1"]


def test_select_webbench_tasks_includes_write_only_when_explicit() -> None:
    tasks = select_webbench_tasks(
        [
            {"benchmark": "webbench", "external_id": "1", "operation": "READ"},
            {"benchmark": "webbench", "external_id": "2", "operation": "WRITE"},
        ],
        allow_write=True,
    )
    assert [task["external_id"] for task in tasks] == ["1", "2"]


def test_score_webbench_runs_reports_completion_not_official_accuracy() -> None:
    runs = [
        WebBenchTaskRun(
            task_id="1",
            operation="READ",
            start_url="https://example.com",
            final_state="success",
            completed=True,
            answer="$19.99",
            steps=4,
            error=None,
            submitted_forms=[],
            downloads=[],
            uploads=[],
            output_dir="out/1",
            trace_file="out/1/network.har",
        ),
        WebBenchTaskRun(
            task_id="2",
            operation="READ",
            start_url="https://example.org",
            final_state="blocked",
            completed=False,
            answer=None,
            steps=2,
            error="CAPTCHA",
            submitted_forms=[],
            downloads=[],
            uploads=[],
            output_dir="out/2",
            trace_file="out/2/network.har",
        ),
    ]
    summary = score_webbench_runs(runs)
    assert summary["attempted"] == 2
    assert summary["completed"] == 1
    assert summary["completion_score"] == 0.5
    assert summary["by_state"] == {"success": 1, "blocked": 1}
    assert "not official WebBench accuracy" in summary["note"]


def test_invalid_empty_read_stop_receipt_tells_model_to_answer_or_block() -> None:
    receipt = _invalid_empty_read_stop_receipt(BrowserAction(kind="stop", reason="blocked by verification"))
    assert receipt["outcome"] == "invalid_action"
    assert receipt["progress"] == "no_change"
    assert "stop.value" in receipt["execution"]["error"]
    assert "Use blocked" in receipt["execution"]["error"]


def test_resolve_eval_body_defaults_to_the_production_chrome_body(monkeypatch) -> None:
    """The benchmark default is the REAL body. With no extension connected the error
    names the remedy instead of silently substituting a second runtime."""
    from app.agent_eval.chrome_body import ChromeExtensionBody, resolve_eval_body
    from app.extension_hub import hub

    monkeypatch.setattr(hub, "get", lambda device_id=None: None)
    with pytest.raises(RuntimeError, match="extension connected"):
        resolve_eval_body("chrome")

    class _Connection:
        device_id = "dev-test"

    monkeypatch.setattr(hub, "get", lambda device_id=None: _Connection())
    body = resolve_eval_body("chrome")
    assert isinstance(body, ChromeExtensionBody)

    with pytest.raises(ValueError, match="expected 'chrome' or 'playwright'"):
        resolve_eval_body("selenium")


def test_playwright_body_remains_an_explicit_disabled_opt_in() -> None:
    import asyncio

    from app.agent_eval.chrome_body import resolve_eval_body
    from app.agent_eval.contract import EvalTask
    from app.agent_eval.playwright_body import PlaywrightEvalBody

    body = resolve_eval_body("playwright")
    assert isinstance(body, PlaywrightEvalBody)
    with pytest.raises(RuntimeError, match="disabled"):
        asyncio.run(body.reset(EvalTask(id="t", goal="g", initial_url="about:blank", family="f", max_steps=1)))


def test_benchmark_run_context_records_commit_and_models() -> None:
    from app.agent_eval.scorecard import benchmark_run_context

    context = benchmark_run_context(benchmark="webbench", parameters={"body_kind": "chrome", "max_steps": 5})
    assert context["benchmark"] == "webbench"
    assert context["parameters"]["body_kind"] == "chrome"
    assert context["git_commit"], "results without a commit are not comparable to anything"
    assert "model" in context


def test_chrome_eval_body_projects_nodes_and_binds_targets_exactly() -> None:
    """The eval brain reads flat ref/role/label; production nodes nest them. The body
    projects deterministically and binds the brain's target to an enumerated selector -
    by exact ref, else by exactly-one exact label match - or fails honestly."""
    from app.agent_eval.chrome_body import ChromeExtensionBody, _eval_element

    node = {
        "element_ref": {"selector": "#cookie-ok", "role": "button", "label": "Accept"},
        "control_kind": "button",
        "role": "button",
        "resolved_label": "Accept",
        "checked": False,
        "disabled": False,
        "value": "",
    }
    flat = _eval_element(node)
    assert flat["ref"] == "#cookie-ok"
    assert flat["role"] == "button"
    assert flat["label"] == "Accept"
    assert "checked" not in flat, "false booleans are noise, not evidence"

    body = ChromeExtensionBody(body=None)
    body._elements = [
        {"ref": "#cookie-ok", "role": "button", "label": "Accept"},
        {"ref": "#show-refunds", "role": "button", "label": "Refunds"},
    ]

    assert body._bind_target({"ref": "#cookie-ok"}) == ("#cookie-ok", "")
    # The exact label the model saw binds to the one element carrying it.
    assert body._bind_target({"ref": "Accept"}) == ("#cookie-ok", "")
    assert body._bind_target({"ref": "  accept "}) == ("#cookie-ok", "")
    assert body._bind_target({"ref": "Nonexistent"}) == ("", "target_not_in_enumerated_elements")
    assert body._bind_target({}) == ("", "missing_target_ref")

    body._elements.append({"ref": "#other-accept", "role": "button", "label": "Accept"})
    assert body._bind_target({"ref": "Accept"}) == ("", "ambiguous_target_label")


def test_chrome_eval_body_absorbs_transient_frame_teardown() -> None:
    """A frame torn down mid-command is routine on redirect-heavy live sites. Observation
    retries once (read-only); an action is NEVER blind-retried - it reports honestly so
    the loop re-observes and re-decides. Non-transient errors still raise."""
    import asyncio

    from app.agent_eval.chrome_body import ChromeExtensionBody
    from app.agent_eval.contract import BrowserAction

    class _FlakyBody:
        def __init__(self) -> None:
            self.observe_calls = 0

        async def observe(self):
            self.observe_calls += 1
            if self.observe_calls == 1:
                raise RuntimeError("Frame with ID 0 was removed.")
            return {"url": "https://example.com", "title": "ok", "nodes": []}

        async def execute(self, intent):
            raise RuntimeError("Frame with ID 4 was removed.")

    flaky = _FlakyBody()
    body = ChromeExtensionBody(flaky)
    observation = asyncio.run(body.observe())
    assert observation.url == "https://example.com"
    assert flaky.observe_calls == 2

    body._elements = [{"ref": "#go", "role": "button", "label": "Go"}]
    receipt = asyncio.run(body.execute(BrowserAction(kind="click", target={"ref": "#go"})))
    assert receipt["execution"]["status"] == "failed"
    assert "transient_page_teardown" in receipt["execution"]["error"]
    assert "Re-observe" in receipt["execution"]["error"]

    class _BrokenBody:
        async def observe(self):
            raise RuntimeError("extension not ready for live actions")

    with pytest.raises(RuntimeError, match="not ready"):
        asyncio.run(ChromeExtensionBody(_BrokenBody()).observe())


def test_chrome_eval_body_presses_keys_via_production_vision_key() -> None:
    """kind='key' submits at the focused control (Enter after a type) through the same
    vision_key machinery the production loop uses - typing alone submits nothing."""
    import asyncio

    from app.agent_eval.chrome_body import ChromeExtensionBody
    from app.agent_eval.contract import BrowserAction

    class _KeyBody:
        def __init__(self) -> None:
            self.pressed: list[str] = []

        async def vision_key(self, key):
            self.pressed.append(key)
            return {"ok": True, "surface_changed": True}

        async def observe(self):
            return {"url": "https://example.com/results", "title": "results", "nodes": []}

    fake = _KeyBody()
    body = ChromeExtensionBody(fake)
    receipt = asyncio.run(body.execute(BrowserAction(kind="key", value="Enter")))
    assert fake.pressed == ["Enter"]
    assert receipt["execution"]["status"] == "succeeded"

    # Default and whitespace values degrade to Enter, the overwhelmingly common submit.
    asyncio.run(body.execute(BrowserAction(kind="key", value="  ")))
    assert fake.pressed == ["Enter", "Enter"]
