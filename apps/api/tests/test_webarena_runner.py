from app.agent_eval.contract import BrowserAction
from app.agent_eval.webarena_runner import (
    final_response_from_action,
    parse_webarena_base_url_overrides,
    render_webarena_url,
    reset_webarena_environment,
    select_manifest_tasks,
    webarena_run_config,
)


def test_render_webarena_url_replaces_placeholder_only() -> None:
    assert render_webarena_url("__SHOPPING_ADMIN__/sales/order") == "http://localhost:7780/admin/sales/order"
    assert render_webarena_url("http://example.test/page") == "http://example.test/page"


def test_parse_base_url_overrides_accepts_site_name_or_placeholder() -> None:
    urls = parse_webarena_base_url_overrides(["SHOPPING_ADMIN=http://127.0.0.1:9000", "__REDDIT__=http://reddit.local"])
    assert urls["__SHOPPING_ADMIN__"] == "http://127.0.0.1:9000"
    assert urls["__REDDIT__"] == "http://reddit.local"


def test_webarena_run_config_uses_site_enum_keys_for_evaluator() -> None:
    config = webarena_run_config({"__SHOPPING_ADMIN__": "http://localhost:7780/admin"})
    assert config["environments"]["shopping_admin"]["urls"] == ["http://localhost:7780/admin", "http://localhost:7780"]
    assert config["environments"]["shopping_admin"]["use_header_login"] is True
    assert config["trace_file_name"] == "network.har"


def test_select_manifest_tasks_filters_external_ids() -> None:
    tasks = select_manifest_tasks([{"external_id": "11"}, {"external_id": "15"}], task_ids="15")
    assert tasks == [{"external_id": "15"}]


def test_final_response_accepts_full_json_from_model() -> None:
    action = BrowserAction(
        kind="stop",
        value='{"task_type":"RETRIEVE","status":"SUCCESS","retrieved_data":[6],"error_details":null}',
    )
    assert final_response_from_action(action)["retrieved_data"] == [6]


def test_final_response_accepts_python_literal_dict_from_model() -> None:
    action = BrowserAction(
        kind="stop",
        value="{'task_type': 'RETRIEVE', 'status': 'SUCCESS', 'retrieved_data': ['hollister', 'Joust Bag'], 'error_details': None}",
    )
    assert final_response_from_action(action)["retrieved_data"] == ["hollister", "Joust Bag"]


def test_final_response_wraps_plain_retrieved_value() -> None:
    action = BrowserAction(kind="stop", value="6")
    response = final_response_from_action(action, task_type="RETRIEVE")
    assert response == {
        "task_type": "RETRIEVE",
        "status": "SUCCESS",
        "retrieved_data": [6],
        "error_details": None,
    }


def test_final_response_serializes_blocked_state() -> None:
    response = final_response_from_action(None, task_type="NAVIGATE", error="login required")
    assert response["task_type"] == "NAVIGATE"
    assert response["status"] == "UNKNOWN_ERROR"
    assert response["retrieved_data"] is None


def test_final_response_serializes_empty_error_as_error() -> None:
    response = final_response_from_action(BrowserAction(kind="takeover", value="login required"), error="")
    assert response["status"] == "UNKNOWN_ERROR"
    assert response["error_details"] == "task did not complete"


def test_reset_webarena_environment_posts_init(monkeypatch) -> None:
    captured = {}

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        @staticmethod
        def read() -> bytes:
            return b'{"success": true}'

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    reset_webarena_environment("http://localhost:7781/")
    assert captured == {"url": "http://localhost:7781/init", "method": "POST", "timeout": 120}
