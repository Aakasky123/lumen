from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserProfile(Base):
    __tablename__ = "user_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    phone: Mapped[str | None] = mapped_column(String(64))
    location: Mapped[str | None] = mapped_column(String(255))
    linkedin: Mapped[str | None] = mapped_column(String(512))
    github: Mapped[str | None] = mapped_column(String(512))
    portfolio: Mapped[str | None] = mapped_column(String(512))
    work_authorization: Mapped[str | None] = mapped_column(String(255))
    sponsorship_status: Mapped[str | None] = mapped_column(String(255))
    salary_expectation: Mapped[str | None] = mapped_column(String(255))
    relocation_preference: Mapped[str | None] = mapped_column(String(255))
    profile_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # Agent BEHAVIOR preferences (how the loop runs for this user) — kept separate from profile_json so
    # they are never mined as candidate facts. See app.agent_settings.
    agent_settings_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CredentialVaultItem(Base):
    __tablename__ = "credential_vault_items"
    __table_args__ = (
        UniqueConstraint("user_id", "origin", "account_label", name="uq_credential_vault_user_origin_label"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    origin: Mapped[str] = mapped_column(String(512), index=True)
    account_label: Mapped[str] = mapped_column(String(128))
    encrypted_payload_json: Mapped[str] = mapped_column(Text)
    available_slots_json: Mapped[list] = mapped_column(JSON, default=list)
    auto_use: Mapped[bool] = mapped_column(Boolean, default=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Resume(Base):
    __tablename__ = "resumes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    base_resume_text: Mapped[str] = mapped_column(Text, default="")
    latex_source: Mapped[str] = mapped_column(Text, default="")
    pdf_url: Mapped[str | None] = mapped_column(String(1024))
    is_base: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class JobPosting(Base):
    __tablename__ = "job_postings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    url: Mapped[str] = mapped_column(String(2048))
    company: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str | None] = mapped_column(String(255))
    location: Mapped[str | None] = mapped_column(String(255))
    employment_type: Mapped[str | None] = mapped_column(String(255))
    description_text: Mapped[str] = mapped_column(Text, default="")
    requirements_json: Mapped[dict] = mapped_column(JSON, default=dict)
    skills_json: Mapped[dict] = mapped_column(JSON, default=dict)
    source_platform: Mapped[str] = mapped_column(String(64), default="generic")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class JobAnalysis(Base):
    __tablename__ = "job_analysis"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_posting_id: Mapped[int] = mapped_column(ForeignKey("job_postings.id"), index=True)
    ingestion_status: Mapped[str] = mapped_column(String(64), default="PENDING")
    role_category: Mapped[str | None] = mapped_column(String(255))
    seniority_estimate: Mapped[str | None] = mapped_column(String(255))
    core_skills_json: Mapped[list] = mapped_column(JSON, default=list)
    preferred_skills_json: Mapped[list] = mapped_column(JSON, default=list)
    missing_keywords_json: Mapped[list] = mapped_column(JSON, default=list)
    resume_tailoring_strategy: Mapped[str] = mapped_column(Text, default="")
    application_complexity_estimate: Mapped[str | None] = mapped_column(String(64))
    fit_score: Mapped[float] = mapped_column(Float, default=0)
    confidence: Mapped[float] = mapped_column(Float, default=0)
    warnings_json: Mapped[list] = mapped_column(JSON, default=list)
    raw_analysis_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ResumeVersion(Base):
    __tablename__ = "resume_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_posting_id: Mapped[int] = mapped_column(ForeignKey("job_postings.id"), index=True)
    resume_id: Mapped[int | None] = mapped_column(ForeignKey("resumes.id"), nullable=True)
    tailored_latex: Mapped[str] = mapped_column(Text, default="")
    tailored_pdf_url: Mapped[str | None] = mapped_column(String(1024))
    ats_score: Mapped[float] = mapped_column(Float, default=0)
    keyword_report_json: Mapped[dict] = mapped_column(JSON, default=dict)
    changed_bullet_summary: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ResumeArtifact(Base):
    __tablename__ = "resume_artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    resume_id: Mapped[int | None] = mapped_column(ForeignKey("resumes.id"), nullable=True, index=True)
    resume_version_id: Mapped[int | None] = mapped_column(ForeignKey("resume_versions.id"), nullable=True, index=True)
    artifact_type: Mapped[str] = mapped_column(String(64), index=True)
    source_path: Mapped[str | None] = mapped_column(String(1024))
    pdf_path: Mapped[str | None] = mapped_column(String(1024))
    log_path: Mapped[str | None] = mapped_column(String(1024))
    compiler_used: Mapped[str | None] = mapped_column(String(64))
    compile_status: Mapped[str] = mapped_column(String(64), default="PENDING", index=True)
    compile_error: Mapped[str | None] = mapped_column(Text)
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApplicationArtifact(Base):
    __tablename__ = "application_artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("operator_runs.id"), nullable=True, index=True)
    application_id: Mapped[int | None] = mapped_column(ForeignKey("applications.id"), nullable=True, index=True)
    job_posting_id: Mapped[int | None] = mapped_column(ForeignKey("job_postings.id"), nullable=True, index=True)
    artifact_kind: Mapped[str] = mapped_column(String(64), index=True)
    source: Mapped[str] = mapped_column(String(64), default="uploaded", index=True)
    filename: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(255), default="application/octet-stream")
    storage_path: Mapped[str | None] = mapped_column(String(1024))
    text_content: Mapped[str] = mapped_column(Text, default="")
    text_preview: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(64), default="active", index=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    job_posting_id: Mapped[int | None] = mapped_column(ForeignKey("job_postings.id"), nullable=True)
    resume_version_id: Mapped[int | None] = mapped_column(ForeignKey("resume_versions.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(64), default="draft")
    current_step: Mapped[str] = mapped_column(String(128), default="created")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AnswerMemory(Base):
    __tablename__ = "answer_memory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    question_pattern: Mapped[str] = mapped_column(Text)
    approved_answer: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OperatorRun(Base):
    __tablename__ = "operator_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[int | None] = mapped_column(ForeignKey("applications.id"), nullable=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    goal: Mapped[str] = mapped_column(Text)
    target_url: Mapped[str] = mapped_column(String(2048))
    current_state: Mapped[str] = mapped_column(String(64), index=True, default="CREATED")
    active_plan: Mapped[list] = mapped_column(JSON, default=list)
    confidence_state: Mapped[dict] = mapped_column(JSON, default=dict)
    risk_level: Mapped[str] = mapped_column(String(32), default="medium")
    cost_estimate: Mapped[float] = mapped_column(Float, default=0)
    status_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RuntimeTask(Base):
    __tablename__ = "runtime_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("operator_runs.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(64), default="pending")
    task_type: Mapped[str] = mapped_column(String(64), default="operator")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=120)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CognitiveEvent(Base):
    __tablename__ = "cognitive_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("operator_runs.id"), nullable=True, index=True)
    application_id: Mapped[int | None] = mapped_column(ForeignKey("applications.id"), nullable=True)
    event_type: Mapped[str] = mapped_column(String(128), index=True)
    event_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    actor: Mapped[str] = mapped_column(String(128), default="runtime")
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    risk_level: Mapped[str] = mapped_column(String(32), default="low")
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)


class ModelProvider(Base):
    __tablename__ = "model_providers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    display_name: Mapped[str] = mapped_column(String(255))
    provider_type: Mapped[str] = mapped_column(String(64), index=True)
    model_name: Mapped[str] = mapped_column(String(255))
    base_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    encrypted_api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_key_hint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    temperature: Mapped[float] = mapped_column(Float, default=0.2)
    max_tokens: Mapped[int] = mapped_column(Integer, default=2048)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=30)
    # low | medium | high, or NULL for the provider's own default reasoning behavior.
    reasoning_effort: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # How a JSON-schema call asks for structure: "strict" (json_schema, the default when NULL) or
    # "json_object" (schema enforced only by Lumen's own validation). Chosen per model by measurement.
    structured_output: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # OpenRouter endpoint tag tried first for this model (e.g. "openai/fast"), falling back to
    # OpenRouter's normal routing when it is down. NULL keeps OpenRouter's price-weighted routing.
    endpoint_preference: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # JSON object of OpenRouter routing knobs: allow_fallbacks, require_parameters, sort.
    endpoint_routing: Mapped[str | None] = mapped_column(String(256), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    health_status: Mapped[str] = mapped_column(String(64), default="unknown", index=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class VisionDecisionProfile(Base):
    __tablename__ = "vision_decision_profiles"
    __table_args__ = (UniqueConstraint("provider_type", "model_name", name="uq_vision_decision_profile_provider_model"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_type: Mapped[str] = mapped_column(String(64), index=True)
    model_name: Mapped[str] = mapped_column(String(255), index=True)
    ewma_latency_seconds: Mapped[float] = mapped_column(Float, default=0)
    token_budget: Mapped[int] = mapped_column(Integer, default=0)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChatThread(Base):
    __tablename__ = "chat_threads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("operator_runs.id"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(255), default="Lumen Operator Chat")
    mode: Mapped[str] = mapped_column(String(64), default="global")
    status: Mapped[str] = mapped_column(String(64), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    thread_id: Mapped[int] = mapped_column(ForeignKey("chat_threads.id"), index=True)
    role: Mapped[str] = mapped_column(String(32))
    content: Mapped[str] = mapped_column(Text, default="")
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    linked_event_id: Mapped[int | None] = mapped_column(ForeignKey("cognitive_events.id"), nullable=True)
    linked_task_id: Mapped[int | None] = mapped_column(ForeignKey("runtime_tasks.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OperatorQuestion(Base):
    __tablename__ = "operator_questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("operator_runs.id"), index=True)
    thread_id: Mapped[int | None] = mapped_column(ForeignKey("chat_threads.id"), nullable=True, index=True)
    question_type: Mapped[str] = mapped_column(String(64), index=True)
    stuck_phase: Mapped[str] = mapped_column(String(128), default="")
    stuck_reason: Mapped[str] = mapped_column(Text, default="")
    prompt: Mapped[str] = mapped_column(Text, default="")
    expected_answer_schema: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(64), default="pending", index=True)
    answer_payload: Mapped[dict] = mapped_column(JSON, default=dict)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PairedDevice(Base):
    """A phone paired as a remote for this backend (see app.remote_access).

    Only the SHA-256 of the bearer token is stored. ``public_key_spki`` is the device's
    hardware-backed signing key; step-up requests must carry its signature over a one-time
    server challenge.
    """

    __tablename__ = "paired_devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    platform: Mapped[str] = mapped_column(String(32), default="android")
    token_sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    public_key_spki: Mapped[str] = mapped_column(Text)
    push_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Until when tasks this phone starts may run with Full autonomy (a fingerprint unlock).
    autonomy_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
