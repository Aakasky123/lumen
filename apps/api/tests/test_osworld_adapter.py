"""OSWorld adapter: the AT-SPI inventory, key chords, and an OS-neutral desktop prompt."""

from app import vision_agent
from app.agent_eval.osworld import a11y
from app.agent_eval.osworld.body import pyautogui_keys

NS = (
    'xmlns:st="https://accessibility.ubuntu.example.org/ns/state" '
    'xmlns:cp="https://accessibility.ubuntu.example.org/ns/component" '
    'xmlns:val="https://accessibility.ubuntu.example.org/ns/value"'
)
TREE = f"""<desktop-frame {NS}>
  <application name="gnome-shell">
    <push-button name="Activities" st:showing="true" st:visible="true" st:enabled="true" cp:screencoord="(0, 0)" cp:size="(90, 30)"/>
  </application>
  <application name="gedit">
    <frame name="notes.txt - gedit" st:showing="true" st:active="true" cp:screencoord="(70, 30)" cp:size="(1850, 1050)">
      <push-button name="Save" st:showing="true" st:visible="true" st:enabled="true" cp:screencoord="(1700, 40)" cp:size="(60, 30)"/>
      <text name="" st:showing="true" st:visible="true" st:editable="true" st:focused="true" st:multi-line="true" cp:screencoord="(80, 90)" cp:size="(1800, 900)">hello world</text>
      <password-text name="Token" val:value="s3cret" st:showing="true" st:visible="true" st:editable="true" cp:screencoord="(80, 1000)" cp:size="(300, 30)"/>
      <radio-button name="" st:showing="true" st:visible="true" st:focusable="true" st:checkable="true" cp:screencoord="(1200, 540)" cp:size="(20, 20)"/>
      <panel name="" st:showing="true" st:visible="true" cp:screencoord="(80, 90)" cp:size="(500, 500)"/>
      <label name="hidden" st:showing="false" st:visible="true" cp:screencoord="(10, 10)" cp:size="(40, 20)"/>
      <label name="offscreen" st:showing="true" st:visible="true" cp:screencoord="(4000, 10)" cp:size="(40, 20)"/>
    </frame>
  </application>
</desktop-frame>"""


def test_the_tree_becomes_a_desktop_control_inventory() -> None:
    parsed = a11y.parse_tree(TREE, screen_width=1920, screen_height=1080)
    names = [(item["role"], item["name"] or item["text"]) for item in parsed["controls"]]
    assert ("push-button", "Activities") in names and ("push-button", "Save") in names
    assert all(name not in {"hidden", "offscreen"} for _, name in names)
    # A nameless control that takes input is listed; a nameless container is not.
    assert ("radio-button", "") in names and ("panel", "") not in names
    # Window-level containers are in the window list, not the click-binding inventory.
    assert all(role != "frame" for role, _ in names)
    assert [item["index"] for item in parsed["controls"]] == list(range(1, len(parsed["controls"]) + 1))
    focused = parsed["focused"]
    assert focused["role"] == "text" and focused["editable"] and focused["multi_line"] and focused["value"] == "hello world"
    token = next(item for item in parsed["controls"] if item["role"] == "password-text")
    assert token["value"] == "" and token["password"] is True
    assert parsed["windows"] == [{"window_id": "win:1.0", "title": "notes.txt - gedit", "app": "gedit", "active": True}]
    save = next(item for item in parsed["controls"] if item["name"] == "Save")
    assert a11y.find_control(TREE, save["selector"], screen_width=1920, screen_height=1080)["name"] == "Save"


def test_key_chords_map_to_pyautogui_names() -> None:
    assert pyautogui_keys("CTRL+SHIFT+T") == ["ctrl", "shift", "t"]
    assert pyautogui_keys("ENTER") == ["enter"]
    assert pyautogui_keys("SUPER") == ["win"]
    assert pyautogui_keys("ALT+F4") == ["alt", "f4"]
    assert pyautogui_keys("PAGE_DOWN") == ["pagedown"]
    assert pyautogui_keys("a") == ["a"]


def test_the_desktop_prompt_names_the_bodys_os_and_windows_stays_identical() -> None:
    view = {"width": 1920, "height": 1080, "url": "desktop://x"}
    default = vision_agent.prepare_model_input(view, {"goal": "g", "execution_surface": "desktop", "desktop_tools": {}})
    windows = vision_agent.prepare_model_input(view, {"goal": "g", "execution_surface": "desktop", "desktop_tools": {"os": "Windows"}})
    ubuntu = vision_agent.prepare_model_input(view, {"goal": "g", "execution_surface": "desktop", "desktop_tools": {"os": "Ubuntu"}})
    assert default["system"] == windows["system"]
    for phrase, _ in vision_agent._DESKTOP_OS_PHRASES:
        assert phrase in windows["system"], phrase
    assert "Windows" not in ubuntu["system"] and "Ubuntu desktop" in ubuntu["system"]
    # The terminal/shell rule is product policy on every OS.
    assert "Do not open a shell, terminal" in ubuntu["system"]
