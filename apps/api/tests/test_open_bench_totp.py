"""The single-field TOTP fixture: server grading, goals, and that each variant's seed travels the way it should."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid

import pytest

from app import credential_vault, models, totp
from app.agent_eval.open_bench import totp_run as run
from app.agent_eval.open_bench import totp_server as srv
from app.db import SessionLocal, init_db


def _get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def _post(url: str, payload: dict) -> dict:
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


@pytest.fixture()
def fixture_server():
    server, state = srv.start_server()
    try:
        yield server, state
    finally:
        server.close()


def test_only_a_current_code_reveals_the_password(fixture_server) -> None:
    server, state = fixture_server
    entry = state["goal"]
    params = totp.parse_seed(entry["seed"])
    url = f"{server.url}/totp/goal/verify"
    current = {totp.totp_code(params, at=__import__("time").time() + step * 30) for step in (-1, 0, 1)}
    wrong = next(code for code in ("123456", "654321", "111111") if code not in current)
    assert _post(url, {"code": wrong}) == {"ok": False}
    assert _post(url, {"code": ""}) == {"ok": False}
    assert _post(url, {"code": "12345a"}) == {"ok": False}
    assert not entry.get("passed")
    assert _post(url, {"code": totp.totp_code(params)}) == {"ok": True, "password": entry["password"]}
    assert entry["passed"] is True


def test_pages_have_one_code_field_and_only_the_page_variant_prints_the_key(fixture_server) -> None:
    server, state = fixture_server
    for variant in srv.VARIANTS:
        status, html = _get(f"{server.url}/totp/{variant}")
        assert status == 200 and html.count('id="otp"') == 1 and 'maxlength="6"' in html
        assert state[variant]["password"] not in html
        shown = state[variant]["seed"][:4] in html
        assert shown is (variant == "page")
    assert _get(f"{server.url}/totp/nope")[0] == 404


def test_goals_carry_the_seed_only_in_the_goal_variant_and_name_the_page() -> None:
    seed = srv.new_seed()
    url = "http://127.0.0.1:5555/totp/goal"
    goals = {variant: run.make_goal(variant, url, seed) for variant in srv.VARIANTS}
    assert totp.seed_identity(seed) in {totp.seed_identity(s) for s in totp.find_seeds(goals["goal"])}
    assert not totp.find_seeds(goals["vault"]) and not totp.find_seeds(goals["page"])
    cleaned, stored = totp.protect_goal(goals["goal"], "k")
    assert seed not in cleaned and len(stored) == 1
    assert totp.goal_names_origin(goals["goal"], url)
    assert not totp.goal_names_origin(goals["goal"], "http://127.0.0.1:6666/totp/goal")


def test_vault_variant_origin_is_storable_for_the_fixture_server(fixture_server) -> None:
    server, state = fixture_server
    init_db()
    with SessionLocal() as db:
        user = models.User(email=f"totp-bench-{uuid.uuid4().hex}@example.test", name="Totp Bench")
        db.add(user)
        db.flush()
        item = credential_vault.create(
            db, user_id=user.id, origin=server.url, account_label="bench", fields={"totp": state["vault"]["seed"]}
        )
        db.commit()
        assert (
            credential_vault.resolve_slot(db, user_id=user.id, item_id=item.id, slot="totp", url=f"{server.url}/totp/vault")
            == state["vault"]["seed"]
        )


def test_jobs_and_grading() -> None:
    jobs = run.build_jobs(["vault", "page"], 2)
    assert [job["name"] for job in jobs] == ["totp-vault-1", "totp-vault-2", "totp-page-1", "totp-page-2"]
    assert run.build_jobs(["goal"], 1)[0]["name"] == "totp-goal"
    with pytest.raises(SystemExit):
        run.build_jobs(["bogus"], 1)
    assert run.grade("TOTP-AB12", ["done: TOTP-AB12"], "")["passed"]
    assert not run.grade("TOTP-AB12", ["no password"], "Verified. TOTP-AB12")["passed"]
    assert run.grade("TOTP-AB12", [], "Verified. TOTP-AB12")["password_on_page"]
