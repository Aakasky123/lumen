from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

from app import config, grounding_judge_retention
from app.agent_eval import judge_replay
from app.lumen_model import LumenModelClient

_ENTRIES = [{"source": "profile", "label": "Country", "value": "United States"}]


def _use_root(monkeypatch, root: Path) -> None:
    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(artifact_root=root))


def _verdict_reply(**metadata):
    return {
        "ok": True,
        "data": {"grounded": True, "entry_index": 0, "reason": "USA is the United States.", "support_quote": "United States"},
        "metadata": {
            "provider_id": 11,
            "model": "qwen/qwen3.8-flash",
            "latency_ms": 1234,
            "finish_reason": "stop",
            "max_tokens": 4096,
            "token_usage": {"prompt_tokens": 900, "completion_tokens": 300},
            **metadata,
        },
    }


def test_retention_writes_each_attempt_with_its_input_and_verdict(monkeypatch, tmp_path) -> None:
    _use_root(monkeypatch, tmp_path)
    monkeypatch.setattr(LumenModelClient, "_call_model_json", lambda self, **kwargs: _verdict_reply())
    client = LumenModelClient()
    client.grounding_retention_run_id = 4242

    result = client.judge_grounding(control="Country", value="USA", entries=_ENTRIES)

    assert result["grounded"] is True
    (path,) = (tmp_path / "runs" / "4242" / "judge-trajectory").glob("*.json")
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["format"] == grounding_judge_retention.RECORD_FORMAT
    assert record["run_id"] == 4242 and record["attempt"] == 1
    assert record["request"]["prompt"]["proposed_value"] == "USA"
    assert record["request"]["schema"]["name"] == "lumen_grounding_judge"
    assert record["request"]["entry_count"] == 1
    assert record["request"]["image_sent"] is False
    assert record["outcome"]["verdict"] == {
        "grounded": True,
        "entry_index": 0,
        "reason": "USA is the United States.",
        "support_quote": "United States",
    }
    assert record["outcome"]["latency_ms"] == 1234
    assert record["outcome"]["usage"]["completion_tokens"] == 300
    assert record["provider"]["model"] == "qwen/qwen3.8-flash"


def test_retention_stores_an_attached_screenshot_beside_the_record(monkeypatch, tmp_path) -> None:
    raw = b"\xff\xd8fake-jpeg"
    image = {"media_type": "image/jpeg", "data": base64.b64encode(raw).decode("ascii")}
    out = grounding_judge_retention.retain_attempt(
        7,
        judge_call_id="abc",
        attempt=2,
        system="s",
        prompt={"proposed_value": "3"},
        schema={"name": "x"},
        provider=None,
        max_tokens=8192,
        timeout_seconds=40,
        image=image,
        image_sent=True,
        entry_count=1,
        result=_verdict_reply(image_included=True),
        root=tmp_path,
    )
    record = json.loads(Path(out["path"]).read_text(encoding="utf-8"))
    (ref,) = record["request"]["images"]
    (loaded,) = grounding_judge_retention.load_images({**record, "_dir": str(Path(out["path"]).parent)})
    assert base64.b64decode(loaded["data"]) == raw
    assert ref["bytes"] == len(raw)


def test_retention_never_raises_into_the_judge(monkeypatch, tmp_path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    _use_root(monkeypatch, blocker)  # runs/ cannot be created under a file
    monkeypatch.setattr(LumenModelClient, "_call_model_json", lambda self, **kwargs: _verdict_reply())
    client = LumenModelClient()
    client.grounding_retention_run_id = 1
    assert client.judge_grounding(control="Country", value="USA", entries=_ENTRIES)["grounded"] is True

    def explode(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(grounding_judge_retention, "retain_attempt", explode)
    assert client.judge_grounding(control="Country", value="USA", entries=_ENTRIES)["grounded"] is True


def test_no_run_id_retains_nothing(monkeypatch, tmp_path) -> None:
    _use_root(monkeypatch, tmp_path)
    monkeypatch.setattr(LumenModelClient, "_call_model_json", lambda self, **kwargs: _verdict_reply())
    LumenModelClient().judge_grounding(control="Country", value="USA", entries=_ENTRIES)
    assert not (tmp_path / "runs").exists()


def _record(path: str, grounded: bool, entry_index: int, entry_count: int = 3) -> dict:
    return {
        "format": grounding_judge_retention.RECORD_FORMAT,
        "_path": path,
        "run_id": 1,
        "request": {"system": "s", "prompt": {}, "schema": {}, "entry_count": entry_count, "image_sent": False},
        "provider": {"model": "m"},
        "outcome": {"ok": True, "verdict": {"grounded": grounded, "entry_index": entry_index, "reason": "r"}},
    }


def test_replay_scorer_counts_agreement_and_flips_with_a_fake_transport() -> None:
    records = [
        _record("a", True, 0),  # candidate agrees exactly
        _record("b", True, 1),  # candidate grounded on another entry
        _record("c", True, 2),  # candidate says not grounded (G->N)
        _record("d", False, -1),  # candidate says grounded (N->G, unsafe)
        _record("e", False, -1),  # candidate reply malformed
        _record("f", False, -1),  # candidate grounded with an out-of-range index -> not grounded, agrees
        {**_record("g", True, 0), "outcome": {"ok": False, "verdict": {}}},  # no recorded verdict: skipped
    ]
    replies = {
        "a": {"grounded": True, "entry_index": 0, "reason": "r"},
        "b": {"grounded": True, "entry_index": 2, "reason": "r"},
        "c": {"grounded": False, "entry_index": -1, "reason": "r"},
        "d": {"grounded": True, "entry_index": 1, "reason": "r"},
        "e": {"grounded": "yes"},
        "f": {"grounded": True, "entry_index": 9, "reason": "r"},
    }

    def transport(provider, record, images):
        return {
            "ok": True,
            "data": replies[record["_path"]],
            "metadata": {"latency_ms": 100 * (ord(record["_path"]) - 96), "token_usage": {"completion_tokens": 50}},
        }

    rows, skipped = judge_replay.run_replay(records, [("cand", object())], transport, repeat=2, workers=2)
    assert skipped == 1
    (summary,) = judge_replay.summarize(rows)
    assert summary["n"] == 12
    assert summary["valid"] == 10
    assert summary["invalid"] == 2
    assert summary["grounded_agree"] == 6  # a, b, f per repeat
    assert summary["entry_agree"] == 4  # a, f per repeat
    assert summary["flips_grounded_to_not"] == 2
    assert summary["flips_not_to_grounded"] == 2
    assert summary["completion_tokens_mean"] == 50
    assert summary["latency_p50_ms"] == 300.0


def test_replay_counts_provider_errors_and_exceptions_as_invalid() -> None:
    records = [_record("a", True, 0), _record("b", True, 0)]

    def transport(provider, record, images):
        if record["_path"] == "a":
            return {"ok": False, "error": "timeout", "metadata": {}}
        raise RuntimeError("boom")

    rows, _ = judge_replay.run_replay(records, [("cand", object())], transport)
    (summary,) = judge_replay.summarize(rows)
    assert summary["invalid"] == 2 and summary["valid"] == 0
    assert {row["outcome"] for row in rows} == {"provider_error", "exception"}


def test_parse_candidate_overrides_effort_and_structured_mode() -> None:
    from dataclasses import dataclass

    @dataclass
    class Row:
        model_name: str = "qwen/qwen3.8-flash"
        display_name: str = "judge"
        reasoning_effort: str | None = "medium"
        structured_output: str | None = "strict"

    label, row = judge_replay.parse_candidate("qwen/qwen3.8-flash@low:json_object", Row())
    assert label == "qwen/qwen3.8-flash@low:json_object"
    assert (row.model_name, row.reasoning_effort, row.structured_output) == ("qwen/qwen3.8-flash", "low", "json_object")
    _, same = judge_replay.parse_candidate("other/model", Row())
    assert (same.reasoning_effort, same.structured_output) == ("medium", "strict")


def test_load_records_filters_by_run(tmp_path) -> None:
    for run_id in (5, 6):
        out = grounding_judge_retention.retain_attempt(
            run_id,
            judge_call_id="x",
            attempt=1,
            system="s",
            prompt={},
            schema={},
            provider=None,
            max_tokens=1,
            timeout_seconds=1,
            image=None,
            image_sent=False,
            entry_count=1,
            result=_verdict_reply(),
            root=tmp_path,
        )
        assert out is not None
    assert [r["run_id"] for r in grounding_judge_retention.load_records(tmp_path, run_ids=[6])] == [6]
    assert len(grounding_judge_retention.load_records(tmp_path, days=1)) == 2


# ---------------------------------------------------------------- variants, legs, routing, budget

from app.agent_eval import judge_packet_variants as variants  # noqa: E402


def _judge_request() -> dict:
    return {
        "system": "You are the grounding judge. Return strict JSON only.",
        "prompt": {
            "task": "Does any entry support the value?",
            "rules": ["Grade support only.", "instrumental: ..."],
            "control": "Country",
            "proposed_value": "USA",
            "entries": _ENTRIES,
            "response_shape": {
                "reason": "one sentence",
                "grounded": "boolean",
                "support_quote": "words",
                "entry_index": "index",
                "instrumental": "bool",
            },
            "untrusted_interaction_context": {},
            "page_evidence": "page text",
        },
        "schema": {
            "name": "lumen_grounding_judge",
            "schema": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                    "grounded": {"type": "boolean"},
                    "support_quote": {"type": "string"},
                    "commits_or_destroys": {"type": "boolean"},
                    "entry_index": {"type": "integer"},
                    "extra": {"type": "string"},
                },
                "required": ["reason", "grounded", "support_quote", "commits_or_destroys", "entry_index", "extra"],
            },
        },
        "entry_count": 1,
        "image_sent": False,
    }


def test_baseline_and_reasoning_off_leave_the_packet_unchanged() -> None:
    request = _judge_request()
    same, forced = variants.apply_variant("baseline", request)
    assert same == request and same is not request and forced == {"reasoning_enabled": None, "reasoning": None}
    same, forced = variants.apply_variant("reasoning_off", request)
    assert same == request and forced == {"reasoning_enabled": False, "reasoning": None}
    same, forced = variants.apply_variant("reasoning_budget_1024", request)
    assert same == request and forced == {"reasoning_enabled": None, "reasoning": {"max_tokens": 1024}}


def test_verdict_first_reorders_schema_and_shape_and_caps_reason_but_keeps_rules() -> None:
    request = _judge_request()
    out, forced = variants.apply_variant("verdict_first", request)
    body = out["schema"]["schema"]
    assert list(body["properties"]) == ["grounded", "entry_index", "commits_or_destroys", "support_quote", "extra", "reason"]
    assert body["required"] == ["grounded", "entry_index", "commits_or_destroys", "support_quote", "extra", "reason"]
    assert body["properties"]["reason"]["description"] == "at most 20 words"
    shape = out["prompt"]["response_shape"]
    assert list(shape) == ["grounded", "entry_index", "instrumental", "support_quote", "reason"]
    assert shape["reason"] == "at most 20 words"
    assert out["prompt"]["rules"] == request["prompt"]["rules"]
    assert out["system"] == request["system"] and forced["reasoning_enabled"] is None
    assert list(request["prompt"]["response_shape"])[0] == "reason"  # the retained request is not mutated


def test_terse_appends_one_rule_on_top_of_verdict_first() -> None:
    request = _judge_request()
    out, _ = variants.apply_variant("terse", request)
    assert out["prompt"]["rules"] == [*request["prompt"]["rules"], variants.TERSE_RULE]
    assert list(out["prompt"]["response_shape"])[-1] == "reason"
    assert "do not deliberate at length" in variants.TERSE_RULE and "at most 20 words" in variants.TERSE_RULE


def test_stable_first_moves_static_keys_ahead_of_per_call_keys() -> None:
    request = _judge_request()
    out, _ = variants.apply_variant("stable_first", request)
    assert list(out["prompt"]) == [
        "task", "rules", "response_shape", "control", "proposed_value", "entries", "untrusted_interaction_context", "page_evidence",
    ]
    # The wire keeps the packet's key order (json.dumps without sort_keys).
    from app.prompt_cache import user_wire_parts

    stable, tail = user_wire_parts(out["system"], out["prompt"])
    text = stable + tail
    assert text.index('"response_shape"') < text.index('"control"')


def test_variants_compose_and_unknown_names_fail() -> None:
    import pytest

    out, forced = variants.apply_variant("terse+stable_first+reasoning_off", _judge_request())
    assert forced["reasoning_enabled"] is False and list(out["prompt"])[:3] == ["task", "rules", "response_shape"]
    for bad in ("fastest", "reasoning_budget_0", "reasoning_budget_x"):
        with pytest.raises(ValueError):
            variants.parse_variant(bad)
    assert out["prompt"]["rules"][-1] == variants.TERSE_RULE
    with pytest.raises(ValueError):
        variants.parse_variant("fastest")


def test_replay_sends_the_variant_packet_and_forces_reasoning_off_in_context() -> None:
    from app import model_provider

    record = {**_record("a", False, -1, entry_count=1), "request": _judge_request()}
    seen = []

    def transport(provider, sent, images):
        seen.append((sent["request"], model_provider._EVAL_TRANSPORT_OVERRIDES.get()))
        return {
            "ok": True,
            "data": {"grounded": False, "entry_index": -1, "reason": "no support"},
            "metadata": {
                "model": "qwen/qwen3.8-flash",
                "latency_ms": 10,
                "finish_reason": "length",
                "reasoning_enabled_at_transport": False,
                "token_usage": {
                    "prompt_tokens": 1_000_000,
                    "completion_tokens": 1_000_000,
                    "completion_tokens_details": {"reasoning_tokens": 700},
                    "cost": 0.5,
                },
            },
        }

    legs = [
        judge_replay.Leg("off", object(), variant="reasoning_off", route={"sort": "throughput"}, route_label="sort=throughput"),
        judge_replay.Leg("terse", object(), variant="terse"),
    ]
    rows, _ = judge_replay.run_replay([record], legs, transport, workers=1)
    (off_request, off_override), (terse_request, terse_override) = seen
    assert off_override == {"reasoning_enabled": False, "reasoning": None, "routing": {"sort": "throughput"}}
    assert terse_override == {"reasoning_enabled": None, "reasoning": None, "routing": {}}
    assert off_request == record["request"]
    assert terse_request["prompt"]["rules"][-1] == variants.TERSE_RULE
    assert model_provider._EVAL_TRANSPORT_OVERRIDES.get() is None  # nothing leaks out of the call
    off_row = rows[0]
    assert off_row["reasoning_tokens"] == 700 and off_row["length_capped"] is True
    assert round(off_row["estimated_cost_usd"], 6) == 0.62 and off_row["reported_cost_usd"] == 0.5
    off_summary, terse_summary = judge_replay.summarize(rows)
    assert off_summary["model"] == "off" and off_summary["reasoning_tokens_mean"] == 700
    assert off_summary["length_capped"] == 1 and off_summary["reasoning_enabled_at_transport"] == {"False": 1}
    assert terse_summary["prompt_key_order_changed"] == 0


def test_unsafe_flips_list_the_record_and_both_reasons() -> None:
    record = {
        **_record("rec.json", False, -1),
        "outcome": {"ok": True, "verdict": {"grounded": False, "entry_index": -1, "reason": "nothing says so"}},
    }

    def transport(provider, sent, images):
        return {"ok": True, "data": {"grounded": True, "entry_index": 0, "reason": "the task implies it"}, "metadata": {}}

    rows, _ = judge_replay.run_replay([record], [("cand", object())], transport)
    assert judge_replay.unsafe_flips(rows) == [
        {
            "leg": "cand",
            "record": "rec.json",
            "gold_id": None,
            "repeat": 0,
            "candidate_entry_index": 0,
            "recorded_reason": "nothing says so",
            "candidate_reason": "the task implies it",
        }
    ]


def _openrouter_row():
    from app.model_provider import ResolvedModelProvider

    return ResolvedModelProvider(
        id=11, display_name="judge", provider_type="openrouter", model_name="qwen/qwen3.8-flash",
        base_url="https://openrouter.example.test/api/v1", api_key="test-only-key", temperature=0.0,
        max_tokens=128, timeout_seconds=5, enabled=True, is_active=True, source="database",
        reasoning_effort="low", structured_output="json_object", endpoint_preference="alibaba",
    )


def test_transport_override_turns_reasoning_off_and_routes_with_require_parameters_kept(monkeypatch) -> None:
    import httpx

    from app import model_provider

    monkeypatch.setattr(model_provider, "_openrouter_catalog", lambda base_url: ({}, {}, {}))
    bodies = []

    def post(url, **kwargs):
        bodies.append(kwargs["json"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}], "usage": {}}, request=httpx.Request("POST", url))

    monkeypatch.setattr(model_provider, "_post_with_transient_retry", post)
    row = _openrouter_row()
    request = _judge_request()

    def call():
        return model_provider.call_model_json(
            row, purpose="grounding_judge", system=request["system"], prompt=request["prompt"], schema=request["schema"]
        )

    plain = call()
    with model_provider.eval_transport_overrides(
        reasoning_enabled=False, routing={"order": ["HostA", "HostB"], "require_parameters": False}
    ):
        forced = call()
    with model_provider.eval_transport_overrides(routing={"sort": "throughput"}):
        sorted_call = call()
    with model_provider.eval_transport_overrides(reasoning={"max_tokens": 512}):
        call()
    assert bodies[3]["reasoning"] == {"max_tokens": 512} and bodies[3]["max_tokens"] == bodies[0]["max_tokens"]
    call()  # nothing persists past the context
    assert bodies[4] == bodies[0]
    assert bodies[0]["reasoning"] == {"effort": "low"}
    assert bodies[0]["provider"] == {"require_parameters": True, "order": ["alibaba"]}
    assert bodies[1]["reasoning"] == {"enabled": False}
    assert bodies[1]["provider"] == {"require_parameters": True, "order": ["HostA", "HostB"]}
    assert bodies[2]["reasoning"] == {"effort": "low"}
    assert bodies[2]["provider"] == {"require_parameters": True, "sort": "throughput"}
    assert plain["metadata"]["reasoning_enabled_at_transport"] is None
    assert forced["metadata"]["reasoning_enabled_at_transport"] is False
    assert sorted_call["ok"]


def test_parse_route_and_build_legs() -> None:
    import pytest

    assert judge_replay.parse_route("default") is None
    assert judge_replay.parse_route("sort=Throughput") == {"sort": "throughput"}
    assert judge_replay.parse_route("order=alibaba,deepinfra/fp8;allow_fallbacks=false") == {
        "order": ["alibaba", "deepinfra/fp8"],
        "allow_fallbacks": False,
    }
    for bad in ("require_parameters=false", "sort=fastest", "order=", "speed=1", "order=bad host"):
        with pytest.raises(ValueError):
            judge_replay.parse_route(bad)
    legs = judge_replay.build_legs(
        _openrouter_row(),
        candidates=["qwen/qwen3.8-flash@none"],
        include_champion=True,
        variants=["baseline", "terse"],
        routes=["default", "sort=throughput"],
    )
    assert len(legs) == 8
    assert legs[0].label == "champion:qwen/qwen3.8-flash@low|baseline"
    assert legs[1].label == "champion:qwen/qwen3.8-flash@low|baseline|sort=throughput"
    assert legs[1].route == {"sort": "throughput"}
    assert legs[-1].candidate == "qwen/qwen3.8-flash@none" and legs[-1].provider.reasoning_effort == "none"


def test_records_load_from_a_manifest_and_a_directory(tmp_path) -> None:
    paths = []
    for run_id in (5, 6):
        out = grounding_judge_retention.retain_attempt(
            run_id, judge_call_id="x", attempt=1, system="s", prompt={}, schema={}, provider=None,
            max_tokens=1, timeout_seconds=1, image=None, image_sent=False, entry_count=1,
            result=_verdict_reply(), root=tmp_path / "corpus",
        )
        paths.append(out["path"])
    (tmp_path / "corpus" / "noise.json").write_text(json.dumps({"format": "other"}), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps([paths[0], str(Path(paths[1]).relative_to(tmp_path)), str(tmp_path / "missing.json")]), encoding="utf-8"
    )
    from_manifest = judge_replay.load_records_from_manifest(manifest)
    assert sorted(record["run_id"] for record in from_manifest) == [5, 6]
    from_dir = judge_replay.load_records_from_dir(tmp_path / "corpus")
    assert sorted(record["run_id"] for record in from_dir) == [5, 6]
    merged = judge_replay.filter_records(from_manifest + from_dir)
    assert len(merged) == 2
    assert [record["run_id"] for record in judge_replay.filter_records(merged, run_ids=[6])] == [6]
    assert all(Path(record["_dir"]) == Path(record["_path"]).parent for record in merged)


def _paid_transport():
    def transport(provider, sent, images):
        return {
            "ok": True,
            "data": {"grounded": True, "entry_index": 0, "reason": "r"},
            "metadata": {"model": "qwen/qwen3.8-flash", "token_usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0}},
        }

    return transport


def test_budget_guard_aborts_under_the_credit_floor_before_any_call() -> None:
    calls = []
    guard = judge_replay.BudgetGuard(lambda: {"total_credits": 10.0, "total_usage": 7.5}, floor=3.0)

    def transport(provider, sent, images):
        calls.append(1)
        return {}

    rows, _ = judge_replay.run_replay([_record("a", True, 0)], [("cand", object())], transport, budget=guard)
    assert rows == [] and calls == []
    assert "under the $3.00 floor" in guard.abort_reason and guard.remaining == 2.5


def test_budget_guard_fails_closed_when_the_credit_check_fails() -> None:
    def broken():
        raise RuntimeError("403")

    guard = judge_replay.BudgetGuard(broken)
    rows, _ = judge_replay.run_replay([_record("a", True, 0)], [("cand", object())], _paid_transport(), budget=guard)
    assert rows == [] and guard.abort_reason.startswith("credit check failed")


def test_budget_guard_rechecks_credits_every_n_calls_and_stops_at_max_spend() -> None:
    checks = []

    def credits():
        checks.append(1)
        return {"total_credits": 100.0, "total_usage": 1.0}

    records = [_record(chr(97 + index), True, 0) for index in range(10)]
    guard = judge_replay.BudgetGuard(credits, check_every=3)
    rows, _ = judge_replay.run_replay(records, [("cand", object())], _paid_transport(), workers=1, budget=guard)
    assert len(rows) == 10 and len(checks) == 4 and guard.abort_reason is None
    assert round(guard.spent, 6) == 1.5  # 10 x 1M prompt tokens x $0.15/M

    guard = judge_replay.BudgetGuard(credits, max_spend=0.4, check_every=50)
    rows, _ = judge_replay.run_replay(records, [("cand", object())], _paid_transport(), workers=1, budget=guard)
    assert len(rows) == 3  # 0.15, 0.30, 0.45 > 0.40: no fourth call starts
    assert "exceeded --max-spend" in guard.abort_reason


def test_credits_fetcher_sends_the_key_only_in_the_authorization_header() -> None:
    sent = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": {"total_credits": 25, "total_usage": 4.5}}

    def http_get(url, **kwargs):
        sent["url"], sent["kwargs"] = url, kwargs
        return Response()

    fetch = judge_replay.openrouter_credits_fetcher("sk-or-secret", http_get=http_get)
    assert fetch() == {"total_credits": 25.0, "total_usage": 4.5}
    assert sent["url"] == "https://openrouter.ai/api/v1/credits" and "secret" not in sent["url"]
    assert sent["kwargs"]["headers"] == {"Authorization": "Bearer sk-or-secret"}
    assert set(sent["kwargs"]) == {"headers", "timeout"}


def test_manifest_rows_skip_unusable_and_repeat_attempts(tmp_path) -> None:
    out = grounding_judge_retention.retain_attempt(
        5, judge_call_id="x", attempt=1, system="s", prompt={}, schema={}, provider=None,
        max_tokens=1, timeout_seconds=1, image=None, image_sent=False, entry_count=1,
        result=_verdict_reply(), root=tmp_path,
    )
    manifest = tmp_path / "m.json"
    manifest.write_text(
        json.dumps(
            {
                "records": [
                    {"path": out["path"], "replay_usable": True, "first_verdict_of_packet": True},
                    {"path": out["path"], "replay_usable": False, "first_verdict_of_packet": True},
                    {"path": out["path"], "replay_usable": True, "first_verdict_of_packet": False},
                ]
            }
        ),
        encoding="utf-8",
    )
    assert len(judge_replay.load_records_from_manifest(manifest)) == 1
    assert len(judge_replay.load_records_from_manifest(manifest, all_attempts=True)) == 2


def _write_gold_set(root: Path) -> Path:
    image = root / "shot.jpg"
    image.write_bytes(b"\xff\xd8gold")
    gold = root / "grounding-viewtoggle"
    (gold / "new").mkdir(parents=True)
    request = {k: v for k, v in _judge_request().items() if k in ("system", "prompt", "schema")}
    for number, label in ((1, "must_be_false"), (2, "may_be_true")):
        case = {"case": number, "id": f"replay-{number:03d}", "expected": {"grounded": label, "note": f"note {number}"}, "request": {**request, "images": [str(image)]}}
        (gold / "new" / f"case-{number:03d}.json").write_text(json.dumps(case), encoding="utf-8")
    (gold / "index.json").write_text(
        json.dumps([{"case": 1, "file": "case-001.json", "grounded": "must_be_false"}, {"case": 2, "file": "case-002.json", "grounded": "may_be_true"}]),
        encoding="utf-8",
    )
    second = root / "grounding-second-opinion"
    (second / "cases").mkdir(parents=True)
    case = {"case": 1, "id": "so-1", "expected": {"second_judge_may_accept": False}, "request": {**request, "images": []}}
    (second / "cases" / "case-001.json").write_text(json.dumps(case), encoding="utf-8")
    (second / "index.json").write_text(json.dumps([{"case": 1, "file": "cases/case-001.json", "second_judge_may_accept": False}]), encoding="utf-8")
    return root


def test_gold_dir_cases_replay_and_count_grounded_on_must_be_false_as_unsafe(tmp_path) -> None:
    root = _write_gold_set(tmp_path)
    records = judge_replay.load_gold_dir(root / "grounding-viewtoggle") + judge_replay.load_gold_dir(root / "grounding-second-opinion")
    assert [record["_gold"]["must_be_false"] for record in records] == [True, False, True]
    first = records[0]
    assert first["request"]["entry_count"] == 1 and first["request"]["image_sent"] is True
    assert first["request"]["max_tokens"] is None  # the judge's production budget applies
    seen_images = []

    def transport(provider, sent, images):
        seen_images.append(len(images))
        return {"ok": True, "data": {"grounded": True, "entry_index": 0, "reason": "looks fine"}, "metadata": {}}

    filtered = judge_replay.filter_records(records, run_ids=[99], days=1)  # gold ignores run/day filters
    rows, skipped = judge_replay.run_replay(filtered, [("cand", object())], transport, workers=1)
    assert skipped == 0 and len(rows) == 3 and seen_images == [1, 1, 0]
    (summary,) = judge_replay.summarize(rows)
    assert summary["gold_must_be_false"] == 2 and summary["gold_unsafe"] == 2
    assert summary["flips_not_to_grounded"] == 2  # the may-be-true case is recorded, never scored
    flips = judge_replay.unsafe_flips(rows)
    assert {flip["gold_id"] for flip in flips} == {"replay-001", "so-1"}
    assert flips[0]["recorded_reason"].startswith("gold must_be_false")


def test_stratified_sample_keeps_every_not_grounded_record() -> None:
    records = [_record(f"g{index}", True, 0) for index in range(10)] + [_record(f"n{index}", False, -1) for index in range(4)]
    sample = judge_replay.stratified_sample(records, 3, seed=7)
    assert sum(1 for record in sample if record["outcome"]["verdict"]["grounded"]) == 3
    assert sum(1 for record in sample if not record["outcome"]["verdict"]["grounded"]) == 4
    assert sample == judge_replay.stratified_sample(records, 3, seed=7)
    assert judge_replay.stratified_sample(records, 50, seed=1) == records
