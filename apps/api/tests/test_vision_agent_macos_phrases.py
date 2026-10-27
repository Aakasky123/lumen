"""Per-OS desktop prompt phrasing: Windows and Ubuntu/Linux stay as they were, macOS names Command keys and Finder."""

from __future__ import annotations

import json

import app.vision_agent as vision_agent


def _desktop_prompt(os_name: str | None) -> dict:
    tools: dict = {"windows": [], "applications": []}
    if os_name is not None:
        tools["os"] = os_name
    return vision_agent.build_prompt(
        {"width": 1280, "height": 800, "url": "", "title": ""},
        {
            "goal": "Copy the invoice total into the sheet",
            "execution_surface": "desktop",
            "surfaces": ["desktop"],
            "desktop_tools": tools,
            "page_evidence": {},
        },
    )


def _instruction(os_name: str | None) -> str:
    return _desktop_prompt(os_name)["instruction"]


def test_windows_desktop_text_is_unchanged_by_os_mapping() -> None:
    raw = vision_agent.DESKTOP_AGENT_SYSTEM
    assert vision_agent.desktop_os_text(raw, "Windows") == raw
    assert _instruction(None) == _instruction("Windows")
    windows = _instruction("Windows")
    assert "the Windows computer task" in windows
    assert "CTRL+A, CTRL+C, CTRL+V, or ALT+TAB" in windows
    assert "File Explorer" in windows
    assert "CMD+" not in windows
    assert "a Linux sandbox with no network" in windows


def test_ubuntu_text_keeps_the_generic_table() -> None:
    ubuntu = _instruction("Ubuntu")
    assert "the front File Explorer window" in ubuntu
    assert "CTRL+C" in ubuntu
    assert "CMD+" not in ubuntu
    assert "on this Ubuntu task machine the machine itself, as its user" in ubuntu
    assert "Windows computer" not in ubuntu
    assert "a Linux sandbox with no network" not in ubuntu
    assert "Accessibility controls in control_inventory" in ubuntu
    assert "the Windows" not in ubuntu


def test_macos_instruction_names_command_keys_and_finder() -> None:
    mac = _instruction("macOS")
    assert "TAB, SHIFT+TAB, CMD+A, CMD+C, CMD+V, or CMD+TAB" in mac
    assert "(select it, then CMD+C or the app's Copy)" in mac
    assert "it may follow the CMD+C in then" in mac
    assert "it, CMD+V pastes it." in mac
    assert "the front Finder window" in mac
    assert "Dangerous system shortcuts and shell windows are unavailable." in mac
    assert "run_command is unavailable on this Mac, so do not choose it." in mac
    assert "simulate Spotlight search" in mac
    assert "the macOS computer task" in mac
    for windows_term in ("CTRL+", "ALT+TAB", "File Explorer", "Windows", "Linux sandbox", "Start/Run", "Windows-key"):
        assert windows_term not in mac, windows_term


def test_macos_system_prompt_names_macos_not_windows() -> None:
    system = vision_agent.desktop_os_text(vision_agent.DESKTOP_AGENT_SYSTEM, "macOS")
    assert "the user's macOS desktop" in system
    assert "raw macOS accessibility controls" in system
    assert "Windows" not in system
    assert "Windows UI Automation" not in system


def test_macos_prompt_is_json_serialisable_and_os_name_is_exact() -> None:
    prompt = _desktop_prompt("macOS")
    assert json.dumps(prompt)
    assert vision_agent.desktop_os_name({"desktop_tools": {"os": "macOS"}}) == "macOS"
    assert vision_agent.desktop_os_name({"desktop_tools": {}}) == "Windows"
