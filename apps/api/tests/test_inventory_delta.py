"""Inventory delta: rows unchanged since the previous step are compacted (never omitted: every call is stateless)."""

from __future__ import annotations

import json

from app import extension_run, vision_agent

VIEW = {"width": 1000, "height": 800, "url": "http://x/form", "title": "x", "scroll_y": 0, "scroll_height": 800}


def _page(n: int = 30, *, changed: dict[int, str] | None = None, focused: int | None = None) -> dict:
    controls = []
    for i in range(1, n + 1):
        controls.append(
            {
                "index": i,
                "tag": "input",
                "type": "text",
                "text": f"Field {i}",
                "value": (changed or {}).get(i, ""),
                "x": 40,
                "y": 20 * i,
                "width": 300,
                "height": 18,
                "focused": i == focused,
                "dom_context": [
                    {"relation": "previous_sibling", "text": f"Section heading {i} with some explanatory words", "depth": 1},
                    {"relation": "parent", "text": f"Group {i}: help text that the page shows beside the field", "depth": 2},
                ],
                "ancestor_chain": [
                    {"tag": "div", "id": f"group-{i}", "role": "group", "depth": 1},
                    {"tag": "form", "id": "main-form", "depth": 3},
                ],
            }
        )
    return {"controls": controls}


def _request(identity: str = "http://x/form|tok", step: int = 2, **overrides) -> dict:
    return {
        "identity": identity,
        "step": step,
        "previous": {},
        "full_reason": "",
        "keep_indices": [],
        "keep_fingerprints": [],
        "now_line": "",
        **overrides,
    }


def _prepare(page: dict, request: dict | None, **context) -> tuple[dict, dict]:
    ctx = {"goal": "g", "execution_surface": "browser", "page_evidence": page, **context}
    if request is not None:
        ctx["_inventory_delta"] = request
    prepared = vision_agent.prepare_model_input(dict(VIEW), ctx)
    return prepared["prompt"], ctx


def _step(state: dict, page: dict, step: int, *, identity: str = "http://x/form|tok", target: int | None = None, **kw):
    """Drive the loop-side bookkeeping the way run_vision_loop does, for one step."""
    request = extension_run._inventory_delta_request(
        state,
        identity=identity,
        step=step,
        payload_mode=kw.pop("payload_mode", "normal"),
        last_outcome=kw.pop("last_outcome", None),
        plan=kw.pop("plan", ""),
    )
    prompt, ctx = _prepare(page, request, **kw)
    extension_run._inventory_delta_remember(state, request)
    action = {"action": "click", "control": target} if target else {"action": "wait"}
    extension_run._inventory_delta_note_decision(state, action, step)
    return prompt, request["result"]


def _rows(prompt: dict) -> dict[int, dict]:
    return {row["index"]: row for row in prompt["control_inventory"]}


def test_first_step_is_full_and_disabled_packets_carry_no_mode_key() -> None:
    prompt, _ = _prepare(_page(), None)
    assert "controls_mode" not in prompt and all("dom_context" in r for r in prompt["control_inventory"])
    state: dict = {}
    prompt, result = _step(state, _page(), 1)
    assert prompt["controls_mode"] == "full" and result["reason"] == "no_previous"
    assert all("dom_context" in r and "ancestor_chain" in r for r in prompt["control_inventory"])


def test_unchanged_page_compacts_unchanged_rows_keeps_ids_and_marks_the_mode() -> None:
    state: dict = {}
    full, _ = _step(state, _page(), 1)
    delta, result = _step(state, _page(changed={7: "hello"}), 2)
    assert result["mode"] == "delta"
    assert delta["controls_mode"].startswith("delta")
    rows = _rows(delta)
    assert sorted(rows) == sorted(_rows(full)), "ids are stable and every row is still listed"
    assert "dom_context" in rows[7] and "ancestor_chain" in rows[7], "a changed row stays whole"
    assert rows[7]["value"] == "hello"
    compact = rows[3]
    assert "dom_context" not in compact and "ancestor_chain" not in compact
    assert compact["text"] == "Field 3" and compact["box"] == _rows(full)[3]["box"] and compact["kind"]
    assert 7 not in delta["controls_unchanged"] and 3 in delta["controls_unchanged"]
    assert len(json.dumps(delta)) < len(json.dumps(full))


def test_focused_targeted_and_plan_named_rows_stay_whole() -> None:
    state: dict = {}
    _step(state, _page(), 1, target=5)
    delta, result = _step(
        state, _page(changed={7: "x"}, focused=9), 2, plan="[done] a\n[now] fill Field 12 next\n[next] b"
    )
    assert result["mode"] == "delta"
    rows = _rows(delta)
    for kept in (5, 7, 9, 12):
        assert "dom_context" in rows[kept], kept
    assert "dom_context" not in rows[20]
    # The target is remembered for two steps, then released.
    _step_state = state
    later, _ = _step(_step_state, _page(changed={7: "x"}), 3)
    assert "dom_context" in _rows(later)[5]
    last, _ = _step(_step_state, _page(changed={7: "x"}), 4)
    assert "dom_context" not in _rows(last)[5], "released after the two steps that followed the target"


def test_navigation_sends_the_full_inventory() -> None:
    state: dict = {}
    _step(state, _page(), 1)
    prompt, result = _step(state, _page(), 2, identity="http://x/form|other-document")
    assert result["mode"] == "full" and result["reason"] == "page_changed"
    assert all("dom_context" in r for r in prompt["control_inventory"])


def test_resync_every_k_steps() -> None:
    state: dict = {}
    modes = [_step(state, _page(), step)[1]["mode"] for step in range(1, 12)]
    k = vision_agent.INVENTORY_DELTA_RESYNC_STEPS
    assert modes[0] == "full" and modes[k] == "full" and modes[2 * k] == "full"
    assert all(mode == "delta" for i, mode in enumerate(modes) if i % k)


def test_failed_action_reduced_payload_and_unsent_previous_resync() -> None:
    state: dict = {}
    _step(state, _page(), 1)
    _, result = _step(state, _page(), 2, last_outcome={"outcome": "action_refused", "code": "click_covered"})
    assert result["reason"] == "after_failed_action"
    _, result = _step(state, _page(), 3, payload_mode="reduced_after_timeout")
    assert result["reason"] == "reduced_payload"
    # The model call of step 4 never returned: step 5 cannot rely on it.
    request = extension_run._inventory_delta_request(
        state, identity="http://x/form|tok", step=4, payload_mode="normal", last_outcome=None, plan=""
    )
    _prepare(_page(), request)
    extension_run._inventory_delta_remember(state, request)
    extension_run._inventory_delta_note_decision(state, {"failure_reason": "provider_timeout"}, 4)
    _, result = _step(state, _page(), 5)
    assert result["reason"] == "previous_not_sent"


def test_a_cut_off_retry_and_a_mostly_changed_screen_are_full() -> None:
    state: dict = {}
    _step(state, _page(), 1)
    request = extension_run._inventory_delta_request(
        state, identity="http://x/form|tok", step=2, payload_mode="normal", last_outcome=None, plan=""
    )
    prompt, _ = _prepare(_page(), request, reply_cut_off={"max_tokens": 1})
    assert prompt["controls_mode"] == "full"
    scrolled = _page()
    for control in scrolled["controls"]:
        control["y"] += 400
    _, result = _step(state, scrolled, 2)
    assert result["mode"] == "full" and result["reason"] == "screen_changed"


def test_identity_helper_uses_url_and_lifecycle_token_or_the_front_window() -> None:
    shot = {"url": "http://x/a", "document_lifecycle_token": "t1", "page_evidence": {}}
    assert extension_run._inventory_delta_identity(shot, "browser") == "http://x/a|t1"
    desk = {"page_evidence": {"windows": [{"window_id": "cgw:1"}, {"window_id": "cgw:2", "active": True}]}}
    assert extension_run._inventory_delta_identity(desk, "desktop") == "desktop|cgw:2"
    assert extension_run._inventory_delta_identity({"page_evidence": {}}, "desktop") == ""


def test_measured_saving_on_a_sample_inventory() -> None:
    state: dict = {}
    full, _ = _step(state, _page(40), 1)
    delta, _ = _step(state, _page(40, changed={4: "a", 5: "b"}, focused=4), 2, target=4)
    before = len(json.dumps(full["control_inventory"]))
    after = len(json.dumps(delta["control_inventory"])) + len(json.dumps(delta["controls_mode"])) + len(
        json.dumps(delta["controls_unchanged"])
    )
    print(f"inventory chars full={before} delta={after} saved={before - after} ({100 * (before - after) / before:.0f}%)")
    assert after < 0.6 * before
