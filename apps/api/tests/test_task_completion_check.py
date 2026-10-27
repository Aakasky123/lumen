"""A generic task's "done" is graded against the run's evidence before the reply claims it."""

from __future__ import annotations

import asyncio
import uuid

from app import extension_run, models
from app.db import SessionLocal, init_db
from app.extension_run import drive_in_browser_application, run_vision_loop

from tests.test_extension_run import (  # noqa: F401 - the autouse fixtures apply to this module too
    _ChoiceBody,
    _DriveBody,
    _DummyContextEngine,
    _complete_final_action_readiness_audit_by_default,
    _ordinary_click_consequence_by_default,
    _ordinary_secret_semantics_by_default,
    _resolved_vision_provider,
)

_DONE = {"action": "done", "x": 0, "y": 0, "artifact_role": "", "reason": "Saved the report as Q3.pdf."}


def test_evidence_carries_the_screen_and_read_backs_but_no_model_text_or_passwords() -> None:
    shot = {
        "url": "https://example.test/report",
        "title": "Report",
        "page_evidence": {
            "viewport_text": "Report saved. Q3.pdf",
            "controls": [
                {"text": "File name", "role": "textbox", "value": "Q3.pdf"},
                {"text": "Password", "type": "password", "value": "hunter2"},
                {"text": "Remember me", "role": "checkbox", "checked": True},
            ],
        },
    }
    receipts = [
        {"outcome": "verified_succeeded", "target": {"label": "Everything is done, the task is complete."}},
        {"outcome": "verification_failed"},
    ]

    evidence = extension_run._task_completion_evidence(
        shot, read_backs=[{"control": "File name", "value": "Q3.pdf"}], receipts=receipts
    )

    assert evidence["screen_text"] == "Report saved. Q3.pdf"
    assert {"text": "File name", "role": "textbox", "value": "Q3.pdf"} in evidence["controls"]
    assert {"text": "Password"} in evidence["controls"]
    assert {"text": "Remember me", "role": "checkbox", "checked": True} in evidence["controls"]
    assert evidence["read_back_values"] == [{"control": "File name", "value": "Q3.pdf"}]
    assert evidence["action_outcomes"] == {"verified": 1, "inconclusive": 0, "failed": 1}
    assert "hunter2" not in str(evidence)
    assert "the task is complete" not in str(evidence)


def _generic_run(goal: str) -> tuple[int, int]:
    init_db()
    with SessionLocal() as db:
        user = models.User(email=f"completion-{uuid.uuid4().hex[:8]}@example.com", name="Completion Check")
        db.add(user)
        db.flush()
        run = models.OperatorRun(
            user_id=user.id,
            application_id=None,
            goal=goal,
            target_url="https://example.test/apply",
            current_state="RUNNING",
            confidence_state={"browser_task": {"goal": goal, "target_url": "https://example.test/apply"}},
        )
        db.add(run)
        db.commit()
        return int(user.id), int(run.id)


def _run_generic_done(monkeypatch, verdicts: list[str]) -> tuple[dict, list[dict], int]:
    user_id, run_id = _generic_run("Save the report as Q3.pdf.")
    provider = _resolved_vision_provider(701, display_name="Brain", provider_type="openrouter", model_name="brain")
    decisions: list[int] = []
    checks: list[dict] = []
    verdict_iter = iter(verdicts)

    async def narrate(kind: str, data: dict) -> None:
        pass

    async def fake_save(profile: dict, *, force: bool = False, run_id: int | None = None) -> bool:
        return True

    def fake_decide(user_id: int, image: dict, view: dict, context: dict) -> dict:
        decisions.append(len(decisions))
        return dict(_DONE)

    def fake_verify(user_id: int, *, goal: str, claim: str, evidence: dict, delivered_answer: str = "") -> dict:
        checks.append({"goal": goal, "claim": claim, "evidence": evidence})
        verdict = next(verdict_iter)
        return {
            "verdict": verdict,
            "verified_summary": "The page shows the report open.",
            "missing": "" if verdict == "complete" else "no saved file is shown",
        }

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr("app.candidate_context.CandidateContextEngine", _DummyContextEngine)
    monkeypatch.setattr(extension_run, "_vision_profile_key_for_user", lambda user_id: ("openrouter", "brain"))
    monkeypatch.setattr(
        extension_run,
        "_load_vision_decision_profile",
        lambda provider_type, model_name: {
            "provider_type": provider_type,
            "model_name": model_name,
            "ewma_latency_seconds": None,
            "token_budget": None,
            "latency_ring_seconds": [],
        },
    )
    monkeypatch.setattr(extension_run, "_save_vision_decision_profile_async", fake_save)
    monkeypatch.setattr(extension_run, "_vision_provider_for_user", lambda user_id: provider)
    monkeypatch.setattr(extension_run, "_vision_escalation_provider_for_user", lambda user_id: None)
    monkeypatch.setattr(extension_run, "_db_event", lambda run_id, event_type, payload: events.append((event_type, payload)))
    monkeypatch.setattr(extension_run, "_record_context_usage_for_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(extension_run, "_decide_vision_action", fake_decide)
    monkeypatch.setattr(extension_run, "_verify_task_completion", fake_verify)

    summary = asyncio.run(
        run_vision_loop(_ChoiceBody(), url=None, narrate=narrate, run_id=run_id, user_id=user_id, skip_navigate=True, max_steps=6)
    )
    checked = [payload for event, payload in events if event == "TASK_COMPLETION_CHECKED"]
    assert all(check["claim"] == _DONE["reason"] and check["goal"] == "Save the report as Q3.pdf." for check in checks)
    return summary, checked, len(decisions)


def test_an_unbacked_done_is_sent_back_once_then_accepted_when_the_evidence_shows_it(monkeypatch) -> None:
    summary, checked, decisions = _run_generic_done(monkeypatch, ["incomplete", "complete"])

    assert decisions == 2
    assert [check["verdict"] for check in checked] == ["incomplete", "complete"]
    # A generic task whose done is graded complete on evidence ends COMPLETED (no human review step to protect).
    assert summary["state"] == "COMPLETED"
    assert summary["reason"] == "task_completed_verified"
    assert summary["outcome"] == "finished"
    assert summary["completion_check"]["verdict"] == "complete"


def test_a_done_the_evidence_never_backs_finishes_with_the_gap_recorded(monkeypatch) -> None:
    summary, checked, decisions = _run_generic_done(monkeypatch, ["incomplete", "incomplete", "incomplete"])

    assert decisions == 3  # sent back twice (as a final action is), never looped
    assert [check["send_back"] for check in checked] == [0, 1, 2]
    assert summary["state"] == "READY_FOR_REVIEW"
    assert summary["outcome"] == "partial"
    assert summary["completion_check"] == {
        "verdict": "incomplete",
        "verified_summary": "The page shows the report open.",
        "missing": "no saved file is shown",
    }


def _completion_message(monkeypatch, completion_check: dict | None, **summary_extra) -> str:
    init_db()
    with SessionLocal() as db:
        user = models.User(email=f"completion-msg-{uuid.uuid4().hex[:8]}@example.com", name="Completion Message")
        db.add(user)
        db.flush()
        run = models.OperatorRun(
            user_id=user.id,
            application_id=None,
            goal="Save the report as Q3.pdf.",
            target_url="https://example.test/report",
            current_state="RUNNING",
            confidence_state={"browser_task": {"goal": "Save the report as Q3.pdf.", "target_url": "https://example.test/report"}},
        )
        db.add(run)
        db.flush()
        thread = models.ChatThread(user_id=user.id, run_id=run.id, title="report", mode="run")
        db.add(thread)
        db.commit()
        run_id, thread_id = int(run.id), int(thread.id)

    async def fake_run_vision_loop(body, *, url, narrate, run_id, user_id, skip_navigate=False, max_steps=60, should_cancel=None):
        return {
            "state": "READY_FOR_REVIEW",
            "reason": "ready_for_review",
            "model_done_reason": "Saved the report as Q3.pdf",
            "actions": 2,
            "receipts": [{"type": "browser_action", "outcome": "verified_succeeded"}],
            **({"completion_check": completion_check} if completion_check else {}),
            **summary_extra,
        }

    messages: list[str] = []
    monkeypatch.setattr(extension_run, "run_vision_loop", fake_run_vision_loop)
    monkeypatch.setattr(extension_run, "_db_message", lambda thread_id, content, payload=None: messages.append(content))
    asyncio.run(
        drive_in_browser_application(
            body=_DriveBody(), run_id=run_id, thread_id=thread_id, url="https://example.test/report", skip_navigate=True
        )
    )
    assert len(messages) == 1
    return messages[0]


def test_the_reply_claims_only_what_the_evidence_backs(monkeypatch) -> None:
    confirmed = _completion_message(
        monkeypatch, {"verdict": "complete", "verified_summary": "The report is saved as Q3.pdf.", "missing": ""}
    )
    assert confirmed.startswith("Saved the report as Q3.pdf. Completed 1 browser action(s) with verified outcomes.")

    unbacked = _completion_message(
        monkeypatch,
        {"verdict": "incomplete", "verified_summary": "The report is open.", "missing": "no saved file is shown"},
    )
    assert unbacked.startswith(
        "The report is open. I couldn't confirm from the browser that the task is finished: no saved file is shown."
    )
    assert "Saved the report as Q3.pdf" not in unbacked

    unchecked = _completion_message(monkeypatch, {"verdict": "unavailable", "verified_summary": "", "missing": ""})
    # No verdict: the receipts lead and the model's statement is labeled as its own unverified claim.
    assert unchecked.startswith(
        "Completed 1 browser action(s) with verified outcomes. I couldn't independently check the result. "
        "The agent's own unverified claim: Saved the report as Q3.pdf."
    )
    no_verdict = _completion_message(monkeypatch, None)
    assert no_verdict.startswith("Completed 1 browser action(s) with verified outcomes.")
    assert "The agent's own unverified claim: Saved the report as Q3.pdf." in no_verdict


def test_the_unverified_count_is_exact_beyond_the_listed_values(monkeypatch) -> None:
    listed = [{"label": f"Field {i}", "reason": "no_evidence"} for i in range(20)]
    message = _completion_message(
        monkeypatch,
        {"verdict": "complete", "verified_summary": "ok", "missing": ""},
        unverified_values=listed,
        unverified_total=23,
    )
    assert "I entered 23 value(s) the evidence checks could not verify" in message
    assert "23 in all; only the first 20 are listed" in message


def test_unverified_values_survive_a_park_and_resume() -> None:
    init_db()
    with SessionLocal() as db:
        user = models.User(email=f"journal-{uuid.uuid4().hex[:8]}@example.com", name="Journal")
        db.add(user)
        db.flush()
        run = models.OperatorRun(
            user_id=user.id, application_id=None, goal="g", target_url="https://example.test", current_state="RUNNING",
            confidence_state={},
        )
        db.add(run)
        db.commit()
        run_id = int(run.id)
    values = [{"label": f"Field {i}", "reason": "no_evidence"} for i in range(50)]
    extension_run._persist_run_journal_for_run(
        run_id, {"notes": [], "plan": "", "unverified_values": values, "unverified_total": 63}
    )
    restored = extension_run._load_run_journal_for_run(run_id)
    assert restored["unverified_values"] == values
    assert restored["unverified_total"] == 63
    # A run that never recorded any restores empty.
    assert extension_run._load_run_journal_for_run(None)["unverified_total"] == 0


def test_the_completion_check_reads_the_whole_page_not_only_the_screen() -> None:
    # v4_keys run 1677 ended scrolled to its last two challenges; "all seven complete" was graded from those
    # two and challenge 1 (never done) passed unseen.
    import asyncio
    from types import SimpleNamespace

    shot = {"url": "https://bench.test/keys", "title": "Keys", "page_evidence": {"viewport_text": "6. Hold a key ✓ 7. Undo ✓"}}
    whole = "1. Move focus by keyboard Badge ID [ ] 2. Arrow grid ✓ ... 6. Hold a key ✓ 7. Undo ✓"
    evidence = extension_run._task_completion_evidence(shot, read_backs=[], receipts=[], whole_page_text=whole)
    assert evidence["whole_page_text"] == whole
    assert "whole_page_text" not in extension_run._task_completion_evidence(shot, read_backs=[], receipts=[])

    async def read(*, max_chars: int, **_kw) -> dict:
        return {"ok": True, "text": whole}

    browser = SimpleNamespace(read_run_tab_text=read)
    assert asyncio.run(extension_run._whole_page_text_for_completion(browser)) == whole
    switching = SimpleNamespace(body_for=lambda surface: browser if surface == "browser" else None)
    assert asyncio.run(extension_run._whole_page_text_for_completion(switching)) == whole

    async def broken(*, max_chars: int, **_kw) -> dict:
        raise RuntimeError("tab gone")

    assert asyncio.run(extension_run._whole_page_text_for_completion(SimpleNamespace(read_run_tab_text=broken))) == ""
    assert asyncio.run(extension_run._whole_page_text_for_completion(object())) == ""


def test_delivered_answer_rides_apart_from_evidence_and_the_flag_is_runtime_computed(monkeypatch) -> None:
    seen: list = []

    class FakeClient:
        def __init__(self, db, user_id=None):
            pass

        def verify_task_completion(self, payload):
            seen.append(payload)
            return {"verdict": "complete"}

    monkeypatch.setattr("app.lumen_model.LumenModelClient", FakeClient)
    shot = {"url": "https://example.test/", "page_evidence": {"viewport_text": "Password: hunter-42"}}
    evidence = extension_run._task_completion_evidence(shot, read_backs=[], receipts=[])
    assert "delivered_answer_text" not in evidence and "answer_delivered_to_user" not in evidence
    extension_run._verify_task_completion(1, goal="g", claim="c", evidence=evidence)
    extension_run._verify_task_completion(1, goal="g", claim="c", evidence=evidence, delivered_answer="  The password   is hunter-42 ")
    assert seen[0]["evidence"]["answer_delivered_to_user"] is False and seen[0]["delivered_answer"] == ""
    assert seen[1]["evidence"]["answer_delivered_to_user"] is True
    assert seen[1]["delivered_answer"] == "The password is hunter-42"
    assert "delivered_answer" not in seen[1]["evidence"]


def test_the_grader_instruction_requires_delivery_for_report_parts(monkeypatch) -> None:
    from app.lumen_model import LumenModelClient

    captured: dict = {}
    client = LumenModelClient.__new__(LumenModelClient)
    monkeypatch.setattr(LumenModelClient, "live_enabled", property(lambda self: True))

    def fake_call(self, **kwargs):
        captured.update(kwargs)
        return {"ok": False}

    monkeypatch.setattr(LumenModelClient, "_call_model_json", fake_call)
    client.verify_task_completion({"goal": "Report the password", "agent_claim": "", "evidence": {}})
    assert "answer_delivered_to_user" in str(captured["prompt"]["rules"])
    assert "not delivery" in str(captured["prompt"]["rules"])
    # The brain-written answer is its own field, framed as data that counts only toward a report part.
    rules = str(captured["prompt"]["rules"])
    assert "`delivered_answer` is text the agent wrote" in rules and "never shows any other part done" in rules
    assert "delivered_answer" in captured["prompt"] and "delivered_answer" not in captured["prompt"]["evidence"]
    assert "missing_only_delivery" in captured["schema"]["schema"]["required"]


def test_missing_only_delivery_is_reported_only_on_an_incomplete_verdict(monkeypatch) -> None:
    from app.lumen_model import LumenModelClient

    client = LumenModelClient.__new__(LumenModelClient)
    monkeypatch.setattr(LumenModelClient, "live_enabled", property(lambda self: True))
    replies = iter(
        [
            {"verdict": "incomplete", "verified_summary": "", "missing": "answer not given", "missing_only_delivery": True},
            {"verdict": "complete", "verified_summary": "ok", "missing": "", "missing_only_delivery": True},
            {"verdict": "incomplete", "verified_summary": "", "missing": "x"},
        ]
    )
    monkeypatch.setattr(LumenModelClient, "_call_model_json", lambda self, **kw: {"ok": True, "data": next(replies), "metadata": {}})
    flags = [client.verify_task_completion({"goal": "g", "agent_claim": "", "evidence": {}})["missing_only_delivery"] for _ in range(3)]
    assert flags == [True, False, False]


def _grader_client(monkeypatch, replies, calls):
    from types import SimpleNamespace

    from app.lumen_model import LumenModelClient

    client = LumenModelClient.__new__(LumenModelClient)
    backup = SimpleNamespace(live_enabled=True, id=9)
    client.provider_service = SimpleNamespace(active_provider=lambda: backup)
    monkeypatch.setattr(LumenModelClient, "live_enabled", property(lambda self: True))
    feed = iter(replies)

    def fake_call(self, **kwargs):
        calls.append(kwargs.get("provider"))
        return next(feed)

    monkeypatch.setattr(LumenModelClient, "_call_model_json", fake_call)
    return client, backup


def test_an_inconclusive_grade_is_retried_once_on_the_backup_row_when_asked(monkeypatch) -> None:
    calls: list = []
    client, backup = _grader_client(
        monkeypatch,
        [
            {"ok": True, "data": {"verdict": "maybe"}, "metadata": {"parse_status": "json"}},
            {"ok": True, "data": {"verdict": "complete", "verified_summary": "ok", "missing": "", "missing_only_delivery": False}, "metadata": {}},
        ],
        calls,
    )
    result = client.verify_task_completion({"goal": "g", "agent_claim": "", "evidence": {}, "retry_inconclusive": True})
    assert result["verdict"] == "complete" and result["retried"] is True
    assert calls == [None, backup]


def test_a_second_inconclusive_grade_stays_unavailable_and_records_the_raw_reply(monkeypatch) -> None:
    calls: list = []
    bad = {"ok": True, "data": {}, "metadata": {"parse_status": "non_json", "raw_output_excerpt": "Sure! The task looks done."}}
    client, _backup = _grader_client(monkeypatch, [bad, bad], calls)
    result = client.verify_task_completion({"goal": "g", "agent_claim": "", "evidence": {}, "retry_inconclusive": True})
    assert result["verdict"] == "unavailable" and len(calls) == 2
    assert "Sure! The task looks done." in result["raw_reply"]
    # Without the flag there is no retry.
    calls.clear()
    client2, _ = _grader_client(monkeypatch, [bad], calls)
    assert client2.verify_task_completion({"goal": "g", "agent_claim": "", "evidence": {}})["verdict"] == "unavailable"
    assert len(calls) == 1


def test_completion_check_fields_carry_the_raw_reply_only_when_present() -> None:
    plain = extension_run._completion_check_fields({"verdict": "complete", "verified_summary": "x", "missing": ""})
    assert "raw_reply" not in plain and "retried" not in plain
    bad = extension_run._completion_check_fields({"verdict": "unavailable", "raw_reply": "garbage" * 100, "retried": True})
    assert bad["retried"] is True and 0 < len(bad["raw_reply"]) <= 300


def test_raw_reply_is_scrubbed_like_stored_event_text_and_capped() -> None:
    raw = (
        "Typed hunter-42 for ann@example.com card 4111 1111 1111 1111 password: swordfish code: AB12CD "
        + "x" * 600
    )
    out = extension_run._completion_check_fields({"verdict": "unavailable", "raw_reply": raw}, ["hunter-42"])["raw_reply"]
    for leaked in ("hunter-42", "ann@example.com", "4111", "swordfish", "AB12CD"):
        assert leaked not in out
    assert len(out) <= 300
    exc = extension_run._scrub_event_text("RuntimeError: bad token: abc123secret for bob@x.io")
    assert "abc123secret" not in exc and "bob@x.io" not in exc


def test_the_inconclusive_sendback_does_not_claim_anything_is_missing() -> None:
    text = extension_run._inconclusive_final_sendback("Submit total")
    assert "could not run" in text and "Do NOT redo committed work" in text and "reply done" in text
    assert "finish" not in text.lower() and "undone" not in text.lower()


def _guard(**kw):
    base = {"selector": "#submit", "label": "Submit total", "url": "https://x.test/a", "lifecycle": "doc1",
            "form_id": "form1", "typed_forms_seen": 0}
    return {**base, **kw}


def test_guard_lapses_on_url_or_document_change_not_on_unrelated_typing() -> None:
    alive = extension_run._final_guard_alive
    assert alive(_guard(), "https://x.test/a", "doc1", [])
    assert not alive(_guard(), "https://x.test/b", "doc1", [])  # URL change
    assert not alive(_guard(), "https://x.test/a", "doc2", [])  # same URL, a new document
    assert alive(_guard(lifecycle=""), "https://x.test/a", "doc2", [])  # a token unknown at registration cannot lapse it
    # Typing into a search box elsewhere (another form, or no form) leaves it alive.
    assert alive(_guard(), "https://x.test/a", "doc1", ["form0", ""])


def test_guard_lapses_when_a_value_is_typed_into_its_own_form_even_if_the_value_repeats() -> None:
    alive = extension_run._final_guard_alive
    # The per-form list records every typing, so an identical re-typed value counts (the deduped text list hides it).
    assert not alive(_guard(typed_forms_seen=1), "https://x.test/a", "doc1", ["form1", "form1"])
    assert alive(_guard(typed_forms_seen=2), "https://x.test/a", "doc1", ["form1", "form1"])
    # A guard whose control has no ancestor form never lapses on typing; unknown typing never lapses either.
    assert alive(_guard(form_id=""), "https://x.test/a", "doc1", ["form1", ""])
    assert alive(_guard(), "https://x.test/a", "doc1", [""])


def test_two_forms_sharing_an_action_are_told_apart_by_form_id() -> None:
    # Both forms could post to "/submit"; only typing into form1 (the guarded one) lapses its guard.
    alive = extension_run._final_guard_alive
    assert alive(_guard(), "https://x.test/a", "doc1", ["form2"])
    assert not alive(_guard(), "https://x.test/a", "doc1", ["form2", "form1"])


def test_a_standing_authority_key_is_withheld_only_when_it_could_repeat_the_guarded_action() -> None:
    hit = extension_run._key_hits_final_guard
    guards = [_guard()]
    assert hit(guards, "#submit", "form1")
    assert hit(guards, "#amount", "form1")  # a sibling field in the guarded form
    assert not hit(guards, "#q", "form0")  # a field in another form
    assert hit(guards, "#agree", "")  # no ancestor form: unknown, fail closed
    assert hit(guards, "", "")  # focus unknown: fail closed
    assert hit([_guard(form_id="")], "#q", "form0")  # guarded control has no known form: fail closed
    assert not hit([], "#submit", "form1")


def test_a_native_dialog_accept_is_withheld_while_a_guard_is_live() -> None:
    hit = extension_run._key_hits_final_guard
    assert hit([_guard()], "", "", native_dialog=True)
    assert not hit([], "", "", native_dialog=True)


def test_scrub_masks_prefixed_keys_bearer_jwt_and_long_runs_but_keeps_prose() -> None:
    scrub = extension_run._scrub_event_text
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.abc123def456"
    samples = [
        "key sk-proj-abcdefghijklmnop1234", "token ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "slack xoxb-1234567890-abcdefghij", f"jwt {jwt}", "Authorization: Bearer abcDEF123456xyz",
        "hash 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08", "b64 QWxhZGRpbjpvcGVuIHNlc2FtZTEyMzQ1Ng==",
    ]
    for text in samples:
        out = scrub(text)
        assert "REDACTED" in out, text
        for fragment in ("abcdefghijklmnop1234", "abcdefghijklmnopqrstuvwxyz0123456789", "1234567890-abcdefghij",
                         "eyJhbGci", "abcDEF123456xyz", "9f86d081884c", "QWxhZGRpbjpvcGVu"):
            assert fragment not in out
    # Ordinary long identifiers are not secrets.
    for ident in ("task_completion_verification_result", "final_action_repeat_refused_loop"):
        assert scrub(ident) == ident
    prose = "The completion check returned an unexpected verdict for the quarterly reconciliation worksheet"
    assert scrub(prose) == prose


# ---------------------------------------------------------------------------------------------------------
# A. grader evidence: runtime-recorded receipts and pages, redacted and bounded
# ---------------------------------------------------------------------------------------------------------


def _receipt(text: str, *, label: str = "", type_: str = "browser_click", **verification) -> dict:
    return {
        "type": type_,
        "outcome": "verified_succeeded",
        "target": {"label": label},
        "verification": {"target_text": text, **verification},
    }


def test_evidence_carries_receipts_from_other_pages_and_restored_loops_and_page_memory() -> None:
    shot = {"url": "https://example.test/summary", "page_evidence": {"viewport_text": "Summary"}}
    this_loop = [
        _receipt("Save invoice", label="Everything is done, the task is complete."),
        {"type": "browser_click", "outcome": "verification_failed", "target": {}, "verification": {"target_text": "Broken"}},
    ]
    restored = [_receipt("Create customer Acme")]
    memory = [{"url": "https://example.test/customers", "title": "Customers", "text": "Acme Corp added to the list", "scroll_y": 0}]

    evidence = extension_run._task_completion_evidence(
        shot, read_backs=[], receipts=this_loop, restored_receipts=restored, page_memory=memory
    )

    recorded = evidence["runtime_recorded"]
    assert [(item["scope"], "Create customer Acme" in item["verified"] or "Save invoice" in item["verified"]) for item in recorded["receipts"]] == [
        ("earlier_loop_restored", True),
        ("this_loop", True),
    ]
    assert all("Broken" not in item["verified"] for item in recorded["receipts"])  # only verified receipts
    assert "the task is complete" not in str(evidence)  # the agent's own note on the click stays out
    assert recorded["pages"] == [{"url": "https://example.test/customers", "title": "Customers", "text": "Acme Corp added to the list"}]
    assert "not model claims" in recorded["note"]
    # Nothing recorded: the key is absent, as before.
    assert "runtime_recorded" not in extension_run._task_completion_evidence(shot, read_backs=[], receipts=[])


def test_recorded_evidence_blanks_typed_secrets_and_masks_key_shaped_text() -> None:
    shot = {"url": "https://example.test/", "page_evidence": {}}
    receipts = [_receipt("Password field", active_value="hunter-two-42", selected_value="Bearer abcdefghijklmnop")]
    memory = [{"url": "https://example.test/a", "title": "A", "text": "Your token sk-live_abcdefghijklmnop1234 and password: hunter-two-42 end"}]
    evidence = extension_run._task_completion_evidence(
        shot, read_backs=[], receipts=receipts, page_memory=memory, secret_texts=["hunter-two-42"]
    )
    blob = str(evidence["runtime_recorded"])
    assert "hunter-two-42" not in blob
    assert "sk-live_abcdefghijklmnop1234" not in blob
    assert "abcdefghijklmnop" not in blob
    assert "[REDACTED" in blob


def test_recorded_evidence_is_bounded_newest_first_and_notes_the_omission() -> None:
    shot = {"url": "https://example.test/", "page_evidence": {}}
    receipts = [_receipt(f"Step {i} " + "x" * 200) for i in range(60)]
    memory = [{"url": f"https://example.test/{i}", "title": str(i), "text": f"page {i} " + "y" * 900} for i in range(10)]
    recorded = extension_run._task_completion_evidence(
        shot, read_backs=[], receipts=receipts, page_memory=memory
    )["runtime_recorded"]
    assert sum(len(item["verified"]) for item in recorded["receipts"]) <= extension_run._COMPLETION_RECEIPTS_CHARS
    assert recorded["receipts"][-1]["n"] == 60 and recorded["receipts_omitted_older"] == 60 - len(recorded["receipts"]) > 0
    assert recorded["pages"][-1]["url"].endswith("/9")
    assert sum(len(item["text"]) for item in recorded["pages"]) <= extension_run._COMPLETION_PAGES_CHARS
    assert recorded["pages_omitted_older_or_trimmed"] > 0


def test_the_grader_prompt_names_runtime_recorded_evidence(monkeypatch) -> None:
    from app.lumen_model import LumenModelClient

    captured: dict = {}
    client = LumenModelClient.__new__(LumenModelClient)
    monkeypatch.setattr(LumenModelClient, "live_enabled", property(lambda self: True))
    monkeypatch.setattr(LumenModelClient, "_call_model_json", lambda self, **kw: captured.update(kw) or {"ok": False})
    client.verify_task_completion({"goal": "g", "agent_claim": "", "evidence": {"runtime_recorded": {"receipts": []}}})
    rules = str(captured["prompt"]["rules"])
    assert "runtime_recorded" in rules and "not the agent's claims" in rules and "earlier loop" in rules
    assert "plan_verdicts" not in captured["schema"]["schema"]["properties"]  # no plan, no per-line ask


# ---------------------------------------------------------------------------------------------------------
# B. per-line plan verdicts
# ---------------------------------------------------------------------------------------------------------

_PLAN = "[done] Create customer Acme\n[done] Send the invoice\n[now] Print the receipt\n[ ] Archive"


def test_plan_lines_are_numbered_with_the_agents_mark() -> None:
    lines = extension_run._plan_lines(_PLAN)
    assert [(item["n"], item["marked"]) for item in lines] == [(1, "done"), (2, "done"), (3, "open"), (4, "open")]
    evidence = extension_run._task_completion_evidence({"url": "u"}, read_backs=[], receipts=[], plan=_PLAN)
    assert [item["n"] for item in evidence["agent_plan_lines"]] == [1, 2, 3, 4]
    assert "agent_plan_lines" not in extension_run._task_completion_evidence({"url": "u"}, read_backs=[], receipts=[])


def test_a_claimed_done_line_the_grader_does_not_confirm_becomes_missing_even_on_a_complete_grade() -> None:
    lines = extension_run._plan_lines(_PLAN)
    check = {
        "verdict": "complete",
        "verified_summary": "ok",
        "missing": "",
        "plan_verdicts": [
            {"n": 1, "verdict": "confirmed", "reason": "customer listed"},
            {"n": 2, "verdict": "not_confirmed", "reason": "no invoice shown"},
            {"n": 3, "verdict": "not_applicable", "reason": "not asked for"},
            {"n": 4, "verdict": "not_confirmed", "reason": ""},
        ],
    }
    graded = extension_run._apply_plan_verdicts(check, lines)
    assert graded["verdict"] == "incomplete"
    assert graded["plan_gaps"][0].startswith("claimed done, not shown: [done] Send the invoice")
    assert graded["plan_gaps"][1].startswith("still open: [ ] Archive")
    assert "Send the invoice" in graded["missing"] and "Print the receipt" not in graded["missing"]
    # An already-incomplete grade keeps its own missing text and gains the gaps.
    incomplete = extension_run._apply_plan_verdicts({**check, "verdict": "incomplete", "missing": "no saved file"}, lines)
    assert incomplete["missing"].startswith("no saved file; claimed done, not shown")
    # Everything confirmed or not applicable: the grade is untouched.
    clean = {**check, "plan_verdicts": [{"n": n, "verdict": "confirmed", "reason": ""} for n in (1, 2, 3, 4)]}
    assert extension_run._apply_plan_verdicts(clean, lines) == clean


def test_without_usable_per_line_verdicts_the_grade_stands() -> None:
    lines = extension_run._plan_lines(_PLAN)
    check = {"verdict": "complete", "verified_summary": "ok", "missing": ""}
    assert extension_run._apply_plan_verdicts(check, lines) == check
    assert extension_run._apply_plan_verdicts({**check, "plan_verdicts": []}, lines)["verdict"] == "complete"
    assert extension_run._apply_plan_verdicts({**check, "plan_verdicts": "garbage"}, lines)["verdict"] == "complete"
    assert extension_run._apply_plan_verdicts({"plan_verdicts": [{"n": 2, "verdict": "not_confirmed"}], **check}, []) == {
        **check, "plan_verdicts": [{"n": 2, "verdict": "not_confirmed"}]
    }


def test_the_wrapper_sends_the_plan_apart_from_evidence_and_applies_the_verdicts(monkeypatch) -> None:
    seen: list = []

    class FakeClient:
        def __init__(self, db, user_id=None):
            pass

        def verify_task_completion(self, payload):
            seen.append(payload)
            return {
                "verdict": "complete",
                "verified_summary": "ok",
                "missing": "",
                "plan_verdicts": [{"n": 1, "verdict": "not_confirmed", "reason": "nothing shows it"}],
            }

    monkeypatch.setattr("app.lumen_model.LumenModelClient", FakeClient)
    evidence = extension_run._task_completion_evidence({"url": "u"}, read_backs=[], receipts=[], plan="[done] Send the invoice")
    check = extension_run._verify_task_completion(1, goal="g", claim="c", evidence=evidence)
    assert seen[0]["agent_plan"] == [{"n": 1, "marked": "done", "text": "[done] Send the invoice"}]
    assert "agent_plan_lines" not in seen[0]["evidence"]
    assert check["verdict"] == "incomplete" and "Send the invoice" in check["missing"]


def test_the_model_client_parses_plan_verdicts_and_falls_back_when_they_are_garbled(monkeypatch) -> None:
    from app.lumen_model import LumenModelClient

    replies: list[dict] = []
    captured: dict = {}
    client = LumenModelClient.__new__(LumenModelClient)
    monkeypatch.setattr(LumenModelClient, "live_enabled", property(lambda self: True))

    def fake_call(self, **kwargs):
        captured.update(kwargs)
        return {"ok": True, "data": replies[0], "metadata": {}}

    monkeypatch.setattr(LumenModelClient, "_call_model_json", fake_call)
    payload = {
        "goal": "g",
        "agent_claim": "",
        "evidence": {},
        "agent_plan": [{"n": 1, "marked": "done", "text": "[done] A"}, {"n": 2, "marked": "open", "text": "[ ] B"}],
    }
    base = {"verdict": "incomplete", "verified_summary": "s", "missing": "m", "missing_only_delivery": False}

    replies.append({**base, "plan_verdicts": [
        {"n": 1, "verdict": "Confirmed", "reason": "r"}, {"n": 2, "verdict": "bogus"}, {"n": 9, "verdict": "confirmed"}, "x"]})
    result = client.verify_task_completion(payload)
    assert result["plan_verdicts"] == [{"n": 1, "verdict": "confirmed", "reason": "r"}]
    assert "plan_verdicts" in captured["schema"]["schema"]["required"]
    assert captured["prompt"]["agent_plan"][0]["n"] == 1 and "plan_verdicts" in str(captured["prompt"]["rules"])

    replies[0] = {**base, "plan_verdicts": "not a list"}
    garbled = client.verify_task_completion(payload)
    assert garbled["verdict"] == "incomplete" and "plan_verdicts" not in garbled

    replies[0] = base  # field absent
    assert "plan_verdicts" not in client.verify_task_completion(payload)


# ---------------------------------------------------------------------------------------------------------
# C. consistent outcomes
# ---------------------------------------------------------------------------------------------------------


def test_outcome_mapping() -> None:
    derive = extension_run._derive_run_outcome
    check = lambda verdict: {"completion_check": {"verdict": verdict}}  # noqa: E731
    assert derive({"state": "COMPLETED", "reason": "final_action_submitted"}) == "finished"
    assert derive({"state": "READY_FOR_REVIEW", **check("complete")}) == "finished"
    assert derive({"state": "READY_FOR_REVIEW", **check("incomplete")}) == "partial"
    assert derive({"state": "READY_FOR_REVIEW", **check("unverified")}) == "unconfirmed"
    assert derive({"state": "READY_FOR_REVIEW", **check("unavailable")}) == "unconfirmed"
    assert derive({"state": "READY_FOR_REVIEW"}) == "unconfirmed"  # a give-up, or an application awaiting review
    assert derive({"state": "READY_FOR_REVIEW", "field_census": [{"status": "required_unfilled"}]}) == "partial"
    assert derive({"state": "BLOCKED"}) == "blocked"
    assert derive({"state": "WAITING_FOR_USER"}) == "blocked"
    assert derive({"state": "FAILED"}) == "blocked"
    assert derive({"state": "CANCELLED"}) == "unconfirmed"


def test_an_inconclusive_done_ends_ready_for_review_unconfirmed(monkeypatch) -> None:
    summary, checked, decisions = _run_generic_done(monkeypatch, ["unverified"])
    assert decisions == 1  # only an `incomplete` grade sends the done back
    assert summary["state"] == "READY_FOR_REVIEW"
    assert summary["outcome"] == "unconfirmed"


def test_the_reply_states_the_outcome(monkeypatch) -> None:
    partial = _completion_message(
        monkeypatch, {"verdict": "incomplete", "verified_summary": "The report is open.", "missing": "no saved file is shown"}
    )
    assert "no saved file is shown. Outcome: partially finished." in partial
    unconfirmed = _completion_message(monkeypatch, {"verdict": "unverified", "verified_summary": "Open.", "missing": "effect not shown"})
    assert "Outcome: unconfirmed." in unconfirmed
    finished = _completion_message(
        monkeypatch,
        {"verdict": "complete", "verified_summary": "ok", "missing": ""},
        state="COMPLETED",
        reason="task_completed_verified",
    )
    assert finished.startswith("Saved the report as Q3.pdf. Completed 1 browser action(s) with verified outcomes.")
    assert "ready for your review" not in finished and "Outcome:" not in finished


def test_an_inconclusive_grade_from_the_active_rows_own_model_is_not_asked_again(monkeypatch) -> None:
    # The first grade already ran on the active (Brain) row's model, so a retry would ask the same model the same
    # question; it is skipped, as the judge failover skips a same-model retry.
    for metadata in (
        {"parse_status": "json", "provider_id": 9},
        {"parse_status": "json", "provider": "OpenRouter", "model": "Brain/Model"},
    ):
        calls: list = []
        client, backup = _grader_client(monkeypatch, [{"ok": True, "data": {"verdict": "maybe"}, "metadata": metadata}], calls)
        backup.provider_type, backup.model_name = "openrouter", "brain/model"
        result = client.verify_task_completion({"goal": "g", "agent_claim": "", "evidence": {}, "retry_inconclusive": True})
        assert result["verdict"] == "unavailable" and "retried" not in result
        assert calls == [None]
    # A different model (here the Checker, named by its metadata) is still asked again on the active row.
    calls = []
    client, backup = _grader_client(
        monkeypatch,
        [
            {"ok": True, "data": {"verdict": "maybe"}, "metadata": {"provider_id": 4, "provider": "anthropic", "model": "haiku"}},
            {"ok": True, "data": {"verdict": "complete", "verified_summary": "ok", "missing": "", "missing_only_delivery": False}, "metadata": {}},
        ],
        calls,
    )
    backup.provider_type, backup.model_name = "openrouter", "brain/model"
    assert client.verify_task_completion({"goal": "g", "agent_claim": "", "evidence": {}, "retry_inconclusive": True})["retried"] is True
    assert calls == [None, backup]
