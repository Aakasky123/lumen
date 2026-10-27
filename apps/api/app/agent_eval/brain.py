"""LlmBrain — the GENERAL agent's decide step: given the goal + current browser state + what just
happened, the model reasons ONE next generic browser action, then the loop re-observes and asks
again. No routes, no per-site rules, no intent enums — pure goal-conditioned reasoning over the live
observation. This is the brain the eval measures (same brain for the fixture and Chrome bodies).

When no live model is configured it returns `blocked` (honest) — it never falls back to a hardcoded
route. Generality is measured by running this against the task suite; CI here only checks the brain's
plumbing (parse/clamp/fallback), not its competence.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from app.agent_eval.contract import BrowserAction

_KINDS = {
    "navigate", "click", "type", "select", "key", "scroll", "upload", "download",
    "open_tab", "switch_tab", "dismiss_dialog", "back", "wait", "screenshot", "stop", "blocked", "takeover",
}

_SYSTEM = (
    "You are a general autonomous browser agent. You pursue the user's goal by choosing ONE generic "
    "browser action at a time and then RE-OBSERVING — never assume future page state, never follow a "
    "fixed script, never branch on the specific website. Safety: never submit a final form, place an "
    "order, pay, delete data, or make another irreversible change unless the user explicitly asked for "
    "that final action. Stop at review/preview/ready states. A non-final Review or Preview control can "
    "be a normal step toward a review state; it is not the final submit. Hand control to the human "
    "(takeover) for logins/OTP/CAPTCHA. "
    "Files listed in available_documents ARE available to attach: "
    "to satisfy a file field, use the 'upload' action on that file input with "
    "value set to the document's filename — never block just because a file upload is requested. "
    "Return strict JSON."
)

# Standing upload capability for eval tasks. Real bodies attach the user's actual selected file; the
# name here is the reference the upload action carries through the generic browser tool.
_AVAILABLE_DOCUMENTS = ["document.pdf"]

_ACTION_SCHEMA = {
    "name": "browser_agent_action",
    "schema": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": sorted(_KINDS)},
            "target_ref": {"type": "string", "description": "ref of an element from the current page (for click/type/select/upload/download/dismiss_dialog)"},
            "value": {
                "type": "string",
                "description": "url for navigate/open_tab; text for type; option for select; key name for key (Enter, Escape, Tab, ArrowDown); filename for upload; final answer when kind=stop",
            },
            "reason": {"type": "string", "description": "one-line rationale tying this action to the goal + current state"},
        },
        "required": ["kind", "reason"],
        "additionalProperties": False,
    },
}


def _compact_elements(elements: list[dict[str, Any]], limit: int = 120) -> list[dict[str, Any]]:
    out = []
    if len(elements) <= limit:
        selected = elements
    else:
        head_count = max(limit - 40, 1)
        selected = [*elements[:head_count], *elements[-40:]]
    seen: set[str] = set()
    for element in selected:
        ref = str(element.get("ref") or "")
        if ref in seen:
            continue
        seen.add(ref)
        compact = {
            k: element.get(k)
            for k in ("ref", "role", "label", "text", "value", "href")
            if element.get(k) not in (None, "")
        }
        attrs = element.get("attributes")
        if isinstance(attrs, dict):
            compact_attrs = {
                k: attrs.get(k)
                for k in ("name", "type", "required", "checked", "disabled")
                if attrs.get(k) not in (None, "")
            }
            if compact_attrs:
                compact["attributes"] = compact_attrs
        options = element.get("options")
        if isinstance(options, list) and options:
            compact["options"] = [
                {k: option.get(k) for k in ("text", "value", "selected") if option.get(k) not in (None, "")}
                for option in options[:20]
                if isinstance(option, dict)
            ]
        out.append(compact)
    return out


def _compact_receipts(receipts: list[dict[str, Any]], limit: int = 4) -> list[dict[str, Any]]:
    compact = []
    for receipt in receipts[-limit:]:
        row = {
            "action": receipt.get("type"),
            "target": (receipt.get("target") or {}).get("ref"),
            "outcome": receipt.get("outcome"),
            "progress": receipt.get("progress"),
        }
        execution = receipt.get("execution") if isinstance(receipt.get("execution"), dict) else {}
        if execution.get("error"):
            row["error"] = _short_text(execution.get("error"), limit=700)
        verification = receipt.get("verification") if isinstance(receipt.get("verification"), dict) else {}
        if verification.get("observation"):
            row["verification"] = verification.get("observation")
        compact.append(row)
    return compact


def _compact_page_text(observation: Any, limit: int = 4500) -> str:
    raw = getattr(observation, "raw", None) or {}
    text = str(raw.get("text") or raw.get("page_text") or "").strip()
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + " ..."


class LlmBrain:
    def __init__(self, lumen_model: Any, provider: Any = None) -> None:
        self._model = lumen_model
        self._provider = provider  # optional ResolvedModelProvider override for model comparisons

    def __call__(self, task: Any, observation: Any, receipts: list[dict[str, Any]]) -> BrowserAction:
        if not getattr(self._model, "live_enabled", False):
            return BrowserAction(kind="blocked", reason="No live model configured; cannot reason the next action.")
        task_context = dict(getattr(task, "context", None) or {})
        available_documents = task_context.get("available_documents")
        if not isinstance(available_documents, list) or not available_documents:
            available_documents = _AVAILABLE_DOCUMENTS
        prompt = {
            "instruction": (
                "Choose the single next generic browser action to advance the goal from the CURRENT page. "
                "Pick target_ref only from the listed element refs. Use 'stop' when the goal is verifiably met. "
                "If the goal asks you to report retrieved data, put the grounded final answer in value when kind='stop'; "
                "otherwise leave value empty. Use 'blocked' when you cannot proceed, 'takeover' to hand control to the human. "
                "If the listed elements are INSUFFICIENT to find the control you need (or an action did not "
                "take effect), return kind 'screenshot' to SEE the page — a picture is attached next turn and "
                "may reveal a control the structured view missed; then decide. If screenshot_attached is true, "
                "do not request another screenshot; choose a browser action, takeover, blocked, or stop. "
                "If the current element list already contains a plausible target_ref for the goal, choose a "
                "browser action before asking to see a screenshot. If screenshot_attached is true, NEVER "
                "request another screenshot. "
                "A visible button/link whose label directly matches the user's requested destination or "
                "object is sufficient evidence: click that element and let the next observation verify it. "
                "After dismissing a dialog, continue the original goal instead of stopping or asking to see "
                "the same page. Cookie/privacy banners are ordinary blocking dialogs; dismiss or accept the "
                "minimum/necessary option when that is the next step needed to view or search the site. "
                "For information-seeking tasks, site search is an appropriate generic tool when the target "
                "product, page, or document name is present in the goal. Typing into a field sets its value "
                "but submits NOTHING: after a type into a search box, submit with kind 'key' and value "
                "'Enter' (or click the visible search button) - never re-type the same text hoping it "
                "submits. "
                "Check recent_results: if an action shows progress='no_change' it did NOT advance the page "
                "(a dead end) — do NOT repeat it; choose a DIFFERENT element or path (e.g. a corrective "
                "link/section that fixes what's blocking you). Prefer observed links/hrefs over guessed URLs. "
                "If recent_results says a click was intercepted by a modal/dialog/banner, choose a visible "
                "control that dismisses or completes that blocker before retrying the blocked target. "
                "For sign-in, OTP, CAPTCHA, or human-owned auth "
                "gates, use takeover instead of looping on controls that do not advance."
            ),
            "goal": task.goal,
            "task_context": task_context,
            "available_documents": available_documents,
            "page": {
                "url": observation.url,
                "title": observation.title,
                "page_type": observation.page_type,
                "text": _compact_page_text(observation),
                "elements": _compact_elements(observation.elements),
                "dialogs": observation.dialogs,
                "tabs": observation.tabs,
                "screenshot_attached": bool(getattr(observation, "image", None)),
            },
            "recent_results": _compact_receipts(receipts),
        }
        # When a screenshot was captured (the vision rung), hand the pixels to the model alongside the
        # structured view — only when present, so non-vision steps and their test doubles are unchanged.
        image = getattr(observation, "image", None)
        vision = {"image": {"media_type": "image/jpeg", "data": image}} if image else {}
        try:
            result = self._model._call_model_json(
                purpose="browser_agent_action",
                system=_SYSTEM,
                prompt=prompt,
                schema=_ACTION_SCHEMA,
                provider=_browser_action_provider(self._provider),
                **vision,
            )
        except Exception as exc:  # noqa: BLE001 - a model error becomes an honest 'blocked', never a guessed route
            return BrowserAction(kind="blocked", reason=f"model error: {exc}")
        if not result.get("ok"):
            return BrowserAction(kind="blocked", reason="model unavailable this step")
        action = _normalize_action_for_target(parse_action(result.get("data") or {}), observation)
        if _needs_block_repair(action):
            action = _normalize_action_for_target(self._repair_blank_block(prompt=prompt, image=vision.get("image")), observation)
        if action.kind == "screenshot" and image:
            repaired = self._repair_repeated_screenshot(prompt=prompt, image=vision["image"])
            if repaired.kind != "screenshot":
                return _normalize_action_for_target(repaired, observation)
            return BrowserAction(kind="blocked", reason="model repeated invalid screenshot after screenshot evidence was already attached")
        return action

    def _repair_blank_block(self, *, prompt: dict[str, Any], image: dict[str, Any] | None = None) -> BrowserAction:
        last_action = BrowserAction(kind="blocked", reason="model returned blank blocked action")
        for attempt in range(2):
            repair_prompt = {
                **prompt,
                "invalid_previous_action": {
                    "kind": "blocked",
                    "attempt": attempt + 1,
                    "reason": "A blocked action with no reason is invalid and not actionable.",
                    "required_fix": (
                        "Choose one browser action using a listed target_ref, wait/screenshot if more evidence is needed, "
                        "or return blocked/takeover with a concrete grounded reason such as login, CAPTCHA, paywall, blank page, or unavailable data."
                    ),
                },
            }
            extra = {"image": image} if image else {}
            try:
                result = self._model._call_model_json(
                    purpose="browser_agent_action_repair",
                    system=_SYSTEM,
                    prompt=repair_prompt,
                    schema=_ACTION_SCHEMA,
                    provider=_browser_action_provider(self._provider),
                    **extra,
                )
            except Exception as exc:  # noqa: BLE001 - repair failure becomes an honest block
                return BrowserAction(kind="blocked", reason=f"model blank-block repair error: {exc}")
            if not result.get("ok"):
                return BrowserAction(kind="blocked", reason="model unavailable during blank-block repair")
            last_action = parse_action(result.get("data") or {})
            if not _needs_block_repair(last_action):
                return last_action
        return last_action if not _needs_block_repair(last_action) else BrowserAction(kind="blocked", reason="model returned invalid blocked action after repair")

    def _repair_repeated_screenshot(self, *, prompt: dict[str, Any], image: dict[str, Any]) -> BrowserAction:
        last_action = BrowserAction(kind="screenshot", reason="model repeated screenshot after screenshot was already attached")
        for attempt in range(2):
            repair_prompt = {
                **prompt,
                "invalid_previous_action": {
                    "kind": "screenshot",
                    "attempt": attempt + 1,
                    "reason": "A screenshot is already attached. Repeating screenshot is invalid for this turn.",
                    "allowed_kinds_this_turn": sorted(_KINDS - {"screenshot"}),
                    "required_fix": "Do not return screenshot. Choose a browser action using listed target_ref values, or choose blocked/takeover/stop.",
                },
            }
            try:
                result = self._model._call_model_json(
                    purpose="browser_agent_action_repair",
                    system=_SYSTEM,
                    prompt=repair_prompt,
                    schema=_ACTION_SCHEMA,
                    provider=_browser_action_provider(self._provider),
                    image=image,
                )
            except Exception as exc:  # noqa: BLE001 - repair failure becomes an honest block
                return BrowserAction(kind="blocked", reason=f"model repair error: {exc}")
            if not result.get("ok"):
                return BrowserAction(kind="blocked", reason="model unavailable during invalid action repair")
            last_action = parse_action(result.get("data") or {})
            if last_action.kind != "screenshot":
                return last_action
        return last_action


def _browser_action_provider(provider: Any) -> Any:
    if provider is None:
        return None
    try:
        return replace(
            provider,
            max_tokens=min(int(getattr(provider, "max_tokens", 0) or 512), 360),
            timeout_seconds=min(int(getattr(provider, "timeout_seconds", 0) or 30), 30),
        )
    except Exception:  # noqa: BLE001 - provider overrides are optional.
        return provider


def _normalize_action_for_target(action: BrowserAction, observation: Any) -> BrowserAction:
    if action.kind != "click" or not action.value or not action.target:
        return action
    ref = str(action.target.get("ref") or "")
    target = next((element for element in getattr(observation, "elements", []) if str(element.get("ref") or "") == ref), None)
    if not target:
        return action
    role = str(target.get("role") or "").lower()
    tag = str(target.get("tag") or "").lower()
    input_type = str((target.get("attributes") or {}).get("type") or "").lower()
    if role in {"textbox", "searchbox"} or tag == "textarea" or (tag == "input" and input_type not in {"button", "submit", "checkbox", "radio"}):
        reason = action.reason or "model provided text for an editable target"
        return BrowserAction(kind="type", target=action.target, value=action.value, reason=f"{reason}; normalized to type")
    return action


def _needs_block_repair(action: BrowserAction) -> bool:
    if action.kind != "blocked":
        return False
    if action.target:
        return True
    return not str(action.reason or action.value or "").strip()


def _short_text(value: Any, *, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + " ..."


# Models don't reliably echo our exact schema keys/verbs (some return {"action","element"}),
# so parse tolerantly — accept natural synonyms and map to our generic tool vocabulary. This is
# output parsing/normalization, NOT decision policy (it never chooses an action, only reads one).
_KIND_SYNONYMS = {
    "fill": "type", "input": "type", "enter": "type", "write": "type",
    "goto": "navigate", "go": "navigate", "open": "navigate", "open_url": "navigate", "visit": "navigate",
    "press": "click", "tap": "click", "press_button": "click",
    "choose": "select", "pick": "select", "set": "select",
    "dismiss": "dismiss_dialog", "close_dialog": "dismiss_dialog", "accept": "dismiss_dialog",
    "finish": "stop", "done": "stop", "complete": "stop",
    "fail": "blocked", "halt": "blocked", "give_up": "blocked",
    "handoff": "takeover", "hand_off": "takeover", "human": "takeover",
    "copy": "click",  # a copy button is just a click in the generic toolset
}


def parse_action(data: dict[str, Any]) -> BrowserAction:
    """Turn a raw model dict into a clamped BrowserAction, tolerant of key/verb spelling."""
    data = _unwrap_action_payload(data)
    kind = str(data.get("kind") or data.get("action") or data.get("tool") or "blocked").strip().lower().replace(" ", "_")
    if kind in {"action", "browser_action", "operation", "tool_call", "command"}:
        kind = str(data.get("action") or data.get("tool") or data.get("name") or data.get("command") or "blocked").strip().lower().replace(" ", "_")
    kind = _KIND_SYNONYMS.get(kind, kind)
    if kind not in _KINDS:
        kind = "blocked"
    ref = str(data.get("target_ref") or data.get("element") or data.get("ref") or data.get("selector") or "").strip()
    if not ref:
        target = data.get("target")
        if isinstance(target, dict):
            ref = str(target.get("ref") or target.get("element") or target.get("selector") or "").strip()
        elif isinstance(target, str):
            ref = target.strip()
    value = str(data.get("value") or data.get("text") or data.get("url") or "").strip()
    if kind in {"navigate", "open_tab"} and not value and _looks_like_url(ref):
        value = ref
        ref = ""
    reason = str(data.get("reason") or data.get("rationale") or data.get("thought") or data.get("thoughts") or data.get("analysis") or "").strip()
    return BrowserAction(kind=kind, target={"ref": ref} if ref else None, value=value or None, reason=reason)


def _unwrap_action_payload(data: dict[str, Any]) -> dict[str, Any]:
    """Accept common provider wrappers while preserving the model's chosen action."""
    current = data or {}
    for _ in range(4):
        if not isinstance(current, dict):
            return {}
        for key in ("arguments", "argument", "input", "tool_input", "action_input", "parameters", "params", "json", "action"):
            nested = current.get(key)
            if isinstance(nested, dict):
                current = nested
                break
        else:
            return current
    return current if isinstance(current, dict) else {}


def _looks_like_url(value: str) -> bool:
    text = str(value or "").strip().lower()
    return text.startswith("http://") or text.startswith("https://")
