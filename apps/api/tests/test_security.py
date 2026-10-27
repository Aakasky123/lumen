from app.security import redact_otp_codes


def test_redact_otp_codes_does_not_redact_code_quality_phrase() -> None:
    text = "Experience writing unit tests and applying software testing best practices to ensure code quality and reliability."

    assert redact_otp_codes(text) == text


def test_redact_otp_codes_redacts_otp_like_context_tokens() -> None:
    assert redact_otp_codes("Use security code 654321 to continue.") == "Use security code [REDACTED_OTP] to continue."
    assert redact_otp_codes("Copy this code: jzZYGjaj") == "Copy this code: [REDACTED_OTP]"


def test_code_keys_are_matched_by_word_not_by_substring() -> None:
    hint = "Bringing a window to the front does not put the caret in its text area. Before typing, click it."
    # "typing" / "shipping" merely contain "pin": their text is not a code.
    assert redact_otp_codes({"typing_requires_caret": hint}) == {"typing_requires_caret": hint}
    assert redact_otp_codes({"shipping": "Ships Monday"}) == {"shipping": "Ships Monday"}
    # A key that names a code still forces redaction, in snake_case or camelCase.
    assert redact_otp_codes({"otp_code": "654321"}) == {"otp_code": "[REDACTED_OTP]"}
    assert redact_otp_codes({"smsCode": "A1B2C3"}) == {"smsCode": "[REDACTED_OTP]"}
    assert redact_otp_codes({"pin": "4821"}) == {"pin": "[REDACTED_OTP]"}
    assert redact_otp_codes({"verification_code": "ZX81QP"}) == {"verification_code": "[REDACTED_OTP]"}


def test_capitalized_words_after_code_are_prose_not_codes() -> None:
    # Seen from the brain seat on os_dialogs: page text and summaries lost ordinary words.
    assert redact_otp_codes("Entered the archive code. Opened the relay console") == (
        "Entered the archive code. Opened the relay console"
    )
    assert redact_otp_codes("Enter the code: Continue") == "Enter the code: Continue"
    assert redact_otp_codes("Pin: Holder name below") == "Pin: Holder name below"
    # Codes still go: digits, all-caps, and mixed case with inner capitals.
    assert redact_otp_codes("Your code: 482913") == "Your code: [REDACTED_OTP]"
    assert redact_otp_codes("code: AB12CD") == "code: [REDACTED_OTP]"
    assert redact_otp_codes("Copy this code: jzZYGjaj") == "Copy this code: [REDACTED_OTP]"


def test_values_under_code_keys_stay_redacted_even_when_they_read_like_words() -> None:
    # Review finding 7: the prose rule must not apply where the key itself says it holds a code.
    assert redact_otp_codes({"otp_code": "Falcon", "pin": "Seven"}) == {"otp_code": "[REDACTED_OTP]", "pin": "[REDACTED_OTP]"}
