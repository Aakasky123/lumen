"""Confirm-ask: when the agent is UNSURE it surfaces a proposed_answer the user can KEEP or CHANGE.

- The question schema carries the proposed answer (so the web card can render Keep/Change).
- "Keep" applies the proposed value verbatim, skipping the interpreter (idempotent, never re-asks).
- "Change" goes through the normal interpreter path.
"""

import uuid

from sqlalchemy import select

from app import models, user_memory
from app.lumen_model import LumenModelClient
from app.cognition import OperatorChatService
from app.db import SessionLocal, init_db
from app.runtime import OperatorRuntimeEngine


def _run_with_unsure_field(
    db,
    *,
    proposed_answer: str,
    is_choice: bool = False,
    options=None,
    in_browser: bool = False,
    label: str = "What is your expected salary?",
) -> tuple[models.OperatorRun, int, models.OperatorQuestion]:
    user = models.User(email=f"confirm-{uuid.uuid4().hex}@example.com", name="Confirm")
    db.add(user)
    db.flush()
    run = models.OperatorRun(user_id=user.id, goal="Apply", target_url="https://ats.example.com/apply", current_state="WAITING_FOR_USER")
    if in_browser:
        run.confidence_state = {"in_browser": {"url": "https://ats.example.com/apply", "device_id": "test-device"}}
    db.add(run)
    db.commit()
    task_id = OperatorRuntimeEngine(db)._phase2_upsert_required_field_approval_task(
        run,
        field={
            "node_id": "n1",
            "snapshot_id": "s1",
            "label": label,
            "options": list(options or []),
            "is_choice": is_choice,
            "required": True,
            "proposed_answer": "" if is_choice else proposed_answer,
            "proposed_option": proposed_answer if is_choice else "",
            "is_unsure": True,
        },
        source="in_browser_field",
    )
    db.commit()
    question = OperatorChatService(db).ensure_question_for_run(run.id, thread_id=None)
    return run, task_id, question


def _facts(user_id: int) -> dict[str, str]:
    return {fact.key: fact.value for fact in user_memory.facts(user_id)}


def _judge_returns(monkeypatch, *facts: dict) -> list[dict]:
    """Stand in for the answer-fact judge (tests run without a live model); records its inputs."""
    calls: list[dict] = []

    def judge(self, payload):
        calls.append(payload)
        return {"ok": True, "facts": [dict(fact) for fact in facts]}

    monkeypatch.setattr(LumenModelClient, "extract_user_facts_from_answer", judge)
    return calls


def test_question_schema_surfaces_the_proposed_answer() -> None:
    init_db()
    with SessionLocal() as db:
        _run, _task_id, question = _run_with_unsure_field(db, proposed_answer="120000 USD")
        field = (question.expected_answer_schema or {})["fields"][0]
        assert field["proposed_answer"] == "120000 USD"
        assert field["is_unsure"] is True


def test_keep_proposed_applies_value_verbatim_skipping_interpreter() -> None:
    init_db()
    with SessionLocal() as db:
        run, task_id, question = _run_with_unsure_field(db, proposed_answer="120000 USD")
        result = OperatorChatService(db)._apply_field_approval_answer(
            run, question, {"keep_proposed": True, "task_ids": [task_id], "answer": "(kept)"}
        )
        assert result["applied"] is True
        interpreted = result["interpretations"][0]
        assert interpreted["resolved_value"] == "120000 USD"  # verbatim, NOT re-interpreted from "(kept)"
        assert interpreted["source"] == "user_kept_proposed"
        task = db.get(models.RuntimeTask, task_id)
        assert task.payload["user_choice"] == "keep"
        # Flywheel: applying a confirm-ask records a structured correction (eval data, not a rule).
        from app.answer_corrections import load_corrections

        corrections = load_corrections(db, user_id=run.user_id)
        assert any(c["agent_proposed"] == "120000 USD" and c["user_action"] == "keep" for c in corrections)


def test_structured_keep_and_remember_skips_chat_models_and_resumes(monkeypatch) -> None:
    init_db()
    import app.extension_run as ext

    proposed = "I built a grounded AI platform and reduced document lookup time by nearly 70%."
    monkeypatch.setattr(ext, "schedule_resume_if_in_browser", lambda run_id: True)
    with SessionLocal() as db:
        run, task_id, question = _run_with_unsure_field(
            db,
            proposed_answer=proposed,
            in_browser=True,
            label="How have you applied AI in your engineering work and what impact did it have?",
        )
        thread = models.ChatThread(user_id=run.user_id, run_id=run.id, mode="run", status="active")
        db.add(thread)
        db.flush()
        question.thread_id = thread.id
        db.commit()
        service = OperatorChatService(db)

        def unexpected_model_call(*args, **kwargs):
            raise AssertionError("an explicit structured card action must not call a chat intent model")

        monkeypatch.setattr(service.lumen_model, "classify_operator_chat_scope", unexpected_model_call)
        monkeypatch.setattr(service, "_interpret_pending_user_reply", unexpected_model_call)
        judged = _judge_returns(monkeypatch, {"key": "ai_engineering_impact", "value": proposed, "section": "work"})

        turn = service._prepare_chat_turn(
            thread_id=thread.id,
            content=proposed,
            payload={
                "source": "side_panel",
                "keep_proposed": True,
                "task_ids": [task_id],
                "save_scope": "permanent",
            },
        )

        assert turn["question_answer"]["applied"] is True
        assert turn["question_answer"]["resolved_value"] == proposed
        assert turn["question_answer"]["save_scope"] == "permanent"
        assert db.get(models.RuntimeTask, task_id).payload["user_choice"] == "keep"
        # "Use & remember" asks the fact judge (explicitly) to key it, then it lands in the profile.
        assert judged and judged[0]["explicit"] is True and judged[0]["answer"] == proposed
        assert _facts(run.user_id)["ai_engineering_impact"] == proposed
        assert db.get(models.OperatorRun, run.id).current_state == "RUNNING"


def test_structured_option_click_is_validated_and_applied_without_interpretation(monkeypatch) -> None:
    init_db()
    import app.extension_run as ext

    monkeypatch.setattr(ext, "schedule_resume_if_in_browser", lambda run_id: True)
    with SessionLocal() as db:
        run, task_id, question = _run_with_unsure_field(
            db,
            proposed_answer="4",
            is_choice=True,
            options=["3", "4", "5+"],
            in_browser=True,
        )
        thread = models.ChatThread(user_id=run.user_id, run_id=run.id, mode="run", status="active")
        db.add(thread)
        db.flush()
        question.thread_id = thread.id
        db.commit()
        service = OperatorChatService(db)
        monkeypatch.setattr(
            service,
            "_interpret_pending_user_reply",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("exact option click should not be reinterpreted")),
        )

        result = service._try_answer_pending_question_from_chat(
            thread,
            "5+",
            payload={"selected_option": "5+", "task_ids": [task_id]},
            chat_scope={"action": "answer_question"},
        )

        assert result["applied"] is True
        assert result["resolved_value"] == "5+"
        assert db.get(models.RuntimeTask, task_id).payload["approved_value"] == "5+"


def test_pending_reply_provider_failure_is_reported_as_system_failure(monkeypatch) -> None:
    init_db()
    with SessionLocal() as db:
        run, task_id, question = _run_with_unsure_field(
            db,
            proposed_answer="",
            label="How Did You Hear About Us?",
        )
        thread = models.ChatThread(user_id=run.user_id, run_id=run.id, mode="run", status="active")
        db.add(thread)
        db.flush()
        question.thread_id = thread.id
        db.commit()
        service = OperatorChatService(db)
        monkeypatch.setattr(
            service,
            "_interpret_pending_user_reply",
            lambda *_args, **_kwargs: {
                "ok": False,
                "route": "unclear",
                "answer_value": "",
                "matched_option": "",
                "memory_intent": "current_answer",
                "save_scope": "one_time",
                "profile_key": "",
                "profile_value": "",
                "confidence": 0.0,
                "rationale": "The previous response did not contain a usable route and answer.",
                "failure_kind": "provider_output_failure",
                "metadata": {
                    "provider": "openrouter",
                    "parse_status": "empty",
                    "finish_reason": "length",
                    "raw_output_empty": True,
                },
            },
        )

        result = service._try_answer_pending_question_from_chat(
            thread,
            "LinkedIn; remember that for future applications.",
            payload={},
            chat_scope={"action": "answer_question"},
        )

        assert result["applied"] is False
        assert result["failure_kind"] == "provider_output_failure"
        assert "system failure" in result["assistant_text"]
        assert "state the answer once more" not in result["assistant_text"]
        assert db.get(models.OperatorQuestion, question.id).status == "pending"
        assert db.get(models.RuntimeTask, task_id).status == "pending"
        event = db.scalars(
            select(models.CognitiveEvent)
            .where(models.CognitiveEvent.run_id == run.id)
            .order_by(models.CognitiveEvent.id.desc())
        ).first()
        assert event.event_type == "PENDING_USER_REPLY_INTERPRETER_FAILED"
        assert event.event_payload["failure_kind"] == "provider_output_failure"


def test_in_browser_answer_does_not_trigger_playwright_resume(monkeypatch) -> None:
    # Regression: answering a confirm-ask on an IN-BROWSER run must NOT call the controlled-live
    # runtime resume (Playwright) — that path hits the external-execution mode guard and blocks the
    # run. In-browser runs resume via schedule_resume_if_in_browser instead.
    init_db()
    from app.runtime import OperatorRuntimeEngine

    calls = {"resume_run": 0}
    monkeypatch.setattr(OperatorRuntimeEngine, "resume_run", lambda self, run_id: calls.__setitem__("resume_run", calls["resume_run"] + 1) or None)
    # Isolate the resume-ROUTING decision: report can_resume without mutating the run state (real
    # _apply_field_approval_answer changes state, which would mask the WAITING_FOR_USER resume branch).
    monkeypatch.setattr(
        OperatorChatService,
        "_apply_answer",
        lambda self, run, question, payload: {"applied": True, "can_resume": True, "interpretations": [{}], "saved_common_answers": []},
    )

    with SessionLocal() as db:
        run, task_id, question = _run_with_unsure_field(db, proposed_answer="5 years", in_browser=True)
        OperatorChatService(db).answer_question(
            question_id=question.id, answer="5", payload={"task_ids": [task_id]}, auto_resume=True, record_user_message=False
        )
        assert calls["resume_run"] == 0  # in-browser run never takes the Playwright resume path

    with SessionLocal() as db:
        run, task_id, question = _run_with_unsure_field(db, proposed_answer="5 years", in_browser=False)
        OperatorChatService(db).answer_question(
            question_id=question.id, answer="5", payload={"task_ids": [task_id]}, auto_resume=True, record_user_message=False
        )
        assert calls["resume_run"] == 1  # a non-in-browser run DOES use the controlled-live resume


def test_picked_option_is_applied_verbatim_not_reinterpreted() -> None:
    # Regression: picking the field's own option "5+" must apply "5+" verbatim, NOT be reinterpreted
    # into "5 years" (which then can't match the dropdown option).
    init_db()
    with SessionLocal() as db:
        run, task_id, question = _run_with_unsure_field(db, proposed_answer="3", is_choice=True, options=["1", "2", "3", "4", "5+"])
        result = OperatorChatService(db)._apply_field_approval_answer(run, question, {"task_ids": [task_id], "answer": "5+"})
        assert result["applied"] is True
        assert result["interpretations"][0]["resolved_value"] == "5+"  # verbatim
        assert db.get(models.RuntimeTask, task_id).payload["interpreted_answer"]["source"] == "user_picked_option"


def test_in_browser_resume_is_reflected_in_result(monkeypatch) -> None:
    # Regression: in-browser runs resume via schedule_resume_if_in_browser (the Playwright resume_run
    # is skipped), so its return must surface as result["resumed"] — else the reply wrongly says
    # "Runtime has not resumed yet" even though it did.
    init_db()
    import app.extension_run as ext

    monkeypatch.setattr(ext, "schedule_resume_if_in_browser", lambda run_id: True)
    with SessionLocal() as db:
        run, task_id, question = _run_with_unsure_field(db, proposed_answer="3", is_choice=True, options=["1", "2", "5+"], in_browser=True)
        result = OperatorChatService(db).answer_question(
            question_id=question.id,
            answer="5+",
            payload={"task_ids": [task_id]},
            auto_resume=True,
            record_user_message=False,
        )
        assert result["resumed"] is True
        assert db.get(models.OperatorRun, run.id).current_state == "RUNNING"


def test_workflow_instruction_resumes_without_becoming_a_field_answer_or_memory(monkeypatch) -> None:
    init_db()
    import app.extension_run as ext

    monkeypatch.setattr(ext, "schedule_resume_if_in_browser", lambda run_id: True)
    with SessionLocal() as db:
        run, task_id, question = _run_with_unsure_field(
            db,
            proposed_answer="",
            in_browser=True,
            label="How have you applied AI in your engineering work and what impact did it have?",
        )
        thread = models.ChatThread(user_id=run.user_id, run_id=run.id, mode="run", status="active")
        db.add(thread)
        db.flush()
        question.thread_id = thread.id
        db.commit()
        answer_count = len(db.scalars(select(models.AnswerMemory).where(models.AnswerMemory.user_id == run.user_id)).all())

        result = OperatorChatService(db)._apply_pending_workflow_instruction_from_chat(
            thread,
            question,
            content="Can you write it based on my resume?",
            interpretation={
                "workflow_instruction": "Retrieve relevant resume evidence, draft a truthful positive answer, and ask me to approve it.",
                "confidence": 0.98,
                "rationale": "The reply delegates composition instead of supplying a fact.",
            },
            human_message_id=123,
        )

        assert result["applied"] is True
        assert result["resolved_value"] is None
        assert db.get(models.OperatorRun, run.id).current_state == "RUNNING"
        assert db.get(models.RuntimeTask, task_id).status == "superseded"
        assert db.get(models.OperatorQuestion, question.id).status == "superseded"
        assert len(db.scalars(select(models.AnswerMemory).where(models.AnswerMemory.user_id == run.user_id)).all()) == answer_count


def test_save_scope_is_model_interpreted_without_phrase_rules(monkeypatch) -> None:
    init_db()
    with SessionLocal() as db:
        _run, _task_id, question = _run_with_unsure_field(db, proposed_answer="3", is_choice=True, options=["1", "2", "5+"])
        svc = OperatorChatService(db)
        monkeypatch.setattr(
            svc.lumen_model,
            "interpret_pending_user_reply",
            lambda payload: {
                "ok": True,
                "route": "answer",
                "answer_value": "5+",
                "matched_option": "5+",
                "memory_intent": "profile_memory",
                "save_scope": "permanent",
                "profile_key": "professional_experience_years",
                "profile_value": "5+",
                "confidence": 0.98,
                "rationale": "Separated the answer from the save instruction.",
            },
        )
        interpreted = svc._interpret_pending_user_reply(question, "Use 5+ years and save this to my profile")
        assert interpreted["answer_value"] == "5+"
        assert interpreted["save_scope"] == "permanent"
        assert "save" not in interpreted["answer_value"].lower()


def test_approved_open_ended_proposal_is_saved_exactly_without_the_reuse_instruction() -> None:
    init_db()
    proposed = (
        "I built a document intelligence platform with semantic search and contextual chat, "
        "reducing document lookup time by nearly 70%."
    )
    with SessionLocal() as db:
        run, task_id, question = _run_with_unsure_field(
            db,
            proposed_answer=proposed,
            label="How have you applied AI in your engineering work and what impact did it have?",
        )
        result = OperatorChatService(db)._apply_field_approval_answer(
            run,
            question,
            {
                "task_ids": [task_id],
                "answer": proposed,
                "save_scope": "permanent",
                "raw_user_reply": "Use that draft and remember it for future applications.",
                "reply_interpretation": {
                    "route": "answer",
                    "answer_value": proposed,
                    "matched_option": "",
                    "workflow_instruction": "",
                    "memory_intent": "profile_memory",
                    "save_scope": "permanent",
                    "profile_key": "ai_engineering_impact_answer",
                    "profile_value": proposed,
                    "confidence": 0.99,
                    "rationale": "The user approved the grounded proposal and requested future reuse.",
                },
            },
        )

        assert result["applied"] is True
        assert result["interpretations"][0]["resolved_value"] == proposed
        stored = _facts(run.user_id)
        assert stored["ai_engineering_impact_answer"] == proposed
        assert all("remember it" not in value.lower() for value in stored.values())
        assert result["memory_receipts"][0]["outcome"] == "verified_succeeded"


def test_rich_profile_fact_uses_exact_choice_only_for_live_field_binding(monkeypatch) -> None:
    init_db()
    with SessionLocal() as db:
        run, task_id, question = _run_with_unsure_field(
            db,
            proposed_answer="3",
            is_choice=True,
            options=["0", "1", "2", "3", "4", "5+"],
        )
        svc = OperatorChatService(db)
        monkeypatch.setattr(
            svc.lumen_model,
            "interpret_pending_user_reply",
            lambda payload: {
                "ok": True,
                "route": "answer",
                "answer_value": "More than 7 years of professional software engineering experience",
                "matched_option": "5+",
                "memory_intent": "profile_memory",
                "save_scope": "permanent",
                "profile_key": "professional_software_engineering_experience",
                "profile_value": "More than 7 years",
                "confidence": 0.98,
                "rationale": "Separated the rich fact from the exact form option.",
                "metadata": {"attempt_count": 1, "total_latency_ms": 1200},
            },
        )

        interpreted = svc._interpret_pending_user_reply(
            question,
            "I have more than 7 years of software engineering experience; remember it.",
        )
        result = svc._apply_field_approval_answer(
            run,
            question,
            {
                "task_ids": [task_id],
                "answer": interpreted["matched_option"],
                "save_scope": interpreted["save_scope"],
                "raw_user_reply": "I have more than 7 years of software engineering experience; remember it.",
                "reply_interpretation": interpreted,
            },
        )

        assert result["applied"] is True
        assert result["interpretations"][0]["resolved_value"] == "5+"
        assert result["interpretations"][0]["profile_value"] == "More than 7 years"
        # The rich fact is what's remembered; the exact option only binds the live field.
        assert _facts(run.user_id)["professional_software_engineering_experience"] == "More than 7 years"


def test_model_reply_envelope_keeps_salary_veteran_and_disability_memory_canonical() -> None:
    init_db()
    cases = [
        {
            "label": "Which expected salary range should I select? Visible options: $120,000 to $130,000.",
            "raw": "pick 120k to 130k and you can save it to memory",
            "answer": "$120,000 to $130,000",
            "scope": "permanent",
            "profile_key": "salary_expectation",
            "profile_value": "$120,000 to $130,000",
        },
        {
            "label": "Veteran Status. Visible option: I am not a protected veteran.",
            "raw": "im not a protected veteran and save it to profile",
            "answer": "I am not a protected veteran",
            "scope": "permanent",
            "profile_key": "veteran_status",
            "profile_value": "I am not a protected veteran",
        },
        {
            "label": "Disability Status. Visible option: No, I don't have a disability.",
            "raw": "no i do not have any disability",
            "answer": "No, I don't have a disability",
            "scope": "one_time",
            "profile_key": "",
            "profile_value": "",
        },
    ]
    for case in cases:
        with SessionLocal() as db:
            run, task_id, question = _run_with_unsure_field(db, proposed_answer="", label=case["label"])
            result = OperatorChatService(db)._apply_field_approval_answer(
                run,
                question,
                {
                    "task_ids": [task_id],
                    "answer": case["answer"],
                    "raw_user_reply": case["raw"],
                    "save_scope": case["scope"],
                    "reply_interpretation": {
                        "ok": True,
                        "route": "answer",
                        "answer_value": case["answer"],
                        "matched_option": case["answer"],
                        "save_scope": case["scope"],
                        "profile_key": case["profile_key"],
                        "profile_value": case["profile_value"],
                        "confidence": 0.99,
                        "rationale": "Canonical human answer.",
                    },
                },
            )
            assert result["applied"] is True
            assert result["interpretations"][0]["resolved_value"] == case["answer"]
            task = db.get(models.RuntimeTask, task_id)
            assert task.payload["approved_value"] == case["answer"]
            assert task.payload["original_user_reply"] == case["raw"]
            from app.extension_run import _saved_answers_for_user

            db.commit()
            # Answers no longer pile up as saved answers; durable ones become profile facts.
            assert _saved_answers_for_user(run.user_id) == []
            stored = _facts(run.user_id)
            if case["scope"] == "permanent":
                assert result["memory_receipts"][0]["outcome"] == "verified_succeeded"
                assert result["saved_common_answers"][0]["memory_intent"] == "profile_memory"
                assert stored[case["profile_key"]] == case["profile_value"]
                assert all("save it" not in value.lower() for value in stored.values())
                if case["profile_key"] in user_memory.PROFILE_COLUMN_KEYS:
                    profile = db.scalars(select(models.UserProfile).where(models.UserProfile.user_id == run.user_id)).first()
                    assert getattr(profile, case["profile_key"]) == case["profile_value"]
            else:
                # Not asked to remember: nothing is written while answering (the judge decides later).
                assert stored == {}


def test_incomplete_model_profile_memory_contract_does_not_fall_back_to_field_rules() -> None:
    init_db()
    with SessionLocal() as db:
        run, task_id, question = _run_with_unsure_field(db, proposed_answer="", label="Preferred work arrangement")
        result = OperatorChatService(db)._apply_field_approval_answer(
            run,
            question,
            {
                "task_ids": [task_id],
                "answer": "Remote",
                "save_scope": "permanent",
                "reply_interpretation": {
                    "ok": True,
                    "route": "answer",
                    "answer_value": "Remote",
                    "matched_option": "",
                    "memory_intent": "profile_memory",
                    "save_scope": "permanent",
                    "profile_key": "",
                    "profile_value": "",
                    "confidence": 0.95,
                    "rationale": "Incomplete provider envelope.",
                },
            },
        )

        assert result["applied"] is False
        assert result["reason"] == "profile_memory_contract_incomplete"
        assert db.scalars(select(models.AnswerMemory).where(models.AnswerMemory.user_id == run.user_id)).first() is None
        profile = db.scalars(select(models.UserProfile).where(models.UserProfile.user_id == run.user_id)).first()
        assert profile is None or not (profile.profile_json or {}).get("common_answers")


def test_memory_write_produces_a_verified_receipt(monkeypatch) -> None:
    # Saving to profile must yield a memory_write receipt that's VERIFIED by reading the fact back
    # from the memory file - so "saved it to your profile" is grounded, never asserted.
    init_db()
    _judge_returns(monkeypatch, {"key": "years_of_experience", "value": "5+", "section": "work"})
    with SessionLocal() as db:
        run, task_id, question = _run_with_unsure_field(db, proposed_answer="3", is_choice=True, options=["1", "2", "5+"])
        result = OperatorChatService(db)._apply_field_approval_answer(
            run, question, {"task_ids": [task_id], "answer": "5+", "save_scope": "permanent"}
        )
        receipts = result["memory_receipts"]
        assert receipts and receipts[0]["type"] == "memory_write"
        assert receipts[0]["outcome"] == "verified_succeeded"  # read back from the memory file
        assert receipts[0]["target"]["scope"] == "profile"


def test_plain_skip_resolves_field_for_current_application_only(monkeypatch) -> None:
    # Plain skip resolves the field without filling and is carried only in this run's human-answer
    # context. It is not durable answer memory.
    init_db()
    import app.extension_run as ext

    monkeypatch.setattr(ext, "schedule_resume_if_in_browser", lambda run_id: True)
    with SessionLocal() as db:
        run, task_id, question = _run_with_unsure_field(db, proposed_answer="", is_choice=True, options=["1", "2", "5+"], in_browser=True)
        answered = OperatorChatService(db).answer_question(
            question_id=question.id,
            answer="skip",
            payload={"task_ids": [task_id], "skip": True},
            auto_resume=True,
            record_user_message=False,
        )
        result = answered["applied"]
        assert result["applied"] is True
        assert result["skipped"] is True
        assert result["scope"] == "application"
        assert result["resumed"] is True
        task_payload = db.get(models.RuntimeTask, task_id).payload
        assert task_payload["skipped"] is True
        assert task_payload["answer_kind"] == "skip"
        assert task_payload["skip_scope"] == "application"
        from app.extension_run import _human_answers_for_run

        human_answers = _human_answers_for_run(run.id)
        assert human_answers == [
            {
                "question": "What is your expected salary?",
                "answer": "[skip - user prefers not to fill this]",
                "answer_kind": "skip",
                    "scope": "application",
                    "source": "user_skip_preference",
                    "raw_user_answer": "skip",
                }
            ]


def test_failed_browser_answer_resume_becomes_retryable_without_reasking(monkeypatch) -> None:
    init_db()
    import app.extension_run as ext

    monkeypatch.setattr(ext, "schedule_resume_if_in_browser", lambda run_id: False)
    with SessionLocal() as db:
        run, task_id, question = _run_with_unsure_field(
            db,
            proposed_answer="",
            in_browser=True,
            label="How many years of relevant experience do you have?",
        )
        answered = OperatorChatService(db).answer_question(
            question_id=question.id,
            answer="5+ years",
            payload={"task_ids": [task_id]},
            auto_resume=True,
            record_user_message=False,
        )

        refreshed = db.get(models.OperatorRun, run.id)
        retry = dict((refreshed.confidence_state or {}).get("in_browser_resume_retry") or {})
        assert answered["resumed"] is False
        assert refreshed.current_state == "WAITING_FOR_USER"
        assert retry["pending"] is True
        assert retry["source"] == "operator_answer"
        assert retry["question_id"] == question.id
        assert OperatorChatService(db).ensure_question_for_run(run.id) is None


def test_always_skip_persists_as_skip_answer_memory(monkeypatch) -> None:
    init_db()
    import app.extension_run as ext
    from app.lumen_answer_interpreter import LumenAnswerInterpreter, SKIP_ANSWER_TEXT
    from sqlalchemy import select

    monkeypatch.setattr(ext, "schedule_resume_if_in_browser", lambda run_id: True)

    def fake_skip_classifier(self, **kwargs):
        return {"answer_kind": "skip", "scope": "always", "confidence": 0.99, "rationale": "User asked to always skip."}

    monkeypatch.setattr(LumenAnswerInterpreter, "_classify_skip_answer", fake_skip_classifier)
    with SessionLocal() as db:
        run, task_id, question = _run_with_unsure_field(db, proposed_answer="", is_choice=True, options=["1", "2", "5+"], in_browser=True)
        result = OperatorChatService(db)._apply_field_approval_answer(
            run, question, {"task_ids": [task_id], "answer": "always skip this"}
        )
        assert result["applied"] is True
        assert result["skipped"] is True
        assert result["scope"] == "always"
        assert result["saved_common_answers"][0]["answer_kind"] == "skip"
        assert result["memory_receipts"][0]["outcome"] == "verified_succeeded"

        from app.extension_run import _human_answers_for_run, _saved_answers_for_user

        row = db.scalars(select(models.AnswerMemory).where(models.AnswerMemory.user_id == run.user_id)).first()
        assert row is not None
        assert row.approved_answer == SKIP_ANSWER_TEXT
        assert _saved_answers_for_user(run.user_id)[-1]["answer_kind"] == "skip"
        assert _saved_answers_for_user(run.user_id)[-1]["scope"] == "always"

        other_run = models.OperatorRun(user_id=run.user_id, goal="Other", target_url="https://example.test", current_state="RUNNING")
        db.add(other_run)
        db.commit()
        assert _human_answers_for_run(other_run.id) == []


def test_non_mapping_choice_reply_is_not_remembered() -> None:
    # Poison guard: a reply to a CHOICE field that maps to NO option (an instruction like "if its
    # optional avoid it", not a value) must NOT be saved to the answer bank — else it's reused every
    # run, matches nothing, and re-asks forever (the race-field stuck loop the user hit).
    init_db()
    with SessionLocal() as db:
        run, task_id, question = _run_with_unsure_field(
            db, proposed_answer="3", is_choice=True, options=["Asian", "White", "Choose not to disclose"]
        )
        OperatorChatService(db)._apply_field_approval_answer(
            run, question, {"task_ids": [task_id], "answer": "if its optional avoid it", "save_scope": "permanent"}
        )
        from sqlalchemy import select

        rows = list(db.scalars(select(models.AnswerMemory).where(models.AnswerMemory.user_id == run.user_id)).all())
        assert all("avoid" not in str(r.approved_answer).lower() for r in rows)  # nothing poisoned


def test_change_goes_through_the_interpreter() -> None:
    init_db()
    with SessionLocal() as db:
        run, task_id, question = _run_with_unsure_field(db, proposed_answer="120000 USD")
        result = OperatorChatService(db)._apply_field_approval_answer(
            run, question, {"task_ids": [task_id], "answer": "150000"}  # no keep_proposed -> Change
        )
        assert result["applied"] is True
        task = db.get(models.RuntimeTask, task_id)
        assert task.payload["user_choice"] == "change"
        assert task.payload["raw_user_answer"] == "150000"
        assert task.payload["interpreted_answer"]["source"] != "user_kept_proposed"
