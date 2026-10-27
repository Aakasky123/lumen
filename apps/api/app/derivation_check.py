"""Deterministic checks for a value the task derives from page or goal data.

A total, a difference, a growth percentage, a x12 annualisation or "30 days before the renewal date"
can never sit inside a single quote. The brain states the computation (`derivation`): the operation,
each input as a verbatim quote plus the value read from it, an optional operand, and the quote of the
rule that asks for the computation. This module checks only what needs no judgment:

* every input quote and the rule quote occur verbatim in the page or goal text (never a user source, so
  a derivation can never ground a fact about the user);
* each input value occurs inside its own quote;
* the arithmetic (sum, difference, product, quotient, percent change, date +/- days or business days, Mon-Fri, no holidays) reproduces the
  proposed value exactly.

Whether the rule fits the control stays with the grounding judge, which is shown the verified
computation. Anything that cannot be checked here is `unchecked` (the judge decides) and anything wrong
is refused: fail-closed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

# Exact lowercase synonyms a brain keeps guessing, read as the canonical op (no fuzzy matching).
DERIVATION_OP_SYNONYMS = {"multiply": "product", "add": "sum", "plus": "sum", "subtract": "difference", "minus": "difference", "divide": "quotient"}
DERIVATION_OPERATIONS = ("sum", "difference", "product", "quotient", "percent_change", "date_add_days", "date_add_business_days", "count")

_MAX_INPUTS = 40

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%B %d, %Y",
    "%b %d, %Y",
    "%B %d %Y",
    "%b %d %Y",
    "%d %B %Y",
    "%d %b %Y",
    "%Y/%m/%d",
    "%m/%d/%Y",
)


@dataclass(frozen=True)
class DerivationResult:
    # "none": no derivation given; "verified": inputs found and arithmetic reproduces the value;
    # "unchecked": inputs found, the arithmetic could not be machine-checked (the judge decides);
    # "invalid": a quote is missing or malformed; "mismatch": the arithmetic gives another value.
    status: str
    reason: str = ""
    note: str = ""

    @property
    def refused(self) -> bool:
        return self.status in {"invalid", "mismatch"}


def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def _alnum(text: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def _quote_in(quote: str, blob: str) -> bool:
    needle = _norm(quote)
    if not needle:
        return False
    if needle in _norm(blob):
        return True
    squashed = _alnum(quote)
    return bool(squashed) and squashed in _alnum(blob)


def _parse_number(text: Any) -> Decimal | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    negative = raw.startswith("(") and raw.endswith(")") or raw.startswith("-") or raw.startswith("−")
    cleaned = re.sub(r"[^0-9.,]", "", raw)
    if not re.search(r"\d", cleaned):
        return None
    cleaned = cleaned.replace(",", "")
    if cleaned.count(".") > 1:
        return None
    try:
        number = Decimal(cleaned)
    except InvalidOperation:
        return None
    return -number if negative else number


def _parse_date(text: Any) -> date | None:
    raw = re.sub(r"\s+", " ", str(text or "")).strip().rstrip(".")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


_DATE_TOKEN = re.compile(
    r"\d{4}-\d{1,2}-\d{1,2}|\d{4}/\d{1,2}/\d{1,2}|\d{1,2}/\d{1,2}/\d{4}"
    r"|[A-Za-z]{3,9}\.? \d{1,2},? \d{4}|\d{1,2} [A-Za-z]{3,9}\.?,? \d{4}"
)
_NUMBER_TOKEN = re.compile(r"[(\-−]?[$€£]?\s?\d[\d,]*(?:\.\d+)?\)?")


def _ambiguous_slash_date(token: str) -> bool:
    """'03/04/2027' reads as March 4 or April 3: never a basis for matching a differently written date."""
    match = re.fullmatch(r"(\d{1,2})/(\d{1,2})/\d{4}", token.strip())
    if not match:
        return False
    first, second = int(match.group(1)), int(match.group(2))
    return first != second and first <= 12 and second <= 12


def _value_in_quote_normalized(value: str, quote: str) -> bool:
    """The value equals something the quote states, in another unambiguous written form: a date
    ('2027-09-18' for 'September 18, 2027') or a number ('18646.89' for '$18,646.89'). The quote itself
    must still be found verbatim in the evidence; only the value's spelling is normalised."""
    value_date = _parse_date(value)
    if value_date is not None:
        for token in _DATE_TOKEN.findall(quote):
            if _ambiguous_slash_date(token):
                continue
            if _parse_date(token) == value_date:
                return True
        return False
    value_number = _parse_number(value)
    if value_number is not None and re.fullmatch(r"[\s(\-−+$€£]*\d[\d,]*(?:\.\d+)?\)?\s*", value):
        for token in _NUMBER_TOKEN.findall(quote):
            if _parse_number(token) == value_number:
                return True
    return False


def _decimal_places(text: str) -> int:
    match = re.search(r"\.(\d+)\s*[^\d]*$", text.strip())
    return len(match.group(1)) if match else 0


def _numbers_equal(computed: Decimal, proposed_text: str) -> bool | None:
    proposed = _parse_number(proposed_text)
    if proposed is None:
        return None
    places = _decimal_places(proposed_text)
    quantum = Decimal(1).scaleb(-places)
    try:
        return computed.quantize(quantum, rounding=ROUND_HALF_UP) == proposed.quantize(quantum, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None


def _compute_number(op: str, values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    try:
        if op == "sum" and len(values) >= 2:
            return sum(values, Decimal(0))
        if op == "difference" and len(values) >= 2:
            result = values[0]
            for item in values[1:]:
                result -= item
            return result
        if op == "product" and len(values) >= 2:
            result = Decimal(1)
            for item in values:
                result *= item
            return result
        if op == "quotient" and len(values) == 2 and values[1] != 0:
            return values[0] / values[1]
        if op == "percent_change" and len(values) == 2 and values[0] != 0:
            return (values[1] - values[0]) / values[0] * Decimal(100)
    except (InvalidOperation, ArithmeticError):
        return None
    return None


def _add_business_days(start: date, count: int) -> date:
    """Step `count` Monday-Friday days from `start` (negative goes back); holidays are not modelled.
    From a weekend start, the first step lands on the nearest weekday in the direction of travel."""
    step = 1 if count >= 0 else -1
    current = start
    remaining = abs(count)
    while remaining:
        current += timedelta(days=step)
        if current.weekday() < 5:
            remaining -= 1
    return current


def verify_derivation(
    derivation: Any,
    proposed_value: Any,
    *,
    goal_text: str,
    page_text: str,
    clock_text: str = "",
) -> DerivationResult:
    """Check a brain-stated derivation against the allowed evidence: the page and goal text, and for an
    input quote also the prompt's own clock line (`local_time`, the run's dated local time: harness-provided
    truth, so "today" can be an input to a date rule)."""
    if derivation and not isinstance(derivation, dict):
        return DerivationResult(
            "invalid",
            "derivation_malformed",
            "derivation must be an object {op, inputs:[{quote, value}], operand?, rule_quote}, not a bare expression",
        )
    if not isinstance(derivation, dict):
        return DerivationResult("none")
    op = str(derivation.get("op") or "").strip().lower()
    op = DERIVATION_OP_SYNONYMS.get(op, op)
    if not op:
        return DerivationResult("none")
    if op == "malformed":
        # _clean_derivation marks a derivation that arrived as a bare string or other non-object.
        return DerivationResult(
            "invalid",
            "derivation_malformed",
            "derivation must be a JSON OBJECT, not a string or expression, with keys op (one of "
            f"{', '.join(DERIVATION_OPERATIONS)}), inputs (a list of {{quote, value}} objects), operand, rule_quote",
        )
    if op not in DERIVATION_OPERATIONS:
        return DerivationResult("invalid", "derivation_operation_unknown", f"operation {op!r} is not one of {', '.join(DERIVATION_OPERATIONS)}")
    inputs_raw = derivation.get("inputs")
    inputs = [item for item in (inputs_raw if isinstance(inputs_raw, list) else []) if isinstance(item, dict)]
    if len(inputs) > _MAX_INPUTS:
        return DerivationResult("invalid", "derivation_too_many_inputs", f"derivation has {len(inputs)} inputs; at most {_MAX_INPUTS}")
    if not inputs:
        return DerivationResult("invalid", "derivation_inputs_missing", "no input quotes were given")
    rule_quote = str(derivation.get("rule_quote") or "").strip()
    operand = str(derivation.get("operand") or "").strip()
    evidence = f"{goal_text or ''}\n{page_text or ''}"

    verified_inputs: list[tuple[str, str]] = []
    for item in inputs:
        quote = str(item.get("quote") or "").strip()
        value = str(item.get("value") or "").strip()
        if not quote or not value:
            return DerivationResult("invalid", "derivation_input_incomplete", "every input needs a quote and the value read from it")
        if not _quote_in(quote, evidence) and not (clock_text and _quote_in(quote, clock_text)):
            return DerivationResult("invalid", "derivation_input_not_found", f"input quote not found verbatim in the page or goal text: {quote[:80]!r}")
        if (
            _norm(value) not in _norm(quote)
            and _alnum(value) not in _alnum(quote)
            and not _value_in_quote_normalized(value, quote)
        ):
            return DerivationResult("invalid", "derivation_input_value_not_in_quote", f"input value {value[:40]!r} is not inside its own quote")
        verified_inputs.append((quote, value))
    if not rule_quote:
        return DerivationResult("invalid", "derivation_rule_missing", "`rule_quote` is empty: copy verbatim, from the goal or page, the sentence of the task that asks for this computation")
    if len(_alnum(rule_quote)) < 8:
        return DerivationResult("invalid", "derivation_rule_too_short", "the rule quote must be the task's own words asking for the computation, not a fragment")
    if not _quote_in(rule_quote, evidence):
        return DerivationResult("invalid", "derivation_rule_not_found", f"rule quote not found verbatim in the page or goal text: {rule_quote[:80]!r}")

    proposed = str(proposed_value or "").strip()
    description = "; ".join(f"{value} (quote: \"{quote[:100]}\")" for quote, value in verified_inputs)
    rule_note = f"rule quoted from the task: \"{rule_quote[:200]}\""

    if op == "count":
        # A count across pages is judged: only the quotes were checked here.
        return DerivationResult("unchecked", note=f"count of the entries quoted: {description}; {rule_note}; claimed result {proposed}")

    if op in {"date_add_days", "date_add_business_days"}:
        business = op == "date_add_business_days"
        unit = "business days" if business else "days"
        base = _parse_date(verified_inputs[0][1])
        try:
            days = int(re.sub(r"\s+", "", operand).replace("−", "-"))
        except ValueError:
            return DerivationResult("invalid", "derivation_operand_missing", f"{op} needs a signed whole number of {unit} as the operand")
        if abs(days) > 36500:
            # A century of days is no task rule; it would also overflow the calendar or spin the business-day walk.
            return DerivationResult("invalid", "derivation_operand_missing", f"{op} operand must be a signed whole number of {unit} within 36500")
        target = _parse_date(proposed)
        if base is None or target is None:
            return DerivationResult("unchecked", note=f"{description}; {days:+d} {unit}; {rule_note}; the date arithmetic could not be machine-checked")
        expected = _add_business_days(base, days) if business else base + timedelta(days=days)
        if expected != target:
            return DerivationResult(
                "mismatch",
                "derivation_arithmetic_mismatch",
                f"{base.isoformat()} {days:+d} {unit} is {expected.isoformat()}, not {target.isoformat()}",
            )
        return DerivationResult(
            "verified",
            note=f"{description}; {days:+d} {unit} gives {expected.isoformat()} (calendar arithmetic checked by code, equal to the proposed value); {rule_note}",
        )

    values: list[Decimal] = []
    for _quote, value in verified_inputs:
        number = _parse_number(value)
        if number is None:
            return DerivationResult("invalid", "derivation_input_not_numeric", f"input value {value[:40]!r} is not a number")
        values.append(number)
    if operand:
        number = _parse_number(operand)
        if number is None:
            return DerivationResult("invalid", "derivation_operand_not_numeric", f"operand {operand[:40]!r} is not a number")
        if abs(number) > 100 and _alnum(operand.lstrip("-−+")) not in _alnum(evidence):
            # An operand the rule states (x12, 30 days) is small or written in the task; a large free number
            # would let any value be "computed" from a page figure.
            return DerivationResult("invalid", "derivation_operand_not_in_evidence", f"operand {operand[:40]!r} is neither small nor written in the page or goal text")
        values.append(number)
    computed = _compute_number(op, values)
    if computed is None:
        return DerivationResult("invalid", "derivation_arithmetic_unsupported", f"{op} cannot be computed from {len(values)} numbers")
    equal = _numbers_equal(computed, proposed)
    if equal is None:
        return DerivationResult("unchecked", note=f"{description}; {op} gives {computed}; {rule_note}; the proposed value is not a plain number")
    if not equal:
        return DerivationResult("mismatch", "derivation_arithmetic_mismatch", f"{op} of the quoted values gives {computed.normalize():f}, not {proposed}")
    return DerivationResult(
        "verified",
        note=f"{description}{f'; operand {operand}' if operand else ''}; {op} gives {computed.normalize():f} (arithmetic checked by code, equal to the proposed value); {rule_note}",
    )


def derivation_found_source(derivation: Any, goal_text: str, page_text: str) -> str:
    """The source ("goal" or "page") whose text holds the rule quote, else ""."""
    quote = str((derivation or {}).get("rule_quote") or "") if isinstance(derivation, dict) else ""
    if _quote_in(quote, goal_text or ""):
        return "goal"
    if _quote_in(quote, page_text or ""):
        return "page"
    return ""
