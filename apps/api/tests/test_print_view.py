"""read_print_view: the page as it renders for print, read-only, citable as page text of the same document only."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from app import extension_run, models, vision_agent
from app.db import SessionLocal, init_db
from app.extension_run import run_vision_loop
from tests.test_extension_run import (  # noqa: F401 - the autouse fixtures apply to this module too
    _DummyContextEngine,
    _complete_final_action_readiness_audit_by_default,
    _ordinary_click_consequence_by_default,
    _ordinary_secret_semantics_by_default,
)

URL = "https://example.test/ticket"
PRINT_ONLY = "Reference number: 48213"
GOAL = "Fill in the reference number field using what the page shows."


def test_the_parser_accepts_the_action_and_the_cheat_sheet_names_it_once() -> None:
    action = vision_agent.normalize_action({"action": "read_print_view", "reason": "see the print version"})
    assert action["action"] == "read_print_view" and not action["invalid_reason"]
    assert vision_agent.normalize_action({"action": "print_view"})["action"] == "read_print_view"
    schema = vision_agent.action_schema()
    assert "read_print_view" in schema["schema"]["properties"]["action"]["enum"]
    sheet = vision_agent.action_cheat_sheet(schema)
    assert sheet.count("read_print_view") == 1
    assert "read_print_view" not in vision_agent.action_cheat_sheet(vision_agent.action_schema(without=["read_print_view"]))


def test_a_body_without_the_capability_is_not_offered_the_action() -> None:
    class Plain:
        def downloads_listing(self, snapshot):
            return []

    class WithPrint(Plain):
        async def vision_read_print_view(self):
            return {"ok": True}

    assert "read_print_view" in extension_run._actions_unavailable_on_body(Plain(), "browser")
    assert "read_print_view" not in extension_run._actions_unavailable_on_body(WithPrint(), "browser")


def test_print_view_text_is_page_evidence_only_for_the_same_document() -> None:
    state = {"url": URL + "#top", "lifecycle": "doc-1", "text": PRINT_ONLY}
    assert extension_run._print_view_for_document(state, URL, "doc-1") == {"text": PRINT_ONLY, "url": URL + "#top"}
    assert extension_run._print_view_for_document(state, URL, "doc-2") is None
    assert extension_run._print_view_for_document(state, "https://example.test/other", "doc-1") is None
    assert extension_run._print_view_for_document({}, URL, "doc-1") is None
    context = {"_print_view": {"text": PRINT_ONLY, "url": URL}, "page_evidence": {"viewport_text": "Ticket"}}
    assert extension_run._grounding_quote_in_blob(PRINT_ONLY, extension_run._grounding_page_blob(context))
    assert not extension_run._grounding_quote_in_blob(PRINT_ONLY, extension_run._grounding_page_blob({"page_evidence": {"viewport_text": "Ticket"}}))


def test_the_grounding_judge_gets_no_print_view_entry() -> None:
    context = {"_print_view": {"text": f"Intro. {PRINT_ONLY}. Thanks.", "url": URL}, "_current_page_url": URL}
    entries = extension_run._grounding_judge_page_entries(
        {"text": "48213", "grounding_quote": PRINT_ONLY},
        {"reason": "value_not_supported_by_quote", "source": "page"},
        decision_context=context,
        bound_control=None,
        page_evidence={"viewport_text": "Ticket"},
        receiving_surface_is_page=True,
    )
    assert all("print" not in entry["label"].lower() and "Thanks" not in entry["value"] for entry in entries)


def test_the_extension_body_sends_the_sensitive_mask_binding_with_the_read() -> None:
    from app.extension_body import ExtensionBody

    sent: list[tuple[str, dict]] = []

    class Stub(ExtensionBody):
        def __init__(self) -> None:
            self._required_sensitive_screenshot_binding = {
                "selector": "#pw",
                "expected_page_context": {"url": URL},
            }

        async def _call(self, method, params=None, *, timeout=0.0):
            sent.append((method, dict(params or {})))
            return {"ok": True}

    asyncio.run(Stub().vision_read_print_view())
    method, params = sent[0]
    assert method == "vision_read_print_view"
    assert params["required_sensitive_mask_binding"]["selector"] == "#pw"


class _PrintBody:
    execution_surface = "browser"
    device_id = "fake-browser-print-view"

    def __init__(self, *, supports_print: bool = True, navigate_after: int = 99, read_url: str = URL, read_token: str = "doc-before") -> None:
        self.read_url = read_url
        self.read_token = read_token
        self.captures = 0
        self.typed: list[tuple[str, str]] = []
        self.print_reads = 0
        self.navigate_after = navigate_after
        if supports_print:
            self.vision_read_print_view = self._read_print_view

    async def _read_print_view(self) -> dict[str, Any]:
        self.print_reads += 1
        return {
            "ok": True,
            "action": "read_print_view",
            "url": self.read_url,
            "document_lifecycle_token": self.read_token,
            "print_text": f"Ticket\n{PRINT_ONLY}\nKeep this slip.",
            "print_only_text": f"{PRINT_ONLY}\nKeep this slip.",
        }

    async def capture_screenshot(self) -> dict:
        self.captures += 1
        moved = self.captures > self.navigate_after
        lifecycle = "doc-after" if moved else "doc-before"
        return {
            "ok": True,
            "image": "jpeg",
            "width": 800,
            "height": 600,
            "url": "https://example.test/elsewhere" if moved else URL,
            "title": "Ticket",
            "scroll_y": 0,
            "scroll_height": 600,
            "document_lifecycle_token": lifecycle,
            "page_evidence": {
                "viewport_text": "Ticket. Print this page. Reference number",
                "document_lifecycle_token": lifecycle,
                "controls": [
                    {
                        "index": 1,
                        "tag": "input",
                        "type": "text",
                        "role": "textbox",
                        "text": "",
                        "label": "Reference number",
                        "context_text": "Reference number",
                        "value": "",
                        "selector": "#ref",
                        "x": 100,
                        "y": 120,
                        "width": 240,
                        "height": 40,
                    }
                ],
            },
        }

    async def vision_type_control(self, selector: str, text: str, **_binding: object) -> dict:
        self.typed.append((selector, text))
        return {"ok": True, "action": "type", "active_typable": True, "active_value": text, "active_value_matches_expected": True}


def _drive(monkeypatch: pytest.MonkeyPatch, body: _PrintBody, decisions: list[dict[str, Any]], *, max_steps: int = 4):
    init_db()
    with SessionLocal() as db:
        user = models.User(email=f"print-view-{uuid.uuid4().hex[:8]}@example.com", name="Print View")
        db.add(user)
        db.flush()
        run = models.OperatorRun(
            user_id=user.id,
            application_id=None,
            goal=GOAL,
            target_url=URL,
            current_state="RUNNING",
            confidence_state={"browser_task": {"goal": GOAL, "target_url": URL}},
        )
        db.add(run)
        db.commit()
        user_id, run_id = int(user.id), int(run.id)
    narrated: list[tuple[str, dict]] = []
    seen_contexts: list[dict[str, Any]] = []

    async def narrate(kind: str, data: dict) -> None:
        narrated.append((kind, data))

    monkeypatch.setattr("app.candidate_context.CandidateContextEngine", _DummyContextEngine)
    monkeypatch.setattr(extension_run, "_record_context_usage_for_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(extension_run, "_db_event", lambda run_id, event_type, payload: None)
    monkeypatch.setattr(
        extension_run,
        "_call_grounding_judge",
        lambda *args, **kwargs: {"grounded": False, "entry_index": -1, "status": "not_grounded", "reason": "no", "metadata": {}},
    )
    monkeypatch.setattr(
        extension_run,
        "_call_credential_target_classifier",
        lambda *args, **kwargs: {"slot": "none", "usage": "none", "confidence": 0.99, "reason": "ordinary", "evidence": "ordinary"},
    )
    monkeypatch.setattr(extension_run, "_call_user_only_question_judge", lambda *args, **kwargs: {"user_only": True})
    queue = iter(decisions)

    def decide(*args: Any, **kwargs: Any) -> dict[str, Any]:
        context = args[3] if len(args) > 3 else kwargs.get("context")
        seen_contexts.append(dict(context) if isinstance(context, dict) else {})
        return next(queue)

    monkeypatch.setattr(extension_run, "_decide_vision_action", decide)
    asyncio.run(run_vision_loop(body, url=None, narrate=narrate, run_id=run_id, user_id=user_id, skip_navigate=True, max_steps=max_steps))
    return narrated, seen_contexts


def _read() -> dict[str, Any]:
    return vision_agent.normalize_action({"action": "read_print_view", "reason": "The page says to print it."})


def _type() -> dict[str, Any]:
    return {
        "action": "type",
        "control": 1,
        "x": 220,
        "y": 140,
        "text": "48213",
        "question": "Reference number",
        "grounding_source": "page",
        "grounding_quote": PRINT_ONLY,
        "reason": "Enter the reference number the print view shows.",
    }


def test_the_run_loop_reads_the_print_view_and_cites_it_on_the_same_document(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _PrintBody()
    narrated, contexts = _drive(monkeypatch, body, [_read(), _type(), _type(), _type()])
    assert body.print_reads == 1
    receipts = [data for kind, data in narrated if kind == "receipt"]
    assert any(r.get("type") == "print_view_read" for r in receipts)
    shown = [c.get("print_view_text") for c in contexts if c.get("print_view_text")]
    assert shown and PRINT_ONLY in shown[0]["print_only_text"] and "Not a user fact" in shown[0]["note"]
    assert body.typed and body.typed[0] == ("#ref", "48213")
    # The text rides in the shown context only: never in the receipt lines.
    assert all("48213" not in str(r.get("verification") or "") for r in receipts if r.get("type") == "print_view_read")


def test_without_the_print_view_the_same_citation_is_not_grounded(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _PrintBody()
    _drive(monkeypatch, body, [_type(), _type(), _type(), _type()])
    assert body.typed == []


def test_the_print_view_is_dropped_once_the_page_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _PrintBody(navigate_after=1)
    _drive(monkeypatch, body, [_read(), _type(), _type(), _type()])
    assert body.print_reads == 1
    assert body.typed == []


def test_an_unsupported_body_gets_an_unsupported_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    body = _PrintBody(supports_print=False)
    narrated, _contexts = _drive(monkeypatch, body, [_read(), _type(), _type(), _type()], max_steps=2)
    text = str([data for kind, data in narrated if kind == "receipt"])
    assert "print_view_unsupported_on_this_body" in text
    assert body.typed == []


@pytest.mark.parametrize("read_url,read_token", [("", "doc-before"), (URL, ""), (URL, "doc-other")])
def test_a_read_without_a_matching_document_identity_is_not_stored(monkeypatch: pytest.MonkeyPatch, read_url: str, read_token: str) -> None:
    body = _PrintBody(read_url=read_url, read_token=read_token)
    _drive(monkeypatch, body, [_read(), _type(), _type(), _type()])
    assert body.print_reads == 1
    assert body.typed == []
