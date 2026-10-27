"""Compressed session context: deterministic bookkeeping + fail-open model compression.

The body appends compact step records and stores/injects the summary; the brain folds
records into the summary (judge provider preferred). A stale or missing summary must
never block a step or count as completion evidence."""

from app import models
from app.lumen_model import (
    LumenModelClient,
    SESSION_CONTEXT_COMPRESSION_MAX_TOKENS,
    SESSION_CONTEXT_COMPRESSION_TIMEOUT_SECONDS,
)
from app.db import SessionLocal, init_db
from app.extension_run import (
    _load_session_context_for_run,
    _persist_session_context_for_run,
    _vision_decision_context,
)
from app.model_provider import ResolvedModelProvider
from app.runtime import OperatorRuntimeEngine
from app.session_context import (
    PENDING_COMPRESS_THRESHOLD,
    PENDING_HARD_CAP,
    SUMMARY_MAX_CHARS,
    adopt_compression,
    append_step_record,
    coerce_state,
    host_of,
    new_state,
    prompt_view,
    should_compress,
    snapshot_for_compression,
)
from app.vision_agent import build_prompt


def _record_args(step: int) -> dict:
    return {
        "step": step,
        "kind": "click",
        "label": f"Question {step}",
        "ok": True,
        "verified": True,
        "result_hint": "clicked",
        "url_host": "jobs.example.com",
    }


def test_step_records_accumulate_and_cap_with_dropped_counter() -> None:
    state = new_state()
    for step in range(PENDING_HARD_CAP + 5):
        append_step_record(state, **_record_args(step))
    assert len(state["pending"]) == PENDING_HARD_CAP
    assert state["dropped_records"] == 5
    assert state["pending"][0]["step"] == 5  # oldest overflow dropped
    record = state["pending"][-1]
    assert record["kind"] == "click"
    assert record["host"] == "jobs.example.com"
    assert "note" not in record  # empty fields omitted


def test_compression_threshold_and_adoption_keep_in_flight_records() -> None:
    state = new_state()
    for step in range(PENDING_COMPRESS_THRESHOLD):
        append_step_record(state, **_record_args(step))
    assert should_compress(state) is True
    snapshot = snapshot_for_compression(state)
    # A record lands while the compression is in flight.
    append_step_record(state, **_record_args(99))
    assert adopt_compression(state, snapshot, "  Filled the identity section.  ") is True
    assert state["summary"] == "Filled the identity section."
    assert state["folded_records"] == PENDING_COMPRESS_THRESHOLD
    assert [record["step"] for record in state["pending"]] == [99]


def test_adoption_rejects_empty_summary_and_trims_long_ones() -> None:
    state = new_state()
    append_step_record(state, **_record_args(1))
    snapshot = snapshot_for_compression(state)
    assert adopt_compression(state, snapshot, "   ") is False
    assert state["pending"]  # unchanged
    assert adopt_compression(state, snapshot, "x" * (SUMMARY_MAX_CHARS + 500)) is True
    assert len(state["summary"]) == SUMMARY_MAX_CHARS


def test_coerce_state_survives_garbage_and_round_trips() -> None:
    assert coerce_state(None) == new_state()
    assert coerce_state({"summary": 42, "pending": "nope", "folded_records": "x"})["pending"] == []
    state = new_state()
    append_step_record(state, **_record_args(1))
    state["summary"] = "did things"
    state["folded_records"] = 3
    restored = coerce_state(state)
    assert restored["summary"] == "did things"
    assert restored["folded_records"] == 3
    assert restored["pending"] == state["pending"]


def test_prompt_view_is_empty_until_first_fold() -> None:
    state = new_state()
    append_step_record(state, **_record_args(1))
    assert prompt_view(state) == {}
    state["summary"] = "Completed page one."
    state["folded_records"] = 6
    view = prompt_view(state)
    assert view["summary"] == "Completed page one."
    assert view["folded_action_records"] == 6
    assert view["unfolded_recent_actions"] == 1


def test_prompt_view_basis_states_its_age_by_later_actions() -> None:
    # Aged by the actions recorded after the fold (stored with the state, so a resumed loop keeps it).
    state = new_state()
    for step in (3, 4, 5, 6, 7):
        append_step_record(state, **{**_record_args(step), "step": step})
    assert adopt_compression(state, snapshot_for_compression(state), "Opened and saved the form.")
    for step in (8, 9):
        append_step_record(state, **{**_record_args(step), "step": step})
    view = prompt_view(coerce_state(state), current_step=0)
    assert "2 action(s) happened after it" in view["basis"]
    assert "session_ledger, verified_receipts, last_reply" in view["basis"]
    assert view["unfolded_recent_actions"] == 2


def test_host_of_strips_www_and_tolerates_junk() -> None:
    assert host_of("https://www.jobs.example.com/a?b=1") == "jobs.example.com"
    assert host_of(None) == ""


def _provider(model_name: str) -> ResolvedModelProvider:
    return ResolvedModelProvider(
        id=7,
        display_name="judge",
        provider_type="openrouter",
        model_name=model_name,
        base_url="https://example.test/v1",
        api_key="k",
        temperature=0.0,
        max_tokens=8192,
        timeout_seconds=120,
        enabled=True,
        is_active=True,
        source="database",
    )


def test_compression_call_prefers_judge_provider_with_bounded_budget(monkeypatch) -> None:
    captured = {}

    def fake_call_model_json(provider, **kwargs):
        captured["provider"] = provider
        captured["purpose"] = kwargs.get("purpose")
        return {"ok": True, "data": {"summary": "  Folded summary. "}, "metadata": {}}

    monkeypatch.setattr("app.lumen_model.call_model_json", fake_call_model_json)
    client = LumenModelClient()
    monkeypatch.setattr(
        client.provider_service, "judge_provider", lambda **_: _provider("fast-judge")
    )
    result = client.compress_session_context(
        goal="apply", summary="old", records=[{"step": 1, "kind": "click"}]
    )
    assert result == "Folded summary."
    assert captured["purpose"] == "session_context_compression"
    assert captured["provider"].model_name == "fast-judge"
    assert captured["provider"].max_tokens == SESSION_CONTEXT_COMPRESSION_MAX_TOKENS
    assert captured["provider"].timeout_seconds == SESSION_CONTEXT_COMPRESSION_TIMEOUT_SECONDS


def test_compression_call_fails_open(monkeypatch) -> None:
    client = LumenModelClient()
    monkeypatch.setattr(client.provider_service, "judge_provider", lambda **_: None)
    monkeypatch.setattr(client.provider_service, "active_provider", lambda: _provider("main"))
    monkeypatch.setattr(
        "app.lumen_model.call_model_json",
        lambda provider, **kwargs: {"ok": False, "data": {}, "error": "down", "metadata": {}},
    )
    assert (
        client.compress_session_context(goal="g", summary="s", records=[{"step": 1}]) is None
    )
    # No records -> nothing to fold, no model call.
    assert client.compress_session_context(goal="g", summary="s", records=[]) is None


def test_session_context_persistence_round_trip() -> None:
    init_db()
    with SessionLocal() as db:
        user = OperatorRuntimeEngine(db).ensure_default_user()
        run = models.OperatorRun(
            user_id=user.id, goal="apply", target_url="https://jobs.example.com/1"
        )
        db.add(run)
        db.commit()
        run_id = run.id
    state = new_state()
    state["summary"] = "Completed the identity page."
    state["folded_records"] = 4
    append_step_record(state, **_record_args(9))
    _persist_session_context_for_run(run_id, state)
    restored = _load_session_context_for_run(run_id)
    assert restored["summary"] == "Completed the identity page."
    assert restored["folded_records"] == 4
    assert restored["pending"][0]["step"] == 9
    assert _load_session_context_for_run(None) == new_state()


def test_build_prompt_carries_session_context() -> None:
    prompt = build_prompt(
        {"width": 100, "height": 100},
        {"goal": "g", "session_context": {"summary": "Earlier pages complete."}},
    )
    assert prompt["session_context"] == {"summary": "Earlier pages complete."}
    assert "session_context" in prompt  # sits alongside session_ledger in the packet


def test_decision_context_trims_summary_to_budget() -> None:
    big = {"session_context": {"summary": "y" * 5000, "folded_action_records": 12}}
    normal = _vision_decision_context(dict(big))
    assert len(normal["session_context"]["summary"]) <= 1200
    assert normal["session_context"]["folded_action_records"] == 12
    reduced = _vision_decision_context(dict(big), payload_mode="reduced_after_timeout")
    assert len(reduced["session_context"]["summary"]) <= 500


def test_adoption_keeps_records_evicted_around_a_running_compression() -> None:
    # Deleting the first record_count records removed records the summary never saw whenever the
    # hard cap evicted older ones while the compression ran.
    from app import session_context

    state: dict = {}
    for step in range(session_context.PENDING_HARD_CAP):
        append_step_record(state, step=step, kind="click", label=f"Control {step}")
    snapshot = snapshot_for_compression(state)
    assert all("seq" not in record for record in snapshot["records"]), "the compressor never sees seq"
    for step in range(session_context.PENDING_HARD_CAP, session_context.PENDING_HARD_CAP + 3):
        append_step_record(state, step=step, kind="type", label=f"Field {step}")

    assert adopt_compression(state, snapshot, "Clicked through the first page.") is True
    assert [record["step"] for record in state["pending"]] == [
        session_context.PENDING_HARD_CAP,
        session_context.PENDING_HARD_CAP + 1,
        session_context.PENDING_HARD_CAP + 2,
    ]


def test_a_resumed_run_keeps_appending_after_the_restored_records() -> None:
    # coerce_state kept the records' seq but lost next_seq, so after a resume new records restarted
    # at 0 and a compression whose snapshot predated them deleted them on adoption.
    from app import session_context

    state = session_context.new_state()
    for step in range(6):
        append_step_record(state, step=step, kind="click", label=f"Control {step}")
    restored = session_context.coerce_state(state)
    snapshot = snapshot_for_compression(restored)
    append_step_record(restored, step=7, kind="type", label="Field after resume")

    assert restored["pending"][-1]["seq"] > snapshot["last_seq"]
    assert adopt_compression(restored, snapshot, "Clicked through the first page.") is True
    assert [record["label"] for record in restored["pending"]] == ["Field after resume"]

    # A state persisted before next_seq existed still restores past its highest record.
    legacy = {"summary": "", "pending": [{"step": 3, "kind": "click", "seq": 9}]}
    assert session_context.coerce_state(legacy)["next_seq"] == 10
