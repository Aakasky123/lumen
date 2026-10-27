from app import extension_run
from app.derivation_check import verify_derivation
from app.vision_agent import _clean_derivation

PAGE = (
    "Renewal date: July 25, 2028 Monthly subscription fee: $6,774.65 "
    "Inbox 12 messages Archive 7 messages"
)
GOAL = "Set the close date to 30 days before that renewal date. Annual contract value is 12 x the monthly fee."


def _date_derivation(**over):
    base = {
        "op": "date_add_days",
        "inputs": [{"quote": "Renewal date: July 25, 2028", "value": "July 25, 2028"}],
        "operand": "-30",
        "rule_quote": "30 days before that renewal date",
    }
    base.update(over)
    return base


def test_date_derivation_verified():
    result = verify_derivation(_date_derivation(), "2028-06-25", goal_text=GOAL, page_text=PAGE)
    assert result.status == "verified"
    assert "2028-06-25" in result.note


def test_product_and_sum_verified():
    product = {
        "op": "product",
        "inputs": [{"quote": "Monthly subscription fee: $6,774.65", "value": "$6,774.65"}],
        "operand": "12",
        "rule_quote": "12 x the monthly fee",
    }
    assert verify_derivation(product, "81295.80", goal_text=GOAL, page_text=PAGE).status == "verified"
    total = {
        "op": "sum",
        "inputs": [
            {"quote": "Inbox 12 messages", "value": "12"},
            {"quote": "Archive 7 messages", "value": "7"},
        ],
        "operand": "",
        "rule_quote": "Set the close date",
    }
    assert verify_derivation(total, "19", goal_text=GOAL, page_text=PAGE).status == "verified"


def test_wrong_arithmetic_refused():
    result = verify_derivation(_date_derivation(), "2028-07-05", goal_text=GOAL, page_text=PAGE)
    assert result.status == "mismatch" and result.refused
    product = {
        "op": "product",
        "inputs": [{"quote": "Monthly subscription fee: $6,774.65", "value": "$6,774.65"}],
        "operand": "12",
        "rule_quote": "12 x the monthly fee",
    }
    assert verify_derivation(product, "81295.00", goal_text=GOAL, page_text=PAGE).status == "mismatch"


def test_missing_input_or_rule_quote_refused():
    invented = _date_derivation(inputs=[{"quote": "Renewal date: August 1, 2028", "value": "August 1, 2028"}])
    assert verify_derivation(invented, "2028-07-02", goal_text=GOAL, page_text=PAGE).reason == "derivation_input_not_found"
    no_rule = _date_derivation(rule_quote="a rule nobody wrote")
    assert verify_derivation(no_rule, "2028-06-25", goal_text=GOAL, page_text=PAGE).reason == "derivation_rule_not_found"
    wrong_value = _date_derivation(inputs=[{"quote": "Renewal date: July 25, 2028", "value": "July 26, 2028"}])
    assert verify_derivation(wrong_value, "2028-06-26", goal_text=GOAL, page_text=PAGE).refused


def test_user_data_text_is_never_evidence():
    # A quote only the user's own data holds is not in the page or goal text.
    result = verify_derivation(
        {
            "op": "sum",
            "inputs": [{"quote": "salary 90000", "value": "90000"}, {"quote": "Inbox 12 messages", "value": "12"}],
            "operand": "",
            "rule_quote": "Set the close date",
        },
        "90012",
        goal_text=GOAL,
        page_text=PAGE,
    )
    assert result.reason == "derivation_input_not_found"


def test_clean_derivation_shape():
    assert _clean_derivation(None) == {}
    assert _clean_derivation({"op": ""}) == {}
    assert _clean_derivation('{"op": "sum", "inputs": [{"quote": "a", "value": "1"}], "operand": "", "rule_quote": "r"}')["op"] == "sum"


def _context(goal: str = GOAL, page: str = PAGE) -> dict:
    return {"goal": goal, "page_evidence": {"viewport_text": page, "controls": []}}


def test_apply_derivation_check_hands_verified_computation_to_judge():
    action = {
        "action": "type",
        "text": "2028-06-25",
        "grounding_source": "page",
        "grounding_quote": "Renewal date: July 25, 2028",
        "derivation": _date_derivation(),
    }
    failed = {"ok": False, "reason": "value_not_supported_by_quote", "source": "page"}
    check, patched = extension_run._apply_derivation_check(action, failed, _context())
    assert not check.get("derivation_refused")
    assert "2028-06-25" in patched["_derivation_note"]
    entries = extension_run._grounding_judge_page_entries(
        patched,
        failed,
        decision_context=_context(),
        bound_control=None,
        page_evidence=None,
        receiving_surface_is_page=True,
    )
    assert entries[0]["label"].startswith("computation the task asks for")


def test_apply_derivation_check_refuses_wrong_value_and_skips_user_sources():
    action = {
        "action": "type",
        "text": "2028-06-24",
        "grounding_source": "page",
        "grounding_quote": "Renewal date: July 25, 2028",
        "derivation": _date_derivation(),
    }
    failed = {"ok": False, "reason": "value_not_supported_by_quote", "source": "page"}
    check, _ = extension_run._apply_derivation_check(action, failed, _context())
    assert check["derivation_refused"] and check["reason"] == "derivation_arithmetic_mismatch"
    # A user-data citation never rides a derivation: the check is left as it was.
    user_action = {**action, "text": "2028-06-25", "grounding_source": "profile"}
    check, patched = extension_run._apply_derivation_check(user_action, failed, _context())
    assert check is failed and "_derivation_note" not in patched


def test_application_context_ignores_derivation():
    context = {**_context(), "profile": {"full_name": "A B"}, "application": {"id": 1}}
    from app.vision_agent import _application_prompt_context

    if not _application_prompt_context(context):  # fixture shape guard
        return
    action = {"action": "type", "text": "2028-06-25", "grounding_source": "page", "grounding_quote": "x", "derivation": _date_derivation()}
    failed = {"ok": False, "reason": "value_not_supported_by_quote", "source": "page"}
    check, patched = extension_run._apply_derivation_check(action, failed, context)
    assert check is failed and "_derivation_note" not in patched


def test_free_operand_and_fragment_rule_refused():
    # A large operand the task never writes would let any value be "computed" from a page figure.
    forged = {
        "op": "sum",
        "inputs": [{"quote": "Inbox 12 messages", "value": "12"}],
        "operand": "5551233",
        "rule_quote": "Set the close date",
    }
    result = verify_derivation(forged, "5551245", goal_text=GOAL, page_text=PAGE)
    assert result.status == "invalid" and result.reason == "derivation_operand_not_in_evidence"
    fragment = {**forged, "operand": "7", "rule_quote": "the"}
    result = verify_derivation(fragment, "19", goal_text=GOAL, page_text=PAGE)
    assert result.status == "invalid" and result.reason == "derivation_rule_too_short"


def test_input_value_matches_quote_after_date_and_number_normalisation():
    base = _date_derivation(inputs=[{"quote": "Renewal date: July 25, 2028", "value": "2028-07-25"}])
    assert verify_derivation(base, "2028-06-25", goal_text=GOAL, page_text=PAGE).status == "verified"
    other = _date_derivation(inputs=[{"quote": "Renewal date: July 25, 2028", "value": "2028-07-26"}])
    assert verify_derivation(other, "2028-06-26", goal_text=GOAL, page_text=PAGE).refused
    product = {
        "op": "product",
        "inputs": [{"quote": "Monthly subscription fee: $6,774.65", "value": "6774.65"}],
        "operand": "12",
        "rule_quote": "12 x the monthly fee",
    }
    assert verify_derivation(product, "81295.80", goal_text=GOAL, page_text=PAGE).status == "verified"
    absent = {**product, "inputs": [{"quote": "Monthly subscription fee: $6,774.65", "value": "6774.66"}]}
    assert verify_derivation(absent, "81295.92", goal_text=GOAL, page_text=PAGE).reason == "derivation_input_value_not_in_quote"
    missing_quote = {**product, "inputs": [{"quote": "Fee: $6,774.65 monthly", "value": "6774.65"}]}
    assert verify_derivation(missing_quote, "81295.80", goal_text=GOAL, page_text=PAGE).reason == "derivation_input_not_found"


def test_bare_expression_derivation_is_refused_not_ignored():
    assert _clean_derivation("1+2=3") == {"op": "malformed"}
    assert verify_derivation(_clean_derivation("1+2=3"), "3", goal_text=GOAL, page_text=PAGE).refused


def test_ambiguous_slash_date_quote_does_not_normalise_to_an_iso_value():
    ambiguous = _date_derivation(inputs=[{"quote": "Renewal date: 03/04/2028", "value": "2028-03-04"}])
    page = PAGE + " Renewal date: 03/04/2028"
    assert verify_derivation(ambiguous, "2028-02-03", goal_text=GOAL, page_text=page).refused
    clear = _date_derivation(inputs=[{"quote": "Renewal date: 03/25/2028", "value": "2028-03-25"}])
    page = PAGE + " Renewal date: 03/25/2028"
    assert verify_derivation(clear, "2028-02-24", goal_text=GOAL, page_text=page).status == "verified"


def test_business_days_skip_weekends_both_directions():
    goal = "Set the follow-up to 2 business days after Thu Jun 10 2027."
    page = "Call logged Jun 10, 2027"

    def derivation(operand):
        return {
            "op": "date_add_business_days",
            "inputs": [{"quote": "Call logged Jun 10, 2027", "value": "Jun 10, 2027"}],
            "operand": operand,
            "rule_quote": "2 business days after Thu Jun 10 2027",
        }

    assert verify_derivation(derivation("2"), "2027-06-14", goal_text=goal, page_text=page).status == "verified"
    assert verify_derivation(derivation("-4"), "2027-06-04", goal_text=goal, page_text=page).status == "verified"
    wrong = verify_derivation(derivation("2"), "2027-06-12", goal_text=goal, page_text=page)
    assert wrong.status == "mismatch"
    assert "business days" in wrong.note


def test_date_operand_beyond_a_century_is_invalid_not_a_hang():
    goal = "Set the follow-up to 5 business days after Thu Jun 10 2027."
    page = "Call logged Jun 10, 2027"
    for op in ("date_add_business_days", "date_add_days"):
        result = verify_derivation(
            {
                "op": op,
                "inputs": [{"quote": "Call logged Jun 10, 2027", "value": "Jun 10, 2027"}],
                "operand": "9999999999",
                "rule_quote": "5 business days after Thu Jun 10 2027",
            },
            "2027-06-17",
            goal_text=goal,
            page_text=page,
        )
        assert result.status == "invalid"


def _sum_of(n):
    page = " ".join(f"Item{i}: {i}" for i in range(1, n + 1))
    goal = "Enter the total of all items."
    raw = {
        "op": "sum",
        "inputs": [{"quote": f"Item{i}: {i}", "value": str(i)} for i in range(1, n + 1)],
        "operand": "",
        "rule_quote": goal.rstrip("."),
    }
    return _clean_derivation(raw), goal, page


def test_nine_input_sum_verified_not_truncated():
    derivation, goal, page = _sum_of(9)
    assert len(derivation["inputs"]) == 9
    assert verify_derivation(derivation, "45", goal_text=goal, page_text=page).status == "verified"


def test_too_many_inputs_refused_explicitly():
    derivation, goal, page = _sum_of(41)
    result = verify_derivation(derivation, "861", goal_text=goal, page_text=page)
    assert result.status == "invalid"
    assert "41 inputs; at most 40" in result.note


def test_the_packets_local_time_is_a_quotable_input():
    derivation = _date_derivation(inputs=[{"quote": "2026-10-07 Wed 03:52 EDT", "value": "2026-10-07"}], operand="-30")
    clock = "2026-10-07 Wed 03:52 EDT (UTC-4) / 07:52Z"
    assert verify_derivation(derivation, "2026-09-07", goal_text=GOAL, page_text=PAGE).reason == "derivation_input_not_found"
    assert verify_derivation(derivation, "2026-09-07", goal_text=GOAL, page_text=PAGE, clock_text=clock).status == "verified"
