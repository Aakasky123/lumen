from __future__ import annotations

from pathlib import Path

from app.agent_eval.interaction_bench import live_run


def test_stale_mission_folders_are_archived_not_deleted(tmp_path: Path) -> None:
    root = tmp_path / "LumenBench"
    (root / "v3-paperwork-12029c" / "inbox").mkdir(parents=True)
    (root / "v3-paperwork-12029c" / "inbox" / "scan.txt").write_text("kept", encoding="utf-8")
    (root / "v3-intake-aa11bb").mkdir()
    (root / "notes").mkdir()  # not a mission folder: left alone

    moved = live_run.archive_stale_workspaces(root)

    assert moved == 2
    assert sorted(p.name for p in root.iterdir()) == ["notes"]
    archived = list((tmp_path / "LumenBench-archive").glob("*/v3-paperwork-12029c/inbox/scan.txt"))
    assert archived and archived[0].read_text(encoding="utf-8") == "kept"
    assert live_run.archive_stale_workspaces(root) == 0
    assert live_run.archive_stale_workspaces(tmp_path / "missing") == 0


def test_a_folder_that_cannot_be_archived_stops_the_run(tmp_path: Path, monkeypatch) -> None:
    import pytest

    root = tmp_path / "LumenBench"
    (root / "v3-paperwork-1705fa").mkdir(parents=True)
    (root / "v3-paperwork-aa11bb").mkdir()
    real_rename = Path.rename

    (root / "v3-paperwork-1705fa" / "inbox").mkdir()
    (root / "v3-paperwork-1705fa" / "ledger.txt").write_text("x", encoding="utf-8")

    def rename(self, target):
        if self.name in {"v3-paperwork-1705fa", "ledger.txt"}:
            raise PermissionError(5, "Access is denied")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", rename)
    with pytest.raises(live_run.StaleWorkspaceError) as caught:
        live_run.archive_stale_workspaces(root)
    assert "v3-paperwork-1705fa" in str(caught.value) and "ledger.txt" in str(caught.value)
    assert not (root / "v3-paperwork-aa11bb").exists(), "the others are still archived"


def test_a_held_folder_is_emptied_into_the_archive(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "LumenBench"
    (root / "v3-paperwork-015832" / "inbox").mkdir(parents=True)
    (root / "v3-paperwork-015832" / "inbox" / "scan.png").write_bytes(b"png")
    real_rename = Path.rename

    def rename(self, target):
        if self.name == "v3-paperwork-015832":
            raise PermissionError(5, "Access is denied")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", rename)
    assert live_run.archive_stale_workspaces(root) == 1
    assert list((root / "v3-paperwork-015832").iterdir()) == [], "only the empty held folder remains"
    assert list((tmp_path / "LumenBench-archive").glob("*/v3-paperwork-015832/inbox/scan.png"))


def test_model_usage_counts_decisions_per_model_and_keeps_unknown_cost_unknown() -> None:
    receipt = lambda model, cost: {"event_type": "VISION_MODEL_TRANSPORT_RECEIPT", "event_payload": {"model": model, "cost_usd": cost}}
    events = [
        receipt("deepseek/deepseek-v4.1-flash", 0.001),
        receipt("deepseek/deepseek-v4.1-flash", 0.002),
        {"event_type": "VISION_BRAIN_ESCALATED", "event_payload": {"reason": "repeated_failures"}},
        receipt("z-ai/glm-5.3-flash", 0.004),
        {"event_type": "VISION_BRAIN_DEESCALATED", "event_payload": {"reason": "durable_progress"}},
    ]

    usage = live_run._model_usage(events)

    assert usage["decisions_by_model"] == {"deepseek/deepseek-v4.1-flash": 2, "z-ai/glm-5.3-flash": 1}
    assert usage["decision_cost_usd"] == 0.007
    assert usage["judge_cost_usd"] is None
    assert (usage["escalations"], usage["deescalations"], usage["provider_failovers"]) == (1, 1, 0)

    # One decision without a cost receipt makes the total unknown rather than an undercount.
    assert live_run._model_usage([*events, receipt("z-ai/glm-5.3-flash", None)])["decision_cost_usd"] is None
    # No RUN_MODEL_CALLS tally: judge cost is unknown, not zero.
    assert usage["judge_cost_usd"] is None


def test_model_usage_sums_judge_calls_across_loop_sessions() -> None:
    tally = lambda purposes: {"event_type": "RUN_MODEL_CALLS", "event_payload": {"by_purpose": purposes}}
    events = [
        tally({"vision_agent_step": {"calls": 4, "failed": 0, "latency_ms": 9000, "cost_usd": 0.004},
               "payment_floor_judge": {"calls": 2, "failed": 0, "latency_ms": 3000, "cost_usd": 0.0002}}),
        tally({"payment_floor_judge": {"calls": 1, "failed": 1, "latency_ms": 1500, "cost_usd": 0.0001},
               "blocked_click_semantics": {"calls": 3, "failed": 0, "latency_ms": 2400, "cost_usd": 0.0006}}),
    ]

    usage = live_run._model_usage(events)

    assert usage["calls_by_purpose"]["payment_floor_judge"] == {"calls": 3, "failed": 1, "latency_ms": 4500, "cost_usd": 0.0003}
    assert usage["judge_cost_usd"] == 0.0009  # decisions are counted separately
    unknown = live_run._model_usage([*events, tally({"grounding_judge": {"calls": 1, "cost_usd": None}})])
    assert unknown["judge_cost_usd"] is None
    # A hedged decision's extra reply is decision spend, never judge spend.
    hedged = live_run._model_usage([*events, tally({"vision_agent_step:hedge_extra": {"calls": 2, "cost_usd": 0.004}})])
    assert hedged["judge_cost_usd"] == 0.0009
    assert hedged["hedge_extra_cost_usd"] == 0.004


def test_the_next_page_waits_until_a_cancelled_run_has_let_go_of_the_body(monkeypatch) -> None:
    states = iter(["RUNNING", "RUNNING", "CANCELLED"])
    current = {"state": "RUNNING"}
    cancels: list[int] = []

    def fake_request(method, path, body=None, timeout=30.0):
        if method == "POST" and path.endswith("/cancel"):
            cancels.append(int(path.split("/")[-2]))
            return {}
        current["state"] = next(states, "CANCELLED")
        return [{"id": 1607, "current_state": current["state"]}]

    monkeypatch.setattr(live_run, "_request", fake_request)
    monkeypatch.setattr(live_run.time, "sleep", lambda seconds: None)

    live_run._clear_active_runs()

    assert cancels == [1607]
    assert current["state"] == "CANCELLED", "returned only once the run had stopped"


def test_closing_bench_tabs_sends_the_local_admin_header(monkeypatch) -> None:
    import io
    import json

    seen: list[tuple[str, str, dict]] = []

    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout=0):
        seen.append((request.get_method(), request.full_url, dict(request.header_items())))
        return _Response(json.dumps({"ok": True, "closed": 2}).encode("utf-8"))

    monkeypatch.setattr(live_run.urllib.request, "urlopen", fake_urlopen)

    live_run._close_bench_tabs()

    assert len(seen) == 1
    method, url, headers = seen[0]
    assert method == "POST" and url.endswith("/operator/extension/close-bench-tabs")
    assert {key.lower(): value for key, value in headers.items()}.get("x-lumen-local") == "1"
