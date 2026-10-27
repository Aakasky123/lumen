import asyncio

from app.control_ids import ControlIdRegistry
from app.extension_body import ExtensionBody
from app.extension_run import _control_inventory_index_map


def _row(selector, index, *, tag="button", y=10, role="", type_=""):
    return {
        "index": index, "selector": selector, "tag": tag, "role": role, "type": type_,
        "x": 5, "y": y, "width": 80, "height": 20, "text": selector,
    }


def test_same_element_keeps_its_id_when_the_list_renumbers():
    registry = ControlIdRegistry()
    first = [_row("#a", 1), _row("#b", 2), _row("#c", 3)]
    registry.relabel(first, scope="https://x.test/p")
    ids = {row["selector"]: row["index"] for row in first}
    # A new control appears at the top; positional numbering would shift every other control.
    second = [_row("#new", 1), _row("#a", 2), _row("#c", 3)]
    registry.relabel(second, scope="https://x.test/p#frag")
    by_selector = {row["selector"]: row["index"] for row in second}
    assert by_selector["#a"] == ids["#a"]
    assert by_selector["#c"] == ids["#c"]


def test_new_controls_get_fresh_ids_never_reused():
    registry = ControlIdRegistry()
    first = [_row("#a", 1), _row("#b", 2)]
    registry.relabel(first, scope="u")
    second = [_row("#a", 1), _row("#c", 2)]  # #b gone, #c new
    registry.relabel(second, scope="u")
    assert second[1]["index"] not in {first[0]["index"], first[1]["index"]}
    third = [_row("#a", 1), _row("#b", 2)]  # #b returns while remembered: its old id
    registry.relabel(third, scope="u")
    assert third[1]["index"] == first[1]["index"]
    assert all(row["index"] > 0 for row in third)


def test_different_page_or_kind_or_missing_selector_is_a_new_control():
    registry = ControlIdRegistry()
    one = [_row("#a", 1), _row("", 2)]
    registry.relabel(one, scope="https://x.test/one")
    two = [_row("#a", 1), _row("", 2)]
    registry.relabel(two, scope="https://x.test/two")
    assert {r["index"] for r in two}.isdisjoint({r["index"] for r in one})
    three = [_row("#a", 1, tag="a")]
    registry.relabel(three, scope="https://x.test/two")
    assert three[0]["index"] != two[0]["index"]


def test_duplicate_selector_in_one_observation_never_shares_an_id():
    registry = ControlIdRegistry()
    rows = [_row("#a", 1), _row("#a", 2)]
    registry.relabel(rows, scope="u")
    assert rows[0]["index"] != rows[1]["index"]


def test_scrolled_out_form_keeps_its_ids_after_a_long_absence():
    # Run 1824 shape: the question fields (ids 1-6) were last listed at the top of the page, the run scrolled
    # away for more than a dozen captures, and the same fields came back as 31-36 under the old 12-observation window.
    registry = ControlIdRegistry()
    form = [_row(f"#a-{n}", n, tag="input") for n in range(1, 7)]
    registry.relabel(form, scope="https://x.test/p")
    first_ids = [row["index"] for row in form]
    for n in range(30):
        below = [_row(f"#below-{n % 3}-{k}", k, y=40 * k) for k in range(1, 6)]
        registry.relabel(below, scope="https://x.test/p")
    back = [_row(f"#a-{n}", n, tag="input") for n in range(1, 7)]
    registry.relabel(back, scope="https://x.test/p")
    assert [row["index"] for row in back] == first_ids
    gone = registry.remembered(scope="https://x.test/p", current=first_ids)
    assert all(row["index"] not in first_ids for row in gone)


def test_remembered_controls_survive_long_absence_and_listing_is_bounded():
    registry = ControlIdRegistry()
    registry.relabel([_row("#a", 1)], scope="u")
    for n in range(25):
        registry.relabel([_row(f"#z{n}", 1)], scope="u")
    assert any(row["selector"] == "#a" for row in registry.remembered(scope="u", current=[]))


def test_same_selector_different_name_never_reuses_an_id_after_a_long_absence():
    registry = ControlIdRegistry()
    one = [{**_row("#row > button", 1), "text": "Delete Ann"}]
    registry.relabel(one, scope="u")
    for n in range(25):
        registry.relabel([_row(f"#z{n}", 1)], scope="u")
    two = [{**_row("#row > button", 1), "text": "Delete Bob"}]
    registry.relabel(two, scope="u")
    assert two[0]["index"] != one[0]["index"]
    back = [{**_row("#row > button", 1), "text": "Delete Ann"}]
    registry.relabel(back, scope="u")
    assert back[0]["index"] == one[0]["index"]


def test_size_cap_evicts_oldest_and_an_evicted_element_is_new_not_reused(monkeypatch):
    import app.control_ids as module

    monkeypatch.setattr(module, "_MAX_REMEMBERED_CONTROLS", 5)
    registry = ControlIdRegistry()
    first = [_row("#a", 1)]
    registry.relabel(first, scope="u")
    for n in range(10):
        registry.relabel([_row(f"#z{n}", 1)], scope="u")
    assert len(registry._ids) <= 5 and len(registry._seen) <= 5
    back = [_row("#a", 1)]
    registry.relabel(back, scope="u")
    assert back[0]["index"] != first[0]["index"]


def test_stale_id_fails_closed_in_the_dispatch_map():
    registry = ControlIdRegistry()
    first = [_row("#a", 1, y=10), _row("#b", 2, y=40)]
    registry.relabel(first, scope="u")
    stale_id = first[1]["index"]
    second = [_row("#a", 1, y=10)]
    registry.relabel(second, scope="u")
    mapped = _control_inventory_index_map({"controls": second, "viewport_height": 800})
    assert second[0]["index"] in mapped and mapped[second[0]["index"]]["selector"] == "#a"
    assert stale_id not in mapped  # same refusal path as a per-screenshot index that no longer exists


def test_extension_body_screenshot_relabels_controls():
    class _Body(ExtensionBody):
        def __init__(self):
            super().__init__(connection=type("C", (), {"device_id": "d"})())
            self.shots = []

        async def _call(self, method, params=None, *, timeout):
            return self.shots.pop(0)

    body = _Body()
    body.shots = [
        {"ok": True, "url": "https://x.test/p", "page_evidence": {"controls": [_row("#a", 1), _row("#b", 2)]}},
        {"ok": True, "url": "https://x.test/p", "page_evidence": {"controls": [_row("#b", 1)]}},
    ]
    one = asyncio.run(body.capture_screenshot())
    two = asyncio.run(body.capture_screenshot())
    assert two["page_evidence"]["controls"][0]["index"] == one["page_evidence"]["controls"][1]["index"]


def test_a_picker_with_many_controls_does_not_push_the_forms_fields_out_of_the_remembered_list():
    class _Body(ExtensionBody):
        def __init__(self):
            super().__init__(connection=type("C", (), {"device_id": "d"})())
            self.shots = []

        async def _call(self, method, params=None, *, timeout):
            return self.shots.pop(0)

    body = _Body()
    form = [_row(f"#field-{n}", n, tag="input") for n in range(1, 11)]
    days = [_row(f"#day-{n}", n) for n in range(1, 121)]
    body.shots = [
        {"ok": True, "url": "u", "page_evidence": {"controls": form + [_row("#open", 11)]}},
        {"ok": True, "url": "u", "page_evidence": {"controls": days}},
        {"ok": True, "url": "u", "page_evidence": {"controls": [_row("#open", 1)]}},
    ]
    first = asyncio.run(body.capture_screenshot())
    field_id = first["page_evidence"]["controls"][8]["index"]
    asyncio.run(body.capture_screenshot())
    third = asyncio.run(body.capture_screenshot())
    remembered = {row["index"]: row for row in third["page_evidence"]["remembered_controls"]}
    assert remembered[field_id]["selector"] == "#field-9"
    assert len(remembered) > 60


def test_same_selector_with_a_different_accessible_name_is_a_new_id():
    registry = ControlIdRegistry()
    first = [{**_row("li:nth-of-type(2) > button", 1), "text": "Delete Alice"}]
    registry.relabel(first, scope="u")
    same = [{**_row("li:nth-of-type(2) > button", 1), "text": "  delete   alice "}]
    registry.relabel(same, scope="u")
    assert same[0]["index"] == first[0]["index"]
    swapped = [{**_row("li:nth-of-type(2) > button", 1), "text": "Delete Bob"}]
    registry.relabel(swapped, scope="u")
    assert swapped[0]["index"] != first[0]["index"]


def test_focused_text_lines_control_is_relabelled_to_the_stable_id():
    class _Body(ExtensionBody):
        def __init__(self):
            super().__init__(connection=type("C", (), {"device_id": "d"})())
            self.shots = []

        async def _call(self, method, params=None, *, timeout):
            return self.shots.pop(0)

    body = _Body()
    body.shots = [
        {"ok": True, "url": "u", "page_evidence": {"controls": [_row("#a", 1)]}},
        {
            "ok": True,
            "url": "u",
            "page_evidence": {
                "controls": [_row("#new", 1), _row("#a", 2)],
                "focused_text_lines": {"control": 2, "lines": ["1|hello"]},
            },
        },
    ]
    one = asyncio.run(body.capture_screenshot())
    two = asyncio.run(body.capture_screenshot())
    a_id = one["page_evidence"]["controls"][0]["index"]
    assert two["page_evidence"]["controls"][1]["index"] == a_id
    assert two["page_evidence"]["focused_text_lines"]["control"] == a_id


def test_remembered_lists_controls_seen_before_and_absent_now_on_the_same_document():
    registry = ControlIdRegistry()
    first = [_row("#a", 1), _row("#far", 2)]
    registry.relabel(first, scope="https://x.test/p")
    far_id = first[1]["index"]
    second = [_row("#a", 1)]
    registry.relabel(second, scope="https://x.test/p#frag")
    rows = registry.remembered(scope="https://x.test/p", current=[row["index"] for row in second])
    assert [row["index"] for row in rows] == [far_id]
    assert rows[0]["selector"] == "#far" and rows[0]["tag"] == "button" and "observation" not in rows[0]
    # Another document sees none of it; a control back on screen is no longer remembered.
    assert registry.remembered(scope="https://x.test/other", current=[]) == []
    third = [_row("#a", 1), _row("#far", 2)]
    registry.relabel(third, scope="https://x.test/p")
    assert third[1]["index"] == far_id
    assert registry.remembered(scope="https://x.test/p", current=[row["index"] for row in third]) == []


def test_select_button_with_dom_id_and_aria_label_keeps_its_id_when_its_value_text_changes():
    # v6 calendar run 1904: the Duration button (#ed-dur, aria-label "Duration") showed its chosen value as
    # text; "30 min" -> "45 min" minted a new id and a chained click by the old id failed.
    registry = ControlIdRegistry()
    label = [{"relation": "aria-label", "text": "Duration"}]
    before = [{**_row("#ed-dur", 1), "id": "ed-dur", "text": "30 min", "accessibility_context": label}]
    registry.relabel(before, scope="u")
    after = [{**_row("#ed-dur", 1), "id": "ed-dur", "text": "45 min", "accessibility_context": label}]
    registry.relabel(after, scope="u")
    assert after[0]["index"] == before[0]["index"]
    relabelled = [{**_row("#ed-dur", 1), "id": "ed-dur", "text": "45 min",
                   "accessibility_context": [{"relation": "aria-label", "text": "Room"}]}]
    registry.relabel(relabelled, scope="u")
    assert relabelled[0]["index"] != before[0]["index"]


def test_shared_aria_label_without_a_dom_id_still_names_rows_by_their_text():
    # Row buttons share one aria-label; after a delete the same positional selector names another row.
    registry = ControlIdRegistry()
    label = [{"relation": "aria-label", "text": "Row actions"}]
    first = [{**_row("li:nth-of-type(2) > button", 1), "text": "Alice", "accessibility_context": label}]
    registry.relabel(first, scope="u")
    shifted = [{**_row("li:nth-of-type(2) > button", 1), "text": "Bob", "accessibility_context": label}]
    registry.relabel(shifted, scope="u")
    assert shifted[0]["index"] != first[0]["index"]


def test_aria_labelledby_names_a_dom_id_control_ahead_of_its_aria_label():
    registry = ControlIdRegistry()
    both = lambda labelled: [  # noqa: E731
        {"relation": "aria-labelledby", "text": labelled}, {"relation": "aria-label", "text": "Pick"}]
    first = [{**_row("#ed-start", 1), "id": "ed-start", "text": "09:00", "accessibility_context": both("Start")}]
    registry.relabel(first, scope="u")
    later = [{**_row("#ed-start", 1), "id": "ed-start", "text": "10:30", "accessibility_context": both("Start")}]
    registry.relabel(later, scope="u")
    assert later[0]["index"] == first[0]["index"]
    other = [{**_row("#ed-start", 1), "id": "ed-start", "text": "10:30", "accessibility_context": both("End")}]
    registry.relabel(other, scope="u")
    assert other[0]["index"] != first[0]["index"]


def test_consequence_floor_key_follows_the_same_name_rule():
    # consequence_floor shares _accessible_name: a select button showing a new value keeps its (stricter-only)
    # floor; an id-less button whose text changes is a new floor identity.
    from app.consequence_floor import control_key

    label = [{"relation": "aria-label", "text": "Duration"}]
    select = lambda text: {"tag": "button", "id": "ed-dur", "text": text, "accessibility_context": label}  # noqa: E731
    assert control_key(url="u", selector="#ed-dur", control=select("30 min")) == control_key(
        url="u", selector="#ed-dur", control=select("45 min"))
    plain = lambda text: {"tag": "button", "text": text}  # noqa: E731
    assert control_key(url="u", selector="form > button", control=plain("Save")) != control_key(
        url="u", selector="form > button", control=plain("Pay"))
