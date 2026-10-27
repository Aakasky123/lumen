"""Replay retained grounding-judge attempts against candidate legs and score verdict agreement.

Every grounding-judge model attempt retains its exact input and outcome under
<artifact_root>/runs/<run>/judge-trajectory/ (app.grounding_judge_retention). This module replays
the attempts that gave a verdict through the production transport
(LumenModelClient._call_model_json_unrecorded, purpose grounding_judge, the recorded max-token and
timeout overrides, the recorded screenshot) for each leg and scores, per leg:

  * agreement     — normalized grounded and entry_index versus the recorded verdict;
  * flips         — recorded grounded -> candidate not (lost support), and recorded not grounded ->
                    candidate grounded (the dangerous direction: the gate would let a value through);
  * invalid       — provider errors, exceptions, and replies without a valid verdict;
  * latency       — p50 / p95 of the transport's own latency_ms;
  * tokens        — mean prompt, completion, and (when usage reports them) reasoning tokens;
  * length-capped — replies that stopped at the token cap (finish_reason "length");
  * cost          — estimated from PRICES_PER_MILLION, and the provider-reported cost when present.

A verdict is normalized as the judge normalizes it: grounded only when ``grounded`` is true and
``entry_index`` names one of the packet's entries; otherwise not grounded with entry_index -1.

A leg is candidate x variant x route:
  * candidates use model_replay's syntax: ``model``, ``model@effort`` (reasoning tier), and a
    ``:json_object`` or ``:strict`` suffix (no suffix keeps the base row's mode). The base row (its
    key, host, and transport) is --base-provider, else the configured grounding-judge provider
    (its own designation, else the judge row).
    ``--include-champion`` replays the base row unchanged.
  * ``--variant NAME`` (repeatable; judge_packet_variants: baseline, reasoning_off, verdict_first,
    terse, stable_first, reasoning_budget_N, composable with "+") transforms the retained packet
    before sending and/or overrides the transport's reasoning block.
  * ``--route SPEC`` (repeatable) adds OpenRouter provider routing to the leg: ``sort=throughput``,
    ``order=HostA,HostB``, ``only=...``, ``ignore=...``, ``allow_fallbacks=false``, several joined
    with ";"; ``default`` keeps the row's own routing. require_parameters keeps its transport
    semantics (it cannot be set here).
``--repeat N`` replays every leg N times, so the champion's own agreement with its recorded verdicts
is the noise floor a candidate's numbers must be read against.

Records come from the artifact root (--runs / --days), or from --records-manifest (a JSON list of
record file paths, or {"records": [{"path", "replay_usable", "first_verdict_of_packet"}, ...]}),
--records-dir (any tree holding grounding_judge_attempt_v1 records), and --gold-dir (a labeled case
set: index.json + case files; a candidate grounded=true on a must-be-false case is UNSAFE).
--sample N --seed S keeps every recorded-not-grounded record and gold case and samples N grounded.

Budget guard: on an OpenRouter base row, the account's remaining credits (GET /api/v1/credits) are
checked before the first batch and every --check-every calls; the run aborts below --credit-floor
USD (default 3.0). --max-spend USD aborts once the estimated spend of the replay exceeds it.

Usage (from apps/api):
  python -m app.agent_eval.judge_replay --limit 40 --include-champion \
      --variant baseline --variant reasoning_off --variant terse --max-spend 1.00
  python -m app.agent_eval.judge_replay --records-manifest corpus.json --include-champion \
      --variant baseline --route default --route sort=throughput --repeat 2

Rows append to <artifact_root>/eval_scores/judge_replay/<timestamp>.jsonl, the summary beside it.
"""

from __future__ import annotations

import argparse
import dataclasses
import itertools
import json
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.agent_eval.judge_packet_variants import apply_variant, parse_variant, prompt_key_order
from app.model_usage import MODEL_PRICES

CHAMPION_LABEL = "champion"
INVALID_OUTCOMES = ("provider_error", "exception", "invalid")
OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/credits"
DEFAULT_CREDIT_FLOOR_USD = 3.0
DEFAULT_CHECK_EVERY = 50
# USD per million tokens (input, output). Completion tokens include hidden reasoning tokens.
PRICES_PER_MILLION: dict[str, tuple[float, float]] = MODEL_PRICES
_ROUTE_LIST_KEYS = ("order", "only", "ignore")
_ROUTE_SORTS = ("latency", "throughput", "price")

# transport(provider, record, images) -> the _call_model_json result dict. The record's "request" is
# the leg's (variant-transformed) packet.
Transport = Callable[[Any, dict[str, Any], list[dict[str, Any]]], dict[str, Any]]


@dataclasses.dataclass(frozen=True)
class Leg:
    label: str
    provider: Any
    candidate: str | None = None
    variant: str = "baseline"
    route: dict[str, Any] | None = None
    route_label: str | None = None


def _as_leg(item: Any) -> Leg:
    if isinstance(item, Leg):
        return item
    label, provider = item
    return Leg(label=str(label), provider=provider, candidate=str(label))


# ---------------------------------------------------------------------------------------- scoring


def normalize_verdict(data: Any, entry_count: int) -> dict[str, Any] | None:
    """The judge's own normalization of one reply; None when the reply is no valid verdict."""
    if not isinstance(data, dict):
        return None
    grounded = data.get("grounded")
    entry_index = data.get("entry_index")
    if not isinstance(grounded, bool) or type(entry_index) is not int or not isinstance(data.get("reason"), str):
        return None
    if grounded and 0 <= entry_index < max(0, int(entry_count)):
        return {"grounded": True, "entry_index": entry_index}
    return {"grounded": False, "entry_index": -1}


def recorded_verdict(record: dict[str, Any]) -> dict[str, Any] | None:
    outcome = record.get("outcome") or {}
    if not outcome.get("ok"):
        return None
    return normalize_verdict(outcome.get("verdict"), int((record.get("request") or {}).get("entry_count") or 0))


def score_reply(recorded: dict[str, Any], candidate: dict[str, Any] | None) -> dict[str, Any]:
    """Agreement and flip direction of one candidate verdict against the recorded one."""
    if candidate is None:
        return {"outcome": "invalid"}
    grounded_match = candidate["grounded"] == recorded["grounded"]
    return {
        "outcome": "valid",
        "candidate_grounded": candidate["grounded"],
        "candidate_entry_index": candidate["entry_index"],
        "grounded_match": grounded_match,
        "entry_match": grounded_match and candidate["entry_index"] == recorded["entry_index"],
        "flip": (
            None
            if grounded_match
            else ("grounded_to_not" if recorded["grounded"] else "not_to_grounded")
        ),
    }


def estimate_cost(model: str | None, prompt_tokens: Any, completion_tokens: Any) -> float | None:
    price = PRICES_PER_MILLION.get(str(model or "").strip().lower())
    if price is None or prompt_tokens is None or completion_tokens is None:
        return None
    return (int(prompt_tokens) * price[0] + int(completion_tokens) * price[1]) / 1_000_000


def _reasoning_tokens(usage: dict[str, Any]) -> int | None:
    details = usage.get("completion_tokens_details")
    if isinstance(details, dict) and details.get("reasoning_tokens") is not None:
        return int(details["reasoning_tokens"])
    if usage.get("reasoning_tokens") is not None:
        return int(usage["reasoning_tokens"])
    return None


def replay_one(
    record: dict[str, Any],
    leg: Leg,
    transport: Transport,
    repeat_index: int = 0,
) -> dict[str, Any]:
    leg = _as_leg(leg)
    recorded = recorded_verdict(record)
    gold = record.get("_gold") if isinstance(record.get("_gold"), dict) else None
    original_request = record.get("request") or {}
    request, overrides = apply_variant(leg.variant, original_request)
    recorded_reason = ((record.get("outcome") or {}).get("verdict") or {}).get("reason")
    row: dict[str, Any] = {
        "model": leg.label,
        "candidate": leg.candidate,
        "variant": leg.variant,
        "route": leg.route_label,
        "repeat": repeat_index,
        "run_id": record.get("run_id"),
        "judge_call_id": record.get("judge_call_id"),
        "attempt": record.get("attempt"),
        "record": record.get("_path"),
        "recorded_model": (record.get("provider") or {}).get("model"),
        "recorded_effort": (record.get("provider") or {}).get("reasoning_effort"),
        "recorded_grounded": (recorded or {}).get("grounded"),
        "recorded_entry_index": (recorded or {}).get("entry_index"),
        "recorded_reason": str(recorded_reason or "")[:300],
        "image_sent": bool(original_request.get("image_sent")),
        "forced_reasoning_enabled": overrides["reasoning_enabled"],
        "forced_reasoning": overrides["reasoning"],
        "prompt_key_order_changed": prompt_key_order(request) != prompt_key_order(original_request),
        "gold_set": (gold or {}).get("set"),
        "gold_id": (gold or {}).get("id"),
        "gold_must_be_false": (gold or {}).get("must_be_false"),
    }
    started = time.perf_counter()
    try:
        from app.grounding_judge_retention import load_images
        from app.model_provider import eval_transport_overrides

        images = load_images(record) if original_request.get("image_sent") else []
        with eval_transport_overrides(
            reasoning_enabled=overrides["reasoning_enabled"], reasoning=overrides["reasoning"], routing=leg.route
        ):
            result = transport(leg.provider, {**record, "request": request}, images)
    except Exception as exc:  # noqa: BLE001 - a broken candidate must not sink the replay
        return {**row, "outcome": "exception", "error": str(exc)[:300], "latency_ms": int((time.perf_counter() - started) * 1000)}
    result = result if isinstance(result, dict) else {}
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    usage = metadata.get("token_usage") if isinstance(metadata.get("token_usage"), dict) else {}
    row["latency_ms"] = metadata.get("latency_ms") if metadata.get("latency_ms") is not None else int((time.perf_counter() - started) * 1000)
    row["completion_tokens"] = usage.get("completion_tokens")
    row["prompt_tokens"] = usage.get("prompt_tokens")
    row["reasoning_tokens"] = _reasoning_tokens(usage)
    row["cached_tokens"] = usage.get("cached_prompt_tokens")
    row["finish_reason"] = metadata.get("finish_reason")
    row["length_capped"] = str(metadata.get("finish_reason") or "").lower() in ("length", "max_tokens")
    row["effective_max_tokens"] = metadata.get("max_tokens")
    row["reasoning_enabled_at_transport"] = metadata.get("reasoning_enabled_at_transport")
    row["reasoning_mandatory_substitute"] = metadata.get("reasoning_mandatory_substitute")
    row["upstream_provider"] = metadata.get("upstream_provider")
    row["structured_output_mode"] = metadata.get("structured_output_mode")
    row["estimated_cost_usd"] = estimate_cost(
        metadata.get("model") or getattr(leg.provider, "model_name", None), row["prompt_tokens"], row["completion_tokens"]
    )
    row["reported_cost_usd"] = usage.get("cost") if isinstance(usage.get("cost"), (int, float)) else None
    if not result.get("ok"):
        return {**row, "outcome": "provider_error", "error": str(result.get("error") or "")[:300]}
    data = result.get("data") or {}
    candidate = normalize_verdict(data, int(original_request.get("entry_count") or 0))
    if recorded is None and gold is not None and candidate is not None:
        # A may-be-true gold case has no verdict to agree with: its reply is recorded, never scored.
        scored = {"outcome": "valid", "candidate_grounded": candidate["grounded"], "candidate_entry_index": candidate["entry_index"], "grounded_match": None, "entry_match": None, "flip": None}
    else:
        scored = score_reply(recorded or {}, candidate)
    if candidate is not None:
        scored["candidate_reason"] = str(data.get("reason") or "")[:300]
        scored["candidate_reason_words"] = len(str(data.get("reason") or "").split())
    return {**row, **scored}


def row_cost(row: dict[str, Any]) -> float:
    """What one replayed call counts against --max-spend: the estimate, else the reported cost."""
    for key in ("estimated_cost_usd", "reported_cost_usd"):
        if isinstance(row.get(key), (int, float)):
            return float(row[key])
    return 0.0


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[int(fraction * (len(ordered) - 1))]


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 1) if values else None


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries = []
    for label in dict.fromkeys(row["model"] for row in rows):
        subset = [row for row in rows if row["model"] == label]
        valid = [row for row in subset if row.get("outcome") == "valid"]
        latencies = [float(row["latency_ms"]) for row in subset if row.get("latency_ms") is not None]
        tokens = [int(row["completion_tokens"]) for row in subset if row.get("completion_tokens") is not None]
        prompt_tokens = [int(row["prompt_tokens"]) for row in subset if row.get("prompt_tokens") is not None]
        reasoning = [int(row["reasoning_tokens"]) for row in subset if row.get("reasoning_tokens") is not None]
        estimated = [float(row["estimated_cost_usd"]) for row in subset if row.get("estimated_cost_usd") is not None]
        reported = [float(row["reported_cost_usd"]) for row in subset if row.get("reported_cost_usd") is not None]
        first = subset[0]
        summaries.append(
            {
                "model": label,
                "candidate": first.get("candidate"),
                "variant": first.get("variant"),
                "route": first.get("route"),
                "n": len(subset),
                "valid": len(valid),
                "invalid": sum(1 for row in subset if row.get("outcome") in INVALID_OUTCOMES),
                "grounded_agree": sum(1 for row in valid if row.get("grounded_match")),
                "entry_agree": sum(1 for row in valid if row.get("entry_match")),
                "flips_grounded_to_not": sum(1 for row in valid if row.get("flip") == "grounded_to_not"),
                "flips_not_to_grounded": sum(1 for row in valid if row.get("flip") == "not_to_grounded"),
                "latency_p50_ms": _percentile(latencies, 0.5),
                "latency_p95_ms": _percentile(latencies, 0.95),
                "completion_tokens_mean": _mean(tokens),
                "completion_tokens_total": sum(tokens) if tokens else None,
                "prompt_tokens_mean": _mean(prompt_tokens),
                "reasoning_tokens_mean": _mean(reasoning),
                "reasoning_tokens_reported": len(reasoning),
                "length_capped": sum(1 for row in subset if row.get("length_capped")),
                "estimated_cost_usd": round(sum(estimated), 6) if estimated else None,
                "reported_cost_usd": round(sum(reported), 6) if reported else None,
                "reasoning_enabled_at_transport": dict(Counter(str(row.get("reasoning_enabled_at_transport")) for row in subset)),
                "reasoning_mandatory_substitute": dict(
                    Counter(str(row["reasoning_mandatory_substitute"]) for row in subset if row.get("reasoning_mandatory_substitute"))
                ),
                "upstream_providers": dict(Counter(str(row["upstream_provider"]) for row in subset if row.get("upstream_provider"))),
                "prompt_key_order_changed": sum(1 for row in subset if row.get("prompt_key_order_changed")),
                # Gold must-be-false cases (--gold-dir): a grounded reply there is UNSAFE (also counted in N->G).
                "gold_must_be_false": sum(1 for row in subset if row.get("gold_must_be_false")),
                "gold_unsafe": sum(1 for row in valid if row.get("gold_must_be_false") and row.get("candidate_grounded")),
            }
        )
    return summaries


def unsafe_flips(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every recorded-not-grounded -> candidate-grounded case, with both reasons."""
    return [
        {
            "leg": row["model"],
            "record": row.get("record"),
            "gold_id": row.get("gold_id"),
            "repeat": row.get("repeat"),
            "candidate_entry_index": row.get("candidate_entry_index"),
            "recorded_reason": row.get("recorded_reason"),
            "candidate_reason": row.get("candidate_reason"),
        }
        for row in rows
        if row.get("flip") == "not_to_grounded"
    ]


# ------------------------------------------------------------------------------------------- legs


def parse_candidate(spec: str, base: Any) -> tuple[str, Any]:
    """``model[@effort][:json_object|:strict]`` -> (label, provider row derived from base)."""
    label, _, mode = str(spec).partition(":")
    model_id, _, effort = label.partition("@")
    changes: dict[str, Any] = {
        "model_name": model_id or base.model_name,
        "display_name": f"replay:{spec}",
        "reasoning_effort": effort or base.reasoning_effort,
    }
    if mode in ("json_object", "strict"):
        changes["structured_output"] = mode
    return str(spec), dataclasses.replace(base, **changes)


def parse_route(spec: str | None) -> dict[str, Any] | None:
    """``key=value[;key=value]`` -> an OpenRouter routing override; None for ``default``/empty."""
    text = str(spec or "").strip()
    if not text or text.lower() in ("default", "none"):
        return None
    from app.model_provider import _ENDPOINT_TAG

    route: dict[str, Any] = {}
    for part in text.split(";"):
        key, sep, value = part.strip().partition("=")
        key, value = key.strip().lower(), value.strip()
        if not sep or not value:
            raise ValueError(f"route part {part!r} is not key=value")
        if key == "require_parameters":
            raise ValueError("require_parameters keeps its transport semantics; a route cannot set it")
        if key == "sort":
            if value.lower() not in _ROUTE_SORTS:
                raise ValueError(f"sort must be one of {_ROUTE_SORTS}")
            route["sort"] = value.lower()
        elif key in _ROUTE_LIST_KEYS:
            hosts = [host.strip() for host in value.split(",") if host.strip()]
            bad = [host for host in hosts if not _ENDPOINT_TAG.fullmatch(host.lower())]
            if not hosts or bad:
                raise ValueError(f"{key} needs comma-separated OpenRouter host tags (bad: {bad})")
            route[key] = hosts
        elif key == "allow_fallbacks":
            if value.lower() not in ("true", "false"):
                raise ValueError("allow_fallbacks must be true or false")
            route["allow_fallbacks"] = value.lower() == "true"
        else:
            raise ValueError(f"unknown route key {key!r}; known: sort, order, only, ignore, allow_fallbacks")
    return route


def build_legs(
    base: Any,
    *,
    candidates: list[str],
    include_champion: bool,
    variants: list[str] | None = None,
    routes: list[str] | None = None,
) -> list[Leg]:
    """candidates (champion first) x variants x routes."""
    candidate_rows: list[tuple[str, Any]] = []
    if include_champion or not candidates:
        candidate_rows.append((f"{CHAMPION_LABEL}:{base.model_name}@{base.reasoning_effort or 'default'}", base))
    candidate_rows.extend(parse_candidate(spec, base) for spec in candidates)
    variant_labels = [parse_variant(spec)[0] for spec in (variants or ["baseline"])]
    route_specs = [(str(spec).strip(), parse_route(spec)) for spec in (routes or ["default"])]
    legs = []
    for (candidate, provider), variant, (route_spec, route) in itertools.product(candidate_rows, variant_labels, route_specs):
        label = f"{candidate}|{variant}" + (f"|{route_spec}" if route else "")
        legs.append(Leg(label=label, provider=provider, candidate=candidate, variant=variant, route=route, route_label=route_spec if route else None))
    return legs


# ------------------------------------------------------------------------------------------ budget


class BudgetGuard:
    """Aborts a replay when the account's remaining credits fall under a floor, or when the replay's
    own estimated spend exceeds max_spend. A failed credit check aborts (fail closed)."""

    def __init__(
        self,
        credits: Callable[[], dict[str, Any]] | None,
        *,
        floor: float = DEFAULT_CREDIT_FLOOR_USD,
        max_spend: float | None = None,
        check_every: int = DEFAULT_CHECK_EVERY,
    ) -> None:
        self.credits = credits
        self.floor = float(floor)
        self.max_spend = None if max_spend is None else float(max_spend)
        self.check_every = max(1, int(check_every))
        self.spent = 0.0
        self.remaining: float | None = None
        self.credit_checks = 0
        self.abort_reason: str | None = None
        self._lock = threading.Lock()

    @property
    def stopped(self) -> bool:
        return self.abort_reason is not None

    def _abort(self, reason: str) -> None:
        with self._lock:
            if self.abort_reason is None:
                self.abort_reason = reason

    def check_credits(self) -> None:
        if self.credits is None or self.stopped:
            return
        self.credit_checks += 1
        try:
            data = self.credits()
            remaining = float(data["total_credits"]) - float(data["total_usage"])
        except Exception as exc:  # noqa: BLE001 - an unknown balance is not a safe balance
            self._abort(f"credit check failed ({type(exc).__name__}: {str(exc)[:120]})")
            return
        self.remaining = remaining
        if remaining < self.floor:
            self._abort(f"remaining credits ${remaining:.2f} under the ${self.floor:.2f} floor")

    def charge(self, cost: float) -> None:
        with self._lock:
            self.spent += float(cost or 0.0)
            over = self.max_spend is not None and self.spent > self.max_spend
        if over:
            self._abort(f"estimated spend ${self.spent:.4f} exceeded --max-spend ${self.max_spend:.4f}")


def _is_openrouter_host(base_url: object) -> bool:
    """True only when the row's host is openrouter.ai itself (never a look-alike), so the key stays home."""
    from urllib.parse import urlparse

    host = (urlparse(str(base_url or "")).hostname or "").casefold()
    return host == "openrouter.ai" or host.endswith(".openrouter.ai")


def openrouter_credits_fetcher(api_key: str, *, url: str = OPENROUTER_CREDITS_URL, http_get: Callable[..., Any] | None = None) -> Callable[[], dict[str, Any]]:
    """GET the account's credits with the key in the Authorization header only."""

    def fetch() -> dict[str, Any]:
        getter = http_get
        if getter is None:
            import httpx

            getter = httpx.get
        response = getter(url, headers={"Authorization": f"Bearer {api_key}"}, timeout=20)
        response.raise_for_status()
        data = (response.json() or {}).get("data") or {}
        return {"total_credits": float(data["total_credits"]), "total_usage": float(data["total_usage"])}

    return fetch


# ------------------------------------------------------------------------------------------ replay


def production_transport(client: Any) -> Transport:
    """The judge's production call path for a fixed provider row (no ledger, no retention)."""

    def call(provider: Any, record: dict[str, Any], images: list[dict[str, Any]]) -> dict[str, Any]:
        request = record.get("request") or {}
        extra: dict[str, Any] = {}
        if images:
            extra["image"] = images[0] if len(images) == 1 else images
        return client._call_model_json_unrecorded(
            purpose="grounding_judge",
            system=str(request.get("system") or ""),
            prompt=request.get("prompt") or {},
            schema=request.get("schema") or {},
            provider=provider,
            max_tokens_override=request.get("max_tokens"),
            timeout_seconds_override=request.get("timeout_seconds"),
            **extra,
        )

    return call


def _images_present(record: dict[str, Any]) -> bool:
    directory = Path(str(record.get("_dir") or ""))
    return all(
        (directory / str(ref.get("path") or "")).is_file()
        for ref in (record.get("request") or {}).get("images") or []
    )


def is_replayable(record: dict[str, Any]) -> bool:
    """A recorded verdict (or a gold label) and every referenced screenshot still on disk."""
    return (recorded_verdict(record) is not None or isinstance(record.get("_gold"), dict)) and _images_present(record)


def run_replay(
    records: list[dict[str, Any]],
    legs: list[Any],
    transport: Transport,
    *,
    repeat: int = 1,
    workers: int = 4,
    budget: BudgetGuard | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Replay every verdict-bearing record on every leg ``repeat`` times; (rows, records skipped).

    A record with no recorded verdict, or whose screenshot was pruned (artifact_retention keeps the
    JSON but drops old run images), is skipped. With a budget, credits are checked before each batch
    of budget.check_every calls, and once the budget aborts no further call starts (calls already in
    flight finish); budget.abort_reason says why."""
    usable = [record for record in records if is_replayable(record)]
    jobs = [
        (record, _as_leg(leg), index)
        for index in range(max(1, int(repeat)))
        for record in usable
        for leg in legs
    ]

    def run(job: tuple[dict[str, Any], Leg, int]) -> dict[str, Any] | None:
        if budget is not None and budget.stopped:
            return None
        row = replay_one(job[0], job[1], transport, job[2])
        if budget is not None:
            budget.charge(row_cost(row))
        return row

    batch_size = budget.check_every if budget is not None else max(1, len(jobs))
    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as executor:
        for start in range(0, len(jobs), batch_size):
            if budget is not None:
                budget.check_credits()
                if budget.stopped:
                    break
            rows.extend(row for row in executor.map(run, jobs[start:start + batch_size]) if row is not None)
            if budget is not None and budget.stopped:
                break
    return rows, len(records) - len(usable)


# ----------------------------------------------------------------------------------------- records


def load_record_file(path: Path | str) -> dict[str, Any] | None:
    from app.grounding_judge_retention import RECORD_FORMAT

    path = Path(path)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("format") != RECORD_FORMAT:
        return None
    record["_path"] = str(path)
    record["_dir"] = str(path.parent)
    return record


def load_records_from_manifest(manifest: Path | str, *, all_attempts: bool = False) -> list[dict[str, Any]]:
    """A JSON list of record paths (strings, or objects with "path"); relative paths resolve
    against the manifest's folder. A {"records": [...]} object is accepted too. An object entry with
    ``replay_usable: false`` is skipped, and so (unless all_attempts) is one with
    ``first_verdict_of_packet: false`` (a repeat attempt on a packet already in the corpus)."""
    manifest = Path(manifest)
    entries = json.loads(manifest.read_text(encoding="utf-8"))
    if isinstance(entries, dict):
        entries = entries.get("records") or entries.get("paths") or []
    records = []
    for entry in entries if isinstance(entries, list) else []:
        raw = entry.get("path") if isinstance(entry, dict) else entry
        if not raw:
            continue
        if isinstance(entry, dict) and (
            entry.get("replay_usable") is False or (not all_attempts and entry.get("first_verdict_of_packet") is False)
        ):
            continue
        path = Path(str(raw))
        if not path.is_absolute():
            path = manifest.parent / path
        record = load_record_file(path)
        if record is not None:
            records.append(record)
    return records


def load_records_from_dir(directory: Path | str) -> list[dict[str, Any]]:
    return [record for path in sorted(Path(directory).rglob("*.json")) if (record := load_record_file(path)) is not None]


_IMAGE_MEDIA_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


def _gold_must_be_false(expected: Any) -> bool | None:
    """True for a must-be-false label, False for a may-be-true one, None when the label is unknown."""
    if not isinstance(expected, dict):
        return None
    if isinstance(expected.get("grounded"), str):
        label = expected["grounded"].strip().lower()
        return True if label == "must_be_false" else False if label == "may_be_true" else None
    if isinstance(expected.get("grounded"), bool):
        return not expected["grounded"]
    if isinstance(expected.get("second_judge_may_accept"), bool):
        return not expected["second_judge_may_accept"]
    return None


def load_gold_dir(directory: Path | str, *, wording: str = "new") -> list[dict[str, Any]]:
    """Labeled grounding cases (eval_runs/grounding-viewtoggle, grounding-second-opinion) as replay records.

    The set's index.json lists each case's file and label (``grounded: must_be_false | may_be_true``,
    or ``second_judge_may_accept``). A case file holds ``request`` {system, prompt, schema, images:
    [absolute image paths]}; a file name with no folder resolves under ``<wording>/`` when it is not
    beside the index. A must-be-false case becomes a record whose verdict is "not grounded", so a
    candidate grounded=true on it is an N->G flip (UNSAFE) and counts in gold_unsafe; a may-be-true
    case carries no verdict (its reply is recorded, not scored). The production defaults for the
    judge's token and timeout budgets apply (the cases do not record them)."""
    from app.grounding_judge_retention import RECORD_FORMAT

    directory = Path(directory)
    index = json.loads((directory / "index.json").read_text(encoding="utf-8"))
    items = index if isinstance(index, list) else (index.get("cases") or [])
    records = []
    for item in items:
        if not isinstance(item, dict) or not item.get("file"):
            continue
        path = directory / str(item["file"])
        if not path.is_file():
            path = directory / wording / str(item["file"])
        try:
            case = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        request = case.get("request") if isinstance(case.get("request"), dict) else {}
        prompt = request.get("prompt") if isinstance(request.get("prompt"), dict) else {}
        must_be_false = _gold_must_be_false(case.get("expected"))
        if must_be_false is None:
            must_be_false = _gold_must_be_false(item)
        if must_be_false is None or not prompt:
            continue
        images = [
            {"path": str(Path(image)), "media_type": _IMAGE_MEDIA_TYPES.get(Path(image).suffix.lower(), "image/jpeg")}
            for image in request.get("images") or []
        ]
        note = str((case.get("expected") or {}).get("note") or item.get("note") or "")
        record: dict[str, Any] = {
            "format": RECORD_FORMAT,
            "run_id": None,
            "judge_call_id": str(case.get("id") or item.get("id") or path.stem),
            "attempt": 1,
            "created_at": "",
            "request": {
                "system": str(request.get("system") or ""),
                "prompt": prompt,
                "schema": request.get("schema") or {},
                "max_tokens": None,
                "timeout_seconds": None,
                "image_sent": bool(images),
                "images": images,
                "entry_count": len(prompt.get("entries") or []),
            },
            "provider": {},
            "outcome": (
                {"ok": True, "verdict": {"grounded": False, "entry_index": -1, "reason": f"gold must_be_false: {note}"[:300]}}
                if must_be_false
                else {"ok": False, "verdict": {}}
            ),
            "_gold": {"set": directory.name, "id": str(case.get("id") or item.get("id") or path.stem), "must_be_false": must_be_false},
            "_path": str(path),
            "_dir": "",  # image paths are absolute
        }
        records.append(record)
    return records


def stratified_sample(records: list[dict[str, Any]], grounded_count: int, *, seed: int = 0) -> list[dict[str, Any]]:
    """Every record whose recorded verdict is not grounded (and every gold case) is kept; at most
    grounded_count of the recorded-grounded records are sampled (seeded). Original order is kept."""
    import random

    grounded = [index for index, record in enumerate(records) if not record.get("_gold") and (recorded_verdict(record) or {}).get("grounded") is True]
    keep = set(random.Random(int(seed)).sample(grounded, min(max(0, int(grounded_count)), len(grounded))))
    drop = set(grounded) - keep
    return [record for index, record in enumerate(records) if index not in drop]


def filter_records(records: list[dict[str, Any]], *, run_ids: list[int] | None = None, days: int | None = None) -> list[dict[str, Any]]:
    """Deduplicated by file, filtered by run and age, oldest first."""
    wanted = {int(item) for item in run_ids} if run_ids else None
    cutoff = datetime.now(timezone.utc).timestamp() - float(days) * 86400.0 if days else None
    seen: set[str] = set()
    out = []
    for record in records:
        key = str(Path(str(record.get("_path") or "")).resolve()).lower()
        if key in seen:
            continue
        seen.add(key)
        if record.get("_gold"):  # labeled cases are not tied to a run or a date
            out.append(record)
            continue
        if wanted is not None and record.get("run_id") not in wanted:
            continue
        if cutoff is not None:
            try:
                if datetime.fromisoformat(str(record.get("created_at"))).timestamp() < cutoff:
                    continue
            except ValueError:
                continue
        out.append(record)
    out.sort(key=lambda item: str(item.get("created_at") or ""))
    return out


# ------------------------------------------------------------------------------------------ output


def write_results(rows: list[dict[str, Any]], root: Path, summary: dict[str, Any] | None = None) -> Path:
    out_dir = Path(root) / "eval_scores" / "judge_replay"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out_path = out_dir / f"{stamp}.jsonl"
    with out_path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, default=str) + "\n")
    if summary is not None:
        (out_dir / f"{stamp}.summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return out_path


def _fmt(value: Any, digits: int = 0) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def format_table(summaries: list[dict[str, Any]]) -> str:
    lines = [
        f"{'leg':62s} {'n':>3} {'val':>3} {'inv':>3} {'grd=':>4} {'ent=':>4} {'G->N':>4} {'N->G!':>5} "
        f"{'p50s':>5} {'p95s':>5} {'ptok':>6} {'ctok':>6} {'rtok':>6} {'cap':>3} {'est$':>8} {'rep$':>8}"
    ]
    for item in summaries:
        p50 = item["latency_p50_ms"] / 1000 if item.get("latency_p50_ms") is not None else None
        p95 = item["latency_p95_ms"] / 1000 if item.get("latency_p95_ms") is not None else None
        lines.append(
            f"{item['model'][:62]:62s} {item['n']:3d} {item['valid']:3d} {item['invalid']:3d} "
            f"{item['grounded_agree']:4d} {item['entry_agree']:4d} {item['flips_grounded_to_not']:4d} "
            f"{item['flips_not_to_grounded']:5d} {_fmt(p50, 1):>5} {_fmt(p95, 1):>5} "
            f"{_fmt(item.get('prompt_tokens_mean'), 0):>6} {_fmt(item.get('completion_tokens_mean'), 0):>6} "
            f"{_fmt(item.get('reasoning_tokens_mean'), 0):>6} {item.get('length_capped', 0):3d} "
            f"{_fmt(item.get('estimated_cost_usd'), 5):>8} {_fmt(item.get('reported_cost_usd'), 5):>8}"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--runs", type=int, nargs="*", default=None, help="Only these run ids (default: all runs).")
    parser.add_argument("--days", type=int, default=None, help="Only records from the last N days.")
    parser.add_argument("--records-manifest", action="append", default=[], help="JSON list of record file paths.")
    parser.add_argument("--records-dir", action="append", default=[], help="A tree holding grounding_judge_attempt_v1 records.")
    parser.add_argument("--manifest-all-attempts", action="store_true", help="Keep manifest rows marked first_verdict_of_packet=false.")
    parser.add_argument("--gold-dir", action="append", default=[], help="A labeled case set (index.json + cases); must-be-false cases count grounded=true as UNSAFE.")
    parser.add_argument("--gold-wording", default="new", help="Sub-folder for gold case files not beside index.json (default: new).")
    parser.add_argument("--sample", type=int, default=None, help="Keep every recorded-not-grounded record (and gold case); sample N recorded-grounded ones.")
    parser.add_argument("--seed", type=int, default=0, help="Seed for --sample.")
    parser.add_argument("--base-provider", type=int, default=None, help="Provider id whose key/transport to reuse (default: the configured grounding-judge provider).")
    parser.add_argument("--candidate", action="append", default=[], help="model[@effort][:json_object|:strict]")
    parser.add_argument("--include-champion", action="store_true", help="Also replay the base row unchanged (noise floor).")
    parser.add_argument("--variant", action="append", default=[], help="Packet variant (repeatable, '+' composes): baseline, reasoning_off, verdict_first, terse, stable_first.")
    parser.add_argument("--route", action="append", default=[], help="OpenRouter routing for a leg (repeatable): default | sort=throughput | order=HostA,HostB | ...")
    parser.add_argument("--repeat", type=int, default=1, help="Replay every leg N times.")
    parser.add_argument("--limit", type=int, default=None, help="At most N records (newest).")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--max-spend", type=float, default=None, help="Abort once the replay's estimated spend exceeds this many USD.")
    parser.add_argument("--credit-floor", type=float, default=DEFAULT_CREDIT_FLOOR_USD, help="Abort when OpenRouter credits left fall under this many USD.")
    parser.add_argument("--check-every", type=int, default=DEFAULT_CHECK_EVERY, help="Re-check credits every N calls.")
    args = parser.parse_args()

    from sqlalchemy import select

    from app import models
    from app.lumen_model import LumenModelClient
    from app.config import get_settings
    from app.db import SessionLocal
    from app.grounding_judge_retention import load_records

    settings = get_settings()
    records: list[dict[str, Any]] = []
    for manifest in args.records_manifest:
        records.extend(load_records_from_manifest(manifest, all_attempts=args.manifest_all_attempts))
    for directory in args.records_dir:
        records.extend(load_records_from_dir(directory))
    for directory in args.gold_dir:
        records.extend(load_gold_dir(directory, wording=args.gold_wording))
    if not (args.records_manifest or args.records_dir or args.gold_dir) or args.runs:
        records.extend(load_records(settings.artifact_root, run_ids=args.runs, days=args.days))
    records = filter_records(records, run_ids=args.runs, days=args.days)
    unreplayable = sum(1 for record in records if not is_replayable(record))
    records = [record for record in records if is_replayable(record)]
    if args.sample is not None:
        records = stratified_sample(records, args.sample, seed=args.seed)
    if args.limit:
        records = records[-int(args.limit):]
    if not records:
        raise SystemExit("no retained grounding-judge records match")
    db = SessionLocal()
    try:
        user_id = db.scalars(select(models.User.id).order_by(models.User.id)).first()
        client = LumenModelClient(db, user_id=int(user_id) if user_id else None)
        base = (
            client.provider_service.provider_by_id(args.base_provider)
            if args.base_provider
            else client.provider_service.grounding_judge_provider(user_id=client.user_id)
        )
        if base is None or not getattr(base, "live_enabled", True):
            raise SystemExit("base provider is unavailable: pass --base-provider")
        try:
            legs = build_legs(base, candidates=args.candidate, include_champion=args.include_champion, variants=args.variant, routes=args.route)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        # The key goes only to the host it belongs to: a row typed openrouter on another host gets no check.
        credits = (
            openrouter_credits_fetcher(base.api_key)
            if base.provider_type == "openrouter" and _is_openrouter_host(base.base_url)
            else None
        )
        if credits is None:
            print("note: base row is not OpenRouter; no credit check (--max-spend still applies)", flush=True)
        budget = BudgetGuard(credits, floor=args.credit_floor, max_spend=args.max_spend, check_every=args.check_every)
        print(
            f"replaying {len(records)} retained judge attempts x {len(legs)} legs x {max(1, args.repeat)} "
            f"(base row {base.id}: {base.model_name}@{base.reasoning_effort or 'default'}, {base.structured_output or 'strict'})",
            flush=True,
        )
        rows, skipped = run_replay(records, legs, production_transport(client), repeat=args.repeat, workers=args.workers, budget=budget)
    finally:
        db.close()
    summaries = summarize(rows)
    flips = unsafe_flips(rows)
    out_path = write_results(
        rows,
        settings.artifact_root,
        {
            "legs": summaries,
            "unsafe_flips": flips,
            "budget": {"spent_estimated_usd": budget.spent, "credits_remaining_usd": budget.remaining, "credit_checks": budget.credit_checks, "abort_reason": budget.abort_reason},
        },
    )
    print(f"\nskipped {unreplayable + skipped} records (no recorded verdict, or screenshot pruned)\nresults: {out_path}\n")
    print(format_table(summaries))
    print("\nG->N: recorded grounded, candidate not.  N->G!: recorded not grounded, candidate grounded (unsafe).")
    print("p50s/p95s: latency seconds; ptok/ctok/rtok: mean prompt/completion/reasoning tokens; cap: length-capped; est$/rep$: estimated/provider-reported cost.")
    for item in summaries:
        if item["gold_must_be_false"]:
            print(f"  {item['model']}: gold must-be-false {item['gold_must_be_false']}, UNSAFE grounded {item['gold_unsafe']}")
        print(f"  {item['model']}: reasoning_enabled_at_transport={item['reasoning_enabled_at_transport']} substitute={item['reasoning_mandatory_substitute'] or '-'} upstream={item['upstream_providers'] or '-'}")
    if flips:
        print("\nUNSAFE N->G cases:")
        for flip in flips:
            print(f"  [{flip['leg']}] {flip['record']}\n    recorded: {flip['recorded_reason']}\n    candidate: {flip['candidate_reason']}")
    remaining = f"${budget.remaining:.2f}" if budget.remaining is not None else "unchecked"
    print(f"\nbudget: estimated spend ${budget.spent:.4f}; credits left {remaining}; checks {budget.credit_checks}")
    if budget.abort_reason:
        print(f"ABORTED: {budget.abort_reason}")


if __name__ == "__main__":
    main()
