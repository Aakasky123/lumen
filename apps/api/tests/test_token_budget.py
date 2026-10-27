from __future__ import annotations

import json

from app import token_budget as tb
from app import vision_agent


def _packet(**extra):
    base = {
        "goal": "Book the flight",
        "your_plan": "1. search",
        "last_reply": "clicked search",
        "viewport": {"width": 1280, "height": 800},
        "control_inventory": [{"index": i, "text": "row %d " % i + "x" * 40} for i in range(40)],
        "recent_actions": [f"action {i} " + "y" * 50 for i in range(6)],
    }
    base.update(extra)
    return base


def test_estimator_sanity():
    assert tb.estimate_tokens("") == 0
    assert 20 <= tb.estimate_tokens("a" * 100) <= 35
    assert tb.estimate_tokens("日本語") == 3
    assert 1000 <= tb.estimate_image_tokens(1280, 800) <= 1400
    assert tb.estimate_image_tokens(1280, 800, "openai") == 85 + 170 * 3 * 2
    assert tb.estimate_image_tokens(300, 300, "gemini") == 258
    assert tb.estimate_image_tokens(4000, 3000) <= 1700


def test_budget_is_a_noop_on_a_typical_packet():
    packet = _packet()
    out, notes, over = tb.fit_packet(packet, cap_tokens=40000, fixed_tokens=10000)
    assert out is packet and notes == [] and over == 0


def test_over_budget_trims_in_order_and_keeps_protected_keys():
    packet = _packet(
        web_results=[{"text": "w" * 6000} for _ in range(3)],
        page_memory=[{"text": "m" * 2000} for _ in range(4)],
        your_notes=[f"step {i}: " + "n" * 400 for i in range(10)],
        session_ledger=[{"text": "l" * 100}] * 10,
    )
    total = tb.estimate_tokens(packet)
    out, notes, over = tb.fit_packet(packet, cap_tokens=total - 4000, fixed_tokens=0)
    assert notes[0].startswith("web_results")
    assert out["context_trimmed"] == notes
    assert len(out["page_memory"]) == 4 and len(out["your_notes"]) == 10  # later stages untouched
    out, notes, over = tb.fit_packet(packet, cap_tokens=50, fixed_tokens=0)
    for key in ("goal", "your_plan", "last_reply", "viewport"):
        assert out[key] == packet[key]
    assert over > 0 and len(out["control_inventory"]) == tb.CONTROL_ROWS_FLOOR
    assert out["recent_actions"] == packet["recent_actions"][-2:]
    kinds = [n.split(":")[0] for n in notes]
    assert kinds.index("web_results") < kinds.index("page_memory") < kinds.index("your_notes")
    assert kinds.index("your_notes") < kinds.index("session_ledger") < kinds.index("recent_actions")
    assert kinds.index("recent_actions") < kinds.index("control_inventory")


def test_focused_control_is_kept_when_rows_are_dropped():
    rows = [{"index": i, "text": "t" * 80, **({"focused": True} if i == 30 else {})} for i in range(60)]
    out, _, _ = tb.fit_packet({"goal": "g", "control_inventory": rows}, cap_tokens=300, fixed_tokens=0)
    assert any(r.get("focused") for r in out["control_inventory"])


def test_goal_and_recent_action_caps():
    goal = tb.cap_goal("g" * 9000)
    assert len(goal) < 6200 and "goal truncated" in goal
    assert tb.cap_goal("short") == "short"
    old_lines = ["a" * 1000, "b" * 1000]
    items = tb.cap_recent_actions(old_lines + ["c" * 1000, "d" * 2000, "ok"])
    # Older history is cut short; the newest three lines (the last step's feedback) keep their instructions.
    assert items[0].startswith("a" * 300) and "+700 chars" in items[0] and items[1].endswith("+700 chars]")
    assert items[2] == "c" * 1000 and items[3].startswith("d" * 1400) and "+600 chars" in items[3] and items[4] == "ok"


def test_prepare_model_input_reports_sizes_and_reduced_mode_uses_the_lower_cap():
    view = {"width": 1280, "height": 800, "url": "https://example.test/", "title": "t"}
    ctx = {
        "goal": "g" * 7000,
        "recent_actions": ["z" * 900, "y", "x", "w" * 900],
        "_image_dims": [(1280, 800)],
        "web_results": [{"text": "w" * 200000}],
    }
    env = vision_agent.prepare_model_input(view, ctx)
    report = env["size_report"]
    assert "goal truncated" in env["prompt"]["goal"]
    assert len(env["prompt"]["recent_actions"][0]) < 400
    assert env["prompt"]["recent_actions"][-1] == "w" * 900
    assert report["images"] > 1000 and report["system"] > 0 and report["cap"] == 40000
    assert env["prompt"].get("context_trimmed")
    reduced = vision_agent.prepare_model_input(view, {**ctx, "payload_mode": "reduced_after_timeout"})
    assert reduced["size_report"]["cap"] == 20000
    json.dumps(env["prompt"], default=str)
