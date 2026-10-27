"""A checkbox group is asked once as a multi-select question and answered with a set of live options.

Run 1791: "Which of the following factors are most important to you? (Select 3)" was asked as a
single-choice card, the user could pick one option, and the brain asked again for the rest.
"""

import uuid

from app import models, vision_agent
from app.lumen_model import LumenModelClient
from app.cognition import OperatorChatService
from app.db import SessionLocal, init_db
from app.extension_run import _human_answers_for_run, _trim_answer_items
from app.runtime import OperatorRuntimeEngine

FACTORS = ["Challenging problems", "Fast-paced startup", "Ownership", "Work-life balance"]


def _run_with_choice(db, *, multiple: bool):
    user = models.User(email=f"multi-{uuid.uuid4().hex}@example.com", name="Multi")
    db.add(user)
    db.flush()
    run = models.OperatorRun(
        user_id=user.id,
        goal="Apply",
        target_url="https://ats.example.com/apply",
        current_state="WAITING_FOR_USER",
        confidence_state={"in_browser": {"url": "https://ats.example.com/apply", "device_id": "test-device"}},
    )
    db.add(run)
    db.commit()
    task_id = OperatorRuntimeEngine(db)._phase2_upsert_required_field_approval_task(
        run,
        field={
            "node_id": "factors",
            "snapshot_id": "s1",
            "selector": "#factor-1",
            "label": "Which factors matter most to you? (Select 3)",
            "options": FACTORS,
            "is_choice": True,
            "multiple": multiple,
            "required": True,
            "is_unsure": True,
            "element_ref": {"selector": "#factor-1", "control_signature": "sig-factor-1"},
        },
        source="in_browser_field",
    )
    db.commit()
    question = OperatorChatService(db).ensure_question_for_run(run.id, thread_id=None)
    thread = models.ChatThread(user_id=run.user_id, run_id=run.id, mode="run", status="active")
    db.add(thread)
    db.flush()
    question.thread_id = thread.id
    db.commit()
    return run, task_id, question, thread


def test_a_multi_select_field_is_marked_on_the_question_contract() -> None:
    init_db()
    with SessionLocal() as db:
        _run, task_id, question, _thread = _run_with_choice(db, multiple=True)
        field = question.expected_answer_schema["fields"][0]
        assert field["multiple"] is True
        assert field["options"] == FACTORS
        assert field["display_reason"] == "Choose one or more of the live options observed from the form."
        assert "Pick one or more" in db.get(models.RuntimeTask, task_id).payload["question_text"]

        _run, _task_id, single, _thread = _run_with_choice(db, multiple=False)
        assert single.expected_answer_schema["fields"][0]["multiple"] is False


def test_a_card_with_several_options_applies_the_set_and_hands_it_to_the_brain(monkeypatch) -> None:
    init_db()
    import app.extension_run as ext

    monkeypatch.setattr(ext, "schedule_resume_if_in_browser", lambda run_id: True)
    with SessionLocal() as db:
        run, task_id, _question, thread = _run_with_choice(db, multiple=True)
        run_id = run.id
        service = OperatorChatService(db)
        monkeypatch.setattr(
            service,
            "_interpret_pending_user_reply",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("an exact option set is not reinterpreted")),
        )

        result = service._try_answer_pending_question_from_chat(
            thread,
            "Work-life balance, Challenging problems",
            # Out of display order on purpose: the stored set follows the field's own option order.
            payload={"selected_options": ["Work-life balance", "Challenging problems"], "task_ids": [task_id]},
            chat_scope={"action": "answer_question"},
        )
        db.commit()

        assert result["applied"] is True
        payload = db.get(models.RuntimeTask, task_id).payload
        assert payload["approved_value"] == "Challenging problems; Work-life balance"
        assert payload["interpreted_answer"]["selected_options"] == ["Challenging problems", "Work-life balance"]
        assert payload["interpreted_answer"]["source"] == "user_picked_options"

    answers = _human_answers_for_run(run_id)
    assert answers[0]["selected_options"] == ["Challenging problems", "Work-life balance"]
    # The ask was bound to one checkbox of the group; that control is not the target of every pick.
    assert "target_selector" not in answers[0] and "target_control_signature" not in answers[0]
    trimmed = _trim_answer_items(answers, limit=6, text_limit=160)
    assert trimmed[0]["selected_options"] == ["Challenging problems", "Work-life balance"]


def test_several_options_never_bind_on_a_single_choice_field_or_outside_the_options() -> None:
    init_db()
    with SessionLocal() as db:
        _run, task_id, question, _thread = _run_with_choice(db, multiple=False)
        service = OperatorChatService(db)
        two = {"selected_options": ["Ownership", "Work-life balance"], "task_ids": [task_id]}
        assert service._structured_pending_field_action(question, two) is None

        _run, task_id, question, _thread = _run_with_choice(db, multiple=True)
        foreign = {"selected_options": ["Ownership", "Free lunch"], "task_ids": [task_id]}
        assert service._structured_pending_field_action(question, foreign) is None
        both = {"selected_option": "Ownership", "selected_options": ["Ownership"], "task_ids": [task_id]}
        assert service._structured_pending_field_action(question, both) is None
        ok = service._structured_pending_field_action(question, {"selected_options": ["Ownership"], "task_ids": [task_id]})
        assert ok == {"action": "selected_options", "selected_options": ["Ownership"], "selected_option": "Ownership"}


def _interpret(monkeypatch, *, multiple: bool, matched_options: list[str]) -> dict:
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
                    "answer_value": "",
                    "matched_option": "",
                    "matched_options": matched_options,
                    "workflow_instruction": "",
                    "memory_intent": "current_answer",
                    "profile_key": "",
                    "profile_value": "",
                    "confidence": 0.95,
                },
                "metadata": {},
            },
        )
        return client.interpret_pending_user_reply(
            {
                "question_type": "field_approval",
                "question_text": "Which factors matter most to you? (Select 3)",
                "visible_options": FACTORS,
                "answer_contract": {"is_choice": True, "multiple": multiple},
                "user_reply": "ownership and work life balance",
            }
        )


def test_a_typed_reply_binds_several_options_only_on_a_multi_select_contract(monkeypatch) -> None:
    init_db()
    result = _interpret(monkeypatch, multiple=True, matched_options=["work-life balance", "Ownership"])
    assert result["ok"] is True
    assert result["matched_options"] == ["Ownership", "Work-life balance"]
    assert result["answer_value"] == "Ownership; Work-life balance"

    assert _interpret(monkeypatch, multiple=True, matched_options=["Ownership", "Free lunch"])["matched_options"] == []
    assert _interpret(monkeypatch, multiple=False, matched_options=["Ownership", "Work-life balance"])["matched_options"] == []


def test_the_brain_can_flag_a_multi_select_ask() -> None:
    flagged = vision_agent.normalize_action(
        {"action": "ask_user", "question": "Pick 3", "options": ["A", "B", "C"], "multi_select": True}
    )
    assert flagged["multi_select"] is True and not flagged["invalid_reason"]
    assert vision_agent.normalize_action({"action": "ask_user", "question": "Pick one", "options": ["A", "B"]})["multi_select"] is False
    assert "multi_select" in vision_agent.action_schema()["schema"]["properties"]


def test_a_reply_to_a_stuck_in_browser_run_reaches_the_brain_and_resumes(monkeypatch) -> None:
    """Run 1791: replies to a generic blocked_reason card were filed in the goal workspace and the
    run never resumed. On an in-browser run the reply is an operator instruction and the loop resumes."""
    init_db()
    import app.extension_run as ext

    scheduled: list[int] = []
    monkeypatch.setattr(ext, "schedule_resume_if_in_browser", lambda run_id: scheduled.append(run_id) or True)
    with SessionLocal() as db:
        user = models.User(email=f"stuck-{uuid.uuid4().hex}@example.com", name="Stuck")
        db.add(user)
        db.flush()
        run = models.OperatorRun(
            user_id=user.id,
            goal="Apply",
            target_url="https://ats.example.com/apply",
            current_state="WAITING_FOR_USER",
            status_message="Waiting for your answer: Which factors matter most? (Select 3)",
            confidence_state={"in_browser": {"url": "https://ats.example.com/apply", "device_id": "test-device"}},
        )
        db.add(run)
        db.flush()
        question = models.OperatorQuestion(
            run_id=run.id,
            question_type="blocked_reason",
            stuck_phase="WAITING_FOR_USER",
            stuck_reason=run.status_message,
            prompt=f"I am stuck because: {run.status_message}.",
            expected_answer_schema={"answer": "string"},
            status="pending",
        )
        db.add(question)
        db.commit()

        result = OperatorChatService(db).answer_question(question_id=question.id, answer="Continue", payload={})
        db.commit()

        assert result["applied"]["target"] == "operator_instruction"
        assert scheduled == [run.id]
        instructions = db.get(models.OperatorRun, run.id).confidence_state["operator_instructions"]
        assert instructions[-1]["instruction"] == "Continue" and instructions[-1]["question_id"] == question.id


def test_an_ask_whose_options_are_page_checkboxes_is_multi_select_without_the_flag() -> None:
    from app.extension_run import _options_are_page_checkboxes

    evidence = {
        "controls": [
            {"tag": "input", "type": "checkbox", "text": "Fast-paced, high-growth  startup environment"},
            {"tag": "input", "type": "checkbox", "text": "Work-life balance"},
            {"tag": "input", "type": "radio", "text": "Full Stack Engineer"},
        ]
    }
    assert _options_are_page_checkboxes(["Fast-paced, high-growth startup environment", "work-life balance"], evidence)
    assert not _options_are_page_checkboxes(["Work-life balance", "Full Stack Engineer"], evidence)
    assert not _options_are_page_checkboxes([], evidence)


def test_a_single_choice_answer_is_not_pinned_to_the_option_the_ask_was_bound_to(monkeypatch) -> None:
    """Run 1791: 'How did you discover Infisical?' was bound to the GitHub radio; the user picked
    'Job post or careers page' and every click on that radio was refused as the wrong target."""
    init_db()
    import app.extension_run as ext

    monkeypatch.setattr(ext, "schedule_resume_if_in_browser", lambda run_id: True)
    with SessionLocal() as db:
        run, task_id, _question, thread = _run_with_choice(db, multiple=False)
        run_id = run.id
        result = OperatorChatService(db)._try_answer_pending_question_from_chat(
            thread,
            "Ownership",
            payload={"selected_option": "Ownership", "task_ids": [task_id]},
            chat_scope={"action": "answer_question"},
        )
        db.commit()
        assert result["applied"] is True

    answer = _human_answers_for_run(run_id)[0]
    assert answer["answer"] == "Ownership"
    assert "target_selector" not in answer and "target_control_signature" not in answer
    assert answer["target_binding_key"]


def test_options_named_for_a_text_box_are_suggestions_not_choices() -> None:
    """Run 1796: the brain asked Pronouns (a text box) with the lone option "n/a"; the card became a
    one-option choice and the user's "He him" was recorded as "n/a"."""
    from app.extension_run import _ask_choices_and_suggestions

    text_box = {"tag": "input", "type": "text"}
    assert _ask_choices_and_suggestions(["n/a"], text_box) == ([], ["n/a"])
    assert _ask_choices_and_suggestions(["n/a"], {"tag": "textarea"}) == ([], ["n/a"])
    # A combobox's input fronts a list; radios, selects, and an unbound ask keep their choices.
    assert _ask_choices_and_suggestions(["Yes", "No"], {"tag": "input", "type": "text", "role": "combobox"}) == (["Yes", "No"], [])
    assert _ask_choices_and_suggestions(["Yes", "No"], {"tag": "input", "type": "radio"}) == (["Yes", "No"], [])
    assert _ask_choices_and_suggestions(["Yes", "No"], {"tag": "select"}) == (["Yes", "No"], [])
    assert _ask_choices_and_suggestions(["Yes", "No"], None) == (["Yes", "No"], [])
    assert _ask_choices_and_suggestions(None, text_box) == ([], [])
