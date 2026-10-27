"""The click-consequence judge reads a trimmed page: the selected control in full, its container, a
one-line label for everything else, and the screenshot only when the control has no text of its own."""

from __future__ import annotations

import json
from typing import Any

from app import extension_run
from app.db import init_db
from app.vision_agent import compact_page_evidence


def _control(index: int, text: str, *, tag: str = "button", x: int = 0, y: int = 0, **extra: Any) -> dict[str, Any]:
    return {
        "index": index,
        "tag": tag,
        "role": "",
        "type": extra.pop("type", ""),
        "id": f"c{index}",
        "name": "",
        "class_name": "btn btn-primary text-sm font-medium rounded px-4 py-2 transition-colors",
        "text": text,
        "nearby_text": f"{text} Some surrounding card text that the page repeats around every control " * 2,
        "context_text": extra.pop("context_text", f"Section {index}"),
        "dom_context": [{"relation": "previous_sibling", "depth": 1, "text": f"Card {index} description text"}] * 4,
        "ancestor_chain": extra.pop("ancestor_chain", [{"depth": 1, "tag": "div", "id": f"card{index}"}]),
        "visual_state": {"background_color": "rgb(0, 0, 0)", "color": "rgb(255, 255, 255)"},
        "x": x,
        "y": y,
        "width": 120,
        "height": 36,
        **extra,
    }


def _checkout_shot(*, target_text: str = "Place order", dialog_chain: bool = True, filler: int = 200) -> tuple[dict[str, Any], dict[str, Any]]:
    dialog = (
        [{"depth": 1, "tag": "div", "id": "checkout-dialog", "role": "dialog"}]
        if dialog_chain
        else [{"depth": 1, "tag": "div", "id": "checkout-panel"}]
    )
    controls = [
        _control(i, f"Product {i} Add to cart", x=40 + (i % 3) * 300, y=100 + (i // 3) * 120) for i in range(1, 25)
    ]
    controls.append(_control(25, "Edit cart", x=900, y=700, ancestor_chain=dialog, context_text="Review your order"))
    target = _control(
        26,
        target_text,
        x=900,
        y=760,
        ancestor_chain=dialog,
        context_text="Review your order",
        href="",
        form_action="/checkout/confirm",
        aria_label="" if not target_text else target_text,
    )
    controls.append(target)
    controls.append(_control(27, "Cancel", x=760, y=760, ancestor_chain=dialog, context_text="Review your order"))
    controls.append(_control(28, "Continue shopping", x=40, y=900))
    shot = {
        "url": "https://shop.example/checkout",
        "title": "Checkout",
        "image": "A" * 60000,
        "page_evidence": {
            "viewport_text": "Review your order. Total $129.00. " + "Filler text. " * filler,
            "controls": controls,
        },
    }
    return shot, target


def _capture_payload(monkeypatch, shot: dict[str, Any], target: dict[str, Any]) -> tuple[dict[str, Any], Any]:
    init_db()
    seen: dict[str, Any] = {}

    def fake_judge(self, payload, *, image=None):  # noqa: ANN001
        seen["payload"] = payload
        seen["image"] = image
        return {"classification": "uncertain", "confidence": 0.0, "evidence": "fixture"}

    monkeypatch.setattr("app.lumen_model.LumenModelClient.judge_blocked_click_semantics", fake_judge)
    extension_run._judge_click_consequence(
        0,
        goal="Buy the headphones",
        safety_policy={},
        shot=shot,
        action={"action": "click", "control_index": target["index"], "reason": "Place the order"},
        bound_control=target,
        selector="#c26",
        x=960,
        y=778,
        recent_verified_history=[{"step": i, "summary": f"step {i}"} for i in range(10)],
        operator_instructions=[],
    )
    return seen["payload"], seen["image"]


def test_trimmed_payload_keeps_target_container_and_heading_and_drops_unrelated_controls(monkeypatch) -> None:
    shot, target = _checkout_shot()
    payload, image = _capture_payload(monkeypatch, shot, target)
    page = payload["page"]["raw_observation"]
    click = payload["selected_click"]
    assert click["raw_control"]["text"] == "Place order"
    assert click["raw_control"]["form_action"] == "/checkout/confirm"
    assert click["raw_control"]["ancestor_chain"][0]["role"] == "dialog"
    assert "Review your order" in page["viewport_text"]
    assert payload["page"]["url"] == "https://shop.example/checkout"
    assert payload["page"]["title"] == "Checkout"
    assert payload["goal"] == "Buy the headphones"
    assert len(payload["recent_verified_history"]) == 6
    container = page["controls_in_target_container"]
    texts = [row["text"] for row in container]
    assert len(container) <= 12
    assert "Edit cart" in texts and "Cancel" in texts
    assert "Place order" not in texts, "the target is not repeated among its siblings"
    # The dialog's own controls rank ahead of the product grid.
    assert texts.index("Edit cart") < len(texts)
    assert "controls" not in page
    # Unrelated controls ride as one-line labels only, never as full rows.
    assert any("Continue shopping" in label for label in page["other_controls_on_page"])
    assert not any(isinstance(item, dict) for item in page["other_controls_on_page"])
    serialized = json.dumps(page)
    assert "dom_context" not in serialized and "class_name" not in serialized
    assert image is not None, "a target inside a dialog keeps the screenshot"
    assert len(page["viewport_text"]) <= 1400


def test_plain_page_target_with_its_own_text_needs_no_screenshot(monkeypatch) -> None:
    shot, target = _checkout_shot(dialog_chain=False, filler=0)
    _, image = _capture_payload(monkeypatch, shot, target)
    assert image is None, "a control with its own text needs no screenshot"


def test_portal_modal_text_past_the_head_is_kept_and_the_image_too(monkeypatch) -> None:
    shot, target = _checkout_shot(target_text="Yes", dialog_chain=False, filler=0)
    target["context_text"] = ""
    shot["page_evidence"]["viewport_text"] = (
        "Page body filler. " * 90 + "Delete user? This cannot be undone. Your card will be charged $499. Cancel Yes"
    )
    assert len(shot["page_evidence"]["viewport_text"]) > 1500
    payload, image = _capture_payload(monkeypatch, shot, target)
    text = payload["page"]["raw_observation"]["viewport_text"]
    assert "This cannot be undone" in text and "charged $499" in text
    assert len(text) <= 1400
    assert image is not None and image["data"] == shot["image"]


def test_icon_only_target_keeps_the_screenshot(monkeypatch) -> None:
    shot, target = _checkout_shot(target_text="")
    payload, image = _capture_payload(monkeypatch, shot, target)
    assert image is not None and image["data"] == shot["image"]
    assert payload["page"]["raw_observation"]["controls_in_target_container"]


def test_unlisted_occluded_canvas_and_desktop_targets_keep_the_screenshot() -> None:
    shot, target = _checkout_shot()
    plain, plain_target = _checkout_shot(dialog_chain=False, filler=0)
    assert extension_run._click_consequence_evidence(plain, plain_target)[1] is False
    assert extension_run._click_consequence_evidence(shot, target)[1] is True
    assert extension_run._click_consequence_evidence(shot, None)[1] is True
    assert extension_run._click_consequence_evidence(plain, {**plain_target, "occluded": True})[1] is True
    assert extension_run._click_consequence_evidence(shot, {**target, "tag": "canvas"})[1] is True
    assert extension_run._click_consequence_evidence(shot, target, drawn_text=True)[1] is True
    desktop = {**shot, "page_evidence": {**shot["page_evidence"], "execution_surface": "desktop"}}
    assert extension_run._click_consequence_evidence(desktop, target)[1] is True
    empty = {**plain, "page_evidence": {"viewport_text": "x"}}
    assert extension_run._click_consequence_evidence(empty, plain_target)[1] is True


def test_payload_is_far_smaller_than_the_full_page_payload(monkeypatch) -> None:
    shot, target = _checkout_shot()
    payload, image = _capture_payload(monkeypatch, shot, target)
    old_page = compact_page_evidence(shot["page_evidence"], control_limit=32)
    trimmed_chars = len(json.dumps(payload["page"]["raw_observation"]))
    old_chars = len(json.dumps(old_page))
    print(f"page evidence chars: before={old_chars} after={trimmed_chars}")
    assert trimmed_chars < old_chars * 0.6


def _judge_by_text(monkeypatch, shot: dict[str, Any], label: str) -> tuple[dict[str, Any], Any]:
    init_db()
    seen: dict[str, Any] = {}

    def fake_judge(self, payload, *, image=None):  # noqa: ANN001
        seen["payload"] = payload
        seen["image"] = image
        return {"classification": "uncertain", "confidence": 0.0, "evidence": "fixture"}

    monkeypatch.setattr("app.lumen_model.LumenModelClient.judge_blocked_click_semantics", fake_judge)
    action = {"action": "click", "target_text": label, "reason": "Open it"}
    extension_run._judge_click_consequence(
        0,
        goal="Pay the invoice",
        safety_policy={},
        shot=shot,
        action=action,
        bound_control=extension_run._label_named_control(action, shot),
        selector="",
        x=0,
        y=0,
        recent_verified_history=[],
        operator_instructions=[],
    )
    return seen["payload"], seen["image"]


def test_target_text_click_on_a_link_is_judged_with_its_destination(monkeypatch) -> None:
    shot, _ = _checkout_shot(dialog_chain=False, filler=0)
    shot["page_evidence"]["controls"].append(
        _control(40, "View invoice", tag="a", x=40, y=300, href="https://evil.example/login?next=pay")
    )
    payload, image = _judge_by_text(monkeypatch, shot, "View invoice")
    raw = payload["selected_click"]["raw_control"]
    assert raw["href"] == "https://evil.example/login?next=pay"
    # The body resolves the label itself at dispatch, so the row is not trusted alone: screenshot kept.
    assert image is not None
    assert raw["text"] == "View invoice"


def test_target_text_link_without_a_destination_keeps_the_screenshot(monkeypatch) -> None:
    shot, _ = _checkout_shot(dialog_chain=False, filler=0)
    shot["page_evidence"]["controls"].append(_control(40, "View invoice", tag="a", x=40, y=300))
    payload, image = _judge_by_text(monkeypatch, shot, "View invoice")
    assert "href" not in payload["selected_click"]["raw_control"]
    assert image is not None


def test_target_text_not_listed_or_ambiguous_keeps_the_screenshot(monkeypatch) -> None:
    shot, _ = _checkout_shot(dialog_chain=False, filler=0)
    _, image = _judge_by_text(monkeypatch, shot, "View invoice")
    assert image is not None
    for i in (40, 41):
        shot["page_evidence"]["controls"].append(
            _control(i, "View invoice", tag="a", x=40, y=300 + i, href=f"https://x{i}.example/")
        )
    payload, image = _judge_by_text(monkeypatch, shot, "View invoice")
    assert image is not None and "href" not in payload["selected_click"]["raw_control"]
