"""Provider-agnostic prompt-cache prefix contract.

Prompt caching is a byte-prefix match on every provider that supports it
(Anthropic explicit cache_control breakpoints; OpenAI, DeepSeek, Qwen, GLM and
Gemini automatic prefix caching). Whatever is identical from the first byte of a
request is billed at the cached rate; the first differing byte ends the match.
A request is therefore laid out stable-first:

  system message   the agent rules block (byte-stable for a surface)
  user message     the prompt packet's run-stable keys (PROMPT_CACHE_STABLE_KEYS:
                   goal, profile, desktop tool manifest, notes, plan: nothing that
                   changes on a typical step or when the surface or focus moves)
                   then its per-step keys (PROMPT_CACHE_VOLATILE_KEYS: receipts,
                   page evidence, control inventory, recent actions)
                   then the per-step dynamic directives
                   then the screenshot(s)

With message history on (LUMEN_BRAIN_MESSAGE_HISTORY), earlier reply/outcome turns sit between the system
message and that user message (vision_agent.message_history_turns). They are byte-stable while in the window,
so they form the cached prefix, and the user message's stable keys then follow them rather than opening the
request. The providers' explicit breakpoints move to the last earlier turn (model_provider.py).

A system string may carry at most one SYSTEM_DYNAMIC_BOUNDARY marker separating
the stable rules from per-step directives (tool-call repair, state recovery,
last-action feedback, mutation constraints). The marker never reaches a
provider. The directives change on most steps, so they ride at the end of the
user text, after the packet, instead of in a system message that precedes it:
anywhere earlier they would end the cached prefix before the packet's stable
keys. Trajectory envelopes and transport receipts hash the raw system string
(marker included) and the prompt dict with sorted keys, so integrity
verification is independent of this wire layout.
"""

from __future__ import annotations

import json
from typing import Any

SYSTEM_DYNAMIC_BOUNDARY = "\n<<lumen:dynamic-system>>\n"


def split_system_cache_prefix(system: str) -> tuple[str, str]:
    """Split a system string into (stable prefix, dynamic suffix).

    The dynamic suffix is "" when no boundary marker is present. Bytes are
    preserved exactly on both sides so transports stay deterministic.
    """
    stable, _, dynamic = str(system or "").partition(SYSTEM_DYNAMIC_BOUNDARY)
    return stable, dynamic


def system_wire_text(system: str) -> str:
    """The system string without its marker (rules + directives as one text). Transports do not
    send this: they send the rules alone and the directives after the packet (user_wire_parts)."""
    stable, dynamic = split_system_cache_prefix(system)
    return stable + dynamic


# Prompt packet keys in cache order. Measured on 446 consecutive step pairs of retained brain
# requests, the share of steps on which each key changed: your_notes 10%, satisfied_controls 14%,
# your_plan 20%, and then (page_memory 67%, control_memory 60%, session_context 74%, session_ledger
# 68%) which are rewritten as the view scrolls or departs, so they are per-step keys, not run-stable
# ones. Only keys that hold across most steps lead the user message (their cached prefix is
# the breakpoint's end); notes and the plan grow append-only and sit last, so a new note keeps the
# match through the old ones.
PROMPT_CACHE_STABLE_KEYS: tuple[str, ...] = (
    "goal",
    "execution_surface",
    "surfaces",
    "task_context",
    "candidate_data_scopes",
    "operator_instructions",
    "user_profile",
    "user_profile_provenance",
    "resume_facts",
    "saved_answers",
    "available_documents",
    "credential_vault",
    "desktop_tools",
    "your_notes",
    "your_plan",
)

# Keys that change on most steps, slowest first: dialogs, tabs and constraints rarely move, the
# memories of departed views and the session summary grow as the run advances, the viewport and
# page state are rewritten every step, and the receipts and recent actions (new on every step) end
# the packet. The page state precedes the receipts: a step whose action left the page as it was
# (a refused click, a wait) then keeps the match through the whole inventory. Any key not named in
# either tuple lands at the start of this section (last_reply, the result of the brain's own previous
# reply, leads it: the first per-step thing the brain reads), so a new key never splits the stable section.
PROMPT_CACHE_VOLATILE_KEYS: tuple[str, ...] = (
    "last_reply",
    "page_covered",
    "native_dialog",
    "browser_tabs",
    "recovery_constraints",
    # Facts that change mid-run, though rarely (an answer arrives, a question is asked, a candidate query
    # returns, the desktop surface re-reads the task page, a control is satisfied): they sit here, ahead of
    # the per-step keys and in this relative order, so they never end the cached stable section.
    "human_answers",
    "already_asked",
    "retrieved_candidate_facts",
    "latest_candidate_query_result",
    "last_page_view",
    "satisfied_controls",
    "downloaded_file_text",
    "print_view_text",
    "form_outline",
    "url_query_parameters",
    "zoom_view",
    "page_memory",
    "control_memory",
    "session_context",
    "session_ledger",
    "viewport",
    "desktop_windows",
    "front_window_text",
    "page_evidence",
    "control_inventory",
    "verified_receipts",
    "recent_actions",
    "local_time",
)

# desktop_tools carries a large fixed manifest (applications, key format, tool targets); its window list
# changes whenever a window opens, moves, or takes focus, so the prompt sends those entries as the separate
# per-step key `desktop_windows` (split_desktop_tools) and desktop_tools stays constant within a run.
DESKTOP_TOOLS_VOLATILE_KEYS: tuple[str, ...] = ("windows", "inventory_error")


def split_desktop_tools(desktop_tools: Any) -> tuple[Any, dict[str, Any]]:
    """(the constant manifest, the per-step window entries) of a desktop_tools dict."""
    if not isinstance(desktop_tools, dict):
        return desktop_tools, {}
    manifest = {key: value for key, value in desktop_tools.items() if key not in DESKTOP_TOOLS_VOLATILE_KEYS}
    volatile = {key: desktop_tools[key] for key in DESKTOP_TOOLS_VOLATILE_KEYS if key in desktop_tools}
    return manifest, volatile


def order_desktop_tools_for_cache(desktop_tools: Any) -> Any:
    if not isinstance(desktop_tools, dict):
        return desktop_tools
    ordered = {key: value for key, value in desktop_tools.items() if key not in DESKTOP_TOOLS_VOLATILE_KEYS}
    ordered.update({key: desktop_tools[key] for key in DESKTOP_TOOLS_VOLATILE_KEYS if key in desktop_tools})
    return ordered


def order_prompt_for_cache(prompt: dict[str, Any]) -> dict[str, Any]:
    """The same keys and values, stable keys first. Pure reordering: nothing is added or dropped."""
    stable = {key: prompt[key] for key in PROMPT_CACHE_STABLE_KEYS if key in prompt}
    named = set(PROMPT_CACHE_STABLE_KEYS) | set(PROMPT_CACHE_VOLATILE_KEYS)
    unnamed = {key: value for key, value in prompt.items() if key not in named}
    volatile = {key: prompt[key] for key in PROMPT_CACHE_VOLATILE_KEYS if key in prompt}
    return {**stable, **unnamed, **volatile}


def prompt_wire_parts(prompt: Any) -> tuple[str, str]:
    """Serialize a prompt packet as (stable text, rest) whose concatenation is exactly
    json.dumps(prompt): the stable text covers the packet's leading run of
    PROMPT_CACHE_STABLE_KEYS, where a cache breakpoint can sit. The packet's own key order is
    kept (order_prompt_for_cache sets it where the prompt is built); a prompt that does not lead
    with stable keys serializes whole as the rest."""
    if not isinstance(prompt, dict):
        return "", json.dumps(prompt, default=str)
    stable_keys = set(PROMPT_CACHE_STABLE_KEYS)
    lead: dict[str, Any] = {}
    for key, value in prompt.items():
        if key not in stable_keys:
            break
        lead[key] = value
    if not lead:
        return "", json.dumps(prompt, default=str)
    rest = {key: value for key, value in prompt.items() if key not in lead}
    lead_text = json.dumps(lead, default=str)
    if not rest:
        return lead_text, ""
    # json.dumps(a + b) == dumps(a)[:-1] + ", " + dumps(b)[1:] with the default separators.
    return lead_text[:-1] + ", ", json.dumps(rest, default=str)[1:]


def user_wire_parts(system: str, prompt: Any, suffix: str = "") -> tuple[str, str]:
    """The user message text as (cacheable prefix, per-step tail): the packet's stable keys, then
    its per-step keys followed by the system string's dynamic directives and any transport
    suffix. Concatenated they are the full user text; images follow them."""
    _stable_system, dynamic = split_system_cache_prefix(system)
    stable_text, rest_text = prompt_wire_parts(prompt)
    return stable_text, rest_text + dynamic + suffix
