"""Field-approval question text keeps choice options and optionality visible."""

from app.db import SessionLocal
from app.runtime import OperatorRuntimeEngine


YES = "Yes - I consent to receiving text messages"
NO = "No - I do not consent to receiving text messages"


def test_choice_question_text_shows_question_options_and_optionality() -> None:
    with SessionLocal() as db:
        eng = OperatorRuntimeEngine(db)
        opts = [YES, NO]
        optional = eng._field_approval_question_text("Do you consent to receiving text messages?", options=opts, required=False, is_choice=True)
        assert YES in optional and NO in optional
        assert "optional" in optional.lower() and "skip" in optional.lower()
        assert "which option should i select" in optional.lower()
        assert "leave it blank" not in optional.lower()

        required = eng._field_approval_question_text("Do you consent to receiving text messages?", options=opts, required=True, is_choice=True)
        assert YES in required and NO in required
        assert "required" in required.lower()
