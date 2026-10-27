from app.answer_normalization import classify_field, normalize_answer_value


LEVEL_OPTIONS = ["none", "basic", "intermediate", "advance"]


def test_experience_level_options_map_years_to_advance() -> None:
    answer, error = normalize_answer_value(
        field_key="javascript_experience",
        label="What is your level of experience with JavaScript?",
        value="3+ years",
        options=LEVEL_OPTIONS,
    )

    assert error is None
    assert answer == "advance"


def test_go_experience_sentence_maps_to_basic_option() -> None:
    answer, error = normalize_answer_value(
        field_key="what_is_your_level_of_experience_with_go",
        label="What is your level of experience with Go?",
        value="Experience working with Go for backend development, APIs, and software engineering tasks.",
        options=LEVEL_OPTIONS,
    )

    assert error is None
    assert answer == "basic"


def test_ai_assisted_yes_maps_to_positive_level_option() -> None:
    answer, error = normalize_answer_value(
        field_key="ai_coding_assistant_experience",
        label="What is your level of experience with AI-Assisted development tools?",
        value="Yes",
        options=LEVEL_OPTIONS,
    )

    assert error is None
    assert answer == "advance"


def test_sponsorship_no_matches_negative_full_sentence_option() -> None:
    options = [
        "Yes, I will require immigration sponsorship now to legally work in the country where the job is located.",
        "Yes, I will require immigration sponsorship in the future to legally work in the country where the job is located.",
        "No, I do not and will not require immigration sponsorship to legally work in the country where the job is located.",
    ]

    answer, error = normalize_answer_value(
        field_key="sponsorship",
        label="Will you now or in the future require company sponsorship?",
        value="No",
        options=options,
    )

    assert error is None
    assert answer == options[2]


def test_previous_employment_full_sentence_option_is_valid_yes_no_field() -> None:
    options = [
        "I have not previously been employed at Affirm",
        "I have been employed at Affirm as a full-time employee",
        "I have been employed at Affirm as an intern",
    ]

    answer, error = normalize_answer_value(
        field_key="previous_employment",
        label="Have you previously been employed at Affirm for any length of time?",
        value="I have not previously been employed at Affirm",
        options=options,
    )

    assert error is None
    assert answer == options[0]


def test_start_date_availability_is_not_misclassified_as_yes_no() -> None:
    label = "When would you be available to begin work?"

    assert classify_field("start_date", label) == "generic"

    answer, error = normalize_answer_value(
        field_key="start_date",
        label=label,
        value="Available to start within two weeks of offer acceptance.",
        options=[],
    )

    assert error is None
    assert answer == "Available to start within two weeks of offer acceptance."
