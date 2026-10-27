from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


class AnswerMemoryUpdate(BaseModel):
    answer: str


class ModelProviderCreate(BaseModel):
    display_name: str
    provider_type: str
    model_name: str
    base_url: str | None = None
    api_key: str | None = None
    temperature: float = 0.2
    max_tokens: int = 2048
    timeout_seconds: int = 30
    reasoning_effort: str | None = None
    structured_output: str | None = None
    endpoint_preference: str | None = None
    endpoint_routing: dict[str, Any] | None = None
    enabled: bool = True
    # Reuse the saved key of an existing provider of the same type and base URL (the key never
    # leaves the server): another model on the same account.
    key_from_provider_id: int | None = None


class ModelProviderAvailableModels(BaseModel):
    provider_type: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    key_from_provider_id: int | None = None


class AgentSettingsUpdate(BaseModel):
    # Partial update. Null provider ids clear their designations. The three seats: Brain
    # (vision_provider_id, which also activates the row), Checker (judge_provider_id) and Escalation.
    vision_provider_id: int | None = None
    vision_escalation_provider_id: int | None = None
    judge_provider_id: int | None = None
    strict_model_provider_allowlist: bool | None = None
    final_action_mode: Literal["approval", "autonomous"] | None = None
    autonomous_attestation_consent: bool | None = None
    autonomous_relay_consent: bool | None = None
    totp_capability: bool | None = None
    max_run_seconds: float | None = None
    max_run_tokens: int | None = None
    max_run_cost_usd: float | None = None


CredentialVaultSlot = Literal["username", "email", "password", "totp"]


class CredentialVaultFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: SecretStr | None = Field(default=None, min_length=1, max_length=2048)
    email: SecretStr | None = Field(default=None, min_length=1, max_length=2048)
    password: SecretStr | None = Field(default=None, min_length=1, max_length=2048)
    # A TOTP seed (base32 or an otpauth://totp URI); the runtime computes codes from it, it is never typed.
    totp: SecretStr | None = Field(default=None, min_length=1, max_length=2048)

    @model_validator(mode="after")
    def require_field(self) -> "CredentialVaultFields":
        if not self.model_fields_set:
            raise ValueError("At least one credential field is required.")
        return self

    def revealed(self) -> dict[CredentialVaultSlot, str | None]:
        values: dict[CredentialVaultSlot, str | None] = {}
        for slot in ("username", "email", "password", "totp"):
            if slot not in self.model_fields_set:
                continue
            value = getattr(self, slot)
            values[slot] = value.get_secret_value() if value is not None else None
        return values


class CredentialVaultCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    origin: str = Field(min_length=1, max_length=512)
    account_label: str = Field(min_length=1, max_length=128)
    fields: CredentialVaultFields
    auto_use: bool = True

    @model_validator(mode="after")
    def require_non_null_field(self) -> "CredentialVaultCreate":
        if not any(value is not None for value in self.fields.revealed().values()):
            raise ValueError("At least one non-null credential field is required.")
        return self


class CredentialVaultUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    origin: str | None = Field(default=None, min_length=1, max_length=512)
    account_label: str | None = Field(default=None, min_length=1, max_length=128)
    fields: CredentialVaultFields | None = None
    auto_use: bool | None = None

    @model_validator(mode="after")
    def require_update(self) -> "CredentialVaultUpdate":
        if not self.model_fields_set:
            raise ValueError("At least one credential vault update is required.")
        for field_name in ("origin", "account_label", "fields", "auto_use"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} cannot be null.")
        return self


class CredentialVaultItemOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    origin: str
    account_label: str
    available_slots: list[CredentialVaultSlot]
    auto_use: bool
    created_at: datetime
    updated_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class CredentialVaultListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[CredentialVaultItemOut]


class CredentialVaultDeleteOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deleted: bool
    id: int


class ProfileUpsert(BaseModel):
    name: str = "Candidate"
    email: str = "candidate@example.com"
    phone: str | None = None
    location: str | None = None
    linkedin: str | None = None
    github: str | None = None
    portfolio: str | None = None
    work_authorization: str | None = None
    sponsorship_status: str | None = None
    salary_expectation: str | None = None
    relocation_preference: str | None = None
    profile_json: dict[str, Any] = Field(default_factory=dict)


class ClosedWorldAttestationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Literal["employment_history"]
    set_key: Literal["employers"]
    members: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_members(self) -> "ClosedWorldAttestationCreate":
        if any(not str(member or "").strip() for member in self.members):
            raise ValueError("members cannot contain an empty value")
        if any(len(str(member)) > 240 for member in self.members):
            raise ValueError("each member must be at most 240 characters")
        return self


class ClosedWorldAttestationOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    scope: str
    set_key: str
    members: list[str]
    exhaustive: bool
    status: Literal["active", "revoked"]
    attested_by: Literal["user"]
    source: Literal["profile_attestation_api"]
    version: int
    created_at: str
    updated_at: str
    revoked_at: str | None = None


class ClosedWorldAttestationEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attestation: ClosedWorldAttestationOut


class ClosedWorldAttestationListOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ClosedWorldAttestationOut]


class ChatThreadCreate(BaseModel):
    title: str | None = None


class ChatMessageCreate(BaseModel):
    content: str
    payload: dict[str, Any] = Field(default_factory=dict)


class SensitiveBrowserContinueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["sensitive_browser_continue"]
    handoff_id: str = Field(min_length=16, max_length=128)


class OperatorQuestionAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str
    payload: dict[str, Any] = Field(default_factory=dict)
    auto_resume: bool = True
    save_scope: str = "one_time"
    approval_token: str | None = Field(default=None, min_length=16, max_length=256)
    final_action_decision: Literal["approve", "decline"] | None = None


class GenerateQuestionArtifactRequest(BaseModel):
    instructions: str | None = None
    artifact_kind: str | None = None


class ApplyQuestionArtifactRequest(BaseModel):
    auto_resume: bool = True


class UpdateApplicationArtifactRequest(BaseModel):
    text_content: str | None = None


class OperatorRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    application_id: int | None
    user_id: int
    goal: str
    target_url: str
    current_state: str
    active_plan: list
    confidence_state: dict
    risk_level: str
    cost_estimate: float
    status_message: str
    created_at: datetime
    updated_at: datetime

    @field_validator("confidence_state", mode="after")
    @classmethod
    def _without_run_journal(cls, value: dict) -> dict:
        # The brain's run journal (notes, plan, and up to 24 pages of seen text, codes included) is loop
        # memory, not run status: it is never served on the run endpoints clients poll. Nor is the downloads
        # snapshot (file names from the user's downloads folder and the token the downloads endpoint checks).
        return {key: item for key, item in value.items() if key not in {"vision_run_journal", "downloads_at_start"}}


class CognitiveEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    run_id: int | None
    application_id: int | None
    event_type: str
    event_payload: dict
    actor: str
    confidence: float
    risk_level: str
    timestamp: datetime


class ChatThreadOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    run_id: int | None
    title: str
    mode: str
    status: str
    created_at: datetime
    updated_at: datetime
    # Model spend recorded for the thread's run(s), in USD; None when the thread has no run.
    cost_usd: float | None = None


class ChatMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    thread_id: int
    role: str
    content: str
    payload_json: dict
    linked_event_id: int | None
    linked_task_id: int | None
    created_at: datetime


class OperatorQuestionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    run_id: int
    thread_id: int | None
    question_type: str
    stuck_phase: str
    stuck_reason: str
    prompt: str
    expected_answer_schema: dict
    status: str
    answer_payload: dict
    applied_at: datetime | None
    resumed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ApplicationArtifactOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    run_id: int | None
    application_id: int | None
    job_posting_id: int | None
    artifact_kind: str
    source: str
    filename: str
    mime_type: str
    storage_path: str | None
    text_content: str
    text_preview: str
    status: str
    metadata_json: dict
    file_size: int
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None


class DevicePairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=16, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    platform: Literal["android", "ios"] = "android"
    # Base64 SubjectPublicKeyInfo DER of the device's hardware-backed signing key.
    public_key: str = Field(min_length=64, max_length=4096)


class PairedDeviceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    platform: str
    has_push: bool
    autonomy_until: datetime | None = None
    last_seen_at: datetime | None
    created_at: datetime


class DevicePushTokenUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    push_token: str | None = Field(default=None, max_length=4096)


class MemoryFactCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    label: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=500)
    section: str | None = Field(default=None, max_length=32)


class MemoryFactUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    label: str | None = Field(default=None, min_length=1, max_length=80)
    value: str | None = Field(default=None, min_length=1, max_length=500)
    section: str | None = Field(default=None, max_length=32)


class BenchLanePinRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: int
    device_id: str = Field(min_length=1, max_length=80)
