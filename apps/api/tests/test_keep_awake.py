import asyncio

import pytest

from app import extension_run, keep_awake


@pytest.fixture
def fake_power(monkeypatch):
    calls: list[tuple[str, int]] = []
    handles = iter(range(101, 200))

    def create() -> int:
        handle = next(handles)
        calls.append(("create", handle))
        return handle

    def clear(handle: int) -> None:
        calls.append(("clear", handle))

    monkeypatch.setattr(keep_awake, "_enabled", lambda: True)
    monkeypatch.setattr(keep_awake, "_create_request", create)
    monkeypatch.setattr(keep_awake, "_clear_request", clear)
    monkeypatch.setattr(keep_awake, "_holders", 0)
    monkeypatch.setattr(keep_awake, "_request", None)
    return calls


def test_overlapping_runs_share_one_request_until_the_last_releases(fake_power) -> None:
    with keep_awake.keep_awake():
        with keep_awake.keep_awake():
            assert keep_awake.holding()
        assert keep_awake.holding()
    assert not keep_awake.holding()
    assert fake_power == [("create", 101), ("clear", 101)]

    with keep_awake.keep_awake():
        pass
    assert fake_power[-2:] == [("create", 102), ("clear", 102)]


def test_disabled_keep_awake_never_touches_the_os(fake_power, monkeypatch) -> None:
    monkeypatch.setattr(keep_awake, "_enabled", lambda: False)
    with keep_awake.keep_awake():
        assert not keep_awake.holding()
    assert fake_power == []


def test_refused_power_request_never_fails_the_run(fake_power, monkeypatch) -> None:
    def refuse() -> int:
        raise OSError("refused")

    monkeypatch.setattr(keep_awake, "_create_request", refuse)
    with keep_awake.keep_awake():
        assert not keep_awake.holding()
    assert keep_awake._holders == 0


def test_vision_loop_holds_the_machine_awake_only_while_it_runs(fake_power, monkeypatch) -> None:
    seen: list[bool] = []

    async def fake_loop(body, **kwargs):
        seen.append(keep_awake.holding())
        return {"state": "READY_FOR_REVIEW"}

    async def failing_loop(body, **kwargs):
        seen.append(keep_awake.holding())
        raise RuntimeError("loop crashed")

    async def narrate(kind: str, data: dict) -> None:
        pass

    monkeypatch.setattr(extension_run, "_run_vision_loop_impl", fake_loop)
    summary = asyncio.run(extension_run.run_vision_loop(object(), url=None, narrate=narrate, run_id=0, user_id=0))
    assert summary["state"] == "READY_FOR_REVIEW"
    assert not keep_awake.holding()

    monkeypatch.setattr(extension_run, "_run_vision_loop_impl", failing_loop)
    with pytest.raises(RuntimeError):
        asyncio.run(extension_run.run_vision_loop(object(), url=None, narrate=narrate, run_id=0, user_id=0))
    assert not keep_awake.holding()

    assert seen == [True, True]
    assert [kind for kind, _ in fake_power] == ["create", "clear", "create", "clear"]
