import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from app import extension_run, model_call_ledger, model_provider, run_prewarm
from app.judge_cache import judge_wire_parts
from app.model_provider import ResolvedModelProvider, call_model_json
from app.prompt_cache import SYSTEM_DYNAMIC_BOUNDARY


class _Response:
    def __init__(self, body: dict | None = None):
        self._body = body or {}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._body


def _provider(provider_type: str, model: str = "claude-sonnet-4-5", **kwargs) -> ResolvedModelProvider:
    return ResolvedModelProvider(
        id=1,
        display_name="t",
        provider_type=provider_type,
        model_name=model,
        base_url=f"https://{provider_type}.example.test/v1",
        api_key="test-only-key",
        temperature=0.0,
        max_tokens=512,
        timeout_seconds=30,
        enabled=True,
        is_active=True,
        source="database",
        **kwargs,
    )


_LONG_RULES = [f"Rule {index}: compare the proposed value against the entry text and be strict about it." for index in range(120)]


# --- shared pooled client ------------------------------------------------------------------


def test_openrouter_catalog_and_pricing_use_the_shared_client(monkeypatch) -> None:
    calls: list[str] = []

    class FakeClient:
        def get(self, url, **kwargs):
            calls.append(url)
            return _Response({"data": []})

    def stray(*args, **kwargs):
        raise AssertionError("a lookup left the shared client")

    monkeypatch.setattr(model_provider, "_shared_http_client", lambda: FakeClient())
    monkeypatch.setattr(model_provider.httpx, "get", stray)
    monkeypatch.setattr(model_provider, "_OPENROUTER_MODEL_CONTEXT", None)
    monkeypatch.setattr(model_provider, "_OPENROUTER_ENDPOINT_PRICES", {})

    model_provider._openrouter_catalog("")
    assert calls and calls[0].endswith("/models")
    model_provider.openrouter_endpoint_completion_price("", "vendor/some-model", "tag")
    assert any("models/vendor/some-model/endpoints" in url for url in calls)


def test_model_listing_uses_the_shared_client(monkeypatch) -> None:
    calls: list[str] = []

    class FakeClient:
        def get(self, url, **kwargs):
            calls.append(url)
            return _Response({"data": [{"id": "m1"}]})

    monkeypatch.setattr(model_provider, "_shared_http_client", lambda: FakeClient())
    monkeypatch.setattr(model_provider.httpx, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("stray get")))
    ids = model_provider.list_available_models(provider_type="openrouter", api_key="key", base_url="https://openrouter.example.test/v1")
    assert ids == ["m1"] and calls


# --- judge cache breakpoint ----------------------------------------------------------------


def _capture_post(monkeypatch, response: dict) -> dict:
    posted: dict = {}

    def fake_post(url, **kwargs):
        posted["json"] = kwargs["json"]
        return _Response(response)

    monkeypatch.setattr(model_provider, "_http_post", fake_post)
    return posted


def _judge_prompt() -> dict:
    # Dynamic evidence first on purpose: the layout puts the static instructions ahead of it.
    return {"evidence": {"entry": "Jordan Smith"}, "task": "Is the value supported?", "rules": _LONG_RULES}


def test_judge_static_block_gets_its_own_breakpoint_on_anthropic(monkeypatch) -> None:
    posted = _capture_post(monkeypatch, {"content": [{"type": "text", "text": "{}"}], "usage": {}})
    call_model_json(_provider("anthropic"), purpose="grounding_judge", system="You are a judge.", prompt=_judge_prompt(), schema=None)
    content = posted["json"]["messages"][0]["content"]
    assert content[0]["cache_control"] == {"type": "ephemeral"}
    assert "Rule 119" in content[0]["text"] and "Jordan Smith" not in content[0]["text"]
    assert "Jordan Smith" in content[1]["text"] and "cache_control" not in content[1]
    assert posted["json"]["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_judge_static_block_gets_its_own_breakpoint_on_openrouter_claude(monkeypatch) -> None:
    posted = _capture_post(monkeypatch, {"choices": [{"message": {"content": "{}"}}], "usage": {}})
    call_model_json(
        _provider("openrouter", "anthropic/claude-sonnet-4.5"), purpose="grounding_judge",
        system="You are a judge.", prompt=_judge_prompt(), schema=None,
    )
    user = posted["json"]["messages"][1]["content"]
    assert user[0]["cache_control"] == {"type": "ephemeral"} and "Rule 119" in user[0]["text"]
    assert "cache_control" not in user[1]


def test_short_judge_block_is_not_marked_and_non_claude_is_untouched(monkeypatch) -> None:
    posted = _capture_post(monkeypatch, {"content": [{"type": "text", "text": "{}"}], "usage": {}})
    call_model_json(_provider("anthropic"), purpose="x", system="Judge.", prompt={"task": "t", "rules": ["a"], "evidence": 1}, schema=None)
    assert isinstance(posted["json"]["messages"][0]["content"], str)

    posted = _capture_post(monkeypatch, {"choices": [{"message": {"content": "{}"}}], "usage": {}})
    call_model_json(_provider("openrouter", "deepseek/deepseek-chat"), purpose="x", system="Judge.", prompt=_judge_prompt(), schema=None)
    assert isinstance(posted["json"]["messages"][1]["content"], str)


def test_haiku_needs_a_larger_block() -> None:
    prompt = {"task": "t", "rules": ["x" * 40 for _ in range(100)], "evidence": 1}  # about 1.2k tokens
    assert judge_wire_parts("s", prompt, "claude-sonnet-4-5")[0]
    assert judge_wire_parts("s", prompt, "claude-haiku-4-5")[0] == ""


def test_judge_split_preserves_every_key() -> None:
    import json

    prompt = _judge_prompt()
    static, rest = judge_wire_parts("s", prompt, "claude-sonnet-4-5")
    assert json.loads(static + rest) == prompt


# --- prewarm -------------------------------------------------------------------------------


def test_prewarm_decision_per_provider() -> None:
    assert run_prewarm.prewarm_kind(_provider("anthropic")) == "cache"
    assert run_prewarm.prewarm_kind(_provider("openrouter", "anthropic/claude-sonnet-4.5")) == "cache"
    for other in (_provider("openai", "gpt-5"), _provider("openrouter", "deepseek/x"), _provider("gemini", "g"), _provider("deepseek", "d")):
        assert run_prewarm.prewarm_kind(other) == "connection"
    assert run_prewarm.prewarm_kind(None) == "none"
    assert run_prewarm.prewarm_kind(SimpleNamespace(live_enabled=False)) == "none"


def test_prewarm_settings_and_env_switches(monkeypatch) -> None:
    monkeypatch.delenv("LUMEN_RUN_PREWARM", raising=False)
    assert run_prewarm.prewarm_enabled({}) is True
    assert run_prewarm.prewarm_enabled({"run_start_prewarm": False}) is False
    monkeypatch.setenv("LUMEN_RUN_PREWARM", "false")
    assert run_prewarm.prewarm_enabled({"run_start_prewarm": True}) is False


def test_cache_prewarm_sends_stable_system_with_breakpoint_and_lands_in_the_ledger(monkeypatch) -> None:
    posted: list[dict] = []
    got: list[str] = []
    monkeypatch.setattr(model_provider, "_http_get", lambda url, **kw: got.append(url) or _Response())
    monkeypatch.setattr(
        model_provider,
        "_http_post",
        lambda url, **kw: posted.append(kw["json"]) or _Response({"content": [{"type": "text", "text": "{"}], "usage": {"input_tokens": 9, "output_tokens": 1}}),
    )
    system = "RULES BLOCK " * 50 + SYSTEM_DYNAMIC_BOUNDARY + "dynamic directive"
    token = model_call_ledger.bind(987001)
    try:
        report = run_prewarm.run_prewarm(_provider("anthropic"), system=system, schema=None, run_id=987001)
    finally:
        tally = model_call_ledger.release(token, 987001)
    assert report["connection"] is True and report["cache"] is True
    assert got and len(posted) == 1
    body = posted[0]
    assert body["max_tokens"] == 1
    assert body["system"][0]["text"] == "RULES BLOCK " * 50  # exactly the wire system text, no directive
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert tally["prewarm"]["calls"] == 1


def test_openai_prewarm_warms_only_the_connection(monkeypatch) -> None:
    posted: list = []
    got: list = []
    monkeypatch.setattr(model_provider, "_http_get", lambda url, **kw: got.append(url) or _Response())
    monkeypatch.setattr(model_provider, "_http_post", lambda url, **kw: posted.append(kw))
    report = run_prewarm.run_prewarm(_provider("openai", "gpt-5"), system="S" * 5000, schema=None, run_id=None)
    assert report == {"kind": "connection", "connection": True, "cache": None}
    assert got and not posted


def test_prewarm_never_raises(monkeypatch) -> None:
    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(model_provider, "_http_get", boom)
    monkeypatch.setattr(model_provider, "_http_post", boom)
    report = run_prewarm.run_prewarm(_provider("anthropic"), system="S" * 500, schema=None, run_id=None)
    assert report["kind"] == "cache" and report["cache"] is False


def test_prewarm_skipped_for_fake_or_missing_provider() -> None:
    assert run_prewarm.run_prewarm(None, system="S", schema=None, run_id=1) == {"kind": "none", "connection": False, "cache": None}


def _start(provider, monkeypatch, sync) -> asyncio.Task | None:
    monkeypatch.setattr(extension_run, "_run_start_prewarm_sync", sync)
    return extension_run._start_run_prewarm(
        SimpleNamespace(),
        user_id=1,
        run_id=1,
        provider=provider,
        goal="g",
        execution_surface="browser",
        candidate_data_access="none",
        candidate_data_scopes=[],
        is_generic_browser_task=True,
        totp_capability=False,
        state={},
    )


def test_start_is_scheduled_and_does_not_block_the_loop(monkeypatch) -> None:
    monkeypatch.delenv("LUMEN_RUN_PREWARM", raising=False)
    release = threading.Event()
    started = threading.Event()

    def slow(**kwargs):
        started.set()
        release.wait(5)
        return {"kind": "cache"}

    async def scenario() -> None:
        begin = time.monotonic()
        task = _start(_provider("anthropic"), monkeypatch, slow)
        assert task is not None
        assert time.monotonic() - begin < 0.5  # returned while the prewarm still waits
        await asyncio.sleep(0.05)
        assert started.is_set() and not task.done()
        release.set()
        await task

    asyncio.run(scenario())


def test_start_skipped_when_disabled_or_provider_not_live(monkeypatch) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("LUMEN_RUN_PREWARM", "false")
        assert _start(_provider("anthropic"), monkeypatch, lambda **kw: {}) is None
        monkeypatch.delenv("LUMEN_RUN_PREWARM", raising=False)
        assert _start(None, monkeypatch, lambda **kw: {}) is None
        assert _start(SimpleNamespace(live_enabled=False), monkeypatch, lambda **kw: {}) is None

    asyncio.run(scenario())


def test_sync_body_honors_the_user_setting_and_never_raises(monkeypatch) -> None:
    monkeypatch.delenv("LUMEN_RUN_PREWARM", raising=False)
    monkeypatch.setattr(extension_run, "get_agent_settings", lambda db, user_id: {"run_start_prewarm": False})
    called: list = []
    monkeypatch.setattr(run_prewarm, "run_prewarm", lambda *a, **k: called.append(1) or {})
    result = extension_run._run_start_prewarm_sync(user_id=1, run_id=1, provider=_provider("anthropic"), context={"goal": "g"}, state={})
    assert result == {"kind": "disabled"} and not called

    monkeypatch.setattr(extension_run, "get_agent_settings", lambda db, user_id: {})
    state: dict = {}
    result = extension_run._run_start_prewarm_sync(
        user_id=1, run_id=1, provider=_provider("anthropic"),
        context={"goal": "g", "execution_surface": "browser", "surfaces": ["browser"], "generic_browser_task": True,
                 "candidate_data_access": "none", "candidate_data_scopes": [], "page_evidence": {}},
        state=state,
    )
    assert called and state.get("system_hash")
