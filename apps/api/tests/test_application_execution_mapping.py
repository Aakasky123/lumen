from app.lumen_answer_interpreter import LumenAnswerInterpreter


def test_lumen_interpreter_resolves_pdf_acknowledgement_to_actual_option() -> None:
    interpreted = LumenAnswerInterpreter().interpret(
        raw_answer="yes, I uploaded the PDF",
        field_key="pdf_resume_acknowledgement",
        label="Please only submit a PDF version of your resume above.",
        options=["I have submitted a PDF version of my resume."],
    )

    assert interpreted.resolved_value == "I have submitted a PDF version of my resume."
    assert interpreted.confidence >= 0.95


def test_lumen_interpreter_resolves_how_did_you_hear_to_linkedin_option() -> None:
    interpreted = LumenAnswerInterpreter().interpret(
        raw_answer="LinkedIn or any listed option is fine",
        field_key="how_did_you_hear_about_this_job",
        label="How did you hear about this job?",
        options=["LinkedIn", "Company Website", "Referral", "Other"],
    )

    assert interpreted.resolved_value == "LinkedIn"
    assert interpreted.confidence >= 0.9


def test_lumen_interpreter_resolves_experience_answer_to_yes() -> None:
    interpreted = LumenAnswerInterpreter().interpret(
        raw_answer="yes based on my resume write 1 line answer",
        field_key="ai_coding_assistant_experience",
        label="Do you have any experience in effectively utilizing AI coding assistants such as Claude Code, Cursor, Codex, etc?",
        options=["Yes", "No"],
    )

    assert interpreted.resolved_value == "Yes"
    assert interpreted.confidence >= 0.9


def test_lumen_interpreter_extracts_years_without_treating_singular_year_as_duration() -> None:
    years = LumenAnswerInterpreter().interpret(
        raw_answer="about 7+ years write around that and save it to the profile so you could use it next time",
        field_key="professional_python_years",
        label="How many years of professional Python experience do you have?",
        options=[],
    )
    graduation = LumenAnswerInterpreter().interpret(
        raw_answer="2020",
        field_key="graduation_year",
        label="Graduation year",
        options=[],
    )

    assert years.resolved_value == "7+ years"
    assert graduation.resolved_value == "2020"


def test_lumen_interpreter_normalizes_location_and_salary_answers() -> None:
    location = LumenAnswerInterpreter().interpret(
        raw_answer="jersey city new jersey",
        field_key="location_city_candidate_location",
        label="Location (City)*",
        options=["Jersey City, New Jersey", "New York, New York"],
    )
    salary = LumenAnswerInterpreter().interpret(
        raw_answer="$70k",
        field_key="salary",
        label="Minimum required base salary",
        options=[],
    )
    assert location.resolved_value == "Jersey City, New Jersey"
    assert salary.resolved_value == "70000"
