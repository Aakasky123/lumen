"""read_form: every form control of the page, on screen or not, read-only, page text for the brain only."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from app import extension_run, vision_agent
from tests.test_extension_run import (  # noqa: F401 - the autouse fixtures apply to this module too
    _complete_final_action_readiness_audit_by_default,
    _ordinary_click_consequence_by_default,
    _ordinary_secret_semantics_by_default,
)
from tests.test_print_view import _drive

URL = "https://example.test/apply"
GOAL = "Fill in the application form using what the page asks."
AUTH_QUESTION = "Are you legally authorized to work in the US?"
FIRST_NAME_SELECTOR = 'input[name="first"]'
RADIO_YES_SELECTOR = "fieldset > label:nth-of-type(1) > input"
RADIO_NO_SELECTOR = "fieldset > label:nth-of-type(2) > input"
PIN_VALUE = "hunter22secret"

# What the extension's enumerator returns for a small form (selectors name the elements; ids come from the observation).
FORM_ROWS: list[dict[str, Any]] = [
    {
        "kind": "radio_group",
        "label": AUTH_QUESTION,
        "required": False,
        "disabled": False,
        "in_view": True,
        "options": [
            {"label": "Yes", "selector": RADIO_YES_SELECTOR, "checked": False},
            {"label": "No", "selector": RADIO_NO_SELECTOR, "checked": True},
        ],
        "selected": ["No"],
    },
    {
        "selector": FIRST_NAME_SELECTOR,
        "tag": "input",
        "kind": "text",
        "label": "First name",
        "required": True,
        "disabled": False,
        "in_view": True,
    },
    {
        "selector": 'select[name="country"]',
        "tag": "select",
        "kind": "select",
        "label": "Country",
        "required": True,
        "disabled": False,
        "in_view": True,
        "selected": ["United States"],
        "options": [
            {"label": "Choose one", "selected": False},
            {"label": "United States", "selected": True},
            {"label": "Canada", "selected": False},
        ],
    },
    {
        "selector": 'input[name="resume"]',
        "tag": "input",
        "kind": "file",
        "label": "Resume",
        "required": False,
        "disabled": False,
        "in_view": True,
        "files": [],
    },
    {
        "selector": 'input[name="start"]',
        "tag": "input",
        "kind": "date",
        "label": "Start date",
        "required": False,
        "disabled": False,
        "in_view": False,
    },
    {
        "selector": 'input[name="pin"]',
        "tag": "input",
        "kind": "password",
        "label": "pin",
        "required": False,
        "disabled": False,
        "in_view": True,
        "value_redacted": True,
        "value": "[REDACTED]",
    },
]


def _observed_controls() -> list[dict[str, Any]]:
    """The latest observation's controls, relabelled with stable ids by the registry (selector + tag keyed)."""
    return [
        {"index": 1, "selector": FIRST_NAME_SELECTOR, "tag": "input", "role": "textbox", "label": "First name",
         "x": 100, "y": 120, "width": 240, "height": 40},
        {"index": 2, "selector": RADIO_YES_SELECTOR, "tag": "input", "role": "radio", "label": "Yes",
         "x": 100, "y": 220, "width": 16, "height": 16},
        {"index": 3, "selector": RADIO_NO_SELECTOR, "tag": "input", "role": "radio", "label": "No",
         "x": 160, "y": 220, "width": 16, "height": 16},
    ]


def test_the_parser_accepts_the_action_and_the_cheat_sheet_names_it_once() -> None:
    action = vision_agent.normalize_action({"action": "read_form", "reason": "learn every question"})
    assert action["action"] == "read_form" and not action["invalid_reason"]
    schema = vision_agent.action_schema()
    assert "read_form" in schema["schema"]["properties"]["action"]["enum"]
    sheet = vision_agent.action_cheat_sheet(schema)
    assert sheet.count("read_form") == 1
    assert "before scrolling to hunt for fields" in sheet
    assert "read_form" not in vision_agent.action_cheat_sheet(vision_agent.action_schema(without=["read_form"]))


def test_a_body_without_the_capability_is_not_offered_the_action() -> None:
    class Plain:
        def downloads_listing(self, snapshot):
            return []

    class WithForm(Plain):
        async def vision_read_form(self):
            return {"ok": True}

    assert "read_form" in extension_run._actions_unavailable_on_body(Plain(), "desktop")
    assert "read_form" in extension_run._actions_unavailable_on_body(Plain(), "browser")
    assert "read_form" not in extension_run._actions_unavailable_on_body(WithForm(), "browser")


def test_the_outline_takes_the_stable_ids_of_the_latest_observation_and_no_selector() -> None:
    rows = extension_run._form_outline_with_ids(FORM_ROWS, _observed_controls())
    by_label = {row.get("label"): row for row in rows}
    assert by_label["First name"]["id"] == 1
    assert "id" not in by_label["Start date"], "a control the observation does not list has no id"
    options = by_label[AUTH_QUESTION]["options"]
    assert [option.get("id") for option in options] == [2, 3]
    assert "selector" not in by_label["First name"]
    assert all("selector" not in option for option in options)


def test_a_masked_value_is_shown_as_redacted_whatever_the_body_sent() -> None:
    leaked = [{"kind": "password", "label": "pin", "value_redacted": True, "value": PIN_VALUE, "in_view": True}]
    text = extension_run._form_outline_text(extension_run._form_outline_with_ids(leaked, []))
    assert "[REDACTED]" in text and PIN_VALUE not in text


def test_the_outline_lines_say_what_is_required_empty_and_chosen() -> None:
    text = extension_run._form_outline_text(extension_run._form_outline_with_ids(FORM_ROWS, _observed_controls()))
    lines = text.split("\n")
    assert lines[0] == (
        f'"{AUTH_QUESTION}": radio_group, in view, selected "No"; options: [2] "Yes", [3] "No" (selected)'
    )
    assert lines[1] == '[1] "First name": text, required, in view, empty'
    assert lines[2] == '"Country": select, required, in view, selected "United States"; options: "Choose one", "United States" (selected), "Canada"'
    assert lines[3] == '"Resume": file, in view, no file attached'
    assert lines[4] == '"Start date": date, off screen, empty'
    assert lines[5] == '"pin": password, in view, value "[REDACTED]"'


def test_a_withheld_value_is_reported_as_present_not_as_empty_or_unchecked() -> None:
    # A hidden control's free-text value is withheld by the body (value_withheld): the brain must not read it as empty.
    # A hidden checkbox or radio reports its checked state, and a masked one shows [REDACTED], not its state.
    rows = [
        {"kind": "text", "label": "Card number", "in_view": False, "hidden": True, "value_withheld": "hidden"},
        {"kind": "checkbox", "label": "Consent", "in_view": False, "hidden": True, "checked": True},
        {"kind": "checkbox", "label": "Token", "in_view": True, "value_redacted": True, "value": "[REDACTED]"},
        {"kind": "radio_group", "label": "Question", "in_view": True, "hidden": True, "selected": ["No"],
         "options": [{"label": "Yes", "checked": False}, {"label": "No", "checked": True}]},
    ]
    lines = extension_run._form_outline_text(rows).split("\n")
    assert lines[0] == '"Card number": text, not rendered now, off screen, value withheld (hidden)'
    assert lines[1] == '"Consent": checkbox, not rendered now, off screen, checked'
    assert lines[2] == '"Token": checkbox, in view, value "[REDACTED]"'
    assert lines[3] == '"Question": radio_group, not rendered now, in view, selected "No"; options: "Yes", "No" (selected)'


def test_a_cut_outline_ends_at_a_whole_row_with_the_marker() -> None:
    rows = [{"kind": "text", "label": f"Field {i}", "in_view": True} for i in range(40)]
    text = extension_run._form_outline_text(rows)
    shown, cut = extension_run._cut_form_outline(text, 200)
    assert cut is True and shown.endswith("truncated]")
    body_cut_text, body_cut = extension_run._cut_form_outline("a\nb", 200, body_cut=True)
    assert body_cut is True and body_cut_text.endswith("truncated]")
    assert extension_run._cut_form_outline("a\nb", 200) == ("a\nb", False)


def test_the_outline_is_page_text_for_the_brain_and_never_grounding_evidence() -> None:
    outline = {"text": f'"{AUTH_QUESTION}": radio_group, in view', "controls": 1, "truncated": False}
    context = {"form_outline": outline, "page_evidence": {"viewport_text": "Application"}}
    blob = extension_run._grounding_page_blob(context)
    assert not extension_run._grounding_quote_in_blob("radio_group", blob)
    assert not extension_run._grounding_quote_in_blob(AUTH_QUESTION, blob)


class _FormBody:
    execution_surface = "browser"
    device_id = "fake-browser-form-outline"

    def __init__(self, *, supports_form: bool = True, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows if rows is not None else FORM_ROWS
        self.form_reads = 0
        self.typed: list[tuple[str, str]] = []
        if supports_form:
            self.vision_read_form = self._read_form

    async def _read_form(self) -> dict[str, Any]:
        self.form_reads += 1
        return {"ok": True, "action": "read_form", "form_outline": copy.deepcopy(self.rows), "form_outline_truncated": False}

    async def capture_screenshot(self) -> dict:
        return {
            "ok": True,
            "image": "jpeg",
            "width": 800,
            "height": 600,
            "url": URL,
            "title": "Application",
            "scroll_y": 0,
            "scroll_height": 600,
            "document_lifecycle_token": "doc-form",
            "page_evidence": {
                "viewport_text": "Application. First name",
                "document_lifecycle_token": "doc-form",
                "controls": copy.deepcopy(_observed_controls()),
            },
        }

    async def vision_type_control(self, selector: str, text: str, **_binding: object) -> dict:
        self.typed.append((selector, text))
        return {"ok": True, "action": "type", "active_typable": True, "active_value": text, "active_value_matches_expected": True}


def _read() -> dict[str, Any]:
    return vision_agent.normalize_action({"action": "read_form", "reason": "Learn every question before scrolling."})


def _wait() -> dict[str, Any]:
    return {"action": "wait", "wait_ms": 100, "reason": "Let the page settle."}


def _type_from_outline() -> dict[str, Any]:
    return {
        "action": "type",
        "control": 1,
        "x": 220,
        "y": 140,
        "text": "Ada",
        "question": "First name",
        "grounding_source": "page",
        "grounding_quote": "First name",
        "reason": "Fill the first name the outline lists.",
    }


def test_the_run_loop_shows_the_outline_on_the_next_step_only_until_the_next_action(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _FormBody()
    narrated, contexts = _drive(monkeypatch, body, [_read(), _wait(), _wait()], max_steps=3)
    assert body.form_reads == 1
    assert "form_outline" in contexts[1]
    outline = contexts[1]["form_outline"]
    assert outline["controls"] == len(FORM_ROWS)
    assert "Page text, not a user fact" in outline["note"]
    assert "[1] \"First name\"" in outline["text"] and PIN_VALUE not in outline["text"]
    assert "form_outline" not in contexts[2]
    receipts = [data for kind, data in narrated if kind == "receipt" and data.get("type") == "form_outline_read"]
    assert receipts and (receipts[0].get("verification") or {}).get("controls") == len(FORM_ROWS)
    assert "First name" not in str(receipts), "the receipt carries the count, never the outline's text"


def test_the_outline_never_grounds_a_typed_value(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _FormBody()
    _drive(monkeypatch, body, [_read(), _type_from_outline(), _type_from_outline(), _type_from_outline()])
    assert body.form_reads == 1
    assert body.typed == []


def test_a_masked_value_in_the_outline_never_reaches_the_brain(monkeypatch: pytest.MonkeyPatch) -> None:
    # A body that let a raw value through: the server still shows the masked form only.
    leaking = copy.deepcopy(FORM_ROWS)
    leaking[-1]["value"] = PIN_VALUE
    body = _FormBody(rows=leaking)
    _narrated, contexts = _drive(monkeypatch, body, [_read(), _wait(), _wait()], max_steps=3)
    shown = str(contexts[1].get("form_outline"))
    assert "[REDACTED]" in shown and PIN_VALUE not in shown


def test_a_body_without_the_outline_refuses_a_stray_call(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _FormBody(supports_form=False)
    narrated, contexts = _drive(monkeypatch, body, [_read(), _wait(), _wait()], max_steps=3)
    text = str([data for kind, data in narrated if kind == "receipt"])
    assert "form_outline_unsupported_on_this_body" in text
    assert all("form_outline" not in context for context in contexts)
