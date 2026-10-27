"""last_reply: a primary that never ran always leads with its state and reason."""

from app.extension_run import _last_reply_summary


def _prepared(action: str, label: str = "") -> dict:
    return {"kind": action, "action": {"action": action, "reason": label}, "bound_label": label}


def test_last_reply_rejected_primary_leads_with_its_reason_run_1818_shape() -> None:
    # run 1818 step 21: type into control 28 (not in the inventory) + then[0] click control 29. The loop
    # recorded "sequence_invalid at primary: control_index_invalid ..." in recent_actions (which names
    # "primary" but is not a NOT RUN line); nothing prepared, nothing executed.
    reply = [
        {"action": "type", "control": 28, "text": "KESTREL20"},
        {"action": "click", "control": 29},
    ]
    notes = ["sequence_invalid at primary: control_index_invalid. control=28 is not resolvable in the current control_inventory."]
    drop = "refused before execution: control_index_invalid - " + notes[0]
    text = _last_reply_summary([], {}, notes, reply, drop)
    assert text.startswith("primary type 'control 28' NOT RUN (refused before execution: control_index_invalid")
    assert "then[0] click 'control 29' not run (primary was not executed)" in text
    assert "step ended first" not in text


def test_last_reply_withheld_primary_and_missing_reason_still_get_a_line() -> None:
    prepared = [_prepared("click", "Apply"), _prepared("click", "Next")]
    stalled = _last_reply_summary(
        prepared, {}, ["no_progress: repeated click"], None, "withheld, repeated action with no progress"
    )
    assert stalled.startswith("primary click 'Apply' NOT RUN (withheld, repeated action with no progress)")
    assert "then[0] click 'Next' not run (primary was not executed)" in stalled
    bare = _last_reply_summary(prepared, {}, [])
    assert "primary click 'Apply' not run (no outcome recorded)" in bare
    assert "then[0] click 'Next' not run (primary was not executed)" in bare
    # a primary that ran keeps the old wording for the follow-ups
    ran = _last_reply_summary(prepared, {0: {"ok": True, "verified": True, "status": "executed"}}, [])
    assert "then[0] click 'Next' not run (step ended first)" in ran
