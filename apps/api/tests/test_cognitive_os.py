import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from goal_test_helpers import create_operator_goal_response, create_run_thread
from app import models, user_memory
from app.lumen_model import LumenModelClient
from app.cognition import OperatorChatService
from app.db import SessionLocal
from app.main import app
from app.runtime import OperatorRuntimeEngine


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    frames: list[tuple[str, dict]] = []
    for block in text.split("\n\n"):
        event = None
        data_lines: list[str] = []
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].lstrip())
        if event and data_lines:
            frames.append((event, json.loads("\n".join(data_lines))))
    return frames


def post_chat(client, thread_id, content, payload=None) -> dict:
    """POST a chat message and reassemble the streamed SSE frames into the legacy body dict."""
    request_body = {"content": content}
    if payload is not None:
        request_body["payload"] = payload
    response = client.post(f"/operator/chat/threads/{thread_id}/messages", json=request_body)
    assert response.status_code == 200, response.text
    meta: dict = {}
    done: dict = {}
    tokens: list[str] = []
    for event, data in _parse_sse(response.text):
        if event == "meta":
            meta = data
        elif event == "token":
            tokens.append(data.get("text", ""))
        elif event == "done":
            done = data
        elif event == "error":
            raise AssertionError(f"chat stream error: {data}")
    return {
        "user_message": meta.get("user_message"),
        "assistant_message": done.get("assistant_message"),
        "context": done.get("context"),
        "chat_scope": meta.get("chat_scope"),
        "pending_question": meta.get("pending_question"),
        "application_request": meta.get("application_request"),
        "run_control": meta.get("run_control"),
        "runtime_correction": meta.get("runtime_correction"),
        "question_answer": meta.get("question_answer"),
        "chat_response": done.get("chat_response"),
        "streamed_text": "".join(tokens),
    }


def test_chat_user_message_is_committed_before_model_scope_inference(monkeypatch) -> None:
    with TestClient(app) as client:
        thread_response = client.post("/operator/chat/threads", json={})
        assert thread_response.status_code == 200, thread_response.text
        thread_id = thread_response.json()["id"]

    observed: dict[str, object] = {}

    with SessionLocal() as db:
        service = OperatorChatService(db)

        def inspect_from_independent_session(_thread, content, **_kwargs):
            with SessionLocal() as reader:
                persisted = reader.scalars(
                    select(models.ChatMessage).where(
                        models.ChatMessage.thread_id == thread_id,
                        models.ChatMessage.role == "user",
                        models.ChatMessage.content == content,
                    )
                ).all()
                observed["message_ids"] = [message.id for message in persisted]
            return {
                "action": "none",
                "primary_intent": "other",
                "in_scope": True,
                "run_related": False,
                "approval_related": False,
                "out_of_scope": False,
                "confidence": 0.9,
                "metadata": {"provider": "test"},
            }

        monkeypatch.setattr(service, "_classify_chat_scope", inspect_from_independent_session)
        turn = service._prepare_chat_turn(
            thread_id=thread_id,
            content="Please inspect this durable turn.",
            payload={"source": "test"},
        )
        turn_user_message_id = turn["user_message"].id

    assert observed["message_ids"] == [turn_user_message_id]


def test_operator_chat_explains_stuck_run_and_applies_user_answer_to_memory() -> None:
    with TestClient(app) as client:
        created = create_operator_goal_response({"job_url": "https://jobs.lever.co/cognitive/1", "auto_start": False})
        assert created.status_code == 200, created.text
        run_id = created.json()["id"]

    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        run.current_state = "WAITING_FOR_USER"
        run.status_message = "Need salary expectation before continuing."
        db.commit()

    with TestClient(app) as client:
        thread_id = create_run_thread(run_id)
        body = post_chat(client, thread_id, "What are you stuck on?")
        assert body["pending_question"]["stuck_reason"] == "Need salary expectation before continuing."
        assert "Need salary expectation" in body["assistant_message"]["content"]

        answered = client.post(
            f"/operator/questions/{body['pending_question']['id']}/answer",
            json={"answer": "$140k base target, flexible for equity", "payload": {}, "auto_resume": False},
        )
        assert answered.status_code == 200, answered.text
        assert answered.json()["question"]["status"] == "applied"
        events = client.get(f"/operator/runs/{run_id}/events").json()
        assert "OPERATOR_ANSWER_APPLIED" in [event["event_type"] for event in events]

    with SessionLocal() as db:
        # The answer lives on the question row and in the event stream; no per-answer
        # memory blob is written anymore (durable user facts flow through user_memory).
        question = db.scalars(
            select(models.OperatorQuestion).where(models.OperatorQuestion.run_id == run_id)
        ).first()
        assert question is not None
        assert "$140k" in str(question.answer_payload)


def test_delete_operator_chat_thread_hides_history_and_detaches_questions() -> None:
    with TestClient(app) as client:
        created = create_operator_goal_response({"job_url": "https://jobs.lever.co/cognitive/delete-thread", "auto_start": False})
        assert created.status_code == 200, created.text
        run_id = created.json()["id"]

    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        run.current_state = "WAITING_FOR_USER"
        run.status_message = "Need one more answer before continuing."
        db.commit()

    with TestClient(app) as client:
        thread_id = create_run_thread(run_id)

    with SessionLocal() as db:
        question = OperatorChatService(db).ensure_question_for_run(run_id, thread_id=thread_id)
        assert question is not None
        assert question.thread_id == thread_id
        question_id = question.id
        db.commit()

    with TestClient(app) as client:
        deleted = client.delete(f"/operator/chat/threads/{thread_id}")
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["status"] == "deleted"
        assert all(item["id"] != thread_id for item in client.get("/operator/chat/threads").json())
        assert client.get(f"/operator/chat/threads/{thread_id}/messages").status_code == 404
        assert client.get(f"/operator/chat/threads?run_id={run_id}").json() == []

    with SessionLocal() as db:
        question = db.get(models.OperatorQuestion, question_id)
        thread = db.get(models.ChatThread, thread_id)
        assert question.thread_id is None
        assert thread.status == "deleted"


def test_global_operator_chat_greets_user_and_explains_agent_role(monkeypatch) -> None:
    def fake_chat_reply(self, payload, *, fallback_answer):
        assert payload["message"] == "hi"
        assert payload["context"]["view_mode"] == "unknown"
        assert payload["chat_scope"]["primary_intent"] == "greeting"
        answer = "Hi, I'm Lumen from the configured model. I help with job applications and resumes."
        yield {"type": "delta", "text": answer}
        yield {"type": "final", "answer": answer, "metadata": {"source": "llm", "provider": "test-provider", "model": "test-chat-model", "latency_ms": 12}}

    monkeypatch.setattr(LumenModelClient, "stream_operator_chat_reply", fake_chat_reply)
    with TestClient(app) as client:
        thread = client.post("/operator/chat/threads", json={})
        assert thread.status_code == 200, thread.text
        thread_id = thread.json()["id"]

        body = post_chat(client, thread_id, "hi")
        assert "configured model" in body["assistant_message"]["content"]
        assert "job applications" in body["assistant_message"]["content"]
        assert body["chat_response"]["source"] == "llm"
        assert body["chat_response"]["provider"] == "test-provider"
        assert body["chat_response"]["scope_classification"]["primary_intent"] == "greeting"
        assert body["application_request"] is None


def test_fresh_chat_is_titled_from_the_first_user_message(monkeypatch) -> None:
    def fake_chat_reply(self, payload, *, fallback_answer):
        answer = "Hi, I'm Lumen."
        yield {"type": "delta", "text": answer}
        yield {"type": "final", "answer": answer, "metadata": {"source": "llm", "provider": "test-provider", "model": "test-chat-model", "latency_ms": 5}}

    monkeypatch.setattr(LumenModelClient, "stream_operator_chat_reply", fake_chat_reply)
    with TestClient(app) as client:
        thread = client.post("/operator/chat/threads", json={})
        assert thread.status_code == 200, thread.text
        assert thread.json()["title"] == "Lumen chat"
        thread_id = thread.json()["id"]

        post_chat(client, thread_id, "hi\nsecond line")
        titles = {item["id"]: item["title"] for item in client.get("/operator/chat/threads").json()}
        assert titles[thread_id] == "hi"

        # Later messages never rename the chat.
        post_chat(client, thread_id, "hello again")
        titles = {item["id"]: item["title"] for item in client.get("/operator/chat/threads").json()}
        assert titles[thread_id] == "hi"


def test_chat_title_redacts_codes_and_stays_one_short_line() -> None:
    from app.cognition import _chat_title_from_message

    assert _chat_title_from_message("  sign me in, OTP 482913  ") == "sign me in, OTP ••••"
    long_title = _chat_title_from_message("Compare the three cheapest nonstop flights from San Francisco to New York next Friday")
    assert len(long_title) <= 60 and long_title.endswith("…")
    assert _chat_title_from_message("\n\n") == ""


def test_operator_chat_identity_question_is_in_scope_and_model_backed(monkeypatch) -> None:
    def fake_chat_reply(self, payload, *, fallback_answer):
        assert payload["chat_scope"]["in_scope"] is True
        assert payload["chat_scope"]["out_of_scope"] is False
        answer = "I'm Lumen. I do tasks on your computer."
        yield {"type": "delta", "text": answer}
        yield {"type": "final", "answer": answer, "metadata": {"source": "llm", "provider": "test-provider", "model": "test-chat-model", "latency_ms": 9}}

    monkeypatch.setattr(LumenModelClient, "stream_operator_chat_reply", fake_chat_reply)
    with TestClient(app) as client:
        thread = client.post("/operator/chat/threads", json={})
        assert thread.status_code == 200, thread.text
        thread_id = thread.json()["id"]

        body = post_chat(client, thread_id, "what is you're name")
        assert body["chat_scope"]["in_scope"] is True
        assert body["chat_scope"]["out_of_scope"] is False
        assert "I'm Lumen" in body["assistant_message"]["content"]
        assert body["chat_response"]["source"] == "llm"


def test_operator_chat_greeting_scope_uses_fast_path(monkeypatch) -> None:
    def fail_model_call(*args, **kwargs):
        raise AssertionError("greeting classification should not call the live model")

    monkeypatch.setattr(LumenModelClient, "_call_model_json", fail_model_call)
    scope = LumenModelClient().classify_operator_chat_scope({"message": "hi", "context": {"view_mode": "user"}})

    assert scope["primary_intent"] == "greeting"
    assert scope["in_scope"] is True
    assert scope["metadata"]["provider"] == "deterministic_fast_path"


def test_operator_chat_capability_question_uses_fast_path(monkeypatch) -> None:
    def fail_model_call(*args, **kwargs):
        raise AssertionError("capability questions should not call the live model")

    monkeypatch.setattr(LumenModelClient, "_call_model_json", fail_model_call)
    scope = LumenModelClient().classify_operator_chat_scope({"message": "what else can you do", "context": {"view_mode": "user"}})

    assert scope["primary_intent"] == "domain_chat"
    assert scope["in_scope"] is True
    assert scope["metadata"]["provider"] == "deterministic_fast_path"


def test_global_operator_chat_starts_application_from_job_link(monkeypatch) -> None:
    original_create_goal = OperatorRuntimeEngine.create_goal

    def fake_create_goal(self, *, job_url: str, goal: str) -> models.OperatorRun:
        run = original_create_goal(self, job_url=job_url, goal=goal)
        run.current_state = "WAITING_FOR_USER"
        run.status_message = "Review before continuing."
        self.db.flush()
        return run

    monkeypatch.setattr(OperatorRuntimeEngine, "create_goal", fake_create_goal)

    with TestClient(app) as client:
        thread = client.post("/operator/chat/threads", json={})
        assert thread.status_code == 200, thread.text
        thread_id = thread.json()["id"]

        body = post_chat(client, thread_id, "Please apply to https://jobs.example.com/software-engineer")
        request = body["application_request"]
        assert request["action"] == "application_started"
        assert request["job_url"] == "https://jobs.example.com/software-engineer"
        assert request["run_id"]
        assert "extension body" in body["assistant_message"]["content"]

    with SessionLocal() as db:
        saved_thread = db.get(models.ChatThread, thread_id)
        assert saved_thread is not None
        assert saved_thread.run_id == request["run_id"]
        assert saved_thread.mode == "run"


def test_operator_chat_emits_run_id_before_requiring_extension_body(monkeypatch) -> None:
    from app.end_to_end_goal import EndToEndGoalService

    def fake_scope(self, thread, content, *, payload, context, pending_question):
        return {"action": "start_application", "in_scope": True, "run_related": False, "out_of_scope": False}

    def fake_start_deferred(self, *, job_url: str) -> dict:
        run = OperatorRuntimeEngine(self.db).create_goal(job_url=job_url, goal=f"Deferred chat application for {job_url}")
        run.status_message = "Queued from chat."
        run.confidence_state = {
            **(run.confidence_state or {}),
            "end_to_end": {"phase": "JOB_INGESTION", "job_url": job_url, "async_execution": {"status": "queued"}},
        }
        self.db.flush()
        return {"run": run, "goal_workspace": run.confidence_state["end_to_end"]}

    monkeypatch.setattr(OperatorChatService, "_classify_chat_scope", fake_scope)
    monkeypatch.setattr(EndToEndGoalService, "start_deferred", fake_start_deferred)

    with SessionLocal() as db:
        user = OperatorRuntimeEngine(db).ensure_default_user()
        service = OperatorChatService(db)
        thread = service.create_thread(user_id=user.id)
        stream = service.stream_message(thread_id=thread.id, content="apply https://jobs.example.com/software-engineer", payload={"source": "user_mode_chat"})

        kind, meta = next(stream)

        assert kind == "meta"
        assert meta["application_request"]["action"] == "application_started"
        assert meta["application_request"]["run_id"]
        assert meta["application_request"]["deferred_application_start"] is True

        kind, _token = next(stream)
        assert kind == "token"

        kind, done = next(stream)
        assert kind == "done"
        assert done["chat_response"]["active_run_id"] == meta["application_request"]["run_id"]
        assert done["chat_response"]["in_browser_required"] is True


def test_operator_chat_refuses_unrelated_general_questions() -> None:
    with TestClient(app) as client:
        thread = client.post("/operator/chat/threads", json={})
        assert thread.status_code == 200, thread.text
        thread_id = thread.json()["id"]

        body = post_chat(client, thread_id, "What is the capital of France?")
        assert body["chat_scope"]["out_of_scope"] is True
        assert "Configure and validate a model provider in Settings" in body["assistant_message"]["content"]


def test_operator_chat_scope_redirect_warms_pleasantry_without_model_call(monkeypatch) -> None:
    def fake_scope(self, thread, content, *, payload, context, pending_question):
        return {"action": "none", "in_scope": False, "run_related": False, "out_of_scope": True, "primary_intent": "out_of_scope"}

    def fail_chat_stream(*args, **kwargs):
        raise AssertionError("scope redirect copy should not call the chat model")

    monkeypatch.setattr(OperatorChatService, "_classify_chat_scope", fake_scope)
    monkeypatch.setattr(LumenModelClient, "stream_operator_chat_reply", fail_chat_stream)

    with TestClient(app) as client:
        thread = client.post("/operator/chat/threads", json={})
        assert thread.status_code == 200, thread.text
        body = post_chat(client, thread.json()["id"], "how are you")

    content = body["assistant_message"]["content"].lower()
    assert body["chat_response"]["source"] == "scope_redirect"
    assert body["chat_response"]["provider"] == "scope_guard"
    assert "ready" in content
    assert "task" in content or "job" in content
    assert "i can only help" not in content


def test_operator_chat_scope_redirect_acknowledges_tone_feedback(monkeypatch) -> None:
    def fake_scope(self, thread, content, *, payload, context, pending_question):
        return {"action": "none", "in_scope": False, "run_related": False, "out_of_scope": True, "primary_intent": "out_of_scope"}

    def fail_chat_stream(*args, **kwargs):
        raise AssertionError("tone redirect copy should not call the chat model")

    monkeypatch.setattr(OperatorChatService, "_classify_chat_scope", fake_scope)
    monkeypatch.setattr(LumenModelClient, "stream_operator_chat_reply", fail_chat_stream)

    with TestClient(app) as client:
        thread = client.post("/operator/chat/threads", json={})
        assert thread.status_code == 200, thread.text
        body = post_chat(client, thread.json()["id"], "ok you dont have to be rude")

    content = body["assistant_message"]["content"].lower()
    assert body["chat_response"]["source"] == "scope_redirect"
    assert "sorry" in content or "abrupt" in content
    assert "task" in content or "correction" in content or "change" in content
    assert "i can only help" not in content


def test_operator_chat_scope_redirect_varies_repeated_off_topic_copy(monkeypatch) -> None:
    def fake_scope(self, thread, content, *, payload, context, pending_question):
        return {"action": "none", "in_scope": False, "run_related": False, "out_of_scope": True, "primary_intent": "out_of_scope"}

    monkeypatch.setattr(OperatorChatService, "_classify_chat_scope", fake_scope)

    with TestClient(app) as client:
        thread = client.post("/operator/chat/threads", json={})
        assert thread.status_code == 200, thread.text
        thread_id = thread.json()["id"]
        first = post_chat(client, thread_id, "What is the capital of France?")
        second = post_chat(client, thread_id, "Write me a poem.")

    assert first["assistant_message"]["content"] != second["assistant_message"]["content"]
    assert first["chat_response"]["provider"] == "scope_guard"
    assert second["chat_response"]["provider"] == "scope_guard"


def test_operator_chat_answers_resume_and_saved_variable_questions() -> None:
    with TestClient(app) as client:
        thread = client.post("/operator/chat/threads", json={})
        assert thread.status_code == 200, thread.text
        thread_id = thread.json()["id"]

        body = post_chat(client, thread_id, "Can you help with my resume and saved variables?")
        assert body["chat_scope"]["in_scope"] is True
        assert body["chat_scope"]["resume_related"] is True
        assert "resume" in body["assistant_message"]["content"].lower()


def test_operator_chat_can_pause_active_run_from_scope_action() -> None:
    with TestClient(app) as client:
        created = create_operator_goal_response({"job_url": "https://jobs.example.com/pause-me", "auto_start": False})
        assert created.status_code == 200, created.text
        run_id = created.json()["id"]

    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        run.current_state = "FILLING_APPLICATION"
        run.status_message = "Runtime is filling the form."
        db.commit()

    with TestClient(app) as client:
        thread_id = create_run_thread(run_id)
        body = post_chat(client, thread_id, "Pause this application for now")
        assert body["chat_scope"]["action"] == "pause"
        assert body["run_control"]["applied"] is True
        assert "Paused" in body["assistant_message"]["content"]

    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        assert run.current_state == "WAITING_FOR_USER"


def test_operator_chat_preserves_job_link_context_for_apply_followup(monkeypatch) -> None:
    original_create_goal = OperatorRuntimeEngine.create_goal

    def fake_create_goal(self, *, job_url: str, goal: str) -> models.OperatorRun:
        run = original_create_goal(self, job_url=job_url, goal=goal)
        run.current_state = "WAITING_FOR_USER"
        run.status_message = "Review before continuing."
        self.db.flush()
        return run

    monkeypatch.setattr(OperatorRuntimeEngine, "create_goal", fake_create_goal)

    with TestClient(app) as client:
        thread = client.post("/operator/chat/threads", json={})
        assert thread.status_code == 200, thread.text
        thread_id = thread.json()["id"]
        detected = post_chat(client, thread_id, "Here is a role: https://jobs.example.com/backend")
        assert detected["application_request"]["action"] == "job_link_detected"

        body = post_chat(client, thread_id, "yes, apply")
        assert body["chat_scope"]["action"] == "start_application"
        assert body["application_request"]["job_url"] == "https://jobs.example.com/backend"
        assert body["application_request"]["run_id"]


def test_operator_chat_can_ingest_job_url_without_starting_run(monkeypatch) -> None:
    from app.job_ingestion import JobIngestionService

    def fake_ingest(self, job_url: str) -> dict:
        user = OperatorRuntimeEngine(self.db).ensure_default_user()
        posting = models.JobPosting(
            user_id=user.id,
            url=job_url,
            company="ExampleCo",
            title="Backend Engineer",
            location="Remote",
            description_text="Build application automation systems.",
            source_platform="generic",
        )
        self.db.add(posting)
        self.db.flush()
        analysis = models.JobAnalysis(job_posting_id=posting.id, ingestion_status="COMPLETED", fit_score=0.81, confidence=0.9)
        self.db.add(analysis)
        self.db.flush()
        return {"job_posting": posting, "job_analysis": analysis}

    monkeypatch.setattr(JobIngestionService, "ingest", fake_ingest)

    with TestClient(app) as client:
        thread = client.post("/operator/chat/threads", json={})
        assert thread.status_code == 200, thread.text
        thread_id = thread.json()["id"]
        body = post_chat(client, thread_id, "Analyze this role https://jobs.example.com/backend")
        assert body["chat_scope"]["action"] == "ingest_job_url"
        assert body["application_request"]["action"] == "job_link_ingested"
        assert body["application_request"]["job_posting_id"]
        assert "Backend Engineer" in body["assistant_message"]["content"]

    with SessionLocal() as db:
        saved_thread = db.get(models.ChatThread, thread_id)
        assert saved_thread.run_id is None


def test_review_stage_model_classified_correction_resumes_same_browser_run(monkeypatch) -> None:
    scheduled: list[int] = []
    monkeypatch.setattr(
        "app.extension_run.schedule_resume_if_in_browser",
        lambda run_id: scheduled.append(run_id) or True,
    )

    with TestClient(app):
        created = create_operator_goal_response(
            {
                "job_url": "https://jobs.example.test/application?utm_source=jobright",
                "auto_start": False,
            }
        )
        assert created.status_code == 200, created.text
        run_id = created.json()["id"]
        thread_id = create_run_thread(run_id)

    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        run.current_state = "READY_FOR_REVIEW"
        run.status_message = "Ready for review."
        run.confidence_state = {
            **(run.confidence_state or {}),
            "in_browser": {
                "url": run.target_url,
                "device_id": "fixture-device",
            },
            "pending_final_action": {
                "label": "Submit Application",
                "question_id": 9001,
                "approval_token": "must-not-survive-correction",
                "status": "declined",
            },
            "operator_instructions": [
                {
                    "instruction": "The source is incorrect. Use Jobright from the current URL and review again without submitting.",
                    "source": "human_review_correction",
                    "message_id": 6999,
                }
            ],
        }
        db.commit()

        result = OperatorChatService(db)._try_apply_review_instruction_from_chat(
            db.get(models.ChatThread, thread_id),
            "The source is incorrect. Use Jobright from the current URL and review again without submitting.",
            chat_scope={
                "run_related": True,
                "primary_intent": "correction",
                "action": "correct_field",
                "confidence": 0.96,
                "rationale": "The user supplied a review-stage field correction.",
            },
            human_message_id=7001,
        )

        assert result is not None
        assert result["applied"] is True
        assert scheduled == [run_id]
        refreshed = db.get(models.OperatorRun, run_id)
        assert refreshed.current_state == "RUNNING"
        assert "pending_final_action" not in refreshed.confidence_state
        instruction = refreshed.confidence_state["operator_instructions"][-1]
        assert len(refreshed.confidence_state["operator_instructions"]) == 1
        assert instruction["source"] == "human_review_correction"
        assert instruction["message_id"] == 7001
        assert "Use Jobright" in instruction["instruction"]
        event = db.scalars(
            select(models.CognitiveEvent).where(
                models.CognitiveEvent.run_id == run_id,
                models.CognitiveEvent.event_type
                == "REVIEW_CORRECTION_RESUME_REQUESTED",
            )
        ).first()
        assert event is not None
        assert event.event_payload["superseded_final_action"] == {
            "label": "Submit Application",
            "question_id": 9001,
            "status": "declined",
        }


def test_a_correction_sent_while_the_run_is_live_is_recorded_for_its_next_step_without_a_second_drive(monkeypatch) -> None:
    """Run 72: a school-city correction sent while the run filled the form was answered "Got it" and dropped."""
    scheduled: list[int] = []
    monkeypatch.setattr(
        "app.extension_run.schedule_resume_if_in_browser",
        lambda run_id: scheduled.append(run_id) or True,
    )

    with TestClient(app):
        created = create_operator_goal_response(
            {"job_url": "https://jobs.example.test/application", "auto_start": False}
        )
        assert created.status_code == 200, created.text
        run_id = created.json()["id"]
        thread_id = create_run_thread(run_id)

    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        run.current_state = "RUNNING"
        run.confidence_state = {
            **(run.confidence_state or {}),
            "in_browser": {"url": run.target_url, "device_id": "fixture-device"},
        }
        db.commit()

        result = OperatorChatService(db)._try_apply_review_instruction_from_chat(
            db.get(models.ChatThread, thread_id),
            "The Education City should be Newark, not Jersey City.",
            chat_scope={"run_related": True, "primary_intent": "correction", "action": "correct_field", "confidence": 0.9},
            human_message_id=7101,
        )

        assert result is not None and result["applied"] is True and result["run_state"] == "RUNNING"
        assert scheduled == [], "the live loop picks it up; no second drive"
        refreshed = db.get(models.OperatorRun, run_id)
        assert refreshed.current_state == "RUNNING"
        assert refreshed.confidence_state["operator_instructions"][-1]["instruction"] == "The Education City should be Newark, not Jersey City."


def test_exact_durable_review_correction_retry_reuses_prior_model_classification() -> None:
    with TestClient(app):
        created = create_operator_goal_response(
            {
                "job_url": "https://jobs.example.test/application",
                "auto_start": False,
            }
        )
        assert created.status_code == 200, created.text
        run_id = created.json()["id"]
        thread_id = create_run_thread(run_id)

    instruction = "Use the current URL source and review again without submitting."
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        run.current_state = "READY_FOR_REVIEW"
        run.confidence_state = {
            **(run.confidence_state or {}),
            "in_browser": {"device_id": "fixture-device", "url": run.target_url},
            "operator_instructions": [
                {
                    "instruction": instruction,
                    "source": "human_review_correction",
                    "message_id": 8100,
                }
            ],
        }
        db.commit()

        scope = OperatorChatService(db)._replayed_review_instruction_scope(
            db.get(models.ChatThread, thread_id),
            f"  {instruction.upper()}  ",
        )

    assert scope is not None
    assert scope["primary_intent"] == "correction"
    assert scope["action"] == "correct_field"
    assert scope["metadata"]["route"] == "exact_durable_review_instruction_replay"
    assert scope["metadata"]["prior_message_id"] == 8100


def test_pending_run_correction_with_urls_updates_active_run_not_new_application(monkeypatch) -> None:
    resumed: list[int] = []
    start_attempts: list[str] = []

    def fake_resume(self, run_id: int):
        resumed.append(run_id)
        return self.db.get(models.OperatorRun, run_id)

    def fake_start_deferred(self, **kwargs):
        start_attempts.append(str(kwargs.get("job_url") or ""))
        raise AssertionError("URL-bearing correction must not start a new application")

    from app.end_to_end_goal import EndToEndGoalService

    monkeypatch.setattr(OperatorRuntimeEngine, "resume_run", fake_resume)
    monkeypatch.setattr(EndToEndGoalService, "start_deferred", fake_start_deferred)

    with TestClient(app) as client:
        created = create_operator_goal_response({"job_url": "https://job-boards.greenhouse.io/known/jobs/8164921002", "auto_start": False})
        assert created.status_code == 200, created.text
        run_id = created.json()["id"]

    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        run.current_state = "WAITING_FOR_USER"
        run.status_message = "Pre-submit validation found unanswered fields."
        profile = db.scalars(select(models.UserProfile).where(models.UserProfile.user_id == run.user_id)).first()
        profile.location = "Jersey City, New Jersey"
        profile.linkedin = "https://www.linkedin.com/in/jordan"
        profile.github = "https://github.com/jordan"
        db.add(
            models.OperatorQuestion(
                run_id=run_id,
                question_type="blocked_reason",
                stuck_phase="APPLICATION_EXECUTION",
                stuck_reason="Pre-submit validation found unanswered fields.",
                prompt="I am stuck because required fields are unresolved.",
                expected_answer_schema={"fields": [{"field_key": "location_city_candidate_location", "label": "Location (City)*"}]},
                status="pending",
            )
        )
        db.commit()

    correction = (
        "so you didnt fill in location and you got my linkedin and github wrong\n"
        "these are my correct links\n"
        "linkedin: www.linkedin.com/in/jordan-park\n"
        "github: https://github.com/jordanpark\n"
        "update them in the live form and also save them to my profile"
    )
    with TestClient(app) as client:
        thread_id = create_run_thread(run_id)
        body = post_chat(client, thread_id, correction)

    assert body["application_request"] is None
    assert body["question_answer"] is None
    assert body["runtime_correction"]["applied"] is True
    assert set(body["runtime_correction"]["profile_updates"]) == {"linkedin", "github"}
    assert set(body["runtime_correction"]["field_keys"]) == {"linkedin", "github"}
    assert resumed == []
    assert start_attempts == []

    with SessionLocal() as db:
        saved_thread = db.get(models.ChatThread, thread_id)
        assert saved_thread.run_id == run_id
        profile = db.scalars(select(models.UserProfile).where(models.UserProfile.user_id == db.get(models.OperatorRun, run_id).user_id)).first()
        assert profile.linkedin == "https://www.linkedin.com/in/jordan-park"
        assert profile.github == "https://github.com/jordanpark"
        stored = _facts(profile.user_id)
        assert stored["linkedin"].value == "https://www.linkedin.com/in/jordan-park"
        assert "your correction" in stored["linkedin"].note


def _facts(user_id: int) -> dict[str, user_memory.UserFact]:
    return {fact.key: fact for fact in user_memory.facts(user_id)}


def _judge_returns(monkeypatch, *facts: dict) -> None:
    """Stand in for the answer-fact judge; tests run without a live model."""
    monkeypatch.setattr(
        LumenModelClient,
        "extract_user_facts_from_answer",
        lambda self, payload: {"ok": True, "facts": [dict(fact) for fact in facts]},
    )


def test_run_questions_persists_field_approval_question_and_answer_updates_task() -> None:
    with TestClient(app) as client:
        created = create_operator_goal_response({"job_url": "https://boards.greenhouse.io/example/jobs/12345", "auto_start": False})
        assert created.status_code == 200, created.text
        run_id = created.json()["id"]

    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        run.current_state = "WAITING_FOR_USER"
        run.status_message = "Low-confidence field mappings require approval before filling."
        db.add(
            models.RuntimeTask(
                run_id=run_id,
                name="Approve field: Sponsorship",
                task_type="field_approval",
                status="pending",
                payload={"field_key": "sponsorship", "label": "Do you now or in the future require sponsorship?", "confidence": 0.6},
            )
        )
        db.commit()

    with TestClient(app) as client:
        questions = client.get(f"/operator/runs/{run_id}/questions")
        assert questions.status_code == 200, questions.text
        body = questions.json()
        assert body
        question_id = body[0]["id"]
        assert body[0]["question_type"] == "field_approval"

        answered = client.post(
            f"/operator/questions/{question_id}/answer",
            json={"answer": "No", "payload": {}, "auto_resume": False},
        )
        assert answered.status_code == 200, answered.text
        assert answered.json()["question"]["status"] == "applied"
        follow_up_questions = client.get(f"/operator/runs/{run_id}/questions")
        assert follow_up_questions.status_code == 200, follow_up_questions.text
        assert not [question for question in follow_up_questions.json() if question["status"] == "pending"]

    with SessionLocal() as db:
        task = db.scalars(select(models.RuntimeTask).where(models.RuntimeTask.run_id == run_id, models.RuntimeTask.task_type == "field_approval")).first()
        assert task.status == "approved"
        assert task.payload["approved_value"] == "No"
        refreshed_run = db.get(models.OperatorRun, run_id)
        assert refreshed_run.confidence_state["end_to_end"]["execution_epoch"] == 1
        epoch_event = db.scalars(
            select(models.CognitiveEvent).where(models.CognitiveEvent.run_id == run_id, models.CognitiveEvent.event_type == "EXECUTION_EPOCH_ADVANCED")
        ).first()
        assert epoch_event is not None
        assert epoch_event.event_payload["reason"] == "field_approval_answered"


def test_flexible_field_answer_resolves_to_linkedin_option() -> None:
    with TestClient(app) as client:
        created = create_operator_goal_response({"job_url": "https://boards.greenhouse.io/example/jobs/12345", "auto_start": False})
        assert created.status_code == 200, created.text
        run_id = created.json()["id"]

    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        run.current_state = "WAITING_FOR_USER"
        run.status_message = "Low-confidence field mappings require approval before filling."
        db.add(
            models.RuntimeTask(
                run_id=run_id,
                name="Provide required field: How did you first learn about us?",
                task_type="field_approval",
                status="pending",
                payload={
                    "field_key": "referral_source",
                    "label": "How did you first learn about this company?",
                    "options": ["LinkedIn", "Company Website", "Referral"],
                    "confidence": 0.45,
                },
            )
        )
        db.commit()

    with TestClient(app) as client:
        question = client.get(f"/operator/runs/{run_id}/questions").json()[0]
        answered = client.post(
            f"/operator/questions/{question['id']}/answer",
            json={"answer": "You can put it as LinkedIn, or select any listed option, that's fine.", "payload": {}, "auto_resume": False},
        )
        assert answered.status_code == 200, answered.text
        payload = answered.json()
        assert payload["resolved_value"] == "LinkedIn"
        assert "LinkedIn" in payload["rationale"]

    with SessionLocal() as db:
        task = db.scalars(select(models.RuntimeTask).where(models.RuntimeTask.run_id == run_id, models.RuntimeTask.task_type == "field_approval")).first()
        assert task.payload["approved_value"] == "LinkedIn"
        assert task.payload["interpreted_answer"]["resolved_value"] == "LinkedIn"


def test_field_answer_can_be_saved_to_profile_and_common_answers(monkeypatch) -> None:
    _judge_returns(monkeypatch, {"key": "location", "value": "Jersey City, New Jersey", "section": "contact"})
    with TestClient(app):
        created = create_operator_goal_response({"job_url": "https://boards.greenhouse.io/profile/jobs/1", "auto_start": False})
        assert created.status_code == 200, created.text
        run_id = created.json()["id"]

    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        run.current_state = "WAITING_FOR_USER"
        task = models.RuntimeTask(
            run_id=run_id,
            name="Provide required field: Location",
            task_type="field_approval",
            status="pending",
            payload={"field_key": "location", "label": "Candidate location", "confidence": 0.45},
        )
        db.add(task)
        db.flush()
        question = models.OperatorQuestion(
            run_id=run_id,
            question_type="field_approval",
            stuck_phase="APPLICATION_EXECUTION",
            stuck_reason="Low-confidence field mappings require approval.",
            prompt="Where are you located?",
            expected_answer_schema={"task_ids": [task.id]},
            status="pending",
        )
        db.add(question)
        db.commit()
        question_id = question.id

    with SessionLocal() as db:
        result = OperatorChatService(db).answer_question(
            question_id=question_id,
            answer="Jersey City, New Jersey",
            payload={"save_scope": "permanent"},
            auto_resume=False,
        )
        assert result["saved_common_answers"]

    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        profile = db.scalars(select(models.UserProfile).where(models.UserProfile.user_id == run.user_id)).first()
        # The fact lives in the profile memory and is mirrored into the location column.
        assert _facts(run.user_id)["location"].value == "Jersey City, New Jersey"
        assert profile.location == "Jersey City, New Jersey"


def test_field_answer_years_extraction_removes_meta_instruction_from_memory(monkeypatch) -> None:
    _judge_returns(monkeypatch, {"key": "professional_python_experience", "value": "7+ years", "section": "work"})
    with TestClient(app):
        created = create_operator_goal_response({"job_url": "https://boards.greenhouse.io/profile/jobs/years", "auto_start": False})
        assert created.status_code == 200, created.text
        run_id = created.json()["id"]

    raw_answer = "about 7+ years write around that and save it to the profile so you could use it next time"
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        run.current_state = "WAITING_FOR_USER"
        task = models.RuntimeTask(
            run_id=run_id,
            name="Provide required field: Python years",
            task_type="field_approval",
            status="pending",
            payload={
                "field_key": "professional_python_years",
                "label": "How many years of professional Python experience do you have?",
                "confidence": 0.0,
            },
        )
        db.add(task)
        db.flush()
        question = models.OperatorQuestion(
            run_id=run_id,
            question_type="field_approval",
            stuck_phase="APPLICATION_EXECUTION",
            stuck_reason="Need Python years",
            prompt="How many years?",
            expected_answer_schema={"task_ids": [task.id]},
            status="pending",
        )
        db.add(question)
        db.commit()
        question_id = question.id

    with SessionLocal() as db:
        result = OperatorChatService(db).answer_question(
            question_id=question_id,
            answer=raw_answer,
            payload={"save_scope": "permanent"},
            auto_resume=False,
        )
        assert result["resolved_value"] == "7+ years"
        assert result["saved_common_answers"][0]["answer"] == "7+ years"

    with SessionLocal() as db:
        task = db.scalars(select(models.RuntimeTask).where(models.RuntimeTask.run_id == run_id, models.RuntimeTask.task_type == "field_approval")).first()
        assert task.payload["raw_user_answer"] == raw_answer
        assert task.payload["approved_value"] == "7+ years"
        run = db.get(models.OperatorRun, run_id)
        stored = _facts(run.user_id)["professional_python_experience"].value
        assert stored == "7+ years"
        assert "save it" not in stored


def test_one_time_field_answer_is_not_stored_as_a_saved_answer() -> None:
    with TestClient(app):
        created = create_operator_goal_response({"job_url": "https://boards.greenhouse.io/profile/jobs/2", "auto_start": False})
        assert created.status_code == 200, created.text
        run_id = created.json()["id"]

    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        run.current_state = "WAITING_FOR_USER"
        before_count = len(list(db.scalars(select(models.AnswerMemory).where(models.AnswerMemory.user_id == run.user_id)).all()))
        before_location = _facts(run.user_id).get("location")
        task = models.RuntimeTask(
            run_id=run_id,
            name="Provide required field: Location",
            task_type="field_approval",
            status="pending",
            payload={"field_key": "location", "label": "Candidate location", "confidence": 0.45},
        )
        db.add(task)
        db.flush()
        question = models.OperatorQuestion(
            run_id=run_id,
            question_type="field_approval",
            stuck_phase="APPLICATION_EXECUTION",
            stuck_reason="Low-confidence field mappings require approval.",
            prompt="Where are you located?",
            expected_answer_schema={"task_ids": [task.id]},
            status="pending",
        )
        db.add(question)
        db.commit()
        question_id = question.id

    with SessionLocal() as db:
        result = OperatorChatService(db).answer_question(
            question_id=question_id,
            answer="Austin, Texas",
            payload={"save_scope": "one_time"},
            auto_resume=False,
        )
        assert result["saved_common_answers"] == []

    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        after_count = len(list(db.scalars(select(models.AnswerMemory).where(models.AnswerMemory.user_id == run.user_id)).all()))
        # No saved-answer row and no profile fact while answering: whether this answer is a durable
        # fact is decided afterwards by the model-judged learner (unavailable in tests).
        assert after_count == before_count
        assert _facts(run.user_id).get("location") == before_location


@pytest.mark.parametrize(
    "remember_request",
    [
        "Yes, save it to memory.",
        "Yes, put it on my profile.",
        "Yes, remember it for future applications.",
        "Yes, keep that as part of what you know about me.",
    ],
)
def test_chat_answer_save_instruction_creates_common_answer(monkeypatch, remember_request: str) -> None:
    resumed: list[int] = []

    def fake_resume(self, run_id: int):
        resumed.append(run_id)
        return self.db.get(models.OperatorRun, run_id)

    monkeypatch.setattr(OperatorRuntimeEngine, "resume_run", fake_resume)
    monkeypatch.setattr(
        "app.lumen_model.LumenModelClient.interpret_pending_user_reply",
        lambda self, payload: {
            "ok": True,
            "route": "answer",
            "answer_value": "Yes",
            "matched_option": "",
            "memory_intent": "profile_memory",
            "save_scope": "permanent",
            "profile_key": "full_stack_web_application_experience",
            "profile_value": "Yes",
            "confidence": 0.99,
            "rationale": "Separated the answer from the save instruction.",
        },
    )

    with TestClient(app) as client:
        created = create_operator_goal_response({"job_url": "https://boards.greenhouse.io/profile/jobs/3", "auto_start": False})
        assert created.status_code == 200, created.text
        run_id = created.json()["id"]

    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        run.current_state = "WAITING_FOR_USER"
        task = models.RuntimeTask(
            run_id=run_id,
            name="Provide required field: Full-stack experience",
            task_type="field_approval",
            status="pending",
            payload={
                "field_key": "full_stack_web_application_experience",
                "label": "Do you have any experience in full-stack web application development?",
                "confidence": 0.45,
            },
        )
        db.add(task)
        db.flush()
        db.add(
            models.OperatorQuestion(
                run_id=run_id,
                question_type="field_approval",
                stuck_phase="APPLICATION_EXECUTION",
                stuck_reason="Low-confidence field mappings require approval.",
                prompt="Do you have full-stack web application experience?",
                expected_answer_schema={"task_ids": [task.id]},
                status="pending",
            )
        )
        db.commit()

    with TestClient(app) as client:
        thread_id = create_run_thread(run_id)
        body = post_chat(client, thread_id, remember_request)
        assert body["question_answer"]["save_scope"] == "permanent"
        assert body["question_answer"]["memory_intent"] == "profile_memory"
        assert body["question_answer"]["saved_common_answers"]
        # The claim is now grounded in a verified memory-write receipt: "saved it to your profile…".
        assert "saved it to your profile" in body["assistant_message"]["content"]
        assert resumed == [run_id]

    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        assert _facts(run.user_id)["full_stack_web_application_experience"].value == "Yes"


def test_chat_pending_question_noop_gets_honest_non_applied_confirmation(monkeypatch) -> None:
    def fake_apply_answer(self, run, question, answer_payload):
        return {
            "applied": False,
            "target": "field_approval_tasks",
            "can_resume": False,
            "interpretations": [
                {
                    "resolved_value": "",
                    "confidence": 0.2,
                    "rationale": "The answer needs a specific approved value.",
                    "source": "test_noop",
                }
            ],
            "saved_common_answers": [],
        }

    monkeypatch.setattr(OperatorChatService, "_apply_answer", fake_apply_answer)
    monkeypatch.setattr(
        "app.lumen_model.LumenModelClient.interpret_pending_user_reply",
        lambda self, payload: {
            "ok": True,
            "route": "answer",
            "answer_value": "I'm not sure.",
            "matched_option": "",
            "save_scope": "one_time",
            "profile_key": "",
            "profile_value": "",
            "confidence": 0.9,
            "rationale": "Preserved the user's uncertain answer for the apply path to evaluate.",
        },
    )

    with TestClient(app) as client:
        created = create_operator_goal_response({"job_url": "https://boards.greenhouse.io/profile/jobs/noop", "auto_start": False})
        assert created.status_code == 200, created.text
        run_id = created.json()["id"]

    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        run.current_state = "WAITING_FOR_USER"
        task = models.RuntimeTask(
            run_id=run_id,
            name="Provide required field: Work authorization",
            task_type="field_approval",
            status="pending",
            payload={"field_key": "work_authorization", "label": "Are you legally authorized to work?", "confidence": 0.45},
        )
        db.add(task)
        db.flush()
        db.add(
            models.OperatorQuestion(
                run_id=run_id,
                question_type="field_approval",
                stuck_phase="APPLICATION_EXECUTION",
                stuck_reason="Low-confidence field mappings require approval.",
                prompt="Are you legally authorized to work?",
                expected_answer_schema={"task_ids": [task.id]},
                status="pending",
            )
        )
        db.commit()

    with TestClient(app) as client:
        thread_id = create_run_thread(run_id)
        body = post_chat(client, thread_id, "I'm not sure.")

    assert body["question_answer"]["applied"] is False
    content = body["assistant_message"]["content"]
    assert "I still need a specific answer for" in content
    assert "The answer needs a specific approved value" in content
    assert "Runtime is continuing" not in content
    assert "asked Runtime to continue" not in content


def test_blocked_reason_field_reply_is_saved_and_resumes_without_reask(monkeypatch, tmp_path) -> None:
    resumed: list[int] = []

    def fake_scope(self, thread, content, *, payload, context, pending_question):
        return {"action": "answer_question", "in_scope": True, "run_related": True, "out_of_scope": False}

    def fake_resume(self, run_id: int):
        resumed.append(run_id)
        return self.db.get(models.OperatorRun, run_id)

    monkeypatch.setattr(OperatorChatService, "_classify_chat_scope", fake_scope)
    monkeypatch.setattr(OperatorRuntimeEngine, "resume_run", fake_resume)

    with TestClient(app) as client:
        created = create_operator_goal_response({"job_url": "https://job-boards.greenhouse.io/known/jobs/8164921002", "auto_start": False})
        assert created.status_code == 200, created.text
        run_id = created.json()["id"]

    artifact_path = tmp_path / "phase2_blocker.json"
    artifact_path.write_text(
        json.dumps(
            {
                "blocker_category": "UNSUPPORTED_WIDGET",
                "evidence": {
                    "failed_action": {
                        "success": False,
                        "action": "select_option",
                        "node_id": "lumen_node_029",
                        "snapshot_id": "snap-github",
                        "failure_reason": "VALUE_NOT_APPLIED",
                        "value_source": "candidate.github",
                        "observation": {
                            "target": {"node_id": "lumen_node_029", "label": "Do you have a Github/Gitlab profile to share with our hiring team?*"},
                            "candidate_options": ["Yes", "No"],
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        run.current_state = "WAITING_FOR_USER"
        run.status_message = "I tried to complete 'Do you have a Github/Gitlab profile to share with our hiring team?*' but could not confirm the widget accepted the value."
        run.confidence_state = {
            **(run.confidence_state or {}),
            "end_to_end": {
                "phase": "APPLICATION_EXECUTION",
                "phase2_blocker_category": "UNSUPPORTED_WIDGET",
                "phase2_blocker_artifact_path": str(artifact_path),
                "pending_question_type": "blocked_reason",
            },
        }
        for _ in range(2):
            db.add(
                models.OperatorQuestion(
                    run_id=run_id,
                    question_type="blocked_reason",
                    stuck_phase="APPLICATION_EXECUTION",
                    stuck_reason=run.status_message,
                    prompt=f"{run.status_message} Review the field or provide the correction needed to continue.",
                    expected_answer_schema={"answer": "field correction or continue after manual review"},
                    status="pending",
                )
            )
        db.commit()

    with TestClient(app) as client:
        thread_id = create_run_thread(run_id)
        body = post_chat(client, thread_id, "yes i do have a github profile, select yes")

    assert body["question_answer"]["applied"] is True
    assert body["question_answer"]["question_type"] == "blocked_reason"
    assert body["question_answer"]["resolved_value"] == "Yes"
    assert "I applied that answer as Yes" in body["assistant_message"]["content"]
    assert resumed == [run_id]

    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        questions = list(db.scalars(select(models.OperatorQuestion).where(models.OperatorQuestion.run_id == run_id, models.OperatorQuestion.question_type == "blocked_reason")).all())
        assert sum(1 for question in questions if question.status == "pending") == 0
        task = db.scalars(select(models.RuntimeTask).where(models.RuntimeTask.run_id == run_id, models.RuntimeTask.task_type == "field_approval")).first()
        assert task is not None
        assert task.status == "approved"
        assert task.payload["interpreted_answer"]["resolved_value"] == "Yes"


def test_chat_applied_claim_absent_without_consuming_transition(monkeypatch) -> None:
    def fake_scope(self, thread, content, *, payload, context, pending_question):
        return {"action": "answer_question", "in_scope": True, "run_related": True, "out_of_scope": False}

    def fake_apply_answer(self, run, question, answer_payload):
        return {"applied": True, "target": "goal_workspace", "can_resume": False, "interpretations": [], "saved_common_answers": []}

    monkeypatch.setattr(OperatorChatService, "_classify_chat_scope", fake_scope)
    monkeypatch.setattr(OperatorChatService, "_apply_answer", fake_apply_answer)

    with TestClient(app) as client:
        created = create_operator_goal_response({"job_url": "https://boards.greenhouse.io/profile/jobs/generic", "auto_start": False})
        assert created.status_code == 200, created.text
        run_id = created.json()["id"]

    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        run.current_state = "WAITING_FOR_USER"
        run.status_message = "Generic blocked reason."
        db.add(
            models.OperatorQuestion(
                run_id=run_id,
                question_type="blocked_reason",
                stuck_phase="APPLICATION_EXECUTION",
                stuck_reason="Generic blocked reason.",
                prompt="Tell me what to remember.",
                expected_answer_schema={"answer": "string"},
                status="pending",
            )
        )
        db.commit()

    with TestClient(app) as client:
        thread_id = create_run_thread(run_id)
        body = post_chat(client, thread_id, "remember this note")

    content = body["assistant_message"]["content"]
    assert body["question_answer"]["applied"] is True
    assert "I applied that answer" not in content
    assert "I recorded that answer" in content


def test_field_backed_blocked_reason_questions_dedupe_by_field() -> None:
    with TestClient(app):
        created = create_operator_goal_response({"job_url": "https://boards.greenhouse.io/profile/jobs/dedupe", "auto_start": False})
        assert created.status_code == 200, created.text
        run_id = created.json()["id"]

    schema = {
        "answer": "field correction or continue after manual review",
        "fields": [{"field_key": "lumen_node_029", "label": "Do you have a Github/Gitlab profile to share with our hiring team?", "options": ["Yes", "No"]}],
    }
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        run.current_state = "WAITING_FOR_USER"
        run.status_message = "I tried to complete the GitHub profile field but could not confirm the widget accepted the value."
        for _ in range(2):
            db.add(
                models.OperatorQuestion(
                    run_id=run_id,
                    question_type="blocked_reason",
                    stuck_phase="APPLICATION_EXECUTION",
                    stuck_reason=run.status_message,
                    prompt="Provide the correction.",
                    expected_answer_schema=schema,
                    status="pending",
                )
            )
        db.commit()
        questions = OperatorChatService(db).list_questions(run_id)
        pending = [question for question in questions if question.question_type == "blocked_reason" and question.status == "pending"]
        assert len(pending) == 1


def test_pending_field_approval_plain_replies_route_before_scope_gate(monkeypatch) -> None:
    resumed: list[int] = []

    def fake_resume(self, run_id: int):
        resumed.append(run_id)
        return self.db.get(models.OperatorRun, run_id)

    monkeypatch.setattr(OperatorRuntimeEngine, "resume_run", fake_resume)
    monkeypatch.setattr(
        "app.lumen_model.LumenModelClient.interpret_pending_user_reply",
        lambda self, payload: {
            "ok": True,
            "route": "answer",
            "answer_value": str(payload.get("user_reply") or ""),
            "matched_option": "",
            "save_scope": "one_time",
            "profile_key": "",
            "profile_value": "",
            "confidence": 0.95,
            "rationale": "The reply is the field answer.",
        },
    )

    fixtures = [
        (
            "run259",
            "Country",
            "lumen_node_006",
            "united states",
            "United States",
        ),
        (
            "run261",
            "How many years of professional Python experience do you have?",
            "lumen_node_024",
            "i have 10+ years of professional python experience",
            "10+ years",
        ),
    ]

    with TestClient(app) as client:
        for suffix, label, field_key, answer, expected in fixtures:
            created = create_operator_goal_response({"job_url": f"https://job-boards.greenhouse.io/known/jobs/{suffix}", "auto_start": False})
            assert created.status_code == 200, created.text
            run_id = created.json()["id"]
            with SessionLocal() as db:
                run = db.get(models.OperatorRun, run_id)
                run.current_state = "WAITING_FOR_USER"
                run.status_message = f"This posting asks: {label}. I don't have a stored answer for it."
                task = models.RuntimeTask(
                    run_id=run_id,
                    name=f"Provide required field: {label}",
                    task_type="field_approval",
                    status="pending",
                    payload={"field_key": field_key, "label": label, "confidence": 0.45, "options": []},
                )
                db.add(task)
                db.flush()
                db.add(
                    models.OperatorQuestion(
                        run_id=run_id,
                        question_type="field_approval",
                        stuck_phase="APPLICATION_EXECUTION",
                        stuck_reason="Low-confidence field mappings require approval.",
                        prompt="I found application fields that need your confirmation before I continue. Please provide the correct answer or approve the suggested values.",
                        expected_answer_schema={"task_ids": [task.id], "fields": [{"id": task.id, "label": label, "field_key": field_key, "options": []}]},
                        status="pending",
                    )
                )
                db.commit()

            thread_id = create_run_thread(run_id)
            body = post_chat(client, thread_id, answer)

            assert body["chat_scope"]["out_of_scope"] is False
            assert body["chat_scope"]["action"] == "answer_question"
            assert body["question_answer"]["applied"] is True
            assert body["run_control"] is None
            assert expected.lower() in str(body["question_answer"]["resolved_value"]).lower()

            with SessionLocal() as db:
                task = db.scalars(
                    select(models.RuntimeTask).where(models.RuntimeTask.run_id == run_id, models.RuntimeTask.task_type == "field_approval")
                ).first()
                assert expected.lower() in str(task.payload.get("approved_value") or "").lower()

    assert len(resumed) == 2


def test_pending_field_approval_control_command_still_routes_to_control() -> None:
    with TestClient(app) as client:
        created = create_operator_goal_response({"job_url": "https://job-boards.greenhouse.io/known/jobs/control", "auto_start": False})
        assert created.status_code == 200, created.text
        run_id = created.json()["id"]

    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        run.current_state = "WAITING_FOR_USER"
        task = models.RuntimeTask(
            run_id=run_id,
            name="Provide required field: Python years",
            task_type="field_approval",
            status="pending",
            payload={"field_key": "lumen_node_024", "label": "How many years of professional Python experience do you have?", "confidence": 0.45},
        )
        db.add(task)
        db.flush()
        db.add(
            models.OperatorQuestion(
                run_id=run_id,
                question_type="field_approval",
                stuck_phase="APPLICATION_EXECUTION",
                stuck_reason="Low-confidence field mappings require approval.",
                prompt="I found application fields that need your confirmation before I continue. Please provide the correct answer or approve the suggested values.",
                expected_answer_schema={"task_ids": [task.id], "fields": [{"id": task.id, "label": task.payload["label"], "field_key": "lumen_node_024"}]},
                status="pending",
            )
        )
        db.commit()

    with TestClient(app) as client:
        thread_id = create_run_thread(run_id)
        body = post_chat(client, thread_id, "pause")

    assert body["run_control"]["action"] == "pause"
    assert body["question_answer"] is None


def _blocked_run_with_thread(client: TestClient, slug: str) -> tuple[int, int]:
    created = create_operator_goal_response({"job_url": f"https://ats.rippling.com/capacity/jobs/{slug}", "auto_start": False})
    assert created.status_code == 200, created.text
    run_id = created.json()["id"]
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        run.current_state = "BLOCKED"
        run.status_message = "Pre-submit validation could not verify a safe final review state."
        db.commit()
    thread_id = create_run_thread(run_id)
    return run_id, thread_id


def test_ensure_question_announce_false_suppresses_duplicate_chat_message() -> None:
    # Deferred-application chat flow: the turn emits its own "Got it… I hit a
    # blocker" reply, so the question must NOT also post a duplicate "I am stuck…"
    # chat message (which previously landed out of order before that reply).
    with TestClient(app) as client:
        run_id, thread_id = _blocked_run_with_thread(client, "announce-false")
    with SessionLocal() as db:
        service = OperatorChatService(db)
        question = service.ensure_question_for_run(run_id, thread_id=thread_id, announce=False)
        db.commit()
        assert question is not None
        messages = service.list_messages(thread_id)
        assert not any(m.role == "assistant" and "I am stuck because" in (m.content or "") for m in messages)


def test_ensure_question_announce_default_adds_chat_message() -> None:
    # Other flows (no primary reply this turn) still surface the question in chat.
    with TestClient(app) as client:
        run_id, thread_id = _blocked_run_with_thread(client, "announce-default")
    with SessionLocal() as db:
        service = OperatorChatService(db)
        question = service.ensure_question_for_run(run_id, thread_id=thread_id)
        db.commit()
        assert question is not None
        messages = service.list_messages(thread_id)
        assert any(m.role == "assistant" and "I am stuck because" in (m.content or "") for m in messages)


def test_list_questions_does_not_announce_duplicate_blocked_chat_message() -> None:
    # The frontend polls /questions while a run is stopping. Polling should create/update the question
    # record for the card, but it must not append a second "I am stuck..." chat bubble.
    with TestClient(app) as client:
        run_id, thread_id = _blocked_run_with_thread(client, "list-no-announce")
    with SessionLocal() as db:
        service = OperatorChatService(db)
        questions = service.list_questions(run_id)
        db.commit()

        pending = [question for question in questions if question.status == "pending"]
        assert len(pending) == 1
        messages = service.list_messages(thread_id)
        assert not any(m.role == "assistant" and "I am stuck because" in (m.content or "") for m in messages)


def test_list_questions_existing_pending_poll_is_read_only(monkeypatch) -> None:
    with TestClient(app) as client:
        run_id, thread_id = _blocked_run_with_thread(client, "list-read-only")
    with SessionLocal() as db:
        service = OperatorChatService(db)
        question = service.ensure_question_for_run(run_id, thread_id=thread_id, announce=False)
        db.commit()
        assert question is not None

        def unexpected_write_path(*_args, **_kwargs):
            raise AssertionError("an existing pending-question poll must remain read-only")

        monkeypatch.setattr(service, "_ensure_question_for_run_serialized", unexpected_write_path)
        monkeypatch.setattr(service, "_reconcile_stale_task_questions", unexpected_write_path)

        questions = service.list_questions(run_id)

        assert [item.id for item in questions if item.status == "pending"] == [question.id]


def test_generic_blocked_reason_questions_dedupe_by_reason() -> None:
    with TestClient(app) as client:
        run_id, _thread_id = _blocked_run_with_thread(client, "generic-blocked-dedupe")
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        assert run is not None
        for _ in range(2):
            db.add(
                models.OperatorQuestion(
                    run_id=run_id,
                    question_type="blocked_reason",
                    stuck_phase="BLOCKED",
                    stuck_reason=run.status_message,
                    prompt=f"I am stuck because: {run.status_message}.",
                    expected_answer_schema={"answer": "string"},
                    status="pending",
                )
            )
        db.commit()

        questions = OperatorChatService(db).list_questions(run_id)
        pending = [question for question in questions if question.question_type == "blocked_reason" and question.status == "pending"]
        assert len(pending) == 1
