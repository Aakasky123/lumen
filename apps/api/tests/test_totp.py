"""TOTP capability: RFC 6238 vectors, seed sources, origin binding, field binding, flag, and no-leak scans."""

from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from typing import Any

import pytest

from app import agent_settings, credential_vault, extension_run, models, totp, vision_agent
from app.db import SessionLocal, init_db
from app.extension_run import run_vision_loop
from tests.test_credential_vault_runtime import (
    _CredentialBody,
    _patch_loop_dependencies,
    _quiet_narrator,
)

SEED = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"
SEED_SPACED = "JBSW Y3DP EHPK 3PXP JBSW Y3DP EHPK 3PXP"


# --- RFC 6238 ------------------------------------------------------------------------------------

_RFC_KEYS = {
    "SHA1": b"12345678901234567890",
    "SHA256": b"12345678901234567890123456789012",
    "SHA512": b"1234567890123456789012345678901234567890123456789012345678901234",
}
_RFC_VECTORS = [
    (59, {"SHA1": "94287082", "SHA256": "46119246", "SHA512": "90693936"}),
    (1111111109, {"SHA1": "07081804", "SHA256": "68084774", "SHA512": "25091201"}),
    (1111111111, {"SHA1": "14050471", "SHA256": "67062674", "SHA512": "99943326"}),
    (1234567890, {"SHA1": "89005924", "SHA256": "91819424", "SHA512": "93441116"}),
    (2000000000, {"SHA1": "69279037", "SHA256": "90698825", "SHA512": "38618901"}),
    (20000000000, {"SHA1": "65353130", "SHA256": "77737706", "SHA512": "47863826"}),
]


@pytest.mark.parametrize("moment,expected", _RFC_VECTORS)
@pytest.mark.parametrize("algorithm", ["SHA1", "SHA256", "SHA512"])
def test_rfc6238_appendix_b_vectors(moment: int, expected: dict[str, str], algorithm: str) -> None:
    secret = base64.b32encode(_RFC_KEYS[algorithm]).decode()
    uri = f"otpauth://totp/Test:a@b?secret={secret}&digits=8&algorithm={algorithm}&period=30"
    params = totp.parse_seed(uri)
    assert params is not None and params.digits == 8 and params.algorithm == algorithm
    assert totp.totp_code(params, at=moment) == expected[algorithm]


def test_default_is_sha1_30s_6_digits_and_a_bare_seed_parses_with_groups() -> None:
    params = totp.parse_seed(SEED_SPACED.lower())
    assert params is not None and (params.digits, params.period, params.algorithm) == (6, 30, "SHA1")
    assert totp.totp_code(params, at=59) == totp.hotp(params.key, 1)
    assert len(totp.totp_code(params, at=59)) == 6
    assert totp.parse_seed(SEED) is not None


def test_bad_seeds_are_refused_and_params_never_print_the_key() -> None:
    assert totp.parse_seed("") is None
    assert totp.parse_seed("not a seed!") is None
    assert totp.parse_seed("JBSWY3DP") is None  # 40 bits: too short
    assert totp.parse_seed("otpauth://hotp/x?secret=" + SEED) is None
    assert totp.parse_seed(f"otpauth://totp/x?secret={SEED}&algorithm=MD5") is None
    assert totp.parse_seed(f"otpauth://totp/x?secret={SEED}&digits=3") is None
    params = totp.parse_seed(SEED)
    assert SEED not in repr(params) and "key" not in repr(params)


def test_find_seeds_in_text_and_goal_origin_naming() -> None:
    text = f"Open https://shop.example.test and use the authenticator key {SEED_SPACED} to log in. Order 1234567."
    assert [totp.seed_identity(s) for s in totp.find_seeds(text)] == [SEED]
    assert totp.find_seeds("No key here, just AUTHENTICATIONCODE words and 123456.") == []
    uri = f"otpauth://totp/Shop:me?secret={SEED}&issuer=Shop"
    assert totp.seed_identity(totp.find_seeds(f"scan {uri} now")[0]) == SEED
    assert totp.goal_names_origin(text, "https://shop.example.test/login")
    assert totp.goal_names_origin("log in to https://github.com/login with the key", "https://github.com/login")
    assert totp.goal_names_origin("log in to www.github.com", "https://github.com/login")
    # Exact host only: a brand, a shared-hosting parent, or a lookalike never matches.
    assert not totp.goal_names_origin("log in to Github with the key", "https://github.com/login")
    assert not totp.goal_names_origin("log in to github.com", "https://attacker.github.io/login")
    assert not totp.goal_names_origin("log in to github.io", "https://attacker.github.io/login")
    assert not totp.goal_names_origin("log in to github.com", "https://github.com.evil.test/login")
    assert not totp.goal_names_origin("log in to mygithub.com", "https://github.com/login")
    assert not totp.goal_names_origin("log in to attacker.github.io", "https://github.io/")
    assert totp.goal_names_origin("log in to attacker.github.io.", "https://attacker.github.io/")
    assert not totp.goal_names_origin(text, "https://evil.test/login")
    assert not totp.goal_names_origin("anything", "")
    assert totp.goal_names_origin("go to http://localhost:5173/otp", "http://localhost:5173/otp")
    assert not totp.goal_names_origin("go to http://localhost:5173/otp", "http://localhost:9999/otp")


# --- vault slot ----------------------------------------------------------------------------------


def test_vault_stores_totp_encrypted_and_rejects_non_seed_values() -> None:
    init_db()
    with SessionLocal() as db:
        user = models.User(email=f"totp-vault-{uuid.uuid4().hex}@example.test", name="Totp Vault")
        db.add(user)
        db.flush()
        origin = f"https://vault-{uuid.uuid4().hex}.example.test"
        item = credential_vault.create(
            db, user_id=user.id, origin=origin, account_label="Main", fields={"totp": SEED}, auto_use=True
        )
        db.commit()
        assert "totp" in item.available_slots_json
        assert SEED not in item.encrypted_payload_json
        assert credential_vault.resolve_slot(db, user_id=user.id, item_id=item.id, slot="totp", url=origin + "/login") == SEED
        with pytest.raises(credential_vault.CredentialVaultError) as raised:
            credential_vault.resolve_slot(db, user_id=user.id, item_id=item.id, slot="totp", url="https://other.example.test/")
        assert SEED not in str(raised.value)
        with pytest.raises(credential_vault.CredentialVaultError) as bad:
            credential_vault.create(
                db, user_id=user.id, origin=origin, account_label="Bad", fields={"totp": "hunter2 hunter2"}
            )
        assert "hunter2" not in str(bad.value)


# --- flag + prompt/schema ------------------------------------------------------------------------


def test_flag_off_by_default_and_schema_and_prompt_do_not_offer_totp() -> None:
    assert agent_settings.DEFAULTS["totp_capability"] is False
    enum_off = vision_agent.action_schema()["schema"]["properties"]["credential_slot"]["enum"]
    assert "totp" not in enum_off
    enum_on = vision_agent.action_schema(totp=True)["schema"]["properties"]["credential_slot"]["enum"]
    assert "totp" in enum_on
    view = {"width": 800, "height": 600}
    off = vision_agent.build_prompt(view, {"goal": "g"})["instruction"]
    on = vision_agent.build_prompt(view, {"goal": "g", "totp_capability": True})["instruction"]
    assert "credential_slot=totp" not in off and "credential_slot=totp" in on
    action = vision_agent.normalize_action(
        {"action": "use_credential", "control": 3, "credential_slot": "totp", "credential_item_id": 0, "reason": "code"}
    )
    assert not action.get("invalid_reason") and action["credential_slot"] == "totp"
    assert action["text"] == "" and action["grounding_quote"] == ""


def test_capability_enabled_by_setting_or_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    init_db()
    with SessionLocal() as db:
        user = models.User(email=f"totp-flag-{uuid.uuid4().hex}@example.test", name="Totp Flag")
        db.add(user)
        db.commit()
        user_id = int(user.id)
    assert extension_run._totp_capability_enabled(user_id) is False
    with SessionLocal() as db:
        agent_settings.update_agent_settings(db, user_id, {"totp_capability": True})
        db.commit()
    assert extension_run._totp_capability_enabled(user_id) is True
    with SessionLocal() as db:
        agent_settings.update_agent_settings(db, user_id, {"totp_capability": False})
        db.commit()
    assert extension_run._totp_capability_enabled(user_id) is False
    monkeypatch.setenv("LUMEN_TOTP_CAPABILITY", "1")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        assert extension_run._totp_capability_enabled(user_id) is True
    finally:
        monkeypatch.delenv("LUMEN_TOTP_CAPABILITY", raising=False)
        get_settings.cache_clear()


# --- the loop ------------------------------------------------------------------------------------


class _OtpBody(_CredentialBody):
    """A login's second step: one code field, optionally with the authenticator key printed on the page."""

    def __init__(self, url: str, *, page_text: str = "Two-step verification. Enter the 6-digit code.") -> None:
        super().__init__(url)
        self.page_text = page_text

    async def capture_screenshot(self) -> dict[str, Any]:
        shot = await super().capture_screenshot()
        if not self.typed:
            shot["page_evidence"]["viewport_text"] = self.page_text
            shot["page_evidence"]["controls"][0].update(
                {"type": "text", "context_text": "Verification code", "selector": "#otp"}
            )
        return shot


def _create_totp_run(*, target_url: str, goal: str) -> tuple[int, int]:
    init_db()
    from app.config import get_settings

    goal, stored = totp.protect_goal(goal, get_settings().token_encryption_key)
    with SessionLocal() as db:
        user = models.User(email=f"totp-runtime-{uuid.uuid4().hex}@example.test", name="Totp Runtime")
        db.add(user)
        db.flush()
        run = models.OperatorRun(
            user_id=user.id,
            application_id=None,
            goal=goal,
            target_url=target_url,
            current_state="RUNNING",
            confidence_state={"totp_seeds": stored} if stored else {},
        )
        db.add(run)
        db.commit()
        return int(run.id), int(user.id)


def _totp_action(item_id: int = 0) -> dict[str, Any]:
    return {
        "action": "use_credential",
        "control": 1,
        "x": 220,
        "y": 160,
        "text": "",
        "question": "Verification code",
        "credential_item_id": item_id,
        "credential_slot": "totp",
        "reason": "Enter the authenticator code.",
    }


def _accepting_totp_classifier(calls: list[dict[str, Any]]):
    def classify(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(dict(kwargs))
        return {
            "slot": "totp",
            "usage": "login",
            "confidence": 0.97,
            "reason": "The control takes an authenticator code.",
            "evidence": "The page asks for the six-digit code.",
        }

    return classify


def _run_totp(
    monkeypatch: pytest.MonkeyPatch,
    *,
    goal: str,
    body: _OtpBody,
    run_id: int,
    user_id: int,
    capability: bool = True,
    classifier: Any = None,
    metadata: list[dict[str, Any]] | None = None,
    vault_seed: str | None = None,
    item_id: int = 0,
    steps: int = 3,
    window_wait: Any = None,
) -> dict[str, Any]:
    events: list[tuple[str, dict[str, Any]]] = []
    contexts: list[dict[str, Any]] = []
    narrations: list[tuple[str, dict[str, Any]]] = []
    resolver_calls: list[dict[str, Any]] = []
    _patch_loop_dependencies(monkeypatch, events=events)
    monkeypatch.setattr(extension_run, "_totp_capability_enabled", lambda _uid: capability)

    async def _no_wait(_params: Any) -> None:
        return None

    monkeypatch.setattr(extension_run, "_totp_window_wait", window_wait or _no_wait)
    monkeypatch.setattr(extension_run, "_credential_vault_metadata_for_url", lambda *_a, **_k: list(metadata or []))
    monkeypatch.setattr(
        extension_run,
        "_credential_vault_resolve_slot",
        lambda *a, **k: resolver_calls.append(dict(k)) or (vault_seed or ""),
    )
    marked: list[dict[str, Any]] = []
    monkeypatch.setattr(extension_run, "_credential_vault_mark_used", lambda *a, **k: marked.append(dict(k)))
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(extension_run, "_call_totp_target_classifier", classifier or _accepting_totp_classifier(calls))

    def decide(_uid: int, _image: dict[str, Any], _view: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        contexts.append(context)
        if not body.typed and sum(1 for c in contexts if c) <= 1:
            return _totp_action(item_id)
        return {"action": "done", "reason": "Finished."}

    monkeypatch.setattr(extension_run, "_decide_vision_action", decide)

    async def narrate(kind: str, data: dict[str, Any]) -> None:
        narrations.append((kind, data))

    summary = asyncio.run(
        run_vision_loop(
            body, url=None, narrate=narrate, run_id=run_id, user_id=user_id, skip_navigate=True, max_steps=steps
        )
    )
    with SessionLocal() as db:
        state = dict(db.get(models.OperatorRun, run_id).confidence_state or {})
    return {
        "summary": summary,
        "events": events,
        "contexts": contexts,
        "narrations": narrations,
        "resolver_calls": resolver_calls,
        "marked": marked,
        "classifier_calls": calls,
        "state": state,
    }


def _typed_code_is_current(code: str, started: float, finished: float) -> bool:
    candidates = {
        totp.totp_code(totp.parse_seed(SEED), at=moment)
        for moment in (started - 1, started, finished, finished + 1)
    }
    return code in candidates


def test_vault_seed_types_the_code_and_the_seed_never_leaks(monkeypatch: pytest.MonkeyPatch) -> None:
    origin = f"https://vault-{uuid.uuid4().hex}.example.test"
    url = f"{origin}/two-step"
    run_id, user_id = _create_totp_run(target_url=url, goal=f"Sign in at {url} and finish the two-step check.")
    body = _OtpBody(url)
    metadata = [{"id": 31337, "origin": origin, "account_label": "Main", "available_slots": ["totp"], "auto_use": True}]
    started = time.time()
    out = _run_totp(
        monkeypatch, goal="", body=body, run_id=run_id, user_id=user_id, metadata=metadata, vault_seed=SEED, item_id=31337
    )
    finished = time.time()
    assert out["summary"]["state"] == "READY_FOR_REVIEW"
    assert len(body.credential_calls) == 1
    selector, typed, kwargs = body.credential_calls[0]
    assert selector == "#otp" and len(typed) == 6 and typed.isdigit()
    assert _typed_code_is_current(typed, started, finished)
    assert out["resolver_calls"] == [{"item_id": 31337, "slot": "totp", "url": url}]
    assert out["marked"] and out["marked"][0]["item_id"] == 31337
    assert body.ordinary_type_calls == []
    assert out["classifier_calls"][0]["seed_source"] == "vault"
    blob = json.dumps(
        {k: out[k] for k in ("summary", "events", "narrations", "state", "contexts")}, default=str
    )
    for secret in (SEED, SEED_SPACED, typed):
        assert secret not in blob
    assert out["summary"]["receipts"][0]["verification"]["active_value_redacted"] is True


def test_goal_seed_for_the_named_site_types_the_code_without_the_brain_seeing_it_move(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin = f"https://goal-{uuid.uuid4().hex}.example.test"
    url = f"{origin}/two-step"
    goal = f"Log in to {origin} and when it asks for the 2-step code use the authenticator key {SEED_SPACED}."
    run_id, user_id = _create_totp_run(target_url=url, goal=goal)
    body = _OtpBody(url)
    started = time.time()
    out = _run_totp(monkeypatch, goal=goal, body=body, run_id=run_id, user_id=user_id)
    finished = time.time()
    assert out["summary"]["state"] == "READY_FOR_REVIEW"
    typed = body.credential_calls[0][1]
    assert _typed_code_is_current(typed, started, finished)
    assert out["resolver_calls"] == [] and out["marked"] == []
    call = out["classifier_calls"][0]
    assert call["seed_source"] == "goal"
    assert any(SEED in form for form in call["forbidden"])
    # F1: the seed is in no stored or shown text: not the persisted goal, the run state, the brain's packets
    # (prompts), the events, the narrations or the summary; the goal carries a placeholder.
    blob = json.dumps({k: out[k] for k in ("summary", "events", "narrations", "contexts", "state")}, default=str)
    assert SEED not in blob and SEED_SPACED not in blob and typed not in blob
    assert "[totp seed provided]" in blob
    with SessionLocal() as db:
        run = db.get(models.OperatorRun, run_id)
        assert SEED not in run.goal and SEED_SPACED not in run.goal and "[totp seed provided]" in run.goal
        stored = json.dumps(run.confidence_state, default=str)
        assert SEED not in stored and SEED_SPACED not in stored
        assert (run.confidence_state or {}).get("totp_seeds")
    with SessionLocal() as db:
        events = json.dumps(
            [(e.event_type, e.event_payload) for e in db.query(models.CognitiveEvent).filter_by(run_id=run_id)], default=str
        )
    assert SEED not in events and SEED_SPACED not in events
    built = vision_agent.build_prompt({"width": 800, "height": 600}, {"goal": json.dumps(out["contexts"][0].get("goal"))})
    assert SEED not in json.dumps(built, default=str)


def test_goal_seed_is_refused_on_a_site_the_goal_does_not_name(monkeypatch: pytest.MonkeyPatch) -> None:
    url = f"https://phish-{uuid.uuid4().hex}.example.test/two-step"
    goal = f"Log in to https://bank-{uuid.uuid4().hex}.legit.test and use the authenticator key {SEED}."
    run_id, user_id = _create_totp_run(target_url=url, goal=goal)
    body = _OtpBody(url)
    out = _run_totp(monkeypatch, goal=goal, body=body, run_id=run_id, user_id=user_id)
    assert body.credential_calls == [] and body.ordinary_type_calls == []
    assert out["classifier_calls"] == []
    assert "totp_origin_not_named_by_goal" in json.dumps(out["contexts"][1:], default=str)
    assert SEED not in json.dumps({k: out[k] for k in ("summary", "events", "narrations")}, default=str)


def test_vault_item_for_another_origin_is_refused_before_any_seed_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    origin = f"https://right-{uuid.uuid4().hex}.example.test"
    url = f"{origin}/two-step"
    run_id, user_id = _create_totp_run(target_url=url, goal=f"Sign in at {url}.")
    body = _OtpBody(url)
    metadata = [
        {"id": 777, "origin": f"https://wrong-{uuid.uuid4().hex}.example.test", "account_label": "x", "available_slots": ["totp"], "auto_use": True}
    ]
    out = _run_totp(monkeypatch, goal="", body=body, run_id=run_id, user_id=user_id, metadata=metadata, vault_seed=SEED, item_id=777)
    assert out["resolver_calls"] == [] and body.credential_calls == []


def test_a_field_that_is_not_a_one_time_code_field_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    origin = f"https://field-{uuid.uuid4().hex}.example.test"
    url = f"{origin}/profile"
    goal = f"Update the profile at {origin}; the authenticator key is {SEED}."
    run_id, user_id = _create_totp_run(target_url=url, goal=goal)
    body = _OtpBody(url, page_text="Profile. Display name.")
    calls: list[dict[str, Any]] = []

    def reject(*_a: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(dict(kwargs))
        return {"slot": "none", "usage": "none", "confidence": 0.99, "reason": "An ordinary profile field.", "evidence": "No code prompt."}

    out = _run_totp(monkeypatch, goal=goal, body=body, run_id=run_id, user_id=user_id, classifier=reject)
    assert calls and body.credential_calls == [] and body.ordinary_type_calls == []
    assert out["classifier_calls"] == [] and "credential_target_semantics_rejected" in json.dumps(out["contexts"][1:], default=str)


def test_flag_off_the_action_is_unavailable_and_nothing_is_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    origin = f"https://off-{uuid.uuid4().hex}.example.test"
    url = f"{origin}/two-step"
    goal = f"Log in to {origin} using the authenticator key {SEED}."
    run_id, user_id = _create_totp_run(target_url=url, goal=goal)
    body = _OtpBody(url)
    out = _run_totp(monkeypatch, goal=goal, body=body, run_id=run_id, user_id=user_id, capability=False)
    assert body.credential_calls == [] and out["classifier_calls"] == [] and out["resolver_calls"] == []
    assert all("totp_capability" not in context for context in out["contexts"])


def test_page_seed_is_judged_with_source_page_and_refused_when_the_task_does_not_direct_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin = f"https://page-{uuid.uuid4().hex}.example.test"
    url = f"{origin}/enroll"
    goal = f"Enter the 6-digit code at {url}."
    run_id, user_id = _create_totp_run(target_url=url, goal=goal)
    body = _OtpBody(url, page_text=f"Authenticator setup. Can't scan? Secret key: {SEED}. Then enter the 6-digit code.")
    calls: list[dict[str, Any]] = []

    def not_directed(*_a: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(dict(kwargs))
        return {"slot": "none", "usage": "none", "confidence": 0.9, "reason": "The task does not direct it.", "evidence": "n/a"}

    out = _run_totp(monkeypatch, goal=goal, body=body, run_id=run_id, user_id=user_id, classifier=not_directed)
    assert calls and calls[0]["seed_source"] == "page" and any(SEED in form for form in calls[0]["forbidden"])
    assert body.credential_calls == []


def test_two_keys_on_the_page_are_refused_not_guessed(monkeypatch: pytest.MonkeyPatch) -> None:
    origin = f"https://two-{uuid.uuid4().hex}.example.test"
    url = f"{origin}/enroll"
    run_id, user_id = _create_totp_run(target_url=url, goal=f"Finish the authenticator step at {url}.")
    other = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    body = _OtpBody(url, page_text=f"Key A {SEED} and key B {other}.")
    out = _run_totp(monkeypatch, goal="", body=body, run_id=run_id, user_id=user_id)
    assert body.credential_calls == [] and out["classifier_calls"] == []


def test_the_classifier_call_receives_the_page_and_goal_with_the_seed_blanked(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class _FakeClient:
        def __init__(self, _db: Any, user_id: int) -> None:
            pass

        def classify_totp_target(self, **kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {"slot": "none", "usage": "none", "confidence": 0.5, "reason": "r", "evidence": "e"}

    monkeypatch.setattr("app.lumen_model.LumenModelClient", _FakeClient)
    forbidden = totp.seed_redaction_strings(SEED)
    extension_run._call_totp_target_classifier(
        1,
        page_evidence={"viewport_text": f"Key {SEED} and {SEED_SPACED}", "controls": [{"label": SEED}]},
        control_evidence={"label": "Code"},
        action_evidence={"credential_slot": "totp"},
        goal=f"use {SEED}",
        seed_source="page",
        forbidden=forbidden,
    )
    shown = json.dumps({k: v for k, v in captured.items() if k != "forbidden_strings"}, default=str)
    assert SEED not in shown and SEED_SPACED not in shown
    assert "[SEED]" in shown


# --- F1: the seed is taken out of the goal at run creation ----------------------------------------------


def test_protect_goal_replaces_every_form_and_stores_the_seed_encrypted() -> None:
    key = "unit-test-key"
    uri = "otpauth://totp/Shop:me?secret=GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ&issuer=Shop"
    goal = f"Sign in at https://shop.example.test with key {SEED_SPACED}, backup {uri}. Order 1234567."
    cleaned, stored = totp.protect_goal(goal, key)
    assert cleaned.count("[totp seed provided]") == 2
    for form in (SEED, SEED_SPACED, "GEZDGNBVGY3TQOJQ", "otpauth://"):
        assert form not in cleaned
    assert "Order 1234567" in cleaned and "https://shop.example.test" in cleaned
    assert len(stored) == 2 and all(SEED not in item and "GEZDGNBV" not in item for item in stored)
    assert {totp.seed_identity(x) for x in totp.reveal_seeds(stored, key)} == {SEED, "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"}
    assert totp.reveal_seeds(stored, "another-key") == []
    # No key: still cleaned, nothing stored in plaintext.
    assert totp.protect_goal(goal, None)[1] == [] and SEED not in totp.protect_goal(goal, None)[0]


def test_runtime_creation_paths_never_persist_the_seed() -> None:
    from app.runtime import OperatorRuntimeEngine as LumenRuntime

    init_db()
    goal = f"Log in at https://shop.example.test using the authenticator key {SEED} and finish."
    with SessionLocal() as db:
        user = models.User(email=f"totp-create-{uuid.uuid4().hex}@example.test", name="Totp Create")
        db.add(user)
        db.flush()
        runtime = LumenRuntime(db)
        run = runtime.create_browser_task(
            user_id=int(user.id), goal=goal, target_url="https://shop.example.test/login"
        ) if hasattr(runtime, "create_browser_task") else None
        assert run is not None
        db.commit()
        run_id = int(run.id)
    with SessionLocal() as db:
        persisted = db.get(models.OperatorRun, run_id)
        everything = json.dumps(
            [persisted.goal, persisted.confidence_state]
            + [(e.event_type, e.event_payload) for e in db.query(models.CognitiveEvent).filter_by(run_id=run_id)],
            default=str,
        )
    assert SEED not in everything and "[totp seed provided]" in everything
    assert (persisted.confidence_state or {}).get("totp_seeds")


# --- F3: the frame that owns the control ---------------------------------------------------------


@pytest.mark.parametrize("frame", [{"frame_id": 5}, {"frame_url": "https://evil.example.test/f"}, {"frame_origin": "https://evil.example.test"}])
def test_a_control_in_a_foreign_or_unidentified_frame_is_refused(frame: dict[str, Any]) -> None:
    page = "https://shop.example.test/login"
    assert extension_run._frame_origin_matches_page({"selector": "#otp", **frame}, page) is False
    assert extension_run._frame_origin_matches_page({"selector": "#otp"}, page) is True
    assert extension_run._frame_origin_matches_page({"selector": "#otp", "frame_id": 0}, page) is True
    assert extension_run._frame_origin_matches_page({"selector": "#otp", "frame_id": 3, "frame_url": "https://shop.example.test/embed"}, page) is True
    assert extension_run._frame_origin_matches_page(None, page) is False


@pytest.mark.parametrize("slot", ["totp", "email"])
def test_credential_into_a_cross_origin_frame_control_is_refused_in_the_loop(monkeypatch: pytest.MonkeyPatch, slot: str) -> None:
    origin = f"https://frame-{uuid.uuid4().hex}.example.test"
    url = f"{origin}/two-step"
    goal = f"Sign in at {origin} using the authenticator key {SEED}."
    run_id, user_id = _create_totp_run(target_url=url, goal=goal)

    class _FramedBody(_OtpBody):
        async def capture_screenshot(self) -> dict[str, Any]:
            shot = await super().capture_screenshot()
            for control in shot["page_evidence"]["controls"]:
                control.update({"frame_id": 7, "frame_url": "https://evil.example.test/embed"})
            return shot

    body = _FramedBody(url)
    metadata = [{"id": 41, "origin": origin, "account_label": "Main", "available_slots": [slot], "auto_use": True}]
    out = _run_totp(monkeypatch, goal=goal, body=body, run_id=run_id, user_id=user_id, metadata=metadata, vault_seed="x" * 8, item_id=41)
    assert body.credential_calls == [] and out["resolver_calls"] == [] and out["classifier_calls"] == []
    assert "credential_control_in_foreign_frame" in json.dumps(out["contexts"][1:], default=str)


# --- F5: never type a code that is about to expire -------------------------------------------------


def test_window_wait_sleeps_until_the_next_window_when_under_five_seconds_remain(monkeypatch: pytest.MonkeyPatch) -> None:
    params = totp.parse_seed(SEED)
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(extension_run.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(totp, "seconds_left", lambda _p, at=None: 3.0)
    asyncio.run(extension_run._totp_window_wait(params))
    assert len(slept) == 1 and 3.0 <= slept[0] < 4.0
    monkeypatch.setattr(totp, "seconds_left", lambda _p, at=None: 12.0)
    asyncio.run(extension_run._totp_window_wait(params))
    assert len(slept) == 1


def test_the_loop_waits_for_the_window_before_computing_the_code(monkeypatch: pytest.MonkeyPatch) -> None:
    origin = f"https://wait-{uuid.uuid4().hex}.example.test"
    url = f"{origin}/two-step"
    goal = f"Sign in at {origin} using the authenticator key {SEED}."
    run_id, user_id = _create_totp_run(target_url=url, goal=goal)
    body = _OtpBody(url)
    waits: list[Any] = []

    async def record_wait(params: Any) -> None:
        waits.append(params)

    out = _run_totp(monkeypatch, goal=goal, body=body, run_id=run_id, user_id=user_id, window_wait=record_wait)
    assert len(waits) == 1 and out["summary"]["state"] == "READY_FOR_REVIEW" and len(body.credential_calls) == 1
