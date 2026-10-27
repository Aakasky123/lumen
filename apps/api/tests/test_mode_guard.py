from __future__ import annotations

import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient

from app.agent_settings import update_agent_settings
from app.lumen_model import LumenModelClient
from app.config import get_settings
from app.db import SessionLocal, init_db
from app.main import app
from app.mode_guard import _default_operational_user, compute_mode_report
from app.model_provider_health import clear_model_provider_health_cache
from app.runtime import OperatorRuntimeEngine


PROJECT_ROOT = Path(__file__).resolve().parents[3]


@contextmanager
def patched_settings(**values):
    settings = get_settings()
    old_values = {key: getattr(settings, key) for key in values}
    clear_model_provider_health_cache()
    for key, value in values.items():
        setattr(settings, key, value)
    try:
        yield settings
    finally:
        for key, value in old_values.items():
            setattr(settings, key, value)
        clear_model_provider_health_cache()


def test_mode_report_defaults_to_approval_without_a_database() -> None:
    assert compute_mode_report().final_action_mode == "approval"


def test_mode_report_reflects_the_default_users_full_autonomy_setting() -> None:
    init_db()
    with SessionLocal() as db:
        OperatorRuntimeEngine(db).ensure_default_user()
        user = _default_operational_user(db)
        update_agent_settings(db, user.id, {"final_action_mode": "autonomous"})
        report = compute_mode_report(db=db)
        update_agent_settings(db, user.id, {"final_action_mode": "approval"})
        db.commit()

    assert report.final_action_mode == "autonomous"


def test_application_goal_snapshots_full_autonomy_at_creation() -> None:
    init_db()
    with SessionLocal() as db:
        runtime = OperatorRuntimeEngine(db)
        user = runtime.ensure_default_user()
        update_agent_settings(db, user.id, {"final_action_mode": "autonomous"})
        run = runtime.create_goal(job_url="https://boards.example.test/jobs/123", goal="Apply")
        policy = dict((run.confidence_state or {}).get("final_action_policy") or {})
        # create_goal committed Full autonomy; the restore must be committed too or it leaks into later tests.
        update_agent_settings(db, user.id, {"final_action_mode": "approval"})
        db.commit()

    assert policy["final_action_mode"] == "autonomous"
    assert policy["standing_authority_granted"] is True
    assert policy["authority_source"] == "user_agent_setting_at_run_creation"


def test_model_unavailable_is_reported_as_a_warning() -> None:
    with patched_settings(use_mock_ai=True):
        report = compute_mode_report()

    assert report.model_mode == "mock_or_deterministic"
    assert "live_model_unavailable" in report.warnings


def test_health_includes_mode_summary() -> None:
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert "mode" in body
    assert "model_mode" in body["mode"]


def test_health_model_handles_provider_error_without_leaking_key(monkeypatch) -> None:
    class ProviderError(Exception):
        pass

    def raise_provider_error(*args, **kwargs):
        raise ProviderError("provider boom")

    monkeypatch.setattr("app.model_provider._http_post", raise_provider_error)
    with patched_settings(use_mock_ai=False, openai_api_key="sk-secret-test", skill_author_model="gpt-test"):
        with TestClient(app) as client:
            response = client.get("/health/model?refresh=true")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["provider"] == "openai"
    assert body["validated"] is False
    assert "provider boom" in body["last_validation_error"]
    assert "sk-secret-test" not in response.text


def test_mode_check_uses_real_default_user_over_stale_placeholder(tmp_path: Path) -> None:
    db_path = tmp_path / "merged-user.db"
    pdf_path = tmp_path / "resume.pdf"
    env = _mode_check_env(db_path)
    env["PYTHONPATH"] = str(PROJECT_ROOT / "apps" / "api") + os.pathsep + env.get("PYTHONPATH", "")
    env.update({"USE_MOCK_AI": "true", "TEST_RESUME_PDF": str(pdf_path)})
    setup = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os
from pathlib import Path
from app import models
from app.db import SessionLocal, init_db

init_db()
pdf_path = Path(os.environ["TEST_RESUME_PDF"])
pdf_path.write_bytes(b"%PDF-1.4\\n" + (b"x" * 256))
with SessionLocal() as db:
    stale = models.User(email="stale@example.com", name="Stale User")
    real = models.User(email="real@lumen.test", name="Real Candidate")
    db.add_all([stale, real])
    db.flush()
    db.add(models.UserProfile(user_id=stale.id, profile_json={}, agent_settings_json={}))
    db.add(models.UserProfile(
        user_id=real.id,
        phone="+1 555 0100",
        location="Jersey City, New Jersey",
        linkedin="https://www.linkedin.com/in/real-candidate",
        work_authorization="Permanent authorization to work in the U.S.",
        sponsorship_status="Does not require sponsorship",
        salary_expectation="90000",
        profile_json={"salary_minimum_base_usd": "90000"},
        agent_settings_json={"vision_provider_id": 3},
    ))
    resume = models.Resume(user_id=real.id, name="Base", base_resume_text="resume", is_base=True)
    db.add(resume)
    db.flush()
    db.add(models.ResumeArtifact(
        resume_id=resume.id,
        artifact_type="BASE_PDF",
        pdf_path=str(pdf_path),
        compiler_used="uploaded",
        compile_status="SUCCESS",
        file_size=pdf_path.stat().st_size,
    ))
    db.commit()
""",
        ],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert setup.returncode == 0, setup.stdout + setup.stderr

    result = subprocess.run([sys.executable, "apps/api/mode_check.py"], cwd=PROJECT_ROOT, env=env, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)

    assert payload["details"]["profile"]["user_id"] == 2
    assert payload["profile_mode"] == "complete"
    assert payload["resume_mode"] == "available"


def test_openai_error_blocks_required_controlled_live_generative_field(monkeypatch) -> None:
    def raise_provider_error(*args, **kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr("app.model_provider._http_post", raise_provider_error)
    with patched_settings(use_mock_ai=False, openai_api_key="sk-test"):
        result = LumenModelClient().answer_application_field(
            {
                "field_key": "role_interest",
                "label": "Why are you interested?",
                "profile": {},
                "resume_facts": {},
                "job": {"title": "Engineer"},
            },
            require_live_model=True,
            block_on_openai_error=True,
        )

    assert result["blocked"] is True
    assert result["blocking_reason"] == "model_error_blocked"
    assert result["metadata"]["provider"] == "model_error_blocked"


def test_safe_role_interest_retries_model_before_blocking(monkeypatch) -> None:
    calls = []

    def fake_call_model_json(provider, *, purpose, system, prompt, schema):
        calls.append({"purpose": purpose, "prompt": prompt})
        if purpose == "application_field_answer":
            return {
                "ok": True,
                "data": {
                    "answer": "",
                    "confidence": 0.2,
                    "rationale": "Too generic.",
                    "needs_followup": True,
                    "source": "deepseek",
                },
                "metadata": {"provider": "deepseek", "model": "deepseek-v4-pro", "latency_ms": 12, "token_usage": {}},
            }
        return {
            "ok": True,
            "data": {
                "answer": "I am interested in this role because it matches my experience building reliable software systems and applied AI workflows.",
                "confidence": 0.82,
                "rationale": "The answer uses supplied job/profile context without unsupported claims.",
                "needs_followup": False,
                "source": "deepseek_retry",
            },
            "metadata": {"provider": "deepseek", "model": "deepseek-v4-pro", "latency_ms": 10, "token_usage": {}},
        }

    monkeypatch.setattr("app.lumen_model.call_model_json", fake_call_model_json)
    with patched_settings(use_mock_ai=False, openai_api_key="sk-test"):
        result = LumenModelClient().answer_application_field(
            {
                "field_key": "role_interest",
                "label": "Why do you think you are the right candidate and what about this role interests you?",
                "profile": {"summary": "Software engineer building AI agents and full-stack systems."},
                "resume_facts": {"skills": ["backend APIs", "full-stack systems", "AI workflows"]},
                "job": {"title": "Junior Software Engineer"},
            },
            require_live_model=True,
            block_on_openai_error=True,
        )

    assert result["needs_followup"] is False
    assert result["answer"]
    assert result["metadata"]["retry_of_unresolved_answer"] is True
    assert [call["purpose"] for call in calls] == ["application_field_answer", "application_field_answer_retry"]


def test_deepseek_field_answer_missing_metadata_is_repaired(monkeypatch) -> None:
    def fake_call_model_json(provider, *, purpose, system, prompt, schema):
        return {
            "ok": True,
            "data": {
                "answer": "I am looking for a role where I can apply my software engineering and AI workflow experience to meaningful product work.",
                "needs_followup": False,
            },
            "metadata": {"provider": "deepseek", "model": "deepseek-v4-pro", "latency_ms": 9, "token_usage": {}},
        }

    monkeypatch.setattr("app.lumen_model.call_model_json", fake_call_model_json)
    with patched_settings(use_mock_ai=False, openai_api_key="sk-test"):
        result = LumenModelClient().answer_application_field(
            {
                "field_key": "why_new_opportunity",
                "label": "Why are you looking for a new opportunity at this time?",
                "profile": {"summary": "Software engineer building AI agents and full-stack systems."},
                "resume_facts": {"skills": ["backend APIs", "full-stack systems", "AI workflows"]},
                "job": {"title": "Software Engineer"},
            },
            require_live_model=True,
            block_on_openai_error=True,
        )

    assert result["needs_followup"] is False
    assert result["answer"]
    assert result["confidence"] >= 0.55
    assert result["metadata"]["schema_repair"] == "field_answer_missing_confidence"


def test_deepseek_field_answer_missing_followup_flag_is_repaired(monkeypatch) -> None:
    def fake_call_model_json(provider, *, purpose, system, prompt, schema):
        assert prompt["field"]["current_candidate_answer"]["answer"].startswith("I am interested")
        return {
            "ok": True,
            "data": {
                "answer": "I am interested in this role because it matches my software engineering background and lets me contribute to reliable product systems.",
                "source": "deepseek",
            },
            "metadata": {"provider": "deepseek", "model": "deepseek-v4-pro", "latency_ms": 9, "token_usage": {}},
        }

    monkeypatch.setattr("app.lumen_model.call_model_json", fake_call_model_json)
    with patched_settings(use_mock_ai=False, openai_api_key="sk-test"):
        result = LumenModelClient().answer_application_field(
            {
                "field_key": "role_interest",
                "label": "Why do you think you are the right candidate and what about this role interests you?",
                "current_candidate_answer": {
                    "answer": "I am interested in this role because it matches my experience building reliable software systems.",
                    "confidence": 0.66,
                    "source": "answer_agent",
                },
                "profile": {"summary": "Software engineer building AI agents and full-stack systems."},
                "resume_facts": {"skills": ["backend APIs", "full-stack systems", "AI workflows"]},
                "job": {"title": "Software Engineer"},
            },
            require_live_model=True,
            block_on_openai_error=True,
        )

    assert result["needs_followup"] is False
    assert result["answer"]
    assert result["confidence"] >= 0.55
    assert result["metadata"]["schema_repair"] == "field_answer_missing_confidence"


def test_deterministic_profile_fields_resolve_without_openai() -> None:
    with patched_settings(use_mock_ai=True):
        result = LumenModelClient().answer_application_field(
            {
                "field_key": "sponsorship",
                "label": "Will you require sponsorship?",
                "allowed_options": ["Yes", "No"],
                "profile": {"sponsorship_status": "Does not require work authorization or sponsorship"},
                "resume_facts": {},
                "job": {},
            }
        )

    assert result["answer"] == "No"
    assert result["metadata"]["provider"] == "mock"


def _mode_check_env(db_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    return env
