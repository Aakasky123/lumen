"""Input-token estimation and a global, priority-ordered budget for the per-step model packet.

No tokenizer is a dependency, so every figure here is an approximation (good to roughly +-15%), meant
for a budget and for sizing reports, not for billing. The provider's reported usage stays the truth.

estimate_tokens: ASCII text at ~3.6 chars per token (JSON-heavy packets tokenize denser than prose,
    which is ~4), every non-ASCII character as one token (CJK and emoji are ~1+ tokens each).
estimate_image_tokens, per provider family:
    anthropic  w*h/750 after scaling to long edge <=1568 px and <=1.15 MP (caps near 1600 tokens).
    openai     high detail: fit in 2048x2048, shortest side to 768, then 85 + 170 * 512px-tiles.
    gemini     258 tokens when both sides <=384, else 258 per 768px tile.

fit_packet: when the whole input (system + schema + history + packet + images) is over the cap, trim the
packet from least to most important until it fits, and say what was trimmed in `context_trimmed`.
Never trimmed: goal (only length-capped), your_plan, last_reply, the focused control, the screenshot.
Deterministic: the same packet and cap always trim the same way.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

CHARS_PER_TOKEN = 3.6
GOAL_MAX_CHARS = 6000
RECENT_ACTION_MAX_CHARS = 300
# The newest lines are the runtime's feedback on the step just taken, and a refusal often gives its reason before
# the instruction (an audit found 49 feedback lines whose instruction starts past char 300; run 50 lost the upload
# route and opened Dropbox popups): those lines keep far more, older history stays short.
RECENT_ACTION_LATEST_COUNT = 3
RECENT_ACTION_LATEST_MAX_CHARS = 1400
# Never trimmed away, whatever the pressure (goal is length-capped but always present).
PROTECTED_KEYS = frozenset({"goal", "your_plan", "last_reply", "instruction", "viewport", "execution_surface"})
CONTROL_ROWS_FLOOR = 12

_NON_ASCII = re.compile(r"[^\x00-\x7f]")


def estimate_tokens(text: Any) -> int:
    value = text if isinstance(text, str) else json.dumps(text, default=str, ensure_ascii=False)
    if not value:
        return 0
    non_ascii = len(_NON_ASCII.findall(value))
    return int(math.ceil((len(value) - non_ascii) / CHARS_PER_TOKEN + non_ascii))


def estimate_image_tokens(width: int, height: int, provider_type: str = "anthropic") -> int:
    w, h = max(1, int(width)), max(1, int(height))
    family = str(provider_type or "").lower()
    if "openai" in family or "gpt" in family:
        scale = min(1.0, 2048 / max(w, h))
        w, h = w * scale, h * scale
        scale = min(1.0, 768 / min(w, h))
        w, h = w * scale, h * scale
        return 85 + 170 * math.ceil(w / 512) * math.ceil(h / 512)
    if "gemini" in family or "google" in family:
        if w <= 384 and h <= 384:
            return 258
        return 258 * math.ceil(w / 768) * math.ceil(h / 768)
    # Anthropic, and the default for OpenRouter-style routes (the largest of the three at typical sizes).
    scale = min(1.0, 1568 / max(w, h), math.sqrt(1_150_000 / (w * h)))  # long edge and ~1.15 MP caps
    return int(math.ceil((w * scale) * (h * scale) / 750))


def image_tokens_total(dims: Any, provider_type: str = "anthropic") -> int:
    total = 0
    for item in dims if isinstance(dims, (list, tuple)) else []:
        try:
            total += estimate_image_tokens(int(item[0]), int(item[1]), provider_type)
        except (TypeError, ValueError, IndexError):
            continue
    return total


def section_report(
    *, system: str, schema: Any, prompt: dict[str, Any], history: Any = None, image_tokens: int = 0
) -> dict[str, Any]:
    """Estimated tokens per section; small enough for a STEP_TIMING event."""
    packet = {key: estimate_tokens(value) for key, value in prompt.items()}
    packet_total = sum(packet.values())
    history_tokens = estimate_tokens(history) if history else 0
    system_tokens = estimate_tokens(system)
    schema_tokens = estimate_tokens(schema)
    top = dict(sorted(packet.items(), key=lambda item: -item[1])[:8])
    return {
        "total": system_tokens + schema_tokens + history_tokens + packet_total + int(image_tokens),
        "system": system_tokens,
        "schema": schema_tokens,
        "history": history_tokens,
        "images": int(image_tokens),
        "packet": packet_total,
        "packet_top": top,
    }


def cap_goal(goal: Any, limit: int = GOAL_MAX_CHARS) -> Any:
    if not isinstance(goal, str) or len(goal) <= limit:
        return goal
    return f"{goal[:limit]}\n[goal truncated: first {limit} of {len(goal)} characters shown]"


def cap_recent_actions(
    items: Any,
    limit: int = RECENT_ACTION_MAX_CHARS,
    *,
    latest_limit: int = RECENT_ACTION_LATEST_MAX_CHARS,
    latest_count: int = RECENT_ACTION_LATEST_COUNT,
) -> Any:
    if not isinstance(items, list):
        return items
    capped = []
    first_latest = len(items) - latest_count
    for position, item in enumerate(items):
        cap = latest_limit if position >= first_latest else limit
        if isinstance(item, str) and len(item) > cap:
            item = f"{item[:cap]}…[+{len(item) - cap} chars]"
        capped.append(item)
    return capped


def _cost(item: Any) -> int:
    return estimate_tokens(item) + 1


def _shrink_text_blob(value: Any, chars: int) -> Any:
    """Cut every long string inside a dict/list entry to `chars`."""
    if isinstance(value, str):
        return value if len(value) <= chars else value[:chars] + "…[cut]"
    if isinstance(value, dict):
        return {k: _shrink_text_blob(v, chars) for k, v in value.items()}
    if isinstance(value, list):
        return [_shrink_text_blob(v, chars) for v in value]
    return value


def fit_packet(
    prompt: dict[str, Any], *, cap_tokens: int, fixed_tokens: int
) -> tuple[dict[str, Any], list[str], int]:
    """Trim `prompt` so fixed_tokens + packet <= cap_tokens when possible.

    Returns (packet, trimmed notes, estimated tokens still over the cap). The packet comes back unchanged
    (same object) when it already fits. Protected keys are never removed."""
    total = fixed_tokens + estimate_tokens(prompt)
    if cap_tokens <= 0 or total <= cap_tokens:
        return prompt, [], 0
    packet = dict(prompt)
    need = total - cap_tokens
    notes: list[str] = []

    def drop_oldest(key: str, floor: int = 0) -> None:
        nonlocal need
        value = packet.get(key)
        if key in PROTECTED_KEYS or need <= 0 or not isinstance(value, list) or len(value) <= floor:
            return
        kept = list(value)
        dropped = 0
        while need > 0 and len(kept) > floor:
            need -= _cost(kept.pop(0))
            dropped += 1
        packet[key] = kept
        notes.append(f"{key}: {dropped} oldest dropped")

    def shrink_then_drop(key: str) -> None:
        nonlocal need
        if key in PROTECTED_KEYS or need <= 0 or not packet.get(key):
            return
        if isinstance(packet[key], list):
            drop_oldest(key, floor=1)
        if need > 0 and packet.get(key):
            shrunk = _shrink_text_blob(packet[key], 1500)
            saved = _cost(packet[key]) - _cost(shrunk)
            if saved > 0:
                packet[key], need = shrunk, need - saved
                notes.append(f"{key}: long text cut")
        if need > 0 and packet.get(key):
            need -= _cost(packet.pop(key))
            notes.append(f"{key}: dropped")

    for key in ("web_results", "downloaded_file_text", "print_view_text"):
        shrink_then_drop(key)
    drop_oldest("page_memory")
    context = packet.get("session_context")
    if need > 0 and isinstance(context, dict) and context:
        need -= _cost(context)
        packet["session_context"] = {}
        notes.append("session_context: dropped")
    drop_oldest("your_notes")
    drop_oldest("earlier_verified_receipts")
    drop_oldest("session_ledger")
    drop_oldest("recent_actions", floor=2)
    rows = packet.get("control_inventory")
    if need > 0 and isinstance(rows, list) and len(rows) > CONTROL_ROWS_FLOOR:
        focus = {i for i, row in enumerate(rows) if isinstance(row, dict) and row.get("focused")}
        anchor = min(focus) if focus else 0
        farthest_first = sorted((i for i in range(len(rows)) if i not in focus), key=lambda i: (-abs(i - anchor), -i))
        removed: set[int] = set()
        for i in farthest_first:
            if need <= 0 or len(rows) - len(removed) <= CONTROL_ROWS_FLOOR:
                break
            need -= _cost(rows[i])
            removed.add(i)
        if removed:
            packet["control_inventory"] = [row for i, row in enumerate(rows) if i not in removed]
            notes.append(f"control_inventory: {len(removed)} rows farthest from the focused control dropped")
    if notes:
        packet["context_trimmed"] = notes
        need += estimate_tokens(notes) + 8
    return packet, notes, max(0, need)
