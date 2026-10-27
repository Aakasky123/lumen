"""Fast judge-model routing: bounded text-only verdicts run on the user-designated
judge provider, the Checker seat (agent_settings.judge_provider_id); routing is opt-in and never a new
failure mode (failed judge calls retry once on the default provider)."""


from types import SimpleNamespace

from app.lumen_model import LumenModelClient, JUDGE_ROUTED_PURPOSES
from app.model_provider import ModelProviderService, ResolvedModelProvider


def _provider(
    *,
    provider_id: int,
    model_name: str,
    max_tokens: int = 2048,
    timeout_seconds: int = 60,
) -> ResolvedModelProvider:
    return ResolvedModelProvider(
        id=provider_id,
        display_name=f"provider-{provider_id}",
        provider_type="openrouter",
        model_name=model_name,
        base_url="https://example.test/v1",
        api_key="test-key",
        temperature=0.2,
        max_tokens=max_tokens,
        timeout_seconds=timeout_seconds,
        enabled=True,
        is_active=True,
        source="database",
    )


JUDGE = _provider(provider_id=7, model_name="fast-judge-model")
ACTIVE = _provider(provider_id=1, model_name="frontier-model")

_SCHEMA = {"name": "verdict", "schema": {"type": "object"}}


def _client(monkeypatch, *, judge, calls):
    client = LumenModelClient()
    monkeypatch.setattr(client.provider_service, "judge_provider", lambda **_: judge)
    monkeypatch.setattr(client.provider_service, "active_provider", lambda: ACTIVE)

    def fake_call_model_json(provider, **kwargs):
        calls.append({"provider": provider, "kwargs": kwargs})
        return {"ok": True, "data": {}, "metadata": {"model": provider.model_name}}

    monkeypatch.setattr("app.lumen_model.call_model_json", fake_call_model_json)
    return client


def test_judge_purpose_routes_to_designated_judge_provider(monkeypatch) -> None:
    calls: list[dict] = []
    client = _client(monkeypatch, judge=JUDGE, calls=calls)
    result = client._call_model_json(
        purpose="target_binding_judge",
        system="judge",
        prompt={"task": "verify"},
        schema=_SCHEMA,
    )
    assert result["ok"] is True
    assert len(calls) == 1
    assert calls[0]["provider"].model_name == "fast-judge-model"
    assert result["metadata"]["judge_provider_routed"] is True


def test_judge_purpose_keeps_active_provider_without_designation(monkeypatch) -> None:
    calls: list[dict] = []
    client = _client(monkeypatch, judge=None, calls=calls)
    result = client._call_model_json(
        purpose="target_binding_judge",
        system="judge",
        prompt={"task": "verify"},
        schema=_SCHEMA,
    )
    assert result["ok"] is True
    assert calls[0]["provider"].model_name == "frontier-model"
    assert "judge_provider_routed" not in (result.get("metadata") or {})


def test_generative_purposes_never_route_to_judge_provider(monkeypatch) -> None:
    for purpose in ("application_proposal_repair", "pending_user_reply_interpretation", "candidate_evidence_retrieval"):
        calls: list[dict] = []
        client = _client(monkeypatch, judge=JUDGE, calls=calls)
        client._call_model_json(purpose=purpose, system="s", prompt={}, schema=_SCHEMA)
        assert calls[0]["provider"].model_name == "frontier-model", purpose
        assert purpose not in JUDGE_ROUTED_PURPOSES


def test_explicit_provider_bypasses_judge_routing(monkeypatch) -> None:
    calls: list[dict] = []
    client = _client(monkeypatch, judge=JUDGE, calls=calls)
    pinned = _provider(provider_id=3, model_name="pinned-vision-model")
    client._call_model_json(
        purpose="grounding_judge",
        system="judge",
        prompt={"task": "grade"},
        schema=_SCHEMA,
        provider=pinned,
    )
    assert calls[0]["provider"].model_name == "pinned-vision-model"


def test_judge_failure_falls_back_to_default_provider(monkeypatch) -> None:
    calls: list[dict] = []
    client = LumenModelClient()
    monkeypatch.setattr(client.provider_service, "judge_provider", lambda **_: JUDGE)
    monkeypatch.setattr(client.provider_service, "active_provider", lambda: ACTIVE)

    def fake_call_model_json(provider, **kwargs):
        calls.append(provider.model_name)
        if provider.model_name == "fast-judge-model":
            return {"ok": False, "data": {}, "error": "judge provider down", "metadata": {}}
        return {"ok": True, "data": {"matches": True}, "metadata": {"model": provider.model_name}}

    monkeypatch.setattr("app.lumen_model.call_model_json", fake_call_model_json)
    result = client._call_model_json(
        purpose="payment_floor_judge",
        system="judge",
        prompt={"task": "verify"},
        schema=_SCHEMA,
    )
    assert calls == ["fast-judge-model", "frontier-model"]
    assert result["ok"] is True
    assert result["metadata"]["judge_provider_fallback"] is True


def test_judge_failure_with_same_active_provider_does_not_retry(monkeypatch) -> None:
    calls: list[dict] = []
    client = LumenModelClient()
    monkeypatch.setattr(client.provider_service, "judge_provider", lambda **_: ACTIVE)
    monkeypatch.setattr(client.provider_service, "active_provider", lambda: ACTIVE)

    def fake_call_model_json(provider, **kwargs):
        calls.append(provider.model_name)
        return {"ok": False, "data": {}, "error": "down", "metadata": {}}

    monkeypatch.setattr("app.lumen_model.call_model_json", fake_call_model_json)
    result = client._call_model_json(
        purpose="claim_support_judge",
        system="judge",
        prompt={"task": "verify"},
        schema=_SCHEMA,
    )
    assert calls == ["frontier-model"]
    assert result.get("blocked") is True  # selected_from_ui failure keeps its existing shape


def test_vision_step_with_image_never_routes_to_judge(monkeypatch) -> None:
    calls: list[dict] = []
    client = _client(monkeypatch, judge=JUDGE, calls=calls)
    vision = _provider(provider_id=5, model_name="vision-model")
    monkeypatch.setattr(client.provider_service, "vision_provider", lambda **_: vision)
    client._call_model_json(
        purpose="vision_agent_step",
        system="see",
        prompt={"goal": "g"},
        schema=_SCHEMA,
        image={"media_type": "image/jpeg", "data": "B64"},
    )
    assert calls[0]["provider"].model_name == "vision-model"


def test_judge_provider_service_returns_none_without_designation() -> None:
    service = ModelProviderService(db=None)
    assert service.judge_provider(user_id=1) is None


def test_judge_provider_service_resolves_designated_provider(monkeypatch) -> None:
    service = ModelProviderService(db=object())
    monkeypatch.setattr(
        "app.agent_settings.get_agent_settings",
        lambda db, user_id: {"judge_provider_id": 7},
    )
    monkeypatch.setattr(service, "provider_by_id", lambda pid: JUDGE if pid == 7 else None)
    resolved = service.judge_provider(user_id=1)
    assert resolved is JUDGE


def test_judge_provider_service_ignores_bad_designation(monkeypatch) -> None:
    service = ModelProviderService(db=object())
    monkeypatch.setattr(
        "app.agent_settings.get_agent_settings",
        lambda db, user_id: {"judge_provider_id": "not-an-id"},
    )
    assert service.judge_provider(user_id=1) is None


# --- The Checker seat: the grounding judge and the secret adjudicator are the judge row -------------

CHECKER = _provider(provider_id=21, model_name="anthropic/claude-haiku-5.5")


def _checker_service(monkeypatch, *, settings: dict, rows: dict | None = None) -> ModelProviderService:
    service = ModelProviderService(db=object())
    monkeypatch.setattr("app.agent_settings.get_agent_settings", lambda db, user_id: settings)
    table = rows if rows is not None else {7: JUDGE, 21: CHECKER}
    monkeypatch.setattr(service, "provider_by_id", lambda pid: table.get(pid))
    return service


def test_the_grounding_judge_and_the_adjudicator_are_the_checker(monkeypatch) -> None:
    service = _checker_service(monkeypatch, settings={"judge_provider_id": 21})
    assert service.judge_provider(user_id=1) is CHECKER
    assert service.grounding_judge_provider(user_id=1) is CHECKER
    assert service.secret_adjudicator_provider(user_id=1) is CHECKER
    # No Checker: nothing (callers keep the vision brain / active provider).
    service = _checker_service(monkeypatch, settings={"judge_provider_id": None})
    assert service.grounding_judge_provider(user_id=1) is None
    assert service.secret_adjudicator_provider(user_id=1) is None
    assert ModelProviderService(db=None).grounding_judge_provider(user_id=1) is None


def test_a_disabled_or_bad_checker_designation_is_unavailable(monkeypatch) -> None:
    # A designated row that is disabled is unavailable (provider_by_id refuses it); it never silently
    # falls back to another row.
    class _Db:
        def get(self, _model, _pid):
            return SimpleNamespace(enabled=False)

    service = ModelProviderService(db=_Db())
    monkeypatch.setattr("app.agent_settings.get_agent_settings", lambda db, user_id: {"judge_provider_id": 21})
    assert service.judge_provider(user_id=1) is None
    assert service.grounding_judge_provider(user_id=1) is None
    service = _checker_service(monkeypatch, settings={"judge_provider_id": "not-an-id"})
    assert service.grounding_judge_provider(user_id=1) is None


def test_every_judge_purpose_routes_to_the_checker_screenshots_included(monkeypatch) -> None:
    calls: list[dict] = []
    client = _client(monkeypatch, judge=CHECKER, calls=calls)
    for purpose in sorted(JUDGE_ROUTED_PURPOSES):
        calls.clear()
        client._call_model_json(purpose=purpose, system="judge", prompt={"task": "verify"}, schema=_SCHEMA)
        assert calls[0]["provider"].model_name == "anthropic/claude-haiku-5.5", purpose
    # With the screenshot too: the Checker reads it (an openrouter row can carry an image).
    calls.clear()
    client._call_model_json(
        purpose="grounding_judge",
        system="judge",
        prompt={"task": "verify"},
        schema=_SCHEMA,
        image={"media_type": "image/jpeg", "data": "B64"},
    )
    assert calls[0]["provider"].model_name == "anthropic/claude-haiku-5.5"
    assert calls[0]["kwargs"].get("image")
    # Session summaries run on the Checker as well.
    calls.clear()
    client.compress_session_context(goal="g", summary="", records=[{"step": 1, "action": "click"}])
    assert calls and all(call["provider"].model_name == "anthropic/claude-haiku-5.5" for call in calls)
