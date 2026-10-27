from __future__ import annotations

from app import screen_ocr


def _line(y: float, *words: tuple[str, float, float]) -> list[dict]:
    return [{"t": text, "x": x, "y": y, "w": w, "h": 16.0} for text, x, w in words]


PARAGRAPH = [
    _line(100, ("Any", 10, 30), ("crate", 46, 44), ("is", 96, 12), ("held", 114, 34), ("for", 154, 24), ("the", 184, 26)),
    _line(130, ("customs", 10, 70), ("release", 86, 62), ("form,", 154, 45), ("signed", 205, 52)),
]


def test_a_phrase_is_dragged_from_just_before_its_first_character_to_just_after_its_last() -> None:
    found = screen_ocr.locate_phrase("customs release", PARAGRAPH, (60, 138))
    assert found["found"] is True and found["occurrences"] == 1
    # "customs" starts at x=10 (10 px per character): the press lands a pixel and a half before it,
    # in the gap; "release" ends at 148, the release just past it. Both at the line's middle.
    assert found["from"] == (8, 138)
    assert found["to"] == (149, 138)
    assert found["box"] == (10, 130, 148, 146)


def test_punctuation_read_into_a_word_is_left_out_of_the_selection() -> None:
    # OCR reads "form," as one word: the selection ends after the m, by the word's average character width.
    found = screen_ocr.locate_phrase("release form", PARAGRAPH, (150, 138))
    assert found["to"] == (154 + 4 * 9, 138)
    # Ignoring letter case, and a phrase that wraps onto the next line runs from one line to the next.
    wrapped = screen_ocr.locate_phrase("THE customs", PARAGRAPH, (100, 120))
    assert wrapped["from"][1] == 108 and wrapped["to"][1] == 138


def test_a_phrase_never_matches_inside_a_longer_word_and_the_nearest_occurrence_wins() -> None:
    assert screen_ocr.locate_phrase("form", [_line(10, ("formal", 10, 60))], (20, 18)) == {"found": False, "occurrences": 0}
    assert screen_ocr.locate_phrase("release permits", PARAGRAPH, (60, 138))["found"] is False
    assert screen_ocr.locate_phrase("", PARAGRAPH, (60, 138))["found"] is False
    two = [_line(100, ("held", 10, 40)), _line(400, ("held", 10, 40))]
    near_bottom = screen_ocr.locate_phrase("held", two, (30, 390))
    assert near_bottom["occurrences"] == 2 and near_bottom["from"][1] == 408


def test_a_word_misread_by_one_letter_still_places_the_drag_but_says_so() -> None:
    # A highlight's edge cost "release" its last letter (OCR read "releas").
    misread = [_line(130, ("customs", 10, 70), ("releas", 86, 62), ("form", 154, 36))]
    found = screen_ocr.locate_phrase("customs release form", misread, (60, 138))
    assert found["found"] is True and found["approximate"] is True
    assert found["to"][0] > 154 + 36
    # An exact reading is never marked approximate, and two letters off is no match.
    assert "approximate" not in screen_ocr.locate_phrase("customs release", PARAGRAPH, (60, 138))
    assert screen_ocr.locate_phrase("customs relays form", misread, (60, 138))["found"] is False
    # Short words must read exactly ("form" is not "fort").
    assert screen_ocr.locate_phrase("fort", misread, (160, 138))["found"] is False


def test_a_capital_i_ocr_reads_as_a_bar_is_a_near_read_only() -> None:
    # v5_reconcile run 1717: Windows OCR read the Segoe UI capital I of INV-1881 as an apostrophe.
    lines = [["Invoice 'NV-1881", "Issued 2027-05-09"], ["Invoice |NV-3469 Issued 2027-05-25"]]
    assert screen_ocr.find_in_lines("INV-1881", lines)["found"] is False, "never proof on its own"
    near = screen_ocr.find_in_lines("INV-1881", lines, zero_misreads=True)
    assert near["found"] is True and near["zero_misread"] is True and near["image_index"] == 0
    assert screen_ocr.find_in_lines("INV-3469", lines, zero_misreads=True)["image_index"] == 1
    # Only where the value has a capital I, and every other character still exact.
    assert screen_ocr.find_in_lines("JNV-1881", lines, zero_misreads=True)["found"] is False
    assert screen_ocr.find_in_lines("INV-1882", lines, zero_misreads=True)["found"] is False
    assert "zero_misread" not in screen_ocr.find_in_lines("INV-5939", [["Invoice INV-5939"]], zero_misreads=True)
