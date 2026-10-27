"""The bytes a provider can cache (system rules + the packet's stable keys + the schema) must not change
between steps of one surface, nor when the body's available actions change on a surface switch."""

from __future__ import annotations

import json

from app import model_provider, vision_agent
from app.prompt_cache import SYSTEM_DYNAMIC_BOUNDARY, user_wire_parts

VIEW = {"width": 1000, "height": 700, "url": "http://bench.test/", "title": "Task"}

DESKTOP_TOOLS = {
    "os": "Windows",
    "applications": [{"app_id": "app:1", "name": "Notepad"}],
    "tool_targets": {"focus_window": "Use an exact window_id."},
}


def _context(surface: str, **overrides) -> dict:
    context = {
        "goal": "Fill in the form",
        "execution_surface": surface,
        "surfaces": ["browser", "desktop"],
        "your_notes": ["n1"],
        "your_plan": "1. open\n2. type",
        "human_answers": [],
        "already_asked": [],
        "satisfied_controls": [],
        "recent_actions": [{"step": 1}],
        "unavailable_actions": (
            ["navigate", "reload", "switch_tab", "watch"]
            if surface == "desktop"
            else ["launch_app", "focus_window", "read_clipboard", "open_file"]
        ),
    }
    if surface == "desktop":
        context["desktop_tools"] = {**DESKTOP_TOOLS, "windows": [{"window_id": "hwnd:1", "title": "Notepad", "active": True}]}
    context.update(overrides)
    return context


def _cached_prefix(model_input: dict) -> tuple[str, str, str]:
    """(system on the wire, the packet's stable text, the schema), exactly as the transports build them."""
    system = model_provider._wire_system_text(model_input["system"])
    stable, _tail = user_wire_parts(model_input["system"], model_input["prompt"])
    return system, stable, json.dumps(model_input["schema"], sort_keys=True)


def test_prefix_is_identical_across_steps_of_one_surface() -> None:
    first = vision_agent.prepare_model_input(VIEW, _context("desktop"))
    second_context = _context(
        "desktop",
        desktop_tools={
            **DESKTOP_TOOLS,
            "windows": [{"window_id": "hwnd:2", "title": "Calculator", "active": True}],  # focus moved
        },
        human_answers=[{"question": "Which account?", "answer": "Work"}],  # a question was answered
        already_asked=["Which account?"],
        satisfied_controls=[{"control": 3}],
        last_page_view={"url": "http://bench.test/", "title": "Task", "text": "changed"},
        recent_actions=[{"step": 1}, {"step": 2}],
    )
    second = vision_agent.prepare_model_input(VIEW, second_context)
    assert first["prompt"] != second["prompt"]
    assert _cached_prefix(first) == _cached_prefix(second)
    # The changing facts are still sent, after the stable section.
    stable, _tail = user_wire_parts(second["system"], second["prompt"])
    _, rest = model_provider._wire_user_text(second["system"], second["prompt"]).split(stable, 1)
    for key in ("desktop_windows", "human_answers", "already_asked", "satisfied_controls", "last_page_view"):
        assert key in second["prompt"] and f'"{key}"' in rest and f'"{key}"' not in stable
    assert list(second["prompt"]).index("human_answers") < list(second["prompt"]).index("already_asked")
    assert list(second["prompt"]).index("already_asked") < list(second["prompt"]).index("satisfied_controls")


def test_surface_switch_keeps_the_schema_and_the_key_text_and_each_surface_keeps_its_prefix() -> None:
    browser = vision_agent.prepare_model_input(VIEW, _context("browser"))
    desktop = vision_agent.prepare_model_input(VIEW, _context("desktop"))
    back = vision_agent.prepare_model_input(VIEW, _context("browser", recent_actions=[{"step": 9}]))

    # Different available actions on each surface change nothing in the schema or the system's key lists...
    assert browser["schema"] == desktop["schema"]
    schema_actions = browser["schema"]["schema"]["properties"]["action"]["enum"]
    for name in ("navigate", "watch", "launch_app", "focus_window", "read_clipboard", "open_file"):
        assert name in schema_actions

    def schema_text(model_input: dict) -> str:
        system = model_input["system"].partition(SYSTEM_DYNAMIC_BOUNDARY)[0]
        return system[system.index("ACTION CHEAT SHEET") :] if "ACTION CHEAT SHEET" in system else ""

    assert schema_text(browser) and schema_text(browser) == schema_text(desktop)
    # ...they ride in a dynamic line after the cache boundary, which is the only part that differs.
    for model_input, names in ((browser, ["launch_app"]), (desktop, ["navigate", "watch"])):
        dynamic = model_input["system"].partition(SYSTEM_DYNAMIC_BOUNDARY)[2]
        assert "unavailable now" in dynamic and all(name in dynamic for name in names)
    # The rules text names the surface it describes, so each surface has its own stable prefix; going back
    # to a surface gives the very bytes of its earlier prefix (still warm in the provider's cache).
    assert _cached_prefix(browser) == _cached_prefix(back)
    assert _cached_prefix(browser)[0] != _cached_prefix(desktop)[0]


def test_transport_receipt_records_a_short_hash_of_the_cached_prefix() -> None:
    first = vision_agent.prepare_model_input(VIEW, _context("desktop"))
    moved = vision_agent.prepare_model_input(
        VIEW, _context("desktop", human_answers=[{"question": "q", "answer": "a"}], recent_actions=[{"step": 4}])
    )
    other_goal = vision_agent.prepare_model_input(VIEW, _context("desktop", goal="Something else"))

    def receipt_for(model_input: dict) -> dict:
        receipt: dict = {}
        model_provider._capture_provider_http_json(
            receipt,
            body={"model": "m"},
            system=model_input["system"],
            prompt=model_input["prompt"],
            included_schema=model_input["schema"],
            included_image=None,
        )
        return receipt

    a, b, c = receipt_for(first), receipt_for(moved), receipt_for(other_goal)
    assert len(a["cached_prefix_sha12"]) == 12
    assert a["cached_prefix_sha12"] == b["cached_prefix_sha12"]
    assert a["provider_payload_prompt_sha256"] != b["provider_payload_prompt_sha256"]
    assert c["cached_prefix_sha12"] != a["cached_prefix_sha12"]
    parts_a, parts_c = a["cached_prefix_parts_sha12"], c["cached_prefix_parts_sha12"]
    assert parts_a["system"] == parts_c["system"] and parts_a["schema"] == parts_c["schema"]
    assert parts_a["stable_keys"] != parts_c["stable_keys"]
