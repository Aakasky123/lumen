"""Facts about a covered page: what the refused point hit, and when one element covers most controls."""

from app import vision_agent
from app.extension_run import _vision_result_hint
from app.prompt_cache import order_prompt_for_cache
from app.receipt_view import receipt_line


def _control(i: int, **extra) -> dict:
    return {"tag": "button", "text": f"b{i}", "x": 10, "y": 10 + i * 30, "width": 50, "height": 20,
            "occluded": True, "covered": True, "covered_by": "div.overlay", "covered_by_id": "veil",
            "covered_by_text": "Please wait", **extra}


def test_page_covered_when_most_controls_share_one_cover() -> None:
    evidence = {"controls": [_control(i) for i in range(4)]}
    assert vision_agent.page_covered_fact(evidence) == (
        "The page is covered by div#veil 'Please wait'; controls under it cannot be clicked."
    )


def test_page_covered_needs_three_rows_and_eighty_percent_one_cover() -> None:
    assert vision_agent.page_covered_fact({"controls": [_control(0), _control(1)]}) == ""
    mixed = [_control(0), _control(1), _control(2), _control(3, covered_by_id="other"), _control(4, covered_by_id="x")]
    assert vision_agent.page_covered_fact({"controls": mixed}) == ""
    free = [_control(0), _control(1), _control(2), {"tag": "a", "text": "free", "x": 1, "y": 1, "width": 5, "height": 5}]
    assert vision_agent.page_covered_fact({"controls": free}) == ""


def test_page_covered_is_a_volatile_key_after_last_reply() -> None:
    context = {"goal": "g", "last_reply": "r", "page_evidence": {"controls": [_control(i) for i in range(3)]}}
    prompt = order_prompt_for_cache(vision_agent.build_prompt({"width": 800, "height": 600}, context))
    keys = list(prompt)
    assert keys.index("page_covered") == keys.index("last_reply") + 1
    plain = vision_agent.build_prompt({"width": 800, "height": 600}, {"goal": "g"})
    assert "page_covered" not in plain


def _refusal(**extra) -> dict:
    return {"ok": False, "action": "click", "failure_reason": "control_not_at_resolved_point",
            "hit_tag": "div", "hit_id": "veil", "hit_text": "Please wait", "hit_covers_viewport": True, **extra}


def test_refusal_names_what_the_point_hit() -> None:
    hint = _vision_result_hint(_refusal())
    assert "covered by div#veil 'Please wait' (covers the whole viewport)" in hint
    other = _vision_result_hint(_refusal(hit_id="", hit_class="btn", hit_text="", hit_covers_viewport=False))
    assert "covered by div.btn (a different control)" in other
    line = receipt_line(
        {"type": "browser_click", "verification": _refusal(), "target": {}}, detailed=False
    )
    assert "the point is covered by div#veil 'Please wait' (covers the whole viewport)" in line


def test_a_click_that_ran_never_reads_as_covered() -> None:
    # WebGames buttons-easy run 1883: a successful click resolved to a child of the dashed target; its own
    # hit_tag (the element reached) must not be worded as a refusal.
    ran = {"ok": True, "action": "click", "hit_tag": "div", "target_tag": "div", "verification_state": "observed_effect"}
    line = receipt_line({"type": "browser_click", "verification": ran, "target": {}}, detailed=True)
    assert "covered" not in line and "different control" not in line
    assert "hit_tag=div" in line
    assert "covered" not in _vision_result_hint(ran)
