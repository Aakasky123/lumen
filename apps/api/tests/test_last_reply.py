"""last_reply: the brain's previous reply, item by item, as the first volatile packet key."""

from app import vision_agent
from app.extension_run import _last_reply_summary
from app.prompt_cache import PROMPT_CACHE_STABLE_KEYS, PROMPT_CACHE_VOLATILE_KEYS, order_prompt_for_cache


def _prepared(action: str, label: str = "") -> dict:
    return {"kind": action, "action": {"action": action, "reason": label}, "bound_label": label}


def _chain() -> tuple[list[dict], dict[int, dict], list[str]]:
    prepared = [
        _prepared("click", "Search"),
        _prepared("type", "SFO"),
        _prepared("click", "Save"),
        _prepared("click", "Close"),
    ]
    outcomes = {
        0: {"ok": True, "verified": True, "status": "executed"},
        1: {"ok": True, "verified": True, "status": "executed"},
    }
    notes = [
        "sequence_aborted at step 3: irreversible_final; remaining actions dropped",
        "NOT RUN: then[1] click 'Save'; then[2] click 'Close' - irreversible_final: Save commits the booking",
    ]
    return prepared, outcomes, notes


def test_last_reply_names_every_item_of_the_previous_reply() -> None:
    text = _last_reply_summary(*_chain())
    print(text)
    assert text.startswith("primary click 'Search' verified; then[0] type 'SFO' verified; NOT RUN [then[1] click 'Save', then[2] click 'Close'] (irreversible_final")
    assert "then[2] click 'Close'" in text and "irreversible_final" in text
    assert len(text) <= 600


def test_last_reply_reports_unverified_refused_and_unreported_items_and_is_capped() -> None:
    prepared = [_prepared("click", "A"), _prepared("click", "B"), _prepared("click", "C")]
    outcomes = {
        0: {"ok": True, "verified": False, "status": "executed", "timing_outcome": "click_unverified"},
        1: {"ok": False, "verified": False, "status": "continue", "timing_outcome": "grounding_refused"},
    }
    text = _last_reply_summary(prepared, outcomes, [])
    assert "primary click 'A' ran unverified (click_unverified)" in text
    assert "then[0] click 'B' refused (grounding_refused)" in text
    assert "then[1] click 'C' not run" in text
    long_notes = ["NOT RUN: " + "x" * 400 + " - " + "y" * 400] * 5
    assert len(_last_reply_summary(prepared, outcomes, long_notes)) <= 800
    assert _last_reply_summary([], {}, []) == ""


def test_last_reply_is_the_first_volatile_key_and_the_stable_prefix_is_unchanged() -> None:
    assert PROMPT_CACHE_VOLATILE_KEYS[0] == "last_reply"
    view = {"width": 800, "height": 600}
    base = {"goal": "g", "recent_actions": ["a"]}
    without = order_prompt_for_cache(vision_agent.build_prompt(view, dict(base)))
    with_reply = order_prompt_for_cache(
        vision_agent.build_prompt(view, {**base, "last_reply": "primary click 'X' verified"})
    )
    keys = list(with_reply)
    stable = [k for k in keys if k in PROMPT_CACHE_STABLE_KEYS]
    assert keys[: len(stable)] == stable
    volatile = [k for k in keys if k not in PROMPT_CACHE_STABLE_KEYS]
    assert [k for k in volatile if k in PROMPT_CACHE_VOLATILE_KEYS][0] == "last_reply"
    assert {k: v for k, v in with_reply.items() if k in PROMPT_CACHE_STABLE_KEYS} == {
        k: v for k, v in without.items() if k in PROMPT_CACHE_STABLE_KEYS
    }
    assert "last_reply" not in without


def test_last_reply_run_1810_verified_final_action_is_not_called_refused() -> None:
    # v6_insights run 1810: the Submit ran under Full autonomy and its outcome was verified; the chained scroll
    # after it was dropped for a fresh screenshot.
    prepared = [_prepared("click", "Submit"), _prepared("scroll", "")]
    outcomes = {
        0: {
            "ok": None,
            "verified": None,
            "status": "continue",
            "timing_outcome": "final_action_verified_continue",
            "executed_action": True,
        },
    }
    notes = [
        "sequence_aborted at step 1: final_action_verified_continue; remaining actions dropped",
        "NOT RUN: then[0] scroll - the preceding action ran and its outcome was verified",
    ]
    text = _last_reply_summary(prepared, outcomes, notes)
    assert "refused" not in text
    assert text.startswith(
        "primary click 'Submit' ran, outcome verified (final_action_verified_continue); NOT RUN [then[0] scroll]"
    )


def test_last_reply_outcome_states_cover_every_path() -> None:
    from app.extension_run import _outcome_state

    assert _outcome_state({"ok": True, "verified": True}) == "verified"
    assert _outcome_state({"timing_outcome": "final_submit_verified", "executed_action": True}).startswith(
        "ran, outcome verified"
    )
    assert _outcome_state({"timing_outcome": "final_submit_approval_required", "executed_action": False}).startswith(
        "awaiting confirmation"
    )
    assert _outcome_state({"timing_outcome": "final_action_reapproval_required", "executed_action": False}).startswith(
        "refused"
    )
    assert _outcome_state({"ok": True, "status": "executed", "timing_outcome": "executed"}).startswith("ran unverified")
    assert (
        _outcome_state({"status": "continue", "timing_outcome": "something_new", "executed_action": True})
        == "ran, effect unverified (something_new)"
    )
    assert (
        _outcome_state({"status": "continue", "timing_outcome": "something_new", "executed_action": False})
        == "not executed (something_new)"
    )
    assert _outcome_state({"status": "continue", "timing_outcome": "something_new"}) == "no confirmed effect (something_new)"


def test_last_reply_run_1813_refused_point_click_chain_still_reports() -> None:
    # v6_shop run 1813: four point-only checkbox clicks; the primary was refused at validation, so nothing was
    # prepared or executed and the packet carried no last_reply at all.
    reply = [{"action": "click", "x": 36, "y": 174}] + [{"action": "click", "x": 36, "y": y} for y in (398, 192, 211)]
    notes = [
        "REFUSED, NOT EXECUTED (click at primary): sequence_invalid at primary: grounding_citation_choice_click_ungrounded "
        "invalid_model_action:grounding_citation_choice_click_ungrounded. A value-setting click action cannot execute"
    ]
    text = _last_reply_summary([], {}, notes, reply)
    assert text.startswith("REFUSED, NOT EXECUTED (click at primary): sequence_invalid at primary")
    assert "then[0] click 'at 36,398' not run" in text and "then[2] click 'at 36,211' not run" in text
    assert "primary click 'at 36,174' not run" not in text
    assert len(text) <= 600


def test_last_reply_says_nothing_took_effect_when_no_note_or_outcome_exists() -> None:
    reply = [{"action": "click", "x": 1, "y": 2}]
    assert _last_reply_summary([], {}, [], reply) == "primary click 'at 1,2' not run (no outcome recorded)"


def test_reply_item_named_by_target_never_by_reason_text() -> None:
    from app.extension_run import _reply_item_target

    action = {"action": "click", "control": 7, "reason": "Open the attendees dropdown to add Ana"}
    assert _reply_item_target({}, action, 40) == "control 7"
    assert _reply_item_target({"bound_label": "Add attendee"}, action, 40) == "Add attendee"
    assert _reply_item_target({}, {"action": "click", "target_text": "Save", "reason": "x"}, 40) == "Save"
    assert _last_reply_summary([_prepared("click", "A")], {}, ["NOT RUN: primary click 'A' - gate"][:0]) != ""
    assert _last_reply_summary([], {}, []) == ""


def test_last_reply_not_run_line_about_an_item_that_ran_is_not_attributed() -> None:
    prepared = [_prepared("click", "A"), _prepared("click", "B")]
    outcomes = {0: {"ok": True, "verified": True, "status": "executed"}}
    notes = ["NOT RUN: primary click 'A' - stale line from elsewhere", "NOT RUN: then[0] click 'B' - gate"]
    text = _last_reply_summary(prepared, outcomes, notes)
    assert "stale line" not in text and "NOT RUN [then[0] click 'B'] (gate)" in text


def test_a_body_failure_reads_failed_not_ran() -> None:
    # WebGames popup-chaos run 1927: then[0]'s control_not_found read "ran unverified (executed)".
    from app.extension_run import _outcome_state

    assert _outcome_state({"ok": False, "status": "executed", "failure_reason": "control_not_found"}) == (
        "failed (control_not_found)"
    )
    assert _outcome_state({"ok": True, "verified": True, "failure_reason": "x"}) == "verified"


def test_a_failure_that_may_have_taken_effect_never_reads_failed() -> None:
    from app.extension_run import _outcome_state

    for outcome in (
        {"ok": False, "status": "executed", "failure_reason": "scroll_execution_outcome_unknown"},
        {"ok": False, "executed_action": True, "failure_reason": "final_submit_page_context_drift"},
        {"ok": False, "status": "executed", "failure_reason": "drag_x", "may_have_run": True},
    ):
        assert not _outcome_state(outcome).startswith("failed"), outcome


def test_a_partially_typed_failure_is_flagged_may_have_run_in_the_trace() -> None:
    # the trace flag is computed from the body result; keys_sent > 0 means text may already be in the field
    from app.extension_run import _outcome_state

    assert not _outcome_state(
        {"ok": False, "status": "executed", "failure_reason": "keystroke_dispatch_failed", "may_have_run": True}
    ).startswith("failed")


def test_a_withheld_primary_shows_its_reason() -> None:
    prepared = [{"kind": "type", "action": {"action": "type", "text": "Monday events"}}]
    drop = "withheld before running: secret_check_unavailable (the secret-risk check did not answer; retry)"
    text = _last_reply_summary(prepared, {}, [], None, drop)
    assert text.startswith("primary type NOT RUN (withheld before running: secret_check_unavailable")


def test_a_primary_with_no_outcome_and_no_drop_never_reads_withheld() -> None:
    prepared = [{"kind": "type", "action": {"action": "type", "text": "Monday events"}}]
    notes = [
        "secret_check_unavailable: the secret-risk check could not run, so that action was withheld.",
        "click_unverified: the click had no visible effect",
    ]
    text = _last_reply_summary(prepared, {}, notes, None, "")
    assert "not run (no outcome recorded)" in text
    assert "NOT RUN (withheld" not in text
