from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import closed_world_attestations, models, user_memory
from app.lumen_answer_interpreter import SKIP_ANSWER_TEXT
from app.candidate_seed import CANDIDATE_SEED_PROVENANCE_KEY, ONBOARDING_SOURCE_REF
from app.resume_selection import active_base_resume


PROVENANCE_PROFILE = "profile"
PROVENANCE_RESUME = "resume"
PROVENANCE_ANSWER_MEMORY = "answer_memory"
PROVENANCE_STORED_VARIABLE = "stored_variable"
PROVENANCE_GENERATED_ANSWER = "generated_answer"
PROVENANCE_USER_APPROVED_ANSWER = "user_approved_answer"
PROVENANCE_FIXTURE_DEMO_SEED = "fixture_demo_seed"
PROVENANCE_TEST_SEED = "test_seed"
PROVENANCE_ONBOARDING_DEFAULT = "onboarding_default"
PROVENANCE_UNKNOWN = "unknown"

# One bounded passage contract is shared by candidate-data enumeration and the
# model-owned semantic retriever. Keeping the cap here prevents either side from
# silently hiding a tail passage that the other side reports as available.
CANDIDATE_EVIDENCE_INPUT_MAX_ENTRIES = 64

VALID_CANDIDATE_PROVENANCE = {
    PROVENANCE_PROFILE,
    PROVENANCE_RESUME,
    PROVENANCE_ANSWER_MEMORY,
    PROVENANCE_STORED_VARIABLE,
    PROVENANCE_GENERATED_ANSWER,
    PROVENANCE_USER_APPROVED_ANSWER,
    PROVENANCE_FIXTURE_DEMO_SEED,
    PROVENANCE_TEST_SEED,
    PROVENANCE_ONBOARDING_DEFAULT,
    PROVENANCE_UNKNOWN,
}

REAL_CONTROLLED_LIVE_EXCLUDED_PROVENANCE = {
    PROVENANCE_FIXTURE_DEMO_SEED,
    PROVENANCE_TEST_SEED,
    PROVENANCE_ONBOARDING_DEFAULT,
}


@dataclass(frozen=True)
class CandidateFact:
    value: str
    category: str
    confidence: float
    source: str
    allowed_usage: str = "application_answer"
    provenance: str = PROVENANCE_UNKNOWN
    source_ref: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "category": self.category,
            "confidence": self.confidence,
            "source": self.source,
            "allowed_usage": self.allowed_usage,
            "provenance": _valid_provenance(self.provenance),
            "source_ref": self.source_ref or self.source,
        }


class CandidateContextEngine:
    def __init__(self, db: Session) -> None:
        self.db = db

    def compact_defaults_with_provenance(self, user_id: int, *, include_seeded_facts: bool = True) -> dict[str, Any]:
        user = self.db.get(models.User, user_id)
        profile = self._profile(user_id)
        profile_seed = _profile_seed_metadata(profile)
        user_provenance = _user_provenance(user, profile_seed)
        profile_provenance = _profile_provenance(user, profile_seed)
        profile_source_ref = _profile_source_ref(profile_seed)
        values: dict[str, Any] = {
            "identity": {"name": None, "email": None},
            "contact": {"phone": None, "linkedin": None, "github": None, "portfolio": None},
            "location": None,
            "work_authorization": None,
            "sponsorship": None,
            "salary/preferences": {"salary_expectation": None, "relocation_preference": None},
            "closed_world_attestations": [],
            "profile_memory": [],
            "resume_file_available": False,
            "sensitive_answer_rules": {
                "invent_sensitive_facts": False,
            },
        }
        provenance: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []

        def set_value(path: str, value: Any, *, source: str, category: str, fact_provenance: str, source_ref: str | None = None) -> None:
            if value in (None, "", [], {}):
                return
            record = _provenance_record(
                path=path,
                source=source,
                category=category,
                provenance=fact_provenance,
                source_ref=source_ref,
            )
            if _excluded(fact_provenance, include_seeded_facts=include_seeded_facts):
                excluded.append(record)
                return
            _assign_path(values, path, value)
            provenance.append(record)

        if user and not _is_bootstrap_account(user):
            # The bootstrap account's "Candidate" / onboarding-…@example.com are placeholders, not facts about the
            # user: shown beside the real name and email from memory, they could be typed into a form.
            set_value("identity.name", user.name, source="users.name", category="identity", fact_provenance=user_provenance, source_ref=_user_source_ref(user, profile_seed))
            set_value("identity.email", user.email, source="users.email", category="contact", fact_provenance=user_provenance, source_ref=_user_source_ref(user, profile_seed))
        if profile:
            profile_fields = [
                ("contact.phone", profile.phone, "user_profiles.phone", "contact"),
                ("contact.linkedin", profile.linkedin, "user_profiles.linkedin", "contact"),
                ("contact.github", profile.github, "user_profiles.github", "contact"),
                ("contact.portfolio", profile.portfolio, "user_profiles.portfolio", "contact"),
                ("location", profile.location, "user_profiles.location", "location"),
                ("work_authorization", profile.work_authorization, "user_profiles.work_authorization", "work_authorization"),
                ("sponsorship", profile.sponsorship_status, "user_profiles.sponsorship_status", "sponsorship"),
                ("salary/preferences.salary_expectation", profile.salary_expectation, "user_profiles.salary_expectation", "salary/preferences"),
                ("salary/preferences.relocation_preference", profile.relocation_preference, "user_profiles.relocation_preference", "salary/preferences"),
            ]
            for path, value, source, category in profile_fields:
                set_value(path, value, source=source, category=category, fact_provenance=profile_provenance, source_ref=profile_source_ref or source)
            active_attestations = closed_world_attestations.active_for_candidate_context(
                profile.profile_json or {}
            )
            for attestation in active_attestations:
                attestation_id = str(attestation.get("attestation_id") or "")
                source = (
                    "user_profiles.profile_json.closed_world_attestations."
                    f"{attestation_id}"
                )
                values["closed_world_attestations"].append(attestation)
                provenance.append(
                    _provenance_record(
                        path=(
                            "closed_world_attestations."
                            f"{len(values['closed_world_attestations']) - 1}"
                        ),
                        source=source,
                        category="closed_world_attestation",
                        provenance=PROVENANCE_USER_APPROVED_ANSWER,
                        source_ref=source,
                    )
                )
            # Closed-world records are server-stamped completeness contracts. Keep each canonical
            # value atomic and ahead of ordinary profile memory so a long common-answer history
            # cannot evict or split the trust proof.
            for memory in closed_world_attestations.profile_memory_records(
                profile.profile_json or {}
            ):
                source = str(memory.get("source") or "")
                values["profile_memory"].append(memory)
                provenance.append(
                    _provenance_record(
                        path=f"profile_memory.{len(values['profile_memory']) - 1}.value",
                        source=source,
                        category="closed_world_attestation",
                        provenance=PROVENANCE_USER_APPROVED_ANSWER,
                        source_ref=source,
                    )
                )
            # What Lumen knows about the user (the profile the user sees and edits): one record per
            # fact, cited as "profile" evidence like every other profile_memory record.
            for fact in user_memory.facts(user_id):
                source = f"user_memory.{fact.key}"
                values["profile_memory"].append({"key": fact.key, "value": fact.value, "source": source})
                provenance.append(
                    _provenance_record(
                        path=f"profile_memory.{len(values['profile_memory']) - 1}.value",
                        source=source,
                        category="profile_memory",
                        provenance=PROVENANCE_USER_APPROVED_ANSWER,
                        source_ref=source,
                    )
                )
        resume = self._latest_resume(user_id)
        resume_provenance = _resume_provenance(user, profile_seed)
        if resume:
            record = _provenance_record(
                path="resume_file_available",
                source=f"resumes.{resume.id}.base_resume_text",
                category="resume",
                provenance=resume_provenance,
                source_ref=_profile_source_ref(profile_seed) if resume_provenance in REAL_CONTROLLED_LIVE_EXCLUDED_PROVENANCE else f"resumes.{resume.id}",
            )
            if _excluded(resume_provenance, include_seeded_facts=include_seeded_facts):
                excluded.append(record)
            else:
                values["resume_file_available"] = True
                provenance.append(record)
        return {
            "values": values,
            "provenance": provenance,
            "excluded": excluded,
            "include_seeded_facts": include_seeded_facts,
            "excluded_provenance": sorted(REAL_CONTROLLED_LIVE_EXCLUDED_PROVENANCE),
        }
    def query(self, user_id: int, query: str, *, limit: int = 6, include_seeded_facts: bool = True) -> list[dict[str, Any]]:
        facts = self._facts(user_id)
        if not include_seeded_facts:
            facts = [fact for fact in facts if fact.provenance not in REAL_CONTROLLED_LIVE_EXCLUDED_PROVENANCE]
        tokens = _tokens(query)
        scored: list[tuple[float, CandidateFact]] = []
        for fact in facts:
            haystack = _tokens(f"{fact.category} {fact.value} {fact.source}")
            score = _score(tokens, haystack) * fact.confidence
            if score > 0 or not tokens:
                scored.append((score, fact))
        scored.sort(key=lambda item: (item[0], item[1].confidence), reverse=True)
        return [fact.as_dict() for _, fact in scored[: max(1, limit)]]

    def _facts(self, user_id: int) -> list[CandidateFact]:
        facts: list[CandidateFact] = []
        user = self.db.get(models.User, user_id)
        profile = self._profile(user_id)
        profile_seed = _profile_seed_metadata(profile)
        user_provenance = _user_provenance(user, profile_seed)
        profile_provenance = _profile_provenance(user, profile_seed)
        profile_source_ref = _profile_source_ref(profile_seed)
        if user:
            facts.append(CandidateFact(user.name, "identity", 0.98, "users.name", provenance=user_provenance, source_ref=_user_source_ref(user, profile_seed)))
            facts.append(CandidateFact(user.email, "contact", 0.98, "users.email", provenance=user_provenance, source_ref=_user_source_ref(user, profile_seed)))
        if profile:
            mapping = [
                ("phone", profile.phone, "contact"),
                ("location", profile.location, "location"),
                ("linkedin", profile.linkedin, "contact"),
                ("github", profile.github, "contact"),
                ("portfolio", profile.portfolio, "contact"),
                ("work_authorization", profile.work_authorization, "work_authorization"),
                ("sponsorship_status", profile.sponsorship_status, "sponsorship"),
                ("salary_expectation", profile.salary_expectation, "salary/preferences"),
                ("relocation_preference", profile.relocation_preference, "salary/preferences"),
            ]
            for source, value, category in mapping:
                if value:
                    facts.append(CandidateFact(str(value), category, 0.94, f"user_profiles.{source}", provenance=profile_provenance, source_ref=profile_source_ref or f"user_profiles.{source}"))
            for key, value in (profile.profile_json or {}).items():
                if key == CANDIDATE_SEED_PROVENANCE_KEY:
                    continue
                if value:
                    category = _category_for_key(str(key))
                    facts.append(CandidateFact(_compact_value(value), category, 0.78, f"user_profiles.profile_json.{key}", provenance=profile_provenance, source_ref=profile_source_ref or f"user_profiles.profile_json.{key}"))
        resume = self._latest_resume(user_id)
        if resume and resume.base_resume_text:
            resume_provenance = _resume_provenance(user, profile_seed)
            resume_source_ref = _profile_source_ref(profile_seed) if resume_provenance in REAL_CONTROLLED_LIVE_EXCLUDED_PROVENANCE else f"resumes.{resume.id}"
            for snippet in _split_resume_snippets(resume.base_resume_text):
                facts.append(CandidateFact(snippet, _category_for_key(snippet), 0.72, f"resumes.{resume.id}.base_resume_text", provenance=resume_provenance, source_ref=resume_source_ref))
        for answer in self.db.scalars(select(models.AnswerMemory).where(models.AnswerMemory.user_id == user_id)).all():
            if str(answer.approved_answer or "").strip() == SKIP_ANSWER_TEXT:
                continue
            category = _category_for_key(f"{answer.question_pattern} {answer.approved_answer}")
            facts.append(
                CandidateFact(
                    answer.approved_answer,
                    category,
                    float(answer.confidence or 0.6),
                    f"answer_memory.{answer.id}:{answer.question_pattern}",
                    _allowed_usage_for_category(category),
                    PROVENANCE_ANSWER_MEMORY,
                    f"answer_memory.{answer.id}",
                )
            )
        # Durable learned user facts come from the one markdown memory file the user can
        # read and edit - not from JSON blob rows scored by heuristics. Each bullet is a
        # user-provided fact, so it carries user-approved provenance.
        from app import user_memory

        for fact in user_memory.facts(user_id):
            facts.append(
                CandidateFact(
                    f"{fact.key.replace('_', ' ')}: {fact.value}",
                    "user memory",
                    0.9,
                    f"user_memory.{fact.key}",
                    "application_answer",
                    PROVENANCE_USER_APPROVED_ANSWER,
                    f"user_memory.{fact.key}",
                )
            )
        facts.append(
            CandidateFact(
                "Do not invent education dates, employment dates, demographics, salary, sponsorship, work authorization, availability, credentials, SSN, government ID, or payment facts.",
                "sensitive-answer policy",
                1.0,
                "runtime_policy",
                "safety_rule",
                PROVENANCE_UNKNOWN,
                "runtime_policy",
            )
        )
        return [fact for fact in facts if fact.value]

    def _profile(self, user_id: int) -> models.UserProfile | None:
        return self.db.scalar(select(models.UserProfile).where(models.UserProfile.user_id == user_id).order_by(models.UserProfile.id.desc()))

    def _latest_resume(self, user_id: int) -> models.Resume | None:
        return active_base_resume(self.db, user_id)


def _category_for_key(value: str) -> str:
    lowered = value.lower()
    if any(token in lowered for token in ("non-compete", "non compete", "noncompete", "restrictive covenant", "conflict of interest", "worked at", "former employer")):
        return "legal/work restrictions"
    if any(token in lowered for token in ("hispanic", "latino", "race", "ethnicity", "eeo", "demographic", "gender")):
        return "eeo/demographics"
    if any(token in lowered for token in ("veteran", "disability", "disabled")):
        return "veteran/disability disclosure"
    if any(token in lowered for token in ("school", "degree", "education", "university", "gpa")):
        return "education"
    if any(token in lowered for token in ("project", "portfolio")):
        return "project blocks"
    if any(token in lowered for token in ("skill", "python", "java", "react", "aws", "technical")):
        return "skills"
    if any(token in lowered for token in ("experience", "employment", "work", "company")):
        return "experience blocks"
    if any(token in lowered for token in ("certification", "certificate")):
        return "certifications"
    if any(token in lowered for token in ("authorization", "visa", "eligible")):
        return "work_authorization"
    if "sponsor" in lowered:
        return "sponsorship"
    return "open-ended answer snippets"


def _allowed_usage_for_category(category: str) -> str:
    if category in {"eeo/demographics", "veteran/disability disclosure"}:
        return "sensitive_preference_only_when_semantically_equivalent"
    if category == "legal/work restrictions":
        return "reuse_for_semantically_equivalent_application_questions"
    return "application_answer"


def _assign_path(values: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cursor: Any = values
    for part in parts[:-1]:
        cursor = cursor.setdefault(part, {})
    cursor[parts[-1]] = value


def _provenance_record(*, path: str, source: str, category: str, provenance: str, source_ref: str | None = None) -> dict[str, Any]:
    return {
        "path": path,
        "category": category,
        "source": source,
        "source_ref": source_ref or source,
        "provenance": _valid_provenance(provenance),
    }


def _excluded(provenance: str, *, include_seeded_facts: bool) -> bool:
    return not include_seeded_facts and _valid_provenance(provenance) in REAL_CONTROLLED_LIVE_EXCLUDED_PROVENANCE


def _valid_provenance(value: str | None) -> str:
    normalized = str(value or "").strip()
    return normalized if normalized in VALID_CANDIDATE_PROVENANCE else PROVENANCE_UNKNOWN


def _profile_seed_metadata(profile: models.UserProfile | None) -> dict[str, Any]:
    if not profile or not isinstance(profile.profile_json, dict):
        return {}
    metadata = profile.profile_json.get(CANDIDATE_SEED_PROVENANCE_KEY)
    return metadata if isinstance(metadata, dict) else {}


def _profile_seed_label(profile_seed: dict[str, Any]) -> str | None:
    label = _valid_provenance(profile_seed.get("label"))
    return label if label in REAL_CONTROLLED_LIVE_EXCLUDED_PROVENANCE else None


def _profile_source_ref(profile_seed: dict[str, Any]) -> str | None:
    source_ref = str(profile_seed.get("source_ref") or "").strip()
    return source_ref or None


def _user_source_ref(user: models.User | None, profile_seed: dict[str, Any]) -> str:
    seed_ref = _profile_source_ref(profile_seed)
    if seed_ref:
        return seed_ref
    return "users.email" if user else ONBOARDING_SOURCE_REF


def _user_provenance(user: models.User | None, profile_seed: dict[str, Any]) -> str:
    seed_label = _profile_seed_label(profile_seed)
    if seed_label:
        return seed_label
    if not user or _is_placeholder_email(user.email):
        return PROVENANCE_ONBOARDING_DEFAULT
    return PROVENANCE_PROFILE


def _profile_provenance(user: models.User | None, profile_seed: dict[str, Any]) -> str:
    seed_label = _profile_seed_label(profile_seed)
    if seed_label:
        return seed_label
    if not user or _is_placeholder_email(user.email):
        return PROVENANCE_ONBOARDING_DEFAULT
    return PROVENANCE_PROFILE


def _resume_provenance(user: models.User | None, profile_seed: dict[str, Any]) -> str:
    seed_label = _profile_seed_label(profile_seed)
    if seed_label:
        return seed_label
    if not user or _is_placeholder_email(user.email):
        return PROVENANCE_ONBOARDING_DEFAULT
    return PROVENANCE_RESUME


def _is_bootstrap_account(user: models.User) -> bool:
    """The account runtime.ensure_default_user creates before the user has told Lumen anything."""
    email = str(user.email or "").strip().lower()
    return email.startswith("onboarding-") and email.endswith("@example.com")


def _is_placeholder_email(email: str | None) -> bool:
    normalized = str(email or "").strip().lower()
    return not normalized or normalized in {"jordan@example.com", "test@example.com", "user@example.com"} or normalized.endswith("@example.com")


def _compact_value(value: Any) -> str:
    if isinstance(value, str):
        return _truncate(value, 700)
    return _truncate(json.dumps(value, default=str, ensure_ascii=True), 700)


def _split_resume_snippets(text: str) -> list[str]:
    lines = [re.sub(r"\s+", " ", line).strip(" -\t") for line in str(text).splitlines()]
    snippets = [line for line in lines if 35 <= len(line) <= 700]
    return snippets[:20]


def _tokens(value: str) -> set[str]:
    return {token for token in re.split(r"[^a-z0-9+#]+", str(value).lower()) if len(token) >= 2}


def _score(query_tokens: set[str], haystack_tokens: set[str]) -> float:
    if not query_tokens:
        return 0.1
    overlap = len(query_tokens & haystack_tokens)
    return overlap / max(len(query_tokens), 1)


def _truncate(value: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "..."
