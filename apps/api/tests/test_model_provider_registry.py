import base64
import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app import models
from app.lumen_model import LumenModelClient
from app.db import SessionLocal, init_db
from app.main import app
from app.model_provider import ModelProviderService, ResolvedModelProvider, call_model_json
from app.model_provider_health import clear_model_provider_health_cache


class FakeResponse:
    def __init__(self, body: dict):
        self.body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self.body


def _clear_model_providers() -> None:
    init_db()
    with SessionLocal() as db:
        db.execute(delete(models.ModelProvider))
        db.commit()
    clear_model_provider_health_cache()


@pytest.fixture(autouse=True)
def clean_model_provider_registry():
    _clear_model_providers()
    yield
    _clear_model_providers()


def test_model_provider_api_masks_keys() -> None:
    secret = "sk-test-secret-model-provider"
    with TestClient(app) as client:
        created = client.post(
            "/model-providers",
            json={
                "display_name": "Custom Test Provider",
                "provider_type": "custom_openai_compatible",
                "model_name": "custom-test-model",
                "base_url": "https://example.test/v1",
                "api_key": secret,
                "temperature": 0.1,
                "max_tokens": 128,
                "timeout_seconds": 5,
                "enabled": True,
            },
        )
        assert created.status_code == 200, created.text
        assert secret not in created.text
        provider = created.json()["provider"]
        assert provider["has_api_key"] is True
        assert provider["api_key_masked"]

        listed = client.get("/model-providers")
        assert listed.status_code == 200, listed.text
        assert secret not in listed.text
        assert any(item["id"] == provider["id"] for item in listed.json()["items"])

    with SessionLocal() as db:
        db.delete(db.get(models.ModelProvider, provider["id"]))
        db.commit()


def test_reasoning_effort_accepts_every_openrouter_tier_and_drops_unknown_values() -> None:
    normalize = ModelProviderService._normalized_reasoning_effort
    for tier in ("none", "minimal", "low", "medium", "high", "xhigh", "max"):
        assert normalize(tier) == tier
    assert normalize(" XHigh ") == "xhigh"
    assert normalize("turbo") is None
    assert normalize(None) is None


def test_vision_provider_returns_truthful_pre_serialization_payload_receipt(monkeypatch) -> None:
    posted: dict = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        posted["json"] = json or {}
        return FakeResponse(
            {
                "choices": [{"message": {"content": '{"action":"done","reason":"complete"}'}}],
                "usage": {"total_tokens": 7},
            }
        )

    monkeypatch.setattr("app.model_provider._http_post", fake_post)
    provider = ResolvedModelProvider(
        id=91,
        display_name="Vision transport test",
        provider_type="custom_openai_compatible",
        model_name="vision-test",
        base_url="https://vision.example/v1",
        api_key="test-only-key",
        temperature=0.0,
        max_tokens=128,
        timeout_seconds=5,
        enabled=True,
        is_active=True,
        source="database",
    )
    system = "Return the next browser action."
    prompt = {"view": {"url": "https://example.test"}, "context": {"goal": "Inspect the page."}}
    schema = {"name": "action", "schema": {"type": "object"}}
    raw_image = b"exact-screenshot-bytes"
    image = {
        "media_type": "image/jpeg",
        "data": base64.b64encode(raw_image).decode("ascii"),
    }

    result = call_model_json(
        provider,
        purpose="vision_agent_step",
        system=system,
        prompt=prompt,
        schema=schema,
        image=image,
    )

    metadata = result["metadata"]
    canonical = lambda value: json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    assert result["ok"] is True
    assert metadata["transport_boundary"] == "provider_http_json_body_before_serialization"
    assert metadata["provider_request_body_prepared"] is True
    assert metadata["provider_http_json_body_sha256"] == hashlib.sha256(canonical(posted["json"])).hexdigest()
    assert metadata["provider_payload_system_sha256"] == hashlib.sha256(system.encode("utf-8")).hexdigest()
    assert metadata["provider_payload_prompt_sha256"] == hashlib.sha256(canonical(prompt)).hexdigest()
    assert metadata["schema_requested"] is True
    assert metadata["provider_requested_schema_sha256"] == hashlib.sha256(canonical(schema)).hexdigest()
    assert metadata["schema_included"] is False
    assert metadata["provider_payload_schema_sha256"] is None
    assert metadata["provider_payload_image_sha256"] == hashlib.sha256(raw_image).hexdigest()
    assert metadata["image_requested"] is True
    assert metadata["image_included"] is True
    assert metadata["provider_type_at_transport"] == "custom_openai_compatible"
    assert any(
        block.get("type") == "image_url"
        for block in posted["json"]["messages"][-1]["content"]
        if isinstance(block, dict)
    )


def test_lumen_model_client_uses_active_custom_provider_for_kept_field_answer(monkeypatch) -> None:
    posted: dict = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        posted["url"] = url
        posted["headers"] = headers or {}
        posted["json"] = json or {}
        posted["timeout"] = timeout
        content = {
            "answer": "I have built reliable software systems and applied AI workflows.",
            "confidence": 0.91,
            "rationale": "Selected provider returned a grounded field answer.",
            "needs_followup": False,
            "source": "custom_provider_answer",
        }
        return FakeResponse({"choices": [{"message": {"content": json_module.dumps(content)}}], "usage": {"total_tokens": 12}})

    json_module = json
    monkeypatch.setattr("app.model_provider._http_post", fake_post)

    with SessionLocal() as db:
        service = ModelProviderService(db)
        provider = service.create_provider(
            {
                "display_name": "Custom Active",
                "provider_type": "custom_openai_compatible",
                "model_name": "custom-reasoner",
                "base_url": "https://example.test/v1",
                "api_key": "sk-custom-active",
                "temperature": 0.2,
                "max_tokens": 256,
                "timeout_seconds": 7,
                "enabled": True,
            }
        )
        db.flush()
        service.activate_provider(provider.id)
        db.commit()

        result = LumenModelClient(db).answer_application_field(
            {
                "field_key": "role_interest",
                "label": "Why are you interested in this role?",
                "profile": {"summary": "Software engineer building reliable software systems and applied AI workflows."},
                "resume_facts": {"resume_text": "Built reliable software systems and applied AI workflows."},
                "job": {"title": "Software Engineer"},
            },
            require_live_model=True,
            block_on_openai_error=True,
        )

    assert result["answer"].startswith("I have built reliable")
    assert result["metadata"]["provider"] == "custom_openai_compatible"
    assert result["metadata"]["model"] == "custom-reasoner"
    assert posted["url"] == "https://example.test/v1/chat/completions"
    assert posted["json"]["model"] == "custom-reasoner"
    assert "sk-custom-active" not in json.dumps(result)


def test_application_proposal_repair_returns_only_model_selected_evidence(monkeypatch) -> None:
    client = LumenModelClient()
    captured: dict = {}

    def fake_call(**kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "data": {
                "decision": "propose",
                "answer": "I'm interested in using my full-stack experience to build reliable enablement tools.",
                "supporting_entry_indexes": [2, 0, 99, 2],
                "rationale": "The candidate evidence supports the professional draft.",
            },
            "metadata": {"provider": "test", "model": "proposal-repair"},
        }

    monkeypatch.setattr(client, "_call_model_json", fake_call)
    entries = [
        {"source": "page", "label": "question", "value": "What interests you about this role?"},
        {"source": "resume", "label": "unrelated", "value": "Unrelated evidence."},
        {"source": "resume", "label": "experience", "value": "Built reliable full-stack applications and backend APIs."},
    ]

    result = client.repair_application_answer_proposal(
        question="What interests you about this role?",
        rejected_draft="An earlier unsupported draft.",
        validation_feedback="Remove unsupported employer claims.",
        entries=entries,
    )

    assert result["decision"] == "propose"
    assert result["supporting_entry_indexes"] == [2, 0]
    assert [item["label"] for item in result["supporting_entries"]] == ["experience", "question"]
    assert captured["purpose"] == "application_proposal_repair"
    assert "Non-factual interest" in captured["prompt"]["task"]


def test_application_proposal_repair_prompt_authorizes_location_world_knowledge(monkeypatch) -> None:
    client = LumenModelClient()
    captured: dict = {}

    def fake_call(**kwargs):
        captured.update(kwargs)
        return {
            "ok": True,
            "data": {
                "decision": "propose",
                "answer": "Yes",
                "supporting_entry_indexes": [0],
                "rationale": "The stored location is inside the New York City metro area, so the primary branch is true.",
            },
            "metadata": {"provider": "test", "model": "proposal-repair"},
        }

    monkeypatch.setattr(client, "_call_model_json", fake_call)

    result = client.repair_application_answer_proposal(
        question=(
            "Are you currently located in New York, Washington DC, or the Bay Area? "
            "If no, are you open to relocation?"
        ),
        rejected_draft="Yes",
        validation_feedback="The proposed value 'Yes' was not literally supported by a candidate entry.",
        entries=[
            {"source": "profile", "label": "location", "value": "Jersey City, NJ"},
            {"source": "profile", "label": "relocation_preference", "value": "Open to relocation"},
        ],
    )

    assert result["decision"] == "propose"
    assert result["answer"] == "Yes"
    assert result["supporting_entry_indexes"] == [0]
    task = captured["prompt"]["task"]
    assert "uncontroversial general world knowledge is not inventing" in task
    assert "named metro area, region, state, or country" in task
    assert "conditional follow-up moot" in task


def test_application_proposal_repair_accepts_unambiguous_compatible_envelope(monkeypatch) -> None:
    client = LumenModelClient()
    monkeypatch.setattr(
        client,
        "_call_model_json",
        lambda **kwargs: {
            "ok": True,
            "data": {
                "propose": "I'm interested in applying my backend and full-stack experience to this role.",
                "supporting_entry_indexes": [1],
            },
            "metadata": {"provider": "compatible", "model": "proposal-repair"},
        },
    )

    result = client.repair_application_answer_proposal(
        question="Why are you interested in this role?",
        rejected_draft="",
        validation_feedback="",
        entries=[
            {"source": "page", "label": "question", "value": "Why are you interested in this role?"},
            {"source": "resume", "label": "experience", "value": "Built backend APIs and full-stack applications."},
        ],
    )

    assert result["decision"] == "propose"
    assert result["answer"].startswith("I'm interested")
    assert result["supporting_entry_indexes"] == [1]


def test_application_proposal_repair_accepts_single_nested_evidence_aliases(monkeypatch) -> None:
    client = LumenModelClient()
    monkeypatch.setattr(
        client,
        "_call_model_json",
        lambda **kwargs: {
            "ok": True,
            "data": {
                "proposal": {
                    "draft": "I would enjoy bringing my API and platform experience to this team.",
                    "evidence_indices": [1],
                }
            },
            "metadata": {"provider": "compatible", "model": "proposal-repair"},
        },
    )

    result = client.repair_application_answer_proposal(
        question="Why this role?",
        rejected_draft="",
        validation_feedback="",
        entries=[
            {"source": "page", "label": "question", "value": "Why this role?"},
            {"source": "resume", "label": "experience", "value": "Built APIs and internal platforms."},
        ],
    )

    assert result["decision"] == "propose"
    assert result["supporting_entry_indexes"] == [1]


def test_openrouter_context_window_comes_from_provider_catalog(monkeypatch) -> None:
    # The provider catalog beats any hardcoded model table: new models report their real
    # context window without anyone maintaining a list, and unknown stays unknown.
    from app import model_provider

    model_provider._OPENROUTER_MODEL_CONTEXT = None
    calls: list[str] = []

    class _CatalogResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "data": [
                    {"id": "openai/gpt-5.6-luna", "context_length": 400000},
                    {"id": "other/model", "context_length": 32768},
                    {"id": "broken/model", "context_length": "not-a-number"},
                ]
            }

    monkeypatch.setattr(
        model_provider,
        "_http_get",
        lambda url, **kwargs: calls.append(str(url)) or _CatalogResponse(),
    )
    try:
        assert model_provider._openrouter_context_length("https://openrouter.ai/api/v1", "openai/gpt-5.6-luna") == 400000
        # The catalog is fetched once and cached; a second model resolves from the table.
        assert model_provider._openrouter_context_length("https://openrouter.ai/api/v1", "other/model") == 32768
        assert model_provider._openrouter_context_length("https://openrouter.ai/api/v1", "never/listed") is None
        assert len(calls) == 1

        provider = ResolvedModelProvider(
            id=91,
            display_name="OpenRouter - GPT-5.6 Luna",
            provider_type="openrouter",
            model_name="openai/gpt-5.6-luna",
            base_url="https://openrouter.ai/api/v1",
            api_key="test-only-key",
            temperature=0.2,
            max_tokens=2048,
            timeout_seconds=30,
            enabled=True,
            is_active=True,
            source="database",
        )
        metadata = model_provider._metadata(provider, latency_ms=5)
        assert metadata["context_window_tokens"] == 400000
        assert metadata["context_window_known"] is True
        assert metadata["context_window_source"] == "provider_catalog:openrouter"
    finally:
        model_provider._OPENROUTER_MODEL_CONTEXT = None


def test_reasoning_off_becomes_the_least_effort_only_where_the_catalog_makes_reasoning_mandatory(monkeypatch) -> None:
    # Gemini 3.8 Flash and GLM 5.3 Flash answer reasoning.enabled=false with 400 "Reasoning is
    # mandatory", which failed every gate closed; they get their least-reasoning tier instead.
    from app import model_provider

    model_provider._OPENROUTER_MODEL_CONTEXT = None

    class _CatalogResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "data": [
                    {"id": "z-ai/glm-5.3-flash", "reasoning": {"mandatory": True, "supported_efforts": ["max", "high", "low"]}},
                    {"id": "google/gemini-3.8-flash", "reasoning": {"mandatory": True, "supported_efforts": ["high", "medium", "low"]}},
                    {"id": "mandatory/no-tiers", "reasoning": {"mandatory": True}},
                    {"id": "openai/gpt-6-luna", "reasoning": {"mandatory": False, "supported_efforts": ["medium", "low", "none"]}},
                    {"id": "xiaomi/mimo-v2.6-flash", "reasoning": {"mandatory": False}},
                ]
            }

    monkeypatch.setattr(model_provider, "_http_get", lambda url, **kwargs: _CatalogResponse())
    base = "https://openrouter.ai/api/v1"
    try:
        assert model_provider._openrouter_reasoning_off(base, "z-ai/glm-5.3-flash") == {"effort": "low"}
        assert model_provider._openrouter_reasoning_off(base, "Google/Gemini-3.8-Flash") == {"effort": "low"}
        assert model_provider._openrouter_reasoning_off(base, "mandatory/no-tiers") is None
        assert model_provider._openrouter_reasoning_off(base, "openai/gpt-6-luna") == {"enabled": False}
        assert model_provider._openrouter_reasoning_off(base, "xiaomi/mimo-v2.6-flash") == {"enabled": False}
        assert model_provider._openrouter_reasoning_off(base, "never/listed") == {"enabled": False}
    finally:
        model_provider._OPENROUTER_MODEL_CONTEXT = None


def test_openrouter_context_catalog_failure_stays_unknown_and_backs_off(monkeypatch) -> None:
    from app import model_provider

    model_provider._OPENROUTER_MODEL_CONTEXT = None
    calls: list[str] = []

    def broken_get(url, **kwargs):
        calls.append(str(url))
        raise RuntimeError("catalog offline")

    monkeypatch.setattr(model_provider, "_http_get", broken_get)
    try:
        assert model_provider._openrouter_context_length("https://openrouter.ai/api/v1", "openai/gpt-5.6-luna") is None
        # Within the retry window the failure is cached - no per-call refetch storm.
        assert model_provider._openrouter_context_length("https://openrouter.ai/api/v1", "openai/gpt-5.6-luna") is None
        assert len(calls) == 1
    finally:
        model_provider._OPENROUTER_MODEL_CONTEXT = None


def test_a_new_model_can_reuse_a_saved_key_on_the_same_service_only() -> None:
    secret = "sk-or-test-saved-key"
    with SessionLocal() as db:
        service = ModelProviderService(db)
        source = service.create_provider(
            {
                "display_name": "OpenRouter brain",
                "provider_type": "openrouter",
                "model_name": "openai/gpt-6-luna",
                "api_key": secret,
            }
        )
        db.commit()
        source_id = source.id
    with TestClient(app) as client:
        created = client.post(
            "/model-providers",
            json={
                "display_name": "OpenRouter adjudicator",
                "provider_type": "openrouter",
                "model_name": "anthropic/claude-haiku-4.5",
                "key_from_provider_id": source_id,
            },
        )
        assert created.status_code == 200, created.text
        assert secret not in created.text
        assert created.json()["provider"]["has_api_key"] is True
        other_service = client.post(
            "/model-providers",
            json={
                "display_name": "Elsewhere",
                "provider_type": "custom_openai_compatible",
                "model_name": "some-model",
                "base_url": "https://example.test/v1",
                "key_from_provider_id": source_id,
            },
        )
        assert other_service.status_code == 400
        assert secret not in other_service.text
    with SessionLocal() as db:
        adjudicator = db.scalar(
            select(models.ModelProvider).where(models.ModelProvider.model_name == "anthropic/claude-haiku-4.5")
        )
        assert ModelProviderService(db)._decrypt_api_key(adjudicator) == secret


def test_structured_output_mode_is_stored_per_provider_and_defaults_to_strict() -> None:
    with SessionLocal() as db:
        service = ModelProviderService(db)
        chosen = service.create_provider(
            {"display_name": "json object brain", "provider_type": "openrouter", "model_name": "vendor/model-a",
             "api_key": "sk-or-test-mode", "structured_output": "JSON_OBJECT"}
        )
        default = service.create_provider(
            {"display_name": "strict brain", "provider_type": "openrouter", "model_name": "vendor/model-b",
             "api_key": "sk-or-test-mode", "structured_output": "whatever"}
        )
        db.flush()
        assert service.resolve_provider(chosen).structured_output == "json_object"
        assert service.resolve_provider(default).structured_output is None
        assert service.public_provider(chosen)["structured_output"] == "json_object"
        assert service.public_provider(default)["structured_output"] == "strict"
        db.rollback()
