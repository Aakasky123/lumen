"""The outcome verifier's packet: only what judging one action's effect needs, never the brain's context."""

import json

from app import extension_run as er
from app.lumen_model import LumenModelClient

_BRAIN_ONLY_KEYS = {
    "rules",
    "control_inventory",
    "page_memory",
    "notes",
    "plan",
    "profile",
    "user_profile",
    "candidate_context",
    "receipts_ledger",
    "history",
}
_ROW_BULK_KEYS = {
    "dom_context",
    "ancestor_chain",
    "accessibility_context",
    "nearby_text",
    "context_text",
    "visual_state",
    "class_name",
    "aria_controls",
    "aria_owns",
    "aria_labelledby",
    "aria_describedby",
    "autocomplete",
    "id",
    "x",
    "y",
    "width",
    "height",
}


def _heavy_row(index, **extra):
    row = {
        "index": index,
        "tag": "button",
        "role": "",
        "type": "",
        "id": f"btn-{index}",
        "name": "",
        "class_name": "btn btn-primary very-long-class-list " * 4,
        "aria_controls": "",
        "aria_describedby": "",
        "text": f"Control {index}",
        "value": "",
        "nearby_text": "Lots of neighbouring words " * 20,
        "context_text": "Section heading | breadcrumb | sidebar " * 10,
        "dom_context": [{"relation": "previous_sibling", "text": "sibling text " * 20, "depth": d} for d in range(8)],
        "ancestor_chain": [{"depth": d, "tag": "div", "id": f"wrap-{d}", "role": "group"} for d in range(5)],
        "accessibility_context": [{"relation": "label", "text": "accessible text " * 10}],
        "visual_state": {"background_color": "rgb(1, 2, 3)", "color": "rgb(0, 0, 0)", "font_weight": "400"},
        "checked": False,
        "selected": False,
        "disabled": False,
        "painted": None,
        "x": 10,
        "y": 20 * index,
        "width": 120,
        "height": 32,
    }
    row.update(extra)
    return row


def _page(text, rows=20):
    controls = [_heavy_row(i) for i in range(rows)]
    controls[1] = _heavy_row(1, tag="input", type="checkbox", text="", label="I agree", checked=False)
    controls[2] = _heavy_row(2, tag="input", type="email", text="", value="a@b.example", placeholder="Email")
    controls[3] = _heavy_row(3, text="Pay now", disabled=True)
    return {
        "viewport_text": text,
        "url": "https://shop.example/checkout",
        "title": "Checkout",
        "scroll_y": 0,
        "controls": controls,
        "off_screen_controls": [{"text": "far away"}],
        "emphasized_text": [{"text": "bold", "style": "b"}],
    }


def _shot(text):
    return {"ok": True, "url": "https://shop.example/checkout", "title": "Checkout", "image": "QUJD", "page_evidence": _page(text)}


def _all_keys(value, found=None):
    found = set() if found is None else found
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(key)
            _all_keys(item, found)
    elif isinstance(value, list):
        for item in value:
            _all_keys(item, found)
    return found


def _capture(monkeypatch):
    captured = []

    def fake_verify(self, payload, *, image=None):
        captured.append({"payload": payload, "image": image})
        return {"status": "uncertain", "confidence": 0.0, "evidence": "", "metadata": {}}

    monkeypatch.setattr(LumenModelClient, "verify_final_action_outcome", fake_verify)
    return captured


def _assert_slim_rows(page):
    rows = page["controls"]
    assert len(rows) == 20
    for row in rows:
        assert not (_ROW_BULK_KEYS & set(row)), sorted(_ROW_BULK_KEYS & set(row))
        assert row["index"] is not None and row["tag"]
    by_index = {row["index"]: row for row in rows}
    # A choice control keeps its explicit unchecked state; a disabled control says so; empty fields are gone.
    assert by_index[1]["checked"] is False and by_index[1]["label"] == "I agree"
    assert by_index[2]["value"] == "a@b.example" and by_index[2]["placeholder"] == "Email"
    assert by_index[3]["disabled"] is True
    assert "disabled" not in by_index[0] and "checked" not in by_index[0] and "value" not in by_index[0]


def test_final_action_outcome_packet_carries_the_action_evidence_and_no_brain_context(monkeypatch):
    captured = _capture(monkeypatch)
    pending = {
        "label": "Place order",
        "decision": "standing_user_authority",
        "target_identity": {"target_selector": "#place-order", "target_tag": "button"},
        "risk_evidence": "places the order and charges the card",
        "page_memory": ["should never travel"],
        "plan": "should never travel",
    }
    er._final_action_outcome_verdict(
        1,
        goal="Buy the blue kettle",
        pending=pending,
        before_shot=_shot("Review your order. Place order"),
        result={"ok": True, "url_changed": True, "title_changed": False, "surface_changed": True},
        post_shot=_shot("Thank you! Order #123 placed."),
        verification_mode="post_action_comparison",
    )
    payload = captured[0]["payload"]
    assert list(payload) == ["verification_mode", "goal", "final_action", "execution", "before", "after"]
    assert payload["goal"] == "Buy the blue kettle"
    assert payload["final_action"]["selector"] == "#place-order"
    assert payload["final_action"]["what_it_does"] == "places the order and charges the card"
    assert payload["execution"]["url_changed"] is True
    assert payload["after"]["page"]["viewport_text"] == "Thank you! Order #123 placed."
    assert payload["before"]["page"]["viewport_text"] == "Review your order. Place order"
    assert captured[0]["image"] == {"media_type": "image/jpeg", "data": "QUJD"}
    for side in ("before", "after"):
        _assert_slim_rows(payload[side]["page"])
        assert "off_screen_controls" not in payload[side]["page"]
        assert "controls_off_screen" not in payload[side]["page"]
        assert "emphasized_text" not in payload[side]["page"]
    assert not (_BRAIN_ONLY_KEYS & _all_keys(payload))
    # The rows were ~2 KB each; the whole two-sided packet is now a few KB.
    assert len(json.dumps(payload)) < 9000


def test_current_state_rung_sends_no_before_and_a_slim_after(monkeypatch):
    captured = _capture(monkeypatch)
    er._final_action_outcome_verdict(
        1,
        goal="g",
        pending={"label": "Submit"},
        before_shot={},
        result={"ok": True},
        post_shot=_shot("Submitted."),
        verification_mode="current_state_after_irreversible_attempt",
    )
    payload = captured[0]["payload"]
    assert payload["before"] == {}
    _assert_slim_rows(payload["after"]["page"])


def test_a_stored_before_entry_with_full_rows_is_slimmed_on_reconciliation(monkeypatch):
    from app.vision_agent import compact_page_evidence

    captured = _capture(monkeypatch)
    old_entry = {"url": "u", "title": "t", "page": compact_page_evidence(_page("Before text"), control_limit=20)}
    assert any("dom_context" in row for row in old_entry["page"]["controls"])
    er._final_action_outcome_verdict(
        1,
        goal="g",
        pending={"label": "Submit"},
        before_shot={"recorded_before": old_entry},
        result={"ok": False},
        post_shot=_shot("Submitted."),
        verification_mode="current_state_after_consumed_attempt",
    )
    before = captured[0]["payload"]["before"]
    assert before["url"] == "u" and before["page"]["viewport_text"] == "Before text"
    _assert_slim_rows(before["page"])
    # What an unverified attempt stores now is already slim, and slimming it again changes nothing.
    stored = er._final_action_before_entry(_shot("Before text"))
    _assert_slim_rows(stored["page"])
    assert er._outcome_page(stored["page"]) == stored["page"]


def test_reversible_readback_and_expected_effect_packets_are_slim(monkeypatch):
    captured = _capture(monkeypatch)
    er._reversible_click_visual_verdict(
        1,
        goal="g",
        action={"question": "Industry", "text": "Retail"},
        bound_label="Retail",
        before_shot=_shot("Industry: Software"),
        result={"ok": True, "activation_attempted": True, "activation_count": 1},
        post_shot=_shot("Industry: Retail"),
    )
    er._expected_effect_verdict(
        1,
        goal="g",
        pending={"expect": "Billing tab selected", "kind": "click", "label": "Billing", "before_shot": _shot("Account")},
        post_shot=_shot("Billing"),
    )
    readback, expected = (item["payload"] for item in captured)
    assert readback["requested_control"]["intended_value"] == "Retail"
    for payload in (readback, expected):
        _assert_slim_rows(payload["after"]["page"])
        assert not (_BRAIN_ONLY_KEYS & _all_keys(payload))
    _assert_slim_rows(readback["before"]["page"])
    assert len(expected["before"]["page"]["controls"]) == 12


def test_text_around_controls_outside_the_viewport_travels_once_per_page():
    # Replay: "Review rejected" sat in a task card above the viewport, reachable only through the rows' DOM
    # context; without it the verifier saw no message at all.
    controls = [
        _heavy_row(
            i,
            dom_context=[
                {"relation": "previous_sibling", "text": "2. Checkout Review rejected: wrong jacket.", "depth": 2},
                {"relation": "previous_sibling", "text": "Send to review", "depth": 0},
            ],
            accessibility_context=[{"relation": "described_by", "text": "Postcode is required"}],
        )
        for i in range(6)
    ]
    page = er._outcome_page_evidence({"viewport_text": "Cart Total $179.90 Send to review", "controls": controls})
    assert page["context_text"] == ["2. Checkout Review rejected: wrong jacket.", "Postcode is required"]
    assert er._outcome_page(page) == page


def test_verifier_instruction_reads_a_negative_check_beside_a_saved_line_as_failed(monkeypatch):
    client = LumenModelClient()
    prompts = []
    provider = type("P", (), {"live_enabled": True})()
    monkeypatch.setattr(client.provider_service, "vision_provider", lambda user_id=None: provider)
    monkeypatch.setattr(
        client,
        "_call_model_json",
        lambda **kwargs: prompts.append(kwargs["prompt"]) or {"ok": True, "data": {"status": "uncertain", "confidence": 0.5, "evidence": "x"}, "metadata": {}},
    )
    client.verify_final_action_outcome({"verification_mode": "current_state_after_irreversible_attempt"}, image={"media_type": "image/jpeg", "data": "B64"})
    assert "not done yet" in prompts[0]["instruction"] and "(Saved, Sent)" in prompts[0]["instruction"]


def test_outcome_text_window_is_wider_than_the_compact_one():
    long_text = "word " * 1000
    page = er._outcome_page_evidence({"viewport_text": long_text, "controls": []})
    assert len(page["viewport_text"]) == er._OUTCOME_VIEWPORT_TEXT_CHARS
    assert er._OUTCOME_VIEWPORT_TEXT_CHARS > 1400


def test_verifier_prompt_names_its_reply_keys_after_the_static_instruction(monkeypatch):
    client = LumenModelClient()
    prompts = []
    provider = type("P", (), {"live_enabled": True})()
    monkeypatch.setattr(client.provider_service, "vision_provider", lambda user_id=None: provider)

    def fake_call(**kwargs):
        prompts.append(kwargs["prompt"])
        return {"ok": True, "data": {"status": "uncertain", "confidence": 0.5, "evidence": "x"}, "metadata": {}}

    monkeypatch.setattr(client, "_call_model_json", fake_call)
    image = {"media_type": "image/jpeg", "data": "B64"}
    client.verify_final_action_outcome({"verification_mode": "post_action_comparison"}, image=image)
    client.verify_final_action_outcome({"verification_mode": "reversible_click_visual_readback"}, image=image)
    client.verify_final_action_outcome({"verification_mode": "expected_effect"}, image=image)
    final, readback, expected = prompts
    for prompt in prompts:
        # Static first (instruction, then the reply's keys), the per-call evidence last: a cacheable prefix.
        assert list(prompt) == ["instruction", "response_shape", "evidence"]
        assert set(prompt["response_shape"]) >= {"status", "confidence", "evidence"}
    assert "awaiting_confirmation" in final["response_shape"]
    assert "observed_value" in readback["response_shape"]
    assert set(expected["response_shape"]) == {"status", "confidence", "evidence"}
