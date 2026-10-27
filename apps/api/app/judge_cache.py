"""Cache layout for judge calls: the static instruction block gets its own breakpoint.

A judge's `system` is a one-line role and its prompt packet starts with the task and rules, none
of which are in prompt_cache.PROMPT_CACHE_STABLE_KEYS (those serve the agent's own packet). The
agent's breakpoint after the packet's stable keys therefore never exists for a judge, and the
rules ride uncached after a system block too short to cache by itself. This module splits a judge
packet as (static instruction text, rest) so a transport can put a breakpoint after the
instructions. Pure layout: the keys are only reordered (static first), never added or dropped.
"""

from __future__ import annotations

import json
from typing import Any

# Keys that hold a judge's fixed instructions. Evidence and the entries being graded follow them.
JUDGE_STATIC_KEYS: tuple[str, ...] = ("task", "rules", "instruction", "instructions", "response_shape")

# Cacheable minimums in tokens (Anthropic): 1024 for Sonnet/Opus-class, 2048 for Haiku. A prefix
# under the minimum silently no-ops, so a breakpoint on one only adds the cache-write premium.
_MIN_TOKENS_DEFAULT = 1024
_MIN_TOKENS_HAIKU = 2048
_CHARS_PER_TOKEN = 3.5  # JSON-heavy English runs ~3.5-4 chars/token; err toward not marking.


def min_cacheable_tokens(model_name: str) -> int:
    return _MIN_TOKENS_HAIKU if "haiku" in str(model_name or "").lower() else _MIN_TOKENS_DEFAULT


def judge_wire_parts(system: str, prompt: Any, model_name: str = "") -> tuple[str, str]:
    """(static instruction text, rest of the packet), or ("", whole packet) when the static block is
    too short for the model to cache or the packet is not a dict with static keys. The system
    string's length counts: the breakpoint's prefix includes the system block before it."""
    if not isinstance(prompt, dict):
        return "", json.dumps(prompt, default=str)
    static = {key: prompt[key] for key in JUDGE_STATIC_KEYS if key in prompt}
    if not static:
        return "", json.dumps(prompt, default=str)
    rest = {key: value for key, value in prompt.items() if key not in static}
    static_text = json.dumps(static, default=str)
    approx_tokens = (len(str(system or "")) + len(static_text)) / _CHARS_PER_TOKEN
    if approx_tokens < min_cacheable_tokens(model_name):
        return "", json.dumps(prompt, default=str)
    if not rest:
        return static_text, ""
    return static_text[:-1] + ", ", json.dumps(rest, default=str)[1:]
