import base64
import hashlib
import json
from dataclasses import replace

import pytest

from app.model_provider import ResolvedModelProvider, call_model_json


class _FakeResponse:
    def __init__(self, body: dict):
        self._body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._body


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _provider(provider_type: str) -> ResolvedModelProvider:
    return ResolvedModelProvider(
        id=73,
        display_name=f"{provider_type} transport test",
        provider_type=provider_type,
        model_name="transport-test-model",
        base_url=f"https://{provider_type}.example.test/v1",
        api_key="test-only-key",
        temperature=0.0,
        max_tokens=128,
        timeout_seconds=5,
        enabled=True,
        is_active=True,
        source="database",
    )


@pytest.mark.parametrize(
    ("provider_type", "response_body"),
    [
        (
            "custom_openai_compatible",
            {"choices": [{"message": {"content": "{}"}}], "usage": {}},
        ),
        (
            "anthropic",
            {"content": [{"type": "text", "text": "{}"}], "usage": {}},
        ),
        (
            "gemini",
            {
                "candidates": [{"content": {"parts": [{"text": "{}"}]}}],
                "usageMetadata": {},
            },
        ),
    ],
)
def test_transport_receipt_does_not_claim_schema_omitted_from_http_json(
    monkeypatch,
    provider_type: str,
    response_body: dict,
) -> None:
    posted: dict = {}

    def fake_post(url, **kwargs):
        posted["json"] = kwargs["json"]
        return _FakeResponse(response_body)

    monkeypatch.setattr("app.model_provider._http_post", fake_post)
    schema = {"name": "browser_action", "schema": {"type": "object"}}

    result = call_model_json(
        _provider(provider_type),
        purpose="transport_receipt_test",
        system="Return JSON.",
        prompt={"goal": "Inspect the page."},
        schema=schema,
    )

    metadata = result["metadata"]
    assert result["ok"] is True
    assert metadata["transport_boundary"] == "provider_http_json_body_before_serialization"
    assert metadata["provider_request_body_prepared"] is True
    assert metadata["provider_http_json_body_sha256"] == _canonical_sha256(posted["json"])
    assert metadata["schema_requested"] is True
    assert metadata["provider_requested_schema_sha256"] == _canonical_sha256(schema)
    assert metadata["schema_included"] is False
    assert metadata["provider_payload_schema_sha256"] is None
    assert schema["name"] not in json.dumps(posted["json"])


def test_openai_transport_receipt_hashes_schema_actually_in_http_json(monkeypatch) -> None:
    posted: dict = {}

    def fake_post(url, **kwargs):
        posted["json"] = kwargs["json"]
        return _FakeResponse({"output_text": "{}", "usage": {}})

    monkeypatch.setattr("app.model_provider._http_post", fake_post)
    schema = {"name": "browser_action", "schema": {"type": "object"}}

    result = call_model_json(
        _provider("openai"),
        purpose="transport_receipt_test",
        system="Return JSON.",
        prompt={"goal": "Inspect the page."},
        schema=schema,
    )

    metadata = result["metadata"]
    assert result["ok"] is True
    assert posted["json"]["text"]["format"]["name"] == schema["name"]
    assert posted["json"]["text"]["format"]["schema"] == schema["schema"]
    assert metadata["provider_http_json_body_sha256"] == _canonical_sha256(posted["json"])
    assert metadata["schema_requested"] is True
    assert metadata["schema_included"] is True
    assert metadata["provider_payload_schema_sha256"] == _canonical_sha256(schema)


def test_openrouter_transport_sends_and_hashes_requested_json_schema(monkeypatch) -> None:
    posted: dict = {}

    def fake_post(url, **kwargs):
        posted["json"] = kwargs["json"]
        return _FakeResponse(
            {"choices": [{"message": {"content": "{}"}}], "usage": {}}
        )

    monkeypatch.setattr("app.model_provider._http_post", fake_post)
    schema = {
        "name": "browser_action",
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"action": {"type": "string"}},
            "required": ["action"],
        },
    }

    result = call_model_json(
        _provider("openrouter"),
        purpose="transport_receipt_test",
        system="Return JSON.",
        prompt={"goal": "Inspect the page."},
        schema=schema,
    )

    metadata = result["metadata"]
    assert result["ok"] is True
    assert posted["json"]["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": schema["name"],
            "strict": True,
            "schema": schema["schema"],
        },
    }
    assert posted["json"]["provider"] == {"require_parameters": True}
    assert metadata["provider_http_json_body_sha256"] == _canonical_sha256(posted["json"])
    assert metadata["schema_requested"] is True
    assert metadata["schema_included"] is True
    assert metadata["provider_payload_schema_sha256"] == _canonical_sha256(schema)


@pytest.mark.parametrize(
    "purpose",
    [
        "operator_chat_scope",
        "operator_chat_scope_protocol_repair",
        "target_binding_judge",
        "control_reference_conflict",
        "secret_request_classification",
        "credential_target_classification",
        "pending_user_reply_interpretation",
    ],
)
def test_openrouter_compact_structural_verdicts_disable_hidden_reasoning(
    monkeypatch,
    purpose: str,
) -> None:
    posted: dict = {}

    def fake_post(url, **kwargs):
        posted["json"] = kwargs["json"]
        return _FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"matches":true,"reason":"Exact raw association."}'
                        }
                    }
                ],
                "usage": {},
            }
        )

    monkeypatch.setattr("app.model_provider._http_post", fake_post)
    schema = {
        "name": "target_binding",
        "schema": {
            "type": "object",
            "properties": {
                "matches": {"type": "boolean"},
                "reason": {"type": "string"},
            },
            "required": ["matches", "reason"],
        },
    }

    result = call_model_json(
        _provider("openrouter"),
        purpose=purpose,
        system="Return JSON.",
        prompt={"question": "Current visible question"},
        schema=schema,
    )

    assert result["ok"] is True
    assert posted["json"]["reasoning"] == {"enabled": False}
    assert result["metadata"]["reasoning_config_included"] is True
    assert result["metadata"]["reasoning_enabled_at_transport"] is False


def test_openrouter_provider_reasoning_effort_shapes_request_unless_reasoning_disabled(
    monkeypatch,
) -> None:
    posted: dict = {}

    def fake_post(url, **kwargs):
        posted["json"] = kwargs["json"]
        return _FakeResponse(
            {
                "choices": [
                    {"message": {"content": '{"matches":true,"reason":"Exact raw association."}'}}
                ],
                "usage": {},
            }
        )

    monkeypatch.setattr("app.model_provider._http_post", fake_post)
    provider = replace(_provider("openrouter"), reasoning_effort="medium")
    schema = {
        "name": "target_binding",
        "schema": {
            "type": "object",
            "properties": {
                "matches": {"type": "boolean"},
                "reason": {"type": "string"},
            },
            "required": ["matches", "reason"],
        },
    }

    # A full agent decision (no reasoning switch from the caller) uses the configured effort.
    result = call_model_json(
        provider,
        purpose="vision_agent_step",
        system="Return JSON.",
        prompt={"question": "Current visible question"},
        schema=schema,
    )
    assert result["ok"] is True
    assert posted["json"]["reasoning"] == {"effort": "medium"}

    # A compact structural verdict still disables hidden reasoning outright.
    result = call_model_json(
        provider,
        purpose="target_binding_judge",
        system="Return JSON.",
        prompt={"question": "Current visible question"},
        schema=schema,
    )
    assert result["ok"] is True
    assert posted["json"]["reasoning"] == {"enabled": False}

    # The payment floor and outcome verifier have small fixed output budgets: a configured effort
    # spent them on hidden reasoning and the floor failed closed with no verdict.
    for purpose in ("payment_floor_judge", "final_action_outcome_verification", "session_context_compression"):
        result = call_model_json(
            provider,
            purpose=purpose,
            system="Return JSON.",
            prompt={"question": "Current visible question"},
            schema=schema,
        )
        assert result["ok"] is True
        assert posted["json"]["reasoning"] == {"enabled": False}, purpose


def test_grounding_judge_keeps_the_rows_reasoning_effort(monkeypatch) -> None:
    # The grounding judge checks arithmetic, composes values from several page entries, and reads the
    # screenshot; it is not a compact reasoning-off gate. Its budget, not its effort, was the problem.
    posted: dict = {}

    def fake_post(url, **kwargs):
        posted["json"] = kwargs["json"]
        return _FakeResponse(
            {"choices": [{"message": {"content": '{"grounded":true,"entry_index":0,"reason":"Stated."}'}}], "usage": {}}
        )

    monkeypatch.setattr("app.model_provider._http_post", fake_post)
    result = call_model_json(
        replace(_provider("openrouter"), reasoning_effort="medium"),
        purpose="grounding_judge",
        system="Return JSON.",
        prompt={"proposed_value": "report-XUHU.txt"},
        schema={"name": "lumen_grounding_judge", "schema": {"type": "object"}},
    )

    assert result["ok"] is True
    assert posted["json"]["reasoning"] == {"effort": "medium"}


def test_transport_receipt_hashes_actual_included_image_without_retaining_it(monkeypatch) -> None:
    posted: dict = {}

    def fake_post(url, **kwargs):
        posted["json"] = kwargs["json"]
        return _FakeResponse(
            {"choices": [{"message": {"content": "{}"}}], "usage": {}}
        )

    monkeypatch.setattr("app.model_provider._http_post", fake_post)
    raw_image = b"provider-transport-image"
    image = {
        "media_type": "image/jpeg",
        "data": base64.b64encode(raw_image).decode("ascii"),
    }

    result = call_model_json(
        _provider("custom_openai_compatible"),
        purpose="transport_receipt_test",
        system="Return JSON.",
        prompt={"goal": "Inspect the page."},
        image=image,
    )

    metadata = result["metadata"]
    assert result["ok"] is True
    assert metadata["image_requested"] is True
    assert metadata["image_included"] is True
    assert metadata["provider_requested_image_sha256"] == hashlib.sha256(raw_image).hexdigest()
    assert metadata["provider_payload_image_sha256"] == hashlib.sha256(raw_image).hexdigest()
    assert image["data"] not in json.dumps(metadata)
    assert metadata["provider_http_json_body_sha256"] == _canonical_sha256(posted["json"])


def test_transport_carries_multiple_images_in_order(monkeypatch) -> None:
    # A vision call may attach supplementary images after the primary screenshot
    # (for example a native-resolution zoom crop). Every image must reach the wire,
    # in order, and the receipt must hash all of them.
    posted: dict = {}

    def fake_post(url, **kwargs):
        posted["json"] = kwargs["json"]
        return _FakeResponse(
            {"choices": [{"message": {"content": "{}"}}], "usage": {}}
        )

    monkeypatch.setattr("app.model_provider._http_post", fake_post)
    raw_primary = b"full-screenshot"
    raw_crop = b"zoom-crop"
    images = [
        {"media_type": "image/jpeg", "data": base64.b64encode(raw_primary).decode("ascii")},
        {"media_type": "image/jpeg", "data": base64.b64encode(raw_crop).decode("ascii")},
    ]

    result = call_model_json(
        _provider("custom_openai_compatible"),
        purpose="transport_receipt_test",
        system="Return JSON.",
        prompt={"goal": "Read the fine print."},
        image=images,
    )

    assert result["ok"] is True
    content = posted["json"]["messages"][-1]["content"]
    image_parts = [part for part in content if part.get("type") == "image_url"]
    assert len(image_parts) == 2
    assert image_parts[0]["image_url"]["url"].endswith(images[0]["data"])
    assert image_parts[1]["image_url"]["url"].endswith(images[1]["data"])
    metadata = result["metadata"]
    assert metadata["image_requested"] is True
    assert metadata["image_included"] is True
    assert metadata["provider_payload_image_sha256"] == hashlib.sha256(raw_primary + raw_crop).hexdigest()


def test_deepseek_v4_flash_transport_remains_text_only_when_image_is_requested(monkeypatch) -> None:
    posted: dict = {}

    def fake_post(url, **kwargs):
        posted["json"] = kwargs["json"]
        return _FakeResponse(
            {"choices": [{"message": {"content": "{}"}}], "usage": {}}
        )

    monkeypatch.setattr("app.model_provider._http_post", fake_post)
    base_provider = _provider("deepseek")
    provider = ResolvedModelProvider(
        **{**base_provider.__dict__, "model_name": "deepseek-v4-flash"}
    )
    raw_image = b"must-not-reach-deepseek"
    image = {
        "media_type": "image/jpeg",
        "data": base64.b64encode(raw_image).decode("ascii"),
    }

    result = call_model_json(
        provider,
        purpose="transport_receipt_test",
        system="Return JSON.",
        prompt={"goal": "Inspect the page."},
        image=image,
    )

    metadata = result["metadata"]
    assert result["ok"] is True
    assert metadata["image_requested"] is True
    assert metadata["image_included"] is False
    assert metadata["provider_requested_image_sha256"] == hashlib.sha256(raw_image).hexdigest()
    assert metadata["provider_payload_image_sha256"] is None
    assert isinstance(posted["json"]["messages"][-1]["content"], str)
    assert image["data"] not in json.dumps(posted["json"])


def test_unavailable_provider_receipt_does_not_claim_prepared_http_body() -> None:
    provider = _provider("anthropic")
    unavailable = ResolvedModelProvider(
        **{
            **provider.__dict__,
            "api_key": None,
            "last_error": "provider unavailable",
        }
    )
    schema = {"name": "browser_action", "schema": {"type": "object"}}

    result = call_model_json(
        unavailable,
        purpose="transport_receipt_test",
        system="Return JSON.",
        prompt={"goal": "Inspect the page."},
        schema=schema,
    )

    metadata = result["metadata"]
    assert result["ok"] is False
    assert metadata["transport_boundary"] == "provider_request_not_prepared"
    assert metadata["provider_request_body_prepared"] is False
    assert metadata["provider_http_json_body_sha256"] is None
    assert metadata["schema_requested"] is True
    assert metadata["schema_included"] is False
    assert metadata["provider_payload_system_sha256"] is None
    assert metadata["provider_payload_prompt_sha256"] is None
    assert metadata["provider_payload_schema_sha256"] is None


def test_transport_error_still_reports_only_the_body_prepared_before_http(monkeypatch) -> None:
    posted: dict = {}

    def failing_post(url, **kwargs):
        posted["json"] = kwargs["json"]
        raise RuntimeError("simulated transport failure")

    monkeypatch.setattr("app.model_provider._http_post", failing_post)

    result = call_model_json(
        _provider("anthropic"),
        purpose="transport_receipt_test",
        system="Return JSON.",
        prompt={"goal": "Inspect the page."},
        schema={"name": "browser_action", "schema": {"type": "object"}},
    )

    metadata = result["metadata"]
    assert result["ok"] is False
    assert metadata["transport_boundary"] == "provider_http_json_body_before_serialization"
    assert metadata["provider_request_body_prepared"] is True
    assert metadata["provider_http_json_body_sha256"] == _canonical_sha256(posted["json"])
    assert metadata["schema_requested"] is True
    assert metadata["schema_included"] is False
    assert metadata["provider_payload_schema_sha256"] is None


def test_openrouter_structured_output_404_falls_back_to_json_object(monkeypatch) -> None:
    import httpx

    posted_bodies: list[dict] = []

    def fake_post(url, **kwargs):
        posted_bodies.append(kwargs["json"])
        if len(posted_bodies) == 1:
            request = httpx.Request("POST", url)
            return httpx.Response(
                404,
                request=request,
                text='{"error":{"message":"No endpoints found that support the requested parameters."}}',
            )
        return _FakeResponse({"choices": [{"message": {"content": "{\"action\":\"scroll\"}"}}], "usage": {}})

    monkeypatch.setattr("app.model_provider._http_post", fake_post)
    schema = {"name": "browser_action", "schema": {"type": "object"}}

    result = call_model_json(
        _provider("openrouter"),
        purpose="transport_fallback_test",
        system="Return JSON.",
        prompt={"goal": "Inspect the page."},
        schema=schema,
    )

    assert result["ok"] is True
    assert len(posted_bodies) == 2
    strict_body, fallback_body = posted_bodies
    assert strict_body["response_format"]["type"] == "json_schema"
    assert strict_body["provider"] == {"require_parameters": True}
    assert fallback_body["response_format"] == {"type": "json_object"}
    assert "provider" not in fallback_body
    assert fallback_body["messages"] == strict_body["messages"], "fallback must not alter the prompt"

    metadata = result["metadata"]
    assert metadata["structured_output_fallback"] is True
    assert metadata["schema_included"] is False
    assert metadata["provider_payload_schema_sha256"] is None
    assert metadata["provider_http_json_body_sha256"] == _canonical_sha256(fallback_body)


def test_openrouter_endpoint_preference_is_tried_first_and_survives_the_schema_fallback(monkeypatch) -> None:
    import httpx

    posted: list[dict] = []

    def fake_post(url, **kwargs):
        posted.append(kwargs["json"])
        if len(posted) == 2:
            return httpx.Response(404, request=httpx.Request("POST", url), text='{"error":{"message":"No endpoints"}}')
        return _FakeResponse({"provider": "OpenAI", "choices": [{"message": {"content": "{\"action\":\"scroll\"}"}}], "usage": {}})

    monkeypatch.setattr("app.model_provider._http_post", fake_post)
    preferred = replace(_provider("openrouter"), endpoint_preference="openai/fast")

    plain = call_model_json(preferred, purpose="t", system="Return JSON.", prompt={"goal": "g"})
    assert posted[0]["provider"] == {"require_parameters": True, "order": ["openai/fast"]}, "fallbacks stay allowed: no allow_fallbacks=false"
    assert plain["metadata"]["upstream_provider"] == "OpenAI"
    assert plain["metadata"]["provider_http_json_body_sha256"] == _canonical_sha256(posted[0])

    schema = {"name": "browser_action", "schema": {"type": "object"}}
    fallback = call_model_json(preferred, purpose="t", system="Return JSON.", prompt={"goal": "g"}, schema=schema)
    strict_body, fallback_body = posted[1], posted[2]
    assert strict_body["provider"] == {"require_parameters": True, "order": ["openai/fast"]}
    assert fallback_body["provider"] == {"order": ["openai/fast"]}, "the fallback drops require_parameters, not the preference"
    assert fallback["metadata"]["structured_output_fallback"] is True

    call_model_json(_provider("openrouter"), purpose="t", system="Return JSON.", prompt={"goal": "g"})
    assert posted[-1]["provider"] == {"require_parameters": True}, "no preference keeps OpenRouter's own routing"
    call_model_json(replace(_provider("custom_openai_compatible"), endpoint_preference="openai/fast"), purpose="t", system="Return JSON.", prompt={})
    assert "provider" not in posted[-1], "routing blocks are OpenRouter-only"


def test_endpoint_preference_accepts_only_endpoint_tags() -> None:
    from app.model_provider import ModelProviderService

    normalize = ModelProviderService._normalized_endpoint_preference
    assert normalize(" OpenAI/Fast ") == "openai/fast"
    assert normalize("azure") == "azure"
    assert normalize("amazon-bedrock/us-east-1") == "amazon-bedrock/us-east-1"
    for rejected in ("", None, "openai/fast/extra", "openai fast", "/fast", "openai/"):
        assert normalize(rejected) is None


def test_a_gate_on_a_reasoning_mandatory_model_sends_its_least_effort_not_a_doomed_disable(monkeypatch) -> None:
    posted: list[dict] = []

    def fake_post(url, **kwargs):
        posted.append(kwargs["json"])
        return _FakeResponse({"choices": [{"message": {"content": '{"matches":true,"reason":"ok"}'}}], "usage": {}})

    monkeypatch.setattr("app.model_provider._http_post", fake_post)
    _catalog(
        monkeypatch,
        {},
        {"transport-test-model": {"mandatory": True, "efforts": ("max", "high", "low")}},
    )
    provider = replace(_provider("openrouter"), reasoning_effort="high")
    gate = call_model_json(provider, purpose="target_binding_judge", system="Return JSON.", prompt={"q": "x"})
    assert posted[-1]["reasoning"] == {"effort": "low"}
    assert gate["metadata"]["reasoning_mandatory_substitute"] == "low"

    decision = call_model_json(provider, purpose="vision_agent_step", system="Return JSON.", prompt={"q": "x"})
    assert posted[-1]["reasoning"] == {"effort": "high"}, "a full decision keeps the configured effort"
    assert "reasoning_mandatory_substitute" not in decision["metadata"]

    _catalog(monkeypatch, {}, {"transport-test-model": {"mandatory": False, "efforts": ("low",)}})
    optional = call_model_json(provider, purpose="target_binding_judge", system="Return JSON.", prompt={"q": "x"})
    assert posted[-1]["reasoning"] == {"enabled": False}
    assert "reasoning_mandatory_substitute" not in optional["metadata"]


def test_openrouter_404_without_require_parameters_is_not_retried(monkeypatch) -> None:
    import httpx

    calls: list[dict] = []

    def fake_post(url, **kwargs):
        calls.append(kwargs["json"])
        return httpx.Response(404, request=httpx.Request("POST", url), text='{"error":{"message":"model not found"}}')

    monkeypatch.setattr("app.model_provider._http_post", fake_post)

    result = call_model_json(
        replace(_provider("openrouter"), endpoint_routing={"require_parameters": False}),
        purpose="transport_fallback_test",
        system="Return JSON.",
        prompt={"goal": "Inspect the page."},
        schema=None,
    )

    assert result["ok"] is False
    assert len(calls) == 1, "nothing was filtered by require_parameters, so a 404 is a real error"
    assert "provider" not in calls[0]

    calls.clear()
    default = call_model_json(
        _provider("openrouter"),
        purpose="transport_fallback_test",
        system="Return JSON.",
        prompt={"goal": "Inspect the page."},
        schema=None,
    )
    assert default["ok"] is False
    assert [("provider" in body) for body in calls] == [True, False], "default sends require_parameters, one retry drops it"


def test_page_rendered_prompt_sections_ride_unredacted_only_beside_their_screenshot() -> None:
    from app.model_provider import _redact_prompt_for_transport

    prompt = {
        "goal": "log in with code AB12CD",
        "human_answers": [{"answer": "PIN: 4821"}],
        "page_memory": [{"text": "Spec code: X7Q2Z9"}],
        "page_evidence": {"viewport_text": "PIN: 5239"},
        "recent_actions": ["type ok value='X7Q2Z9'"],
    }
    with_screenshot = _redact_prompt_for_transport(prompt, screenshot_attached=True)
    assert with_screenshot["page_memory"] == [{"text": "Spec code: X7Q2Z9"}]
    assert with_screenshot["page_evidence"] == {"viewport_text": "PIN: 5239"}
    assert with_screenshot["recent_actions"] == ["type ok value='X7Q2Z9'"]
    # What the user or task supplied is still scrubbed; the page's pixels never carried it.
    assert "AB12CD" not in with_screenshot["goal"]
    assert "4821" not in str(with_screenshot["human_answers"])

    text_only = _redact_prompt_for_transport(prompt, screenshot_attached=False)
    assert "X7Q2Z9" not in str(text_only["page_memory"])
    assert "5239" not in str(text_only["page_evidence"])


def test_chat_post_retries_a_dropped_connection_but_not_a_timeout(monkeypatch):
    import httpx
    import pytest

    import app.model_provider as model_provider

    monkeypatch.setattr(model_provider.time, "sleep", lambda seconds: None)
    calls = {"n": 0}

    def flaky(url, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

    monkeypatch.setattr(model_provider, "_http_post", flaky)
    response = model_provider._post_with_transient_retry("https://provider.test/chat/completions", json={})
    assert response.status_code == 200
    assert calls["n"] == 2

    def slow(url, **kwargs):
        calls["n"] += 1
        raise httpx.ReadTimeout("timed out")

    calls["n"] = 0
    monkeypatch.setattr(model_provider, "_http_post", slow)
    with pytest.raises(httpx.ReadTimeout):
        model_provider._post_with_transient_retry("https://provider.test/chat/completions", json={})
    assert calls["n"] == 1


def _catalog(monkeypatch, parameters: dict[str, frozenset[str]], reasoning: dict | None = None) -> None:
    import time

    from app import model_provider

    monkeypatch.setattr(model_provider, "_OPENROUTER_MODEL_CONTEXT", (time.time(), {}, parameters, reasoning or {}))


def test_a_json_object_provider_sends_one_request_without_a_doomed_strict_attempt(monkeypatch) -> None:
    # GPT-6 Luna looped on whitespace to the token cap in 15% of strict-schema calls and in none
    # of its json_object calls, so its provider row asks for json_object and Lumen's validator
    # enforces the schema. The strict attempt is never sent, so no 404 round trip precedes it.
    posted: list[dict] = []

    def fake_post(url, **kwargs):
        posted.append(kwargs["json"])
        return _FakeResponse({"choices": [{"message": {"content": "{\"action\":\"click\"}"}}], "usage": {}})

    monkeypatch.setattr("app.model_provider._http_post", fake_post)
    _catalog(monkeypatch, {})
    result = call_model_json(
        replace(_provider("openrouter"), structured_output="json_object"),
        purpose="vision_agent_step",
        system="Return JSON.",
        prompt={"goal": "Inspect the page."},
        schema={"name": "browser_action", "schema": {"type": "object"}},
    )

    assert result["ok"] is True
    assert len(posted) == 1
    assert posted[0]["response_format"] == {"type": "json_object"}
    assert posted[0]["provider"] == {"require_parameters": True}, "json_object is still never ignored by an upstream"
    metadata = result["metadata"]
    assert metadata["structured_output_mode"] == "json_object"
    assert metadata["schema_requested"] is True
    assert metadata["schema_included"] is False
    assert "structured_output_fallback" not in metadata


def test_temperature_is_left_out_only_for_a_model_the_catalog_says_declines_it(monkeypatch) -> None:
    # Under provider.require_parameters an unaccepted parameter leaves no endpoint to route to,
    # so a strict schema would 404 and degrade on every call.
    posted: list[dict] = []

    def fake_post(url, **kwargs):
        posted.append(kwargs["json"])
        return _FakeResponse({"choices": [{"message": {"content": "{}"}}], "usage": {}})

    monkeypatch.setattr("app.model_provider._http_post", fake_post)
    schema = {"name": "browser_action", "schema": {"type": "object"}}

    _catalog(monkeypatch, {"transport-test-model": frozenset({"max_tokens", "response_format", "structured_outputs"})})
    declined = call_model_json(_provider("openrouter"), purpose="t", system="Return JSON.", prompt={}, schema=schema)
    assert "temperature" not in posted[-1]
    assert posted[-1]["response_format"]["type"] == "json_schema"
    assert declined["metadata"]["temperature_omitted"] is True
    assert declined["metadata"]["structured_output_mode"] == "strict"

    _catalog(monkeypatch, {"transport-test-model": frozenset({"temperature", "response_format", "structured_outputs"})})
    accepted = call_model_json(_provider("openrouter"), purpose="t", system="Return JSON.", prompt={}, schema=schema)
    assert posted[-1]["temperature"] == 0.0
    assert "temperature_omitted" not in accepted["metadata"]

    # An unlisted model (or an unreachable catalog) keeps the request exactly as it was.
    _catalog(monkeypatch, {})
    call_model_json(_provider("openrouter"), purpose="t", system="Return JSON.", prompt={}, schema=schema)
    assert posted[-1]["temperature"] == 0.0


def test_a_failed_catalog_refresh_keeps_the_last_good_reasoning_contract(monkeypatch) -> None:
    # Dropping the tables on a failed daily refresh would turn a reasoning-mandatory brain's gates
    # back into 400 "Reasoning is mandatory" and fail every one of them closed.
    import time

    from app import model_provider

    monkeypatch.setattr(
        model_provider,
        "_OPENROUTER_MODEL_CONTEXT",
        (
            time.time() - model_provider._OPENROUTER_MODEL_CONTEXT_TTL_SECONDS - 1,
            {"z-ai/glm-5.3-flash": 1000},
            {},
            {"z-ai/glm-5.3-flash": {"mandatory": True, "efforts": ("max", "high", "low")}},
        ),
    )

    def offline(*args, **kwargs):
        raise RuntimeError("catalog offline")

    monkeypatch.setattr(model_provider, "_http_get", offline)
    assert model_provider._openrouter_reasoning_off("https://openrouter.ai/api/v1", "z-ai/glm-5.3-flash") == {"effort": "low"}
    assert model_provider._openrouter_context_length("https://openrouter.ai/api/v1", "z-ai/glm-5.3-flash") == 1000


def test_a_gate_rejected_for_mandatory_reasoning_retries_once_at_the_least_effort(monkeypatch) -> None:
    import httpx

    posted: list[dict] = []

    def fake_post(url, **kwargs):
        posted.append(kwargs["json"])
        if len(posted) == 1:
            return httpx.Response(400, request=httpx.Request("POST", url), text='{"error":{"message":"Reasoning is mandatory for this endpoint and cannot be disabled."}}')
        return _FakeResponse({"choices": [{"message": {"content": '{"matches":true,"reason":"ok"}'}}], "usage": {}})

    monkeypatch.setattr("app.model_provider._http_post", fake_post)
    _catalog(monkeypatch, {})  # the catalog does not list the model
    result = call_model_json(_provider("openrouter"), purpose="target_binding_judge", system="Return JSON.", prompt={"q": "x"})
    assert result["ok"] is True
    assert posted[0]["reasoning"] == {"enabled": False}
    assert posted[1]["reasoning"] == {"effort": "low"}
    assert result["metadata"]["reasoning_mandatory_substitute"] == "low"
    assert result["metadata"]["provider_http_json_body_sha256"] == _canonical_sha256(posted[1])


def test_transient_retries_share_the_first_attempts_time_budget(monkeypatch) -> None:
    import httpx

    import app.model_provider as model_provider

    clock = {"now": 100.0}
    monkeypatch.setattr(model_provider.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(model_provider.time, "sleep", lambda seconds: clock.__setitem__("now", clock["now"] + seconds))
    timeouts: list[float] = []

    def dropped_late(url, **kwargs):
        timeouts.append(kwargs["timeout"])
        clock["now"] += 6.0
        raise httpx.ReadError("connection reset")

    monkeypatch.setattr(model_provider, "_http_post", dropped_late)
    with pytest.raises(httpx.ReadError):
        model_provider._post_with_transient_retry("https://provider.test/chat/completions", json={}, timeout=10)
    assert timeouts[0] == 10
    assert len(timeouts) == 2 and timeouts[1] < 4.0, "the retry only gets what is left of the 10 s budget"


def test_streaming_chat_uses_the_rows_routing_and_effort(monkeypatch) -> None:
    import httpx

    from app import model_provider

    captured: dict = {}

    class _Stream:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def iter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"hi"}}]}'
            yield "data: [DONE]"

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def stream(self, method, url, headers=None, json=None, timeout=None):
            captured["json"] = json
            captured["timeout"] = timeout
            return _Stream()

    # Streamed chat replies go through the shared warm pool, with the row's own timeout.
    monkeypatch.setattr(model_provider, "_shared_http_client", lambda: _Client())
    _catalog(monkeypatch, {"transport-test-model": frozenset({"max_tokens"})})
    provider = replace(_provider("openrouter"), reasoning_effort="low", endpoint_preference="openai/fast")
    deltas = list(model_provider._iter_openai_compatible_deltas(provider, system="Be brief.", prompt={"q": "x"}))
    assert ("delta", "hi") in deltas
    assert captured["json"]["reasoning"] == {"effort": "low"}
    assert captured["json"]["provider"] == {"order": ["openai/fast"]}
    assert "temperature" not in captured["json"]
    assert captured["timeout"] == provider.timeout_seconds


def test_a_rate_limit_or_empty_upstream_reply_is_retried_within_the_budget(monkeypatch):
    import httpx

    import app.model_provider as model_provider

    pauses: list[float] = []
    monkeypatch.setattr(model_provider.time, "sleep", lambda seconds: pauses.append(seconds))
    statuses = iter([429, 503, 200])

    def limited(url, **kwargs):
        status = next(statuses)
        headers = {"retry-after": "1"} if status == 429 else {}
        return httpx.Response(status, json={"ok": status == 200}, headers=headers, request=httpx.Request("POST", url))

    monkeypatch.setattr(model_provider, "_http_post", limited)
    response = model_provider._post_with_transient_retry("https://provider.test/chat/completions", json={}, timeout=30)
    assert response.status_code == 200
    assert pauses == [1.0, 1.6]

    # Out of retries (or budget), the last error reply is returned for the caller to classify.
    monkeypatch.setattr(model_provider, "_http_post", lambda url, **kwargs: httpx.Response(429, json={}, request=httpx.Request("POST", url)))
    assert model_provider._post_with_transient_retry("https://provider.test/chat/completions", json={}, timeout=30).status_code == 429
    # A client error that retrying cannot fix is returned at once.
    pauses.clear()
    monkeypatch.setattr(model_provider, "_http_post", lambda url, **kwargs: httpx.Response(400, json={}, request=httpx.Request("POST", url)))
    assert model_provider._post_with_transient_retry("https://provider.test/chat/completions", json={}, timeout=30).status_code == 400
    assert pauses == []


def test_an_avoided_openrouter_host_is_ignored_and_routing_goes_by_throughput() -> None:
    from app.model_provider import _openrouter_routing

    pinned = replace(_provider("openrouter"), endpoint_preference="wafer")
    assert _openrouter_routing(pinned) == {"order": ["wafer"]}
    hedge = replace(pinned, endpoint_preference=None, endpoint_avoid="wafer")
    assert _openrouter_routing(hedge) == {"ignore": ["wafer"], "sort": "throughput"}
    capped = replace(hedge, endpoint_max_completion_price=0.6)
    assert _openrouter_routing(capped) == {"ignore": ["wafer"], "sort": "throughput", "max_price": {"completion": 0.6}}
    assert _openrouter_routing(replace(_provider("custom_openai_compatible"), endpoint_avoid="wafer")) == {}


def test_an_endpoint_price_is_read_from_the_listing_by_tag_or_slug(monkeypatch) -> None:
    from app import model_provider

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": {"endpoints": [
                {"tag": "wafer", "pricing": {"completion": "0.0000006"}},
                {"tag": "atlas-cloud/fp8", "pricing": {"completion": "0.000000564"}},
            ]}}

    calls: list[str] = []
    monkeypatch.setattr(model_provider, "_OPENROUTER_ENDPOINT_PRICES", {})
    monkeypatch.setattr(model_provider, "_http_get", lambda url, timeout: calls.append(url) or _Response())
    price = model_provider.openrouter_endpoint_completion_price
    assert price("https://openrouter.ai/api/v1", "deepseek/deepseek-v4.1-flash", "wafer") == pytest.approx(0.6)
    assert price("https://openrouter.ai/api/v1", "deepseek/deepseek-v4.1-flash", "atlas-cloud") == pytest.approx(0.564)
    assert price("https://openrouter.ai/api/v1", "deepseek/deepseek-v4.1-flash", "unknown") is None
    assert calls == ["https://openrouter.ai/api/v1/models/deepseek/deepseek-v4.1-flash/endpoints"], "cached"


def test_row_routing_config_pins_the_endpoint_and_sets_require_parameters(monkeypatch) -> None:
    from app.model_provider import _openrouter_routing, normalize_endpoint_routing

    posted: list[dict] = []

    def fake_post(url, **kwargs):
        posted.append(kwargs["json"])
        return _FakeResponse({"provider": "Wafer", "choices": [{"message": {"content": "{\"action\":\"scroll\"}"}}], "usage": {}})

    monkeypatch.setattr("app.model_provider._http_post", fake_post)
    pinned = replace(
        _provider("openrouter"),
        endpoint_preference="wafer",
        endpoint_routing={"allow_fallbacks": False, "require_parameters": True, "sort": "latency"},
    )
    call_model_json(pinned, purpose="t", system="Return JSON.", prompt={"goal": "g"})
    assert posted[-1]["provider"] == {"require_parameters": True, "order": ["wafer"], "allow_fallbacks": False}

    loose = replace(pinned, endpoint_preference=None, endpoint_routing={"require_parameters": False, "sort": "latency"})
    call_model_json(loose, purpose="t", system="Return JSON.", prompt={"goal": "g"})
    assert posted[-1]["provider"] == {"sort": "latency"}

    # A row pinned with allow_fallbacks false stays on its host even for a cut-off retry's skip.
    assert _openrouter_routing(replace(pinned, endpoint_skip=("wafer",))) == {"order": ["wafer"], "allow_fallbacks": False}
    # A preferred-but-unpinned row: a skip or a hedge's avoid moves the request off the host.
    preferred = replace(pinned, endpoint_routing={"sort": "latency"})
    assert _openrouter_routing(replace(preferred, endpoint_skip=("wafer",))) == {"ignore": ["wafer"], "sort": "latency"}
    assert "allow_fallbacks" not in _openrouter_routing(replace(preferred, endpoint_preference=None, endpoint_avoid="wafer"))

    assert normalize_endpoint_routing('{"allow_fallbacks": false, "sort": "Latency", "bogus": 1}') == {"allow_fallbacks": False, "sort": "latency"}
    assert normalize_endpoint_routing({"allow_fallbacks": "no", "sort": "x"}) is None
    assert normalize_endpoint_routing("not json") is None
