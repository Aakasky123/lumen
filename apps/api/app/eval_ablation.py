"""Eval-only rule ablation: switch named check groups and prompt-rule groups off to measure what they change.

Never set in production. The switch is honoured only when ALL of these hold, otherwise every call here
reports "inactive" and every guarded call site runs exactly as without this module:

- LUMEN_EVAL_ABLATION names one or more registered groups (comma-separated) and/or
  LUMEN_EVAL_SCREENSHOT_WIDTH names a screenshot width;
- LUMEN_EVAL_MARKER is exactly ``EVAL_MARKER_VALUE`` (the explicit eval marker);
- the process is not a packaged (frozen) desktop build;
- the database is a SQLite file that is neither any checkout's ``storage/lumen.db`` nor this data root's
  default: an ablation arm always runs on a copy (app.agent_eval.ablation.run starts its own API on its own port
  and database copy).

A request that fails any condition (or names an unknown group) is refused as a whole and the refusal is visible
in ``status()`` (GET /eval/ablation), so the runner can abort instead of measuring production by mistake.

"Off" for a check group means the run proceeds as if the check had returned its benign verdict; the run is
never crashed and the action is never skipped because of it. Each substitution is recorded on the run as an
EVAL_ABLATION_SUBSTITUTED event, and every run started while an ablation is active gets an EVAL_ABLATION_ACTIVE
event and an ``eval_ablation`` entry in its confidence_state, so an ablated result can never pass for production.

Safety floors are not ablatable (EXCLUDED lists them with the reason). A group whose check also serves a floor
is ablated only where the floor does not depend on it (each group's ``floor_guard`` says how).
"""

from __future__ import annotations

import copy
import logging
import os
import sys
from contextlib import nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable

_log = logging.getLogger(__name__)

ENV_GROUPS = "LUMEN_EVAL_ABLATION"
ENV_MARKER = "LUMEN_EVAL_MARKER"
ENV_SCREENSHOT_WIDTH = "LUMEN_EVAL_SCREENSHOT_WIDTH"
ENV_BRAIN_REASONING_EFFORT = "LUMEN_EVAL_BRAIN_REASONING_EFFORT"
BRAIN_REASONING_EFFORTS = ("minimal", "low", "medium", "high")
EVAL_MARKER_VALUE = "eval-ablation-not-production"
SCREENSHOT_WIDTH_RANGE = (480, 3840)

EVENT_ACTIVE = "EVAL_ABLATION_ACTIVE"
EVENT_SUBSTITUTED = "EVAL_ABLATION_SUBSTITUTED"


@dataclass(frozen=True)
class Group:
    name: str
    kind: str  # "check" | "prompt" | "prompt_schema"
    description: str
    call_sites: tuple[str, ...] = ()
    purposes: tuple[str, ...] = ()  # model-call purposes (RUN_MODEL_CALLS) the group's calls are tallied under
    benign: str = ""  # the verdict the run proceeds with while the group is off
    floor_guard: str = ""  # where the group stays ON because a safety floor depends on it
    caveat: str = ""  # what a "no measurable effect" result does NOT license
    prompt_prefixes: tuple[str, ...] = ()  # instruction lines (bullets) the group removes, by their exact start
    schema_properties: tuple[str, ...] = ()  # reply-schema keys the group removes (top level and in `then` items)
    schema_actions: tuple[str, ...] = ()  # action names the group removes from the action enums


_RECEIPT_CAVEAT = (
    "This check backs user-facing receipts (AGENTS.md: claims come only from verified receipts). In the off arm "
    "its receipts are not real verifications. A no-effect result supports making it conditional (skip it when a "
    "deterministic signal already proves the outcome), not deleting it."
)

CHECK_GROUPS: tuple[Group, ...] = (
    Group(
        name="click_consequence_judge",
        kind="check",
        description="The click-consequence preflight judge run before every click, key activation, dialog accept "
        "and drag end, and the semantics judge for a click the extension intercepted as a contextual submit.",
        call_sites=("extension_run._judge_click_consequence_unfloored", "extension_run._judge_blocked_click_semantics"),
        purposes=("blocked_click_semantics",),
        benign="classification=intermediate, confidence=1.0 (an ordinary reversible click)",
        floor_guard="Stays on in Approval mode (final-action approval floor) and whenever the deterministic payment "
        "check sees card fields on the page (payment floor); the run's monotonic consequence floor and the "
        "extension's deterministic submit interception are untouched.",
        caveat="Off, a consequential click in Full autonomy reaches the payment floor only through the extension's "
        "deterministic submit interception; read the payment-trap pages before acting on a no-effect result.",
    ),
    Group(
        name="payment_floor_judge",
        kind="check",
        description="The model call of the final-action payment floor (judge_payment_final_action).",
        call_sites=("extension_run._judge_final_action_payment",),
        purposes=("payment_floor_judge",),
        benign="payment=false, fail_closed=false",
        floor_guard="The deterministic payment floor (card-field controls on the page) and the run's sticky "
        "per-control payment verdicts stay on: payment = deterministic hit only.",
        caveat="With it off, a payment page without card fields is not stopped; judge it on the payment-trap pages.",
    ),
    Group(
        name="credential_target_classification",
        kind="check",
        description="The credential-target classifier asked for every plain `type` into a field (is this field a "
        "login/sign-up credential that must go through the vault?).",
        call_sites=("extension_run._call_credential_target_classifier",),
        purposes=("credential_target_classification",),
        benign="slot=none, usage=none, confidence=1.0 (not a credential field: an ordinary type)",
        floor_guard="Only plain `type` actions. use_credential (the vault gate before any stored secret is "
        "decrypted) and the one-time-code classifier always run; the secret classifier, adjudicator and "
        "destination judge still inspect every typed value.",
    ),
    Group(
        name="session_context_compression",
        kind="check",
        description="The background fold of older steps into the session_context summary.",
        call_sites=("extension_run._compress_session_context_sync",),
        purposes=("session_context_compression",),
        benign="no new summary (compression fails open; the prompt keeps the last adopted summary, none)",
    ),
    Group(
        name="final_action_outcome_verification",
        kind="check",
        description="The verifier that reads the page after a final (irreversible) action and after a consumed "
        "attempt's reconciliation.",
        call_sites=("extension_run._final_action_outcome_verdict",),
        purposes=("final_action_outcome_verification",),
        benign="status=succeeded, confidence=1.0, evidence says the outcome was NOT verified",
        caveat=_RECEIPT_CAVEAT,
    ),
    Group(
        name="visual_readback_judge",
        kind="check",
        description="The vision read-back of a reversible choice click (radio, option, slider) and the "
        "expected-effect judge for an ambiguous step's `expect` (same model purpose as the outcome verifier).",
        call_sites=("extension_run._reversible_click_visual_verdict", "extension_run._expected_effect_verdict"),
        purposes=("final_action_outcome_verification",),
        benign="read-back: succeeded with observed_value = the intended value; expectation: no judge (no line)",
        caveat=_RECEIPT_CAVEAT,
    ),
    Group(
        name="task_completion_verification",
        kind="check",
        description="The grader of the agent's `done` against the run's evidence (sends the run back once, and "
        "decides what the completion reply may claim).",
        call_sites=("extension_run._verify_task_completion",),
        purposes=("task_completion_verification",),
        benign="verdict=complete",
        caveat=_RECEIPT_CAVEAT,
    ),
    Group(
        name="grounding_judge",
        kind="check",
        description="The semantic grounding judge for a value whose cited quote did not match verbatim.",
        call_sites=("extension_run._call_grounding_judge",),
        purposes=("grounding_judge",),
        benign="grounded on the first cited page entry",
        floor_guard="Only when every evidence entry is page text. A value cited from the goal, the user's "
        "profile/resume/memory/saved or human answers, or a downloaded file is always judged (never fill an "
        "ungrounded fact about the user); a page-grounded value still goes through the whose-fact gate.",
    ),
    Group(
        name="brain_hedge",
        kind="runtime",
        description="The hedged brain decision (model_hedge.hedged_call): a decision unanswered after "
        "HEDGE_AFTER_SECONDS gets a parallel second request.",
        call_sites=("model_hedge.hedged_call",),
        purposes=("vision_agent_step:hedge_extra",),
        benign="one unhedged request per decision (the call an unhedged loop makes)",
    ),
)

# Prompt groups cut the instruction block along its existing bullet boundaries: every bullet is one line of the
# instruction, and a group removes the lines that start with one of its prefixes. tests/test_eval_ablation.py
# checks that each prefix matches exactly one line of a generic browser task's instruction.
PROMPT_GROUPS: tuple[Group, ...] = (
    Group(
        name="prompt_browser_tool_notes",
        kind="prompt",
        description="Rules for back/forward, navigate/reload, scroll-with-text (Find) and switch_tab.",
        prompt_prefixes=("- back / forward:", "- navigate / reload (browser surface):", "- scroll with text is the browser's Find:", "- switch_tab:"),
    ),
    Group(
        name="prompt_zoom_notes",
        kind="prompt",
        description="The zoom rule (when and how to zoom a region).",
        prompt_prefixes=("- When on-screen text is too small or blurry",),
    ),
    Group(
        name="prompt_notes_plan",
        kind="prompt",
        description="How to use your_notes, your_plan and earlier_verified_receipts.",
        prompt_prefixes=("- your_notes holds facts", "- your_plan is your checklist", "- earlier_verified_receipts, when present"),
    ),
    Group(
        name="prompt_progress_discipline",
        kind="prompt",
        description="To-advance / dropdown discipline, 'verified progress is settled', and the declined-warning loop rule.",
        prompt_prefixes=("- To advance:", "- Verified progress is SETTLED.", "- When a movement you attempt raises a confirmation warning"),
    ),
    Group(
        name="prompt_packet_glossary",
        kind="prompt",
        description="What each packet key means (last_reply, recent_actions, operator_instructions, session_ledger, "
        "session_context, satisfied_controls, page_evidence, page_memory, control_memory, control_inventory, row, "
        "emphasized_text, focused_text_lines, folder_view).",
        prompt_prefixes=(
            "- last_reply states what became",
            "- Read recent_actions carefully.",
            "- operator_instructions are direct",
            "- session_ledger is the run's",
            "- session_context is a compressed",
            "- satisfied_controls contains controls",
            "- Use page_evidence as raw extra eyes",
            "- page_memory is text this run's pages",
            "- Use control_memory as raw prior controls",
            "- Use control_inventory first",
            "- A row whose label repeats",
            "- page_evidence.emphasized_text lists",
            "- page_evidence.focused_text_lines, when present",
            "- page_evidence.folder_view, when present",
        ),
    ),
    Group(
        name="prompt_typing_and_targeting",
        kind="prompt",
        description="Control index + box-center targeting, the Typing rule, and the `question` key rule.",
        prompt_prefixes=("- When the target appears in control_inventory,", "- Typing: set action=`type`", "- For click/type/upload on a form value-setting control"),
    ),
    Group(
        name="prompt_external_failure",
        kind="prompt",
        description="External failure has priority over recovery controls (stop with external_failure).",
        prompt_prefixes=("- External failure has priority",),
    ),
    Group(
        name="prompt_grounding_rules",
        kind="prompt",
        description="How to cite (grounding_source/grounding_quote, goal/page/file citations, shortening an "
        "over-limit answer, 'citations remain required'). The runtime's citation checks are unchanged.",
        prompt_prefixes=(
            "- For type, enter only a value grounded",
            "- A value read from a file this run downloaded",
            "- Grounding citation:",
            "- When a page-enforced input limit truncates",
            "- Citations remain required.",
        ),
    ),
    Group(
        name="prompt_ask_user_rules",
        kind="prompt",
        description="When and how to use ask_user, and how to apply human_answers. The runtime's ask gates are unchanged.",
        prompt_prefixes=("- Use ask_user when a user preference", "- human_answers are explicit answers"),
    ),
    Group(
        name="prompt_control_state_notes",
        kind="prompt",
        description="painted, occluded, list_position, native_dialog, native select and color input notes.",
        prompt_prefixes=(
            "- A control's text is its DOM text.",
            "- occluded: true on a control",
            "- list_position on a control",
            "- native_dialog, when present,",
            "- A native select (inventory kind select)",
            "- A color input (inventory kind input/color)",
        ),
    ),
    Group(
        name="prompt_scroll_rules",
        kind="prompt",
        description="Wheel-at-point / region-index scrolling and the 'do not scroll just because you acted' rule.",
        prompt_prefixes=("- scroll with control=0 and x,y inside", "- Do NOT scroll just because you acted;"),
    ),
    Group(
        name="prompt_final_action_handoff",
        kind="prompt",
        description="The final-action handoff rule (completion audit before nominating the final control, "
        "approval-context fields). The runtime's final-action interception is unchanged.",
        prompt_prefixes=("- Final-action handoff:",),
    ),
    Group(
        name="then_chains",
        kind="prompt_schema",
        description="Chained follow-up actions: the `then` reply key and its rules (verified action sequences, "
        "time-critical controls, timed gaps, the closing 'return one action' line).",
        prompt_prefixes=("- Verified action sequences:", "- Time-critical controls:", "- Timed gaps:", "Return one action, with `then: []`"),
        schema_properties=("then",),
    ),
    Group(
        name="derivation",
        kind="prompt_schema",
        description="Computed values: the `derivation` reply key and its rule.",
        prompt_prefixes=("- A value the task computes from page or goal data",),
        schema_properties=("derivation",),
    ),
    Group(
        name="skipped_optional",
        kind="prompt_schema",
        description="The `skipped_optional` sidecar and its rule.",
        prompt_prefixes=("- `skipped_optional` is model-owned",),
        schema_properties=("skipped_optional",),
    ),
    Group(
        name="advanced_gestures",
        kind="prompt_schema",
        description="Press-and-hold, modifier clicks, follow (moving targets) and drag routes: their reply keys "
        "(hold_ms, modifiers, follow, path) and the moving-target, slider, drag-route, select-text and "
        "large-surface rules.",
        prompt_prefixes=("- Moving targets:", "- Sliders and range controls:", "- Drag along a route:", "- Selecting exact text:", "- Large surfaces"),
        schema_properties=("hold_ms", "modifiers", "follow", "path"),
    ),
    Group(
        name="rare_actions",
        kind="prompt_schema",
        description="Rarely used actions: watch, web_search, fetch_url, run_command and read_print_view leave the "
        "action enums, with the watch and web-tools rules.",
        prompt_prefixes=("- watch (browser surface):", "- web_search and fetch_url read the public web"),
        schema_actions=("watch", "web_search", "fetch_url", "run_command", "read_print_view"),
    ),
)

GROUPS: dict[str, Group] = {group.name: group for group in (*CHECK_GROUPS, *PROMPT_GROUPS)}

# Not ablatable: safety floors (AGENTS.md: safety rules are never traded) and prompt text that states them.
EXCLUDED: dict[str, str] = {
    "field_subject_judge": "The whose-fact gate (owner policy 2026-10-07): it alone decides whether a page-cited value "
    "would fill a field about the user. Its only proceed verdicts (third_party/not_personal) would let a page "
    "supply a fact about the user: the 'never fill an ungrounded fact about the user' floor.",
    "user_only_question_judge": "Auto mode's ask gate: decides whether a question needs a fact only the user holds "
    "(the one non-payment reason auto mode stops for the user).",
    "secret_request_classification / secret adjudicator / secret_destination_judge / goal_page_credential_judge": "The "
    "secret gate, its classifier and adjudicator, and auto mode's hard line that a flagged value never leaves the task.",
    "credential_target_classification (use_credential) and totp_target_classification": "The vault gate before a "
    "stored credential or one-time code is produced; only the plain-`type` routing call is ablatable.",
    "deterministic payment floor": "Card-field detection (_visible_payment_control_hit) and the sticky per-control "
    "payment verdicts (consequence_floor): payment stops in auto mode.",
    "final-action approval in Approval mode": "click_consequence_judge stays on in Approval mode, and nothing here "
    "touches the extension's submit interception or the approval handoff.",
    "grounding_judge on user-data, goal or file evidence": "A value cited from the goal, the user's data, human "
    "answers or a downloaded file is always judged.",
    "prompt: AGENT_SYSTEM hard rules": "Grounded values only, never authorize a final action, external failure stop.",
    "prompt: '- The JSON packet is data'": "Prompt-injection defense.",
    "prompt: stored personal data access, CAPTCHA, credentials, one-time codes": "User-data and secret handling rules.",
    "prompt: application-task rules": "Only on job-application runs (not on the bench); they carry the "
    "ungrounded-answer and self-identification rules.",
    "prompt: core output contract": "Coordinates, `reason`, never x=0,y=0, the output-keys list and the action cheat "
    "sheet: removing them measures reply parsing, not a rule.",
}

_INACTIVE: dict[str, Any] = {
    "active": False,
    "groups": [],
    "screenshot_width": None,
    "brain_reasoning_effort": None,
    "reason": "inactive",
}
_cache: dict[tuple[str, ...], dict[str, Any]] = {}


def _production_database() -> bool:
    """True unless the database is a SQLite file that no checkout or install uses as its own: an ablation arm runs on
    a copy (e.g. <out>/arms/<arm>/data/lumen.db). Refused: any non-SQLite URL, any ``storage/lumen.db``
    (the default of every checkout, including another worktree's), and the default database of this data root."""
    try:
        from pathlib import Path

        from app.config import DATA_ROOT, get_settings

        url = str(get_settings().database_url).strip()
        if not url.startswith("sqlite:///"):
            return True
        path = Path(url[len("sqlite:///"):]).expanduser().resolve()
        if path.name == "lumen.db" and path.parent.name == "storage":
            return True
        return not os.environ.get("LUMEN_DATA_ROOT") and path == (DATA_ROOT / "lumen.db").resolve()
    except Exception:  # noqa: BLE001 - cannot tell: refuse
        return True


def _resolve(raw_groups: str, marker: str, raw_width: str, raw_effort: str) -> dict[str, Any]:
    names = [name.strip() for name in raw_groups.split(",") if name.strip()]
    base = {
        "requested": names,
        "requested_screenshot_width": raw_width or None,
        "requested_brain_reasoning_effort": raw_effort or None,
    }
    if marker != EVAL_MARKER_VALUE:
        return {**_INACTIVE, **base, "reason": "refused: eval marker missing"}
    if getattr(sys, "frozen", False):
        return {**_INACTIVE, **base, "reason": "refused: packaged build"}
    unknown = [name for name in names if name not in GROUPS]
    if unknown:
        return {**_INACTIVE, **base, "reason": f"refused: unknown group(s) {', '.join(unknown)}"}
    width: int | None = None
    if raw_width:
        try:
            width = int(raw_width)
        except ValueError:
            return {**_INACTIVE, **base, "reason": "refused: screenshot width is not an integer"}
        if not SCREENSHOT_WIDTH_RANGE[0] <= width <= SCREENSHOT_WIDTH_RANGE[1]:
            return {**_INACTIVE, **base, "reason": f"refused: screenshot width outside {SCREENSHOT_WIDTH_RANGE}"}
    effort = raw_effort.lower() or None
    if effort and effort not in BRAIN_REASONING_EFFORTS:
        return {**_INACTIVE, **base, "reason": f"refused: brain reasoning effort not one of {BRAIN_REASONING_EFFORTS}"}
    if _production_database():
        return {**_INACTIVE, **base, "reason": "refused: production database"}
    return {
        **base,
        "active": True,
        "groups": sorted(set(names)),
        "screenshot_width": width,
        "brain_reasoning_effort": effort,
        "reason": "active (eval only)",
        "marker": EVAL_MARKER_VALUE,
    }


def _raw_env() -> tuple[str, str, str, str]:
    return (
        os.environ.get(ENV_GROUPS) or "",
        os.environ.get(ENV_MARKER) or "",
        (os.environ.get(ENV_SCREENSHOT_WIDTH) or "").strip(),
        (os.environ.get(ENV_BRAIN_REASONING_EFFORT) or "").strip(),
    )


def _requested() -> bool:
    return any((os.environ.get(name) or "").strip() for name in (ENV_GROUPS, ENV_SCREENSHOT_WIDTH, ENV_BRAIN_REASONING_EFFORT))


def status() -> dict[str, Any]:
    """The ablation this process runs under; {"active": False, ...} in production."""
    if not _requested():
        return dict(_INACTIVE)
    key = _raw_env()
    resolved = _cache.get(key)
    if resolved is None:
        resolved = _resolve(*key)
        _cache[key] = resolved
        if not resolved["active"]:
            _log.warning("eval ablation requested but %s", resolved["reason"])
    return dict(resolved)


def is_active() -> bool:
    return _requested() and bool(status()["active"])


def disabled(group: str) -> bool:
    """Whether this group is switched off (always False in production)."""
    if not (os.environ.get(ENV_GROUPS) or "").strip():
        return False
    return group in status()["groups"]


def screenshot_width(default: int) -> int:
    """The brain's screenshot width: the eval override when one is active, else ``default``."""
    if not (os.environ.get(ENV_SCREENSHOT_WIDTH) or "").strip():
        return default
    return int(status()["screenshot_width"] or default)


# --- brain call (reasoning effort, hedge) ----------------------------------------------------------------------

_BRAIN_WRAPPED: ContextVar[bool] = ContextVar("eval_ablation_brain_wrapped", default=False)


def brain_overrides_active() -> bool:
    """Whether the brain decision call (model_hedge.hedged_call) runs under an eval override."""
    if not ((os.environ.get(ENV_BRAIN_REASONING_EFFORT) or "").strip() or (os.environ.get(ENV_GROUPS) or "").strip()):
        return False
    if _BRAIN_WRAPPED.get():
        return False
    current = status()
    return bool(current["active"] and (current["brain_reasoning_effort"] or "brain_hedge" in current["groups"]))


def call_brain(hedged: Callable[..., dict[str, Any]], call: Callable[..., dict[str, Any]], provider: Any, **kwargs: Any) -> dict[str, Any]:
    """One brain decision under the active overrides: the reasoning effort replaces the provider row's
    (model_provider.eval_transport_overrides, this call only: judge calls never see it), and with brain_hedge
    off the call is made once, unhedged. The stored provider row is never changed."""
    from app.model_provider import eval_transport_overrides

    current = status()
    effort = current["brain_reasoning_effort"]
    token = _BRAIN_WRAPPED.set(True)
    try:
        with eval_transport_overrides(reasoning={"effort": effort}) if effort else nullcontext():
            if "brain_hedge" in current["groups"]:
                kwargs.pop("hedge_after", None)
                return call(provider, **kwargs)
            return hedged(call, provider, **kwargs)
    finally:
        _BRAIN_WRAPPED.reset(token)


# --- check groups -------------------------------------------------------------------------------------------


def _current_run() -> int | None:
    try:
        from app import model_call_ledger

        return model_call_ledger.current_run()
    except Exception:  # noqa: BLE001
        return None


def note_substitution(group: str, call_site: str, **detail: Any) -> None:
    """Record on the run that an ablated check's benign verdict stood in for a real call. Never raises."""
    run_id = _current_run()
    if run_id is None:
        return
    try:
        from app.db import SessionLocal
        from app.event_store import emit_event

        with SessionLocal() as db:
            emit_event(
                db,
                event_type=EVENT_SUBSTITUTED,
                payload={"group": group, "call_site": call_site, **detail},
                run_id=run_id,
                actor="eval_ablation",
            )
            db.commit()
    except Exception:  # noqa: BLE001 - bookkeeping never changes the run
        pass


def _click_floor_holds(user_id: Any, page_evidence: Any) -> bool:
    """True when a floor needs the real click judge: Approval mode, no run, or card fields on the page."""
    run_id = _current_run()
    if run_id is None:
        return True
    try:
        from app import extension_run

        if extension_run._final_action_mode_for_run(run_id, int(user_id or 0)) != "autonomous":
            return True
        return bool(extension_run._visible_payment_control_hit(page_evidence if isinstance(page_evidence, dict) else {})[0])
    except Exception:  # noqa: BLE001 - cannot tell: keep the real judge
        return True


def benign_verdict(group: str, call_site: str = "", **context: Any) -> dict[str, Any] | None:
    """The verdict an ablated check returns in place of its model call, or None to run the real check
    (the group is on, or a safety floor needs the real verdict here)."""
    if not disabled(group):
        return None
    tag = f"eval ablation: {group} off"
    verdict: dict[str, Any] | None
    if group == "click_consequence_judge":
        if _click_floor_holds(context.get("user_id"), context.get("page_evidence")):
            return None
        verdict = {"classification": "intermediate", "confidence": 1.0, "evidence": tag, "metadata": {"eval_ablation": group}, "fail_closed": False}
    elif group == "payment_floor_judge":
        verdict = {"payment": False, "summary": "", "reason": f"eval_ablation:{group}", "metadata": {"eval_ablation": group}, "fail_closed": False}
    elif group == "credential_target_classification":
        action = context.get("action_evidence") if isinstance(context.get("action_evidence"), dict) else {}
        if str(action.get("action") or "").strip().lower() != "type":
            return None
        verdict = {"slot": "none", "usage": "none", "confidence": 1.0, "reason": tag, "evidence": tag}
    elif group == "final_action_outcome_verification":
        verdict = {"status": "succeeded", "confidence": 1.0, "evidence": f"{tag}; the outcome was NOT verified", "metadata": {"eval_ablation": group}}
    elif group == "visual_readback_judge":
        if context.get("mode") == "expected_effect":
            # An expectation with no judge leaves the step reading exactly as it did (empty metadata = no judge).
            verdict = {"status": "uncertain", "confidence": 0.0, "evidence": "", "metadata": {}}
        else:
            verdict = {
                "status": "succeeded",
                "confidence": 1.0,
                "evidence": f"{tag}; the read-back was NOT verified",
                "observed_value": str(context.get("intended_value") or ""),
                "metadata": {"eval_ablation": group},
            }
    elif group == "task_completion_verification":
        verdict = {"verdict": "complete", "verified_summary": "", "missing": "", "missing_only_delivery": False, "metadata": {"eval_ablation": group}}
    elif group == "grounding_judge":
        entries = [entry for entry in (context.get("entries") or []) if isinstance(entry, dict)]
        if not entries or any(str(entry.get("source") or "").strip().lower() != "page" for entry in entries):
            return None
        verdict = {
            "grounded": True,
            "entry_index": 0,
            "status": "grounded",
            "reason": tag,
            "support_quote": str(entries[0].get("value") or "")[:200],
            "metadata": {"eval_ablation": group},
        }
    else:
        return None
    note_substitution(group, call_site)
    return verdict


def skip(group: str, call_site: str = "") -> bool:
    """For a check whose benign result is "nothing" (session-context compression): True when it is off."""
    if not disabled(group):
        return False
    note_substitution(group, call_site)
    return True


# --- prompt and schema groups --------------------------------------------------------------------------------


def prompt_ablation_active() -> bool:
    """Whether any prompt or prompt+schema group is off (always False in production)."""
    if not (os.environ.get(ENV_GROUPS) or "").strip():
        return False
    return any(GROUPS[name].kind in {"prompt", "prompt_schema"} for name in status()["groups"])


def strip_prompt_lines(text: str, groups: list[str] | tuple[str, ...]) -> str:
    prefixes = tuple(prefix for name in groups for prefix in GROUPS[name].prompt_prefixes)
    if not prefixes:
        return text
    return "\n".join(line for line in text.split("\n") if not line.startswith(prefixes))


def ablate_schema(schema: dict[str, Any], groups: list[str] | tuple[str, ...]) -> dict[str, Any]:
    properties = {prop for name in groups for prop in GROUPS[name].schema_properties}
    actions = {action for name in groups for action in GROUPS[name].schema_actions}
    if not properties and not actions:
        return schema
    schema = copy.deepcopy(schema)
    body = schema.get("schema") if isinstance(schema.get("schema"), dict) else schema
    nodes = [body]
    then = (body.get("properties") or {}).get("then")
    if isinstance(then, dict) and isinstance(then.get("items"), dict):
        nodes.append(then["items"])
    for node in nodes:
        props = node.get("properties") or {}
        for prop in properties:
            props.pop(prop, None)
        if isinstance(node.get("required"), list):
            node["required"] = [key for key in node["required"] if key not in properties]
        action_node = props.get("action")
        if actions and isinstance(action_node, dict) and isinstance(action_node.get("enum"), list):
            action_node["enum"] = [name for name in action_node["enum"] if name not in actions]
    return schema


def ablate_prompt(system: str, schema: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The system text and reply schema with the active prompt groups' lines and schema keys removed."""
    groups = [name for name in status()["groups"] if GROUPS[name].kind in {"prompt", "prompt_schema"}]
    return strip_prompt_lines(system, groups), ablate_schema(schema, groups)


# --- run provenance -------------------------------------------------------------------------------------------


def record_run_start(run_id: int) -> None:
    """Mark the run as an eval ablation run (event + confidence_state). Never raises."""
    current = status()
    if not current["active"]:
        return
    marker = {
        "groups": current["groups"],
        "screenshot_width": current["screenshot_width"],
        "brain_reasoning_effort": current["brain_reasoning_effort"],
        "marker": EVAL_MARKER_VALUE,
        "not_production": True,
    }
    try:
        from app import models
        from app.db import SessionLocal
        from app.event_store import emit_event

        with SessionLocal() as db:
            emit_event(db, event_type=EVENT_ACTIVE, payload=marker, run_id=run_id, actor="eval_ablation")
            run = db.get(models.OperatorRun, run_id)
            if run is not None:
                state = dict(run.confidence_state or {})
                state["eval_ablation"] = marker
                run.confidence_state = state
            db.commit()
    except Exception:  # noqa: BLE001 - the runner also checks GET /eval/ablation
        _log.exception("could not record the eval ablation marker on run %s", run_id)


# --- measurement helpers (tests and the runner's report) ------------------------------------------------------

REFERENCE_CONTEXT: dict[str, Any] = {"goal": "Complete the tasks on this page.", "execution_surface": "browser", "page_evidence": {}}


def prompt_group_token_sizes(context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Estimated tokens each group removes from a generic browser step (instruction lines and schema keys)."""
    from app import token_budget, vision_agent

    ctx = dict(context or REFERENCE_CONTEXT)
    instruction = vision_agent.build_prompt({"width": 1120, "height": 700}, ctx)["instruction"]
    schema = vision_agent.action_schema()
    lines = instruction.split("\n")
    out: dict[str, Any] = {
        "instruction_tokens": token_budget.estimate_tokens(instruction),
        "schema_tokens": token_budget.estimate_tokens(schema),
        "groups": {},
    }
    for group in PROMPT_GROUPS:
        removed = [line for line in lines if line.startswith(group.prompt_prefixes)] if group.prompt_prefixes else []
        rule_tokens = sum(token_budget.estimate_tokens(line) for line in removed)
        schema_tokens = token_budget.estimate_tokens(schema) - token_budget.estimate_tokens(ablate_schema(schema, [group.name]))
        out["groups"][group.name] = {
            "kind": group.kind,
            "lines": len(removed),
            "rule_tokens": rule_tokens,
            "schema_tokens": schema_tokens,
            "total_tokens": rule_tokens + schema_tokens,
        }
    return out


def schema_section_token_sizes() -> dict[str, int]:
    """Estimated tokens of each top-level reply-schema key (its node with description)."""
    from app import token_budget, vision_agent

    properties = vision_agent.action_schema()["schema"]["properties"]
    sizes = {name: token_budget.estimate_tokens({name: node}) for name, node in properties.items()}
    return dict(sorted(sizes.items(), key=lambda item: -item[1]))


def registry() -> dict[str, Any]:
    """The registry as data (report, GET /eval/ablation?registry=1)."""
    return {
        "groups": {
            group.name: {
                "kind": group.kind,
                "description": group.description,
                "call_sites": list(group.call_sites),
                "purposes": list(group.purposes),
                "benign": group.benign,
                "floor_guard": group.floor_guard,
                "caveat": group.caveat,
                "prompt_prefixes": list(group.prompt_prefixes),
                "schema_properties": list(group.schema_properties),
                "schema_actions": list(group.schema_actions),
            }
            for group in GROUPS.values()
        },
        "excluded": dict(EXCLUDED),
        "knobs": {
            "screenshot_width": f"{ENV_SCREENSHOT_WIDTH}: the brain screenshot's max width (production "
            "_VISION_SCREENSHOT_WIDTH = 1120); coord_scale follows the downscale",
            "brain_reasoning_effort": f"{ENV_BRAIN_REASONING_EFFORT}: one of {BRAIN_REASONING_EFFORTS}, replacing the "
            "brain row's OpenRouter reasoning effort for vision_agent_step calls only",
        },
        "env": {
            "groups": ENV_GROUPS,
            "marker": ENV_MARKER,
            "marker_value": EVAL_MARKER_VALUE,
            "screenshot_width": ENV_SCREENSHOT_WIDTH,
            "brain_reasoning_effort": ENV_BRAIN_REASONING_EFFORT,
        },
    }
