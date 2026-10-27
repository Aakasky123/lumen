"""Requiredness projection for question cards."""

from app.form_graph import field_is_explicitly_optional, requiredness


def test_required_flag_projects_to_required() -> None:
    node = {"label": "What interests you?", "required": True}
    assert requiredness(node) == {"state": "required_proven", "confidence": 0.9, "evidence": ["required_flag"]}


def test_page_text_saying_optional_outranks_the_required_flag() -> None:
    node = {"label": "Gender", "section_heading": "Completion is voluntary", "required": True}
    assert requiredness(node)["state"] == "optional_proven"


def test_visible_marker_keeps_a_voluntary_section_field_required() -> None:
    node = {"label": "Confirm eligibility *", "section_heading": "Completion is voluntary", "required": True}
    assert requiredness(node)["state"] == "required_proven"


def test_field_is_explicitly_optional_reads_page_words() -> None:
    assert field_is_explicitly_optional({"label": "Portfolio (optional)"}) is True
    assert field_is_explicitly_optional({"label": "Confirm eligibility *", "section_heading": "Completion is voluntary"}) is False
    assert field_is_explicitly_optional({"label": "First name", "section_heading": "Your details"}) is False
