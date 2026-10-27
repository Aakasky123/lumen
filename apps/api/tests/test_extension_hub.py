"""The persistent backend <-> extension channel (the spine of the chat-driven in-browser body).

Covers request/response correlation, error propagation, the connection registry, and the
ExtensionBody adapter mapping observe/navigate/execute to channel calls. Uses asyncio.run()
directly so it needs no pytest-asyncio config. See memory: in-browser-extension-architecture.
"""

import asyncio

from fastapi.testclient import TestClient

from app.extension_body import ExtensionBody
from app.extension_hub import (
    REQUIRED_LIVE_ACTION_CAPABILITIES,
    ExtensionCommandError,
    ExtensionConnection,
    ExtensionHub,
    ExtensionTransportError,
)
from app.main import app


class FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)


class FakeConn:
    def __init__(self, device_id: str = "dev-fake") -> None:
        self.device_id = device_id
        self.calls: list[tuple] = []

    async def call(self, method: str, params: dict | None = None, *, timeout: float = 30.0):
        self.calls.append((method, params, timeout))
        return {"ok": True, "method": method}


def _accept_current_hello(conn: ExtensionConnection) -> None:
    assert conn.accept_hello(
        {
            "type": "hello",
            "device_id": conn.device_id,
            "capabilities": {
                capability: True for capability in REQUIRED_LIVE_ACTION_CAPABILITIES
            },
        }
    )


def test_call_sends_command_and_resolves_on_response() -> None:
    async def scenario():
        ws = FakeWS()
        conn = ExtensionConnection("dev-1", ws)
        _accept_current_hello(conn)

        async def respond():
            while not ws.sent:
                await asyncio.sleep(0)
            conn.resolve(ws.sent[-1]["id"], {"ok": True, "page_type": "application_form"})

        result, _ = await asyncio.gather(conn.call("observe", timeout=2), respond())
        return ws, result

    ws, result = asyncio.run(scenario())
    assert result == {"ok": True, "page_type": "application_form"}
    assert ws.sent[-1]["type"] == "command" and ws.sent[-1]["method"] == "observe"


def test_call_propagates_error() -> None:
    async def scenario():
        ws = FakeWS()
        conn = ExtensionConnection("dev-1", ws)
        _accept_current_hello(conn)

        async def respond():
            while not ws.sent:
                await asyncio.sleep(0)
            conn.resolve(ws.sent[-1]["id"], None, error="element not found")

        try:
            await asyncio.gather(conn.call("execute", {"intent": {}}, timeout=2), respond())
        except ExtensionCommandError as exc:
            return str(exc), type(exc)
        return None

    assert asyncio.run(scenario()) == ("element not found", ExtensionCommandError)


def test_call_normalizes_send_failure_and_cleans_pending_request() -> None:
    class ClosedWS:
        async def send_json(self, message: dict) -> None:
            raise RuntimeError("websocket already closed")

    async def scenario():
        conn = ExtensionConnection("dev-closed", ClosedWS())
        _accept_current_hello(conn)
        try:
            await conn.call("observe", timeout=2)
        except ExtensionTransportError as exc:
            return str(exc), dict(conn._pending), exc.command_may_have_executed
        return None

    assert asyncio.run(scenario()) == ("websocket already closed", {}, True)


def test_hub_register_get_and_unregister_fails_pending() -> None:
    async def scenario():
        hub = ExtensionHub()
        ws = FakeWS()
        conn = hub.register("dev-9", ws)
        _accept_current_hello(conn)
        assert hub.get() is conn and hub.get("dev-9") is conn
        assert hub.connected_devices() == ["dev-9"]
        pending = asyncio.ensure_future(conn.call("observe", timeout=5))
        await asyncio.sleep(0)  # let the command be sent + future registered
        hub.unregister("dev-9")
        raised = False
        command_may_have_executed = False
        try:
            await pending
        except ExtensionTransportError as exc:
            raised = True
            command_may_have_executed = exc.command_may_have_executed
        return raised, command_may_have_executed, hub.connected_devices()

    raised, command_may_have_executed, devices = asyncio.run(scenario())
    assert raised is True
    assert command_may_have_executed is True
    assert devices == []


def test_stale_socket_cleanup_does_not_unregister_replacement() -> None:
    hub = ExtensionHub()
    stale = hub.register("dev-stable", FakeWS())
    replacement = hub.register("dev-stable", FakeWS())

    assert hub.get("dev-stable") is replacement
    assert hub.unregister("dev-stable", stale) is False
    assert hub.get("dev-stable") is replacement
    assert hub.connected_devices() == ["dev-stable"]

    assert hub.unregister("dev-stable", replacement) is True
    assert hub.connected_devices() == []


def test_connection_rejects_live_commands_until_exact_capability_hello() -> None:
    async def scenario():
        ws = FakeWS()
        conn = ExtensionConnection("dev-stale", ws)
        incomplete = conn.accept_hello(
            {
                "type": "hello",
                "device_id": "dev-stale",
                "capabilities": {"sensitive_page_evidence_redaction": True},
            }
        )
        try:
            await conn.call("navigate", {"url": "https://example.test"}, timeout=2)
        except ExtensionTransportError as exc:
            return (
                incomplete,
                str(exc),
                exc.command_may_have_executed,
                ws.sent,
                conn.public_status(),
            )
        raise AssertionError("A stale extension unexpectedly received a live browser command.")

    accepted, error, command_may_have_executed, sent, status = asyncio.run(scenario())
    assert accepted is True
    assert "reload it" in error
    assert command_may_have_executed is False
    assert sent == []
    assert status["live_actions_ready"] is False
    assert "atomic_screenshot_observation" in status["missing_live_action_capabilities"]


def test_extension_body_maps_methods_to_channel_calls() -> None:
    async def scenario():
        conn = FakeConn()
        body = ExtensionBody(conn)
        await body.navigate("https://x.example/apply")
        await body.observe()
        await body.execute({"action": "fill", "selector": "#a", "value": "v"})
        return conn.calls

    calls = asyncio.run(scenario())
    assert [c[0] for c in calls] == ["navigate", "observe", "execute"]
    assert calls[0][1] == {"url": "https://x.example/apply", "new_tab": True}
    assert calls[2][1] == {"intent": {"action": "fill", "selector": "#a", "value": "v"}}
    assert not hasattr(ExtensionBody, "vision_retry_final_submit")


def test_extension_body_projects_lost_post_dispatch_scroll_response_as_unknown() -> None:
    class ResponseLostConn(FakeConn):
        async def call(
            self,
            method: str,
            params: dict | None = None,
            *,
            timeout: float = 30.0,
        ):
            self.calls.append((method, params, timeout))
            raise ExtensionTransportError(
                "response lost after dispatch",
                command_may_have_executed=True,
            )

    async def scenario():
        conn = ResponseLostConn("dev-scroll-lost")
        body = ExtensionBody(conn, connection_lookup=lambda _device_id: conn)
        result = await body.vision_scroll(
            180,
            selector="#opaque-current-control",
            expected_target_identity={"target_selector": "#opaque-current-control"},
            expected_page_context={"url": "https://example.test"},
        )
        return conn.calls, result

    calls, result = asyncio.run(scenario())
    assert len(calls) == 1
    assert calls[0][0] == "vision_scroll"
    assert result == {
        "ok": False,
        "action": "scroll",
        "mode": "element_scroll",
        "failure_reason": "scroll_execution_outcome_unknown",
        "scroll_attempted": True,
        "browser_mutation_attempted": True,
        "execution_outcome_unknown": True,
    }


def test_extension_body_restores_active_indicator_after_worker_replacement() -> None:
    async def scenario():
        stale = FakeConn("dev-indicator")
        replacement = FakeConn("dev-indicator")
        current = {"connection": stale}
        body = ExtensionBody(
            stale,
            connection_lookup=lambda device_id: (
                current["connection"] if device_id == "dev-indicator" else None
            ),
        )
        await body.set_control_indicator(True, run_id=77)
        current["connection"] = replacement
        await body.observe()
        await body.set_control_indicator(False, run_id=77)
        return stale.calls, replacement.calls

    stale_calls, replacement_calls = asyncio.run(scenario())
    assert stale_calls == [
        (
            "set_control_indicator",
            {
                "active": True,
                "message": "Lumen is using your computer",
                "run_id": 77,
            },
            8.0,
        )
    ]
    assert [call[0] for call in replacement_calls] == [
        "set_control_indicator",
        "observe",
        "set_control_indicator",
    ]
    assert replacement_calls[-1][1]["active"] is False


def test_extension_body_preserves_null_identity_for_selectorless_coordinate_click() -> None:
    expected_page_context = {
        "url": "https://example.test/seats",
        "surface_signature": "surface-17",
        "screenshot_sha256": "shot-17",
    }
    final_target_identity = {
        "target_selector": "#confirm",
        "target_text": "Confirm",
        "target_tag": "button",
        "target_type": "button",
        "target_role": "",
        "dispatch_path": "dom_activation",
        "page_url": "https://example.test/seats",
    }

    async def scenario():
        conn = FakeConn()
        body = ExtensionBody(conn)
        await body.vision_click(
            240,
            180,
            expected_target_identity=None,
            expected_page_context=expected_page_context,
        )
        await body.vision_click_control(
            "#confirm",
            240,
            180,
            allow_final_submit=True,
            expected_target_identity=final_target_identity,
            expected_page_context=expected_page_context,
        )
        return conn.calls

    calls = asyncio.run(scenario())
    coordinate_params = calls[0][1]
    assert calls[0][0] == "vision_click"
    assert coordinate_params["expected_target_identity"] is None
    assert coordinate_params["expected_page_context"] == expected_page_context
    assert coordinate_params["allow_final_submit"] is False

    final_params = calls[1][1]
    assert calls[1][0] == "vision_click_control"
    assert final_params["expected_target_identity"] == final_target_identity
    assert final_params["expected_page_context"] == expected_page_context
    assert final_params["allow_final_submit"] is True


def test_extension_body_marks_exact_sensitive_screenshot_control_without_a_value() -> None:
    async def scenario():
        conn = FakeConn()
        body = ExtensionBody(conn)
        await body.mark_sensitive_screenshot_control(
            "#recovery-answer",
            expected_page_context={
                "url": "https://example.test/recovery",
                "surface_signature": "surface-17",
            },
        )
        return conn.calls

    calls = asyncio.run(scenario())
    assert calls == [
        (
            "mark_sensitive_screenshot_control",
            {
                "selector": "#recovery-answer",
                "expected_page_context": {
                    "url": "https://example.test/recovery",
                    "surface_signature": "surface-17",
                },
            },
            12.0,
        )
    ]
    assert "value" not in calls[0][1]


def test_extension_body_rebinds_sensitive_mask_and_requires_it_on_screenshot() -> None:
    class RebindConn(FakeConn):
        async def call(
            self,
            method: str,
            params: dict | None = None,
            *,
            timeout: float = 30.0,
        ):
            self.calls.append((method, params, timeout))
            if method == "ensure_sensitive_screenshot_control":
                return {
                    "ok": True,
                    "action": method,
                    "selector_bound": True,
                    "page_context_bound": True,
                    "mask_surface_signature": "mask-surface-17",
                    "binding_rebound": True,
                }
            return {"ok": True, "method": method}

    async def scenario():
        conn = RebindConn()
        body = ExtensionBody(conn)
        await body.ensure_sensitive_screenshot_control(
            "#recovery-answer",
            expected_page_context={
                "url": "https://example.test/recovery",
                "mask_surface_signature": "mask-surface-17",
            },
        )
        structured_observation = await body.observe()
        await body.capture_screenshot()
        await body.vision_scroll(240)
        return conn.calls, structured_observation

    calls, structured_observation = asyncio.run(scenario())
    assert calls[0] == (
        "ensure_sensitive_screenshot_control",
        {
            "selector": "#recovery-answer",
            "expected_page_context": {
                "url": "https://example.test/recovery",
                "mask_surface_signature": "mask-surface-17",
            },
        },
        12.0,
    )
    assert calls[1] == (
        "screenshot",
        {
            "full_page": False,
            "required_sensitive_mask_binding": {
                "selector": "#recovery-answer",
                "expected_page_context": {
                    "url": "https://example.test/recovery",
                    "mask_surface_signature": "mask-surface-17",
                },
            },
        },
        24.0,
    )
    assert calls[2] == (
        "vision_scroll",
        {
            "dy": 240,
            "required_sensitive_mask_binding": {
                "selector": "#recovery-answer",
                "expected_page_context": {
                    "url": "https://example.test/recovery",
                    "mask_surface_signature": "mask-surface-17",
                },
            },
        },
        20.0,
    )
    assert "value" not in calls[0][1]
    assert "value" not in calls[1][1]["required_sensitive_mask_binding"]
    assert "value" not in calls[2][1]["required_sensitive_mask_binding"]
    assert structured_observation == {
        "ok": False,
        "action": "observe",
        "failure_reason": "sensitive_semantic_observe_unavailable",
        "observation_discarded": True,
    }
    assert all(call[0] != "observe" for call in calls)


def test_extension_body_reads_the_run_tab_text_with_the_required_sensitive_binding() -> None:
    class RebindConn(FakeConn):
        async def call(self, method: str, params: dict | None = None, *, timeout: float = 30.0):
            self.calls.append((method, params, timeout))
            if method == "ensure_sensitive_screenshot_control":
                return {
                    "ok": True,
                    "action": method,
                    "selector_bound": True,
                    "page_context_bound": True,
                    "mask_surface_signature": "mask-surface-17",
                }
            if method == "read_run_tab_text":
                # A refused read never carries a rebind demand, so the browser body's binding stays.
                return {"ok": False, "action": method, "failure_reason": "sensitive_selector_drift"}
            return {"ok": True, "method": method}

    async def scenario():
        plain_conn = FakeConn()
        plain = await ExtensionBody(plain_conn).read_run_tab_text(max_chars=6000)
        conn = RebindConn()
        body = ExtensionBody(conn)
        await body.ensure_sensitive_screenshot_control(
            "#recovery-answer",
            expected_page_context={"url": "https://example.test/recovery", "mask_surface_signature": "mask-surface-17"},
        )
        refused = await body.read_run_tab_text()
        await body.capture_screenshot()
        return plain_conn.calls, plain, conn.calls, refused

    plain_calls, plain, calls, refused = asyncio.run(scenario())
    assert plain_calls == [("read_run_tab_text", {"max_chars": 6000}, 8.0)]
    assert plain == {"ok": True, "method": "read_run_tab_text"}
    assert calls[1][0] == "read_run_tab_text"
    assert calls[1][1]["required_sensitive_mask_binding"]["selector"] == "#recovery-answer"
    assert refused["ok"] is False
    assert "required_sensitive_mask_binding" in calls[2][1], "the read did not drop the browser body's binding"


def test_extension_body_watch_carries_the_required_sensitive_binding() -> None:
    class RebindConn(FakeConn):
        async def call(self, method: str, params: dict | None = None, *, timeout: float = 30.0):
            self.calls.append((method, params, timeout))
            if method == "ensure_sensitive_screenshot_control":
                return {
                    "ok": True,
                    "action": method,
                    "selector_bound": True,
                    "page_context_bound": True,
                    "mask_surface_signature": "mask-surface-17",
                }
            return {"ok": True, "method": method}

    async def scenario():
        plain_conn = FakeConn()
        await ExtensionBody(plain_conn).vision_watch(selector="#gauge", duration_ms=3000)
        conn = RebindConn()
        body = ExtensionBody(conn)
        await body.ensure_sensitive_screenshot_control(
            "#recovery-answer",
            expected_page_context={"url": "https://example.test/recovery", "mask_surface_signature": "mask-surface-17"},
        )
        await body.vision_watch(selector="#gauge", point=(40, 60), duration_ms=3000)
        return plain_conn.calls, conn.calls

    plain_calls, calls = asyncio.run(scenario())
    assert plain_calls == [("vision_watch", {"selector": "#gauge", "duration_ms": 3000}, 13.0)]
    method, params, _timeout = calls[1]
    assert method == "vision_watch"
    assert params["selector"] == "#gauge" and params["point"] == [40, 60]
    # The extension refuses to watch the model-marked control (or anything holding it).
    assert params["required_sensitive_mask_binding"]["selector"] == "#recovery-answer"


def test_extension_body_sends_exact_target_binding_only_for_targeted_scroll() -> None:
    async def scenario():
        conn = FakeConn()
        body = ExtensionBody(conn)
        await body.vision_scroll(240)
        await body.vision_scroll(
            180,
            selector="#menu-option",
            expected_target_identity={
                "target_selector": "#menu-option",
                "target_text": "Option",
                "target_tag": "button",
                "target_type": "",
                "target_role": "option",
                "dispatch_path": "dom_activation",
                "page_url": "https://example.test/apply",
            },
            expected_page_context={
                "url": "https://example.test/apply",
                "document_lifecycle_token": "doc-1",
            },
        )
        return conn.calls

    calls = asyncio.run(scenario())
    assert calls[0] == ("vision_scroll", {"dy": 240}, 20.0)
    assert calls[1] == (
        "vision_scroll",
        {
            "dy": 180,
            "selector": "#menu-option",
            "expected_target_identity": {
                "target_selector": "#menu-option",
                "target_text": "Option",
                "target_tag": "button",
                "target_type": "",
                "target_role": "option",
                "dispatch_path": "dom_activation",
                "page_url": "https://example.test/apply",
            },
            "expected_page_context": {
                "url": "https://example.test/apply",
                "document_lifecycle_token": "doc-1",
            },
        },
        20.0,
    )


def test_extension_body_drops_server_mask_binding_after_guarded_credential_drift() -> None:
    class CredentialDriftConn(FakeConn):
        async def call(
            self,
            method: str,
            params: dict | None = None,
            *,
            timeout: float = 30.0,
        ):
            self.calls.append((method, params, timeout))
            if method == "ensure_sensitive_screenshot_control":
                return {
                    "ok": True,
                    "action": method,
                    "selector_bound": True,
                    "page_context_bound": True,
                    "mask_surface_signature": "mask-surface-login",
                    "binding_rebound": True,
                }
            if method == "vision_type_credential_control":
                return {
                    "ok": False,
                    "action": "type",
                    "failure_reason": "sensitive_mask_page_context_drift",
                    "page_context_mismatch_field": "url",
                    "sensitive_mask_rebind_required": True,
                    "browser_mutation_attempted": False,
                }
            return {"ok": True, "method": method}

    async def scenario():
        conn = CredentialDriftConn()
        body = ExtensionBody(conn)
        await body.ensure_sensitive_screenshot_control(
            "#account",
            expected_page_context={
                "url": "https://example.test/login",
                "mask_surface_signature": "mask-surface-login",
            },
        )
        await body.vision_type_credential_control(
            "#account",
            "transient-vault-value",
            expected_target_identity={
                "target_selector": "#account",
                "target_text": "Account",
                "target_tag": "input",
                "target_type": "email",
                "target_role": "",
                "dispatch_path": "dom_activation",
                "page_url": "https://example.test/login",
            },
            expected_page_context={"url": "https://example.test/login"},
        )
        await body.capture_screenshot()
        return conn.calls

    calls = asyncio.run(scenario())
    credential_params = calls[1][1]
    assert credential_params["required_sensitive_mask_binding"]["selector"] == "#account"
    assert calls[2] == ("screenshot", {"full_page": False}, 24.0)


def test_extension_body_preserves_all_semantic_mask_selectors_across_rebind() -> None:
    class MultiRebindConn(FakeConn):
        async def call(
            self,
            method: str,
            params: dict | None = None,
            *,
            timeout: float = 30.0,
        ):
            self.calls.append((method, params, timeout))
            if method == "ensure_sensitive_screenshot_control":
                return {
                    "ok": True,
                    "action": method,
                    "selector_bound": True,
                    "page_context_bound": True,
                    "mask_surface_signature": "mask-surface-multi",
                    "bound_selectors": [
                        "#first-sensitive",
                        "#second-sensitive",
                    ],
                }
            return {"ok": True, "method": method}

    async def scenario():
        conn = MultiRebindConn()
        body = ExtensionBody(conn)
        await body.ensure_sensitive_screenshot_controls(
            ["#first-sensitive", "#second-sensitive"],
            expected_page_context={
                "url": "https://example.test/recovery",
                "mask_surface_signature": "mask-surface-multi",
            },
        )
        await body.capture_screenshot()
        return conn.calls

    calls = asyncio.run(scenario())
    assert calls[0][1]["selectors"] == [
        "#first-sensitive",
        "#second-sensitive",
    ]
    assert calls[1][1]["required_sensitive_mask_binding"] == {
        "selector": "#first-sensitive",
        "selectors": ["#first-sensitive", "#second-sensitive"],
        "expected_page_context": {
            "url": "https://example.test/recovery",
            "mask_surface_signature": "mask-surface-multi",
        },
    }


def test_extension_body_resolves_replacement_connection_by_stable_device_id() -> None:
    async def scenario():
        stale = FakeConn("dev-stable")
        replacement = FakeConn("dev-stable")
        body = ExtensionBody(stale, connection_lookup=lambda device_id: replacement if device_id == "dev-stable" else None)
        result = await body.observe()
        return result, stale.calls, replacement.calls

    result, stale_calls, replacement_calls = asyncio.run(scenario())
    assert result == {"ok": True, "method": "observe"}
    assert stale_calls == []
    assert replacement_calls == [("observe", None, 30.0)]


def test_status_endpoint_reports_no_devices_by_default() -> None:
    body = TestClient(app).get("/operator/extension/status").json()
    assert body["connected"] is False
    assert body["live_actions_ready"] is False
    assert body["devices"] == []
    assert body["connections"] == []


def test_websocket_hello_exposes_live_action_readiness() -> None:
    with TestClient(app) as client:
        with client.websocket_connect("/extension/ws?device_id=dev-current") as socket:
            assert socket.receive_json() == {
                "type": "welcome",
                "device_id": "dev-current",
            }
            socket.send_json(
                {
                    "type": "hello",
                    "device_id": "dev-current",
                    "capabilities": {
                        capability: True
                        for capability in REQUIRED_LIVE_ACTION_CAPABILITIES
                    },
                }
            )
            socket.send_json({"type": "ping"})
            status = client.get("/operator/extension/status").json()
            assert status["connected"] is True
            assert status["live_actions_ready"] is True
            assert status["connections"][0]["device_id"] == "dev-current"
            assert status["connections"][0]["missing_live_action_capabilities"] == []
