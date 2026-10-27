"""ChromeExtensionBody — the EvalBody that drives the REAL in-browser body (the extension) in real
Chrome. Same interface as ScriptedFixtureBody, so the agent loop is identical; this is the
authoritative test of whether Lumen is becoming a general browser agent (real timing, focus, tabs,
downloads, redirects, dialogs, dynamic DOM).

NOT CI-gated: it needs a live extension connection. Run it against CONTROLLED LOCAL pages first
(local dynamic site / fake login / redirect / popup / download server / upload form / tab task /
error page / job simulator), not the live web.

Honest limit: the current extension body is field-completion-centric (observe + fill/select/click/
upload + navigate). Generic actions it doesn't support yet (dismiss_dialog, download, open_tab,
scroll) are reported as rejected receipts rather than faked — exactly the gap the eval should expose
and the next work (brain-reasoned generic tools) should close.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from app.agent_eval.contract import BrowserAction, EnvironmentOutcome, EvalTask, Observation
from app.operation_receipt import make_receipt

# BrowserAction.kind -> the extension body's intent action (what it can do today).
_SUPPORTED = {"navigate", "click", "type", "select", "key", "upload"}
_INTENT = {"type": "fill"}

# Routine Chrome races on redirect-heavy live sites: the page tears down its frame while
# a command is in flight. Transient page state, not a task failure.
_TRANSIENT_PAGE_ERROR = re.compile(
    r"frame with id \d+ was removed|frame was removed|document was detached|"
    r"cannot access contents of|page is navigating|target closed",
    re.IGNORECASE,
)


def connected_chrome_eval_body() -> "ChromeExtensionBody":
    """The real-Chrome eval body, bound to the most recently connected extension device.

    This is the authoritative benchmark surface: the production body, real timing, real
    focus, the user's real Chrome. Raises with a plain remedy when nothing is connected.
    """
    from app.extension_body import ExtensionBody
    from app.extension_hub import hub

    connection = hub.get(None)
    if not connection:
        raise RuntimeError(
            "No extension connected. Open Chrome with the Lumen extension while the API "
            "is running, then retry the benchmark."
        )
    return ChromeExtensionBody(ExtensionBody(connection))


def resolve_eval_body(
    body_kind: str,
    *,
    headless: bool = True,
    har_path: Any = None,
    extra_http_headers: dict[str, str] | None = None,
) -> Any:
    """Benchmark body factory. 'chrome' (default) runs the production body; 'playwright'
    remains an explicit opt-in that raises its own disabled error rather than silently
    substituting a second runtime.

    extra_http_headers apply only to the playwright kind: real Chrome sends the user's
    real session, which is the point of benchmarking on the production body."""
    kind = str(body_kind or "chrome").strip().lower()
    if kind == "chrome":
        return connected_chrome_eval_body()
    if kind == "playwright":
        from app.agent_eval.playwright_body import PlaywrightEvalBody

        return PlaywrightEvalBody(
            headless=headless, har_path=har_path, extra_http_headers=extra_http_headers
        )
    raise ValueError(f"Unknown eval body kind {body_kind!r}; expected 'chrome' or 'playwright'.")


def _eval_element(node: dict[str, Any]) -> dict[str, Any]:
    """Project one production observation node into the flat eval element contract.

    The production observer emits nested refs (element_ref.selector etc.); the eval brain
    reads flat ref/role/label keys. Without this projection the model saw elements with
    no refs at all and improvised targets from visible labels, which then failed to
    resolve. Deterministic flattening of enumerated evidence - no interpretation."""
    element_ref = node.get("element_ref") if isinstance(node.get("element_ref"), dict) else {}
    field_ref = node.get("field_ref") if isinstance(node.get("field_ref"), dict) else {}
    locator_ref = node.get("locator_ref") if isinstance(node.get("locator_ref"), dict) else {}
    ref = str(
        element_ref.get("selector") or field_ref.get("selector") or locator_ref.get("selector") or ""
    ).strip()
    flat: dict[str, Any] = {
        "ref": ref,
        "role": str(node.get("role") or node.get("control_kind") or element_ref.get("role") or ""),
        "label": str(node.get("resolved_label") or node.get("label") or element_ref.get("label") or ""),
    }
    value = node.get("value")
    if value not in (None, ""):
        flat["value"] = value
    for key in ("checked", "disabled", "required", "readonly"):
        if node.get(key) is True:
            flat[key] = True
    if node.get("placeholder"):
        flat["placeholder"] = node.get("placeholder")
    if node.get("section_heading"):
        flat["section"] = node.get("section_heading")
    options = node.get("options")
    if isinstance(options, list) and options:
        flat["options"] = options[:20]
    return flat


class ChromeExtensionBody:
    def __init__(self, body: Any) -> None:
        # `body` is an app.extension_body.ExtensionBody bound to a connected device.
        self._body = body
        self._last: dict[str, Any] = {}
        self._elements: list[dict[str, Any]] = []

    async def reset(self, task: EvalTask) -> Observation:
        if task.initial_url:
            await self._body.navigate(task.initial_url)
        return await self.observe()

    async def observe(self) -> Observation:
        # Observation is read-only, so a frame torn down mid-observe (redirects, reloads)
        # is retried once after the page settles instead of killing the whole task.
        try:
            raw = await self._body.observe() or {}
        except Exception as exc:  # noqa: BLE001 - only the transient class is retried
            # Timeouts on heavy DOMs (post-search storefronts, YouTube) and frame
            # teardowns are both transient page states: one settled retry each.
            transient = isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or _TRANSIENT_PAGE_ERROR.search(str(exc))
            if not transient:
                raise RuntimeError(f"observe failed ({type(exc).__name__}): {exc}") from exc
            await asyncio.sleep(1.5)
            raw = await self._body.observe() or {}
        self._last = raw
        nodes = raw.get("nodes") if isinstance(raw.get("nodes"), list) else raw.get("elements") or []
        self._elements = [
            _eval_element(node) for node in nodes if isinstance(node, dict)
        ]
        return Observation(
            url=str(raw.get("url") or ""),
            title=str(raw.get("title") or ""),
            page_type=raw.get("page_type"),
            elements=list(self._elements),
            dialogs=list(raw.get("dialogs") or []),
            tabs=list(raw.get("tabs") or []),
            raw=raw,
        )

    def _bind_target(self, target: dict[str, Any] | None) -> tuple[str, str]:
        """Resolve the brain's target to an exact enumerated selector, or say why not.

        Binding is deterministic exact-match against the CURRENT observation only:
        1. the cited ref/selector IS an enumerated element's ref;
        2. else it exactly equals (case/whitespace-insensitive) the label of exactly
           one enumerated element - the model cited what it could see;
        ambiguity or absence fails honestly instead of clicking a guess."""
        raw = str((target or {}).get("selector") or (target or {}).get("ref") or "").strip()
        if not raw:
            return "", "missing_target_ref"
        refs = {element.get("ref") for element in self._elements if element.get("ref")}
        if raw in refs:
            return raw, ""
        normalized = " ".join(raw.split()).casefold()
        matches = [
            element
            for element in self._elements
            if element.get("ref")
            and " ".join(str(element.get("label") or "").split()).casefold() == normalized
        ]
        if len(matches) == 1:
            return str(matches[0]["ref"]), ""
        if len(matches) > 1:
            return "", "ambiguous_target_label"
        return "", "target_not_in_enumerated_elements"

    async def execute(self, action: BrowserAction) -> dict[str, Any]:
        attempt = {"status": "attempted"}
        if action.kind not in _SUPPORTED:
            # Truthful: the real body can't do this generic action yet — do NOT fake it.
            return make_receipt(
                operation_id=action.kind, type=action.kind, target=action.target or {},
                attempt={"status": "not_attempted", "reason": "unsupported_by_body", "detail": action.kind},
                execution={"status": "not_executed"}, verification={"status": "not_verified"},
            )
        before_url = str(self._last.get("url") or "")
        try:
            if action.kind == "navigate":
                result = await self._body.navigate(action.value or "")
            elif action.kind == "key":
                # A key press acts on the currently focused control (typically the field
                # the previous type filled), via the production vision_key machinery -
                # typing sets a value but submits nothing, Enter is how a human submits.
                result = await self._body.vision_key(str(action.value or "Enter").strip() or "Enter")
            else:
                selector, bind_failure = self._bind_target(action.target)
                if bind_failure:
                    return make_receipt(
                        operation_id=action.kind,
                        type=action.kind,
                        target=action.target or {},
                        attempt={"status": "not_attempted", "reason": bind_failure},
                        execution={
                            "status": "rejected",
                            "error": (
                                f"{bind_failure}: the target could not be bound to an enumerated "
                                "element of the CURRENT observation. Cite target_ref exactly as "
                                "listed in elements[].ref."
                            ),
                        },
                        verification={"status": "not_verified"},
                    )
                intent = {"action": _INTENT.get(action.kind, action.kind), "selector": selector, "value": action.value}
                result = await self._body.execute(intent)
        except Exception as exc:  # noqa: BLE001 - only the transient class is absorbed
            if not _TRANSIENT_PAGE_ERROR.search(str(exc)):
                raise RuntimeError(
                    f"{action.kind} execute failed ({type(exc).__name__}): {exc}"
                ) from exc
            # The frame tore down while the action was in flight. Never blind-retry an
            # action (a click that half-applied could double-activate); report honestly
            # and let the loop re-observe the settled page and re-decide.
            await asyncio.sleep(1.5)
            return make_receipt(
                operation_id=action.kind,
                type=action.kind,
                target=action.target or {},
                attempt={"status": "attempted"},
                execution={
                    "status": "failed",
                    "error": (
                        f"transient_page_teardown: {exc}. The page navigated or reloaded while "
                        "the action was in flight. Re-observe the settled page; if the intended "
                        "effect already happened, continue from it, otherwise act again."
                    ),
                },
                verification={"status": "not_verified"},
            )
        ok = bool((result or {}).get("ok"))
        # The action itself is already done; a failing post-action read-back must degrade
        # the receipt's verification, never kill the task (the loop re-observes next).
        observe_error = ""
        try:
            after = await self._body.observe() or {}
        except Exception as exc:  # noqa: BLE001 - verification degrades, execution stands
            observe_error = f"{type(exc).__name__}: {exc}"
            after = {}
        if after:
            self._last = after
            observation = {"url_changed": str(after.get("url") or "") != before_url, "new_page_type": after.get("page_type")}
        else:
            observation = {"url_changed": False, "post_action_observe_error": observe_error or "empty observation"}
        execution = {"status": "succeeded"} if ok else {"status": "failed", "error": str((result or {}).get("error") or "execute failed")}
        verification = {"status": "succeeded" if ok else "not_verified", "observation": observation}
        return make_receipt(operation_id=action.kind, type=action.kind, target=action.target or {}, attempt=attempt, execution=execution, verification=verification)

    async def capture_screenshot(self) -> dict[str, Any]:
        """The vision rung against real Chrome — delegates to the extension body's screenshot command."""
        cap = getattr(self._body, "capture_screenshot", None)
        if not callable(cap):
            return {"ok": False, "error": "screenshot unsupported by body"}
        return await cap() or {"ok": False}

    async def inspect_outcome(self) -> EnvironmentOutcome:
        raw = self._last or (await self._body.observe() or {})
        # Best-effort ground truth from the live page. Downloads/uploads/submission would come from
        # the extension surfacing chrome.downloads + form-submit signals (a follow-up so these tasks
        # are checkable on real Chrome).
        return EnvironmentOutcome(
            url=str(raw.get("url") or ""),
            page_text=str(raw.get("text") or raw.get("title") or ""),
            downloads=list(raw.get("downloads") or []),
            uploads=list(raw.get("uploads") or []),
            submitted_forms=list(raw.get("submitted_forms") or []),
            open_tabs=list(raw.get("tabs") or []),
        )

    async def cleanup(self) -> None:
        return None
