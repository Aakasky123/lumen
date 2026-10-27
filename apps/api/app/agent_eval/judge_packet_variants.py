"""Named transforms of a retained grounding-judge request, for speed A/Bs in judge_replay.

A variant rewrites the retained request (system, prompt dict, schema) before it is sent, and may
force the transport's reasoning switch. Variants compose with "+" (``terse+stable_first``). Eval
tooling only: production judge wording, schema, and model are untouched.

  baseline       unchanged.
  reasoning_off  same packet; the leg forces reasoning off at the transport
                 (model_provider.eval_transport_overrides -> reasoning.enabled false, or the
                 model's least-reasoning tier when its catalog entry says reasoning is mandatory).
  verdict_first  schema properties/required and response_shape reordered so the verdict fields
                 come first and ``reason`` last; reason capped to "at most 20 words". Rules unchanged.
  terse          verdict_first plus one appended rule (TERSE_RULE).
  stable_first   top-level prompt keys reordered static-first (task, rules, response_shape) ahead
                 of the per-call keys (control, proposed_value, entries, page_evidence, ...).
  reasoning_budget_N
                 same packet; the OpenRouter reasoning block becomes {"max_tokens": N} in place of
                 the row's effort (Qwen 3.8 Flash has no effort tiers, so effort does nothing there).
                 The call's overall max_tokens is unchanged.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

VERDICT_FIRST_ORDER = (
    "grounded",
    "entry_index",
    "evidence_status",
    "instrumental",
    "commits_or_destroys",
    "support_quote",
)
REASON_CAP = "at most 20 words"
TERSE_RULE = "Decide from the entries; do not deliberate at length; the reason is at most 20 words."
STATIC_PROMPT_KEYS = ("task", "rules", "response_shape")


def _verdict_first_keys(keys: list[str]) -> list[str]:
    lead = [key for key in VERDICT_FIRST_ORDER if key in keys]
    middle = [key for key in keys if key not in VERDICT_FIRST_ORDER and key != "reason"]
    return lead + middle + (["reason"] if "reason" in keys else [])


def _reorder(mapping: dict[str, Any], order: list[str]) -> dict[str, Any]:
    return {key: mapping[key] for key in order}


def verdict_first(request: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(request)
    schema = out.get("schema")
    body = schema.get("schema") if isinstance(schema, dict) else None
    if isinstance(body, dict) and isinstance(body.get("properties"), dict):
        properties = body["properties"]
        properties = _reorder(properties, _verdict_first_keys(list(properties)))
        if isinstance(properties.get("reason"), dict):
            properties["reason"] = {**properties["reason"], "description": REASON_CAP}
        body["properties"] = properties
        if isinstance(body.get("required"), list):
            body["required"] = _verdict_first_keys(list(body["required"]))
    prompt = out.get("prompt")
    if isinstance(prompt, dict) and isinstance(prompt.get("response_shape"), dict):
        shape = prompt["response_shape"]
        shape = _reorder(shape, _verdict_first_keys(list(shape)))
        if "reason" in shape:
            shape["reason"] = REASON_CAP
        prompt["response_shape"] = shape
    return out


def terse(request: dict[str, Any]) -> dict[str, Any]:
    out = verdict_first(request)
    prompt = out.get("prompt")
    if isinstance(prompt, dict):
        rules = prompt.get("rules")
        if isinstance(rules, list):
            prompt["rules"] = [*rules, TERSE_RULE]
        elif isinstance(rules, str):
            prompt["rules"] = f"{rules}\n{TERSE_RULE}"
        else:
            prompt["rules"] = [TERSE_RULE]
    return out


def stable_first(request: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(request)
    prompt = out.get("prompt")
    if isinstance(prompt, dict):
        static = [key for key in STATIC_PROMPT_KEYS if key in prompt]
        out["prompt"] = _reorder(prompt, static + [key for key in prompt if key not in STATIC_PROMPT_KEYS])
    return out


def _unchanged(request: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(request)


@dataclass(frozen=True)
class Variant:
    name: str
    transform: Callable[[dict[str, Any]], dict[str, Any]]
    # Transport overrides (model_provider.eval_transport_overrides): reasoning_enabled forces the
    # reasoning switch; reasoning replaces the OpenRouter reasoning block (e.g. a token budget).
    reasoning_enabled: bool | None = None
    reasoning: dict[str, Any] | None = None


VARIANTS: dict[str, Variant] = {
    "baseline": Variant("baseline", _unchanged),
    "reasoning_off": Variant("reasoning_off", _unchanged, reasoning_enabled=False),
    "verdict_first": Variant("verdict_first", verdict_first),
    "terse": Variant("terse", terse),
    "stable_first": Variant("stable_first", stable_first),
}
_BUDGET_PREFIX = "reasoning_budget_"


def _variant(name: str) -> Variant | None:
    if name in VARIANTS:
        return VARIANTS[name]
    # reasoning_budget_N: same packet, reasoning {"max_tokens": N} in place of the row's effort; the
    # call's overall max_tokens is unchanged.
    if name.startswith(_BUDGET_PREFIX) and name[len(_BUDGET_PREFIX):].isdigit() and int(name[len(_BUDGET_PREFIX):]) > 0:
        return Variant(name, _unchanged, reasoning={"max_tokens": int(name[len(_BUDGET_PREFIX):])})
    return None


def parse_variant(spec: str) -> tuple[str, list[Variant]]:
    """``name[+name...]`` -> (label, variants in application order). Unknown names raise ValueError."""
    names = [part.strip() for part in str(spec or "baseline").split("+") if part.strip()] or ["baseline"]
    unknown = [name for name in names if _variant(name) is None]
    if unknown:
        raise ValueError(f"unknown variant(s) {unknown}; known: {sorted(VARIANTS)} and {_BUDGET_PREFIX}N")
    return "+".join(names), [_variant(name) for name in names]


def apply_variant(spec: str, request: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The transformed request and the transport overrides the leg forces
    ({"reasoning_enabled": bool | None, "reasoning": dict | None}; None = the transport's own)."""
    _, variants = parse_variant(spec)
    out = request
    overrides: dict[str, Any] = {"reasoning_enabled": None, "reasoning": None}
    for variant in variants:
        out = variant.transform(out)
        if variant.reasoning_enabled is not None:
            overrides["reasoning_enabled"] = variant.reasoning_enabled
        if variant.reasoning is not None:
            overrides["reasoning"] = dict(variant.reasoning)
    return out, overrides


def prompt_key_order(request: dict[str, Any]) -> list[str]:
    prompt = request.get("prompt")
    return list(prompt) if isinstance(prompt, dict) else []
