"""totp_run never drives an owner stack: it needs a bench port and a --bench-db that its launch configuration confirms."""

from __future__ import annotations

import pytest

from app.agent_eval.open_bench import totp_run as run

CONFIGS = [
    {"name": "api-bench", "port": 8021, "env": {"DATABASE_URL": "sqlite:///C:/Projects/Lumen/storage/totp-bench.db"}},
    {"name": "api-main", "port": 8002, "env": {"DATABASE_URL": "sqlite:///C:/Projects/Lumen/storage/lumen.db"}},
    {"name": "api-default", "port": 8031, "env": {"DATABASE_URL": "sqlite:///C:/Projects/Lumen/storage/lumen.db"}},
]
BENCH_DB = "sqlite:///C:/Projects/Lumen/storage/totp-bench.db"


def test_a_bench_stack_with_its_matching_database_is_allowed() -> None:
    assert run.bench_stack_problem("http://127.0.0.1:8021", BENCH_DB, CONFIGS) is None
    assert run.bench_stack_problem("http://127.0.0.1:8021", "C:\\Projects\\Lumen\\storage\\totp-bench.db", CONFIGS) is None


@pytest.mark.parametrize("port", [8001, 8002, 8011, 8041])
def test_owner_and_other_session_ports_are_refused_whatever_the_flag_says(port: int) -> None:
    assert run.bench_stack_problem(f"http://127.0.0.1:{port}", BENCH_DB, CONFIGS)


def test_the_flag_is_required_and_must_match_the_launch_configuration() -> None:
    assert "required" in run.bench_stack_problem("http://127.0.0.1:8021", None, CONFIGS)
    assert "does not match" in run.bench_stack_problem("http://127.0.0.1:8021", "sqlite:///C:/other.db", CONFIGS)
    assert "no launch configuration" in run.bench_stack_problem("http://127.0.0.1:8099", BENCH_DB, CONFIGS)
    assert run.bench_stack_problem("http://127.0.0.1:8021", BENCH_DB, [])


def test_the_default_application_database_is_never_a_bench_database() -> None:
    default = "sqlite:///C:/Projects/Lumen/storage/lumen.db"
    assert "default application database" in run.bench_stack_problem("http://127.0.0.1:8031", default, CONFIGS)


def test_main_refuses_before_touching_the_api(monkeypatch: pytest.MonkeyPatch) -> None:
    touched: list[str] = []
    monkeypatch.setattr(run.bc, "load_live_run", lambda api: touched.append(api))
    monkeypatch.setattr(run, "launch_configs", lambda path=None: CONFIGS)
    with pytest.raises(SystemExit) as raised:
        run.main(["--api", "http://127.0.0.1:8021", "--arm", "on"])
    assert "refusing" in str(raised.value) and touched == []
    with pytest.raises(SystemExit):
        run.main(["--api", "http://127.0.0.1:8002", "--arm", "on", "--bench-db", BENCH_DB])
    assert touched == []
