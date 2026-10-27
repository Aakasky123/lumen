"""User-level agent BEHAVIOR preferences — how the loop runs, never WHAT it decides.

These are knobs the user owns (a toggle in Settings), not strategy. They tune execution/coverage; the
brain still decides every value. Stored on UserProfile in a dedicated column (`agent_settings_json`)
so they are never mined as candidate facts the way `profile_json` is.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select

from app import models


def normalize_question_key(label: str) -> str:
    """A stable key for a question/field so a user's 'skip this' is recognised across forms regardless
    of punctuation/casing. Not a keyword rule — just identity for the user's own remembered choice."""
    return re.sub(r"[^a-z0-9]+", " ", str(label or "").lower()).strip()

# Supported execution capability settings. These are not strategy or agent-version switches.
DEFAULTS: dict[str, Any] = {
    # The three model seats, each a model-provider row id (None leaves the seat unset).
    # Brain: decides every step and serves chat replies and text-only calls. Setting it also makes the
    # row the active provider (update_agent_settings), so the Brain and the active row never diverge.
    "vision_provider_id": None,
    # Escalation: the smarter vision brain. After repeated failures on one part of a task the loop
    # hands that part to it, and returns to the Brain once it makes progress. Never the Brain or the
    # Checker.
    "vision_escalation_provider_id": None,
    # Checker: bounded verdicts (grounding, target binding, payment floor, secret/credential
    # classification, task completion, ...), the second opinion the secret gate asks when the brain's
    # secret-risk verdict is uncertain, and the vision backup that takes a step when the Brain's
    # provider fails. Never the Brain's model: the gate never adjudicates with the same model, and
    # without a distinct one it hands the control to the user. When unset, judges run on the provider
    # they would use without it (vision or active).
    "judge_provider_id": None,
    # When true, model calls may use only enabled database providers selected by the user. In
    # particular, a stored .env OpenAI key is not an implicit adjudication or vision fallback.
    "strict_model_provider_allowlist": False,
    # "approval" pauses before consequential final actions. "autonomous" records standing user
    # authority for tasks created while the setting is active; exact target binding and post-action
    # verification still remain mandatory runtime responsibilities.
    "final_action_mode": "approval",
    # When true and a run's final-action mode is autonomous, the user has authorized Lumen to confirm
    # read/agree attestations (for example "I have read the terms") on their behalf instead of
    # asking. It reaches the model as a standing user answer it may cite; the model still decides
    # which checkbox is such an attestation, and grounding still verifies the citation.
    "autonomous_attestation_consent": False,
    # When true and a run's final-action mode is autonomous, the user has authorized Lumen to carry
    # values between the task page and the desktop applications it opens, and among those
    # applications (a code a console shows, typed into the page that opened it or into another of the
    # task's apps). It reaches the secret classifier as part of the user's own task; passwords,
    # one-time or recovery codes, and payment data stay secret regardless.
    "autonomous_relay_consent": False,
    # When true the brain is offered credential_slot=totp: the runtime computes an RFC 6238 code from a seed the
    # user stored for the exact site, or one the user's own goal (or the goal-directed page) supplies, and types
    # it into a verified one-time-code field. The seed is a credential; the brain never sees it. Off by default.
    "totp_capability": False,
    # When true a run opens the provider connection before its first screenshot and, for providers
    # that cache only at explicit breakpoints (Anthropic, Claude on OpenRouter), writes the rules
    # block's cache with a one-token request so step 1 reads it. See run_prewarm.py.
    "run_start_prewarm": True,
    # The Chrome profile directory the runtime launches when it opens the browser body itself.
    # Set ONLY from the user's own answer to the profile-choice question (multiple profiles are
    # never chosen in code); cleared/ignored automatically when the profile no longer exists.
    "chrome_profile_directory": None,
    # Run budgets, checked between steps. None = the built-in default (extension_run._VISION_DEFAULT_MAX_RUN_SECONDS /
    # _VISION_DEFAULT_MAX_RUN_TOKENS; no cost cap), 0 = no limit, a positive number = that limit. A run that
    # hits one ends BLOCKED and says the budget ran out. Seconds exclude time spent waiting for the user.
    "max_run_seconds": None,
    "max_run_tokens": None,
    "max_run_cost_usd": None,
}

_RUN_BUDGET_KEYS = ("max_run_seconds", "max_run_tokens", "max_run_cost_usd")

# Seats an earlier version kept apart that the Checker (judge_provider_id) now fills. Stored settings without a
# Checker adopt the first of these that is set (the adjudicator first) and is not the Brain's row.
_LEGACY_CHECKER_KEYS = ("secret_adjudicator_provider_id", "grounding_judge_provider_id")
_LEGACY_SEAT_KEYS = (*_LEGACY_CHECKER_KEYS, "vision_backup_provider_id")


def _adopt_legacy_checker(stored: dict[str, Any]) -> dict[str, Any]:
    """The stored settings, with a missing Checker filled from a legacy seat (never the Brain's row)."""
    if stored.get("judge_provider_id") is not None:
        return stored
    for key in _LEGACY_CHECKER_KEYS:
        legacy = stored.get(key)
        if legacy is not None and legacy != stored.get("vision_provider_id"):
            return {**stored, "judge_provider_id": legacy}
    return stored


def _provider_model_key(db: Any, provider_id: Any) -> tuple[str, str, str] | None:
    """(type, base URL, model) of a provider row: the identity "the same model" compares. None when unknown."""
    from app.model_provider import DEFAULT_BASE_URLS

    try:
        row = db.get(models.ModelProvider, int(provider_id))
    except (TypeError, ValueError):
        return None
    model = str(getattr(row, "model_name", "") or "").strip().casefold()
    if row is None or not model:
        return None
    provider_type = str(row.provider_type or "").strip().casefold()
    base_url = str(row.base_url or DEFAULT_BASE_URLS.get(provider_type) or "").strip().rstrip("/").casefold()
    return (provider_type, base_url, model)


def _same_seat(db: Any, first: Any, second: Any) -> bool:
    """Whether two seat ids name the same row, or rows serving the same model at the same endpoint."""
    if first is None or second is None:
        return False
    if first == second:
        return True
    first_key = _provider_model_key(db, first)
    return first_key is not None and first_key == _provider_model_key(db, second)


def _profile(db: Any, user_id: int) -> models.UserProfile | None:
    return db.scalar(
        select(models.UserProfile)
        .where(models.UserProfile.user_id == user_id)
        .order_by(models.UserProfile.id.desc())
    )


def get_agent_settings(db: Any, user_id: int) -> dict[str, Any]:
    """Return the user's agent settings, merged over defaults (only known keys are surfaced)."""
    profile = _profile(db, user_id)
    stored = _adopt_legacy_checker(dict(getattr(profile, "agent_settings_json", None) or {}) if profile else {})
    merged = {key: stored.get(key, default) for key, default in DEFAULTS.items()}
    if merged.get("final_action_mode") not in {"approval", "autonomous"}:
        merged["final_action_mode"] = "approval"
    return merged


def update_agent_settings(
    db: Any,
    user_id: int,
    updates: dict[str, Any],
    *,
    activate_brain: bool = True,
) -> dict[str, Any]:
    """Apply partial updates (only known keys) and return the full, merged settings. Caller commits.

    Setting the Brain (vision_provider_id) also activates that row, so the Brain and the active provider stay one
    choice; a row that cannot be activated (disabled, no API key) raises ValueError before anything is stored.
    `activate_brain=False` is for a caller that sets the active row itself (a provider-bundle import). The seat
    invariants then hold: the Checker is never the Brain's model, and Escalation is neither of them.
    """
    profile = _profile(db, user_id)
    if profile is None:
        profile = models.UserProfile(user_id=user_id, agent_settings_json={})
        db.add(profile)
        db.flush()
    current = _adopt_legacy_checker(dict(profile.agent_settings_json or {}))
    for key in _LEGACY_SEAT_KEYS:
        current.pop(key, None)
    brain_update = updates.get("vision_provider_id")
    if activate_brain and brain_update is not None:
        row = db.get(models.ModelProvider, int(brain_update))
        if row is None or not (row.is_active and row.enabled):
            from app.model_provider import ModelProviderService

            ModelProviderService(db).activate_provider(int(brain_update))
    for key, default in DEFAULTS.items():
        if key not in updates:
            continue
        value = updates[key]
        if key == "final_action_mode":
            if value in {"approval", "autonomous"}:
                current[key] = value
            continue
        if key in _RUN_BUDGET_KEYS:
            if value is None:
                current.pop(key, None)
            elif isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                current[key] = value
            continue
        if isinstance(default, bool):
            if value is not None:
                current[key] = bool(value)
        else:
            current[key] = value  # nullable provider-id settings accept an int OR None to clear
    brain = current.get("vision_provider_id")
    # The Checker is the secret gate's second opinion on the brain, so it never serves the Brain's model.
    if _same_seat(db, current.get("judge_provider_id"), brain):
        current["judge_provider_id"] = None
    # The escalation brain decides steps, so it can be neither the Brain nor the Checker that double-checks
    # the brain's secret-risk verdicts.
    escalation = current.get("vision_escalation_provider_id")
    if _same_seat(db, escalation, brain) or _same_seat(db, escalation, current.get("judge_provider_id")):
        current["vision_escalation_provider_id"] = None
    profile.agent_settings_json = current
    db.add(profile)
    return get_agent_settings(db, user_id)
