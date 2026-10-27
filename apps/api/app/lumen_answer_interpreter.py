from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from app.answer_normalization import normalize_answer_key, normalize_answer_value

SKIP_ANSWER_TEXT = "[skip - user prefers not to fill this]"


@dataclass(frozen=True)
class InterpretedAnswer:
    resolved_value: str
    confidence: float
    rationale: str
    source: str = "lumen_answer_interpreter"
    needs_followup: bool = False
    memory_update: dict[str, Any] = field(default_factory=dict)
    answer_kind: str = "value"
    scope: str = ""
    profile_key: str = ""
    profile_value: str = ""


class LumenAnswerInterpreter:
    """Deterministic v1 interpreter for user answers to operator questions."""

    def interpret(
        self,
        *,
        raw_answer: str,
        field_key: str,
        label: str,
        options: list[str] | None = None,
        profile: dict[str, Any] | None = None,
        resume_facts: dict[str, Any] | None = None,
        company: str | None = None,
        model_client: Any | None = None,
    ) -> InterpretedAnswer:
        answer = (raw_answer or "").strip()
        profile = profile or {}
        resume_facts = resume_facts or {}
        options = [str(option).strip() for option in (options or []) if str(option).strip()]
        skip_intent = self._classify_skip_answer(
            raw_answer=answer,
            label=label,
            options=options,
            model_client=model_client,
        )
        if skip_intent.get("answer_kind") == "skip":
            scope = "always" if skip_intent.get("scope") == "always" else "application"
            return InterpretedAnswer(
                SKIP_ANSWER_TEXT,
                float(skip_intent.get("confidence") or 0.9),
                str(skip_intent.get("rationale") or "Lumen classified the user's answer as an instruction to skip this field."),
                source="lumen_answer_interpreter_skip",
                answer_kind="skip",
                scope=scope,
                memory_update={},
            )
        lowered = answer.lower()
        key = field_key.lower()
        label_l = label.lower()
        normalized_key = normalize_answer_key(field_key, label)

        if key == "email" or "email" in label_l:
            extracted = self._extract_email(answer)
            if extracted:
                return InterpretedAnswer(extracted, 0.99, "Lumen extracted an email address from your answer.", memory_update={"profile": {"email": extracted}})
            return InterpretedAnswer(answer, 0.4, "Lumen could not find a valid email address in your answer.", needs_followup=True)

        if normalized_key == "salary_minimum_base_usd":
            value, error = normalize_answer_value(field_key=field_key, label=label, value=answer, options=options)
            if not error:
                return InterpretedAnswer(value, 0.97, "Lumen normalized the salary answer to numeric USD only.", memory_update={"profile": {"salary_expectation": value}, "profile_json": {"salary_minimum_base_usd": value}})
            profile_salary = profile.get("salary_minimum_base_usd") or profile.get("salary_expectation")
            value, error = normalize_answer_value(field_key=field_key, label=label, value=profile_salary, options=options)
            if not error:
                return InterpretedAnswer(value, 0.94, "Lumen reused the saved profile salary answer.", memory_update={"profile_json": {"salary_minimum_base_usd": value}})
            return InterpretedAnswer(answer, 0.35, error or "Lumen could not normalize the salary answer.", needs_followup=True)

        if normalized_key == "relocation_assistance_required":
            value, error = normalize_answer_value(field_key=field_key, label=label, value=answer, options=options)
            if not error:
                return InterpretedAnswer(
                    value,
                    0.96,
                    "Lumen interpreted the relocation assistance answer as a yes/no response.",
                    memory_update={"profile_json": {"relocation_assistance_required": value}},
                )
            return InterpretedAnswer(answer, 0.35, error or "Lumen could not normalize the relocation assistance answer.", needs_followup=True)

        if normalized_key == "location_city_candidate_location":
            value, error = normalize_answer_value(field_key=field_key, label=label, value=answer, options=options)
            if not error:
                return InterpretedAnswer(value, 0.96, "Lumen normalized the candidate location answer.", memory_update={"profile": {"location": value}, "profile_json": {"location_city_candidate_location": value}})

        if key == "referral_source" or "how did you first learn" in label_l or "how did you hear" in label_l or "source" in label_l:
            option = self._resolve_option(answer, options, preferred_terms=["linkedin", "linked in"])
            if option:
                return InterpretedAnswer(option, 0.94, f"Lumen matched your flexible answer to the available source option '{option}'.", memory_update={"profile_json": {"referral_source": option}})
            if "linkedin" in lowered or "linked in" in lowered:
                return InterpretedAnswer("LinkedIn", 0.9, "Lumen resolved the source answer to LinkedIn from your instruction.", memory_update={"profile_json": {"referral_source": "LinkedIn"}})
            if "company" in lowered or "website" in lowered:
                return InterpretedAnswer("Company Website", 0.86, "Lumen resolved the source answer to Company Website.", memory_update={"profile_json": {"referral_source": "Company Website"}})
            if options and any(term in lowered for term in ("any", "whatever", "listed", "fine")):
                return InterpretedAnswer(options[0], 0.78, f"Lumen selected the first available listed option: {options[0]}.", memory_update={"profile_json": {"referral_source": options[0]}})
            return InterpretedAnswer(answer, 0.62, "Lumen preserved the source answer but confidence is limited.", needs_followup=not bool(answer))

        if key == "previous_employment" or "previously" in label_l and "employed" in label_l:
            if self._is_yes(answer):
                return InterpretedAnswer("Yes", 0.9, "Lumen interpreted your answer as prior employment.")
            if self._is_no(answer):
                return InterpretedAnswer("No", 0.95, "Lumen interpreted your answer as no prior employment.")
            inferred = self.infer_previous_employment(company=company, resume_facts=resume_facts)
            if inferred:
                return inferred

        if key in {"sponsorship", "requires_work_authorization", "eu_citizen", "permanent_work_authorization"}:
            if self._is_yes(answer):
                return InterpretedAnswer("Yes", 0.96, "Lumen interpreted your answer as yes.")
            if self._is_no(answer):
                return InterpretedAnswer("No", 0.96, "Lumen interpreted your answer as no.")

        if key in {
            "ai_coding_assistant_experience",
            "full_stack_web_application_experience",
            "ai_engineering_big_data_cloud_experience",
        } or "ai coding assistant" in label_l or "full-stack web application" in label_l:
            if self._is_yes(answer):
                return InterpretedAnswer("Yes", 0.94, "Lumen interpreted this experience question as yes.")
            if self._is_no(answer):
                return InterpretedAnswer("No", 0.94, "Lumen interpreted this experience question as no.")

        years_answer = self._extract_years_answer(answer, field_key=key, label=label_l)
        if years_answer:
            return InterpretedAnswer(years_answer, 0.9, "Lumen extracted the years-of-experience value from your answer.")

        if key in {"nys_tax_withholding_acknowledgement", "reference_calls_acknowledgement"} or "nys tax" in label_l or "reference calls" in label_l:
            if "acknowledge" in lowered:
                return InterpretedAnswer("I acknowledge", 0.94, "Lumen interpreted this as an acknowledgement.")
            if self._is_yes(answer):
                if any(option.lower() == "i acknowledge" for option in options):
                    return InterpretedAnswer("I acknowledge", 0.93, "Lumen mapped your yes answer to the required acknowledgement option.")
                return InterpretedAnswer("Yes", 0.9, "Lumen interpreted this as approval/acknowledgement.")

        if key == "pdf_resume_acknowledgement" or "pdf version of your resume" in label_l:
            option = self._resolve_option(answer, options, preferred_terms=["pdf", "submitted"])
            if option:
                return InterpretedAnswer(option, 0.98, f"Lumen matched the resume-format acknowledgement to '{option}'.")
            if "pdf" in lowered or self._is_yes(answer):
                return InterpretedAnswer("I have submitted a PDF version of my resume.", 0.96, "Lumen resolved this as the PDF resume acknowledgement.")

        option = self._resolve_option(answer, options)
        if option:
            return InterpretedAnswer(option, 0.9, f"Lumen matched your answer to the available option '{option}'.")

        return InterpretedAnswer(answer, 0.84 if answer else 0.0, "Lumen used your answer as provided.", needs_followup=not bool(answer))

    def _classify_skip_answer(
        self,
        *,
        raw_answer: str,
        label: str,
        options: list[str],
        model_client: Any | None,
    ) -> dict[str, Any]:
        """Model-owned classifier for explicit skip preferences.

        This intentionally has no keyword fallback: if the model path is unavailable, the answer is
        treated as a value/clarification and the normal interpreter continues.
        """
        answer = (raw_answer or "").strip()
        if not answer or model_client is None:
            return {"answer_kind": "value", "scope": "", "confidence": 0.0, "rationale": "No classifier input."}
        schema = {
            "name": "lumen_answer_skip_intent",
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "answer_kind": {"type": "string", "enum": ["value", "skip"]},
                    "scope": {"type": "string", "enum": ["", "application", "always"]},
                    "confidence": {"type": "number"},
                    "rationale": {"type": "string"},
                },
                "required": ["answer_kind", "scope", "confidence", "rationale"],
            },
        }
        try:
            result = model_client._call_model_json(
                purpose="answer_skip_intent",
                system=(
                    "You classify a user's reply to one pending browser-form question. Return strict JSON only. "
                    "Decide only whether the user explicitly instructed Lumen to leave this field blank/skip it, "
                    "or whether the reply is a value, clarification, question, or anything else."
                ),
                prompt={
                    "instruction": (
                        "Return answer_kind='skip' only when the user is explicitly telling Lumen not to fill "
                        "this pending field. Use scope='always' only when they also ask to remember/always/never "
                        "fill this kind of field in future; otherwise use scope='application'. For all actual "
                        "answers, option selections, opt-out disclosure choices, clarifying questions, or "
                        "ambiguous text, return answer_kind='value' and scope=''."
                    ),
                    "pending_question": str(label or ""),
                    "visible_options": options,
                    "user_reply": answer,
                },
                schema=schema,
            )
        except Exception:  # noqa: BLE001 - skip detection fails closed
            return {"answer_kind": "value", "scope": "", "confidence": 0.0, "rationale": "Skip classifier failed."}
        data = result.get("data") if isinstance(result, dict) and result.get("ok") else {}
        if not isinstance(data, dict):
            return {"answer_kind": "value", "scope": "", "confidence": 0.0, "rationale": "Skip classifier unavailable."}
        answer_kind = str(data.get("answer_kind") or "").strip().lower()
        if answer_kind != "skip":
            return {
                "answer_kind": "value",
                "scope": "",
                "confidence": float(data.get("confidence") or 0.0) if isinstance(data.get("confidence"), (int, float)) else 0.0,
                "rationale": str(data.get("rationale") or ""),
            }
        try:
            confidence = max(0.0, min(1.0, float(data.get("confidence") or 0.0)))
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < 0.55:
            return {"answer_kind": "value", "scope": "", "confidence": confidence, "rationale": str(data.get("rationale") or "")}
        scope = "always" if str(data.get("scope") or "").strip().lower() == "always" else "application"
        return {
            "answer_kind": "skip",
            "scope": scope,
            "confidence": confidence,
            "rationale": str(data.get("rationale") or "Explicit skip instruction."),
        }

    def infer_previous_employment(self, *, company: str | None, resume_facts: dict[str, Any]) -> InterpretedAnswer | None:
        company = (company or "").strip()
        if not company:
            return None
        known = [str(item).lower() for item in resume_facts.get("known_employers", [])]
        if known and company.lower() not in known:
            return InterpretedAnswer(
                "No",
                0.9,
                f"Lumen inferred no prior employment at {company} because it is absent from known resume employers.",
                source="resume_profile_inference",
                memory_update={"inferred_answers": {"previous_employment": {"company": company, "answer": "No"}}},
            )
        return None

    def _resolve_option(self, answer: str, options: list[str], preferred_terms: list[str] | None = None) -> str | None:
        if not options:
            return None
        answer_l = answer.lower()
        for term in preferred_terms or []:
            if term in answer_l:
                for option in options:
                    if term.replace(" ", "") in option.lower().replace(" ", ""):
                        return option
        for option in options:
            option_l = option.lower()
            if option_l in answer_l or answer_l in option_l:
                return option
        best = max(options, key=lambda option: SequenceMatcher(a=answer_l, b=option.lower()).ratio())
        if SequenceMatcher(a=answer_l, b=best.lower()).ratio() >= 0.72:
            return best
        return None

    def _extract_email(self, value: str) -> str | None:
        match = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", value, re.IGNORECASE)
        return match.group(0) if match else None

    def _extract_years_answer(self, value: str, *, field_key: str, label: str) -> str | None:
        context = f"{field_key} {label}".lower()
        if not re.search(r"\b(?:years|yrs?)\b", context):
            return None
        text = re.sub(r"\s+", " ", value or "").strip()
        if not text:
            return None
        with_unit = re.search(r"\b(\d+(?:\.\d+)?\s*\+?(?:\s*-\s*\d+(?:\.\d+)?)?)\s*(years?|yrs?)\b", text, re.IGNORECASE)
        if with_unit:
            amount = re.sub(r"\s+", "", with_unit.group(1))
            unit = "years"
            if "-" in amount:
                amount = re.sub(r"\s*-\s*", "-", with_unit.group(1)).replace(" ", "")
            return f"{amount} {unit}"
        bare = re.search(r"\b(\d+(?:\.\d+)?\s*\+?)\b", text)
        if bare:
            amount = bare.group(1).replace(" ", "")
            return f"{amount} years"
        return None

    def _is_yes(self, value: str) -> bool:
        normalized = value.strip().lower()
        return normalized in {"yes", "y", "yeah", "yep", "true", "correct", "affirmative"} or bool(re.match(r"^(yes|yeah|yep)\b", normalized))

    def _is_no(self, value: str) -> bool:
        normalized = value.strip().lower()
        return normalized in {"no", "n", "nope", "false", "never"} or normalized.startswith("no,") or normalized.startswith("no ")
