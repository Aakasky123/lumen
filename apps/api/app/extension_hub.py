"""Persistent backend <-> in-browser-extension channel.

The extension's background service worker holds a WebSocket to the backend and acts as a
headless body: the brain (server-side, driven from the chat UI) sends typed commands
(observe / execute / navigate) down this channel and awaits the result. All user input and
output stays in the chat — the extension has no UI of its own.

This module is transport only: a connection registry plus request/response correlation. It
holds no intelligence and makes no value decisions (dumb body, smart brain).
"""

from __future__ import annotations

import asyncio
from typing import Any


REQUIRED_LIVE_ACTION_CAPABILITIES = frozenset(
    {
        "active_control_indicator",
        "atomic_credential_replay",
        "atomic_targetable_scroll",
        "atomic_screenshot_observation",
        "credential_replay_result_redaction",
        "exact_final_action_page_binding",
        "exact_observation_target_identity",
        "sensitive_page_evidence_redaction",
        "sensitive_screenshot_region_masking",
        "targetable_scroll_container",
        "targetable_scroll_outcome_unknown",
        "transient_semantic_screenshot_masking",
        "transient_semantic_screenshot_mask_rebind",
    }
)


class ExtensionTransportError(RuntimeError):
    """The browser-body channel disconnected or could not send a command."""

    def __init__(
        self,
        message: str,
        *,
        command_may_have_executed: bool = False,
    ) -> None:
        super().__init__(message)
        # Transport loss after dispatch is not proof of browser non-execution. Keep this
        # value-free stage fact on the exception so callers can withhold duplicate actions.
        self.command_may_have_executed = bool(command_may_have_executed)


class ExtensionCommandError(RuntimeError):
    """The connected browser body rejected a command after receiving it."""


class ExtensionConnection:
    """One connected extension. Correlates async request ids to responses so the brain can
    `await conn.call("observe")` and get the body's reply."""

    def __init__(self, device_id: str, websocket: Any) -> None:
        self.device_id = device_id
        self.websocket = websocket
        self.hello_received = False
        self.capabilities: frozenset[str] = frozenset()
        self.extension_build = ""
        self._pending: dict[str, asyncio.Future] = {}
        self._counter = 0

    def accept_hello(self, message: dict[str, Any]) -> bool:
        """Record the body's structural safety contract without inferring page meaning."""
        if str(message.get("device_id") or "") != self.device_id:
            return False
        raw_capabilities = message.get("capabilities")
        if not isinstance(raw_capabilities, dict):
            return False
        self.capabilities = frozenset(
            str(name)
            for name, enabled in raw_capabilities.items()
            if enabled is True and str(name)
        )
        self.extension_build = str(message.get("build") or "")[:120]
        self.hello_received = True
        return True

    def missing_live_action_capabilities(self) -> list[str]:
        return sorted(REQUIRED_LIVE_ACTION_CAPABILITIES.difference(self.capabilities))

    @property
    def live_actions_ready(self) -> bool:
        return bool(self.hello_received and not self.missing_live_action_capabilities())

    def public_status(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "hello_received": self.hello_received,
            "live_actions_ready": self.live_actions_ready,
            "capabilities": sorted(self.capabilities),
            "missing_live_action_capabilities": self.missing_live_action_capabilities(),
            "extension_build": self.extension_build or None,
        }

    def _next_id(self) -> str:
        self._counter += 1
        return f"cmd-{self._counter}"

    async def call(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30.0) -> Any:
        if not self.live_actions_ready:
            missing = ", ".join(self.missing_live_action_capabilities()) or "extension hello"
            raise ExtensionTransportError(
                f"Connected Lumen extension is not ready for live actions; reload it ({missing})."
            )
        request_id = self._next_id()
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[request_id] = future
        dispatch_started = False
        try:
            try:
                # Once the transport call starts, an exception cannot prove that the peer did
                # not receive and execute the command. Mark conservatively before awaiting I/O.
                dispatch_started = True
                await self.websocket.send_json({"type": "command", "id": request_id, "method": method, "params": params or {}})
            except Exception as exc:  # noqa: BLE001 - normalize framework-specific websocket errors
                if not future.done():
                    future.cancel()
                raise ExtensionTransportError(
                    str(exc) or "Extension command could not be sent.",
                    command_may_have_executed=dispatch_started,
                ) from exc
            try:
                return await asyncio.wait_for(future, timeout=timeout)
            except ExtensionTransportError as exc:
                if dispatch_started and not exc.command_may_have_executed:
                    raise ExtensionTransportError(
                        str(exc) or "Extension command response was lost.",
                        command_may_have_executed=True,
                    ) from exc
                raise
        finally:
            self._pending.pop(request_id, None)

    def resolve(self, request_id: str, result: Any, error: str | None = None) -> None:
        future = self._pending.get(request_id)
        if not future or future.done():
            return
        if error is not None:
            future.set_exception(ExtensionCommandError(error))
        else:
            future.set_result(result)

    async def notify(self, message_type: str, payload: dict[str, Any] | None = None) -> None:
        """Send a one-way notification to the extension.

        Notifications are intentionally fire-and-forget: unlike body commands, they do not
        participate in request/response correlation and must never block browser progress.
        """
        await self.websocket.send_json({"type": message_type, "payload": payload or {}})

    def fail_all(self, reason: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ExtensionTransportError(reason))
        self._pending.clear()


# Gauntlet bench lanes: headless Chromium bodies that live_run starts so browser-only pages run in
# parallel. A lane takes only the runs of chat threads pinned to it and never the user's own tasks.
BENCH_LANE_PREFIX = "bench-lane-"


def is_bench_lane(device_id: str | None) -> bool:
    return str(device_id or "").startswith(BENCH_LANE_PREFIX)


class ExtensionHub:
    """Registry of connected extension bodies, keyed by device id."""

    def __init__(self) -> None:
        self._connections: dict[str, ExtensionConnection] = {}
        # Chat thread id -> the bench lane its runs must use (in memory; bench runs are short-lived).
        self._thread_lanes: dict[int, str] = {}
        # The uvicorn event loop, captured when the websocket connects, so the sync chat
        # answer path can schedule an async in-browser resume onto it (run_coroutine_threadsafe).
        self.loop: Any = None

    def register(self, device_id: str, websocket: Any) -> ExtensionConnection:
        existing = self._connections.get(device_id)
        if existing:
            existing.fail_all("Replaced by a new connection for this device.")
        connection = ExtensionConnection(device_id, websocket)
        self._connections[device_id] = connection
        return connection

    def unregister(self, device_id: str, connection: ExtensionConnection | None = None) -> bool:
        """Remove only the socket instance that is actually disconnecting.

        Chrome can replace an MV3 worker socket before the old websocket's ``finally`` block
        runs.  Device id alone is therefore not enough ownership proof: stale cleanup must not
        evict the replacement connection that now owns the same device id.
        """
        current = self._connections.get(device_id)
        if current is None:
            if connection is not None:
                connection.fail_all("Extension disconnected.")
            return False
        if connection is not None and current is not connection:
            connection.fail_all("Extension disconnected.")
            return False
        removed = self._connections.pop(device_id)
        removed.fail_all("Extension disconnected.")
        return True

    def get(self, device_id: str | None = None) -> ExtensionConnection | None:
        if device_id:
            return self._connections.get(device_id)
        # Default to the most recently registered body that is the user's own (never a bench lane).
        own = [connection for connection in self._connections.values() if not is_bench_lane(connection.device_id)]
        return own[-1] if own else None

    def pin_thread_to_lane(self, thread_id: int, device_id: str) -> None:
        if not is_bench_lane(device_id):
            raise ValueError("Only a bench lane can be pinned to a thread.")
        self._thread_lanes[int(thread_id)] = device_id

    def lane_for_thread(self, thread_id: int | None) -> str | None:
        return self._thread_lanes.get(int(thread_id)) if thread_id is not None else None

    def get_for_thread(self, thread_id: int | None) -> ExtensionConnection | None:
        """The body a chat thread's run uses: its pinned bench lane (None while that lane is not
        connected — never another body), else the user's own default body."""
        lane = self.lane_for_thread(thread_id)
        return self._connections.get(lane) if lane else self.get(None)

    def connected_devices(self) -> list[str]:
        return list(self._connections.keys())

    def connection_statuses(self) -> list[dict[str, Any]]:
        return [connection.public_status() for connection in self._connections.values()]


# Process-wide singleton (one uvicorn process owns the live connections).
hub = ExtensionHub()
