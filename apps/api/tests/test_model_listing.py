"""Dynamic model selection: a provider key -> the list of models it can use, parsed uniformly across
provider shapes (OpenAI-compatible data[].id, Anthropic data[].id, Gemini models[].name). Powers the
frontend model dropdown. HTTP is mocked — this tests the parsing + error mapping, not the live APIs."""

import httpx
import pytest

from app import model_provider
from app.model_provider import list_available_models


class _Resp:
    def __init__(self, status: int, payload: dict) -> None:
        self.status_code = status
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=httpx.Request("GET", "http://x"), response=httpx.Response(self.status_code))


def test_openai_compatible_models_parsed(monkeypatch) -> None:
    monkeypatch.setattr(model_provider, "_http_get", lambda *a, **k: _Resp(200, {"data": [{"id": "deepseek-v4-pro"}, {"id": "deepseek-v4-flash"}]}))
    models = list_available_models(provider_type="deepseek", api_key="sk-x")
    assert models == ["deepseek-v4-flash", "deepseek-v4-pro"]  # sorted, deduped


def test_anthropic_models_parsed(monkeypatch) -> None:
    captured = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured["headers"] = kwargs.get("headers") or {}
        return _Resp(200, {"data": [{"id": "claude-opus-4-8"}, {"id": "claude-sonnet-4-6"}]})

    monkeypatch.setattr(model_provider, "_http_get", fake_get)
    models = list_available_models(provider_type="anthropic", api_key="sk-ant")
    assert "claude-opus-4-8" in models
    assert captured["headers"].get("x-api-key") == "sk-ant"  # anthropic auth header, not Bearer


def test_gemini_models_parsed_and_stripped(monkeypatch) -> None:
    payload = {
        "models": [
            {"name": "models/gemini-2.5-pro", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},  # not chat -> dropped
        ]
    }
    monkeypatch.setattr(model_provider, "_http_get", lambda *a, **k: _Resp(200, payload))
    models = list_available_models(provider_type="gemini", api_key="g-key")
    assert models == ["gemini-2.5-pro"]  # "models/" prefix stripped; embedding model excluded


def test_rejected_key_is_a_clean_error(monkeypatch) -> None:
    monkeypatch.setattr(model_provider, "_http_get", lambda *a, **k: _Resp(401, {}))
    with pytest.raises(ValueError, match="rejected"):
        list_available_models(provider_type="openai", api_key="bad")


def test_missing_key_and_unknown_type_are_rejected() -> None:
    with pytest.raises(ValueError):
        list_available_models(provider_type="deepseek", api_key="")
    with pytest.raises(ValueError):
        list_available_models(provider_type="not_a_provider", api_key="x")
