from app.lumen_model import LumenModelClient
from app.db import SessionLocal, init_db


def test_pending_reply_keeps_source_delegation_out_of_answer_memory(monkeypatch) -> None:
    init_db()
    with SessionLocal() as db:
        client = LumenModelClient(db)
        monkeypatch.setattr(LumenModelClient, "live_enabled", property(lambda self: True))
        monkeypatch.setattr(
            client,
            "_call_model_json",
            lambda **kwargs: {
                "ok": True,
                "data": {
                    "route": "instruction",
                    "answer_value": "",
                    "matched_option": "",
                    "workflow_instruction": "Draft a truthful answer from relevant resume evidence and ask me to approve it.",
                    "memory_intent": "current_answer",
                    "profile_key": "",
                    "profile_value": "",
                    "confidence": 0.98,
                },
                "metadata": {"provider": "compatible"},
            },
        )

        result = client.interpret_pending_user_reply(
            {
                "question_type": "field_approval",
                "question_text": "How have you applied AI in your engineering work?",
                "visible_options": [],
                "user_reply": "Can you write this based on my resume?",
            }
        )

    assert result["ok"] is True
    assert result["route"] == "instruction"
    assert result["answer_value"] == ""
    assert result["workflow_instruction"].startswith("Draft a truthful answer")
    assert result["save_scope"] == "one_time"


def test_pending_reply_can_approve_proposal_and_request_future_reuse(monkeypatch) -> None:
    init_db()
    proposed = "I built a document intelligence platform with semantic search and contextual chat, reducing lookup time by nearly 70%."
    with SessionLocal() as db:
        client = LumenModelClient(db)
        monkeypatch.setattr(LumenModelClient, "live_enabled", property(lambda self: True))
        monkeypatch.setattr(
            client,
            "_call_model_json",
            lambda **kwargs: {
                "ok": True,
                "data": {
                    "route": "answer",
                    "answer_value": proposed,
                    "matched_option": "",
                    "workflow_instruction": "",
                    "memory_intent": "profile_memory",
                    "profile_key": "ai_engineering_impact_answer",
                    "profile_value": proposed,
                    "confidence": 0.99,
                },
                "metadata": {"provider": "compatible"},
            },
        )

        result = client.interpret_pending_user_reply(
            {
                "question_type": "field_approval",
                "question_text": "How have you applied AI in your engineering work?",
                "visible_options": [],
                "answer_contract": {"proposed_answer": proposed},
                "user_reply": "Use that answer and remember it for future applications.",
            }
        )

    assert result["ok"] is True
    assert result["route"] == "answer"
    assert result["answer_value"] == proposed
    assert result["memory_intent"] == "profile_memory"
    assert result["profile_value"] == proposed


def test_pending_reply_accepts_complete_semantics_when_compatible_provider_omits_score(monkeypatch) -> None:
    init_db()
    with SessionLocal() as db:
        client = LumenModelClient(db)
        monkeypatch.setattr(LumenModelClient, "live_enabled", property(lambda self: True))
        monkeypatch.setattr(
            client,
            "_call_model_json",
            lambda **kwargs: {
                "ok": True,
                "data": {
                    "route": "answer",
                    "answer_value": "I am not a protected veteran",
                    "matched_option": "I am not a protected veteran",
                    "memory_intent": "profile_memory",
                    "profile_key": "veteran_status",
                    "profile_value": "I am not a protected veteran",
                },
                "metadata": {"provider": "compatible"},
            },
        )

        result = client.interpret_pending_user_reply(
            {
                "question_type": "field_approval",
                "question_text": "Veteran status",
                "visible_options": ["I am not a protected veteran", "Choose not to disclose"],
                "user_reply": "im not a protected veteran and save it to profile",
            }
        )

    assert result["ok"] is True
    assert result["answer_value"] == "I am not a protected veteran"
    assert result["memory_intent"] == "profile_memory"
    assert result["save_scope"] == "permanent"
    assert result["profile_key"] == "veteran_status"
    assert result["confidence"] == 0.9


def test_pending_reply_derives_persistence_scope_from_model_owned_memory_intent(monkeypatch) -> None:
    init_db()
    with SessionLocal() as db:
        client = LumenModelClient(db)
        monkeypatch.setattr(LumenModelClient, "live_enabled", property(lambda self: True))
        monkeypatch.setattr(
            client,
            "_call_model_json",
            lambda **kwargs: {
                "ok": True,
                "data": {
                    "route": "answer",
                    "answer_value": "Remote",
                    "matched_option": "Remote",
                    "memory_intent": "profile_memory",
                    "profile_key": "preferred_work_arrangement",
                    "profile_value": "Remote",
                    "confidence": 0.97,
                    "rationale": "Retain this preference for future use.",
                },
                "metadata": {"provider": "compatible"},
            },
        )

        result = client.interpret_pending_user_reply(
            {
                "question_type": "field_approval",
                "question_text": "Preferred work arrangement",
                "visible_options": ["Remote", "Hybrid"],
                "user_reply": "Remote; keep that in mind going forward.",
            }
        )

    assert result["ok"] is True
    assert result["memory_intent"] == "profile_memory"
    assert result["save_scope"] == "permanent"


def test_pending_reply_retries_once_with_a_short_exact_contract_repair(monkeypatch) -> None:
    init_db()
    calls = []
    with SessionLocal() as db:
        client = LumenModelClient(db)
        monkeypatch.setattr(LumenModelClient, "live_enabled", property(lambda self: True))

        def fake_call(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return {"ok": True, "data": {}, "metadata": {}}
            return {
                "ok": True,
                "data": {
                    "route": "unclear",
                    "answer_value": "No, I don't have a disability",
                    "matched_option": "No, I don't have a disability",
                    "memory_intent": "current_answer",
                    "profile_key": "",
                    "profile_value": "",
                    "confidence": 0.94,
                },
                "metadata": {},
            }

        monkeypatch.setattr(client, "_call_model_json", fake_call)
        result = client.interpret_pending_user_reply(
            {
                "question_type": "field_approval",
                "question_text": "Disability status",
                "visible_options": ["No, I don't have a disability"],
                "user_reply": "no i do not have any disability",
            }
        )

    assert len(calls) == 2
    assert "protocol_repair" in calls[1]["prompt"]
    assert calls[0]["timeout_seconds_override"] == 8
    assert calls[1]["timeout_seconds_override"] in {8, 10}
    assert calls[0]["max_tokens_override"] == 768
    assert calls[1]["max_tokens_override"] in {512, 768}
    assert result["ok"] is True
    assert result["route"] == "answer"
    assert result["answer_value"] == "No, I don't have a disability"
    assert result["memory_intent"] == "current_answer"


def test_pending_reply_surfaces_length_exhaustion_as_provider_output_failure(monkeypatch) -> None:
    init_db()
    calls = []
    with SessionLocal() as db:
        client = LumenModelClient(db)
        monkeypatch.setattr(LumenModelClient, "live_enabled", property(lambda self: True))

        def fake_call(**kwargs):
            calls.append(kwargs)
            return {
                "ok": True,
                "data": {},
                "metadata": {
                    "provider": "openrouter",
                    "model": "reasoning-model",
                    "parse_status": "empty",
                    "finish_reason": "length",
                    "raw_output_empty": True,
                    "latency_ms": 5,
                },
            }

        monkeypatch.setattr(client, "_call_model_json", fake_call)
        result = client.interpret_pending_user_reply(
            {
                "question_type": "field_approval",
                "question_text": "How did you hear about us?",
                "visible_options": [],
                "user_reply": "LinkedIn; remember that for future applications.",
            }
        )

    assert len(calls) == 2
    assert result["ok"] is False
    assert result["failure_kind"] == "provider_output_failure"
    assert result["metadata"]["finish_reason"] == "length"


def test_pending_reply_surfaces_repeated_contract_violation_as_schema_failure(monkeypatch) -> None:
    init_db()
    with SessionLocal() as db:
        client = LumenModelClient(db)
        monkeypatch.setattr(LumenModelClient, "live_enabled", property(lambda self: True))
        monkeypatch.setattr(
            client,
            "_call_model_json",
            lambda **_kwargs: {
                "ok": True,
                "data": {"route": "answer"},
                "metadata": {"parse_status": "json"},
            },
        )
        result = client.interpret_pending_user_reply(
            {
                "question_type": "field_approval",
                "question_text": "How did you hear about us?",
                "visible_options": [],
                "user_reply": "LinkedIn",
            }
        )

    assert result["ok"] is False
    assert result["failure_kind"] == "schema_invalid"


def test_pending_reply_preserves_a_valid_model_owned_unclear_route(monkeypatch) -> None:
    init_db()
    calls = []
    with SessionLocal() as db:
        client = LumenModelClient(db)
        monkeypatch.setattr(LumenModelClient, "live_enabled", property(lambda self: True))

        def fake_call(**kwargs):
            calls.append(kwargs)
            return {
                "ok": True,
                "data": {
                    "route": "unclear",
                    "answer_value": "",
                    "matched_option": "",
                    "workflow_instruction": "",
                    "memory_intent": "current_answer",
                    "profile_key": "",
                    "profile_value": "",
                    "confidence": 0.9,
                },
                "metadata": {"parse_status": "json"},
            }

        monkeypatch.setattr(client, "_call_model_json", fake_call)
        result = client.interpret_pending_user_reply(
            {
                "question_type": "field_approval",
                "question_text": "Which location do you prefer?",
                "visible_options": [],
                "user_reply": "Either one is fine.",
            }
        )

    assert len(calls) == 1
    assert result["ok"] is False
    assert result["failure_kind"] == "semantic_unclear"


def test_profile_memory_contract_is_repaired_without_phrase_matching(monkeypatch) -> None:
    init_db()
    calls = []
    with SessionLocal() as db:
        client = LumenModelClient(db)
        monkeypatch.setattr(LumenModelClient, "live_enabled", property(lambda self: True))

        def fake_call(**kwargs):
            calls.append(kwargs["prompt"])
            data = {
                "route": "answer",
                "answer_value": "Remote",
                "matched_option": "Remote",
                "memory_intent": "profile_memory",
                "profile_key": "",
                "profile_value": "",
                "confidence": 0.94,
                "rationale": "Retain this preference for later.",
            }
            if len(calls) == 2:
                data.update({"profile_key": "work_location_preference", "profile_value": "Remote"})
            return {"ok": True, "data": data, "metadata": {}}

        monkeypatch.setattr(client, "_call_model_json", fake_call)
        result = client.interpret_pending_user_reply(
            {
                "question_type": "field_approval",
                "question_text": "Preferred work arrangement",
                "visible_options": ["Remote", "Hybrid"],
                "user_reply": "Remote; keep that as part of what you know about me.",
            }
        )

    assert len(calls) == 2
    assert "future-retention intent from the full meaning" in calls[0]["instruction"]
    assert "missing its stable profile_key" in calls[1]["protocol_repair"]
    assert result["ok"] is True
    assert result["memory_intent"] == "profile_memory"
    assert result["save_scope"] == "permanent"
    assert result["profile_key"] == "work_location_preference"
    assert result["profile_value"] == "Remote"


def test_rich_fact_is_preserved_separately_from_broader_form_option(monkeypatch) -> None:
    init_db()
    calls = []
    with SessionLocal() as db:
        client = LumenModelClient(db)
        monkeypatch.setattr(LumenModelClient, "live_enabled", property(lambda self: True))

        def fake_call(**kwargs):
            calls.append(kwargs["prompt"])
            return {
                "ok": True,
                "data": {
                    "route": "answer",
                    "answer_value": "More than 7 years of professional software engineering experience",
                    "matched_option": "5+",
                    "memory_intent": "profile_memory",
                    "profile_key": "professional_software_engineering_experience",
                    "profile_value": "More than 7 years",
                    "confidence": 0.98,
                },
                "metadata": {},
            }

        monkeypatch.setattr(client, "_call_model_json", fake_call)
        result = client.interpret_pending_user_reply(
            {
                "question_type": "field_approval",
                "question_text": "How many years of professional software engineering experience do you have?",
                "visible_options": ["0", "1", "2", "3", "4", "5+"],
                "user_reply": "I have more than 7 years of software engineering experience; remember it.",
            }
        )

    assert len(calls) == 1
    assert result["ok"] is True
    assert result["answer_value"] == "More than 7 years of professional software engineering experience"
    assert result["matched_option"] == "5+"
    assert result["profile_value"] == "More than 7 years"
