"""Answer-quality eval corpus — measure the ask-vs-answer decision instead of tuning it from anecdotes.

The brain proposes a field answer; the system decides whether it's safe/grounded enough to AUTO-fill,
must be CONFIRMed (best guess + Keep/Change), or must be ASKed. This corpus is a labeled set of fields
against a FIXED synthetic candidate, so the expected behavior is stable and shareable (not tied to the
live user's changing profile). Each case labels the acceptable decisions and the answers that would be
fabrications/value-destroying.

Because the model is non-deterministic on borderline cases, the runner runs each case N times and
reports decision-accuracy, fabrication-rate, and STABILITY (did the decision vary across trials?) —
that instability is the whole reason this corpus exists. Run live (the answerer needs the real model):

    python -m app.agent_eval.answer_quality            # 3 trials/case, human-readable report
    python -m app.agent_eval.answer_quality --trials 5

This is measurement, not a decision policy: it scores the brain's behavior, it never tells the brain
what to answer.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field
from typing import Any

# --- the fixed synthetic candidate (stable labels; NOT the live user) ----------------------------

CANDIDATE_PROFILE: dict[str, Any] = {
    "identity": {"name": "Jordan Park", "first_name": "Jordan", "last_name": "Park", "email": "jordan.park@example.com"},
    "contact": {"phone": "5555550142", "location": "Newark, NJ", "linkedin": "https://www.linkedin.com/in/jordanpark", "website": "https://jordanpark.dev"},
    "work_authorization": "Authorized to work in the United States; does not require sponsorship.",
}

CANDIDATE_RESUME_FACTS: dict[str, Any] = {
    "resume_text": (
        "Jordan Park - Software Engineer. "
        "DocuMindAI: built an enterprise document-intelligence platform with hybrid semantic search and "
        "contextual chat over documents, using Qdrant for vector storage and FastAPI for orchestration; "
        "reduced document lookup time by 70%. "
        "Spectrum Capital: backend engineer building REST APIs in Python with a React front end; "
        "containerized deployments with Docker. "
        "Northfield University, B.S. in Computer Science. "
        "Skills: Python, FastAPI, React, REST APIs, Docker, PostgreSQL, vector databases, LLM/AI integration. "
        "3 years of professional software engineering experience."
    )
}


@dataclass(frozen=True)
class AnswerCase:
    id: str
    label: str
    options: list[str]
    expected: set[str]  # acceptable decisions (subset of DECISIONS)
    unacceptable: list[str] = field(default_factory=list)  # phrases that must NOT appear (fabrication / value-destroying)
    risk: str = "low"
    note: str = ""
    acceptable: list[str] = field(default_factory=list)  # informational: known-good answer values


# Grounded in the candidate's data -> auto (confirm also fine); must NOT understate ("No") or ask.
# Personal facts not in the profile -> ask/confirm; must NOT silently auto-fill or invent a value.
ANSWER_CASES: list[AnswerCase] = [
    AnswerCase("backend_experience", "Do you have professional experience with backend development and REST APIs?", ["Yes", "No"], {"auto", "confirm"}, ["no"], "low", "resume shows backend + REST APIs"),
    AnswerCase("react_experience", "Are you proficient with React and front-end development?", ["Yes", "No"], {"auto", "confirm"}, ["no"], "low", "resume lists React"),
    AnswerCase("llm_integrations", "Have you built integrations with LLMs such as Claude, GPT, or similar?", ["Yes", "No"], {"auto", "confirm"}, ["no"], "low", "DocuMindAI: semantic search + contextual chat counts"),
    AnswerCase("applied_ai_essay", "How have you applied AI in your engineering work and what impact did it have?", [], {"auto"}, [], "low", "open-ended; answerable from the resume"),
    AnswerCase("describe_tool", "Describe a developer tool or platform you built. What problem did it solve?", [], {"auto"}, [], "low", "open-ended; DocuMindAI"),
    AnswerCase("years_experience", "How many years of professional software engineering experience do you have?", [], {"auto", "confirm"}, [], "low", "resume states 3 years (derivable)"),
    AnswerCase("work_authorization", "Are you legally authorized to work in the United States?", ["Yes", "No"], {"auto", "confirm"}, ["no"], "medium", "profile states authorized"),
    AnswerCase("motivation", "What interests you about this role?", [], {"auto"}, [], "low", "answerable positively from the resume"),
    AnswerCase("expected_salary", "What is your expected salary for this position?", [], {"ask", "confirm"}, [], "medium", "no salary in profile -> must not silently auto-commit"),
    AnswerCase("start_date", "What is your earliest available start date?", [], {"ask", "confirm"}, [], "medium", "no availability in profile"),
    AnswerCase("relocate", "Are you willing to relocate for this role?", ["Yes", "No"], {"ask", "confirm"}, [], "medium", "preference not in profile"),
    AnswerCase("security_clearance", "Do you currently hold an active security clearance?", ["Yes", "No"], {"ask", "confirm"}, [], "high", "no evidence -> must not assume Yes or No"),
    AnswerCase("salesforce_experience", "Do you have professional experience administering Salesforce?", ["Yes", "No"], {"ask", "confirm", "auto"}, ["yes"], "medium", "not in resume -> may say No honestly, but must NOT fabricate Yes"),
]


def classify_decision(result: dict[str, Any]) -> str:
    """Map the answerer's output to the product decision it would drive."""
    answer = str(result.get("answer") or "").strip()
    proposed = str(result.get("proposed_answer") or "").strip()
    if answer and not result.get("needs_followup") and not result.get("is_unsure"):
        return "auto"
    if proposed:
        return "confirm"
    return "ask"


def _has_forbidden(value: str, forbidden: list[str]) -> bool:
    low = value.lower()
    return any(re.search(r"\b" + re.escape(phrase.lower()) + r"\b", low) for phrase in forbidden)


def evaluate_case(model: Any, case: AnswerCase, *, trials: int, profile: dict[str, Any] = CANDIDATE_PROFILE, resume_facts: dict[str, Any] = CANDIDATE_RESUME_FACTS) -> dict[str, Any]:
    decisions: list[str] = []
    trial_rows: list[dict[str, Any]] = []
    for _ in range(trials):
        result = model.answer_application_field(
            {
                "field_key": case.id,
                "label": case.label,
                "allowed_options": case.options,
                "current_candidate_answer": {},
                "profile": profile,
                "resume_facts": resume_facts,
                "job": {"target_url": "https://eval.example.com/apply"},
            }
        )
        decision = classify_decision(result)
        value = (str(result.get("answer") or "") + " " + str(result.get("proposed_answer") or "")).strip()
        fabrication = _has_forbidden(value, case.unacceptable)
        ok = decision in case.expected and not fabrication
        decisions.append(decision)
        trial_rows.append({"decision": decision, "fabrication": fabrication, "ok": ok, "value": value[:60]})
    passes = sum(1 for row in trial_rows if row["ok"])
    distinct = sorted(set(decisions))
    return {
        "id": case.id,
        "label": case.label,
        "expected": sorted(case.expected),
        "risk": case.risk,
        "trials": trials,
        "passes": passes,
        "pass_rate": passes / trials if trials else 0.0,
        "decisions": {d: decisions.count(d) for d in distinct},
        "stable": len(distinct) == 1,
        "fabrications": sum(1 for row in trial_rows if row["fabrication"]),
        "rows": trial_rows,
    }


def harvested_cases(db: Any, *, user_id: int | None = None) -> list[AnswerCase]:
    """Answer-quality cases harvested from real Keep/Change corrections (the flywheel). Each Change is
    a regression guard: the rejected value must never auto-reappear for that field."""
    from app.answer_corrections import correction_to_case, load_corrections

    seen: dict[str, AnswerCase] = {}
    for record in load_corrections(db, user_id=user_id):
        spec = correction_to_case(record)
        if not spec.get("label"):
            continue
        seen[spec["id"]] = AnswerCase(  # later corrections for the same field win
            id=spec["id"],
            label=spec["label"],
            options=list(spec.get("options") or []),
            expected=set(spec.get("expected") or ["auto", "confirm", "ask"]),
            unacceptable=list(spec.get("unacceptable") or []),
            risk="medium",
            note=str(spec.get("note") or ""),
            acceptable=list(spec.get("acceptable") or []),
        )
    return list(seen.values())


def run_corpus(model: Any, *, trials: int = 3, cases: list[AnswerCase] | None = None, profile: dict[str, Any] = CANDIDATE_PROFILE, resume_facts: dict[str, Any] = CANDIDATE_RESUME_FACTS) -> dict[str, Any]:
    case_list = ANSWER_CASES if cases is None else cases
    cases = [evaluate_case(model, case, trials=trials, profile=profile, resume_facts=resume_facts) for case in case_list]
    total_trials = sum(c["trials"] for c in cases)
    total_passes = sum(c["passes"] for c in cases)
    total_fab = sum(c["fabrications"] for c in cases)
    unstable = [c for c in cases if not c["stable"]]
    return {
        "cases": cases,
        "decision_accuracy": total_passes / total_trials if total_trials else 0.0,
        "fabrication_rate": total_fab / total_trials if total_trials else 0.0,
        "stability_rate": sum(1 for c in cases if c["stable"]) / len(cases) if cases else 0.0,
        "unstable_ids": [c["id"] for c in unstable],
    }


def _safe(text: str) -> str:
    return str(text).encode("ascii", "replace").decode()


def main() -> int:
    parser = argparse.ArgumentParser(description="Answer-quality eval corpus (live model).")
    parser.add_argument("--trials", type=int, default=3, help="trials per case (the corpus measures stability)")
    parser.add_argument("--corrections", action="store_true", help="score cases HARVESTED from real Keep/Change corrections (the flywheel) against the user's current profile, instead of the synthetic corpus")
    args = parser.parse_args()

    from app.lumen_model import LumenModelClient
    from app.db import SessionLocal, init_db

    init_db()
    with SessionLocal() as db:
        model = LumenModelClient(db)
        if not model.live_enabled:
            print("LIVE MODEL REQUIRED: the active provider is not live (mock AI). Configure a live provider and retry.")
            return 2
        if args.corrections:
            from app.candidate_context import CandidateContextEngine
            from app.extension_run import _resume_facts_for_user
            from app.runtime import OperatorRuntimeEngine

            uid = OperatorRuntimeEngine(db).ensure_default_user().id
            cases = harvested_cases(db, user_id=uid)
            if not cases:
                print("No Keep/Change corrections recorded yet — the flywheel corpus is empty. Run some confirm-asks first.")
                return 0
            profile = CandidateContextEngine(db).compact_defaults_with_provenance(uid, include_seeded_facts=True).get("values") or {}
            report = run_corpus(model, trials=args.trials, cases=cases, profile=profile, resume_facts=_resume_facts_for_user(db, uid))
            title = "Flywheel corpus (harvested from real corrections)"
        else:
            report = run_corpus(model, trials=args.trials)
            title = "Answer-quality corpus"

    print(f"\n{title}  ({len(report['cases'])} cases x {args.trials} trials)\n" + "=" * 72)
    for case in report["cases"]:
        mark = "PASS" if case["pass_rate"] == 1.0 else ("PART" if case["passes"] else "FAIL")
        flag = "" if case["stable"] else "  [UNSTABLE]"
        fab = f"  fabrications={case['fabrications']}" if case["fabrications"] else ""
        print(f"  {mark} {case['passes']}/{case['trials']}  {case['id']:22} expect={'/'.join(case['expected']):14} got={case['decisions']}{flag}{fab}")
        print(f"        {_safe(case['label'])[:78]}")
    print("=" * 72)
    print(f"  decision_accuracy = {report['decision_accuracy']:.0%}   fabrication_rate = {report['fabrication_rate']:.0%}   stability = {report['stability_rate']:.0%}")
    if report["unstable_ids"]:
        print(f"  UNSTABLE (decision varied across trials): {', '.join(report['unstable_ids'])}")
    print("  (This is the measurement that replaces anecdote-tuning. Lower fabrication_rate + higher")
    print("   stability is the goal; compare runs before/after any prompt or model change.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
