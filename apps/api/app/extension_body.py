"""The in-browser body interface, server-side.

A thin wrapper the brain's run loop calls just like it calls the Playwright adapter —
observe() / navigate() / execute(intent) — but the work happens in the user's real Chrome via
the extension channel. This is the seam that lets the existing cognition core drive either
body (Playwright or extension) without caring which.
"""

from __future__ import annotations

from typing import Any, Callable

from app.control_ids import ControlIdRegistry
from app.extension_hub import ExtensionConnection, ExtensionTransportError, hub


def _sensitive_selectors(value: Any) -> list[str]:
    raw = value if isinstance(value, (list, tuple)) else [value]
    selectors: list[str] = []
    for item in raw:
        selector = str(item or "").strip()
        if not selector or selector in selectors:
            continue
        selectors.append(selector)
        if len(selectors) >= 24:
            break
    return selectors


def _result_invalidates_sensitive_binding(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    if (
        result.get("url_changed")
        or result.get("document_lifecycle_changed")
        or result.get("adopted_new_tab")
        or result.get("sensitive_mask_rebind_required")
    ):
        return True
    mismatch_field = str(
        result.get("page_context_mismatch_field")
        or result.get("mismatch_field")
        or ""
    ).strip()
    failure_reason = str(result.get("failure_reason") or "").strip()
    return bool(
        mismatch_field in {"url", "document_lifecycle_token"}
        and (
            "page_context_drift" in failure_reason
            or failure_reason.startswith("credential_replay_")
        )
    )


class ExtensionBody:
    def __init__(
        self,
        connection: ExtensionConnection,
        *,
        connection_lookup: Callable[[str], ExtensionConnection | None] | None = None,
    ) -> None:
        self._connection = connection
        self._connection_lookup = connection_lookup or hub.get
        self._required_sensitive_screenshot_binding: dict[str, Any] | None = None
        self._control_indicator_active = False
        self._control_indicator_run_id: int | None = None
        self._control_ids = ControlIdRegistry()

    @property
    def device_id(self) -> str:
        return self._connection.device_id

    async def _call(self, method: str, params: dict[str, Any] | None = None, *, timeout: float) -> Any:
        # A service-worker reload replaces the WebSocket object but keeps the stable device id.
        # Resolve that id before every primitive so the next observe/replan turn can continue on the
        # replacement channel instead of holding a permanently stale connection object.
        previous = self._connection
        replacement = self._connection_lookup(self.device_id)
        if replacement is not None:
            self._connection = replacement
        if (
            replacement is not None
            and replacement is not previous
            and self._control_indicator_active
            and method != "set_control_indicator"
        ):
            await self._connection.call(
                "set_control_indicator",
                {
                    "active": True,
                    "message": "Lumen is using your computer",
                    "run_id": self._control_indicator_run_id,
                },
                timeout=8.0,
            )
        result = await self._connection.call(method, params, timeout=timeout)
        if (
            self._required_sensitive_screenshot_binding is not None
            and _result_invalidates_sensitive_binding(result)
        ):
            # The extension clears its transient binding whenever the exact document changes or a
            # guarded command says rebinding is required. Mirror that state here; otherwise every
            # later command would resend a stale selector/page identity and could never re-observe.
            self._required_sensitive_screenshot_binding = None
        return result

    async def set_control_indicator(
        self,
        active: bool,
        *,
        run_id: int | None = None,
        message: str = "Lumen is using your computer",
        timeout: float = 8.0,
    ) -> dict[str, Any]:
        """Show or hide the non-interactive safety indicator on the owned task tab."""
        self._control_indicator_active = bool(active)
        self._control_indicator_run_id = int(run_id) if active and run_id is not None else None
        result = await self._call(
            "set_control_indicator",
            {
                "active": bool(active),
                "message": str(message or "Lumen is using your computer")[:120],
                "run_id": self._control_indicator_run_id,
            },
            timeout=timeout,
        )
        return dict(result) if isinstance(result, dict) else {"ok": False}

    def _params_with_sensitive_mask_binding(
        self,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        guarded = dict(params)
        binding = self._required_sensitive_screenshot_binding
        if binding is not None:
            required_binding: dict[str, Any] = {
                "selector": str(binding.get("selector") or ""),
                "expected_page_context": dict(
                    binding.get("expected_page_context") or {}
                ),
            }
            selectors = _sensitive_selectors(binding.get("selectors"))
            if len(selectors) > 1:
                required_binding["selectors"] = selectors
            guarded["required_sensitive_mask_binding"] = required_binding
        return guarded

    async def navigate(self, url: str, *, timeout: float = 45.0) -> dict[str, Any]:
        # A new browser task gets its own owned tab. This keeps the Lumen cockpit alive and gives
        # later observe/act calls an explicit target instead of whichever user tab is active.
        result = await self._call("navigate", {"url": url, "new_tab": True}, timeout=timeout)
        if isinstance(result, dict) and result.get("ok"):
            self._required_sensitive_screenshot_binding = None
        return result

    async def observe(self, *, timeout: float = 30.0) -> dict[str, Any]:
        """Return the extension's raw page observation (url, title, text, enumerated nodes)."""
        if self._required_sensitive_screenshot_binding is not None:
            # The structured observer cannot apply a model-owned semantic selector without
            # expanding deterministic perception. The vision screenshot command is the safe
            # post-takeover observation path because it atomically rebinds and redacts in-page.
            return {
                "ok": False,
                "action": "observe",
                "failure_reason": "sensitive_semantic_observe_unavailable",
                "observation_discarded": True,
            }
        return await self._call("observe", timeout=timeout)

    async def execute(self, intent: dict[str, Any], *, timeout: float = 30.0) -> dict[str, Any]:
        """Run one typed intent (fill/select/click/upload) and return the post-state."""
        if self._required_sensitive_screenshot_binding is not None:
            return {
                "ok": False,
                "action": str((intent or {}).get("action") or "execute"),
                "failure_reason": "sensitive_semantic_execute_unavailable",
                "browser_mutation_attempted": False,
            }
        return await self._call("execute", {"intent": intent}, timeout=timeout)

    async def read_run_tab_text(self, *, max_chars: int = 6000, timeout: float = 8.0) -> dict[str, Any]:
        """The run's own tab's whole rendered text ({ok, url, title, text, truncated}), read in place.

        Read-only: the tab is not activated or focused and nothing is captured, so a run working on the
        real screen is undisturbed. The extension redacts it in-page by the rules the screenshot's
        evidence uses; a required sensitive-mask binding rides along so its controls stay covered.
        """
        params = self._params_with_sensitive_mask_binding({"max_chars": int(max_chars)})
        result = await self._call("read_run_tab_text", params, timeout=timeout)
        return dict(result) if isinstance(result, dict) else {"ok": False, "action": "read_run_tab_text"}

    async def close_bench_tabs(self, *, timeout: float = 10.0) -> dict[str, Any]:
        """Close every tab showing a loopback Interaction Gauntlet page (/eval/bench/), and with it any
        popup window: evaluation hygiene between pages. Nothing else the user has open matches."""
        return await self._call("close_bench_tabs", {}, timeout=timeout)

    async def reload_extension(self, *, timeout: float = 5.0) -> dict[str, Any]:
        """Ask the unpacked extension to reload itself (picks up edited source); it reconnects with
        the same device id, so the next primitive resolves the replacement channel."""
        return await self._call("reload_extension", {}, timeout=timeout)

    async def capture_screenshot(
        self,
        *,
        full_page: bool = False,
        lossless: bool = False,
        timeout: float = 24.0,
    ) -> dict[str, Any]:
        """Capture the visible viewport as a base64 JPEG + page state (width/height/dpr/url/scroll) so the
        vision loop can map the model's pixel coordinates back to the page. The image is RAW EVIDENCE for
        the model — deterministic code never parses it."""
        request: dict[str, Any] = {"full_page": full_page}
        if lossless:
            # The same masked atomic capture as PNG, for a zoom that must not inherit JPEG loss.
            request["lossless"] = True
        params = self._params_with_sensitive_mask_binding(request)
        result = await self._call("screenshot", params, timeout=timeout)
        if isinstance(result, dict) and isinstance(result.get("page_evidence"), dict):
            # Control ids stay with their element across this run's observations (control_ids.py).
            evidence = result["page_evidence"]
            relabelled = self._control_ids.relabel(
                evidence.get("controls"), scope=result.get("url") or evidence.get("url")
            )
            lines = evidence.get("focused_text_lines")
            if isinstance(lines, dict) and lines.get("control") in relabelled:
                # The focused text's control was named by its positional index; name it by the stable id.
                lines["control"] = relabelled[lines["control"]]
            # Controls seen earlier on this document and not listed now: the runtime may scroll one back into view.
            scope = result.get("url") or evidence.get("url")
            remembered = self._control_ids.remembered(
                scope=scope,
                current=[row.get("index") for row in (evidence.get("controls") or []) if isinstance(row, dict)],
            )
            if remembered:
                # Every remembered control, not the newest N by id: a date picker lists 100+ day buttons, and a
                # cap that kept the highest ids dropped the form's low-numbered fields (run 1848: control 9
                # 'is in neither control_inventory nor control_memory' though the field was still on the page).
                # The registry's own size cap bounds this list; it is read only by the runtime.
                evidence["remembered_controls"] = remembered
        return result

    async def mark_sensitive_screenshot_control(
        self,
        selector: str,
        *,
        expected_page_context: dict[str, Any],
        timeout: float = 12.0,
    ) -> dict[str, Any]:
        """Bind one model-classified control to transient masking on the exact observed page.

        The body receives only a selector and page context. It never receives or stores the control
        value, and it does not decide whether the control is sensitive.
        """
        result = await self._call(
            "mark_sensitive_screenshot_control",
            {
                "selector": selector,
                "expected_page_context": dict(expected_page_context or {}),
            },
            timeout=timeout,
        )
        if (
            isinstance(result, dict)
            and result.get("ok")
            and result.get("selector_bound") is True
            and result.get("page_context_bound") is True
            and result.get("mask_surface_signature")
        ):
            selectors = _sensitive_selectors(
                result.get("bound_selectors") or selector
            )
            self._required_sensitive_screenshot_binding = {
                "selector": selectors[0],
                "expected_page_context": {
                    "url": str((expected_page_context or {}).get("url") or ""),
                    "mask_surface_signature": str(
                        result.get("mask_surface_signature") or ""
                    ),
                },
            }
            if len(selectors) > 1:
                self._required_sensitive_screenshot_binding["selectors"] = selectors
        return result

    async def ensure_sensitive_screenshot_control(
        self,
        selector: str,
        *,
        expected_page_context: dict[str, Any],
        timeout: float = 12.0,
    ) -> dict[str, Any]:
        return await self.ensure_sensitive_screenshot_controls(
            [selector],
            expected_page_context=expected_page_context,
            timeout=timeout,
        )

    async def ensure_sensitive_screenshot_controls(
        self,
        selectors: list[str],
        *,
        expected_page_context: dict[str, Any],
        timeout: float = 12.0,
    ) -> dict[str, Any]:
        """Check or recreate value-free semantic screenshot-mask bindings.

        This is used after explicit direct entry and before the next observation because an MV3
        worker restart clears the worker's in-memory binding. Only the selector and the opaque,
        value-free page/surface identity cross the channel.
        """
        exact_selectors = _sensitive_selectors(selectors)
        if not exact_selectors:
            return {
                "ok": False,
                "action": "ensure_sensitive_screenshot_control",
                "failure_reason": "sensitive_mask_selector_required",
                "selector_bound": False,
                "page_context_bound": False,
            }
        command_selectors: dict[str, Any] = (
            {"selector": exact_selectors[0]}
            if len(exact_selectors) == 1
            else {"selectors": exact_selectors}
        )
        result = await self._call(
            "ensure_sensitive_screenshot_control",
            {
                **command_selectors,
                "expected_page_context": dict(expected_page_context or {}),
            },
            timeout=timeout,
        )
        if (
            isinstance(result, dict)
            and result.get("ok")
            and result.get("selector_bound") is True
            and result.get("page_context_bound") is True
            and result.get("mask_surface_signature")
        ):
            bound_selectors = _sensitive_selectors(
                result.get("bound_selectors") or exact_selectors
            )
            if bound_selectors:
                self._required_sensitive_screenshot_binding = {
                    "selector": bound_selectors[0],
                    "expected_page_context": {
                        "url": str((expected_page_context or {}).get("url") or ""),
                        "mask_surface_signature": str(
                            result.get("mask_surface_signature") or ""
                        ),
                    },
                }
                if len(bound_selectors) > 1:
                    self._required_sensitive_screenshot_binding["selectors"] = (
                        bound_selectors
                    )
        return result

    # --- pure-vision coordinate actions (the brain acts in screenshot pixel space) ----------------
    async def vision_click(
        self,
        x: int,
        y: int,
        *,
        allow_final_submit: bool = False,
        allow_guarded_intermediate: bool = False,
        expected_target_identity: dict[str, Any] | None = None,
        expected_page_context: dict[str, Any] | None = None,
        button: str = "left",
        clicks: int = 1,
        hold_ms: int = 0,
        modifiers: list[str] | None = None,
        follow: dict[str, Any] | None = None,
        target_text: str = "",
        target_wait_ms: int = 0,
        glide_ms: int | None = None,
        settle: bool = True,
        refuse_value_control: bool = False,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        wait_budget = max(0, min(30000, int(target_wait_ms or 0))) if target_text else 0
        return await self._call(
            "vision_click",
            self._params_with_sensitive_mask_binding({
                "x": x,
                "y": y,
                "button": button,
                "clicks": int(clicks),
                "hold_ms": int(hold_ms or 0),
                "modifiers": list(modifiers or []),
                "allow_final_submit": bool(allow_final_submit),
                "allow_guarded_intermediate": bool(allow_guarded_intermediate),
                "expected_target_identity": (
                    dict(expected_target_identity)
                    if expected_target_identity is not None
                    else None
                ),
                "expected_page_context": dict(expected_page_context or {}),
                **({"follow": dict(follow)} if follow else {}),
                **({"target_text": str(target_text)[:120]} if target_text else {}),
                **({"target_wait_ms": wait_budget} if wait_budget else {}),
                **({"glide_ms": int(glide_ms)} if glide_ms is not None else {}),
                **({} if settle else {"settle": False}),
                **({"refuse_value_control": True} if refuse_value_control else {}),
            }),
            # The page waits for a labeled control up to target_wait_ms; the transport outlasts it.
            timeout=max(float(timeout), wait_budget / 1000 + 20),
        )

    async def vision_click_control(
        self,
        selector: str,
        fallback_x: int,
        fallback_y: int,
        *,
        allow_final_submit: bool = False,
        allow_guarded_intermediate: bool = False,
        expected_target_identity: dict[str, Any] | None = None,
        expected_page_context: dict[str, Any] | None = None,
        button: str = "left",
        clicks: int = 1,
        hold_ms: int = 0,
        modifiers: list[str] | None = None,
        point: tuple[int, int] | None = None,
        follow: dict[str, Any] | None = None,
        glide_ms: int | None = None,
        settle: bool = True,
        allow_rebind_on_drift: bool = False,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        # point: on a rendered surface (canvas/SVG), where on it to click, as an offset in screenshot
        # pixels from the control's observed top-left corner; other controls are clicked at center.
        return await self._call(
            "vision_click_control",
            self._params_with_sensitive_mask_binding({
                "button": button,
                "clicks": int(clicks),
                "hold_ms": int(hold_ms or 0),
                "modifiers": list(modifiers or []),
                **({"point": [int(point[0]), int(point[1])]} if point else {}),
                **({"follow": dict(follow)} if follow else {}),
                **({"glide_ms": int(glide_ms)} if glide_ms is not None else {}),
                **({} if settle else {"settle": False}),
                **({"allow_rebind_on_drift": True} if allow_rebind_on_drift else {}),
                "selector": selector,
                "fallback_x": fallback_x,
                "fallback_y": fallback_y,
                "allow_final_submit": bool(allow_final_submit),
                "allow_guarded_intermediate": bool(allow_guarded_intermediate),
                "expected_target_identity": dict(expected_target_identity or {}),
                "expected_page_context": dict(expected_page_context or {}),
            }),
            timeout=timeout,
        )

    async def vision_type(self, text: str, *, timeout: float = 20.0) -> dict[str, Any]:
        return await self._call(
            "vision_type",
            self._params_with_sensitive_mask_binding({"text": text}),
            timeout=timeout,
        )

    async def vision_type_control(
        self,
        selector: str,
        text: str,
        *,
        expected_target_identity: dict[str, Any] | None = None,
        expected_page_context: dict[str, Any] | None = None,
        point: tuple[int, int] | None = None,
        at_caret: bool = False,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        """Type into one observed control. ``point`` is where on a rendered surface (canvas/SVG)
        the typing goes, as an offset in screenshot pixels from the control's observed top-left
        corner (so a scroll since the observation moves it with the control): that painted field
        is clicked before the keys. ``at_caret`` inserts at the field's caret, replacing only its
        selection, instead of setting the whole value."""
        params: dict[str, Any] = {
            "selector": selector,
            "text": text,
            "expected_target_identity": dict(expected_target_identity or {}),
            "expected_page_context": dict(expected_page_context or {}),
        }
        if point:
            params["point"] = [int(point[0]), int(point[1])]
        if at_caret:
            params["at_caret"] = True
        return await self._call(
            "vision_type_control",
            self._params_with_sensitive_mask_binding(params),
            timeout=timeout,
        )

    async def vision_type_credential_control(
        self,
        selector: str,
        text: str,
        *,
        expected_target_identity: dict[str, Any] | None = None,
        expected_page_context: dict[str, Any] | None = None,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        """Type one vault-resolved value while forcing value-free browser receipts.

        The exact value is transient command data. The extension verifies equality locally and
        returns only booleans/lengths, even when the page exposes an ordinary text input.
        """

        return await self._call(
            "vision_type_credential_control",
            self._params_with_sensitive_mask_binding(
                {
                    "selector": selector,
                    "text": text,
                    "expected_target_identity": dict(expected_target_identity or {}),
                    "expected_page_context": dict(expected_page_context or {}),
                }
            ),
            timeout=timeout,
        )

    async def vision_scroll(
        self,
        dy: int,
        *,
        dx: int = 0,
        selector: str = "",
        expected_target_identity: dict[str, Any] | None = None,
        expected_page_context: dict[str, Any] | None = None,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"dy": dy}
        if dx:
            params["dx"] = dx
        if selector:
            params.update(
                {
                    "selector": selector,
                    "expected_target_identity": dict(
                        expected_target_identity or {}
                    ),
                    "expected_page_context": dict(expected_page_context or {}),
                }
            )
        try:
            return await self._call(
                "vision_scroll",
                self._params_with_sensitive_mask_binding(params),
                timeout=timeout,
            )
        except ExtensionTransportError as exc:
            if not exc.command_may_have_executed:
                raise
            # The browser may already have mutated before its response was lost. Project only
            # fixed, value-free execution facts so the runtime can retain a receipt and block
            # without issuing another scroll.
            return {
                "ok": False,
                "action": "scroll",
                "mode": "element_scroll" if selector else "document_scroll",
                "failure_reason": "scroll_execution_outcome_unknown",
                "scroll_attempted": True,
                "browser_mutation_attempted": True,
                "execution_outcome_unknown": True,
            }

    async def vision_scroll_into_view(
        self,
        selector: str,
        *,
        expected_target_identity: dict[str, Any] | None = None,
        timeout: float = 12.0,
    ) -> dict[str, Any]:
        """Scroll the one element this selector names into view (nearest), after the page confirms the
        element still has the structural identity (tag, role, type) it had when seen. Nothing is clicked or
        typed; the caller re-observes before trusting the control."""
        return await self._call(
            "vision_scroll_into_view",
            {"selector": str(selector or ""), "expected_target_identity": dict(expected_target_identity or {})},
            timeout=timeout,
        )

    async def vision_wait(self, wait_ms: int, *, text: str = "", timeout: float = 12.0) -> dict[str, Any]:
        """Pause, or wait (up to wait_ms) until the model-named exact text is visible on the page.
        The body reports only whether the text appeared; the model chose what to wait for."""
        params: dict[str, Any] = {"wait_ms": int(wait_ms)}
        if text:
            params["text"] = str(text)[:200]
        # The page waits up to wait_ms; the transport must outlast it.
        return await self._call("vision_wait", params, timeout=max(float(timeout), min(30000, int(wait_ms or 0)) / 1000 + 6))

    async def vision_drag(
        self,
        x: int,
        y: int,
        to_x: int,
        to_y: int,
        *,
        button: str = "left",
        path: list[tuple[int, int]] | None = None,
        hold_ms: int = 0,
        modifiers: list[str] | None = None,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        """Press at a screenshot point, move in steps (through any waypoints), release at another:
        sliders, dials, canvases, text selection, and native drag-and-drop. Image pixels.
        hold_ms waits pressed before moving (long-press-to-drag); modifiers are held throughout."""
        params: dict[str, Any] = {"x": int(x), "y": int(y), "to_x": int(to_x), "to_y": int(to_y), "button": str(button or "left")}
        if path:
            params["path"] = [[int(px), int(py)] for px, py in path][:40]
        hold = max(0, min(5000, int(hold_ms or 0)))
        if hold:
            params["hold_ms"] = hold
        if modifiers:
            params["modifiers"] = [str(item) for item in modifiers]
        try:
            return await self._call("vision_drag", params, timeout=float(timeout) + hold / 1000)
        except ExtensionTransportError as exc:
            if not exc.command_may_have_executed:
                raise
            return {
                "ok": False,
                "action": "drag",
                "failure_reason": "drag_execution_outcome_unknown",
                "activation_attempted": True,
                "execution_outcome_unknown": True,
            }

    async def vision_select_text(self, text: str, x: int, y: int, *, timeout: float = 15.0) -> dict[str, Any]:
        """Drag-select exactly this page text: the occurrence nearest (x, y) in screenshot pixels, with a
        real pointer drag across its characters' own edges; the selection is read back."""
        return await self._call(
            "vision_select_text",
            {"text": str(text or "")[:400], "x": int(x or 0), "y": int(y or 0)},
            timeout=timeout,
        )

    async def vision_scroll_to_text(self, text: str, *, occurrence: int = 1, timeout: float = 15.0) -> dict[str, Any]:
        """The browser's Find: bring the nth rendered occurrence of this exact text into view,
        scrolling every enclosing region."""
        return await self._call(
            "vision_scroll_to_text",
            {"text": str(text or "")[:200], "occurrence": max(1, int(occurrence or 1))},
            timeout=timeout,
        )

    async def vision_watch(
        self,
        *,
        selector: str = "",
        point: tuple[int, int] | None = None,
        duration_ms: int = 5000,
    ) -> dict[str, Any]:
        """Read one element's text continuously for duration_ms and return every distinct value in
        order with its time: eyes for values that change faster than a screenshot round trip.
        A required sensitive-mask binding rides along: the extension refuses to watch a sensitive
        control (watch_target_sensitive) and reads nothing."""
        duration = max(200, min(15000, int(duration_ms or 0)))
        params: dict[str, Any] = {"selector": selector, "duration_ms": duration}
        if point:
            params["point"] = [int(point[0]), int(point[1])]
        return await self._call(
            "vision_watch", self._params_with_sensitive_mask_binding(params), timeout=duration / 1000.0 + 10.0
        )

    async def vision_read_print_view(self, *, timeout: float = 15.0) -> dict[str, Any]:
        """The run tab's main-frame text as the page renders for print, and the lines only print shows.
        Read-only: the extension toggles print media emulation on its debugger session and restores it.
        Redacted like read_run_tab_text: the required sensitive-mask binding rides along."""
        return await self._call(
            "vision_read_print_view", self._params_with_sensitive_mask_binding({}), timeout=timeout
        )

    async def vision_read_form(self, *, target_selector: str = "", timeout: float = 15.0) -> dict[str, Any]:
        """Every form control of the run tab (main document and same-origin frames), on screen or not: label, kind,
        required, value or chosen options, owning form, and whether it is in view. Read-only; redacted like the print
        view, with the required sensitive-mask binding riding along. With `target_selector`, `form_target` names the
        form that control belongs to (null when the selector names no one element)."""
        params: dict[str, Any] = {"target_selector": str(target_selector)[:400]} if target_selector else {}
        return await self._call("vision_read_form", self._params_with_sensitive_mask_binding(params), timeout=timeout)

    async def vision_switch_tab(self, target: str, *, timeout: float = 15.0) -> dict[str, Any]:
        """Bring one of this run's own tabs (a tab_id from browser_tabs) to the front."""
        return await self._call("vision_switch_tab", {"target": str(target or "")}, timeout=timeout)

    async def vision_hover(self, x: int, y: int, *, dwell_ms: int = 0, timeout: float = 15.0) -> dict[str, Any]:
        """Move the pointer to a screenshot point and leave it there (dwell_ms longer for a delayed
        tooltip or menu). Coordinates are image pixels."""
        params: dict[str, Any] = {"x": int(x), "y": int(y)}
        dwell = max(0, min(5000, int(dwell_ms or 0)))
        if dwell:
            params["dwell_ms"] = dwell
        return await self._call("vision_hover", params, timeout=float(timeout) + dwell / 1000)

    async def vision_wheel(
        self,
        x: int,
        y: int,
        dx: int,
        dy: int,
        *,
        modifiers: list[str] | None = None,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        """A mouse wheel at a screenshot point: scrolls the innermost scrollable region under the
        pointer (or the page), exactly as a person's wheel does. Coordinates are image pixels.
        Modifiers are held during the wheel (ctrl+wheel zooms a map or canvas)."""
        params: dict[str, Any] = {"x": int(x), "y": int(y), "dx": int(dx), "dy": int(dy)}
        if modifiers:
            params["modifiers"] = [str(item) for item in modifiers]
        try:
            return await self._call("vision_wheel", self._params_with_sensitive_mask_binding(params), timeout=timeout)
        except ExtensionTransportError as exc:
            if not exc.command_may_have_executed:
                raise
            return {
                "ok": False,
                "action": "scroll",
                "mode": "wheel_scroll",
                "failure_reason": "scroll_execution_outcome_unknown",
                "scroll_attempted": True,
                "browser_mutation_attempted": True,
                "execution_outcome_unknown": True,
            }

    async def navigate_back(self, *, timeout: float = 30.0) -> dict[str, Any]:
        """The browser's Back button (real history navigation)."""
        return await self._call("navigate_back", {}, timeout=timeout)

    async def navigate_forward(self, *, timeout: float = 30.0) -> dict[str, Any]:
        """The browser's Forward button (real history navigation)."""
        return await self._call("navigate_forward", {}, timeout=timeout)

    async def vision_navigate(self, url: str, *, new_tab: bool = False, timeout: float = 45.0) -> dict[str, Any]:
        """The browser's address bar: open an http(s) URL in this task's tab, or in a new tab this
        task owns (the current tab and anything typed into it stay as they are)."""
        return await self._call(
            "vision_navigate",
            {"url": str(url or "")[:2048], "new_tab": bool(new_tab)},
            timeout=timeout,
        )

    async def vision_reload(self, *, timeout: float = 30.0) -> dict[str, Any]:
        """The browser's Reload button for this task's tab."""
        return await self._call("vision_reload", {}, timeout=timeout)

    async def vision_key(
        self,
        key: str,
        *,
        repeat: int = 1,
        hold_ms: int = 0,
        timeout: float = 20.0,
    ) -> dict[str, Any]:
        """Press one key or chord; repeat presses it again (Backspace x25), hold_ms keeps each
        press down that long before release (a held arrow in a game)."""
        params: dict[str, Any] = {"key": key}
        presses = max(1, min(200, int(repeat or 1)))
        hold = max(0, min(5000, int(hold_ms or 0)))
        if presses > 1:
            params["repeat"] = presses
        if hold:
            params["hold_ms"] = hold
        # Each press takes a few tens of milliseconds plus its hold; the transport outlasts them all.
        return await self._call(
            "vision_key",
            self._params_with_sensitive_mask_binding(params),
            timeout=float(timeout) + presses * (0.05 + hold / 1000),
        )

    async def vision_upload(self, x: int, y: int, document: dict[str, Any], *, timeout: float = 35.0) -> dict[str, Any]:
        return await self._call(
            "vision_upload",
            self._params_with_sensitive_mask_binding(
                {"x": x, "y": y, "document": document}
            ),
            timeout=timeout,
        )

    async def vision_upload_control(
        self,
        selector: str,
        document: dict[str, Any],
        fallback_x: int,
        fallback_y: int,
        *,
        expected_target_identity: dict[str, Any] | None = None,
        expected_page_context: dict[str, Any] | None = None,
        timeout: float = 35.0,
    ) -> dict[str, Any]:
        return await self._call(
            "vision_upload_control",
            self._params_with_sensitive_mask_binding({
                "selector": selector,
                "document": document,
                "expected_target_identity": dict(expected_target_identity or {}),
                "expected_page_context": dict(expected_page_context or {}),
            }),
            timeout=timeout,
        )

    # The browser saves downloads to this computer's downloads folder, which the API reads directly.
    def downloads_snapshot(self) -> dict[str, Any]:
        from app import run_downloads

        return run_downloads.local_snapshot()

    def downloads_listing(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        from app import run_downloads

        return run_downloads.local_listing(snapshot)
