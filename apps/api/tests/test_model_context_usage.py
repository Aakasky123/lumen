from app.model_context import context_usage_from_metadata, model_context_window, normalize_token_usage


def test_normalize_token_usage_accepts_provider_shapes() -> None:
    assert normalize_token_usage({"prompt_tokens": 10, "completion_tokens": 3}) == {
        "prompt_tokens": 10,
        "completion_tokens": 3,
        "total_tokens": 13,
    }
    assert normalize_token_usage({"input_tokens": 7, "output_tokens": 2, "total_tokens": 9}) == {
        "prompt_tokens": 7,
        "completion_tokens": 2,
        "total_tokens": 9,
    }
    assert normalize_token_usage({"promptTokenCount": 5, "candidatesTokenCount": 4, "totalTokenCount": 9}) == {
        "prompt_tokens": 5,
        "completion_tokens": 4,
        "total_tokens": 9,
    }


def test_model_context_window_is_known_only_for_supported_profiles() -> None:
    for provider, model in (
        ("deepseek", "deepseek-v4-pro"),
        ("openrouter", "deepseek/deepseek-v4-flash"),
    ):
        known = model_context_window(provider, model)
        assert known["limit_tokens"] == 1_000_000
        assert known["known"] is True

    unknown = model_context_window("openrouter", "some-new-model-without-profile")
    assert unknown["limit_tokens"] is None
    assert unknown["known"] is False


def test_context_usage_from_real_model_metadata() -> None:
    usage = context_usage_from_metadata(
        {
            "provider": "openrouter",
            "provider_display_name": "DeepSeek V4 Flash",
            "model": "deepseek/deepseek-v4-flash",
            "purpose": "browser_agent_step",
            "token_usage": {"prompt_tokens": 12000, "completion_tokens": 600, "total_tokens": 12600},
        },
        source="browser_agent_step",
    )

    assert usage
    assert usage["used_tokens"] == 12000
    assert usage["completion_tokens"] == 600
    assert usage["total_tokens"] == 12600
    assert usage["limit_tokens"] == 1_000_000
    assert usage["context_window_source"] == "known_model_profile:deepseek-v4"
    assert usage["usage_source"] == "provider_response.token_usage"


def test_context_usage_does_not_invent_unknown_window() -> None:
    usage = context_usage_from_metadata(
        {
            "provider": "openrouter",
            "model": "unknown-model",
            "token_usage": {"prompt_tokens": 42},
        }
    )

    assert usage
    assert usage["used_tokens"] == 42
    assert usage["limit_tokens"] is None
    assert usage["context_window_known"] is False
