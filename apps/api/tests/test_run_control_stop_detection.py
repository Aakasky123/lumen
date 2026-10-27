"""Explicit end/stop commands must DETERMINISTICALLY route to run_control "stop" — so the run is
actually cancelled, not just claimed by the model. Regression: "end that"/"end it" (which the busy
message literally tells the user to type) used to miss detection, so the LLM fabricated "run ended"
while the run stayed active."""

import pytest

from app.lumen_model import LumenModelClient
from app.db import SessionLocal, init_db


@pytest.fixture(scope="module")
def client():
    init_db()
    with SessionLocal() as db:
        yield LumenModelClient(db)


@pytest.mark.parametrize("message", ["end it", "end that", "end this", "stop", "stop it", "stop the run", "cancel", "cancel this", "kill it", "end the application"])
def test_explicit_end_commands_route_to_stop(client, message) -> None:
    scope = client.classify_operator_chat_scope({"message": message, "view_mode": "user"})
    assert scope.get("action") == "stop", f"{message!r} -> {scope.get('action')!r}"


@pytest.mark.parametrize("message", ["end of day summary please", "apply to this job", "what's the status"])
def test_non_commands_do_not_route_to_stop(client, message) -> None:
    scope = client.classify_operator_chat_scope({"message": message, "view_mode": "user"})
    assert scope.get("action") != "stop", f"{message!r} wrongly -> stop"


def test_fastpath_only_for_explicit_commands_else_brain(client) -> None:
    # An explicit command may skip the brain (fast + actually acts).
    assert client._operator_chat_scope_fast_path_reason({"action": "stop", "primary_intent": "run_control"}, {"message": "stop"}, identity_question=False) == "clear_action_stop"
    # A loose keyword hit ("cancel" buried in a sentence) must NOT fast-path -> the brain decides,
    # so it never stops a run on a stray word.
    assert client._operator_chat_scope_fast_path_reason({"action": "stop", "primary_intent": "run_control", "run_related": True}, {"message": "please cancel my subscription"}, identity_question=False) is None
    # Ambiguous, action-less domain messages no longer short-circuit (old clear_domain_scope) ->
    # they reach the brain to be understood.
    assert client._operator_chat_scope_fast_path_reason({"action": "none", "run_related": True}, {"message": "i think i'm done with that one, what now"}, identity_question=False) is None
    # Trivial turns still fast-path (no model call).
    assert client._operator_chat_scope_fast_path_reason({"primary_intent": "greeting", "action": "none"}, {"message": "hi"}, identity_question=False) == "obvious_greeting"
