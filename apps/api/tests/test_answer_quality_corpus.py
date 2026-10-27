"""The answer-quality corpus + its decision classifier are CI-guarded (deterministic). The live
scoring (run_corpus against the real model) is a separate live tool, per CLAUDE.md."""

from app.agent_eval.answer_quality import (
    ANSWER_CASES,
    AnswerCase,
    _has_forbidden,
    classify_decision,
)


def test_classify_decision_maps_answerer_output_to_product_decision() -> None:
    # Confident grounded -> auto.
    assert classify_decision({"answer": "Yes", "proposed_answer": "", "is_unsure": False, "needs_followup": False}) == "auto"
    # Unsure with a best guess -> confirm.
    assert classify_decision({"answer": "", "proposed_answer": "Open to market range", "is_unsure": True, "needs_followup": True}) == "confirm"
    # No answer, no guess -> ask.
    assert classify_decision({"answer": "", "proposed_answer": "", "is_unsure": False, "needs_followup": True}) == "ask"
    # An answer flagged unsure is NOT auto (must be confirmed/asked).
    assert classify_decision({"answer": "Maybe", "proposed_answer": "Maybe", "is_unsure": True, "needs_followup": True}) == "confirm"


def test_forbidden_phrase_matching_is_word_bounded() -> None:
    assert _has_forbidden("No", ["no"]) is True
    assert _has_forbidden("Yes", ["no"]) is False
    assert _has_forbidden("I am open to market range", ["open to market range"]) is True
    assert _has_forbidden("Norway is lovely", ["no"]) is False  # word-bounded, not substring


def test_corpus_is_well_formed() -> None:
    assert ANSWER_CASES, "corpus must not be empty"
    ids = [c.id for c in ANSWER_CASES]
    assert len(ids) == len(set(ids)), "case ids must be unique"
    for case in ANSWER_CASES:
        assert isinstance(case, AnswerCase)
        assert case.label.strip(), f"{case.id} has an empty label"
        assert case.expected, f"{case.id} has no expected decisions"
        assert case.expected <= {"auto", "confirm", "ask"}, f"{case.id} expects unknown decisions: {case.expected}"


def test_corpus_covers_both_grounded_and_ungrounded_families() -> None:
    # Must exercise both directions or it isn't measuring the real risk.
    auto_only = [c for c in ANSWER_CASES if c.expected == {"auto"} or c.expected == {"auto", "confirm"}]
    ask_side = [c for c in ANSWER_CASES if "ask" in c.expected]
    assert auto_only, "need grounded cases that should auto-fill"
    assert ask_side, "need ungrounded cases that should ask/confirm"
