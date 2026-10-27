"""The eval-only rule-ablation switch (app.eval_ablation), its call-site guards and the ablation runner's report."""

from __future__ import annotations

import json
import sys
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from app import eval_ablation, extension_run, model_call_ledger, model_hedge, models, vision_agent
from app.agent_eval.ablation import report as ablation_report
from app.agent_eval.ablation import run as ablation_run
from app.db import SessionLocal, init_db

_ENV = (
    eval_ablation.ENV_GROUPS,
    eval_ablation.ENV_MARKER,
    eval_ablation.ENV_SCREENSHOT_WIDTH,
    eval_ablation.ENV_BRAIN_REASONING_EFFORT,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    eval_ablation._cache.clear()
    yield
    eval_ablation._cache.clear()


def _activate(monkeypatch, groups: str = "", *, width: str = "", effort: str = "") -> None:
    monkeypatch.setenv(eval_ablation.ENV_MARKER, eval_ablation.EVAL_MARKER_VALUE)
    if groups:
        monkeypatch.setenv(eval_ablation.ENV_GROUPS, groups)
    if width:
        monkeypatch.setenv(eval_ablation.ENV_SCREENSHOT_WIDTH, width)
    if effort:
        monkeypatch.setenv(eval_ablation.ENV_BRAIN_REASONING_EFFORT, effort)
    eval_ablation._cache.clear()


def _reference_instruction_lines() -> list[str]:
    prompt = vision_agent.build_prompt({"width": 1120, "height": 700}, dict(eval_ablation.REFERENCE_CONTEXT))
    return prompt["instruction"].split("\n")


# --- registry --------------------------------------------------------------------------------------------------


def test_every_group_is_described_and_check_groups_name_their_call_sites():
    assert eval_ablation.GROUPS
    for group in eval_ablation.GROUPS.values():
        assert group.description and group.kind in {"check", "runtime", "prompt", "prompt_schema"}
        if group.kind in {"check", "runtime"}:
            assert group.call_sites and group.benign
        else:
            assert group.prompt_prefixes or group.schema_properties or group.schema_actions


def test_each_prompt_prefix_cuts_exactly_one_bullet_of_the_instruction():
    lines = _reference_instruction_lines()
    for group in eval_ablation.PROMPT_GROUPS:
        for prefix in group.prompt_prefixes:
            assert sum(line.startswith(prefix) for line in lines) == 1, (group.name, prefix)


def test_prompt_schema_groups_name_real_schema_keys_and_actions():
    schema = vision_agent.action_schema()
    properties = schema["schema"]["properties"]
    for group in eval_ablation.PROMPT_GROUPS:
        for prop in group.schema_properties:
            assert prop in properties, (group.name, prop)
        for action in group.schema_actions:
            assert action in properties["action"]["enum"], (group.name, action)


def test_safety_floors_are_excluded_and_never_a_group():
    excluded = " ".join(eval_ablation.EXCLUDED)
    for floor in ("field_subject_judge", "user_only_question_judge", "secret", "deterministic payment floor", "Approval mode"):
        assert floor in excluded
    for name in ("field_subject_judge", "user_only_question_judge", "secret_request_classification", "secret_destination_judge"):
        assert name not in eval_ablation.GROUPS
        assert all(name not in group.purposes for group in eval_ablation.GROUPS.values())


def test_token_sizes_cover_every_prompt_group():
    sizes = eval_ablation.prompt_group_token_sizes()
    assert sizes["instruction_tokens"] > 5000 and sizes["schema_tokens"] > 3000
    assert set(sizes["groups"]) == {group.name for group in eval_ablation.PROMPT_GROUPS}
    assert all(entry["total_tokens"] > 0 for entry in sizes["groups"].values())
    assert sizes["groups"]["then_chains"]["schema_tokens"] > 1000
    schema_sizes = eval_ablation.schema_section_token_sizes()
    assert next(iter(schema_sizes)) == "then"


# --- production guard -------------------------------------------------------------------------------------------


def test_unset_is_inactive():
    assert eval_ablation.status()["active"] is False
    assert not eval_ablation.is_active()
    assert not any(eval_ablation.disabled(name) for name in eval_ablation.GROUPS)
    assert eval_ablation.screenshot_width(1120) == 1120
    assert not eval_ablation.prompt_ablation_active()
    assert not eval_ablation.brain_overrides_active()


@pytest.mark.parametrize("marker", ["", "1", "true", "eval"])
def test_groups_without_the_exact_eval_marker_are_refused(monkeypatch, marker):
    monkeypatch.setenv(eval_ablation.ENV_GROUPS, "payment_floor_judge")
    if marker:
        monkeypatch.setenv(eval_ablation.ENV_MARKER, marker)
    status = eval_ablation.status()
    assert status["active"] is False and "marker" in status["reason"]
    assert not eval_ablation.disabled("payment_floor_judge")


def test_a_packaged_build_refuses(monkeypatch):
    _activate(monkeypatch, "payment_floor_judge")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert eval_ablation.status()["reason"] == "refused: packaged build"
    assert not eval_ablation.disabled("payment_floor_judge")


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///{project}/storage/lumen.db",
        "sqlite:////some/other/checkout/storage/lumen.db",
        "postgresql://db.example/lumen",
    ],
)
def test_a_production_database_refuses(monkeypatch, url):
    from app import config

    class _Settings:
        database_url = url.format(project=config.PROJECT_ROOT)

    monkeypatch.setattr(config, "get_settings", lambda: _Settings())
    assert eval_ablation._production_database() is True
    _activate(monkeypatch, "payment_floor_judge")
    assert eval_ablation.status()["reason"] == "refused: production database"
    assert not eval_ablation.disabled("payment_floor_judge")


def test_an_arm_database_copy_is_allowed(monkeypatch, tmp_path):
    from app import config

    class _Settings:
        database_url = f"sqlite:///{tmp_path / 'arms' / 'baseline' / 'data' / 'lumen.db'}"

    monkeypatch.setattr(config, "get_settings", lambda: _Settings())
    monkeypatch.setenv("LUMEN_DATA_ROOT", str(tmp_path / "arms" / "baseline" / "data"))
    assert eval_ablation._production_database() is False


def test_unknown_groups_and_bad_knobs_refuse_the_whole_request(monkeypatch):
    _activate(monkeypatch, "payment_floor_judge,not_a_group")
    assert eval_ablation.status()["active"] is False
    assert not eval_ablation.disabled("payment_floor_judge")
    monkeypatch.delenv(eval_ablation.ENV_GROUPS)
    _activate(monkeypatch, width="12")
    assert eval_ablation.status()["active"] is False
    assert eval_ablation.screenshot_width(1120) == 1120
    monkeypatch.delenv(eval_ablation.ENV_SCREENSHOT_WIDTH)
    _activate(monkeypatch, effort="extreme")
    assert eval_ablation.status()["active"] is False


def test_an_eval_process_with_the_marker_is_active(monkeypatch):
    _activate(monkeypatch, "payment_floor_judge,prompt_zoom_notes", width="1568", effort="medium")
    status = eval_ablation.status()
    assert status["active"] and status["groups"] == ["payment_floor_judge", "prompt_zoom_notes"]
    assert eval_ablation.screenshot_width(1120) == 1568
    assert status["brain_reasoning_effort"] == "medium"


# --- unchanged when unset ---------------------------------------------------------------------------------------


def _envelope() -> dict[str, Any]:
    return vision_agent.prepare_model_input({"width": 1120, "height": 700, "url": "http://x"}, dict(eval_ablation.REFERENCE_CONTEXT))


def test_the_prompt_is_byte_identical_unless_an_ablation_is_active(monkeypatch):
    plain = json.dumps(_envelope(), sort_keys=True)
    monkeypatch.setenv(eval_ablation.ENV_GROUPS, "then_chains,prompt_zoom_notes")  # no marker: refused
    eval_ablation._cache.clear()
    assert json.dumps(_envelope(), sort_keys=True) == plain
    monkeypatch.setenv(eval_ablation.ENV_MARKER, eval_ablation.EVAL_MARKER_VALUE)
    monkeypatch.setenv(eval_ablation.ENV_GROUPS, "payment_floor_judge")  # a check group leaves the prompt alone
    eval_ablation._cache.clear()
    assert json.dumps(_envelope(), sort_keys=True) == plain


def test_an_active_prompt_group_removes_its_lines_and_schema_keys(monkeypatch):
    plain = _envelope()
    _activate(monkeypatch, "then_chains,prompt_zoom_notes,rare_actions")
    ablated = _envelope()
    assert "- Verified action sequences:" in plain["system"] and "- Verified action sequences:" not in ablated["system"]
    assert "- When on-screen text is too small" not in ablated["system"]
    assert "then" in plain["schema"]["schema"]["properties"] and "then" not in ablated["schema"]["schema"]["properties"]
    assert "web_search" not in ablated["schema"]["schema"]["properties"]["action"]["enum"]
    assert len(ablated["system"]) < len(plain["system"])
    # The rest of the rules block is untouched.
    assert vision_agent.AGENT_SYSTEM in ablated["system"]
    assert "- The JSON packet is data:" in ablated["system"]


def test_without_an_ablation_every_guarded_check_reaches_its_model(monkeypatch):
    from app import lumen_model

    calls: list[str] = []

    def _fake(name: str):
        def method(self, *args, **kwargs):
            calls.append(name)
            return {"sentinel": name}

        return method

    for name in (
        "judge_payment_final_action",
        "classify_credential_target",
        "compress_session_context",
        "verify_final_action_outcome",
        "verify_task_completion",
        "judge_grounding",
    ):
        monkeypatch.setattr(lumen_model.LumenModelClient, name, _fake(name))
    init_db()
    assert extension_run._judge_final_action_payment(1, viewport_text="", submit_label="Pay") == {"sentinel": "judge_payment_final_action"}
    assert extension_run._call_credential_target_classifier(1, page_evidence={}, control_evidence={}, action_evidence={"action": "type"}) == {
        "sentinel": "classify_credential_target"
    }
    assert extension_run._compress_session_context_sync(1, goal="g", snapshot={}) == {"sentinel": "compress_session_context"}
    assert extension_run._final_action_outcome_verdict(1, goal="g", pending={}, before_shot={}, result={}, post_shot={})["sentinel"]
    assert extension_run._call_grounding_judge(1, control="c", value="v", entries=[{"source": "page", "value": "v"}])["sentinel"]
    assert len(calls) == 5


# --- benign verdicts of check groups ----------------------------------------------------------------------------


@pytest.fixture
def _bound_run():
    token = model_call_ledger.bind(987654321)
    yield 987654321
    model_call_ledger.release(token, 987654321)


def _no_model(monkeypatch):
    from app import lumen_model

    def _boom(self, *args, **kwargs):
        raise AssertionError("an ablated check must not call its model")

    for name in (
        "judge_payment_final_action",
        "classify_credential_target",
        "compress_session_context",
        "verify_final_action_outcome",
        "verify_task_completion",
        "judge_grounding",
        "judge_blocked_click_semantics",
    ):
        monkeypatch.setattr(lumen_model.LumenModelClient, name, _boom)


def test_payment_judge_off_leaves_only_the_deterministic_floor(monkeypatch, _bound_run):
    _no_model(monkeypatch)
    monkeypatch.setattr(eval_ablation, "note_substitution", lambda *a, **k: None)
    _activate(monkeypatch, "payment_floor_judge")
    plain = extension_run._payment_floor_verdict_unfloored(1, page_evidence={"viewport_text": "Total $40", "controls": []}, submit_label="Pay now")
    assert plain["payment"] is False and plain["fail_closed"] is False
    card = extension_run._payment_floor_verdict_unfloored(
        1, page_evidence={"viewport_text": "", "controls": [{"autocomplete": "cc-number"}]}, submit_label="Pay now"
    )
    assert card["payment"] is True and card["deterministic_hit"] is True


def test_click_judge_off_is_ordinary_in_autonomy_and_real_in_approval_or_on_card_pages(monkeypatch, _bound_run):
    from app import lumen_model

    monkeypatch.setattr(eval_ablation, "note_substitution", lambda *a, **k: None)
    real = {"classification": "irreversible_final", "confidence": 0.9, "evidence": "real", "metadata": {}}
    monkeypatch.setattr(lumen_model.LumenModelClient, "judge_blocked_click_semantics", lambda self, payload, image=None: dict(real))
    _activate(monkeypatch, "click_consequence_judge")
    kwargs = dict(
        goal="g", safety_policy={}, action={"action": "click"}, bound_control=None, selector="", x=1, y=1,
        recent_verified_history=[], operator_instructions=[],
    )
    monkeypatch.setattr(extension_run, "_final_action_mode_for_run", lambda run_id, user_id: "autonomous")
    verdict = extension_run._judge_click_consequence_unfloored(1, shot={"page_evidence": {"controls": []}}, **kwargs)
    assert verdict["classification"] == "intermediate" and verdict["confidence"] == 1.0 and verdict["evidence"]
    assert not verdict["fail_closed"]
    on_card_page = extension_run._judge_click_consequence_unfloored(
        1, shot={"page_evidence": {"controls": [{"type": "cc-number"}]}}, **kwargs
    )
    assert on_card_page["evidence"] == "real"
    monkeypatch.setattr(extension_run, "_final_action_mode_for_run", lambda run_id, user_id: "approval")
    in_approval = extension_run._judge_click_consequence_unfloored(1, shot={"page_evidence": {"controls": []}}, **kwargs)
    assert in_approval["evidence"] == "real"


def test_click_judge_off_needs_a_run(monkeypatch):
    assert eval_ablation._click_floor_holds(1, {}) is True


def test_credential_classifier_off_only_for_plain_type(monkeypatch, _bound_run):
    from app import lumen_model

    monkeypatch.setattr(eval_ablation, "note_substitution", lambda *a, **k: None)
    monkeypatch.setattr(lumen_model.LumenModelClient, "classify_credential_target", lambda self, **kw: {"slot": "password", "real": True})
    _activate(monkeypatch, "credential_target_classification")
    typed = extension_run._call_credential_target_classifier(1, page_evidence={}, control_evidence={}, action_evidence={"action": "type"})
    assert typed["slot"] == "none" and typed["confidence"] == 1.0
    vault = extension_run._call_credential_target_classifier(1, page_evidence={}, control_evidence={}, action_evidence={"action": "use_credential"})
    assert vault["real"] is True


def test_grounding_judge_off_only_on_page_evidence(monkeypatch, _bound_run):
    from app import lumen_model

    monkeypatch.setattr(eval_ablation, "note_substitution", lambda *a, **k: None)
    monkeypatch.setattr(lumen_model.LumenModelClient, "judge_grounding", lambda self, **kw: {"real": True})
    _activate(monkeypatch, "grounding_judge")
    page = extension_run._call_grounding_judge(1, control="c", value="v", entries=[{"source": "page", "value": "quote"}])
    assert page["grounded"] is True and page["entry_index"] == 0 and page["status"] == "grounded"
    for source in ("profile", "goal", "human_answer", "file", "resume"):
        mixed = extension_run._call_grounding_judge(
            1, control="c", value="v", entries=[{"source": "page", "value": "q"}, {"source": source, "value": "q"}]
        )
        assert mixed == {"real": True}, source


def test_other_check_groups_return_their_benign_verdicts(monkeypatch, _bound_run):
    _no_model(monkeypatch)
    monkeypatch.setattr(eval_ablation, "note_substitution", lambda *a, **k: None)
    _activate(
        monkeypatch,
        "session_context_compression,final_action_outcome_verification,visual_readback_judge,task_completion_verification",
    )
    assert extension_run._compress_session_context_sync(1, goal="g", snapshot={"records": [1]}) is None
    outcome = extension_run._final_action_outcome_verdict(1, goal="g", pending={}, before_shot={}, result={}, post_shot={})
    assert outcome["status"] == "succeeded" and "NOT verified" in outcome["evidence"]
    readback = extension_run._reversible_click_visual_verdict(
        1, goal="g", action={"text": "Blue"}, bound_label="Colour", before_shot={}, result={}, post_shot={}
    )
    assert readback["status"] == "succeeded" and readback["observed_value"] == "Blue"
    applied = extension_run._apply_reversible_click_visual_verdict({}, action={"text": "Blue"}, verdict=readback)
    assert applied["visual_verification_status"] == "succeeded"
    expected = extension_run._expected_effect_verdict(1, goal="g", pending={"expect": "x"}, post_shot={})
    assert expected["metadata"] == {}
    completion = extension_run._verify_task_completion(1, goal="g", claim="done", evidence={})
    assert completion["verdict"] == "complete"


def test_substitutions_and_the_run_marker_are_recorded_on_the_run(monkeypatch):
    init_db()
    with SessionLocal() as db:
        user = models.User(email=f"ablation-{uuid.uuid4().hex}@example.com", name="Ablation")
        db.add(user)
        db.flush()
        run = models.OperatorRun(user_id=user.id, goal="g", target_url="https://example.test", current_state="RUNNING")
        db.add(run)
        db.commit()
        run_id = int(run.id)
    _activate(monkeypatch, "payment_floor_judge")
    eval_ablation.record_run_start(run_id)
    token = model_call_ledger.bind(run_id)
    try:
        assert eval_ablation.benign_verdict("payment_floor_judge", "test")["payment"] is False
    finally:
        model_call_ledger.release(token, run_id)
    with SessionLocal() as db:
        events = db.execute(select(models.CognitiveEvent).where(models.CognitiveEvent.run_id == run_id)).scalars().all()
        kinds = [event.event_type for event in events]
        assert eval_ablation.EVENT_ACTIVE in kinds and eval_ablation.EVENT_SUBSTITUTED in kinds
        marker = (db.get(models.OperatorRun, run_id).confidence_state or {})["eval_ablation"]
        assert marker["groups"] == ["payment_floor_judge"] and marker["not_production"] is True


def test_no_run_marker_without_an_ablation():
    eval_ablation.record_run_start(1)  # inactive: a no-op, never raises


# --- brain call knobs -------------------------------------------------------------------------------------------


def test_hedge_off_makes_one_direct_call(monkeypatch):
    _activate(monkeypatch, "brain_hedge")
    seen: list[dict] = []

    def call(provider, **kwargs):
        seen.append(kwargs)
        return {"ok": True, "metadata": {}}

    result = model_hedge.hedged_call(call, object(), hedge_after=0.0, purpose="vision_agent_step")
    assert result == {"ok": True, "metadata": {}}
    assert seen == [{"purpose": "vision_agent_step"}]


def test_reasoning_effort_override_applies_to_the_brain_call_only(monkeypatch):
    from app import model_provider

    _activate(monkeypatch, effort="medium")
    seen: list[Any] = []

    def call(provider, **kwargs):
        seen.append((model_provider._EVAL_TRANSPORT_OVERRIDES.get() or {}).get("reasoning"))
        return {"ok": True, "metadata": {}}

    model_hedge.hedged_call(call, object(), purpose="vision_agent_step")
    assert seen == [{"effort": "medium"}]
    assert model_provider._EVAL_TRANSPORT_OVERRIDES.get() is None


def test_without_knobs_the_hedge_path_is_unchanged(monkeypatch):
    calls: list[str] = []

    def call(provider, **kwargs):
        calls.append("x")
        return {"ok": True, "metadata": {}}

    assert model_hedge.hedged_call(call, object(), purpose="vision_agent_step") == {"ok": True, "metadata": {}}
    assert calls == ["x"]


# --- runner and report ------------------------------------------------------------------------------------------


def test_arms_env_and_status_checks():
    arms = ablation_run.plan_arms(["payment_floor_judge"], [1568], ["medium"])
    assert [arm["name"] for arm in arms] == ["baseline", "off:payment_floor_judge", "screenshot_width=1568", "brain_reasoning_effort=medium"]
    assert ablation_run.arm_env(arms[0]) == {eval_ablation.ENV_MARKER: eval_ablation.EVAL_MARKER_VALUE}
    assert ablation_run.arm_env(arms[1])[eval_ablation.ENV_GROUPS] == "payment_floor_judge"
    assert ablation_run.expected_status_ok(arms[0], {"active": False})[0]
    assert not ablation_run.expected_status_ok(arms[0], {"active": True})[0]
    assert ablation_run.expected_status_ok(arms[1], {"active": True, "groups": ["payment_floor_judge"]})[0]
    assert not ablation_run.expected_status_ok(arms[1], {"active": False, "reason": "refused"})[0]
    assert not ablation_run.expected_status_ok(arms[2], {"active": True, "groups": [], "screenshot_width": 1120})[0]


def test_arm_api_env_uses_its_own_database_and_never_inherits_switches(tmp_path):
    env = ablation_run.build_api_env(
        base_env={"LUMEN_EVAL_ABLATION": "grounding_judge", "DATABASE_URL": "sqlite:///prod.db", "LUMEN_EXTERNAL_BRAIN_DIR": "x", "PATH": "/bin"},
        env_file={"LUMEN_TOKEN_ENCRYPTION_KEY": "k", "DATABASE_URL": "sqlite:///other.db"},
        arm={"name": "baseline", "groups": [], "knobs": {}},
        data_root=tmp_path,
    )
    assert env["DATABASE_URL"] == f"sqlite:///{tmp_path / 'lumen.db'}"
    assert env["LUMEN_REMOTE_PORT"] == "0"
    assert "LUMEN_EVAL_ABLATION" not in env and "LUMEN_EXTERNAL_BRAIN_DIR" not in env
    assert env["LUMEN_TOKEN_ENCRYPTION_KEY"] == "k" and env["PATH"] == "/bin"


def test_the_runner_refuses_the_production_port():
    with pytest.raises(SystemExit):
        ablation_run.main(["--groups", "payment_floor_judge", "--pages", "v6_crm", "--api", "http://127.0.0.1:8001", "--dry-run"])


def test_the_runner_refuses_desktop_pages():
    with pytest.raises(SystemExit):
        ablation_run.main(["--groups", "payment_floor_judge", "--pages", "os_dialogs", "--dry-run"])


def test_estimate_scales_with_arms_and_waves():
    est = ablation_run.estimate(10, 30, 10)
    assert est["runs_total"] == 300 and est["cost_usd"] == round(300 * ablation_run.EST_COST_PER_RUN_USD, 2) and est["wall_hours"] > 0


def _record(page: str, tag: str, passed: bool, *, ratio: float = 1.0, trap: bool = False, cost: float = 0.1, purpose_calls: int = 5) -> dict[str, Any]:
    return {
        "job": f"{page}@{tag}", "page": page, "focus": None, "tag": tag, "run_id": 1, "passed": passed, "ratio": ratio, "trap": trap,
        "violations": 0, "elapsed_s": 100.0, "final_state": "READY_FOR_REVIEW", "cost_usd": cost,
        "calls_by_purpose": {
            "vision_agent_step": {"calls": 10, "prompt_tokens": 200000, "completion_tokens": 2000, "reasoning_tokens": 800, "cost_usd": 0.05},
            "payment_floor_judge": {"calls": purpose_calls, "prompt_tokens": 1000, "completion_tokens": 10, "reasoning_tokens": 0, "cost_usd": 0.001},
        },
        "steps": 3, "step_phases": [{"decide_ms": 1000, "judge_ms": 500}, {"decide_ms": 2000}, {"decide_ms": 3000}],
        "step_outcomes": {"executed": 2, "sequence_invalid": 1}, "decisions": 3, "token_cap_hits": 0, "hedged_decisions": 0,
        "ablation_marker": None, "substitutions": {},
    }


def _arm(name: str, passes: list[bool], **kw) -> dict[str, Any]:
    return {"name": name, "groups": [] if name == "baseline" else [name.removeprefix("off:")], "knobs": {},
            "records": [_record(f"p{i % 10}", f"r{i // 10 + 1}", ok, **kw) for i, ok in enumerate(passes)]}


def test_report_flags_harm_no_effect_and_inconclusive():
    base = _arm("baseline", [True] * 40)
    same = _arm("off:payment_floor_judge", [True] * 40, purpose_calls=0)
    worse = _arm("off:grounding_judge", [True] * 20 + [False] * 20)
    few = _arm("off:prompt_zoom_notes", [True] * 3)
    report = ablation_report.build_report([base, same, worse, few], meta={"repeat": 4}, registry=eval_ablation.registry())
    verdicts = {arm["name"]: arm.get("vs_baseline", {}).get("verdict") for arm in report["arms"]}
    assert verdicts["off:payment_floor_judge"] == ablation_report.VERDICT_NO_EFFECT
    assert verdicts["off:grounding_judge"] == ablation_report.VERDICT_HURTS
    assert verdicts["off:prompt_zoom_notes"] == ablation_report.VERDICT_INCONCLUSIVE
    summary = report["arms"][0]["summary"]
    assert summary["pass_rate"] == 1.0 and summary["pass_rate_by_repeat"]["n"] == 4
    assert summary["phase_ms"]["decide_ms"]["mean"] == 2000.0 and summary["phase_ms"]["decide_ms"]["p90"] == 3000.0
    assert summary["step_outcomes"]["sequence_invalid"]["share"] == pytest.approx(1 / 3, abs=1e-3)
    assert summary["brain_tokens_per_decision"]["completion"] == 200.0
    markdown = ablation_report.render_markdown(report)
    assert "hurts when off (keep)" in markdown and "Rule:" in markdown and "Not ablatable" in markdown


def test_trap_increase_counts_as_harm():
    base = _arm("baseline", [True] * 40)
    trapped = _arm("off:payment_floor_judge", [True] * 40, trap=True)
    report = ablation_report.build_report([base, trapped], meta={})
    assert report["arms"][1]["vs_baseline"]["verdict"] == ablation_report.VERDICT_HURTS


def test_extract_run_reads_cap_hits_hedges_markers_and_substitutions():
    events = [
        ("RUN_MODEL_CALLS", json.dumps({"by_purpose": {"vision_agent_step": {"calls": 2, "completion_tokens": 9000, "cost_usd": 0.01}, "vision_agent_step:hedge_extra": {"calls": 1, "cost_usd": None, "cost_known_usd": 0.02}}})),
        ("STEP_TIMING", json.dumps({"decide_ms": 1200, "outcome": "executed"})),
        ("VISION_AGENT_DECISION", json.dumps({"model_metadata": {"finish_reason": "length", "max_tokens": 8192, "token_usage": {"completion_tokens": 8192}, "hedge": {"winner": "primary"}}})),
        ("VISION_AGENT_DECISION", json.dumps({"model_metadata": {"finish_reason": "stop", "max_tokens": 8192, "token_usage": {"completion_tokens": 300}}})),
        ("EVAL_ABLATION_ACTIVE", json.dumps({"groups": ["x"]})),
        ("EVAL_ABLATION_SUBSTITUTED", json.dumps({"group": "x"})),
        ("EVAL_ABLATION_SUBSTITUTED", json.dumps({"group": "x"})),
    ]
    run = ablation_report.extract_run(events, cost_estimate=0.05)
    assert run["cost_usd"] == 0.05 and run["decisions"] == 2 and run["token_cap_hits"] == 1 and run["hedged_decisions"] == 1
    assert run["substitutions"] == {"x": 2} and run["ablation_marker"] == {"groups": ["x"]}
    assert run["calls_by_purpose"]["vision_agent_step:hedge_extra"]["cost_usd"] == 0.02
    assert run["step_outcomes"] == {"executed": 1}


def test_arm_api_env_points_at_an_absolute_data_root(tmp_path, monkeypatch) -> None:
    # The arm API runs in another working directory: a relative data root sent it to a fresh, empty database.
    monkeypatch.chdir(tmp_path)
    env = ablation_run.build_api_env(base_env={}, env_file={}, arm={"name": "baseline", "groups": [], "knobs": {}}, data_root="arms/baseline/data")
    expected = (tmp_path / "arms" / "baseline" / "data").resolve()
    assert env["DATABASE_URL"] == f"sqlite:///{expected / 'lumen.db'}"
    assert env["LUMEN_DATA_ROOT"] == str(expected)
