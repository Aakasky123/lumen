from __future__ import annotations

import re
from typing import Any


def normalize_answer_key(field_key: str | None, label: str | None) -> str:
    raw_key = str(field_key or "").strip()
    key = raw_key.lower()
    text = f"{key} {label or ''}".lower()
    if _looks_demographic(text):
        compact = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
        return compact[:80] or "demographic"
    if "relocation assistance" in text or ("relocat" in text and "assistance" in text):
        return "relocation_assistance_required"
    if key == "relocation" or "relocat" in text:
        return "relocation_preference"
    if key in {"location_city_candidate_location", "candidate_location"}:
        return "location_city_candidate_location"
    if "location" in text or "candidate city" in text or re.search(r"\bcity\b", text):
        return "location_city_candidate_location"
    if key in {"salary_minimum_base_usd", "salary"} or "salary" in text or "compensation" in text or "base pay" in text:
        return "salary_minimum_base_usd"
    if (
        key in {"sponsorship_required", "sponsorship", "requires_sponsorship", "requires_work_authorization"}
        or "require work authorization" in text
        or "requires work authorization" in text
        or "need work authorization" in text
        or "sponsor" in text
        or "visa" in text
    ):
        return "sponsorship_required"
    if key in {"us_work_authorized", "work_authorization", "permanent_work_authorization"} or "legally authorized" in text or "authorized to work" in text or "authorization" in text:
        return "us_work_authorized"
    if key in {"referral_source", "source"} or "how did you hear" in text or "how did you first learn" in text or "source" in text:
        return "source"
    if (
        key in {"start_date", "availability_start_date"}
        or "available to begin work" in text
        or "available to start" in text
        or "start date" in text
    ):
        return "availability_start_date"
    if key == "ai_coding_assistant_experience" or "ai coding assistant" in text or "cursor" in text or "claude code" in text or "codex" in text:
        return "ai_coding_assistant_experience"
    if key in {"full_stack_experience", "full_stack_web_application_experience"} or "full-stack" in text or "full stack" in text:
        return "full_stack_experience"
    if "work history" in text or "gap" in text:
        return "work_history_gaps"
    compact = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return compact[:80] or raw_key or "unknown_field"


def classify_field(field_key: str | None, label: str | None, options: list[str] | None = None) -> str:
    normalized_key = normalize_answer_key(field_key, label)
    text = f"{field_key or ''} {label or ''}".lower()
    options_l = [option.lower() for option in (options or [])]
    if _looks_demographic(text):
        return "demographic"
    if normalized_key == "salary_minimum_base_usd":
        return "salary"
    if normalized_key == "location_city_candidate_location":
        return "location"
    if normalized_key in {"relocation_assistance_required", "relocation_preference"}:
        return "yes_no" if _looks_yes_no_options(options_l) else "generic"
    if normalized_key == "sponsorship_required":
        return "sponsorship"
    if normalized_key == "us_work_authorized":
        return "work_authorization"
    if normalized_key == "availability_start_date":
        return "generic"
    if "acknowledge" in text or "certify" in text or "terms" in text or "legal" in text or "signature" in text:
        return "legal_acknowledgement"
    if re.search(r"\b(why|what|how|describe|tell us|tell me)\b", text):
        return "generic"
    if _looks_yes_no_options(options_l) or re.search(r"\b(do you|are you|have you|can you|will you|would you)\b", text):
        return "yes_no"
    if normalized_key == "source":
        return "source"
    return "generic"


def normalize_answer_value(*, field_key: str | None, label: str | None, value: Any, options: list[str] | None = None) -> tuple[str, str | None]:
    text = str(value or "").strip()
    field_type = classify_field(field_key, label, options)
    if not text:
        return "", "answer is empty"
    if field_type == "salary":
        salary = _extract_salary(text)
        if not salary:
            return text, "salary must be numeric USD only, for example 70000"
        return salary, None
    if field_type == "location":
        normalized_location = _normalize_location(text)
        matched = _match_option(normalized_location, options or [])
        if options and not matched:
            return normalized_location, "location answer must match one of the available options"
        return matched or normalized_location, None
    if field_type in {"yes_no", "sponsorship", "work_authorization"}:
        matched_option = _match_option(text, options or [])
        if matched_option and not _looks_yes_no_options([option.lower() for option in (options or [])]):
            return matched_option, None
        yes_no = _normalize_yes_no_value(text)
        if not yes_no:
            return text, "answer must be Yes or No"
        matched = _match_option(yes_no, options or [])
        return matched or yes_no, None
    if field_type == "source":
        matched = _match_option(text, options or [])
        if matched:
            return matched, None
        if "linkedin" in text.lower() or "linked in" in text.lower():
            return _match_option("LinkedIn", options or []) or "LinkedIn", None
        return text, None
    if normalize_answer_key(field_key, label) == "work_history_gaps" and text.lower() in {"na", "n/a", "none", "no"}:
        return "N/A", None
    experience_option = _match_experience_level_option(text, field_key, label, options or [])
    if experience_option:
        return experience_option, None
    matched = _match_option(text, options or [])
    return matched or text, None


def _extract_salary(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    k_match = re.search(r"\$?\s*(\d{2,3}(?:\.\d+)?)\s*k\b", text)
    if k_match:
        return str(int(float(k_match.group(1)) * 1000))
    money_match = re.search(r"\$?\s*(\d{2,3}(?:,\d{3})+|\d{5,7})(?:\s*(?:usd|dollars|\$))?", text)
    if money_match:
        return money_match.group(1).replace(",", "")
    return text if re.fullmatch(r"\d{5,7}", text) else ""


def _looks_demographic(text: str) -> bool:
    return bool(re.search(r"\b(hispanic|latino|ethnicity|race|gender|veteran|disability)\b", text.lower()))


def _normalize_location(value: str) -> str:
    text = re.sub(r"\s+", " ", value.strip())
    compact = re.sub(r"[^a-z]", "", text.lower())
    if compact in {"jerseycitynewjersey", "jerseycitynj"}:
        return "Jersey City, New Jersey"
    if compact in {"newyorknewyork", "newyorkny"}:
        return "New York, New York"
    if "," in text:
        return ", ".join(part.strip().title() for part in text.split(",", 1))
    parts = text.split()
    if len(parts) >= 3:
        return f"{' '.join(parts[:-2]).title()}, {' '.join(parts[-2:]).title()}"
    return text.title()


def _normalize_yes_no_value(value: str) -> str | None:
    normalized = value.strip().lower()
    if normalized in {"yes", "y", "true", "1", "authorized"} or re.match(r"^(yes|yeah|yep)\b", normalized):
        return "Yes"
    if normalized in {"no", "n", "false", "0", "none"} or re.match(r"^(no|nope|not|do not|don't|does not|without)\b", normalized):
        return "No"
    if "does not require" in normalized or "do not require" in normalized or "without sponsorship" in normalized or "no sponsorship" in normalized:
        return "No"
    if "legally authorized" in normalized or "permanent authorization" in normalized or "authorized to work" in normalized:
        return "Yes"
    return None


def _match_option(value: str, options: list[str]) -> str | None:
    if not options:
        return None
    normalized = value.strip().lower()
    yes_no_match = _match_yes_no_option(normalized, options)
    if yes_no_match:
        return yes_no_match
    for option in options:
        if option.strip().lower() == normalized:
            return option
    for option in options:
        option_l = option.strip().lower()
        if normalized in option_l or option_l in normalized:
            return option
    return None


def _match_yes_no_option(normalized: str, options: list[str]) -> str | None:
    if normalized not in {"yes", "no"}:
        return None
    preferred: list[str] = []
    for option in options:
        option_l = option.strip().lower()
        if normalized == "yes":
            if option_l == "yes" or option_l.startswith("yes,") or option_l.startswith("yes "):
                preferred.append(option)
            elif not option_l.startswith("no") and any(term in option_l for term in ("authorized", "eligible", "i am currently")):
                preferred.append(option)
        else:
            if option_l == "no" or option_l.startswith("no,") or option_l.startswith("no "):
                preferred.append(option)
            elif any(term in option_l for term in ("do not", "does not", "will not", "not require", "without sponsorship", "no sponsorship")):
                preferred.append(option)
    return preferred[0] if preferred else None


def _match_experience_level_option(value: str, field_key: str | None, label: str | None, options: list[str]) -> str | None:
    if not options:
        return None
    option_by_lower = {option.strip().lower(): option for option in options if option.strip()}
    level_options = {"none", "basic", "intermediate", "advance", "advanced", "expert"}
    if not (set(option_by_lower) & level_options):
        return None
    text = value.strip().lower()
    prompt = f"{field_key or ''} {label or ''}".lower()

    def pick(*names: str) -> str | None:
        for name in names:
            if name in option_by_lower:
                return option_by_lower[name]
        return None

    if re.search(r"\b(no|none|0|zero)\b", text):
        return pick("none")
    if "go" in prompt and not re.search(r"\b(2\+?|3\+?|4\+?|5\+?|advanced?|expert|proficient|strong)\b", text):
        return pick("basic", "intermediate", "advance", "advanced")
    if re.search(r"\b(3\+?|4\+?|5\+?)\s*(years?|yrs?)\b", text) or re.search(r"\b(advance|advanced|expert|proficient|strong)\b", text):
        return pick("advance", "advanced", "expert", "intermediate", "basic")
    if re.search(r"\b(2\+?)\s*(years?|yrs?)\b", text) or re.search(r"\b(intermediate|production|professional)\b", text):
        return pick("intermediate", "advance", "advanced", "basic")
    if re.search(r"\b(experience|worked|working|built|writing|applying|used|using|yes)\b", text):
        if any(term in prompt for term in ("ai-assisted", "ai assisted", "coding assistant", "claude code", "cursor", "copilot", "codex")):
            return pick("advance", "advanced", "intermediate", "basic")
        if any(term in prompt for term in ("unit tests", "testing best practices", "javascript", "python", "ruby", "java")):
            return pick("intermediate", "advance", "advanced", "basic")
        return pick("basic", "intermediate", "advance", "advanced")
    if re.search(r"\b(basic|familiar|some)\b", text):
        return pick("basic", "intermediate")
    return None


def _looks_yes_no_options(options_l: list[str]) -> bool:
    if not options_l:
        return False
    values = {option.strip().lower() for option in options_l}
    return bool(values & {"yes", "no"}) and len(values) <= 4
