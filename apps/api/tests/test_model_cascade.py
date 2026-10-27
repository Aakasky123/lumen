"""DeepSeek V4 thinking-mode enablement for the JSON/agentic call path."""

from types import SimpleNamespace

from app.model_provider import _deepseek_thinking_extra


def test_thinking_enabled_for_deepseek_v4_models() -> None:
    for model in ("deepseek-v4-flash", "deepseek-v4-pro", "DeepSeek-V4-Pro"):
        provider = SimpleNamespace(provider_type="deepseek", model_name=model)
        assert _deepseek_thinking_extra(provider) == {"thinking": {"type": "enabled"}}


def test_thinking_not_added_for_legacy_or_other_providers() -> None:
    assert _deepseek_thinking_extra(SimpleNamespace(provider_type="deepseek", model_name="deepseek-chat")) == {}
    assert _deepseek_thinking_extra(SimpleNamespace(provider_type="deepseek", model_name="deepseek-reasoner")) == {}
    assert _deepseek_thinking_extra(SimpleNamespace(provider_type="openai", model_name="gpt-4o")) == {}
    assert _deepseek_thinking_extra(SimpleNamespace(provider_type="anthropic", model_name="claude-pro-v4")) == {}
