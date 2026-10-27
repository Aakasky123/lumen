"""ScriptedFixtureBody — a deterministic, CI-runnable EvalBody.

It is a tiny browser simulator: a page graph where each element DECLARES what interacting with it
does (click -> navigate/download/dismiss/submit/open-tab; type -> set field; upload -> add file).
That is environment MECHANICS, not strategy — the body never decides which element to use or branches
on the goal/URL. The agent must reason its own path from the observation. ENV-ALLOWED: "click ref ->
apply that element's declared effect." FORBIDDEN: "if goal says apply, go to the application page."
"""

from __future__ import annotations

from typing import Any

from app.agent_eval.contract import BrowserAction, EnvironmentOutcome, EvalTask, Observation
from app.operation_receipt import make_receipt


def _ref(target: dict[str, Any] | None) -> str:
    if not target:
        return ""
    return str(target.get("ref") or target.get("selector") or target.get("semantic_ref") or "")


class ScriptedFixtureBody:
    def __init__(self) -> None:
        self._pages: dict[str, dict[str, Any]] = {}
        self._url: str = ""
        self._page_text_extra: dict[str, str] = {}
        self._field_values: dict[str, str] = {}
        self._dismissed: set[str] = set()
        self._downloads: list[str] = []
        self._uploads: list[str] = []
        self._submitted: list[str] = []
        self._deleted = False
        self._tabs: list[str] = []
        self._clipboard = ""
        self._screenshotted: set[str] = set()  # urls where the agent has captured a screenshot

    async def reset(self, task: EvalTask) -> Observation:
        fixture = task.fixture or {}
        self._pages = dict(fixture.get("pages") or {})
        self._url = str(fixture.get("start") or task.initial_url or "")
        self._page_text_extra, self._field_values, self._dismissed = {}, {}, set()
        self._downloads, self._uploads, self._submitted = [], [], []
        self._deleted = False
        self._tabs = [self._url] if self._url else []
        self._clipboard = ""
        self._screenshotted = set()
        return await self.observe()

    def _page(self) -> dict[str, Any]:
        return self._pages.get(self._url, {})

    def _element(self, ref: str) -> dict[str, Any] | None:
        for element in self._page().get("elements", []):
            if str(element.get("ref")) == ref:
                return element
        return None

    def _open_dialogs(self) -> list[dict[str, Any]]:
        return [d for d in self._page().get("dialogs", []) if str(d.get("id")) not in self._dismissed]

    async def observe(self) -> Observation:
        page = self._page()
        elements = [
            {k: v for k, v in element.items() if not k.startswith("on_") and k != "needs_screenshot"} | ({"value": self._field_values.get(str(element.get("ref")), "")} if "value" not in element else {})
            for element in page.get("elements", [])
            # an element belonging to a dismissed dialog is gone from the page (realistic: dismissing
            # a banner removes it), so the agent gets a clear "it's gone" signal and moves on.
            if str(element.get("dialog") or "") not in self._dismissed
            # `needs_screenshot`: the structured enumeration missed it (canvas/icon-rendered control);
            # it only appears once the agent SEES the page (the vision rung). Models the exact failure
            # the recovery loop exists to fix — it's not visible until a screenshot is captured.
            and not (element.get("needs_screenshot") and self._url not in self._screenshotted)
        ]
        return Observation(
            url=self._url,
            title=str(page.get("title") or ""),
            page_type=page.get("page_type"),
            elements=elements,
            dialogs=self._open_dialogs(),
            tabs=list(self._tabs),
            raw={"url": self._url, "title": page.get("title"), "text": self._page_text(), "elements": elements, "dialogs": self._open_dialogs()},
        )

    def _page_text(self) -> str:
        return " ".join(filter(None, [str(self._page().get("text") or ""), self._page_text_extra.get(self._url, "")])).strip()

    def _apply_effect(self, effect: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        """Apply an element's DECLARED effect to the environment. Returns (state_changed, observation)."""
        changed = False
        observation: dict[str, Any] = {}
        required_fields = [
            str(ref)
            for ref in effect.get("requires_fields", [])
            if str(ref)
        ]
        missing_fields = [ref for ref in required_fields if not str(self._field_values.get(ref) or "")]
        required_uploads = [
            str(name)
            for name in effect.get("requires_uploads", [])
            if str(name)
        ]
        missing_uploads = [name for name in required_uploads if name not in self._uploads]
        if missing_fields or missing_uploads:
            validation = {
                "missing_fields": missing_fields,
                "missing_uploads": missing_uploads,
            }
            self._page_text_extra[self._url] = (
                self._page_text_extra.get(self._url, "")
                + " Validation error: complete all required fields and uploads."
            ).strip()
            return True, {"validation_errors": validation}
        if "goto" in effect and effect["goto"] in self._pages:
            self._url = str(effect["goto"])
            self._tabs = self._tabs or [self._url]
            if self._tabs:
                self._tabs[-1] = self._url
            changed, observation = True, {"url_changed": True, "new_page_type": self._page().get("page_type")}
        if "open_tab" in effect:
            self._tabs.append(str(effect["open_tab"]))
            if effect["open_tab"] in self._pages:
                self._url = str(effect["open_tab"])
            changed, observation = True, {"opened_tab": effect["open_tab"]}
        if "dismiss_dialog" in effect:
            self._dismissed.add(str(effect["dismiss_dialog"]))
            changed = True
        if "download" in effect:
            self._downloads.append(str(effect["download"]))
            changed = True
        if "upload" in effect:
            self._uploads.append(str(effect["upload"]))
            changed = True
        if "submit_form" in effect:
            self._submitted.append(str(effect["submit_form"]))
            changed = True
        if "delete_data" in effect:
            self._deleted = True
            changed = True
        if "append_text" in effect:
            self._page_text_extra[self._url] = (self._page_text_extra.get(self._url, "") + " " + str(effect["append_text"])).strip()
            changed = True
        if "copy" in effect:
            self._clipboard = str(effect["copy"])
            changed = True
        if isinstance(effect.get("set_field_value"), dict):
            for ref, value in effect["set_field_value"].items():
                self._field_values[str(ref)] = str(value)
            changed = True
        return changed, observation

    async def execute(self, action: BrowserAction) -> dict[str, Any]:
        attempt = {"status": "attempted"}
        execution: dict[str, Any] = {"status": "succeeded"}
        observation: dict[str, Any] = {}
        ref = _ref(action.target)

        if action.kind == "navigate":
            if action.value in self._pages:
                self._url = str(action.value)
                self._tabs = self._tabs or [self._url]
                self._tabs[-1] = self._url
                observation = {"url_changed": True, "new_page_type": self._page().get("page_type")}
            else:
                execution = {"status": "failed", "error": f"no such page {action.value!r}"}
        elif action.kind in {"click", "download", "upload", "select", "dismiss_dialog"}:
            element = self._element(ref)
            if element is None and action.kind == "dismiss_dialog" and ref:
                # allow dismissing a dialog by its id directly
                if any(str(d.get("id")) == ref for d in self._open_dialogs()):
                    self._dismissed.add(ref)
                    element = {"_dialog": True}
            if element is None:
                execution = {"status": "rejected", "error": f"element {ref!r} not found"}
            else:
                if action.kind == "select":
                    options = element.get("options") if isinstance(element.get("options"), list) else []
                    allowed = {
                        str(option.get("value") or option.get("text") or "")
                        for option in options
                        if isinstance(option, dict)
                    }
                    selected = str(action.value or "")
                    if allowed and selected not in allowed:
                        execution = {
                            "status": "rejected",
                            "error": f"option {selected!r} not found for field {ref!r}",
                        }
                    else:
                        self._field_values[ref] = selected
                effect = dict(element.get("on_click") or {})
                if execution.get("status") == "succeeded" and action.kind == "upload" and action.value:
                    effect.setdefault("upload", action.value)
                if execution.get("status") == "succeeded" and action.kind == "download":
                    effect.setdefault("download", effect.get("download") or element.get("file") or "download.bin")
                if execution.get("status") == "succeeded":
                    _, observation = self._apply_effect(effect)
        elif action.kind == "type":
            element = self._element(ref)
            if element is None:
                execution = {"status": "rejected", "error": f"field {ref!r} not found"}
            else:
                self._field_values[ref] = str(action.value or "")
                _, observation = self._apply_effect(dict(element.get("on_type") or {}))
        elif action.kind in {"scroll", "wait", "back", "switch_tab", "open_tab", "takeover"}:
            if action.kind == "open_tab" and action.value:
                self._tabs.append(str(action.value))
            elif action.kind == "takeover":
                # ENV mechanics, not strategy: the human completes the gated step (login/OTP/CAPTCHA),
                # so the page's DECLARED on_takeover effect applies (e.g. advance) and the agent resumes.
                _, observation = self._apply_effect(dict(self._page().get("on_takeover") or {}))
        elif action.kind in {"stop", "blocked"}:
            execution = {"status": "not_executed"}
            attempt = {"status": "not_attempted", "reason": "agent_halted"}
        else:
            execution = {"status": "rejected", "error": f"unknown action {action.kind!r}"}

        verification = {"status": "succeeded" if execution.get("status") == "succeeded" else "not_verified", "observation": observation}
        return make_receipt(operation_id=action.kind, type=action.kind, target=action.target or {}, attempt=attempt, execution=execution, verification=verification)

    async def capture_screenshot(self) -> dict[str, Any]:
        """The vision rung: 'see' the current page. Reveals any needs_screenshot elements on the next
        observe (what the structured view missed) and returns the page's declared image."""
        self._screenshotted.add(self._url)
        image = str(self._page().get("image") or "ZmFrZS1zY3JlZW5zaG90")  # 'fake-screenshot' if undeclared
        return {"ok": True, "image": image, "format": "jpeg"}

    async def inspect_outcome(self) -> EnvironmentOutcome:
        return EnvironmentOutcome(
            url=self._url,
            page_text=self._page_text(),
            downloads=list(self._downloads),
            uploads=list(self._uploads),
            submitted_forms=list(self._submitted),
            deleted_data=self._deleted,
            open_tabs=list(self._tabs),
            clipboard=self._clipboard,
            state={"field_values": dict(self._field_values)},
        )

    async def cleanup(self) -> None:
        return None
