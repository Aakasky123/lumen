from app.extension_run import _grounding_raw_relation_items
from app.text_trim import TRUNCATION_MARKER, trim_marked

LONG = (
    "Challenge 3. One region " + "filler words here " * 12
    + "Show only Middle East & Africa on the Revenue by region chart, then submit the total."
)


def test_trim_marked_short_text_untouched_and_ampersand_preserved():
    assert trim_marked("Middle East & Africa", 260) == "Middle East & Africa"


def test_trim_marked_cuts_at_word_boundary_with_explicit_marker():
    out = trim_marked(LONG, 260)
    assert len(out) <= 260
    assert out.endswith(TRUNCATION_MARKER)
    body = out[: -len(TRUNCATION_MARKER)]
    assert LONG.startswith(body)
    assert LONG[len(body)] == " "


def test_trim_marked_never_splits_middle_east_and_africa_silently():
    text = "x " * 115 + "Show only Middle East & Africa on the chart"
    out = trim_marked(text, 260)
    assert out.endswith(TRUNCATION_MARKER)
    assert not out.endswith("Middle East ")


def test_grounding_relation_items_mark_truncation():
    items = _grounding_raw_relation_items([{"relation": "previous_sibling", "text": LONG, "depth": 0}], limit=8)
    assert items[0]["text"].endswith(TRUNCATION_MARKER)
    short = _grounding_raw_relation_items([{"text": "Middle East & Africa"}], limit=8)
    assert short[0]["text"] == "Middle East & Africa"
