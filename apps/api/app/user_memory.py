"""Lumen's user memory: one plain-markdown file of what Lumen knows about the user.

This is the single store of user facts. The user sees and edits it as their profile
(About you), the brain reads it as candidate "profile" evidence, and the fixed profile
columns are a mirror of it (``mirror_to_profile``). The contract is deliberately small:

* One file per user: ``{data_root}/memory/user-{id}.md`` - human-readable, human-editable.
* One fact per bullet line: ``- key: value`` with provenance in a trailing HTML comment
  the user never sees when the markdown renders.
* Facts sit under ``## Section`` headings (SECTION_TITLES); the model picks the section
  when it learns a fact, and the user can move it.
* Writes are upserts by key and atomic (temp file + replace). Deletes are line removals.
  Lines that are not facts (prose the user typed) are kept.
* The model decides WHAT is worth remembering and how to key it; this module only stores
  and returns raw facts. No scores, no decay.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.config import DATA_ROOT

_LOCK = threading.Lock()
_HEADER = "# What Lumen knows about you\n"
_SECTION_LINE = re.compile(r"^##\s+(?P<title>.+?)\s*$")

# Section key -> heading, in file order.
SECTION_TITLES: dict[str, str] = {
    "about": "About you",
    "contact": "Contact",
    "work": "Work",
    "education": "Education",
    "preferences": "Preferences",
    "sensitive": "Sensitive",
    "other": "Other",
}
SECTIONS = tuple(SECTION_TITLES)
_SECTION_BY_TITLE = {title.casefold(): key for key, title in SECTION_TITLES.items()}

# Standard keys the models use for the facts the old profile columns held. The mirror copies
# exactly these into user_profiles / users so code that reads the columns sees the same facts.
PROFILE_COLUMN_KEYS = {
    "phone": "phone",
    "location": "location",
    "linkedin": "linkedin",
    "github": "github",
    "portfolio": "portfolio",
    "work_authorization": "work_authorization",
    "sponsorship_status": "sponsorship_status",
    "salary_expectation": "salary_expectation",
    "relocation_preference": "relocation_preference",
}
STANDARD_KEY_SECTIONS = {
    "name": "about",
    "preferred_name": "about",
    "pronouns": "about",
    "email": "contact",
    "phone": "contact",
    "location": "contact",
    "linkedin": "contact",
    "github": "contact",
    "portfolio": "contact",
    "work_authorization": "work",
    "sponsorship_status": "work",
    "salary_expectation": "preferences",
    "relocation_preference": "preferences",
}
# Guidance shared by every model that writes memory, so the same fact always lands on the same key.
KEY_GUIDE = (
    "Keys are short stable snake_case. Use these standard keys when the fact is one of them: "
    + ", ".join(STANDARD_KEY_SECTIONS)
    + ". Sections: about (identity), contact, work (roles, employers, experience, authorization), "
    "education, preferences (job search, salary, location, how they like things done), sensitive "
    "(demographics, disability, veteran status, health), other."
)
_FACT_LINE = re.compile(
    r"^- (?P<key>[a-z0-9_]{1,80}): (?P<value>.*?)(?:\s*<!--\s*(?P<note>[^>]*?)\s*-->)?\s*$"
)
_MAX_VALUE_CHARS = 500
_MAX_FACTS = 200


@dataclass(frozen=True)
class UserFact:
    key: str
    value: str
    note: str = ""
    section: str = "other"


_SSN = re.compile(r"\b\d{3}[- ]?\d{2}[- ]?\d{4}\b")
_LONG_NUMBER = re.compile(r"\d[\d -]{10,}\d")


def looks_like_secret_number(value: str) -> bool:
    """A value shaped like an SSN or a card/account number. Learned facts never store these;
    secrets belong in the credential vault, and the user can still type anything themselves."""
    text = str(value or "")
    if _SSN.search(text):
        # A formatted US phone number (3-3-4) is not an SSN (3-2-4).
        return True
    for match in _LONG_NUMBER.finditer(text):
        if sum(ch.isdigit() for ch in match.group(0)) >= 12:
            return True
    return False


def normalize_section(raw: str | None, key: str = "") -> str:
    """A known section key; a standard key's own section when none is given."""
    value = str(raw or "").strip().lower()
    if value in SECTION_TITLES:
        return value
    if value.casefold() in _SECTION_BY_TITLE:
        return _SECTION_BY_TITLE[value.casefold()]
    return STANDARD_KEY_SECTIONS.get(key, "other")


def normalize_memory_key(raw: str) -> str:
    """The one key normalization rule, shared with the chat contract's profile_key."""
    return re.sub(r"[^a-z0-9_]+", "_", str(raw or "").strip().lower()).strip("_")[:80]


def _memory_dir() -> Path:
    # DATA_ROOT honors LUMEN_DATA_ROOT (packaged app, tests) and defaults to the
    # repo's storage/ in dev - the memory file lives beside the DB it describes.
    return DATA_ROOT / "memory"


def memory_path(user_id: int) -> Path:
    return _memory_dir() / f"user-{int(user_id)}.md"


def markdown(user_id: int) -> str:
    """The user's memory file verbatim; empty string when nothing is remembered yet."""
    try:
        return memory_path(user_id).read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError:
        return ""


def facts(user_id: int) -> list[UserFact]:
    """Parsed facts in file order. Non-fact lines (headers, prose) are ignored."""
    parsed: list[UserFact] = []
    section = "other"
    for line in markdown(user_id).splitlines():
        heading = _SECTION_LINE.match(line)
        if heading:
            section = _SECTION_BY_TITLE.get(heading.group("title").casefold(), "other")
            continue
        match = _FACT_LINE.match(line)
        if not match:
            continue
        value = match.group("value").strip()
        if not value:
            continue
        parsed.append(
            UserFact(
                key=match.group("key"),
                value=value,
                note=(match.group("note") or "").strip(),
                section=section,
            )
        )
    return parsed


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _section_of_line(lines: list[str], index: int) -> str:
    for current in reversed(lines[:index]):
        heading = _SECTION_LINE.match(current)
        if heading:
            return _SECTION_BY_TITLE.get(heading.group("title").casefold(), "other")
    return "other"


def _insert_in_section(lines: list[str], section: str, line: str) -> None:
    """Append a fact line at the end of its section, creating the heading in order if needed."""
    title = SECTION_TITLES[section]
    heading_index = next(
        (index for index, current in enumerate(lines) if (m := _SECTION_LINE.match(current)) and m.group("title").casefold() == title.casefold()),
        None,
    )
    if heading_index is None:
        order = SECTIONS.index(section)
        # Before the first heading that belongs later in the order, else at the end.
        insert_at = len(lines)
        for index, current in enumerate(lines):
            heading = _SECTION_LINE.match(current)
            if heading and SECTIONS.index(_SECTION_BY_TITLE.get(heading.group("title").casefold(), "other")) > order:
                insert_at = index
                break
        block = [f"## {title}", line, ""]
        if insert_at > 0 and lines[insert_at - 1].strip():
            block.insert(0, "")
        lines[insert_at:insert_at] = block
        return
    end = heading_index + 1
    while end < len(lines) and not _SECTION_LINE.match(lines[end]):
        end += 1
    # Place after the section's last non-blank line.
    insert_at = end
    while insert_at > heading_index + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    lines.insert(insert_at, line)


def remember(
    user_id: int,
    key: str,
    value: str,
    *,
    section: str | None = None,
    source_run_id: int | None = None,
    source_note: str = "",
) -> UserFact | None:
    """Upsert one durable fact. Returns the stored fact, or None when key/value is unusable.

    The value is stored exactly as given (single line, length-capped); the note records
    when and where the fact was learned. An existing key is replaced in place (or moved
    when a different section is given) so the file never holds contradictory duplicates.
    """
    normalized_key = normalize_memory_key(key)
    clean_value = " ".join(str(value or "").replace("<!--", " ").replace("-->", " ").split()).strip()[:_MAX_VALUE_CHARS]
    if not normalized_key or not clean_value:
        return None
    learned = datetime.now(timezone.utc).date().isoformat()
    note = f"{learned}" + (f", run {int(source_run_id)}" if source_run_id else "")
    clean_source = " ".join(str(source_note or "").split()).replace("-->", "")[:80]
    if clean_source:
        note = f"{note}, {clean_source}"
    line = f"- {normalized_key}: {clean_value} <!-- {note} -->"

    with _LOCK:
        existing = markdown(user_id)
        lines = existing.splitlines() if existing.strip() else [_HEADER.rstrip("\n"), ""]
        current_index = next(
            (index for index, current in enumerate(lines) if (m := _FACT_LINE.match(current)) and m.group("key") == normalized_key),
            None,
        )
        if current_index is not None:
            current_section = _section_of_line(lines, current_index)
            final_section = normalize_section(section, normalized_key) if section else current_section
            if final_section == current_section:
                lines[current_index] = line
            else:
                del lines[current_index]
                _insert_in_section(lines, final_section, line)
        else:
            if sum(1 for current in lines if _FACT_LINE.match(current)) >= _MAX_FACTS:
                return None
            final_section = normalize_section(section, normalized_key)
            _insert_in_section(lines, final_section, line)
        _drop_empty_sections(lines)
        _write_atomic(memory_path(user_id), "\n".join(lines).rstrip("\n") + "\n")
    return UserFact(key=normalized_key, value=clean_value, note=note, section=final_section)


def _drop_empty_sections(lines: list[str]) -> None:
    """Remove a section heading with no facts or prose under it."""
    index = 0
    while index < len(lines):
        if _SECTION_LINE.match(lines[index]):
            end = index + 1
            while end < len(lines) and not _SECTION_LINE.match(lines[end]):
                end += 1
            if not any(current.strip() for current in lines[index + 1 : end]):
                del lines[index:end]
                continue
        index += 1
    while len(lines) > 1 and not lines[-1].strip() and not lines[-2].strip():
        lines.pop()


def remember_all_reconciled(
    db,
    user_id: int,
    candidates: list[dict[str, str]],
    *,
    source_run_id: int | None = None,
    source_note: str = "",
    implicit: bool = False,
    overwrite_conflicts: bool = False,
) -> list[dict[str, str]]:
    """Store candidate facts with model-judged deduplication against existing memory.

    The same underlying fact asked different ways ("do you need sponsorship" vs "work
    authorization") must live as ONE bullet. Semantic equivalence is a model judgment,
    so when memory is non-empty the model decides per candidate: skip (already stored),
    update (same fact - reuse the existing key), or add. Deterministic code enforces the
    guarantee shape and degrades to exact-key upsert when no model is available (a
    possible duplicate beats losing the fact).

    Returns one record per candidate: {key, value, action} with action in
    add|update|skip|conflict|unstorable|refused.

    Guarantees enforced here, whatever the model decided: a value shaped like an SSN or a
    card/account number is refused; ``implicit`` learning (the user did not ask) never stores
    a Sensitive-section fact; ``overwrite_conflicts`` (the user explicitly asked to keep this
    value) turns a conflict into an update.
    """
    cleaned = [
        {
            "key": normalize_memory_key(item.get("key") or ""),
            "value": " ".join(str(item.get("value") or "").split()).strip(),
            "section": str(item.get("section") or ""),
        }
        for item in candidates
        if isinstance(item, dict)
    ]
    cleaned = [item for item in cleaned if item["key"] and item["value"]]
    refused = [
        {"key": item["key"], "value": "", "action": "refused"}
        for item in cleaned
        if looks_like_secret_number(item["value"])
        or (implicit and normalize_section(item["section"] or None, item["key"]) == "sensitive")
    ]
    refused_keys = {item["key"] for item in refused}
    cleaned = [item for item in cleaned if item["key"] not in refused_keys]
    if not cleaned:
        return refused
    existing = facts(user_id)
    decisions: list[dict[str, str]] | None = None
    if existing:
        try:
            from app.lumen_model import LumenModelClient

            reconciled = LumenModelClient(db).reconcile_memory_facts(
                existing=[{"key": fact.key, "value": fact.value} for fact in existing],
                candidates=cleaned,
            )
            if reconciled.get("ok"):
                decisions = reconciled.get("decisions") or []
        except Exception:  # noqa: BLE001 - degrade to exact-key upsert, never lose the fact
            decisions = None
    try:
        return refused + _apply_decisions(
            user_id,
            cleaned,
            existing,
            decisions,
            source_run_id=source_run_id,
            source_note=source_note,
            overwrite_conflicts=overwrite_conflicts,
        )
    finally:
        mirror_to_profile(db, user_id)


def _apply_decisions(user_id, cleaned, existing, decisions, *, source_run_id, source_note, overwrite_conflicts) -> list[dict[str, str]]:
    applied: list[dict[str, str]] = []
    if decisions is None:
        # Empty memory or no model: exact-key upsert is the degraded but honest path.
        for item in cleaned:
            stored = remember(user_id, item["key"], item["value"], section=item["section"] or None, source_run_id=source_run_id, source_note=source_note)
            applied.append(
                {"key": stored.key if stored else item["key"], "value": item["value"], "action": "add" if stored else "unstorable"}
            )
        return applied
    existing_keys = {fact.key for fact in existing}
    existing_values = {fact.key: fact.value for fact in existing}
    by_index = {int(decision["candidate_index"]): decision for decision in decisions}
    for index, item in enumerate(cleaned):
        decision = by_index.get(index)
        if decision is None:
            # Model silence is NOT a skip: a candidate the model never ruled on falls
            # back to exact-key upsert, the same degraded path as no model at all.
            stored = remember(user_id, item["key"], item["value"], section=item["section"] or None, source_run_id=source_run_id, source_note=source_note)
            applied.append(
                {"key": stored.key if stored else item["key"], "value": item["value"], "action": "add" if stored else "unstorable"}
            )
            continue
        if decision.get("action") == "skip":
            matched_key = str(decision.get("key") or item["key"])
            applied.append({"key": normalize_memory_key(matched_key) or item["key"], "value": item["value"], "action": "skip"})
            continue
        if decision.get("action") == "conflict":
            conflict_key = normalize_memory_key(str(decision.get("key") or ""))
            if conflict_key in existing_keys and overwrite_conflicts:
                decision = {**decision, "action": "update", "key": conflict_key, "value": item["value"]}
            elif conflict_key in existing_keys:
                # Nothing is written on a conflict: contradicting values are the USER's
                # call, so the record carries both sides for the caller to ask about.
                applied.append(
                    {
                        "key": conflict_key,
                        "value": str(decision.get("value") or item["value"]),
                        "existing_value": existing_values.get(conflict_key, ""),
                        "action": "conflict",
                    }
                )
                continue
            else:
                decision = {**decision, "action": "add", "key": item["key"]}
        # The write-boundary guarantee, independent of who produced the decision: an
        # update must land on an existing bullet; anything else is an add under the
        # candidate's own key, so a third key spelling can never appear.
        action = str(decision.get("action") or "add")
        target_key = normalize_memory_key(str(decision.get("key") or ""))
        if action == "update" and target_key not in existing_keys:
            action, target_key = "add", item["key"]
        if action == "update" and target_key not in {fact.key for fact in facts(user_id)}:
            # The user deleted or renamed this fact while the model was deciding: don't bring it back.
            applied.append({"key": target_key, "value": item["value"], "action": "skip"})
            continue
        if not target_key:
            target_key = item["key"]
        decision = {**decision, "action": action, "key": target_key}
        stored = remember(
            user_id,
            target_key,
            str(decision.get("value") or item["value"]),
            section=(item["section"] or None) if action == "add" else None,
            source_run_id=source_run_id,
            source_note=source_note,
        )
        applied.append(
            {
                "key": stored.key if stored else item["key"],
                "value": str(decision.get("value") or item["value"]),
                "action": str(decision.get("action")) if stored else "unstorable",
            }
        )
    return applied


def mirror_to_profile(db, user_id: int, *, cleared: tuple[str, ...] | list[str] = ()) -> None:
    """Copy the standard facts into the profile columns, so code that reads them sees the profile.

    A column is overwritten from its fact and cleared only when the caller removed that fact
    (``cleared``); a column with no fact is left alone. The account's own name and email in the
    users table are never touched. Caller commits.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from app import models

    if not isinstance(db, Session):
        return

    by_key = {fact.key: fact.value for fact in facts(user_id)}
    profile = db.scalars(select(models.UserProfile).where(models.UserProfile.user_id == user_id)).first()
    if profile is None:
        profile = models.UserProfile(user_id=user_id, profile_json={})
        db.add(profile)
    for key, column in PROFILE_COLUMN_KEYS.items():
        if by_key.get(key):
            setattr(profile, column, by_key[key])
        elif key in cleared:
            setattr(profile, column, None)
    db.flush()


def forget(user_id: int, key: str) -> bool:
    """Remove one fact by key. Returns True when a line was removed."""
    normalized_key = normalize_memory_key(key)
    if not normalized_key:
        return False
    with _LOCK:
        existing = markdown(user_id)
        if not existing:
            return False
        kept = []
        removed = False
        for current in existing.splitlines():
            match = _FACT_LINE.match(current)
            if match and match.group("key") == normalized_key:
                removed = True
                continue
            kept.append(current)
        if removed:
            _drop_empty_sections(kept)
            _write_atomic(memory_path(user_id), "\n".join(kept).rstrip("\n") + "\n")
        return removed
