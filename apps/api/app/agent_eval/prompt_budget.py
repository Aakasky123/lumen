"""Measure the brain prompt packet: characters per section and how much of it two consecutive steps share.

Reads retained brain-seat requests (storage/external_brain/<seat>/run-*/step-*.request.json, read-only),
rebuilds each packet the way the current code would shape it (brain-facing receipt lines, current
cache key order) and compares it with the packet as it was retained (receipts as {type, outcome,
evidence-JSON}, the earlier key order). Run from apps/api:

    python -m app.agent_eval.prompt_budget [--root PATH] [--limit N]

A prompt cache is a byte-prefix match, so the number that matters is the common prefix of step N and
step N+1 as a share of step N+1's packet. The retained receipts carry only their evidence string
(cut at 900 characters), so the rebuilt receipts are recovered from it and the "after" receipt
size is an approximation: the fields past the old cut are absent from both sides.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import statistics
import sys
from pathlib import Path
from typing import Any, Callable

from app.prompt_cache import PROMPT_CACHE_STABLE_KEYS, PROMPT_CACHE_VOLATILE_KEYS, order_prompt_for_cache
from app.receipt_view import brain_receipts
from app.vision_agent import _dedupe_inventory_rows, slim_inventory_rows

DEFAULT_ROOT = Path(__file__).resolve().parents[4] / "storage" / "external_brain"

# The key order before the receipt/cache-order change (prompt_cache at bdf5296).
BASELINE_STABLE_KEYS: tuple[str, ...] = (
    "goal", "execution_surface", "surfaces", "task_context", "candidate_data_scopes", "operator_instructions",
    "user_profile", "user_profile_provenance", "resume_facts", "saved_answers", "available_documents",
    "human_answers", "already_asked", "credential_vault", "retrieved_candidate_facts",
    "latest_candidate_query_result", "desktop_tools", "page_memory", "control_memory", "last_page_view",
    "your_notes", "your_plan",
)
BASELINE_VOLATILE_KEYS: tuple[str, ...] = (
    "satisfied_controls", "native_dialog", "browser_tabs", "verified_receipts", "session_ledger",
    "recovery_constraints", "url_query_parameters", "viewport", "zoom_view", "session_context",
    "front_window_text", "page_evidence", "control_inventory", "recent_actions", "local_time",
)


def order_by(stable: tuple[str, ...], volatile: tuple[str, ...]) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def order(prompt: dict[str, Any]) -> dict[str, Any]:
        named = set(stable) | set(volatile)
        return {
            **{k: prompt[k] for k in stable if k in prompt},
            **{k: v for k, v in prompt.items() if k not in named},
            **{k: prompt[k] for k in volatile if k in prompt},
        }

    return order


def _parse_evidence(evidence: str) -> dict[str, Any]:
    try:
        data = json.loads(evidence)
        if isinstance(data, dict):
            return data
    except ValueError:
        pass
    # Cut at 900 characters: recover the flat "key": value pairs that survived.
    pairs: dict[str, Any] = {}
    for match in re.finditer(r'"([a-z_0-9]+)": ("(?:[^"\\]|\\.)*"|true|false|null|-?[0-9.]+)', evidence):
        try:
            pairs[match.group(1)] = json.loads(match.group(2))
        except ValueError:
            pass
    return pairs


def raw_receipts_from_retained(receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stored-receipt shape ({type, outcome, verification, target}) from a retained {type, outcome, evidence}."""
    raw = []
    for item in receipts or []:
        if not isinstance(item, dict):
            continue
        if "evidence" not in item:
            raw.append(item)
            continue
        data = _parse_evidence(str(item.get("evidence") or ""))
        verification = data.get("verification") if isinstance(data.get("verification"), dict) else {}
        target = data.get("target") if isinstance(data.get("target"), dict) else {}
        if not verification:
            # A truncated evidence string: the flat pairs belong to the verification block, label/selector to the target.
            flat = {k: v for k, v in data.items() if k not in {"verification", "target", "label", "selector"}}
            verification = flat
            target = {k: data[k] for k in ("label", "selector") if k in data}
        raw.append({"type": item.get("type"), "outcome": item.get("outcome"), "verification": verification, "target": target})
    return raw


def rebuild(prompt: dict[str, Any], order: Callable[[dict[str, Any]], dict[str, Any]] = order_prompt_for_cache) -> dict[str, Any]:
    """The packet as the current code shapes it."""
    packet = dict(prompt)
    if isinstance(prompt.get("control_inventory"), list):
        packet["control_inventory"] = slim_inventory_rows(_dedupe_inventory_rows(copy.deepcopy(prompt["control_inventory"])))
    packet["verified_receipts"] = brain_receipts(raw_receipts_from_retained(prompt.get("verified_receipts") or []))
    if packet.get("earlier_verified_receipts"):
        packet["earlier_verified_receipts"] = brain_receipts(
            raw_receipts_from_retained(prompt["earlier_verified_receipts"]), detailed=0
        )
    return order(packet)


def reorder_only(prompt: dict[str, Any], order: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    return order(dict(prompt))


def common_prefix_length(a: str, b: str) -> int:
    limit = min(len(a), len(b))
    low, high = 0, limit
    while low < high:  # the longest equal prefix, by bisection on slices
        mid = (low + high + 1) // 2
        if a[:mid] == b[:mid]:
            low = mid
        else:
            high = mid - 1
    return low


def first_diff_key(a: dict[str, Any], b: dict[str, Any]) -> str:
    for key in list(a) + [k for k in b if k not in a]:
        if json.dumps(a.get(key), sort_keys=True, default=str) != json.dumps(b.get(key), sort_keys=True, default=str):
            return key
        if list(a).index(key) != (list(b).index(key) if key in b else -1):
            return key
    return ""


def load_runs(root: Path, limit: int | None = None) -> list[list[dict[str, Any]]]:
    runs = []
    for run_dir in sorted(root.glob("*/run-*")):
        steps = []
        for path in sorted(run_dir.glob("step-*.request.json")):
            try:
                steps.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        if steps:
            runs.append(steps)
    return runs[:limit] if limit else runs


def section_sizes(packets: list[dict[str, Any]]) -> dict[str, float]:
    sizes: dict[str, list[int]] = {}
    for packet in packets:
        for key, value in packet.items():
            sizes.setdefault(key, []).append(len(json.dumps(value, default=str)))
    return {key: statistics.median(values) for key, values in sizes.items()}


def prefix_stats(runs: list[list[dict[str, Any]]], shape: Callable[[dict[str, Any]], dict[str, Any]]) -> dict[str, Any]:
    shares, prefixes, totals, breakers = [], [], [], {}
    for steps in runs:
        previous = None
        for step in steps:
            packet = shape(step["prompt"])
            text = json.dumps(packet, default=str)
            totals.append(len(text))
            if previous is not None:
                prev_text = json.dumps(previous, default=str)
                shared = common_prefix_length(prev_text, text)
                prefixes.append(shared)
                shares.append(shared / max(1, len(text)))
                key = first_diff_key(previous, packet)
                breakers[key] = breakers.get(key, 0) + 1
            previous = packet
    return {
        "packet_chars_median": statistics.median(totals) if totals else 0,
        "common_prefix_median": statistics.median(prefixes) if prefixes else 0,
        "common_prefix_share_mean": statistics.mean(shares) if shares else 0.0,
        "first_differing_key": dict(sorted(breakers.items(), key=lambda item: -item[1])[:6]),
    }


def inventory_row_chars(runs: list[list[dict[str, Any]]]) -> dict[str, float]:
    """Mean characters per inventory row: as retained, after the shared-context dedupe the compactor
    now applies, and after the brain-facing slimming."""
    counts = {"retained": [0, 0], "deduped": [0, 0], "slim": [0, 0]}
    for steps in runs:
        for step in steps:
            rows = step["prompt"].get("control_inventory")
            if not isinstance(rows, list) or not rows:
                continue
            deduped = _dedupe_inventory_rows(copy.deepcopy(rows))
            for name, view in (("retained", rows), ("deduped", deduped), ("slim", slim_inventory_rows(deduped))):
                counts[name][0] += len(json.dumps(view, default=str))
                counts[name][1] += len(view)
    return {name: round(chars / max(1, n), 1) for name, (chars, n) in counts.items()}


def report(runs: list[list[dict[str, Any]]]) -> dict[str, Any]:
    baseline_order = order_by(BASELINE_STABLE_KEYS, BASELINE_VOLATILE_KEYS)
    before_packets = [baseline_order(step["prompt"]) for steps in runs for step in steps]
    after_packets = [rebuild(step["prompt"]) for steps in runs for step in steps]
    before_sizes, after_sizes = section_sizes(before_packets), section_sizes(after_packets)
    biggest = sorted(before_sizes, key=lambda key: -before_sizes[key])[:8]
    return {
        "steps": len(before_packets),
        "before": prefix_stats(runs, baseline_order),
        "after": prefix_stats(runs, rebuild),
        "section_chars_median": {key: [before_sizes.get(key, 0), after_sizes.get(key, 0)] for key in biggest},
        "inventory_chars_per_row": inventory_row_chars(runs),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(DEFAULT_ROOT))
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)
    runs = load_runs(Path(args.root), args.limit)
    if not runs:
        print(f"no retained requests under {args.root}", file=sys.stderr)
        return 1
    print(json.dumps(report(runs), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
